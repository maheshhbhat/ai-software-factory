#!/usr/bin/env python3
"""Fresh-context, exact-head review wrapper: pull-request identity in, outcome out."""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

DEFAULT_REVIEW_TIMEOUT = 60

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "factory" / "runtime"))
import review_route  # noqa: E402
import observability as obs  # noqa: E402
import runlog  # noqa: E402
import streaming  # noqa: E402

REAL_SUBPROCESS_RUN = subprocess.run


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
    review_home.mkdir(mode=0o700)
    env.update({"HOME": str(review_home), "USER": "factory-reviewer",
                "LOGNAME": "factory-reviewer"})
    return env


def git_auth_header(token: str) -> str:
    value = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Authorization: Basic {value}"


def command(input_path: pathlib.Path, output_path: pathlib.Path) -> list[str]:
    template = os.environ.get("FACTORY_REVIEW_MODEL_CMD", "").strip()
    if template:
        return [part.replace("{input_file}", str(input_path))
                    .replace("{output_file}", str(output_path))
                for part in shlex.split(template)]
    payload = (HERE.joinpath("prompt.md").read_text()
               + f"\n\nWrite the JSON outcome to: {output_path}\n\nInput: "
               + input_path.read_text())
    # The verdict is read from the outcome file, never from stdout, so asking
    # for JSON on stdout costs the review nothing and is what makes the
    # reviewer state its own usage. It grants no tool and changes no identity.
    return ["claude", "-p", payload, "--permission-mode", "acceptEdits",
            "--tools", "Write", "--allowedTools", "Write",
            "--disallowedTools", "Bash,Agent",
            "--output-format", "stream-json", "--verbose",
            "--safe-mode", "--no-session-persistence"]


def engine_name(cmd) -> str | None:
    """The engine a reviewer command launches, recognised or not."""
    return pathlib.PurePath(cmd[0]).name.lower() if cmd else None


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


def printed(value) -> str:
    """Whatever a subprocess wrote, decoded for us or not."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def run(cmd, *, cwd, timeout=DEFAULT_REVIEW_TIMEOUT, env=None):
    try:
        if subprocess.run is REAL_SUBPROCESS_RUN:
            result = streaming.run(
                cmd, cwd=cwd, env=env or clean_environment(), timeout=timeout,
                component="reviewer", operation="engine-stream",
                engine=engine_name(cmd))
        else:
            result = subprocess.run(
                cmd, cwd=cwd, env=env or clean_environment(), timeout=timeout,
                capture_output=True, text=True)
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(f"reviewer unavailable: timeout after {timeout}s",
                          printed(exc.stdout)) from exc
    if result.returncode:
        raise ReviewError(
            f"reviewer unavailable: exit {result.returncode}: "
            f"{(result.stderr or result.stdout or '')[:300]}",
            printed(result.stdout))
    return result


def launch_reviewer(cmd, *, cwd, timeout, env, story, pull_request):
    """Run the reviewer, recording what it reported about its own usage.

    One runtime-log record per invocation, whether the launch completed or
    failed, naming the story it reviewed. A reviewer that reported no usage is
    recorded as having reported none; no number is substituted for it.
    """
    engine = engine_name(cmd)
    runlog.event("review.engine.started", story=story, pull_request=pull_request,
                 engine=engine, timeout_seconds=timeout)
    try:
        result = run(cmd, cwd=cwd, timeout=timeout, env=env)
    except ReviewError as error:
        runlog.event("review.engine.failed", story=story, pull_request=pull_request,
                     engine=engine, detail=str(error))
        runlog.engine_usage(story=story, engine=engine, phase="review",
                            pull_request=pull_request, launch="failed",
                            output=error.output)
        raise
    # A launch that produced no stdout at all reported no usage — that is a
    # record saying so, never a zero standing in for a measurement.
    runlog.event("review.engine.finished", story=story, pull_request=pull_request,
                 engine=engine)
    runlog.engine_usage(story=story, engine=engine, phase="review",
                        pull_request=pull_request, launch="completed",
                        output=printed(getattr(result, "stdout", None)))
    return result


def criteria(body: str) -> str:
    import re
    match = re.search(r"(?ms)^### Falsifiable acceptance criteria\s*$\n(.*?)(?=^### |\Z)", body or "")
    if not match:
        raise ReviewError("approved Project criteria unavailable")
    return match.group(1).strip()


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
            timeout=DEFAULT_REVIEW_TIMEOUT):
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
    project_number = int(next(line[1:] for line in (story.get("body") or "").splitlines()
                              if line.startswith("#") and line[1:].isdigit()))
    project = client.api(f"/issues/{project_number}")
    project_labels = {x.get("name") for x in project.get("labels", [])}
    if "project:active" not in project_labels:
        raise ReviewError("approved active Project unavailable")
    adrs = [x for x in client.pages("/issues?state=all")
            if "type:adr" in {label.get("name") for label in x.get("labels", [])}]
    fields = {"head": target.head,
              "diff": [{"filename": x.get("filename"), "status": x.get("status"),
                         "patch": x.get("patch", "")} for x in
                        client.pages(f"/pulls/{pull_number}/files")],
              "story_spec": story.get("body", ""),
              "project_criteria": criteria(project.get("body", "")),
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
        input_path = workspace / "input.json"
        staging_path = staging_outcome_path(workspace)
        output_path = outcome_path(workspace)
        store_outcome(staging_path, output_path)
        input_path.write_text(json.dumps(fields, sort_keys=True))
        # The fresh identity is built before the launch is attempted, so a
        # credential that could not be assembled is not recorded as an engine
        # invocation that happened.
        reviewer_env = review_environment(workspace / "reviewer-home")
        with obs.Activity("independent-review", "engine", "reviewing",
                          trace_id=review_trace, repo=repo, story=story_number,
                          project=project_number, pull_request=pull_number):
            launch_reviewer(command(input_path, staging_path), cwd=workspace / "repo",
                            timeout=timeout, env=reviewer_env, story=story_number,
                            pull_request=pull_number)
        finalize_outcome(staging_path, output_path)
        result = parse_result(output_path, target.head)

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
