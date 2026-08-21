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
a single run; it is not a cursor, nothing persists it, and a restart with no
local state re-derives identical behaviour.

Usage:
    poller.py --repo owner/name --commitment 54 [--interval 60] [--once]
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import completion   # noqa: E402  — post-worker-success transition (#104)
import continuation  # noqa: E402  — decision-comment consumption (#71)
import humanqueue   # noqa: E402  — what is waiting on a person (#111)
import planning_route  # noqa: E402  — project planning invocation route (#190)
import review_link  # noqa: E402  — delivery-PR lifecycle reconciliation (#111)
import runlog       # noqa: E402  — operational record (#104)
import sequencer    # noqa: E402  — dependency/project lifecycle sequencing (#193)
import workers      # noqa: E402  — standard worker contract (#84)

DISPATCHER = os.path.join(HERE, "..", "dispatcher", "dispatcher.py")

# The canonical dispatch line, and nothing else. Anchored at both ends and
# strict about every field: this is the one string that crosses the boundary
# between deciding and doing, so a near-miss must not be read as a dispatch.
DISPATCH_RE = re.compile(
    r"^DISPATCH story=#(?P<story>\d+) project=#(?P<project>\d+) agent=(?P<agent>[a-z0-9][a-z0-9-]{2,31})$"
)

DEFAULT_INTERVAL = 60


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
             .replace("{agent}", dispatch["agent"]) for p in parts]


