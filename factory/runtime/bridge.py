#!/usr/bin/env python3
"""Worker launch bridge — the factory's own handoff to a real CLI engine.

Phase 2, story #90. Until now the runtime emitted a `WAKE worker=… story=#N`
line and a human-configured standing CLI session picked it up. That session was
not a factory-owned launcher, so worker swappability was proven in tests and not
in the world. This bridge closes that gap: the runtime invokes the engine
itself.

It is the *implementation* behind a worker declaration, not a new orchestrator.
Capacity Pool chooses and invokes eligible providers. The bridge supplies only
the bounded acknowledgement task and verifies its durable side effect.

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
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = pathlib.Path(HERE).parents[1]
sys.path.insert(0, str(ROOT))
import runlog   # noqa: E402 — operational record (#104)
import workers  # noqa: E402 — the launch budget this process is killed by
from factory.capacity_pool.executor import CapacityExecutor  # noqa: E402
from factory.capacity_pool.policy import POLICIES, resolved_registry  # noqa: E402
from factory.capacity_pool.providers import (  # noqa: E402
    AttemptResult, InvocationPayload, ProviderAdapter, cli_adapter,
    provider_environment,
)
from factory.capacity_pool.state import CapacityState  # noqa: E402

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


def bridge_payload(prompt: str) -> InvocationPayload:
    return InvocationPayload(
        prompt, access="workspace-write", network_access=True,
        skip_git_repo_check=True,
        allowed_tools=("Bash(gh issue comment:*)", "Bash(gh issue view:*)",
                       "Bash(date:*)"),
        disallowed_tools=("Write", "Edit", "Agent"))


def bounded_bridge_adapter(provider: str, *, since: str | None, repo: str,
                           story: int, environment: dict,
                           runner=subprocess.run) -> ProviderAdapter:
    """Convert durable acknowledgement evidence into the side-effect verdict."""
    base = cli_adapter(
        provider, cwd=ROOT, environment=environment, runner=runner,
        mutation_state=lambda: "none")

    def invoke(**kwargs):
        started = time.monotonic()
        result = base.run(**kwargs)
        acknowledgement = acknowledgement_verdict(repo, story, since)
        runlog.event(
            "bridge.engine.exit", engine=kwargs["model"], provider=provider,
            story=story, outcome=result.outcome,
            elapsed_ms=runlog.elapsed_ms(started),
            stdout=runlog.tail(result.output), stderr=runlog.tail(result.diagnostic))
        runlog.event(
            "bridge.acknowledgement", engine=kwargs["model"], provider=provider,
            story=story, since=since,
            verdict={True: "PRESENT", False: "ABSENT",
                     None: "UNVERIFIABLE"}[acknowledgement])
        if acknowledgement is True:
            return AttemptResult(
                "success", result.output, result.consumed_budget_units,
                mutation_state="post-mutation",
                diagnostic="acknowledgement verified")
        if acknowledgement is None:
            return AttemptResult(
                "ambiguous-mutation", result.output,
                result.consumed_budget_units, mutation_state="ambiguous",
                diagnostic="acknowledgement could not be verified")
        if result.succeeded:
            return AttemptResult(
                "schema-invalid", result.output, result.consumed_budget_units,
                diagnostic="no acknowledgement was posted")
        return result

    return ProviderAdapter(provider, invoke)


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
        report(f"[bridge] WARN: GitHub read failed: {exc}")
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


def report(message: str) -> None:
    """Print a diagnosis where it will still be readable after a failure.

    `workers.launch` keeps a launched worker's **stdout** and a failed one's
    **stderr**, so a failure explained on stdout is a failure explained nowhere:
    the trail reads `FAILED exit 1:` with the reason discarded. The verdict this
    module exists to produce is exactly the one that would vanish, and an audit
    trail believed to be complete while silently dropping its most important
    line is worse than no trail at all (#93).
    """
    print(message, file=sys.stderr, flush=True)


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
        report(f"[bridge] WARN: unexpected comments payload for #{story}")
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


def bridge_environment(provider: str) -> dict[str, str]:
    env = provider_environment(provider)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        env["GH_TOKEN"] = token
    return env


def main(argv: list[str], *, state: CapacityState | None = None, registry=None,
         runner=None) -> int:
    parser = argparse.ArgumentParser(description="Factory worker launch bridge (#90)")
    parser.add_argument("--story", required=True, type=int)
    parser.add_argument("--project", required=True, type=int)
    parser.add_argument("--repo", default=os.environ.get("FACTORY_REPO", DEFAULT_REPO))
    parser.add_argument("--dry-run", action="store_true",
                        help="print the invocation without running it")
    args = parser.parse_args(argv)

    prompt = task_prompt(args.repo, args.story, args.project)
    print(f"[bridge] capacity-pool story=#{args.story} project=#{args.project}",
          flush=True)
    runlog.event("bridge.dispatch", engine="capacity-pool", story=args.story,
                 project=args.project, repo=args.repo,
                 timeout_s=ENGINE_TIMEOUT_SECONDS)

    if args.dry_run:
        return 0

    runner = runner or workers.run_observed

    since = server_now(args.repo, args.story)
    started = time.monotonic()
    owns_state = state is None
    if state is None:
        configured = os.environ.get("FACTORY_CAPACITY_STATE", "").strip()
        state_path = (pathlib.Path(configured) if configured else
                      ROOT / "runs" / "capacity-pool.sqlite")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = CapacityState(state_path, uri=False)
    try:
        available = tuple(registry or resolved_registry(health=state.health))
        adapters = {provider: bounded_bridge_adapter(
            provider, since=since, repo=args.repo, story=args.story,
            environment=bridge_environment(provider), runner=runner)
            for provider in {item.provider for item in available}}
        capacity = CapacityExecutor(
            adapters, state,
            telemetry=lambda **values: runlog.event(
                "bridge.capacity", story=args.story, project=args.project,
                **values))
        result = capacity.execute(
            task_key=f"bridge:{args.repo}:{args.story}:{since}",
            request=POLICIES["bridge"].request(
                total_timeout_seconds=ENGINE_TIMEOUT_SECONDS),
            registry=available, payload=bridge_payload(prompt))
    finally:
        if owns_state:
            state.close()
    elapsed = runlog.elapsed_ms(started)
    attempt = result.attempts[-1] if result.attempts else {}
    engine = attempt.get("model", "none")
    verdict = ("LAUNCHED" if result.outcome == "success" else
               "AMBIGUOUS" if result.outcome == "ambiguous-mutation" else
               "FAILED")
    exit_code = 0 if verdict == "LAUNCHED" else 2 if verdict == "AMBIGUOUS" else 1
    runlog.event("bridge.outcome", engine=engine, story=args.story,
                 project=args.project, verdict=verdict, exit=exit_code,
                 elapsed_ms=elapsed, reason=result.outcome)
    if verdict == "LAUNCHED":
        print(f"[bridge] {engine} accepted story #{args.story}", flush=True)
    elif verdict == "AMBIGUOUS":
        report("[bridge] acknowledgement is unverifiable; do not launch another worker")
    else:
        report(f"[bridge] FAIL: capacity outcome {result.outcome}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
