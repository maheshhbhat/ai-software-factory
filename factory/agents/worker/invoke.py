#!/usr/bin/env python3
"""Headless bounded delivery: claimed Story identity in, one durable PR out.

Capacity Pool owns model/provider choice, credentials, command construction,
health, leases, and fallback. This wrapper owns the product mutation boundary:
it supplies the Story prompt, validates changed paths, runs product tests, and
writes the linked pull request only after those checks pass.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "factory" / "gates"))
sys.path.insert(0, str(ROOT / "factory" / "runtime"))
import merge_gate  # noqa: E402
import observability as obs  # noqa: E402
import runlog  # noqa: E402
import correction_context  # noqa: E402
from factory.capacity_pool.executor import CapacityExecutor  # noqa: E402
from factory.capacity_pool import admission as capacity_admission  # noqa: E402
from factory.capacity_pool.policy import POLICIES, resolved_registry  # noqa: E402
from factory.capacity_pool.providers import (  # noqa: E402
    InvocationPayload, cli_adapter, provider_environment,
)
from factory.capacity_pool.state import CapacityState, default_state_path  # noqa: E402
from factory.runtime import operating_envelope  # noqa: E402

DEFAULT_TIMEOUT = 3600
DEFAULT_MAX_USD = 40.0
MARKER = "worker-artifact"
SECTION = r"^### {name}\s*$\n(.*?)(?=^### |\Z)"
STORY_LINK = re.compile(r"(?m)^Story: #(\d+)$")
START_MARKER = "<!-- factory-worker-start:v1 -->"


class DeliveryError(RuntimeError):
    """A bounded delivery could not be completed.

    `output` carries whatever the failed subprocess printed. An engine that
    crashed or ran out of time still spent what it spent before it did, and
    that report is the only place those tokens are ever counted.
    """

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def platform_diagnostics(checkout: pathlib.Path) -> dict:
    """Return non-sensitive facts that explain local filesystem failures.

    ``os.access`` reports permission-bit access.  When it says writable but the
    real Git operation returns ``EPERM``, the useful conclusion is that an
    external policy (for example a process sandbox) denied the operation.
    """
    checkout = checkout.absolute()
    git_dir = checkout / ".git"
    resolution_error = None
    try:
        if git_dir.is_file():
            marker = git_dir.read_text(errors="replace").strip()
            if marker.startswith("gitdir:"):
                candidate = pathlib.Path(marker.removeprefix("gitdir:").strip())
                git_dir = candidate if candidate.is_absolute() else checkout / candidate
        git_dir = git_dir.resolve()
    except OSError as exc:
        resolution_error = f"{type(exc).__name__}: {exc}"

    def describe(path: pathlib.Path) -> dict:
        try:
            metadata = path.stat()
        except OSError as exc:
            return {"path": str(path), "stat_error": f"{type(exc).__name__}: {exc}"}
        return {
            "path": str(path),
            "mode": oct(stat.S_IMODE(metadata.st_mode)),
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
            "writable_by_access_check": os.access(path, os.W_OK),
        }

    result = {
        "platform": sys.platform,
        "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
        "effective_gid": os.getegid() if hasattr(os, "getegid") else None,
        "git_dir": describe(git_dir),
        "git_worktrees_dir": describe(git_dir / "worktrees"),
        "sandbox_marker_names": sorted(
            name for name in os.environ
            if "SANDBOX" in name.upper() or name.upper().startswith("CODEX_")),
    }
    if resolution_error:
        result["git_dir_resolution_error"] = resolution_error
    return result


# Not credentials: tools, locale, and scratch space for product tests.
BASE_ENVIRONMENT = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SHELL")


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
            if exc.code in (401, 403, 404):
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
                               "body": body, "draft": False})

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


def capacity_state_path(environ=None) -> pathlib.Path:
    return default_state_path(ROOT, environ)


def state_version(events: list[dict]) -> str:
    claims = [item for item in events if item.get("event") == "labeled" and
              (item.get("label") or {}).get("name") == "story:claimed"]
    if not claims:
        raise DeliveryError("Story has no durable claimed state version")
    latest = claims[-1]
    return str(latest.get("id") or latest.get("created_at"))


def marker(story: int, version: str) -> str:
    return f"<!-- {MARKER}:{story}:{version} -->"


def worker_start_body(*, repo: str, story: int, version: str,
                      reservation: str, invocation: str) -> str:
    value = {"schema_version": 1, "repo": repo.lower(), "story": story,
             "state_version": version, "reservation_id": reservation,
             "invocation_id": invocation,
             "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    return START_MARKER + "\n\n```json\n" + json.dumps(
        value, sort_keys=True) + "\n```"


def publish_worker_start(client: GitHub, *, repo: str, story: int, version: str,
                         reservation: str, invocation: str) -> None:
    body = worker_start_body(repo=repo, story=story, version=version,
                             reservation=reservation, invocation=invocation)
    created = client.api(
        f"/issues/{story}/comments", method="POST", value={"body": body})
    if not isinstance(created, dict) or created.get("body") != body:
        raise DeliveryError("durable worker-start write was not acknowledged")


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


def tool_environment(environ=None) -> dict[str, str]:
    """Tools, locale and scratch space — what a non-engine subprocess gets.

    No credential, on any platform, for any engine declared later. The
    delivered change's own test command runs under this.
    """
    environ = os.environ if environ is None else environ
    return {key: value for key, value in environ.items()
            if key in BASE_ENVIRONMENT}


def printed(value) -> str:
    """Whatever a subprocess wrote, decoded for us or not.

    `TimeoutExpired` hands back bytes when the process died mid-stream; a
    completed process hands back text.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def run(cmd: list[str], *, cwd: pathlib.Path, timeout: int, env=None,
        runner=subprocess.run) -> subprocess.CompletedProcess:
    try:
        result = runner(cmd, cwd=str(cwd), capture_output=True, text=True,
                        timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        # Keep both streams. A timeout's stderr was dropped entirely, and for
        # an engine killed mid-explanation that was the half worth reading.
        detail = f"bounded execution exhausted after {timeout}s"
        stderr_excerpt = runlog.diagnostic_excerpt(printed(exc.stderr))
        if stderr_excerpt:
            detail += f"; stderr diagnostic: {stderr_excerpt}"
        raise DeliveryError(detail, printed(exc.stdout)) from exc
    if result.returncode:
        # The engine's own explanation lives in these streams. A 300-character
        # slice of stderr lost it twice: the 2026-08-22 authentication refusal
        # ("Please run /login") was printed to stdout and discarded, and the
        # 2026-08-23 failures of #332 recorded `command failed (1):` with
        # nothing after the colon. runlog.tail keeps 2000 characters and
        # redacts credentials — both properties wanted here.
        raise DeliveryError(
            f"command failed ({result.returncode})"
            f"; stderr diagnostic: {runlog.diagnostic_excerpt(result.stderr)}"
            f"; stdout diagnostic: {runlog.diagnostic_excerpt(result.stdout)}",
            printed(result.stdout))
    return result


def git(args: list[str], cwd: pathlib.Path, *, timeout=300, runner=subprocess.run):
    return run(["git", *args], cwd=cwd, timeout=timeout, runner=runner)


def _remote_repo(url: str) -> str:
    value = url.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        return value.removeprefix("git@github.com:")
    marker = "github.com/"
    return value.split(marker, 1)[1] if marker in value else ""


@contextlib.contextmanager
def checkout_for_repo(repo: str, configured: pathlib.Path,
                      runner=subprocess.run):
    """Use the configured checkout only when its origin is the requested repo."""
    try:
        origin = git(["remote", "get-url", "origin"], configured,
                     runner=runner).stdout.strip()
    except DeliveryError:
        origin = ""
    if _remote_repo(origin).lower() == repo.lower():
        yield configured
        return
    with tempfile.TemporaryDirectory(prefix="factory-product-checkout-") as temp:
        checkout = pathlib.Path(temp) / "repository"
        run(["gh", "repo", "clone", repo, str(checkout), "--", "--quiet"],
            cwd=pathlib.Path(temp), timeout=300, runner=runner)
        yield checkout


def changed_paths(worktree: pathlib.Path, base: str,
                  runner=subprocess.run) -> list[str]:
    tracked = git(["diff", "--name-only", f"{base}...HEAD"], worktree,
                  runner=runner).stdout.splitlines()
    # --untracked-files=all: plain --porcelain collapses a brand-new directory
    # to one `dir/` line, which no `dir/sub/**` scope can match — the worker
    # then refuses its own in-scope work (#358, found live by fixture #357).
    pending = git(["status", "--porcelain", "--untracked-files=all"], worktree,
                  runner=runner).stdout.splitlines()
    paths = tracked + [line[3:] for line in pending if len(line) > 3]
    return sorted(set(path for path in paths if path))


def repository_test_command(worktree: pathlib.Path) -> list[str]:
    """Resolve validation from the product checkout, never from the factory."""
    override = os.environ.get("FACTORY_DELIVERY_TEST_CMD", "").strip()
    if override:
        return shlex.split(override)
    package = worktree / "package.json"
    if package.is_file():
        try:
            manifest = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeliveryError(f"invalid product package.json: {exc}") from exc
        if ((manifest.get("scripts") or {}).get("test") or "").strip():
            return ["npm", "test"]
    factory_tests = worktree / "factory" / "agents" / "worker" / "test_repo.sh"
    if factory_tests.is_file():
        return [str(factory_tests)]
    raise DeliveryError("repository declares no supported test command")


def build_input(client: GitHub, story: dict, project: dict, *, repo: str,
                pull_request: dict | None = None) -> dict:
    story_comments = client.pages(f"/issues/{story['number']}/comments")
    pull_comments = (client.pages(f"/issues/{pull_request['number']}/comments")
                     if pull_request is not None else [])
    decisions = [item for item in client.pages("/issues?state=all")
                 if "type:adr" in labels(item)]
    obligations = operating_envelope.obligations(
        story.get("body") or "", project.get("body") or "")
    packet = correction_context.assemble(
        repository=repo, project=project["number"], story=story,
        pull_request=pull_request,
        story_comments=story_comments, pull_comments=pull_comments)
    return {"story": story, "project": project,
            "operating_envelope_obligations": obligations, "adrs": decisions,
            "correction_context": packet}


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


def protected_control_scope(repo: str, scope: list[str]) -> list[str]:
    """Return protected paths only when delivery targets the factory itself."""
    if repo.rsplit("/", 1)[-1] != "ai-software-factory":
        return []
    return merge_gate.protected_factory_scope(scope)


def execute(repo: str, story_number: int, token: str, checkout: pathlib.Path,
            *, timeout: int | None = None, max_usd: float | None = None,
            runner=subprocess.run, client: GitHub | None = None,
            state: CapacityState | None = None, registry=None,
            reservation: str | None = None) -> Delivery:
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
    task_key = (capacity_admission.delivery_task_key(
                    repo, story, next_attempt=False)
                if reservation else f"delivery:{repo}:{story_number}:{version}")
    delivery_trace = obs.story_trace_id(repo, story_number, events)
    durable = marker(story_number, version)
    pulls = linked_prs(story_number, client.pull_requests())
    if pulls and durable in (pulls[0].get("body") or ""):
        pull = pulls[0]
        return Delivery(story_number, project_number, pull["head"]["ref"],
                        pull["number"], pull["head"]["sha"], True)

    scope, error = merge_gate.parse_scope(story.get("body") or "")
    if error:
        raise DeliveryError(f"invalid Story scope: {error}")
    protected = protected_control_scope(repo, scope)
    if protected:
        raise DeliveryError(
            "FACTORY_SELF_MODIFICATION_FORBIDDEN: protected Story scope: "
            + ", ".join(protected))
    branch = pulls[0]["head"]["ref"] if pulls else f"story/{story_number}-delivery"
    remote = git(["ls-remote", "--heads", "origin", branch], checkout,
                 runner=runner).stdout.strip()
    if remote:
        git(["fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"],
            checkout, runner=runner)
    base_ref = f"origin/{branch}" if remote else f"origin/{default}"
    value = build_input(client, story, project, repo=repo,
                        pull_request=pulls[0] if pulls else None)
    packet = value["correction_context"]
    obs.process_event(
        "delivery.correction-context", trace_id=delivery_trace,
        repo=repo, story=story_number, project=project_number,
        retry=packet["retry"], pull_request=packet["pull_request"],
        head=packet["head"], digest=packet["digest"],
        source_ids=[item["comment_id"] for item in packet["records"]])
    with tempfile.TemporaryDirectory(prefix=f"factory-story-{story_number}-") as temp:
        worktree = pathlib.Path(temp)
        git(["worktree", "add", "--detach", str(worktree), base_ref], checkout,
            runner=runner)
        try:
            prompt = (HERE.joinpath("prompt.md").read_text()
                      + "\n\n## Invocation input\n\n" + json.dumps(value, indent=2))
            owns_state = state is None
            if state is None:
                state_path = capacity_state_path()
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state = CapacityState(state_path, uri=False)
            try:
                available = tuple(registry or resolved_registry(health=state.health))
                labels_set = set(labels(story))
                triggers = frozenset(
                    name for name, candidates in {
                        "hazard": {"hazard", "risk:hazard"},
                        "high-complexity": {"high-complexity", "complexity:high"},
                    }.items() if labels_set & candidates)
                prior_models = state.models_for_task_prefix(
                    capacity_admission.delivery_task_prefix(repo, story),
                    exclude_task_key=task_key)
                request = POLICIES["delivery"].request(
                    triggers=triggers, total_timeout_seconds=bounds.timeout,
                    total_budget_units=bounds.max_usd,
                    prior_models=prior_models)

                def mutation_state():
                    return ("post-mutation" if changed_paths(
                        worktree, f"origin/{default}", runner=runner) else "none")

                adapters = {provider: cli_adapter(
                    provider, cwd=worktree,
                    environment=provider_environment(provider), runner=runner,
                    mutation_state=mutation_state)
                    for provider in {item.provider for item in available}}
                capacity = CapacityExecutor(
                    adapters, state,
                    telemetry=lambda **fields: obs.telemetry(
                        component="delivery-worker", operation="capacity-route",
                        story=story_number, project=project_number, **fields))

                def validate(_output):
                    produced = changed_paths(
                        worktree, f"origin/{default}", runner=runner)
                    if not produced:
                        raise DeliveryError("worker produced no repository changes")
                    outside = merge_gate.paths_out_of_scope(produced, scope)
                    if outside:
                        raise DeliveryError("worker changed paths outside Story scope: "
                                            + ", ".join(outside))

                with obs.Activity("delivery-worker", "engine", "executing",
                                  trace_id=delivery_trace, repo=repo,
                                  story=story_number, project=project_number):
                    result = capacity.execute(
                        task_key=task_key,
                        request=request, registry=available,
                        payload=InvocationPayload(prompt, access="workspace-write"),
                        validate=validate, reservation_id=reservation,
                        on_started=(lambda lease: publish_worker_start(
                            client, repo=repo, story=story_number, version=version,
                            reservation=lease.lease_id, invocation=task_key)))
                if result.attempts:
                    final_attempt = result.attempts[-1]
                    runlog.engine_usage(
                        story=story_number, project=project_number,
                        engine=final_attempt["model"], phase="worker",
                        launch=("completed" if result.outcome == "success" else "failed"),
                        output=result.output)
                if result.outcome != "success":
                    detail = f": {runlog.tail(result.output)}" if result.output else ""
                    raise DeliveryError(
                        f"delivery capacity failed: {result.outcome}{detail}")
            finally:
                if owns_state:
                    state.close()
            paths = changed_paths(worktree, f"origin/{default}", runner=runner)
            if not paths:
                raise DeliveryError("worker produced no repository changes")
            outside = merge_gate.paths_out_of_scope(paths, scope)
            if outside:
                raise DeliveryError("worker changed paths outside Story scope: " +
                                    ", ".join(outside))
            tests = repository_test_command(worktree)
            # Not an engine: the delivered change's own tests have nothing to
            # log in to, so they run without any credential.
            with obs.Activity("delivery-worker", "tests", "running",
                              trace_id=delivery_trace, repo=repo,
                              story=story_number, project=project_number):
                run(tests, cwd=worktree, timeout=bounds.timeout,
                    env=tool_environment(), runner=runner)
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
    obs.process_event("delivery.pull-request.written", trace_id=delivery_trace,
                      repo=repo, story=story_number, project=project_number,
                      pull_request=fresh["number"], head=head)
    return Delivery(story_number, project_number, branch, fresh["number"], head, False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--story", required=True, type=int)
    parser.add_argument("--checkout", default=str(ROOT))
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--reservation")
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("delivery failed: no GH_TOKEN/GITHUB_TOKEN", file=sys.stderr)
        return 2
    if not args.reservation or not re.fullmatch(r"[0-9a-f]{32}", args.reservation):
        print("delivery failed: a valid admission reservation is required",
              file=sys.stderr)
        return 2
    try:
        with checkout_for_repo(args.repo, pathlib.Path(args.checkout)) as checkout:
            result = execute(args.repo, args.story, token, checkout,
                             timeout=args.timeout, max_usd=args.max_usd,
                             reservation=args.reservation)
    except Exception as exc:
        obs.operational_log("ERROR", "delivery worker failed", exc=exc,
                            component="delivery-worker", operation="delivery",
                            repo=args.repo, story=args.story,
                            platform_diagnostics=platform_diagnostics(
                                pathlib.Path(args.checkout)))
        print(f"delivery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        # The engine's own account, when one exists, travels with the failure:
        # the launcher records this stream on worker.launch.end, which is where
        # a poison report goes looking for an explanation (#330).
        output = getattr(exc, "output", "")
        if output:
            print(f"engine output tail:\n{runlog.tail(output)}", file=sys.stderr)
        return 1
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
