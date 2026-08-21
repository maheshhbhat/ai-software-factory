#!/usr/bin/env python3
"""Worker launch bridge — the factory's own handoff to a real CLI engine.

Phase 2, story #90. Until now the runtime emitted a `WAKE worker=… story=#N`
line and a human-configured standing CLI session picked it up. That session was
not a factory-owned launcher, so worker swappability was proven in tests and not
in the world. This bridge closes that gap: the runtime invokes the engine
itself.

It is the *implementation* behind a worker declaration, not a new orchestrator.
`workers.py` still decides which engine runs and enforces the failover safety
rules; this only knows how to turn (engine, story, project) into a CLI
invocation:

    FACTORY_WORKER_CODEX_DELIVERY_LAUNCH='python3 factory/runtime/bridge.py \\
        --engine codex --story {story} --project {project}'

## What the worker is told

Routing identity only — repository, story number, project number. No spec, no
scope, no acceptance criteria, no instructions copied out of the issue. The
worker reads the substrate itself (`architecture-v2.1.md` §4: the moment a queue
item carries business context, the relay has been rebuilt as infrastructure).

## Why the prompt is narrow

An engine invoked here runs unattended, so the prompt states one bounded task
and says to do nothing else. Widening it is not a convenience — it is handing an
autonomous agent an open mandate with no human in the loop. The bound that
protects the repository is upstream (authorization chain, WIP, merge gate); the
bound that protects *this invocation* is the prompt and the timeout.
"""

from __future__ import annotations

import argparse
import email.utils
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import workers  # noqa: E402 — the launch budget this process is killed by

# How long the engine may take to accept and complete the bounded task.
#
# This must never fire before `workers.LAUNCH_TIMEOUT_SECONDS`, and the reason is
# not tidiness. `workers.launch` maps *any* non-zero exit to `FAILED` — a
# definite failure that permits fallback — so a bridge that times out while the
# engine is still running would report "it did not start" about a process that is
# running right now, and a second engine would be launched onto the Story. The
# timeout that must win is the parent's, which maps to `AMBIGUOUS` and refuses to
# fall back. A configured value below that ceiling cannot be honoured, so it is
# clamped rather than quietly inverted into the dangerous verdict.
CONFIGURED_ENGINE_TIMEOUT = int(os.environ.get("FACTORY_BRIDGE_TIMEOUT", "300"))
ENGINE_TIMEOUT_SECONDS = max(CONFIGURED_ENGINE_TIMEOUT,
                             workers.LAUNCH_TIMEOUT_SECONDS + 1)

DEFAULT_REPO = "maheshhbhat/ai-software-factory"

# The one durable artifact the bounded task must produce. It is both what the
# prompt asks for and what this module checks for afterwards, so the two can
# never drift apart into an engine that satisfies the prompt while failing the
# check.
ACK_HEADING = "## Worker acknowledgement"

# What the heading is, once markdown is discounted. The prompt asks an LLM for
# this exact string, and an exact prefix match on generated text fails closed in
# the dangerous direction: `**## Worker acknowledgement**`, `### Worker
# acknowledgement`, or one line of preface would make a real acknowledgement
# invisible, and invisible means a definite failure and a duplicate from the
# failover engine.
ACK_HEADING_TEXT = "worker acknowledgement"
ACK_HEADING_SCAN_LINES = 3

