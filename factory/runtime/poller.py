#!/usr/bin/env python3
"""Persistent dispatcher runtime — poll, parse, wake the worker.

Phase 2 Runtime 1 (#65). This is the last piece of the relay: before it, a human
read an issue number and typed `work #N` to start work the labels had already
authorized. The loop below does that instead.

Design rule, and the reason this file is small: **the runtime holds no judgment.**
It does not decide what is authorized, eligible, in-scope, or within WIP; it does
not recover leases, and it never touches a story's lifecycle. Every one of those
decisions belongs to `factory/dispatcher/dispatcher.py`, which this invokes as a
subprocess and whose stdout it reads. Everything protecting the repository from a
bad dispatch — the authorization chain, the trust boundary, WIP and attempt
limits, the required merge gate — sits upstream of here. If this file ever grows
a policy decision, that is the bug.

What it does, per poll:

    1. run the dispatcher with claiming enabled
    2. read only canonical `DISPATCH story=#N project=#P agent=<id>` lines
    3. launch the configured worker once per line

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
    """Resolve the worker adapter for a dispatch.

    `FACTORY_WORKER_CMD` is the whole extension point: set it to a Codex or any
    other launcher and dispatcher semantics do not change. Placeholders are
    substituted by name so an adapter takes only what it needs.

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


def wake_worker(dispatch: dict) -> str:
    cmd = worker_command(dispatch)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkerLaunchFailed(f"{cmd[0]}: {exc}") from exc
    if result.returncode != 0:
        raise WorkerLaunchFailed(
            f"{cmd[0]} exited {result.returncode}: {(result.stderr or '').strip()[:200]}")
    return (result.stdout or "").strip()


def poll_once(repo: str, commitment: int, seen: set[int], claim: bool = True) -> list[dict]:
    """One cycle. Returns the dispatches that produced a wake-up."""
    stdout = run_dispatcher(repo, commitment, claim)
    woken = []
    for dispatch in parse_dispatches(stdout):
        if dispatch["story"] in seen:
            # Belt and braces only: GitHub already prevents this by not
            # re-offering a claimed story.
            print(f"[poller] already woken this run, skipping story "
                  f"#{dispatch['story']}", flush=True)
            continue
        output = wake_worker(dispatch)
        seen.add(dispatch["story"])
        print(f"[poller] woke {dispatch['agent']} for story #{dispatch['story']} "
              f"(project #{dispatch['project']})", flush=True)
        if output:
            print(output, flush=True)
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
