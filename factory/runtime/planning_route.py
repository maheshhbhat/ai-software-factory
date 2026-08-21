"""Claim `project:ready-for-planning` artifacts for one headless invocation."""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dispatcher"))
import dispatcher  # noqa: E402

READY = "project:ready-for-planning"
PLANNING = "project:planning"


def select(issues: dict[int, dict]) -> list[int]:
    return [number for number in sorted(issues)
            if "type:project" in dispatcher.labels_of(issues[number])
            and dispatcher.is_trusted(issues[number])
            and dispatcher.lifecycle_of(issues[number], dispatcher.PROJECT_LIFECYCLE)
            in (READY, PLANNING)]


def run(repo: str, token: str, apply: bool = True) -> list[int]:
    issues = dispatcher.fetch_issues(repo, token)
    claimed = []
    for number in select(issues):
        if not apply:
            claimed.append(number)
            continue
        fresh = dispatcher.fetch_issue(repo, number, token)
        if fresh is None:
            continue
        current = dispatcher.lifecycle_of(fresh, dispatcher.PROJECT_LIFECYCLE)
        if current == PLANNING:
            claimed.append(number)
            print(f"[planning-route] #{number}: retry existing {PLANNING}", flush=True)
            continue
        if current != READY:
            continue
        labels = dispatcher.labels_of(fresh) - {READY}
        labels.add(PLANNING)
        dispatcher._api(f"https://api.github.com/repos/{repo}/issues/{number}", token,
                        method="PATCH", payload={"labels": sorted(labels)})
        claimed.append(number)
        print(f"[planning-route] #{number}: {READY} -> {PLANNING}", flush=True)
    return claimed
