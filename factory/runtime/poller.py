#!/usr/bin/env python3
"""Persistent dispatcher runtime — poll, parse, wake the worker.

Phase 2 Runtime 1 (#65). This is the last piece of the relay: before it, a human
read an issue number and typed `work #N` to start work the labels had already
authorized. The loop below does that instead.

Design rule, and the reason this file is small: **the runtime holds no judgment.**
It does not decide what is authorized, eligible, in-scope, or within WIP; it does
not recover leases, and it decides nothing about a story's lifecycle. Every one
of those decisions belongs to `factory/dispatcher/dispatcher.py`, which this
invokes as a subprocess and whose stdout it reads, or to the narrow modules
beside this one. Everything protecting the repository from a bad dispatch — the
authorization chain, the trust boundary, WIP and attempt limits, the required
merge gate — sits upstream of here. If this file ever grows a policy decision,
that is the bug.

What it does, per poll:

    1. consume any human decision comment sitting on a project at a bell
    2. run the dispatcher with claiming enabled
    3. read only canonical `DISPATCH story=#N project=#P agent=<id>` lines
    4. launch the configured worker once per line
    5. ask `completion.py` whether the finished worker ends its story

Step 1 exists because a bell is rung as a *comment on an existing issue*, which
new-issue discovery cannot see — observed live three times (#55, #61, #66),
each needing a human to type `work #N` to continue work already approved. It
runs before dispatch so an approval and the work it unblocks land in one cycle.

Step 5 exists because a Story that succeeds and is never closed does not stay
still: `story:claimed` is a lease, so after `CLAIM_LEASE` the dispatcher recovers
it and a second worker is dispatched onto work that is already done (#104). The
conditions and the transition are `completion.py`'s; this file only asks.

Idempotency comes from GitHub, not from here. A claimed story is no longer
`story:ready`, so the next poll's dispatcher simply does not emit it. The
in-process `seen` set is a belt-and-braces guard against double-launching inside
a single *cycle* — it must not survive the cycle it was set in. A set built
once per process silently skipped every redispatch: a Story returned to
`story:ready` by review findings was claimed again and no worker was ever
launched (#328 cycled ready → claimed → in-review on 2026-08-23 with head
6147e002 unchanged while the reviewer re-read identical code). `cycle()` owns
the guard's lifetime; nothing persists it, and a restart with no local state
re-derives identical behaviour.

Usage:
    poller.py --repo owner/name --commitment 54 [--interval 60] [--once]
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import tempfile
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "dispatcher"))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import completion   # noqa: E402  — post-worker-success transition (#104)
import continuation  # noqa: E402  — decision-comment consumption (#71)
import dispatcher   # noqa: E402  — live issue/PR substrate for Phase 4 review routing
import humanqueue   # noqa: E402  — what is waiting on a person (#111)
import planning_route  # noqa: E402  — project planning invocation route (#190)
import review_link  # noqa: E402  — delivery-PR lifecycle reconciliation (#111)
import review_route  # noqa: E402  — exact-head advisory review routing (#215)
import observability as obs  # noqa: E402
import readiness_receipt  # noqa: E402
import runlog       # noqa: E402  — operational record (#104)
import sequencer    # noqa: E402  — dependency/project lifecycle sequencing (#193)
import workers      # noqa: E402  — standard worker contract (#84)
from factory.capacity_pool.state import CapacityState, default_state_path  # noqa: E402

DISPATCHER = os.path.join(HERE, "..", "dispatcher", "dispatcher.py")

# The canonical dispatch line, and nothing else. Anchored at both ends and
# strict about every field: this is the one string that crosses the boundary
# between deciding and doing, so a near-miss must not be read as a dispatch.
DISPATCH_RE = re.compile(
    r"^DISPATCH story=#(?P<story>\d+) project=#(?P<project>\d+) "
    r"agent=(?P<agent>[a-z0-9][a-z0-9-]{2,31}) "
    r"reservation=(?P<reservation>[0-9a-f]{32})$"
)

DEFAULT_INTERVAL = 60
MAX_IDLE_INTERVAL = 300


class PollerAlreadyRunning(Exception):
    """The same repository/commitment poller already owns the local lock."""


def poller_lock_path(repo: str, commitment: int,
                     root: str | None = None) -> str:
    digest = hashlib.sha256(
        f"{readiness_receipt.canonical_repo(repo)}#{commitment}".encode()
    ).hexdigest()[:20]
    return os.path.join(root or tempfile.gettempdir(),
                        f"factory-poller-{digest}.lock")


def acquire_poller_lock(repo: str, commitment: int,
                        root: str | None = None):
    path = poller_lock_path(repo, commitment, root)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = io.TextIOWrapper(io.FileIO(descriptor, "r+", closefd=True),
                              encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.seek(0)
        holder = handle.read().strip() or "unknown process"
        handle.close()
        raise PollerAlreadyRunning(holder) from exc
    handle.seek(0); handle.truncate()
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    handle.write(
        f"pid={os.getpid()} started_at={started_at} "
        f"repo={readiness_receipt.canonical_repo(repo)} commitment={commitment}\n")
    handle.flush(); os.fsync(handle.fileno())
    return handle


class MalformedDispatch(Exception):
    """A line began with DISPATCH but is not the canonical form. Fail closed:
    something upstream changed shape, and guessing at intent is how a runtime
    launches work nobody authorized."""


class DispatcherFailed(Exception):
    """The dispatcher exited non-zero. It fails closed by design, so this is
    reported and the poll is skipped — never treated as 'no work to do'."""


class WorkerLaunchFailed(Exception):
    """The worker adapter could not be launched. The claim has already landed in
    GitHub, so this is loud: the story is claimed but nothing is working it."""

    def __init__(self, message: str, *, definite: bool = False,
                 attempt_started: bool | None = None,
                 mutation_state: str = "none", terminal_outcome: str = "",
                 recovery_ref: str = ""):
        super().__init__(message)
        self.definite = definite
        self.attempt_started = attempt_started
        self.mutation_state = mutation_state
        self.terminal_outcome = terminal_outcome
        self.recovery_ref = recovery_ref


class RateLimitExhausted(Exception):
    """GitHub told the whole cycle to stop making requests."""

    def __init__(self, reset_epoch: int | None = None):
        self.reset_epoch = reset_epoch
        super().__init__("GitHub API rate limit exhausted")


class PollResult(list):
    """Dispatch list with a flag for non-dispatch lifecycle progress."""

    def __init__(self):
        super().__init__()
        self.changed = False


def rate_limit_from(error: BaseException) -> RateLimitExhausted | None:
    """Classify only explicit GitHub limit responses; ordinary 403s stay errors."""
    if not isinstance(error, urllib.error.HTTPError) or error.code not in (403, 429):
        return None
    headers = error.headers or {}
    remaining = headers.get("X-RateLimit-Remaining")
    retry_after = headers.get("Retry-After")
    if remaining != "0" and retry_after is None:
        return None
    reset = headers.get("X-RateLimit-Reset")
    try:
        reset_epoch = int(reset) if reset else int(time.time()) + int(retry_after or 60)
    except (TypeError, ValueError):
        reset_epoch = int(time.time()) + 60
    return RateLimitExhausted(reset_epoch)


def rate_limit_delay(error: RateLimitExhausted, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return max(60, int((error.reset_epoch or int(now) + 60) - now) + 1)


def adaptive_interval(base: int, current: int, active: bool,
                      maximum: int = MAX_IDLE_INTERVAL) -> int:
    """Return to the requested interval on activity; double while idle."""
    return base if active else min(maximum, max(base, current * 2))


def parse_dispatches(stdout: str) -> list[dict]:
    """Extract canonical dispatch lines. Raises on a near-miss.

    Lines that do not mention DISPATCH at all are ordinary dispatcher report
    output and are ignored. A line that starts with the token but does not match
    exactly is an error, not a shrug.
    """
    dispatches = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("DISPATCH"):
            continue
        match = DISPATCH_RE.match(line)
        if not match:
            raise MalformedDispatch(line)
        dispatches.append({
            "story": int(match.group("story")),
            "project": int(match.group("project")),
            "agent": match.group("agent"),
            "reservation": match.group("reservation"),
        })
    return dispatches


def run_dispatcher(repo: str, commitment: int, claim: bool,
                   python: str = sys.executable) -> str:
    """Invoke the dispatcher and return its stdout."""
    cmd = [python, DISPATCHER, "--repo", repo, "--commitment", str(commitment)]
    if claim:
        cmd.append("--claim")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise DispatcherFailed(
            f"exit {result.returncode}: {(result.stderr or result.stdout).strip()[:400]}")
    return result.stdout


def worker_command(dispatch: dict) -> list[str]:
    """Resolve the single-worker adapter for a dispatch.

    Legacy single-worker path, kept for the simple case and for configurations
    predating the worker contract. `FACTORY_WORKER_CMD` is its extension point.
    When `FACTORY_WORKER_ORDER` declares workers, `wake_worker` routes through
    `workers.py` instead and this is not used.

    Note what is *not* passed: no spec, no scope, no acceptance criteria. The
    worker reconstructs context from GitHub itself (`architecture-v2.1.md` §4 —
    the moment a queue item copies business context, the relay has been rebuilt
    as infrastructure).
    """
    template = os.environ.get("FACTORY_WORKER_CMD", "").strip()
    if not template:
        # Default adapter: announce on stdout. Under the repository's existing
        # monitor, one stdout line is one notification to the delivery worker,
        # which is the wake-up. No parallel orchestration framework is created.
        return ["printf", "%s\n",
                f"WAKE worker={dispatch['agent']} story=#{dispatch['story']} "
                f"project=#{dispatch['project']}"]
    parts = shlex.split(template)
    return [p.replace("{story}", str(dispatch["story"]))
             .replace("{project}", str(dispatch["project"]))
             .replace("{agent}", dispatch["agent"])
             .replace("{reservation}", dispatch.get("reservation", "")) for p in parts]


def story_launch_bound(repo: str, story: int) -> int | None:
    """The launch bound for this dispatch, from the Story's own `### Spend cap`.

    The launcher must not kill the worker before the bound the Story set
    (#345). A fetch failure selects the fallback (None) rather than blocking
    the dispatch: a Story that cannot be read is the worker's problem to
    report, not a reason to never start it.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    try:
        issue = dispatcher.fetch_issue(repo, story, token)
        return workers.launch_timeout((issue or {}).get("body"))
    except Exception:  # noqa: BLE001 — fallback, never a blocked dispatch
        return None


def release_unstarted_reservation(reservation: str) -> bool:
    """Return true only when durable capacity state proves no model start."""
    root = pathlib.Path(__file__).resolve().parents[2]
    path = default_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = CapacityState(path, uri=False)
    try:
        status = state.lease_status(reservation)
        if status in {"active", "expired"}:
            return state.release(reservation)
        return status == "released"
    finally:
        state.close()


def wake_worker(dispatch: dict,
                timeout_s: int | None = None) -> workers.LaunchReport:
    """Hand one dispatch to a worker. Returns the report of the one that ran.

    With workers declared (#84), routing goes through the standard contract:
    health and capability decide eligibility, configured order decides
    preference, and failover is sequential and stops at the first success. An
    ambiguous launch outcome does **not** fall back — the worker may be running,
    and a second worker on one Story is worse than a late one. The claim stays
    in GitHub either way, and the bounded §9.4 lease recovers it if nothing
    started.

    With no workers declared, the legacy single-command path is used unchanged.
    It is reported in the same shape so that everything downstream — the log, the
    completion pass — reads one kind of result and not two.
    """
    specs = workers.configured_workers()
    if specs:
        report, trail = workers.dispatch_to_worker(
            specs, dispatch["story"], dispatch["project"],
            reservation=dispatch["reservation"], timeout_s=timeout_s)
        for entry in trail:
            reason = getattr(entry, "reason", None) or getattr(entry, "result", "")
            print(f"[worker] {entry.worker}: {reason} {entry.detail}".rstrip(), flush=True)
        if report is None:
            terminal = [entry for entry in trail
                        if getattr(entry, "result", "") ==
                        workers.Result.TERMINAL_FAILURE]
            if terminal:
                failed = terminal[-1]
                detail = (f"{failed.worker} completed after changing files; "
                          f"terminal_outcome={failed.terminal_outcome}; "
                          f"mutation_state={failed.mutation_state}; "
                          f"recovery_ref={failed.recovery_ref}")
                raise WorkerLaunchFailed(
                    detail, definite=True, attempt_started=True,
                    mutation_state=failed.mutation_state,
                    terminal_outcome=failed.terminal_outcome,
                    recovery_ref=failed.recovery_ref)
            failures = [entry for entry in trail
                        if getattr(entry, "result", "") == workers.Result.FAILED]
            if failures:
                failed = failures[-1]
                terminal = (getattr(failed, "stderr", "") or
                            getattr(failed, "stdout", "") or failed.detail)
                raise WorkerLaunchFailed(
                    f"{failed.worker} completed with failure: {terminal}",
                    definite=True, attempt_started=False)
            raise WorkerLaunchFailed(
                "no worker accepted the assignment; see the [worker] trail above",
                definite=not any(getattr(item, "result", "") == workers.Result.AMBIGUOUS
                                 for item in trail),
                attempt_started=(None if any(
                    getattr(item, "result", "") == workers.Result.AMBIGUOUS
                    for item in trail) else False))
        return report

    cmd = worker_command(dispatch)
    started = time.monotonic()
    runlog.event("worker.launch.start", worker=dispatch["agent"], story=dispatch["story"],
                 project=dispatch["project"], cmd=runlog.command(cmd), legacy=True)
    try:
        result = workers.run_observed(cmd, capture_output=True, text=True, timeout=60)
    except OSError as exc:
        runlog.event("worker.launch.end", worker=dispatch["agent"], story=dispatch["story"],
                     project=dispatch["project"], result="FAILED", legacy=True,
                     elapsed_ms=runlog.elapsed_ms(started), detail=str(exc))
        raise WorkerLaunchFailed(
            f"{cmd[0]}: {exc}", definite=True, attempt_started=False) from exc
    except subprocess.SubprocessError as exc:
        runlog.event("worker.launch.end", worker=dispatch["agent"], story=dispatch["story"],
                     project=dispatch["project"], result="AMBIGUOUS", legacy=True,
                     elapsed_ms=runlog.elapsed_ms(started), detail=str(exc))
        raise WorkerLaunchFailed(
            f"{cmd[0]}: {exc}", definite=False, attempt_started=None) from exc
    report = workers.report_from(dispatch["agent"], result, started)
    runlog.event("worker.launch.end", worker=report.worker, story=dispatch["story"],
                 project=dispatch["project"], result=report.result, legacy=True,
                 exit=report.exit_code, pid=report.pid, elapsed_ms=report.elapsed_ms,
                 stdout=report.stdout, stderr=report.stderr,
                 mutation_state=report.mutation_state,
                 terminal_outcome=report.terminal_outcome,
                 recovery_ref=report.recovery_ref,
                 attempt_started=report.attempt_started)
    if not report.launched:
        raise WorkerLaunchFailed(
            f"{cmd[0]} exited {result.returncode}: {(result.stderr or '').strip()[:200]}",
            definite=True, attempt_started=report.attempt_started,
            mutation_state=report.mutation_state,
            terminal_outcome=report.terminal_outcome,
            recovery_ref=report.recovery_ref)
    return report


def run_continuation(repo: str, claim: bool = True) -> list:
    """Consume human decisions on projects waiting at bells.

    Kept in its own module: continuation reads §5 decision comments, which is a
    different question from dispatch eligibility, and it is worker-neutral —
    advancing a lifecycle is not work assigned to anyone.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    return continuation.run(repo, token, apply=claim)


def complete_story(dispatch: dict, report, claim: bool) -> None:
    """Ask the completion path whether the finished worker ends its Story.

    Note what this function does *not* contain: no condition, no state, no
    target label. It hands over identity and a launch verdict and prints what
    comes back. Every precondition — still claimed, no linked pull request,
    durable acknowledgement after the claim instant — and the transition itself
    live in `completion.py`, because the runtime holding no judgment is the
    property that keeps the factory auditable, and "just one check here" is how
    that property is lost.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    outcome = completion.record_success(
        repo=dispatch["repo"], story=dispatch["story"], project=dispatch["project"],
        launch_result=report.result, worker=report.worker, token=token, apply=claim)
    if outcome.action:
        print(f"[completion] #{outcome.number}: {outcome.action} "
              f"({outcome.reason})", flush=True)
    else:
        print(f"[completion] #{outcome.number} left as-is: {outcome.reason}"
              + (f" — {outcome.detail}" if outcome.detail else ""), flush=True)


def run_review_link(repo: str, claim: bool = True,
                    commitment: int | None = None) -> list:
    """Turn a delivery pull request into the lifecycle transitions it implies.

    Kept in its own module for the same reason continuation and completion are:
    whether a pull request is open or merged is a different question from
    dispatch eligibility, and GitHub's merge state is a verdict the delivering
    credential cannot fabricate (§9.14) — which is what makes these two
    transitions mechanical at all.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    return review_link.run(repo, token, apply=claim, commitment=commitment)


def review_targets(pulls: list[dict], issues: dict[int, dict],
                   comments: dict[int, list[dict]]) -> list[review_route.ReviewTarget]:
    targets = []
    for pull in sorted(pulls, key=lambda x: x.get("number", 0)):
        # This repository can carry pull requests unrelated to the factory.
        # Zero canonical links means "not routable" and cannot advance. One is
        # factory work. More than one remains an ambiguity that fails closed in
        # story_number below.
        if not review_route.LINK.findall(
                (pull.get("body") or "").replace("\r\n", "\n")):
            continue
        try:
            number = review_route.story_number(pull)
            if number not in issues:
                continue
            value = review_route.target(pull, issues.get(number, {}), comments.get(number, []))
        except review_route.RouteError as exc:
            raise WorkerLaunchFailed(
                f"review route failed closed for PR #{pull.get('number')}: {exc}") from exc
        if value is not None:
            targets.append(value)
    return targets


def wake_reviewer(repo: str, target: review_route.ReviewTarget) -> None:
    wrapper = os.path.join(HERE, "..", "agents", "review", "run.sh")
    template = os.environ.get("FACTORY_REVIEW_CMD", "").strip()
    command = ([x.replace("{repo}", repo).replace("{pull_request}", str(target.pull_request))
                for x in shlex.split(template)] if template
               else [wrapper, repo, str(target.pull_request)])
    # Keep stdout captured because poller stdout is a notification channel, but
    # let the reviewer's structured progress events flow live on stderr.
    # The wrapper owns the reviewer's three-minute internal bound. Give it a
    # separate cleanup margin so the outer poller cannot kill it at the exact
    # instant it is writing a bounded failure or durable approval.
    result = subprocess.run(
        command, stdout=subprocess.PIPE, text=True,
        timeout=int(os.environ.get("FACTORY_REVIEW_LAUNCH_TIMEOUT", "210")))
    if result.returncode:
        raise WorkerLaunchFailed(
            f"review PR #{target.pull_request} failed: {(result.stdout or '')[:400]}")


def route_merge(repo: str, pull: dict, comments: list[dict], *, apply=True) -> bool:
    """Merge only the exact head approved by the independent reviewer.

    Persistent auto-merge is deliberately forbidden here.  GitHub preserves an
    enabled auto-merge request when a branch is force-pushed, so an approval for
    head A can otherwise merge head B.  ``--match-head-commit`` makes the merge
    compare-and-swap: GitHub rejects it if the head changed after this read.
    """
    if not review_link.exact_head_approved(pull, comments):
        return False
    if pull.get("mergeable_state") != "clean":
        return False
    if apply:
        head = (pull.get("head") or {}).get("sha", "")
        if not head:
            raise WorkerLaunchFailed(
                f"exact-head merge routing failed for PR #{pull.get('number')}: "
                "GitHub returned no head SHA")
        result = subprocess.run(["gh", "pr", "merge", str(pull["number"]), "--repo", repo,
                                 "--squash", "--match-head-commit", head],
                                capture_output=True, text=True)
        if result.returncode:
            raise WorkerLaunchFailed(
                f"exact-head merge routing failed: "
                f"{(result.stderr or result.stdout)[:400]}")
    return True


def disable_legacy_auto_merge(repo: str, pulls: list[dict], *, apply=True) -> set[int]:
    """Remove sticky auto-merge left by factory versions before Story #586."""
    disabled = set()
    for pull in pulls:
        if (pull.get("state") or "").lower() != "open":
            continue
        if not review_route.LINK.findall(
                (pull.get("body") or "").replace("\r\n", "\n")):
            continue
        if not pull.get("auto_merge"):
            continue
        if apply:
            result = subprocess.run(
                ["gh", "pr", "merge", str(pull["number"]), "--repo", repo,
                 "--disable-auto"], capture_output=True, text=True)
            if result.returncode:
                raise WorkerLaunchFailed(
                    f"legacy auto-merge disable failed for PR #{pull['number']}: "
                    f"{(result.stderr or result.stdout)[:400]}")
        disabled.add(pull["number"])
    return disabled


def refresh_behind_branches(repo: str, pulls: list[dict], issues: dict[int, dict],
                            *, apply: bool = True) -> set[int]:
    """Refresh factory Story PRs that cannot merge against the current base.

    Updating the branch changes its head SHA.  The caller therefore excludes it
    from this review pass; the next poll re-runs checks and obtains a fresh-head
    review before enabling auto-merge.
    """
    refreshed = set()
    if not apply:
        return refreshed
    try:
        targets = review_route.behind_targets(pulls, issues)
    except review_route.RouteError as exc:
        raise WorkerLaunchFailed(f"stale branch route failed closed: {exc}") from exc
    for number in targets:
        result = subprocess.run(
            ["gh", "pr", "update-branch", str(number), "--repo", repo],
            capture_output=True, text=True)
        if result.returncode:
            raise WorkerLaunchFailed(
                f"stale branch refresh failed for PR #{number}: "
                f"{(result.stderr or result.stdout)[:400]}")
        refreshed.add(number)
    return refreshed


def hydrate_review_pulls(repo: str, pulls: list[dict], token: str) -> list[dict]:
    """Read detail records for open factory-linked PR list entries.

    GitHub's pull-request list response omits ``mergeable_state``.  The detail
    endpoint computes it, so stale-branch routing must consume that record.
    Unrelated pull requests retain their list record and cost no extra request.
    """
    hydrated = []
    for pull in pulls:
        linked = review_route.LINK.findall(
            (pull.get("body") or "").replace("\r\n", "\n"))
        if pull.get("state") == "open" and not pull.get("draft") and linked:
            pull = dispatcher._api(
                f"https://api.github.com/repos/{repo}/pulls/{pull['number']}", token)
        hydrated.append(pull)
    return hydrated


def run_phase4_reviews(repo: str, apply: bool = True,
                       commitment: int | None = None) -> list[review_route.ReviewTarget]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    all_issues = dispatcher.fetch_issues(repo, token)
    allowed = (review_link.authorized_story_numbers(all_issues, commitment)
               if commitment is not None else set(all_issues))
    issues = {number: issue for number, issue in all_issues.items() if number in allowed}
    all_pulls = dispatcher.fetch_pull_requests(repo, token)
    pulls = []
    for pull in all_pulls:
        try:
            story = review_route.story_number(pull)
        except review_route.RouteError:
            continue
        if story in issues:
            pulls.append(pull)
    pulls = hydrate_review_pulls(repo, pulls, token)
    comments = {number: dispatcher._api(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100", token)
                for number in issues}
    disable_legacy_auto_merge(repo, pulls, apply=apply)
    refreshed = refresh_behind_branches(repo, pulls, issues, apply=apply)
    targets = review_targets(
        [pull for pull in pulls if pull.get("number") not in refreshed], issues, comments)
    reviewed_pulls = {}
    for target in targets:
        if not apply:
            continue
        wake_reviewer(repo, target)
        fresh_pull = dispatcher._api(
            f"https://api.github.com/repos/{repo}/pulls/{target.pull_request}", token)
        fresh_comments = dispatcher._api(
            f"https://api.github.com/repos/{repo}/issues/{target.story}/comments?per_page=100",
            token)
        reviewed_pulls[target.pull_request] = (fresh_pull, fresh_comments)

    # Retry exact-head-approved PRs on every pass.  This is what replaces sticky
    # auto-merge: checks may still be pending when review finishes, so a later
    # poll performs the atomic merge once GitHub reports the head clean.
    for candidate, candidate_comments in reviewed_pulls.values():
        route_merge(repo, candidate, candidate_comments, apply=apply)
    for pull in pulls:
        if pull.get("number") in reviewed_pulls:
            continue
        try:
            story = review_route.story_number(pull)
        except review_route.RouteError:
            continue
        route_merge(repo, pull, comments.get(story, []), apply=apply)
    return targets


def run_human_queue(repo: str) -> list:
    """Say what is waiting on a person. Reads durable state, writes nothing."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    return humanqueue.run(repo, token)


def run_sequencer(repo: str, commitment: int, claim: bool = True) -> list:
    """Advance dependency-ready stories and fully delivered projects."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    return sequencer.run(repo, token, apply=claim, commitment=commitment)


def run_planning_route(repo: str, claim: bool = True) -> list[int]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    return planning_route.run(repo, token, apply=claim)


def wake_planner(repo: str, artifact: int) -> None:
    wrapper = os.path.join(HERE, "..", "agents", "planning", "run.sh")
    template = os.environ.get("FACTORY_PLANNING_CMD", "").strip()
    command = ([part.replace("{repo}", repo).replace("{artifact}", str(artifact))
                for part in shlex.split(template)] if template
               else [wrapper, repo, str(artifact)])
    model_timeout = int(os.environ.get("FACTORY_PLANNING_TIMEOUT", "900"))
    outer_timeout = int(os.environ.get(
        "FACTORY_PLANNING_OUTER_TIMEOUT", str(model_timeout + 120)))
    if outer_timeout <= model_timeout:
        raise WorkerLaunchFailed(
            "planning outer timeout must exceed the logical model timeout")
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=outer_timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WorkerLaunchFailed(
            f"planning artifact #{artifact} failed ({result.returncode}): "
            f"{detail[-400:]}")
    if result.stdout:
        print(f"[planning] #{artifact}: {result.stdout.strip()}", flush=True)


def cycle(repo: str, commitment: int, claim: bool = True) -> list[dict]:
    """One poll with a fresh intra-cycle duplicate guard.

    The guard exists to stop one cycle double-launching a story its own
    dispatcher printed twice; scoped any wider it becomes a skip list that
    outlives the redispatch it is supposed to allow (#342).
    """
    return poll_once(repo, commitment, set(), claim=claim)


def poll_once(repo: str, commitment: int, seen: set[int],
              claim: bool = True) -> PollResult:
    """One cycle. Returns the dispatches that produced a wake-up."""
    woken = PollResult()
    # Reconciliation runs first: a story whose delivery merged should leave
    # `story:claimed` before WIP is counted, or finished work keeps a worker slot
    # it no longer needs. Isolated like every other pass — one failing pass must
    # never stop authorized work from dispatching.
    def isolated(name, function):
        try:
            with obs.Activity("poller", name, "running", repo=repo,
                              commitment=commitment) as activity:
                result = function()
                activity.progress("finished")
                if result:
                    woken.changed = True
                return result
        except Exception as exc:  # noqa: BLE001 - passes intentionally isolate failure
            limited = rate_limit_from(exc)
            if limited is not None:
                raise limited from exc
            obs.operational_log("ERROR", f"{name} pass failed; poll continues",
                                exc=exc, component="poller", operation=name,
                                repo=repo, commitment=commitment)
            return None

    isolated("review-link", lambda: run_review_link(
        repo, claim, commitment=commitment))

    if os.environ.get("FACTORY_PHASE4_REVIEWS") == "1":
        isolated("independent-review", lambda: run_phase4_reviews(
            repo, claim, commitment=commitment))

    # A decision consumed now can unblock work the same cycle. Isolated: a
    # continuation failure must not stop already-authorized work from
    # dispatching — the two answer different questions, and coupling their
    # failure modes would let a malformed comment halt the whole factory.
    isolated("continuation", lambda: run_continuation(repo, claim))

    # Sequencing consumes the active project created by continuation above and
    # may expose ready stories to dispatch in this same cycle.
    isolated("sequencer", lambda: run_sequencer(repo, commitment, claim))

    # Claim each planning transition before invoking. A duplicate poll sees
    # `project:planning`, so GitHub state suppresses duplicate launches.
    try:
        for artifact in isolated("planning-route", lambda: run_planning_route(repo, claim)) or []:
            if claim:
                with obs.Activity("planner", "planning", "launching", repo=repo,
                                  artifact=artifact):
                    wake_planner(repo, artifact)
    except Exception as exc:  # noqa: BLE001 — loud, dispatch remains isolated
        obs.operational_log("ERROR", "planning launch failed; poll continues",
                            exc=exc, component="poller", operation="planning",
                            repo=repo, commitment=commitment, artifact=artifact)

    # Said on every poll, after the passes that can change what is waiting and
    # before dispatch, so the list describes this cycle rather than the last one.
    isolated("human-queue", lambda: run_human_queue(repo))

    stdout = run_dispatcher(repo, commitment, claim)
    if not claim:
        # A dry run is the production entrypoint's readiness view. Preserve the
        # dispatcher's read-only decision so operators and acceptance tooling can
        # see capacity and the exact Stories that would be selected without
        # invoking the dispatcher out of band.
        print("[poller] dispatcher dry-run plan", flush=True)
        print(stdout.rstrip(), flush=True)
    for dispatch in parse_dispatches(stdout):
        if dispatch["story"] in seen:
            # Belt and braces only, and only within this cycle: GitHub already
            # prevents this by not re-offering a claimed story.
            print(f"[poller] already woken this cycle, skipping story "
                  f"#{dispatch['story']}", flush=True)
            continue
        attempt_trace = None
        try:
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
            attempt_trace = obs.story_trace_id(
                repo, dispatch["story"],
                dispatcher.fetch_timeline(repo, dispatch["story"], token))
        except Exception as trace_error:  # noqa: BLE001 - dispatch remains authorized
            obs.operational_log("ERROR", "dispatch trace could not be read back",
                                exc=trace_error, component="poller", operation="dispatch",
                                repo=repo, story=dispatch["story"],
                                project=dispatch["project"])
        obs.process_event("dispatch.received", trace_id=attempt_trace,
                          story=dispatch["story"], project=dispatch["project"],
                          agent=dispatch["agent"], repo=repo)
        with obs.Activity("poller", "worker-delivery", "launching", repo=repo,
                          story=dispatch["story"], project=dispatch["project"],
                          worker=dispatch["agent"], trace_id=attempt_trace) as activity:
            try:
                report = wake_worker(
                    dispatch, timeout_s=story_launch_bound(repo, dispatch["story"]))
            except WorkerLaunchFailed as exc:
                if not (claim and exc.definite):
                    raise
                token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
                evidence = os.path.join(os.environ.get("FACTORY_RUN_DIR", "run log"),
                                        "process-events.jsonl")
                recovery_reason = str(exc)
                if exc.attempt_started is True:
                    recovery = dispatcher.release_definite_failure
                    recovery_reason += (
                        f"; mutation_state={exc.mutation_state}"
                        f"; terminal_outcome={exc.terminal_outcome}"
                        f"; recovery_ref={exc.recovery_ref}")
                elif exc.attempt_started is False:
                    try:
                        unstarted = release_unstarted_reservation(
                            dispatch["reservation"])
                    except Exception as recovery_error:
                        raise exc from recovery_error
                    if not unstarted:
                        raise WorkerLaunchFailed(
                            f"{exc}; worker outcome is not proven safe for "
                            "Attempt release",
                            definite=False, attempt_started=False) from exc
                    recovery = dispatcher.release_unstarted_failure
                else:
                    raise WorkerLaunchFailed(
                        "worker outcome is not proven safe for Attempt release",
                        definite=False, attempt_started=None) from exc
                try:
                    released, detail = recovery(
                        repo, dispatch["story"], token,
                        reason=recovery_reason, evidence=evidence)
                except Exception as recovery_error:  # claim remains safe and visible
                    raise exc from recovery_error
                if not released:
                    raise WorkerLaunchFailed(
                        f"confirmed failure could not release claim: {detail}",
                        definite=True) from exc
                print(f"[recovery] Story #{dispatch['story']}: {detail}", flush=True)
                continue
            activity.progress("worker-returned", result=report.result)
        seen.add(dispatch["story"])
        # Report the engine that actually ran, not the agent named on the
        # DISPATCH line. Under failover those differ, and an audit trail that
        # says Claude ran when Codex did is worse than no trail at all.
        print(f"[poller] woke {report.worker} for story #{dispatch['story']} "
              f"(project #{dispatch['project']})", flush=True)
        if report.detail:
            print(f"{report.worker}: {report.detail}", flush=True)
        # Isolated exactly as continuation is, and for the same reason: a
        # completion failure must not stop the poll. The Story stays claimed,
        # which the §9.4 lease already knows how to resolve.
        try:
            complete_story({**dispatch, "repo": repo}, report, claim)
        except Exception as exc:  # noqa: BLE001 — reported, never fatal to the poll
            obs.operational_log("ERROR", "completion failed; claim left unchanged",
                                exc=exc, component="poller", operation="completion",
                                repo=repo, story=dispatch["story"],
                                project=dispatch["project"])
        woken.append(dispatch)
        woken.changed = True
    return woken
def run_loop(args) -> int:
    print(f"[poller] watching {args.repo} against commitment #{args.commitment}, "
          f"every {args.interval}s"
          + (" (dry run — no claims)" if args.dry_run else ""), flush=True)

    with obs.bound_context(repo=args.repo, commitment=args.commitment,
                           component="poller"):
      delay = args.interval
      while True:
        try:
            with obs.Activity("poller", "cycle", "reconciling", repo=args.repo,
                              commitment=args.commitment):
                result = cycle(args.repo, args.commitment, claim=not args.dry_run)
                delay = adaptive_interval(
                    args.interval, delay,
                    bool(result) or getattr(result, "changed", False),
                    args.max_idle_interval)
        except RateLimitExhausted as exc:
            delay = rate_limit_delay(exc)
            reset = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()
            print(f"[poller] PAUSED: GitHub rate limit exhausted; no more API calls "
                  f"until {reset}", flush=True)
            if args.once:
                return 75
        except MalformedDispatch as exc:
            print(f"[poller] FAIL: dispatcher emitted a non-canonical DISPATCH line, "
                  f"so no worker was launched: {exc}", flush=True)
        except DispatcherFailed as exc:
            print(f"[poller] FAIL: dispatcher error, nothing dispatched: {exc}", flush=True)
        except WorkerLaunchFailed as exc:
            print(f"[poller] FAIL: story is claimed in GitHub but the worker did not "
                  f"start: {exc}. The claim is left alone — this runtime never edits "
                  f"lifecycle to compensate.", flush=True)
        except Exception as exc:  # noqa: BLE001 — a crash must not look like idleness
            obs.operational_log("CRITICAL", "poll cycle crashed", exc=exc,
                                component="poller", operation="cycle",
                                repo=args.repo, commitment=args.commitment)
            print(f"[poller] FAIL: {type(exc).__name__}: {exc}", flush=True)

        if args.once:
            return 0
        time.sleep(delay)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Persistent dispatcher runtime (#65)")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--commitment", required=True, type=int,
                        help="issue number of the standing roadmap commitment")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"active/base seconds between polls (default {DEFAULT_INTERVAL})")
    parser.add_argument("--max-idle-interval", type=int, default=MAX_IDLE_INTERVAL,
                        help=f"idle backoff ceiling (default {MAX_IDLE_INTERVAL})")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="do not claim; useful for observing decisions")
    parser.add_argument("--readiness-receipt",
                        help="doctor receipt path (default: scoped temp path)")
    args = parser.parse_args(argv)

    if args.interval < 1 or args.max_idle_interval < args.interval:
        parser.error("--interval must be positive and --max-idle-interval must be >= it")

    if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
        print("[poller] FAIL: no GITHUB_TOKEN/GH_TOKEN. The dispatcher cannot read "
              "durable state, so nothing is polled. Fail closed.", flush=True)
        return 1

    try:
        singleton = acquire_poller_lock(args.repo, args.commitment)
    except PollerAlreadyRunning as exc:
        print(f"[poller] REFUSED: another poller already owns {args.repo} "
              f"commitment #{args.commitment} ({exc})", flush=True)
        return 73
    try:
        if not args.dry_run:
            path = (pathlib.Path(args.readiness_receipt)
                    if args.readiness_receipt else
                    readiness_receipt.default_path(args.repo, args.commitment))
            try:
                readiness_receipt.validate(
                    path, repo=args.repo, commitment=args.commitment,
                    revision=readiness_receipt.factory_revision(
                        pathlib.Path(HERE).parents[1]), environ=dict(os.environ))
            except readiness_receipt.ReceiptError as exc:
                print(f"[poller] BLOCKED: doctor readiness receipt refused: {exc}",
                      flush=True)
                return 78
        return run_loop(args)
    finally:
        singleton.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
