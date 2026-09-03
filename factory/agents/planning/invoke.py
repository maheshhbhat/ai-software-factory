#!/usr/bin/env python3
"""Headless planning invocation: artifact identity in, durable GitHub plan out."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
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
import observability as obs  # noqa: E402
from factory.capacity_pool.executor import CapacityExecutor  # noqa: E402
from factory.capacity_pool.policy import POLICIES, resolved_registry  # noqa: E402
from factory.capacity_pool.providers import (  # noqa: E402
    InvocationPayload, cli_adapter, provider_environment,
)
from factory.capacity_pool.state import CapacityState, default_state_path  # noqa: E402

DEFAULT_TIMEOUT = 900
DEFAULT_MAX_USD = 5.0


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
                    path.lower().endswith((".js", ".mjs", ".cjs", ".ts", ".tsx",
                                           ".jsx", ".py", ".json", ".toml", ".md",
                                           ".yml", ".yaml", ".html", ".htm", ".css"))]
    sources, total = {}, 0
    for path in source_paths:
        text = content(path)
        total += len(text.encode())
        if total > 500_000:
            raise InvocationError(
                "repository read constraint failed: grounded source context exceeds 500KB")
        sources[path] = text
    evidence = repository_evidence(files, sources)
    return product, adrs, {"default_branch": branch, "files": files,
                           "sources": sources, **evidence}


def repository_evidence(files: list[str], sources: dict[str, str]) -> dict:
    """Extract only explicit, mechanically checkable ownership and policy facts."""
    known = set(files)
    owners, forbidden, assertions = [], set(), []
    for source_path, text in sources.items():
        if source_path.lower().endswith((".html", ".htm")):
            for target in re.findall(
                    r"<script\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]", text, re.I):
                target = target.split("?", 1)[0].lstrip("./")
                if target in known:
                    owners.append({
                        "path": target,
                        "terms": ["browser", "visible", "render", "display", "ui",
                                  "interaction", "text", "disclosure"],
                        "evidence": f"{source_path} loads {target}",
                    })
        if source_path.lower().endswith(".json"):
            try:
                manifest = json.loads(text)
            except json.JSONDecodeError:
                manifest = None
            if isinstance(manifest, dict):
                for container in (manifest.get("factoryPolicy"), manifest.get("policy")):
                    if isinstance(container, dict):
                        values = container.get("forbiddenDependencies") or []
                        if isinstance(values, list):
                            forbidden.update(item for item in values
                                             if isinstance(item, str) and item)
        for line_number, line in enumerate(text.splitlines(), 1):
            lowered = line.lower()
            if not re.search(r"\b(?:assert|expect)\b", lowered):
                continue
            if not re.search(
                    r"\b(?:undefined|false|null|empty|not\.|notin|not_in|does not)",
                    lowered):
                continue
            names = re.findall(
                r"(?:devdependencies|dependencies)(?:\?\.)?(?:\[['\"]|\.)"
                r"([@A-Za-z0-9_./-]+)", line, re.I)
            for name in names:
                forbidden.add(name)
                assertions.append({
                    "kind": "forbidden-dependency", "name": name,
                    "evidence": f"{source_path}:{line_number}",
                })
            if (re.search(r"(?:devdependencies|dependencies)", lowered)
                    and re.search(r"\{\s*\}", line)):
                forbidden.add("*")
                assertions.append({
                    "kind": "forbidden-dependency", "name": "*",
                    "evidence": f"{source_path}:{line_number}",
                })
    return {"production_owners": owners,
            "forbidden_dependencies": sorted(forbidden),
            "policy_assertions": assertions}


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


def _parse_output(raw: str) -> dict:
    try:
        try:
            parsed_stdout = json.loads(raw)
        except json.JSONDecodeError:
            parsed_stdout = None
        if isinstance(parsed_stdout, dict) and "type" not in parsed_stdout and "structured_output" not in parsed_stdout:
            envelope = parsed_stdout
        else:
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            envelope = None
            for event in reversed(events):
                if not isinstance(event, dict):
                    continue
                structured = event.get("structured_output")
                if isinstance(structured, dict):
                    envelope = structured
                    break
                message = event.get("message")
                content = message.get("content", []) if isinstance(message, dict) else []
                for block in reversed(content):
                    if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "StructuredOutput":
                        continue
                    candidate = block.get("input")
                    if isinstance(candidate, str):
                        try:
                            candidate = json.loads(candidate)
                        except json.JSONDecodeError:
                            continue
                    if isinstance(candidate, dict):
                        envelope = candidate
                        break
                if envelope is not None:
                    break
            if envelope is None:
                for event in reversed(events):
                    raw_result = event.get("result") if isinstance(event, dict) else None
                    if isinstance(raw_result, str) and raw_result.strip():
                        try:
                            candidate = json.loads(raw_result)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(candidate, dict):
                            envelope = candidate
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


def _capacity_state() -> CapacityState:
    path = default_state_path(ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    return CapacityState(path, uri=False)


def _planning_triggers(value: dict) -> frozenset[str]:
    labels = set((value.get("trigger") or {}).get("labels", []))
    triggers = set()
    if labels & {"architecture", "type:architecture", "risk:architecture"}:
        triggers.add("architecture")
    if labels & {"high-complexity", "complexity:high"}:
        triggers.add("high-complexity")
    return frozenset(triggers)


def run_model(value: dict, timeout: int, max_usd: float,
              runner=subprocess.run, clock=time.monotonic, *,
              state: CapacityState | None = None, registry=None) -> dict:
    altitude = contract.select_altitude(set((value.get("trigger") or {}).get("labels", [])))
    schema_value = contract.json_schema(altitude)
    prompt = (HERE.joinpath("prompt.md").read_text()
              + "\n\n## Invocation input\n\n" + json.dumps(value, indent=2)
              + "\n\nReturn only the contract JSON object; do not write GitHub directly.")
    with tempfile.NamedTemporaryFile("w", suffix=".schema.json", encoding="utf-8") as schema, \
            tempfile.NamedTemporaryFile("w", suffix=".result.json", encoding="utf-8") as output:
        json.dump(schema_value, schema)
        schema.flush()
        owns_state = state is None
        state = state or _capacity_state()
        try:
            available = tuple(registry or resolved_registry(health=state.health))
            request = POLICIES["planning"].request(
                triggers=_planning_triggers(value), total_timeout_seconds=timeout,
                total_budget_units=max_usd)
            payload = InvocationPayload(prompt, schema_value, pathlib.Path(schema.name),
                                        pathlib.Path(output.name))
            adapters = {provider: cli_adapter(
                provider, cwd=ROOT, environment=provider_environment(provider), runner=runner)
                for provider in {item.provider for item in available}}
            executor = CapacityExecutor(
                adapters, state,
                telemetry=lambda **fields: obs.telemetry(
                    component="planning-agent", operation="capacity-route", **fields),
                monotonic=clock)
            parsed = None
            def validate(raw):
                nonlocal parsed
                parsed = _parse_output(raw)
                contract.validate_output(altitude, parsed, value.get("repository"))
            material = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            result = executor.execute(
                task_key="planning:" + hashlib.sha256(material).hexdigest(),
                request=request, registry=available, payload=payload, validate=validate)
            if result.outcome != "success":
                raise InvocationError(f"planning capacity failed: {result.outcome}")
            return parsed
        finally:
            if owns_state:
                state.close()

def execute(repo: str, artifact: int, token: str, timeout: int, max_usd: float,
            runner=subprocess.run, *, state=None, registry=None) -> artifacts.WrittenPlan:
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
    output = run_model(value, timeout, max_usd, runner=runner,
                       state=state, registry=registry)
    contract.validate_output(altitude, output, repository)
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
