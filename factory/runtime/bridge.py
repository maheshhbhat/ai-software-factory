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
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# How long the engine may take to accept and complete the bounded task. Exceeding
# it is reported as a timeout, which `workers.py` treats as AMBIGUOUS — never as
# a definite failure, because the engine may still be running.
ENGINE_TIMEOUT_SECONDS = int(os.environ.get("FACTORY_BRIDGE_TIMEOUT", "300"))

DEFAULT_REPO = "maheshhbhat/ai-software-factory"

# The one durable artifact the bounded task must produce. It is both what the
# prompt asks for and what this module checks for afterwards, so the two can
# never drift apart into an engine that satisfies the prompt while failing the
# check.
ACK_HEADING = "## Worker acknowledgement"

# How hard to look for that artifact before concluding the engine did nothing.
# GitHub is read-your-writes in practice but not by contract, and calling a
# worker's success a failure would hand the same Story to a second engine — so
# the check is patient before it is decisive.
ACK_CHECK_ATTEMPTS = 3
ACK_CHECK_DELAY_SECONDS = 2

# The commands the Claude engine may run, and nothing else. The prompt names one
# action; the permission surface is that action plus the read it is allowed to
# do for context. `--permission-mode acceptEdits` was the original grant and was
# strictly wrong in both directions: it authorised file edits the prompt
# forbids, while leaving Bash — where the whole assignment lives — behind an
# approval prompt no unattended run can ever answer (#96).
CLAUDE_ALLOWED_TOOLS = "Bash(gh issue comment:*),Bash(gh issue view:*)"


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


def acknowledgement_ids(repo: str, story: int) -> set | None:
    """Ids of the acknowledgement comments currently on the Story.

    `None` means the question could not be answered — no token, no network, an
    API error. That is deliberately distinct from "none found": one is ignorance
    and the other is evidence, and this module must never spend the first as if
    it were the second.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "ai-software-factory-bridge"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = (f"https://api.github.com/repos/{repo}/issues/{story}"
           f"/comments?per_page=100")
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=20) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[bridge] WARN: cannot read comments on #{story}: {exc}", flush=True)
        return None
    if not isinstance(payload, list):
        print(f"[bridge] WARN: unexpected comments payload for #{story}", flush=True)
        return None
    return {comment.get("id") for comment in payload
            if (comment.get("body") or "").lstrip().startswith(ACK_HEADING)}


def worker_acted(repo: str, story: int, before: set | None) -> bool:
    """Did *this* invocation produce a new acknowledgement?

    The check compares against a snapshot taken before launch, so an
    acknowledgement left by an earlier run is not counted as proof that this
    engine did anything.

    When the answer is unknowable the verdict is `True` — the pre-#96 behaviour,
    stated as a limitation rather than hidden: an unverifiable launch is
    reported as it always was. The runtime refuses to poll without a token, so
    the blind path is not the normal one.
    """
    if before is None:
        print("[bridge] WARN: acknowledgement unverifiable; reporting the engine's "
              "own exit status", flush=True)
        return True
    for attempt in range(ACK_CHECK_ATTEMPTS):
        current = acknowledgement_ids(repo, story)
        if current is None:
            print("[bridge] WARN: acknowledgement unverifiable; reporting the engine's "
                  "own exit status", flush=True)
            return True
        if current - before:
            return True
        if attempt + 1 < ACK_CHECK_ATTEMPTS:
            time.sleep(ACK_CHECK_DELAY_SECONDS)
    return False


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
    print(f"[bridge] exec: {' '.join(printable)}", flush=True)

    if args.dry_run:
        return 0

    # Snapshot first: the question this bridge answers afterwards is whether a
    # *new* acknowledgement appeared, not whether one exists.
    before = acknowledgement_ids(args.repo, args.story)

    try:
        completed = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=ENGINE_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        # Definite: the engine is not installed, so it certainly did not run.
        print(f"[bridge] FAIL: engine not available: {exc}", flush=True)
        return 1
    except subprocess.TimeoutExpired:
        # Ambiguous by nature. Exit non-zero so the runtime notices, but say
        # plainly that the engine may still be working — `workers.py` maps its
        # own timeout to AMBIGUOUS and refuses to fall back.
        print(f"[bridge] TIMEOUT after {ENGINE_TIMEOUT_SECONDS}s: the engine may still "
              f"be running. Do not launch another worker for this story.", flush=True)
        return 2

    tail = (completed.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        print(f"[bridge] {line}", flush=True)
    if completed.returncode != 0:
        print(f"[bridge] FAIL: engine exited {completed.returncode}: "
              f"{(completed.stderr or '').strip()[-300:]}", flush=True)
        return 1

    # An exit code proves the process ended, never that the assignment was
    # carried out. #96 caught the Claude engine ending cleanly having posted
    # nothing, which the runtime then recorded as LAUNCHED — a definite success,
    # so failover was correctly suppressed and the no-op became terminal. The
    # check below is what makes "did nothing" a *definite failure* instead, which
    # is the one verdict that lets another engine try.
    if not worker_acted(args.repo, args.story, before):
        print(f"[bridge] FAIL: {args.engine} exited 0 without posting an "
              f"acknowledgement on #{args.story}; it did not do the work. "
              f"Treat as a definite failure — another engine may try.", flush=True)
        return 1

    print(f"[bridge] {args.engine} accepted story #{args.story}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
