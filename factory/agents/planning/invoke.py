#!/usr/bin/env python3
"""Headless planning invocation: artifact identity in, durable GitHub plan out."""

from __future__ import annotations

import argparse
import hashlib
import json
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
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "runtime"))
import artifacts  # noqa: E402
import contract  # noqa: E402
import streaming  # noqa: E402

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
    return ["claude", "-p", payload, "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(contract.json_schema(altitude), separators=(",", ":")),
            "--max-budget-usd", str(max_usd), "--permission-mode", "dontAsk",
            "--no-session-persistence"]


def run_model(value: dict, timeout: int, max_usd: float,
              runner=subprocess.run) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as handle:
        json.dump(value, handle)
        handle.flush()
        command = model_command(handle.name, timeout, max_usd)
        try:
            if runner is subprocess.run:
                result = streaming.run(
                    command, cwd=ROOT, env=os.environ.copy(), timeout=timeout,
                    component="planning-agent", operation="engine-stream",
                    artifact=(value.get("trigger") or {}).get("number"))
            else:
                result = runner(command, capture_output=True, text=True,
                                timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise InvocationError(f"planning timeout exhausted after {timeout}s") from exc
    if result.returncode != 0:
        raise InvocationError(
            f"planning model failed ({result.returncode}): {(result.stderr or '')[:300]}")
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
                tool_payload = next((block.get("input") for block in reversed(content)
                                     if isinstance(block, dict)
                                     and block.get("type") == "tool_use"
                                     and block.get("name") == "StructuredOutput"
                                     and isinstance(block.get("input"), dict)), None)
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
