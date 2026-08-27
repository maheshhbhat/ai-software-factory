#!/usr/bin/env python3
"""Fresh-context, exact-head review wrapper: pull-request identity in, outcome out."""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

DEFAULT_REVIEW_TIMEOUT = 180
MAX_OWNER_EVIDENCE_CHARS = 8_000

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "factory" / "runtime"))
import review_route  # noqa: E402
import observability as obs  # noqa: E402
import runlog  # noqa: E402
import streaming  # noqa: E402
from factory.capacity_pool.executor import CapacityExecutor  # noqa: E402
from factory.capacity_pool.policy import POLICIES, resolved_registry  # noqa: E402
from factory.capacity_pool.providers import (  # noqa: E402
    AttemptResult, InvocationPayload, ProviderAdapter, cli_adapter,
    provider_environment,
)
from factory.capacity_pool.state import CapacityState  # noqa: E402
from factory.runtime import operating_envelope  # noqa: E402

class ReviewError(RuntimeError):
    """A review could not be delivered.

    `output` carries whatever the failed subprocess printed, so a reviewer that
    crashed or timed out can still have its own usage report read off it.
    """

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def api(self, path, *, method="GET", value=None):
        data = None if value is None else json.dumps(value).encode()
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "factory-reviewer"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                raise ReviewError(f"repository access constraint failed: GitHub returned {exc.code}") from exc
            raise

    def pages(self, path):
        values, page = [], 1
        while True:
            join = "&" if "?" in path else "?"
            batch = self.api(f"{path}{join}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ReviewError(f"malformed collection: {path}")
            values.extend(batch)
            if len(batch) < 100:
                return values
            page += 1


def clean_environment() -> dict[str, str]:
    keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL",
            "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    return {key: value for key, value in os.environ.items() if key in keep}


def review_environment(review_home: pathlib.Path) -> dict[str, str]:
    """Fresh reviewer identity context with credential material only."""
    env = clean_environment()
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        operator_home = os.environ.get("HOME", "")
        credentials = pathlib.Path(operator_home) / ".factory-reviewer-token"
        try:
            token = credentials.read_text().strip()
        except OSError as exc:
            raise ReviewError("reviewer credential unavailable") from exc
        if not isinstance(token, str) or not token:
            raise ReviewError("reviewer credential unavailable")
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    review_home.mkdir(mode=0o700, parents=True)
    env.update({"HOME": str(review_home), "USER": "factory-reviewer",
                "LOGNAME": "factory-reviewer"})
    return env


def git_auth_header(token: str) -> str:
    value = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Authorization: Basic {value}"


def outcome_path(workspace: pathlib.Path) -> pathlib.Path:
    # Inside the tool sandbox, but outside the PR-controlled worktree. A pull
    # request can never pre-seed a path under the clone's .git metadata.
    return workspace / "repo" / ".git" / "factory-review-out.json"


def staging_outcome_path(workspace: pathlib.Path) -> pathlib.Path:
    return workspace / "repo" / ".factory-review-out.json"


def store_outcome(staging: pathlib.Path, output: pathlib.Path) -> None:
    # The checkout may contain an attacker-controlled file at the staging path.
    # Remove it before the reviewer runs, then let trusted wrapper code move the
    # newly written result under .git before anything parses it.
    staging.unlink(missing_ok=True)
    output.unlink(missing_ok=True)


def finalize_outcome(staging: pathlib.Path, output: pathlib.Path) -> None:
    try:
        staging.replace(output)
    except OSError as exc:
        raise ReviewError("malformed reviewer output") from exc


def reviewer_provider_environment(provider: str,
                                  review_home: pathlib.Path) -> dict[str, str]:
    """Build a provider-specific reviewer identity without repository tokens."""
    if provider == "anthropic":
        return provider_environment(provider, review_environment(review_home))
    review_home.mkdir(mode=0o700, parents=True)
    source = dict(os.environ)
    configured = source.get("FACTORY_REVIEW_CODEX_HOME", "").strip()
    if configured:
        source["CODEX_HOME"] = configured
    elif source.get("HOME"):
        source["CODEX_HOME"] = str(pathlib.Path(source["HOME"]) / ".codex")
    source.pop("HOME", None)
    source.pop("GH_TOKEN", None)
    source.pop("GITHUB_TOKEN", None)
    return provider_environment(provider, source)


def review_payload(fields: dict, staging: pathlib.Path) -> InvocationPayload:
    prompt = (HERE.joinpath("prompt.md").read_text()
              + f"\n\nWrite the JSON outcome to: {staging}\n\nInput: "
              + json.dumps(fields, sort_keys=True))
    return InvocationPayload(
        # The reviewer writes the structured artifact with its only allowed
        # tool.  Do not also pass this path as Codex's --output-last-message:
        # Codex writes that file after the turn and would replace the valid
        # JSON artifact with final prose such as "Wrote the review outcome".
        prompt, access="workspace-write",
        allowed_tools=("Write",), disallowed_tools=("Bash", "Agent"))


def bounded_review_adapter(provider: str, *, cwd: pathlib.Path, environment: dict,
                           staging: pathlib.Path, output: pathlib.Path,
                           runner=subprocess.run) -> ProviderAdapter:
    """Discard every failed attempt's private artifact before a peer runs."""
    base = cli_adapter(provider, cwd=cwd, environment=environment, runner=runner)

    def invoke(**kwargs):
        store_outcome(staging, output)
        result = base.run(**kwargs)
        if not result.succeeded:
            store_outcome(staging, output)
        return result

    return ProviderAdapter(provider, invoke)


def criteria(body: str) -> str:
    match = re.search(r"(?ms)^### Falsifiable acceptance criteria\s*$\n(.*?)(?=^### |\Z)", body or "")
    if not match:
        raise ReviewError("approved Project criteria unavailable")
    return match.group(1).strip()


def section(body: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^### {re.escape(heading)}\s*$\n(.*?)(?=^### |\Z)", body or "")
    if not match:
        return ""
    return match.group(1).strip()


def issue_labels(issue: dict) -> list[str]:
    return sorted(label.get("name", "") for label in issue.get("labels", [])
                  if label.get("name"))


def story_project(body: str) -> int | None:
    value = section(body, "Project")
    match = re.fullmatch(r"#([1-9][0-9]*)", value)
    return int(match.group(1)) if match else None


def story_dependencies(body: str) -> list[int]:
    value = section(body, "Depends-on")
    if value == "none":
        return []
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines or any(not re.fullmatch(r"#[1-9][0-9]*", line) for line in lines):
        raise ReviewError("malformed Story dependency topology")
    return [int(line[1:]) for line in lines]


def project_story_plan(issues: list[dict], project_number: int,
                       current_number: int) -> list[dict]:
    siblings = {
        int(item["number"]): item for item in issues
        if "type:story" in issue_labels(item)
        and story_project(item.get("body") or "") == project_number
    }
    if current_number not in siblings:
        raise ReviewError("current Story unavailable in approved Project topology")
    dependencies = {
        number: story_dependencies(item.get("body") or "")
        for number, item in siblings.items()
    }
    for number, refs in dependencies.items():
        missing = sorted(set(refs) - set(siblings))
        if missing:
            raise ReviewError(
                f"Story #{number} dependency leaves approved Project: {missing}")

    visiting, visited = set(), set()

    def visit(number: int) -> None:
        if number in visiting:
            raise ReviewError("cyclic Story dependency topology")
        if number in visited:
            return
        visiting.add(number)
        for dependency in dependencies[number]:
            visit(dependency)
        visiting.remove(number)
        visited.add(number)

    for number in sorted(siblings):
        visit(number)

    prerequisites = set()

    def collect(number: int) -> None:
        for dependency in dependencies[number]:
            if dependency not in prerequisites:
                prerequisites.add(dependency)
                collect(dependency)

    collect(current_number)
    plan = []
    for number, item in sorted(siblings.items()):
        labels = issue_labels(item)
        if number == current_number:
            relation = "current"
        elif item.get("state") == "closed" or "story:completed" in labels:
            relation = "completed"
        elif number in prerequisites:
            relation = "prerequisite"
        else:
            relation = "future"
        plan.append({
            "number": number,
            "title": item.get("title") or "",
            "body": item.get("body") or "",
            "labels": labels,
            "state": item.get("state") or "open",
            "phase": section(item.get("body") or "", "Phase"),
            "depends_on": dependencies[number],
            "relation": relation,
        })
    return plan


def normalized_checks(value: dict) -> list[dict]:
    checks = value.get("check_runs", []) if isinstance(value, dict) else []
    return sorted(({
        "name": item.get("name") or "",
        "status": item.get("status") or "",
        "conclusion": item.get("conclusion"),
        "details_url": item.get("details_url") or "",
    } for item in checks), key=lambda item: (item["name"], item["details_url"]))


def prior_review_findings(comments: list[dict]) -> list[dict]:
    marker = re.compile(
        r"<!-- review-outcome:[1-9][0-9]*:[0-9a-f]{7,64}:findings -->")
    return [
        {"body": item.get("body") or "", "created_at": item.get("created_at")}
        for item in comments
        if (item.get("body") or "").startswith("## Review findings")
        and marker.search(item.get("body") or "")
    ]


def exact_head_owner_evidence(comments: list[dict], pull_number: int,
                              head: str) -> list[dict]:
    """Return only trusted, canonical evidence bound to this PR head."""
    marker = re.compile(
        rf"<!-- owner-evidence:v1:pr:{pull_number}:head:{re.escape(head)} -->")
    trusted = {"OWNER", "COLLABORATOR"}
    evidence = []
    for item in comments:
        body = item.get("body") or ""
        association = (item.get("author_association")
                       or item.get("authorAssociation") or "").upper()
        if (association in trusted
                and body.startswith("## Owner evidence — not an acceptance decision")
                and marker.search(body)):
            evidence.append({
                "body": body[:MAX_OWNER_EVIDENCE_CHARS],
                "created_at": item.get("created_at") or item.get("createdAt"),
            })
    return evidence


def parse_result(path: pathlib.Path, head: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError("malformed reviewer output") from exc
    allowed = {"head", "verdict", "summary"} if value.get("verdict") == "approval" else {"head", "verdict", "findings"}
    if set(value) != allowed or value.get("head") != head:
        raise ReviewError("malformed or stale-head reviewer output")
    if value["verdict"] == "approval" and not isinstance(value.get("summary"), str):
        raise ReviewError("malformed reviewer approval")
    if value["verdict"] == "findings" and (not isinstance(value.get("findings"), list)
                                             or not value["findings"]
                                             or not all(isinstance(x, str) and x.strip() for x in value["findings"])):
        raise ReviewError("malformed reviewer findings")
    if value["verdict"] not in ("approval", "findings"):
        raise ReviewError("malformed reviewer verdict")
    return value


def execute(repo: str, pull_number: int, token: str, *, client=None,
            timeout=DEFAULT_REVIEW_TIMEOUT, state: CapacityState | None = None,
            registry=None, runner=None):
    client = client or GitHub(repo, token)
    runlog.event("review.preparing", repo=repo, pull_request=pull_number)
    client.api("")
    pull = client.api(f"/pulls/{pull_number}")
    story_number = review_route.story_number(pull)
    story = client.api(f"/issues/{story_number}")
    timeline = client.pages(f"/issues/{story_number}/timeline")
    review_trace = obs.story_trace_id(repo, story_number, timeline)
    comments = client.pages(f"/issues/{story_number}/comments")
    target = review_route.target(pull, story, comments)
    if target is None:
        return {"status": "replay", "pull_request": pull_number,
                "head": (pull.get("head") or {}).get("sha")}
    project_number = story_project(story.get("body") or "")
    if project_number is None:
        raise ReviewError("Story Project reference unavailable")
    project = client.api(f"/issues/{project_number}")
    project_labels = {x.get("name") for x in project.get("labels", [])}
    if "project:active" not in project_labels:
        raise ReviewError("approved active Project unavailable")
    issues = client.pages("/issues?state=all")
    story_plan = project_story_plan(issues, project_number, story_number)
    adrs = [x for x in issues
            if "type:adr" in {label.get("name") for label in x.get("labels", [])}]
    check_state = normalized_checks(client.api(f"/commits/{target.head}/check-runs"))
    prior_findings = prior_review_findings(comments)
    owner_evidence = exact_head_owner_evidence(
        comments, pull_number, target.head)
    fields = {"head": target.head,
              "diff": [{"filename": x.get("filename"), "status": x.get("status"),
                         "patch": x.get("patch", "")} for x in
                        client.pages(f"/pulls/{pull_number}/files")],
              "story_spec": story.get("body", ""),
              "project_criteria": criteria(project.get("body", "")),
              "project_plan": {
                  "number": project_number,
                  "title": project.get("title") or "",
                  "body": project.get("body") or "",
                  "labels": sorted(project_labels),
                  "state": project.get("state") or "open",
                  "stories": story_plan,
              },
              "trusted_checks": check_state,
              "prior_findings": prior_findings,
              "owner_evidence": owner_evidence,
              "operating_envelope_obligations": operating_envelope.obligations(
                  story.get("body", ""), project.get("body", "")),
              "adrs": [{"number": x.get("number"), "title": x.get("title"),
                        "body": x.get("body")} for x in adrs]}
    with tempfile.TemporaryDirectory(prefix=f"factory-review-{pull_number}-{target.head[:8]}-") as temp:
        workspace = pathlib.Path(temp)
        obs.process_event("review.clone.started", trace_id=review_trace, repo=repo,
                          story=story_number, project=project_number,
                          pull_request=pull_number, head=target.head)
        clone_env = clean_environment()
        clone_env.update({"GIT_CONFIG_COUNT": "1",
                          "GIT_CONFIG_KEY_0": "http.extraHeader",
                          "GIT_CONFIG_VALUE_0": git_auth_header(token)})
        with obs.Activity("independent-review", "clone", "cloning",
                          trace_id=review_trace, repo=repo, story=story_number,
                          project=project_number, pull_request=pull_number):
            subprocess.run(["git", "clone", "--quiet", f"https://github.com/{repo}.git", "repo"],
                           cwd=workspace, env=clone_env, check=True,
                           capture_output=True, text=True, timeout=timeout)
            subprocess.run(["git", "checkout", "--quiet", target.head], cwd=workspace / "repo",
                           env=clean_environment(), check=True, capture_output=True,
                           text=True, timeout=timeout)
        runlog.event("review.clone.finished", story=story_number,
                     pull_request=pull_number, head=target.head)
        staging_path = staging_outcome_path(workspace)
        output_path = outcome_path(workspace)
        store_outcome(staging_path, output_path)
        owns_state = state is None
        if state is None:
            configured = os.environ.get("FACTORY_CAPACITY_STATE", "").strip()
            state_path = (pathlib.Path(configured) if configured else
                          ROOT / "runs" / "capacity-pool.sqlite")
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state = CapacityState(state_path, uri=False)
        try:
            available = tuple(registry or resolved_registry(health=state.health))
            if runner is None:
                def runner(command, **kwargs):
                    return streaming.run(
                        command, component="reviewer",
                        operation="capacity-engine-stream",
                        engine=pathlib.PurePath(command[0]).name.lower(), **kwargs)
            labels = project_labels | {
                item.get("name") for item in story.get("labels", [])}
            triggers = frozenset(
                name for name, candidates in {
                    "high-risk": {"high-risk", "risk:high"},
                    "architecture": {"architecture", "type:architecture"},
                    "security": {"security", "risk:security"},
                }.items() if labels & candidates)
            request = POLICIES["review"].request(
                triggers=triggers, total_timeout_seconds=timeout)
            providers = {item.provider for item in available}
            adapters = {provider: bounded_review_adapter(
                provider, cwd=workspace / "repo",
                environment=reviewer_provider_environment(
                    provider, workspace / "reviewer-home" / provider),
                staging=staging_path, output=output_path, runner=runner)
                for provider in providers}
            capacity = CapacityExecutor(
                adapters, state,
                telemetry=lambda **values: obs.telemetry(
                    component="independent-review", operation="capacity-route",
                    story=story_number, project=project_number,
                    pull_request=pull_number, **values))
            parsed = {}

            def validate(_output):
                finalize_outcome(staging_path, output_path)
                parsed.update(parse_result(output_path, target.head))

            with obs.Activity("independent-review", "engine", "reviewing",
                              trace_id=review_trace, repo=repo, story=story_number,
                              project=project_number, pull_request=pull_number):
                capacity_result = capacity.execute(
                    task_key=f"review:{repo}:{pull_number}:{target.head}",
                    request=request, registry=available,
                    payload=review_payload(fields, staging_path),
                    validate=validate)
            if capacity_result.attempts:
                final_attempt = capacity_result.attempts[-1]
                runlog.engine_usage(
                    story=story_number, engine=final_attempt["model"],
                    phase="review", pull_request=pull_number,
                    launch=("completed" if capacity_result.outcome == "success"
                            else "failed"), output=capacity_result.output)
            if capacity_result.outcome != "success":
                detail = (f": {capacity_result.output}"
                          if capacity_result.output else "")
                raise ReviewError(
                    f"review capacity failed: {capacity_result.outcome}{detail}")
            result = dict(parsed)
        finally:
            if owns_state:
                state.close()

    runlog.event("review.publishing", story=story_number,
                 pull_request=pull_number, head=target.head,
                 verdict=result["verdict"])
    fresh = client.api(f"/pulls/{pull_number}")
    if (fresh.get("head") or {}).get("sha") != target.head:
        raise ReviewError("stale-head result refused")
    fresh_comments = client.pages(f"/issues/{story_number}/comments")
    if review_route.outcomes(fresh_comments, pull_number, target.head):
        raise ReviewError("duplicate outcome delivery refused")
    if result["verdict"] == "approval":
        detail = result["summary"]
        heading = "## Review approval"
    else:
        detail = "\n".join(f"- {x}" for x in result["findings"])
        heading = "## Review findings"
    body = (f"{heading}\n\nhead: `{target.head}`\n\n{detail}\n\n"
            f"{review_route.marker(pull_number, target.head, result['verdict'])}")
    client.api(f"/issues/{story_number}/comments", method="POST", value={"body": body})
    if result["verdict"] == "findings":
        current = client.api(f"/issues/{story_number}")
        names = {x.get("name") for x in current.get("labels", [])}
        if "story:in-review" not in names:
            raise ReviewError("story changed before findings transition")
        names.discard("story:in-review")
        names.add("story:ready")
        client.api(f"/issues/{story_number}", method="PATCH", value={"labels": sorted(names)})
    durable = review_route.outcomes(client.pages(f"/issues/{story_number}/comments"),
                                    pull_number, target.head)
    if durable != [result["verdict"]]:
        raise ReviewError("durable review read-back failed")
    runlog.event("review.published", story=story_number,
                 pull_request=pull_number, head=target.head,
                 verdict=result["verdict"])
    obs.process_event("review.outcome.published", trace_id=review_trace, repo=repo,
                      story=story_number, project=project_number,
                      pull_request=pull_number, head=target.head,
                      verdict=result["verdict"])
    return {"status": result["verdict"], "pull_request": pull_number, "head": target.head}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pull-request", required=True, type=int)
    parser.add_argument("--timeout", type=int, default=DEFAULT_REVIEW_TIMEOUT)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("review failed: no GH_TOKEN/GITHUB_TOKEN", file=sys.stderr)
        return 2
    try:
        print(json.dumps(execute(args.repo, args.pull_request, token, timeout=args.timeout),
                         sort_keys=True))
        return 0
    except Exception as exc:
        obs.operational_log("ERROR", "independent review failed", exc=exc,
                            component="independent-review", operation="review",
                            repo=args.repo, pull_request=args.pull_request)
        print(f"review failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
