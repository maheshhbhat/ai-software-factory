#!/usr/bin/env python3
"""Audited Claude and Codex CLI adapters.

Agents provide opaque payloads and validate outputs.  Only this module knows
provider command syntax or maps provider diagnostics to shared failure reasons.
"""

from __future__ import annotations

import pathlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass

from .base import AttemptResult, ProviderAdapter


@dataclass(frozen=True)
class InvocationPayload:
    text: str
    output_schema: dict | None = None
    schema_path: pathlib.Path | None = None
    output_path: pathlib.Path | None = None
    access: str = "read-only"
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    network_access: bool = False
    skip_git_repo_check: bool = False


def classify_failure(text: str, returncode: int) -> str:
    value = text.lower()
    if "rate limit" in value or "429" in value:
        return "rate-limit"
    if "quota" in value or "usage limit" in value or "session limit" in value:
        return "quota"
    if "auth" in value or "login" in value or "unauthorized" in value:
        return "auth"
    if "not found" in value or returncode == 127:
        return "missing-executable"
    if "unavailable" in value or "connection" in value:
        return "unavailable"
    return "unknown-failure"


BASE_ENVIRONMENT = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL"})
PROVIDER_ENVIRONMENT = {
    "openai": frozenset({"HOME", "OPENAI_API_KEY", "CODEX_HOME"}),
    "anthropic": frozenset({
        "HOME", "USER", "LOGNAME", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    }),
    "meta": frozenset({"HOME", "USER", "LOGNAME", "META_API_KEY", "MUSE_HOME"}),
}


def provider_environment(provider: str, environ=None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    if provider not in PROVIDER_ENVIRONMENT:
        raise ValueError(f"unsupported provider adapter: {provider}")
    allowed = BASE_ENVIRONMENT | PROVIDER_ENVIRONMENT[provider]
    return {key: value for key, value in environ.items() if key in allowed}


def claude_command(*, model: str, effort: str, payload: InvocationPayload,
                   budget_units: float) -> list[str]:
    permission = "acceptEdits" if payload.access == "workspace-write" else "dontAsk"
    command = ["claude", "-p", payload.text, "--model", model, "--effort", effort,
            "--max-budget-usd", str(budget_units), "--permission-mode", permission,
            "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
    if payload.output_schema is not None:
        command += ["--json-schema", json.dumps(
            payload.output_schema, separators=(",", ":"))]
    if payload.allowed_tools:
        tools = ",".join(payload.allowed_tools)
        command += ["--tools", tools, "--allowedTools", tools]
    if payload.disallowed_tools:
        command += ["--disallowedTools", ",".join(payload.disallowed_tools)]
    return command


def codex_command(*, model: str, effort: str, payload: InvocationPayload) -> list[str]:
    command = ["codex", "exec", "--model", model, "--config",
            f'model_reasoning_effort="{effort}"', "--sandbox", payload.access,
            "--ephemeral", "--ignore-user-config", "--ignore-rules"]
    if payload.network_access:
        command += ["--config", "sandbox_workspace_write.network_access=true"]
    if payload.skip_git_repo_check:
        command += ["--skip-git-repo-check"]
    if payload.schema_path is not None:
        command += ["--output-schema", str(payload.schema_path)]
    if payload.output_path is not None:
        command += ["--output-last-message", str(payload.output_path)]
    return command + ["--json", payload.text]


def muse_command(*, model: str, effort: str, payload: InvocationPayload) -> list[str]:
    # `muse exec` is Meta's headless mode. There is no monetary budget flag;
    # the step cap is the only run bound the CLI offers, so the shared
    # executor's reserved-budget accounting is the real spend control
    # (unreported usage consumes the full reservation). Approval and the OS
    # sandbox stay at their safe defaults; the sandbox is only relaxed for
    # workspace-write payloads, and network access must be asked for.
    command = ["muse", "exec", "--json", "--model", model,
               "--reasoning-effort", effort, "--no-session-log",
               "--no-foreign-personal-context", "--max-model-steps", "40",
               "--workspace", "."]
    if payload.access == "workspace-write":
        command += ["--disable-approval"]
    if payload.network_access:
        command += ["--sandbox-network", "full"]
    return command + [payload.text]


def cli_adapter(provider: str, *, cwd: pathlib.Path, environment: dict[str, str],
                runner=subprocess.run, mutation_state=lambda: "none") -> ProviderAdapter:
    if provider not in {"anthropic", "openai", "meta"}:
        raise ValueError(f"unsupported provider adapter: {provider}")

    def invoke(*, model, effort, timeout_seconds, budget_units, payload,
               working_directory=None):
        value = payload if isinstance(payload, InvocationPayload) else InvocationPayload(str(payload))
        if provider == "anthropic":
            command = claude_command(model=model, effort=effort, payload=value,
                                     budget_units=budget_units)
        elif provider == "meta":
            command = muse_command(model=model, effort=effort, payload=value)
        else:
            command = codex_command(model=model, effort=effort, payload=value)
        try:
            result = runner(command, cwd=str(working_directory or cwd), env=environment,
                            capture_output=True, text=True, timeout=timeout_seconds)
        except FileNotFoundError as exc:
            return AttemptResult("missing-executable", consumed_budget_units=0,
                                 diagnostic=str(exc)[:500])
        except subprocess.TimeoutExpired as exc:
            return AttemptResult("timeout", mutation_state=mutation_state(),
                                 diagnostic=str(exc)[:500])
        if result.returncode:
            diagnostic = ((result.stderr or "") + "\n" + (result.stdout or ""))[-500:]
            reported = None
            for line in (result.stdout or "").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = event.get("total_cost_usd") if isinstance(event, dict) else None
                if isinstance(value, (int, float)) and value >= 0:
                    reported = float(value)
            return AttemptResult(classify_failure(diagnostic, result.returncode),
                                 consumed_budget_units=reported,
                                 mutation_state=mutation_state(), diagnostic=diagnostic)
        output = result.stdout or ""
        if value.output_path is not None and value.output_path.exists():
            written = value.output_path.read_text(encoding="utf-8")
            if written.strip():
                output = written
        return AttemptResult("success", output, None)

    def probe(*, model, timeout_seconds, effort="low"):
        # A minimal claude-fable-5 reply costs ~$0.15, so a 0.1 cap makes the
        # probe fail on budget with valid credentials — indistinguishable in
        # the health store from an auth failure.
        #
        # The probe must not inherit repository instructions: run from a
        # neutral temporary directory. A probe launched from the factory
        # checkout loads the repo's CLAUDE.md/AGENTS.md, and an engine
        # following those rules can refuse to echo the token as a
        # fake-pass request (observed live, 2026-08-25). skip_git_repo_check
        # keeps the Codex adapter working outside a repository.
        with tempfile.TemporaryDirectory() as neutral:
            result = invoke(model=model, effort=effort,
                            timeout_seconds=timeout_seconds,
                            budget_units=0.5, working_directory=neutral,
                            payload=InvocationPayload("Reply exactly CAPACITY_OK",
                                                      skip_git_repo_check=True))
        def exact(value):
            if value == "CAPACITY_OK":
                return True
            if isinstance(value, dict):
                return any(exact(item) for item in value.values())
            if isinstance(value, list):
                return any(exact(item) for item in value)
            return False
        answered = any(line.strip() == "CAPACITY_OK"
                       for line in result.output.splitlines())
        for line in result.output.splitlines():
            try:
                answered = answered or exact(json.loads(line))
            except json.JSONDecodeError:
                pass
        return result.succeeded and answered

    return ProviderAdapter(provider, invoke, probe)
