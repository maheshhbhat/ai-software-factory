#!/usr/bin/env python3
"""Audited Claude and Codex CLI adapters.

Agents provide opaque payloads and validate outputs.  Only this module knows
provider command syntax or maps provider diagnostics to shared failure reasons.
"""

from __future__ import annotations

import pathlib
import subprocess

from .base import AttemptResult, ProviderAdapter


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


def claude_command(*, model: str, effort: str, payload: str,
                   budget_units: float) -> list[str]:
    return ["claude", "-p", payload, "--model", model, "--effort", effort,
            "--max-budget-usd", str(budget_units), "--permission-mode", "dontAsk",
            "--output-format", "json", "--no-session-persistence"]


def codex_command(*, model: str, effort: str, payload: str) -> list[str]:
    return ["codex", "exec", "--model", model, "--config",
            f'model_reasoning_effort="{effort}"', "--sandbox", "read-only",
            "--ephemeral", "--ignore-user-config", "--json", payload]


def cli_adapter(provider: str, *, cwd: pathlib.Path, environment: dict[str, str],
                runner=subprocess.run) -> ProviderAdapter:
    if provider not in {"anthropic", "openai"}:
        raise ValueError(f"unsupported provider adapter: {provider}")

    def invoke(*, model, effort, timeout_seconds, budget_units, payload):
        text = payload if isinstance(payload, str) else str(payload)
        command = (claude_command(model=model, effort=effort, payload=text,
                                  budget_units=budget_units)
                   if provider == "anthropic"
                   else codex_command(model=model, effort=effort, payload=text))
        try:
            result = runner(command, cwd=str(cwd), env=environment,
                            capture_output=True, text=True, timeout=timeout_seconds)
        except FileNotFoundError as exc:
            return AttemptResult("missing-executable", diagnostic=str(exc)[:500])
        except subprocess.TimeoutExpired as exc:
            return AttemptResult("timeout", diagnostic=str(exc)[:500])
        if result.returncode:
            diagnostic = ((result.stderr or "") + "\n" + (result.stdout or ""))[-500:]
            return AttemptResult(classify_failure(diagnostic, result.returncode),
                                 diagnostic=diagnostic)
        return AttemptResult("success", result.stdout or "", None)

    def probe(*, model, timeout_seconds):
        result = invoke(model=model, effort="low", timeout_seconds=timeout_seconds,
                        budget_units=0.1, payload="Reply exactly CAPACITY_OK")
        return result.succeeded and "CAPACITY_OK" in result.output

    return ProviderAdapter(provider, invoke, probe)
