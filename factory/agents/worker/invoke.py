#!/usr/bin/env python3
"""Headless bounded delivery: claimed Story identity in, one durable PR out."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "factory" / "gates"))
import merge_gate  # noqa: E402

DEFAULT_TIMEOUT = 3600
DEFAULT_MAX_USD = 40.0
MARKER = "worker-artifact"
SECTION = r"^### {name}\s*$\n(.*?)(?=^### |\Z)"
STORY_LINK = re.compile(r"(?m)^Story: #(\d+)$")


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Bounds:
    max_usd: float
    timeout: int


@dataclass(frozen=True)
class Delivery:
    story: int
    project: int
    branch: str
    pull_request: int
    head: str
    replay: bool


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def api(self, path: str, *, method: str = "GET", value=None):
        data = None if value is None else json.dumps(value).encode()
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "factory-delivery-worker"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                raise DeliveryError(
                    f"repository access constraint failed: GitHub returned {exc.code}") from exc
            raise

    def issue(self, number: int):
        return self.api(f"/issues/{number}")

    def pages(self, path: str):
        result, page = [], 1
        while True:
            join = "&" if "?" in path else "?"
            batch = self.api(f"{path}{join}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise DeliveryError(f"GitHub returned malformed collection for {path}")
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1

    def pull_requests(self):
        return self.pages("/pulls?state=all")

    def create_pr(self, title: str, head: str, base: str, body: str):
        return self.api("/pulls", method="POST",
                        value={"title": title, "head": head, "base": base,
                               "body": body, "draft": True})

    def update_pr(self, number: int, body: str):
        return self.api(f"/pulls/{number}", method="PATCH", value={"body": body})


def section(body: str, name: str) -> str:
    match = re.search(SECTION.format(name=re.escape(name)), body or "",
                      re.MULTILINE | re.DOTALL)
    if not match:
        raise DeliveryError(f"Story is missing ### {name}")
    return match.group(1).strip()


def labels(issue: dict) -> set[str]:
    return {item.get("name", "") if isinstance(item, dict) else str(item)
            for item in issue.get("labels", [])}


def parse_bounds(body: str) -> Bounds:
    raw = section(body, "Spend cap")
    money = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", raw)
    minutes = re.search(r"([0-9]+)\s*min", raw, re.I)
    if not money or not minutes:
        raise DeliveryError("Spend cap must contain `$N / N min`")
    value = Bounds(float(money.group(1)), int(minutes.group(1)) * 60)
    if value.max_usd <= 0 or value.timeout <= 0:
        raise DeliveryError("Spend cap bounds must be positive")
    return value


def state_version(events: list[dict]) -> str:
    claims = [item for item in events if item.get("event") == "labeled" and
              (item.get("label") or {}).get("name") == "story:claimed"]
    if not claims:
        raise DeliveryError("Story has no durable claimed state version")
    latest = claims[-1]
    return str(latest.get("id") or latest.get("created_at"))


def marker(story: int, version: str) -> str:
    return f"<!-- {MARKER}:{story}:{version} -->"


def linked_prs(story: int, pulls: list[dict]) -> list[dict]:
    found = []
    for pull in pulls:
        matches = STORY_LINK.findall((pull.get("body") or "").replace("\r\n", "\n"))
        if matches.count(str(story)) > 1:
            raise DeliveryError(f"PR #{pull.get('number')} duplicates Story: #{story}")
        if str(story) in matches:
            found.append(pull)
    if len(found) > 1:
        raise DeliveryError(f"multiple pull requests link Story: #{story}")
    return found


def clean_environment() -> dict[str, str]:
    keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL",
            "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    return {key: value for key, value in os.environ.items() if key in keep}


def model_command(input_file: str, bounds: Bounds) -> list[str]:
    template = os.environ.get("FACTORY_DELIVERY_MODEL_CMD", "").strip()
    if template:
        return [part.replace("{input_file}", input_file)
                    .replace("{max_usd}", str(bounds.max_usd))
                    .replace("{timeout}", str(bounds.timeout))
                for part in shlex.split(template)]
    prompt = HERE.joinpath("prompt.md").read_text()
    payload = prompt + "\n\n## Invocation input\n\n" + pathlib.Path(input_file).read_text()
    return ["claude", "-p", payload, "--max-budget-usd", str(bounds.max_usd),
            "--permission-mode", "dontAsk", "--no-session-persistence"]


def run(cmd: list[str], *, cwd: pathlib.Path, timeout: int, env=None,
        runner=subprocess.run) -> subprocess.CompletedProcess:
    try:
        result = runner(cmd, cwd=str(cwd), capture_output=True, text=True,
                        timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise DeliveryError(f"bounded execution exhausted after {timeout}s") from exc
    if result.returncode:
        raise DeliveryError(
            f"command failed ({result.returncode}): {(result.stderr or '')[:300]}")
    return result


def git(args: list[str], cwd: pathlib.Path, *, timeout=300, runner=subprocess.run):
    return run(["git", *args], cwd=cwd, timeout=timeout, runner=runner)


def changed_paths(worktree: pathlib.Path, base: str,
                  runner=subprocess.run) -> list[str]:
    tracked = git(["diff", "--name-only", f"{base}...HEAD"], worktree,
                  runner=runner).stdout.splitlines()
    pending = git(["status", "--porcelain"], worktree, runner=runner).stdout.splitlines()
    paths = tracked + [line[3:] for line in pending if len(line) > 3]
    return sorted(set(path for path in paths if path))


def build_input(client: GitHub, story: dict, project: dict) -> dict:
    comments = client.pages(f"/issues/{story['number']}/comments")
    decisions = [item for item in client.pages("/issues?state=all")
                 if "type:adr" in labels(item)]
    return {"story": story, "project": project, "adrs": decisions,
            "review_findings": [item for item in comments
                                if "## Review findings" in (item.get("body") or "")]}


def read_back_pr(client: GitHub, story_number: int, durable: str,
                 *, attempts: int = 3) -> dict:
    """Confirm the canonical artifact through GitHub, tolerating brief lag."""
    for attempt in range(attempts):
        fresh = linked_prs(story_number, client.pull_requests())
        if len(fresh) == 1 and durable in (fresh[0].get("body") or ""):
            return fresh[0]
        if attempt + 1 < attempts:
            time.sleep(1)
    raise DeliveryError("durable PR read-back failed")


def execute(repo: str, story_number: int, token: str, checkout: pathlib.Path,
            *, timeout: int | None = None, max_usd: float | None = None,
            runner=subprocess.run, client: GitHub | None = None) -> Delivery:
    client = client or GitHub(repo, token)
    metadata = client.api("")  # read preflight before any write
    default = metadata.get("default_branch")
    if not default:
        raise DeliveryError("repository access constraint failed: default branch unavailable")
    story = client.issue(story_number)
    project_number = int(section(story.get("body") or "", "Project").removeprefix("#"))
    project = client.issue(project_number)
    bounds = parse_bounds(story.get("body") or "")
    if timeout is not None:
        bounds = Bounds(bounds.max_usd, min(bounds.timeout, timeout))
    if max_usd is not None:
        bounds = Bounds(min(bounds.max_usd, max_usd), bounds.timeout)
    if bounds.timeout <= 0 or bounds.max_usd <= 0:
        raise DeliveryError("effective timeout and spend bounds must be positive")
    events = client.pages(f"/issues/{story_number}/timeline")
    version = state_version(events)
    durable = marker(story_number, version)
    pulls = linked_prs(story_number, client.pull_requests())
    if pulls and durable in (pulls[0].get("body") or ""):
        pull = pulls[0]
        return Delivery(story_number, project_number, pull["head"]["ref"],
                        pull["number"], pull["head"]["sha"], True)

    scope, error = merge_gate.parse_scope(story.get("body") or "")
    if error:
        raise DeliveryError(f"invalid Story scope: {error}")
    branch = pulls[0]["head"]["ref"] if pulls else f"story/{story_number}-delivery"
    remote = git(["ls-remote", "--heads", "origin", branch], checkout,
                 runner=runner).stdout.strip()
    if remote:
        git(["fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"],
            checkout, runner=runner)
    base_ref = f"origin/{branch}" if remote else f"origin/{default}"
    value = build_input(client, story, project)
    with tempfile.TemporaryDirectory(prefix=f"factory-story-{story_number}-") as temp:
        worktree = pathlib.Path(temp)
        git(["worktree", "add", "--detach", str(worktree), base_ref], checkout,
            runner=runner)
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                json.dump(value, handle)
                input_file = handle.name
            try:
                run(model_command(input_file, bounds), cwd=worktree,
                    timeout=bounds.timeout, env=clean_environment(), runner=runner)
            finally:
                pathlib.Path(input_file).unlink(missing_ok=True)
            paths = changed_paths(worktree, f"origin/{default}", runner=runner)
            if not paths:
                raise DeliveryError("worker produced no repository changes")
            outside = merge_gate.paths_out_of_scope(paths, scope)
            if outside:
                raise DeliveryError("worker changed paths outside Story scope: " +
                                    ", ".join(outside))
            tests = os.environ.get(
                "FACTORY_DELIVERY_TEST_CMD",
                str(HERE / "test_repo.sh")).strip()
            run(shlex.split(tests), cwd=worktree, timeout=bounds.timeout,
                env=clean_environment(), runner=runner)
            git(["add", "--", *paths], worktree, runner=runner)
            git(["commit", "-m", f"Deliver Story #{story_number}"], worktree,
                runner=runner)
            git(["push", "origin", f"HEAD:refs/heads/{branch}"], worktree,
                runner=runner)
            head = git(["rev-parse", "HEAD"], worktree, runner=runner).stdout.strip()
        finally:
            git(["worktree", "remove", "--force", str(worktree)], checkout,
                runner=runner)

    body = f"Story: #{story_number}\n\n{durable}\n"
    if pulls:
        pull = client.update_pr(pulls[0]["number"], body)
    else:
        pull = client.create_pr(f"Story #{story_number}: bounded delivery",
                                branch, default, body)
    fresh = read_back_pr(client, story_number, durable)
    return Delivery(story_number, project_number, branch, fresh["number"], head, False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--story", required=True, type=int)
    parser.add_argument("--checkout", default=str(ROOT))
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--max-usd", type=float)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("delivery failed: no GH_TOKEN/GITHUB_TOKEN", file=sys.stderr)
        return 2
    try:
        result = execute(args.repo, args.story, token, pathlib.Path(args.checkout),
                         timeout=args.timeout, max_usd=args.max_usd)
    except Exception as exc:
        print(f"delivery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
