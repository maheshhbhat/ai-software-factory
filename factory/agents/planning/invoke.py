#!/usr/bin/env python3
"""Headless planning invocation: artifact identity in, durable GitHub plan out."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "runtime"))
import artifacts  # noqa: E402
import contract  # noqa: E402
import streaming  # noqa: E402
import observability as obs  # noqa: E402
from factory.capacity_pool import router as capacity_pool  # noqa: E402

DEFAULT_TIMEOUT = 900
DEFAULT_MAX_USD = 5.0
DEFAULT_PRIMARY_MODEL = "claude-fable-5"
DEFAULT_PRIMARY_EFFORT = "medium"
DEFAULT_FALLBACK_MODEL = "gpt-5.6-sol"
DEFAULT_FALLBACK_EFFORT = "medium"
PRIMARY_ENVELOPE_SHARE = 0.8
FALLBACK_SECONDS_PER_BUDGET_UNIT = 180


class InvocationError(RuntimeError):
    pass


def labels_of(issue: dict) -> list[str]:
    return [label["name"] if isinstance(label, dict) else label
            for label in issue.get("labels", [])]


def state_version(client: artifacts.GitHubStore, issue: dict) -> str:
    """Stable across the invocation's own comments/body writes."""
    altitude = contract.select_altitude(set(labels_of(issue)))
    if altitude is contract.Altitude.CAMPAIGN:
        stable = json.dumps({"body": issue.get("body") or "", "labels": sorted(labels_of(issue))},
                            sort_keys=True).encode()
        return hashlib.sha256(stable).hexdigest()[:20]
    timeline = client._pages(f"/issues/{issue['number']}/timeline")
    lifecycle = [event for event in timeline if event.get("event") == "labeled"
                 and (event.get("label") or {}).get("name", "").startswith("project:")]
    if not lifecycle:
        raise InvocationError("project trigger has no durable lifecycle state version")
    latest = lifecycle[-1]
    return str(latest.get("id") or latest.get("created_at"))


def read_repository(client: artifacts.GitHubStore) -> tuple[str, list[dict], dict]:
    """Private-repository read preflight. No writer is called before this returns."""
    metadata = client._api("")
    branch = metadata.get("default_branch")
    if not branch:
        raise InvocationError("repository read constraint failed: default branch unavailable")
    tree = client._api(f"/git/trees/{branch}?recursive=1")
    files = sorted(item["path"] for item in tree.get("tree", [])
                   if item.get("type") == "blob")
    product_paths = [path for path in files if path.lower() == "product.md"]
    if len(product_paths) != 1:
        raise InvocationError("repository read constraint failed: product.md missing or ambiguous")

    def content(path):
        item = client._api(f"/contents/{path}")
        import base64
        return base64.b64decode(item["content"]).decode("utf-8")

    product = content(product_paths[0])
    adr_paths = [path for path in files
                 if path.lower().endswith(".md") and
                 ("/adr" in f"/{path.lower()}" or "/decisions/" in f"/{path.lower()}/")]
    adrs = [{"path": path, "content": content(path)} for path in adr_paths]
    source_paths = [path for path in files if path != product_paths[0] and
                    path.lower().endswith((".js", ".mjs", ".cjs", ".json", ".md",
                                           ".yml", ".yaml"))]
    sources, total = {}, 0
    for path in source_paths:
        text = content(path)
        total += len(text.encode())
        if total > 500_000:
            raise InvocationError(
                "repository read constraint failed: grounded source context exceeds 500KB")
        sources[path] = text
    return product, adrs, {"default_branch": branch, "files": files, "sources": sources}


def prompt_version() -> str:
    return hashlib.sha256(HERE.joinpath("prompt.md").read_bytes()).hexdigest()[:12]