# How hard to look for that artifact before concluding the engine did nothing.
# GitHub is read-your-writes in practice but not by contract, and calling a
# worker's success a failure would hand the same Story to a second engine — so
# the check is patient before it is decisive.
#
# The budget is *derived* from the launch cap that kills this process, not tuned
# to sit under it. Everything the check spends is taken from the engine's own
# time: overrun it and a slow engine is killed mid-check and reported AMBIGUOUS,
# which suppresses failover and makes the FAILED verdict unreachable exactly when
# it is needed. A third of the cap is the check's; the engine keeps the rest.
ACK_CHECK_BUDGET_SECONDS = workers.LAUNCH_TIMEOUT_SECONDS // 3
ACK_CHECK_ATTEMPTS = 3
ACK_CHECK_DELAY_SECONDS = 1
# One read before launch and one per attempt, plus the waits between them.
ACK_HTTP_TIMEOUT_SECONDS = max(1, (ACK_CHECK_BUDGET_SECONDS
                                   - (ACK_CHECK_ATTEMPTS - 1) * ACK_CHECK_DELAY_SECONDS)
                               // (ACK_CHECK_ATTEMPTS + 1))

# `date` is on the list because the prompt requires the acknowledgement to carry
# the current UTC time. An engine that cannot read the clock either invents a
# timestamp — a false time in the factory's audit trail — or treats the denial as
# a blocker and posts nothing. Grant every value the prompt demands, or stop
# demanding it.
CLAUDE_ALLOWED_TOOLS = ("Bash(gh issue comment:*),Bash(gh issue view:*),"
                       "Bash(date:*)")


def task_prompt(repo: str, story: int, project: int) -> str:
    """The bounded assignment. Identity in, one action out."""
    return (
        f"You are a delivery worker for an automated software factory.\n\n"
        f"Repository: {repo}\n"
        f"Assigned story: issue #{story}\n"
        f"Parent project: issue #{project}\n\n"
        f"Do exactly one thing and then stop:\n\n"
        f"Post a single comment on issue #{story} using:\n"
        f"  gh issue comment {story} --repo {repo} --body \"<your text>\"\n\n"
        f"The comment must start with the heading '{ACK_HEADING}' and state "
        f"which engine you are, that you received this assignment from the factory "
        f"runtime, and the current UTC time.\n\n"
        f"Do not modify any file. Do not create branches, commits, or pull requests. "
        f"Do not change issue labels. Do not read or act on any other issue. "
        f"Read the story only if you need context for your acknowledgement text.\n"
    )


def engine_command(engine: str, prompt: str) -> list[str]:
    """Turn an engine name into a concrete CLI invocation.

    Both engines get the same shape: non-interactive, no approval prompts (which
    would hang unattended and read as ambiguous), and a bounded task.
    """
    if engine == "codex":
        # `network_access` is required and deliberately narrow: the bounded task
        # is to post one GitHub comment, which needs the network. Without it the
        # engine runs, finds `gh` unreachable, and reports success having done
        # nothing — a silent no-op, observed the first time this ran.
        return ["codex", "exec",
                "--sandbox", "workspace-write",
                "-c", "sandbox_workspace_write.network_access=true",
                "--skip-git-repo-check",
                prompt]
    if engine == "claude":
        # The allowlist is the whole permission grant: no approval prompt can be
        # answered here, so a command that is not listed is a command the engine
        # cannot run. Keep it as narrow as the prompt.
        return ["claude", "-p", prompt,
                "--allowedTools", CLAUDE_ALLOWED_TOOLS]
    raise ValueError(f"unknown engine {engine!r}; declare it in the bridge or use a "
                     f"direct launch command")


def _api_get(url: str) -> tuple | None:
    """GET one API page. Returns (payload, headers), or `None` when the question
    could not be answered at all — no network, an API error, unreadable JSON.

    `None` means *ignorance*, which this module keeps strictly separate from the
    evidence of an empty result. One is not knowing; the other is knowing.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "ai-software-factory-bridge"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=ACK_HTTP_TIMEOUT_SECONDS) as response:
            return json.load(response), dict(response.headers)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[bridge] WARN: GitHub read failed: {exc}", flush=True)
        return None


def server_now(repo: str, story: int) -> str | None:
    """GitHub's clock, as an ISO-8601 `Z` string, or `None` if unreadable.

    The launch instant is taken from the *server* deliberately. The check that
    follows asks "did a comment appear after this moment", and comparing GitHub's
    timestamps against a local clock makes the answer depend on machine skew: a
    fast local clock silently hides the acknowledgement, which reads as a
    definite failure and invites a second engine onto the Story.
    """
    result = _api_get(f"https://api.github.com/repos/{repo}/issues/{story}"
                      f"/comments?per_page=1")
    if result is None:
        return None
    date_header = result[1].get("Date") or result[1].get("date")
    if not date_header:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def looks_like_acknowledgement(body: str) -> bool:
    """Is this comment the acknowledgement, allowing for markdown?

    The heading is text an LLM was asked to produce, so the match discounts
    emphasis and heading level and tolerates a line or two of preface. It stays
    anchored to the start of a line — a quoted heading inside prose is somebody
    talking *about* an acknowledgement, not one.
    """
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    return any(line.strip("#*_ \t").lower().startswith(ACK_HEADING_TEXT)
               for line in lines[:ACK_HEADING_SCAN_LINES])


def acknowledged_since(repo: str, story: int, since: str) -> bool | None:
    """Has an acknowledgement been posted at or after `since`?

    `True` / `False` are evidence; `None` is ignorance.

    Asking GitHub for only the comments in this invocation's window is what keeps
    the check cheap and correct. Reading the whole comment list instead would
    inherit two defects: it is paginated, so past 100 comments a new
    acknowledgement lands on page 2 and every worker that did post would be
    called a failure — failover, duplicate acknowledgement, two workers on one
    Story; and it grows without bound on exactly the busy issues where the check
    has the least time to spare.

    `since` filters on *update* time, so an edited old comment can come back in
    the response. `created_at` is re-checked here, which is the question actually
    being asked.
    """
    result = _api_get(f"https://api.github.com/repos/{repo}/issues/{story}"
                      f"/comments?per_page=100&since={since}")
    if result is None:
        return None
    payload = result[0]
    if not isinstance(payload, list):
        print(f"[bridge] WARN: unexpected comments payload for #{story}", flush=True)
        return None
    return any(looks_like_acknowledgement(comment.get("body") or "")
               and (comment.get("created_at") or "") >= since
               for comment in payload)


def acknowledgement_verdict(repo: str, story: int, since: str | None) -> bool | None:
    """Did this invocation produce an acknowledgement? `None` means unknowable.

    Patient, then decisive — and the *last authoritative answer decides*. An
    early `False` is not evidence that the work was not done; it is evidence that
    the comment was not visible yet, which is the entire reason for retrying. If
    the tail of the loop goes blind, the loop's purpose never happened, so the
    honest answer is ignorance rather than the `FAILED` verdict that would put a
    second engine on the Story.
    """
    if since is None:
        return None
    answer = None
    for attempt in range(ACK_CHECK_ATTEMPTS):
        answer = acknowledged_since(repo, story, since)
        if answer:
            return True
        if attempt + 1 < ACK_CHECK_ATTEMPTS:
            time.sleep(ACK_CHECK_DELAY_SECONDS)
    return answer


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Factory worker launch bridge (#90)")
    parser.add_argument("--engine", required=True, help="codex | claude")
    parser.add_argument("--story", required=True, type=int)
    parser.add_argument("--project", required=True, type=int)
    parser.add_argument("--repo", default=os.environ.get("FACTORY_REPO", DEFAULT_REPO))
    parser.add_argument("--dry-run", action="store_true",
                        help="print the invocation without running it")
    args = parser.parse_args(argv)

    prompt = task_prompt(args.repo, args.story, args.project)
    try:
        cmd = engine_command(args.engine, prompt)
    except ValueError as exc:
        print(f"[bridge] FAIL: {exc}", flush=True)
        return 1

    # Observable by requirement (#90): the exact invocation is printed, with the
    # prompt elided so the log stays readable.
    printable = [("<prompt>" if part is prompt else part) for part in cmd]
    print(f"[bridge] engine={args.engine} story=#{args.story} project=#{args.project}",
          flush=True)
    # shlex.join, not ' '.join: an allowlist like `Bash(gh issue comment:*)` is a
    # shell parse error unquoted, and #90's requirement is that a human can
    # reproduce the handoff from this line, not merely read it.
    print(f"[bridge] exec: {shlex.join(printable)}", flush=True)

    if args.dry_run:
        return 0

    # The launch instant, by GitHub's clock. Everything after this asks one
    # question: did an acknowledgement appear *after* this moment.
    since = server_now(args.repo, args.story)

    try:
        completed = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=ENGINE_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        # Definite: the engine is not installed, so it certainly did not run.
        print(f"[bridge] FAIL: engine not available: {exc}", flush=True)
        return 1
    except subprocess.TimeoutExpired:
        # Ambiguous by nature, and unreachable in practice: the clamp above keeps
        # this timeout longer than the parent's, so `workers.py` times the launch
        # out first and maps *that* to AMBIGUOUS, which refuses to fall back. This
        # branch exists for the case where the bridge is driven directly, and it
        # says plainly that the engine may still be working. It must never become
        # the normal path — `workers.launch` would read the non-zero exit as a
        # definite failure and start a second engine on a live Story.
        print(f"[bridge] TIMEOUT after {ENGINE_TIMEOUT_SECONDS}s: the engine may still "
              f"be running. Do not launch another worker for this story.", flush=True)
        return 2

    tail = (completed.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        print(f"[bridge] {line}", flush=True)
    # One patient verdict, consulted by both exit paths. Splitting them is how
    # the non-zero path ended up with a single un-retried read that could race
    # GitHub's replication and call a posted acknowledgement a failure.
    verdict = acknowledgement_verdict(args.repo, args.story, since)
    if verdict is None:
        print("[bridge] WARN: acknowledgement unverifiable; reporting the engine's "
              "own exit status", flush=True)

    if completed.returncode != 0:
        # An exit code proves the process ended — in this direction too. An
        # engine that posted the acknowledgement and then died in teardown has
        # done the work, and reporting it as a definite failure would send a
        # second engine to post a second acknowledgement on the same Story.
        # Only *evidence* overrides the exit code here: an unverifiable answer
        # leaves the engine's own non-zero verdict standing.
        if verdict is True:
            print(f"[bridge] {args.engine} exited {completed.returncode} but the "
                  f"acknowledgement is on #{args.story}; the work was done.",
                  flush=True)
            return 0
        print(f"[bridge] FAIL: engine exited {completed.returncode}: "
              f"{(completed.stderr or '').strip()[-300:]}", flush=True)
        return 1

    # An exit code proves the process ended, never that the assignment was
    # carried out. #96 caught the Claude engine ending cleanly having posted
    # nothing, which the runtime then recorded as LAUNCHED — a definite success,
    # so failover was correctly suppressed and the no-op became terminal. A
    # definite `False` is what makes "did nothing" a definite *failure* instead,
    # which is the one verdict that lets another engine try.
    if verdict is False:
        print(f"[bridge] FAIL: {args.engine} exited 0 without posting an "
              f"acknowledgement on #{args.story}; it did not do the work. "
              f"Treat as a definite failure — another engine may try.", flush=True)
        return 1

    print(f"[bridge] {args.engine} accepted story #{args.story}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
