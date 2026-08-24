#!/usr/bin/env python3
"""Operator repair for a claim stranded by a confirmed worker malfunction."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "dispatcher"))
import dispatcher  # noqa: E402


def confirmed_failure(path: pathlib.Path, repo: str, story: int) -> dict:
    matches = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw)
        if (event.get("event") == "worker.launch.end"
                and event.get("repo") == repo
                and event.get("story") == story):
            matches.append(event)
    if not matches:
        raise ValueError("evidence contains no worker.launch.end for this Story")
    event = matches[-1]
    if event.get("result") != "FAILED" or event.get("exit") in (None, 0):
        raise ValueError("latest worker outcome is not a confirmed non-zero failure")
    return event


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--story", required=True, type=int)
    parser.add_argument("--evidence", required=True, type=pathlib.Path,
                        help="process-events.jsonl from the failed run")
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        print("repair refused: no GitHub credential", file=sys.stderr)
        return 2
    try:
        event = confirmed_failure(args.evidence, args.repo, args.story)
        ok, detail = dispatcher.release_definite_failure(
            args.repo, args.story, token, reason=args.reason,
            evidence=str(args.evidence), operator=True)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"repair refused: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print(f"repair refused: {detail}", file=sys.stderr)
        return 1
    print(f"repair applied: Story #{args.story}; worker exit {event['exit']}; {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