def review_comments(client: artifacts.GitHubStore, number: int) -> list[dict]:
    """Ground revisions in human comments, excluding the agent's own artifacts."""
    return [{"id": item.get("id"), "author": (item.get("user") or {}).get("login"),
             "created_at": item.get("created_at"), "body": item.get("body") or ""}
            for item in client.list_comments(number)
            if f"<!-- {artifacts.MARKER}:" not in (item.get("body") or "")]


def feedback_version(comments: list[dict]) -> str:
    stable = json.dumps(comments, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(stable).hexdigest()[:12]


def existing_plan(client: artifacts.GitHubStore, artifact: int) -> dict:
    prefix = f"<!-- {artifacts.MARKER}:{artifact}:"
    issues = [{"number": item["number"], "title": item.get("title"),
               "labels": labels_of(item), "body": item.get("body") or ""}
              for item in client.list_issues("all")
              if prefix in (item.get("body") or "") and
              (":adr -->" in item["body"] or ":story:" in item["body"])]
    digests = [{"id": item.get("id"), "body": item.get("body") or ""}
               for item in client.list_comments(artifact)
               if prefix in (item.get("body") or "") and ":digest -->" in item["body"]]
    return {"issues": issues, "digests": digests}


def verify_with_retry(client, trigger, key, altitude, attempts=5, sleeper=time.sleep):
    """Retry GitHub's eventually-consistent issue listing for a bounded window."""
    error = None
    for attempt in range(attempts):
        try:
            return artifacts.verify(client, trigger, key, altitude)
        except artifacts.ArtifactError as exc:
            error = exc
            if attempt + 1 < attempts:
                sleeper(attempt + 1)
    raise error


def model_command(input_path: str, timeout: int, max_usd: float) -> list[str]:
    template = os.environ.get("FACTORY_PLANNING_MODEL_CMD", "").strip()
    if template:
        return [part.replace("{input_file}", input_path)
                .replace("{max_usd}", str(max_usd)).replace("{timeout}", str(timeout))
                for part in shlex.split(template)]
    prompt = HERE.joinpath("prompt.md").read_text()
    input_value = json.loads(pathlib.Path(input_path).read_text())
    altitude = contract.select_altitude(set(input_value["trigger"]["labels"]))
    payload = (prompt + "\n\n## Invocation input\n\n"
               + json.dumps(input_value, indent=2)
               + "\n\nReturn only the contract JSON object; do not write GitHub directly.")
    model = os.environ.get("FACTORY_PLANNING_PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL).strip()
    effort = os.environ.get("FACTORY_PLANNING_PRIMARY_EFFORT", DEFAULT_PRIMARY_EFFORT).strip()
    if not model or effort not in {"low", "medium", "high", "max"}:
        raise InvocationError("invalid planning primary model or effort configuration")
    return ["claude", "-p", payload, "--model", model, "--effort", effort,
            "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(contract.json_schema(altitude), separators=(",", ":")),
            "--max-budget-usd", str(max_usd), "--permission-mode", "dontAsk",
            "--no-session-persistence"]


def fallback_model_command(input_path: str, schema_path: str,
                           output_path: str, model: str,
                           effort: str) -> list[str]:
    """Build the independent Codex fallback without inheriting user tuning."""
    prompt = HERE.joinpath("prompt.md").read_text()
    input_value = json.loads(pathlib.Path(input_path).read_text())
    payload = (prompt + "\n\n## Invocation input\n\n"
               + json.dumps(input_value, indent=2)
               + "\n\nReturn only the contract JSON object; do not write GitHub directly.")
    return [
        "codex", "exec", "--model", model,
        "--config", f'model_reasoning_effort="{effort}"',
        "--sandbox", "read-only", "--ephemeral", "--ignore-user-config",
        "--ignore-rules", "--output-schema", schema_path,
        "--output-last-message", output_path, "--json", payload,
    ]


def _run(command: list[str], value: dict, timeout: int, runner) -> subprocess.CompletedProcess:
    if runner is subprocess.run:
        return streaming.run(
            command, cwd=ROOT, env=os.environ.copy(), timeout=timeout,
            component="planning-agent", operation="engine-stream",
            artifact=(value.get("trigger") or {}).get("number"))
    return runner(command, capture_output=True, text=True, timeout=timeout)


def _codex_stdout(result, output_path: pathlib.Path) -> str:
    """Prefer Codex's schema-bound final file; keep direct stdout testable."""
    if output_path.exists() and output_path.stat().st_size:
        return output_path.read_text(encoding="utf-8")
    return result.stdout or ""


def planning_route(timeout: int, max_usd: float) -> capacity_pool.RoutePlan:
    primary_model = os.environ.get(
        "FACTORY_PLANNING_PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL).strip()
    primary_effort = os.environ.get(
        "FACTORY_PLANNING_PRIMARY_EFFORT", DEFAULT_PRIMARY_EFFORT).strip()
    fallback_model = os.environ.get(
        "FACTORY_PLANNING_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL).strip()
    fallback_effort = os.environ.get(
        "FACTORY_PLANNING_FALLBACK_EFFORT", DEFAULT_FALLBACK_EFFORT).strip()
    if primary_effort != fallback_effort:
        raise InvocationError("planning route requires one effort across its logical task")
    supported = frozenset({"low", "medium", "high", "max"})
    if (not primary_model or not fallback_model or primary_effort not in supported
            or fallback_effort not in supported):
        raise InvocationError("invalid planning capacity route configuration")
    capabilities = frozenset({"reason", "json"})
    registry = (
        capacity_pool.ModelCapacity(
            primary_model, "anthropic", capacity_pool.Tier.FLAGSHIP,
            capabilities, supports_effort=supported, latency_rank=1),
        capacity_pool.ModelCapacity(
            fallback_model, "openai", capacity_pool.Tier.FLAGSHIP,
            capabilities, supports_effort=supported, latency_rank=2),
    )
    request = capacity_pool.RouteRequest(
        "planning", capabilities, capacity_pool.Tier.FLAGSHIP, primary_effort,
        timeout, max_usd, preferred_provider="anthropic")
    return capacity_pool.route(request, registry, max_steps=2)


def _failure_reason(result) -> str | None:
    if result is None:
        return "unavailable"
    text = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    if "session limit" in text or "quota" in text:
        return "quota"
    if "429" in text or "rate limit" in text or "rate-limit" in text:
        return "rate-limit"
    if any(marker in text for marker in ("authentication", "not logged in", "login required",
                                         "oauth", "token expired")):
        return "auth"
    return None


def _reported_cost(result) -> float | None:
    if result is None:
        return None
    values = []
    for line in (result.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = event.get("total_cost_usd") if isinstance(event, dict) else None
        if isinstance(value, (int, float)) and value >= 0:
            values.append(float(value))
    return values[-1] if values else None


def run_model(value: dict, timeout: int, max_usd: float,
              runner=subprocess.run, clock=time.monotonic) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as handle, \
            tempfile.NamedTemporaryFile("w", suffix=".schema.json", encoding="utf-8") as schema, \
            tempfile.NamedTemporaryFile("w", suffix=".result.json", encoding="utf-8") as output:
        json.dump(value, handle)
        handle.flush()
        custom_primary = bool(os.environ.get("FACTORY_PLANNING_MODEL_CMD", "").strip())
        plan = None if custom_primary else planning_route(timeout, max_usd)
        if plan is not None:
            obs.operational_log(
                "INFO", "planning capacity route selected",
                component="planning-agent", operation="capacity-route",
                primary_model=plan.primary.model,
                fallback_models=[step.model for step in plan.steps[1:]],
                effort=plan.primary.effort,
                total_timeout_seconds=plan.total_timeout_seconds,
                total_budget_units=plan.total_budget_units)
        if custom_primary:
            json.dump({}, schema)
        else:
            altitude = contract.select_altitude(
                set((value.get("trigger") or {}).get("labels", [])))
            json.dump(contract.json_schema(altitude), schema)
        schema.flush()
        output_path = pathlib.Path(output.name)
        primary_timeout = timeout
        primary_budget = max_usd
        if plan is not None and len(plan.steps) > 1:
            primary_timeout = max(1, int(timeout * PRIMARY_ENVELOPE_SHARE))
            primary_budget = max_usd * PRIMARY_ENVELOPE_SHARE
        command = model_command(handle.name, primary_timeout, primary_budget)
        started = clock()
        timed_out = False
        try:
            result = _run(command, value, primary_timeout, runner)
        except subprocess.TimeoutExpired as exc:
            if custom_primary:
                raise InvocationError(
                    f"planning timeout exhausted after {primary_timeout}s") from exc
            result = None
            timed_out = True
        except FileNotFoundError:
            if custom_primary:
                raise
            result = None
        if result is None or result.returncode != 0:
            if custom_primary:
                detail = "" if result is None else (result.stderr or "")[:300]
                code = "unavailable" if result is None else result.returncode
                raise InvocationError(f"planning model failed ({code}): {detail}")
            reason = "timeout" if timed_out else _failure_reason(result)
            if reason not in plan.fallback_on:
                detail = "" if result is None else (result.stderr or result.stdout or "")[:300]
                raise InvocationError(f"planning primary failed without eligible fallback: {detail}")
            elapsed = max(0, min(timeout, math.ceil(clock() - started)))
            if timed_out:
                elapsed = max(elapsed, primary_timeout)
            reported_cost = _reported_cost(result)
            if result is None and not timed_out:
                consumed = 0.0  # a missing executable cannot consume provider budget
            else:
                consumed = primary_budget if reported_cost is None else reported_cost
            remaining_time, remaining_budget = capacity_pool.remaining_envelope(
                plan, elapsed_seconds=elapsed, consumed_budget_units=consumed)
            fallback_timeout = min(
                remaining_time,
                int(remaining_budget * FALLBACK_SECONDS_PER_BUDGET_UNIT),
            )
            if fallback_timeout <= 0 or remaining_budget <= 0:
                raise InvocationError("planning capacity envelope exhausted before fallback")
            fallback_step = plan.steps[1]
            obs.operational_log(
                "WARNING", "planning capacity fallback activated",
                component="planning-agent", operation="capacity-route",
                reason=reason, model=fallback_step.model,
                provider=fallback_step.provider, effort=fallback_step.effort,
                elapsed_seconds=elapsed,
                remaining_budget_units=remaining_budget,
                attempt_timeout_seconds=fallback_timeout)
            fallback = fallback_model_command(
                handle.name, schema.name, output.name,
                fallback_step.model, fallback_step.effort)
            try:
                result = _run(fallback, value, fallback_timeout, runner)
            except subprocess.TimeoutExpired as exc:
                raise InvocationError(
                    f"planning fallback timeout exhausted after {fallback_timeout}s") from exc
            except FileNotFoundError as exc:
                raise InvocationError("planning fallback executable is unavailable") from exc
            if result.returncode != 0:
                raise InvocationError(
                    f"planning fallback failed ({result.returncode}): "
                    f"{(result.stderr or '')[:300]}")
            obs.operational_log(
                "INFO", "planning capacity fallback completed",
                component="planning-agent", operation="capacity-route",
                model=fallback_step.model, provider=fallback_step.provider,
                effort=fallback_step.effort)
            result.stdout = _codex_stdout(result, output_path)
    try:
        try:
            parsed_stdout = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed_stdout = None
        if (isinstance(parsed_stdout, dict)
                and "type" not in parsed_stdout
                and "structured_output" not in parsed_stdout):
            envelope = parsed_stdout
        else:
            events = [json.loads(line) for line in result.stdout.splitlines()
                      if line.strip()]
            envelope = None
            # The StructuredOutput tool has already validated the schema. Prefer
            # that payload to any later result text or wrapper envelope.
            for event in reversed(events):
                if not isinstance(event, dict):
                    continue
                structured = event.get("structured_output")
                if isinstance(structured, dict):
                    envelope = structured
                    break
                message = event.get("message")
                content = message.get("content", []) if isinstance(message, dict) else []
                tool_payload = None
                for block in reversed(content):
                    if (not isinstance(block, dict)
                            or block.get("type") != "tool_use"
                            or block.get("name") != "StructuredOutput"):
                        continue
                    candidate = block.get("input")
                    if isinstance(candidate, str):
                        try:
                            candidate = json.loads(candidate)
                        except json.JSONDecodeError:
                            continue
                    if isinstance(candidate, dict):
                        tool_payload = candidate
                        break
                if tool_payload is not None:
                    envelope = tool_payload
                    break
            if envelope is None:
                for event in reversed(events):
                    if not isinstance(event, dict):
                        continue
                    raw = event.get("result")
                    if isinstance(raw, str) and raw.strip():
                        try:
                            parsed = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(parsed, dict):
                            envelope = parsed
                            break
        if isinstance(envelope, dict) and isinstance(envelope.get("structured_output"), dict):
            envelope = envelope["structured_output"]
        elif isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
            envelope = json.loads(envelope["result"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvocationError("planning model returned malformed JSON") from exc
    if not isinstance(envelope, dict):
        raise InvocationError("planning model returned a non-object")
    return envelope


def execute(repo: str, artifact: int, token: str, timeout: int, max_usd: float,
            runner=subprocess.run) -> artifacts.WrittenPlan:
    client = artifacts.GitHubStore(repo, token)
    try:
        issue = client.get_issue(artifact)
        product, adrs, repository = read_repository(client)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            raise InvocationError(
                f"repository read constraint failed: GitHub returned {exc.code}; "
                "no planning artifacts were written") from exc
        raise
    feedback = review_comments(client, artifact)
    prior_plan = existing_plan(client, artifact)
    value = {"trigger": {**issue, "labels": labels_of(issue)}, "product": product,
             "adrs": adrs, "repository": repository, "review_comments": feedback,
             "existing_plan": prior_plan}
    validated = contract.validate_input(value)
    altitude = contract.select_altitude(set(validated.trigger["labels"]))
    key = (f"{artifact}:{state_version(client, issue)}:{altitude.value}:"
           f"prompt-{prompt_version()}:feedback-{feedback_version(feedback)}")
    output = run_model(value, timeout, max_usd, runner=runner)
    contract.validate_output(altitude, output)
    artifacts.write(client, value["trigger"], key, output)
    verified = verify_with_retry(client, value["trigger"], key, altitude)
    if altitude is contract.Altitude.PROJECT:
        fresh = client.get_issue(artifact)
        labels = set(labels_of(fresh))
        if "project:planning" not in labels:
            raise InvocationError(
                "verified project output cannot finish: trigger is not project:planning")
        labels.remove("project:planning")
        labels.add("project:awaiting-ready")
        client.update_labels(artifact, sorted(labels))
    return verified


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Headless planning agent")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--artifact", required=True, type=int)
    parser.add_argument("--timeout", type=int,
                        default=int(os.environ.get("FACTORY_PLANNING_TIMEOUT", DEFAULT_TIMEOUT)))
    parser.add_argument("--max-usd", type=float,
                        default=float(os.environ.get("FACTORY_PLANNING_MAX_USD", DEFAULT_MAX_USD)))
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("planning failed: no GH_TOKEN/GITHUB_TOKEN", file=sys.stderr)
        return 2
    if args.timeout <= 0 or args.max_usd <= 0:
        print("planning failed: timeout and max-usd must be positive", file=sys.stderr)
        return 2
    try:
        result = execute(args.repo, args.artifact, token, args.timeout, args.max_usd)
    except Exception as exc:  # fail loudly at the headless boundary
        print(f"planning failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"altitude": result.altitude.value, "project": result.project,
                      "adr": result.adr, "stories": result.stories}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