def wake_worker(dispatch: dict) -> workers.LaunchReport:
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
            specs, dispatch["story"], dispatch["project"])
        for entry in trail:
            reason = getattr(entry, "reason", None) or getattr(entry, "result", "")
            print(f"[worker] {entry.worker}: {reason} {entry.detail}".rstrip(), flush=True)
        if report is None:
            raise WorkerLaunchFailed(
                "no worker accepted the assignment; see the [worker] trail above")
        return report

    cmd = worker_command(dispatch)
    started = time.monotonic()
    runlog.event("worker.launch.start", worker=dispatch["agent"], story=dispatch["story"],
                 project=dispatch["project"], cmd=runlog.command(cmd), legacy=True)
    try:
        result = workers.run_observed(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        runlog.event("worker.launch.end", worker=dispatch["agent"], story=dispatch["story"],
                     project=dispatch["project"], result="FAILED", legacy=True,
                     elapsed_ms=runlog.elapsed_ms(started), detail=str(exc))
        raise WorkerLaunchFailed(f"{cmd[0]}: {exc}") from exc
    report = workers.report_from(dispatch["agent"], result, started)
    runlog.event("worker.launch.end", worker=report.worker, story=dispatch["story"],
                 project=dispatch["project"], result=report.result, legacy=True,
                 exit=report.exit_code, pid=report.pid, elapsed_ms=report.elapsed_ms,
                 stdout=report.stdout, stderr=report.stderr)
    if not report.launched:
        raise WorkerLaunchFailed(
            f"{cmd[0]} exited {result.returncode}: {(result.stderr or '').strip()[:200]}")
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


def run_review_link(repo: str, claim: bool = True) -> list:
    """Turn a delivery pull request into the lifecycle transitions it implies.

    Kept in its own module for the same reason continuation and completion are:
    whether a pull request is open or merged is a different question from
    dispatch eligibility, and GitHub's merge state is a verdict the delivering
    credential cannot fabricate (§9.14) — which is what makes these two
    transitions mechanical at all.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    return review_link.run(repo, token, apply=claim)


def run_human_queue(repo: str) -> list:
    """Say what is waiting on a person. Reads durable state, writes nothing."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    return humanqueue.run(repo, token)


def run_sequencer(repo: str, claim: bool = True) -> list:
    """Advance dependency-ready stories and fully delivered projects."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    return sequencer.run(repo, token, apply=claim)


def run_planning_route(repo: str, claim: bool = True) -> list[int]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    return planning_route.run(repo, token, apply=claim)


def wake_planner(repo: str, artifact: int) -> None:
    wrapper = os.path.join(HERE, "..", "agents", "planning", "run.sh")
    template = os.environ.get("FACTORY_PLANNING_CMD", "").strip()
    command = ([part.replace("{repo}", repo).replace("{artifact}", str(artifact))
                for part in shlex.split(template)] if template
               else [wrapper, repo, str(artifact)])
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=int(os.environ.get("FACTORY_PLANNING_TIMEOUT", "900")))
    if result.returncode != 0:
        raise WorkerLaunchFailed(
            f"planning artifact #{artifact} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout)[:400]}")
    if result.stdout:
        print(f"[planning] #{artifact}: {result.stdout.strip()}", flush=True)


def poll_once(repo: str, commitment: int, seen: set[int], claim: bool = True) -> list[dict]:
    """One cycle. Returns the dispatches that produced a wake-up."""
    # Reconciliation runs first: a story whose delivery merged should leave
    # `story:claimed` before WIP is counted, or finished work keeps a worker slot
    # it no longer needs. Isolated like every other pass — one failing pass must
    # never stop authorized work from dispatching.
    try:
        run_review_link(repo, claim)
    except Exception as exc:  # noqa: BLE001 — reported, never fatal to dispatch
        print(f"[poller] review-link pass failed, dispatch continues: "
              f"{type(exc).__name__}: {exc}", flush=True)

    # A decision consumed now can unblock work the same cycle. Isolated: a
    # continuation failure must not stop already-authorized work from
    # dispatching — the two answer different questions, and coupling their
    # failure modes would let a malformed comment halt the whole factory.
    try:
        run_continuation(repo, claim)
    except Exception as exc:  # noqa: BLE001 — reported, never fatal to dispatch
        print(f"[poller] continuation pass failed, dispatch continues: "
              f"{type(exc).__name__}: {exc}", flush=True)

    # Sequencing consumes the active project created by continuation above and
    # may expose ready stories to dispatch in this same cycle.
    try:
        run_sequencer(repo, claim)
    except Exception as exc:  # noqa: BLE001 — reported, never fatal to dispatch
        print(f"[poller] sequencer pass failed, dispatch continues: "
              f"{type(exc).__name__}: {exc}", flush=True)

    # Claim each planning transition before invoking. A duplicate poll sees
    # `project:planning`, so GitHub state suppresses duplicate launches.
    try:
        for artifact in run_planning_route(repo, claim):
            if claim:
                wake_planner(repo, artifact)
    except Exception as exc:  # noqa: BLE001 — loud, dispatch remains isolated
        print(f"[poller] planning pass failed, dispatch continues: "
              f"{type(exc).__name__}: {exc}", flush=True)

    # Said on every poll, after the passes that can change what is waiting and
    # before dispatch, so the list describes this cycle rather than the last one.
    try:
        run_human_queue(repo)
    except Exception as exc:  # noqa: BLE001 — reported, never fatal to dispatch
        print(f"[poller] human-queue pass failed, dispatch continues: "
              f"{type(exc).__name__}: {exc}", flush=True)

    stdout = run_dispatcher(repo, commitment, claim)
    woken = []
    for dispatch in parse_dispatches(stdout):
        if dispatch["story"] in seen:
            # Belt and braces only: GitHub already prevents this by not
            # re-offering a claimed story.
            print(f"[poller] already woken this run, skipping story "
                  f"#{dispatch['story']}", flush=True)
            continue
        runlog.event("dispatch.received", story=dispatch["story"],
                     project=dispatch["project"], agent=dispatch["agent"], repo=repo)
        report = wake_worker(dispatch)
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
            print(f"[poller] completion pass failed for story #{dispatch['story']}, "
                  f"the claim is left alone: {type(exc).__name__}: {exc}", flush=True)
        woken.append(dispatch)
    return woken


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Persistent dispatcher runtime (#65)")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--commitment", required=True, type=int,
                        help="issue number of the standing roadmap commitment")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"seconds between polls (default {DEFAULT_INTERVAL})")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="do not claim; useful for observing decisions")
    args = parser.parse_args(argv)

    if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
        print("[poller] FAIL: no GITHUB_TOKEN/GH_TOKEN. The dispatcher cannot read "
              "durable state, so nothing is polled. Fail closed.", flush=True)
        return 1

    print(f"[poller] watching {args.repo} against commitment #{args.commitment}, "
          f"every {args.interval}s"
          + (" (dry run — no claims)" if args.dry_run else ""), flush=True)

    seen: set[int] = set()
    while True:
        try:
            poll_once(args.repo, args.commitment, seen, claim=not args.dry_run)
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
            print(f"[poller] FAIL: {type(exc).__name__}: {exc}", flush=True)

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
