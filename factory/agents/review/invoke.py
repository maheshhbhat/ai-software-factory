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

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "factory" / "runtime"))
import review_route  # noqa: E402


class ReviewError(RuntimeError):
    pass


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
    keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL", "HOME", "USER", "LOGNAME",
            "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    return {key: value for key, value in os.environ.items() if key in keep}


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
    return ["claude", "-p", payload, "--permission-mode", "acceptEdits",
            "--allowedTools", "Write", "--disallowedTools", "Bash",
            "--no-session-persistence"]


def run(cmd, *, cwd, timeout=3600):
    try:
        result = subprocess.run(cmd, cwd=cwd, env=clean_environment(), timeout=timeout,
                                capture_output=True, text=True)
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(f"reviewer unavailable: timeout after {timeout}s") from exc
    if result.returncode:
        raise ReviewError(
            f"reviewer unavailable: exit {result.returncode}: "
            f"{(result.stderr or result.stdout or '')[:300]}")


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


def execute(repo: str, pull_number: int, token: str, *, client=None, timeout=3600):
    client = client or GitHub(repo, token)
    client.api("")
    pull = client.api(f"/pulls/{pull_number}")
    story_number = review_route.story_number(pull)
    story = client.api(f"/issues/{story_number}")
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
    fields = {"diff": [{"filename": x.get("filename"), "status": x.get("status"),
                         "patch": x.get("patch", "")} for x in
                        client.pages(f"/pulls/{pull_number}/files")],
              "story_spec": story.get("body", ""),
              "project_criteria": criteria(project.get("body", "")),
              "adrs": [{"number": x.get("number"), "title": x.get("title"),
                        "body": x.get("body")} for x in adrs]}
    with tempfile.TemporaryDirectory(prefix=f"factory-review-{pull_number}-{target.head[:8]}-") as temp:
        workspace = pathlib.Path(temp)
        clone_env = clean_environment()
        clone_env.update({"GIT_CONFIG_COUNT": "1",
                          "GIT_CONFIG_KEY_0": "http.extraHeader",
                          "GIT_CONFIG_VALUE_0": git_auth_header(token)})
        subprocess.run(["git", "clone", "--quiet", f"https://github.com/{repo}.git", "repo"],
                       cwd=workspace, env=clone_env, check=True,
                       capture_output=True, text=True, timeout=timeout)
        subprocess.run(["git", "checkout", "--quiet", target.head], cwd=workspace / "repo",
                       env=clean_environment(), check=True, capture_output=True,
                       text=True, timeout=timeout)
        input_path, output_path = workspace / "input.json", workspace / "out.json"
        input_path.write_text(json.dumps(fields, sort_keys=True))
        run(command(input_path, output_path), cwd=workspace / "repo", timeout=timeout)
        result = parse_result(output_path, target.head)

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
    return {"status": result["verdict"], "pull_request": pull_number, "head": target.head}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pull-request", required=True, type=int)
    parser.add_argument("--timeout", type=int, default=3600)
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
        print(f"review failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
