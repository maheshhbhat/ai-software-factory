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
import os
import subprocess
import sys

# How long the engine may take to accept and complete the bounded task. Exceeding
# it is reported as a timeout, which `workers.py` treats as AMBIGUOUS — never as
# a definite failure, because the engine may still be running.
ENGINE_TIMEOUT_SECONDS = int(os.environ.get("FACTORY_BRIDGE_TIMEOUT", "300"))

DEFAULT_REPO = "maheshhbhat/ai-software-factory"


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
        f"The comment must start with the heading '## Worker acknowledgement' and state "
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
        return ["claude", "-p", prompt,
                "--permission-mode", "acceptEdits"]
    raise ValueError(f"unknown engine {engine!r}; declare it in the bridge or use a "
                     f"direct launch command")


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

    print(f"[bridge] {args.engine} accepted story #{args.story}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
