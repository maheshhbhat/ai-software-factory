#!/usr/bin/env python3
"""Deterministic lifecycle sequencing for dependency-ready factory work.

The sequencer owns the two mechanical Phase 3 transitions named in §4:
`story:blocked -> story:ready` and `project:active -> project:awaiting-acceptance`.
It reads GitHub on every pass, derives decisions without a cursor, then re-reads
each subject immediately before applying its one lifecycle-label replacement.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dispatcher"))
sys.path.insert(0, os.path.join(HERE, "..", "gates"))
import dispatcher  # noqa: E402
import merge_gate  # noqa: E402

BLOCKED = "story:blocked"
READY = "story:ready"
ACTIVE = "project:active"
AWAITING_ACCEPTANCE = "project:awaiting-acceptance"
IN_FLIGHT = frozenset({dispatcher.READY, dispatcher.CLAIMED, dispatcher.IN_REVIEW})


@dataclass(frozen=True)
class Decision:
    number: int
    current: str
    target: str
    reason: str


def _references(body: str, section: str) -> tuple[list[int], str | None]:
    raw = merge_gate.parse_section(body or "", section)
    if raw is None:
        return [], f"{section} missing"
    lines = [line.strip() for line in raw.strip().splitlines() if line.strip()]
    if section == "Depends-on" and lines == ["none"]:
        return [], None
    refs = []
    for line in lines:
        match = re.fullmatch(r"#([1-9][0-9]*)", line)
        if not match:
            return [], f"{section} malformed: {line!r}"
        refs.append(int(match.group(1)))
    return refs, None


def _has_cycle(start: int, dependencies: dict[int, list[int]]) -> bool:
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(number: int) -> bool:
        if number in visiting:
            return True
        if number in visited:
            return False
        visiting.add(number)
        if any(visit(dep) for dep in dependencies.get(number, [])):
            return True
        visiting.remove(number)
        visited.add(number)
        return False

    return visit(start)


def plan_story_readiness(issues: dict[int, dict], wip_limit: int) -> list[Decision]:
    """Choose dependency-satisfied blocked stories in stable issue order."""
    stories = {n: issue for n, issue in issues.items()
               if "type:story" in dispatcher.labels_of(issue)}
    dependencies: dict[int, list[int]] = {}
    malformed: set[int] = set()
    for number, story in stories.items():
        refs, error = _references(story.get("body") or "", "Depends-on")
        dependencies[number] = refs
        if error:
            malformed.add(number)

    reserved = sum(
        dispatcher.lifecycle_of(story, dispatcher.STORY_LIFECYCLE) in IN_FLIGHT
        and (story.get("state") or "OPEN").upper() == "OPEN"
        for story in stories.values()
    )
    capacity = max(0, wip_limit - reserved)
    decisions = []
    for number in sorted(stories):
        story = stories[number]
        if capacity == 0:
            break
        if dispatcher.lifecycle_of(story, dispatcher.STORY_LIFECYCLE) != BLOCKED:
            continue
        if not dispatcher.is_trusted(story):
            continue
        if number in malformed or _has_cycle(number, dependencies):
            continue
        refs = dependencies[number]
        if any(ref not in issues for ref in refs):
            continue
        if not all(dispatcher.dependency_satisfied(issues[ref])[0] for ref in refs):
            continue
        project_number, error = dispatcher.section_ref(story.get("body") or "", "Project")
        project = issues.get(project_number) if not error else None
        if project is None or dispatcher.lifecycle_of(
                project, dispatcher.PROJECT_LIFECYCLE) != ACTIVE:
            continue
        if not dispatcher.is_trusted(project):
            continue
        decisions.append(Decision(number, BLOCKED, READY, "dependencies satisfied"))
        capacity -= 1
    return decisions


def plan_project_completion(issues: dict[int, dict]) -> list[Decision]:
    """Advance only active projects whose declared children all succeeded."""
    decisions = []
    for number in sorted(issues):
        project = issues[number]
        if "type:project" not in dispatcher.labels_of(project):
            continue
        if dispatcher.lifecycle_of(project, dispatcher.PROJECT_LIFECYCLE) != ACTIVE:
            continue
        if not dispatcher.is_trusted(project):
            continue
        stories, error = _references(project.get("body") or "", "Stories")
        if error or not stories or any(story not in issues for story in stories):
            continue
        if all(dispatcher.is_trusted(issues[story]) and
               dispatcher.lifecycle_of(issues[story], dispatcher.STORY_LIFECYCLE)
               in dispatcher.TERMINAL_SUCCESS for story in stories):
            decisions.append(Decision(number, ACTIVE, AWAITING_ACCEPTANCE,
                                      "all declared stories reached terminal success"))
    return decisions


def plan(issues: dict[int, dict], wip_limit: int = dispatcher.WIP_LIMIT) -> list[Decision]:
    return plan_project_completion(issues) + plan_story_readiness(issues, wip_limit)


def fetch_all_issues(repo: str, token: str) -> dict[int, dict]:
    issues = {}
    for state in ("open", "closed"):
        page = 1
        while True:
            batch = dispatcher._api(  # one canonical authenticated API boundary
                f"https://api.github.com/repos/{repo}/issues"
                f"?state={state}&per_page=100&page={page}", token)
            if not batch:
                break
            for issue in batch:
                if "pull_request" not in issue:
                    issues[issue["number"]] = issue
            if len(batch) < 100:
                break
            page += 1
    return issues


def apply_decision(repo: str, decision: Decision, token: str) -> tuple[bool, str]:
    fresh = dispatcher.fetch_issue(repo, decision.number, token)
    if fresh is None:
        return False, "subject disappeared"
    prefix = (dispatcher.STORY_LIFECYCLE if decision.current.startswith("story:")
              else dispatcher.PROJECT_LIFECYCLE)
    if dispatcher.lifecycle_of(fresh, prefix) != decision.current:
        return False, "state changed before write"
    labels = dispatcher.labels_of(fresh) - {decision.current}
    labels.add(decision.target)
    dispatcher._api(
        f"https://api.github.com/repos/{repo}/issues/{decision.number}", token,
        method="PATCH", payload={"labels": sorted(labels)})
    return True, f"{decision.current} -> {decision.target}"


def run(repo: str, token: str, apply: bool = True,
        wip_limit: int = dispatcher.WIP_LIMIT) -> list[Decision]:
    decisions = plan(fetch_all_issues(repo, token), wip_limit)
    applied = []
    for decision in decisions:
        if not apply:
            print(f"[sequencer] would advance #{decision.number}: "
                  f"{decision.current} -> {decision.target}", flush=True)
            applied.append(decision)
            continue
        ok, note = apply_decision(repo, decision, token)
        print(f"[sequencer] #{decision.number}: "
              f"{note if ok else 'skipped — ' + note}", flush=True)
        if ok:
            applied.append(decision)
    return applied
