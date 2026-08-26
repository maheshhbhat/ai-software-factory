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
import shlex
import subprocess
import sys
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dispatcher"))
sys.path.insert(0, os.path.join(HERE, "..", "gates"))
import dispatcher  # noqa: E402
import merge_gate  # noqa: E402
import operating_envelope  # noqa: E402
import production_readiness  # noqa: E402

BLOCKED = "story:blocked"
READY = "story:ready"
ACTIVE = "project:active"
AWAITING_ACCEPTANCE = "project:awaiting-acceptance"
# §4.1.1 — continuous work. It has no end, so it has no acceptance edge, and the
# sequencer must exclude it by recognising this value rather than by falling
# through some other guard.
STANDING = "project:standing"
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


def _matches_commitment(project: dict, commitment: int | None) -> bool:
    if commitment is None:
        return True
    reference, error = dispatcher.section_ref(
        project.get("body") or "", "Roadmap commitment")
    return error is None and reference == commitment


def plan_story_readiness(issues: dict[int, dict], wip_limit: int,
                         commitment: int | None = None) -> list[Decision]:
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
        if not _matches_commitment(project, commitment):
            continue
        decisions.append(Decision(number, BLOCKED, READY, "dependencies satisfied"))
        capacity -= 1
    return decisions


class Skip:
    """Named reasons a project is not advanced to `project:awaiting-acceptance`.

    A project the sequencer leaves alone has to be able to say which fact about
    itself excluded it. Without that, "standing projects are excluded" and "this
    project happened to trip an unrelated guard" look identical from outside.
    """
    NOT_A_PROJECT = "not a type:project issue"
    STANDING = "standing project — §4.1.1: no acceptance edge exists"
    NOT_ACTIVE = "project is not active"
    UNTRUSTED = "project author is outside the §9.9 trust boundary"
    STORIES_UNUSABLE = "declared stories missing, empty, or unparseable"
    STORIES_UNFINISHED = "a declared story has not reached terminal success"
    OUTSIDE_COMMITMENT = "project is outside the selected roadmap commitment"
    READINESS_MISSING = "exact-revision production readiness has not passed"


def completion_skip(project: dict, issues: dict[int, dict],
                    commitment: int | None = None, *,
                    readiness_artifact: dict | None = None,
                    readiness_mode: str = "warning") -> str | None:
    """The reason this project may not advance, or None if it may.

    Each clause tests an independent property of the project, and the two
    lifecycle outcomes are chosen by the *value* of the lifecycle label rather
    than by which guard happens to run first. So reordering these clauses cannot
    change the reason a given project yields: a standing project is excluded for
    being standing, never as a side effect of the `Stories` guard below — which
    matters because that guard is silent today and #298 is removing it.
    """
    if "type:project" not in dispatcher.labels_of(project):
        return Skip.NOT_A_PROJECT
    lifecycle = dispatcher.lifecycle_of(project, dispatcher.PROJECT_LIFECYCLE)
    if lifecycle != ACTIVE:
        return Skip.STANDING if lifecycle == STANDING else Skip.NOT_ACTIVE
    if not dispatcher.is_trusted(project):
        return Skip.UNTRUSTED
    if not _matches_commitment(project, commitment):
        return Skip.OUTSIDE_COMMITMENT
    stories, error = _references(project.get("body") or "", "Stories")
    if error or not stories or any(story not in issues for story in stories):
        return Skip.STORIES_UNUSABLE
    if not all(dispatcher.is_trusted(issues[story]) and
               dispatcher.lifecycle_of(issues[story], dispatcher.STORY_LIFECYCLE)
               in dispatcher.TERMINAL_SUCCESS for story in stories):
        return Skip.STORIES_UNFINISHED
    has_envelope_contract = bool(operating_envelope.section(
        project.get("body") or "", "Operating envelope"))
    if (has_envelope_contract and not production_readiness.permits_completion(
            readiness_artifact, readiness_mode)):
        return Skip.READINESS_MISSING
    return None


def plan_project_completion(issues: dict[int, dict],
                            commitment: int | None = None, *,
                            readiness_artifacts: dict[int, dict] | None = None,
                            readiness_mode: str = "warning") -> list[Decision]:
    """Advance only active projects whose declared children all succeeded."""
    return [Decision(number, ACTIVE, AWAITING_ACCEPTANCE,
                     "all declared stories reached terminal success")
            for number in sorted(issues)
            if completion_skip(
                issues[number], issues, commitment,
                readiness_artifact=(readiness_artifacts or {}).get(number),
                readiness_mode=readiness_mode) is None]


def plan(issues: dict[int, dict], wip_limit: int = dispatcher.WIP_LIMIT,
         commitment: int | None = None, *,
         readiness_artifacts: dict[int, dict] | None = None,
         readiness_mode: str = "warning") -> list[Decision]:
    return (plan_project_completion(
                issues, commitment, readiness_artifacts=readiness_artifacts,
                readiness_mode=readiness_mode)
            + plan_story_readiness(issues, wip_limit, commitment))


def _pages(url: str, token: str) -> list[dict]:
    values, page = [], 1
    while True:
        join = "&" if "?" in url else "?"
        batch = dispatcher._api(f"{url}{join}per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise production_readiness.ReadinessError(
                "production-readiness comments response is malformed")
        values.extend(batch)
        if len(batch) < 100:
            return values
        page += 1


def blocking_readiness_artifacts(repo: str, token: str,
                                 issues: dict[int, dict]) -> dict[int, dict]:
    metadata = dispatcher._api(f"https://api.github.com/repos/{repo}", token)
    branch = metadata.get("default_branch") if isinstance(metadata, dict) else None
    if not isinstance(branch, str) or not branch:
        raise production_readiness.ReadinessError(
            "production-readiness default branch is unavailable")
    integrated = dispatcher._api(
        f"https://api.github.com/repos/{repo}/commits/{branch}", token)
    revision = integrated.get("sha") if isinstance(integrated, dict) else None
    if not isinstance(revision, str):
        raise production_readiness.ReadinessError(
            "production-readiness integrated revision is unavailable")
    artifacts = {}
    for number, project in issues.items():
        if ("type:project" not in dispatcher.labels_of(project)
                or dispatcher.lifecycle_of(
                    project, dispatcher.PROJECT_LIFECYCLE) != ACTIVE
                or not operating_envelope.section(
                    project.get("body") or "", "Operating envelope")):
            continue
        envelope = operating_envelope.parse_project(project.get("body") or "")
        comments = _pages(
            f"https://api.github.com/repos/{repo}/issues/{number}/comments", token)
        artifact = production_readiness.latest(
            comments, repo=repo, project=number, revision=revision,
            envelope=envelope)
        if artifact is not None:
            artifacts[number] = artifact
    return artifacts


def run_production_readiness(repo: str, project: int, token: str) -> bool:
    template = os.environ.get(
        "FACTORY_PRODUCTION_READINESS_LAUNCH",
        "python3 factory/agents/readiness/invoke.py --repo {repo} --project {project}")
    try:
        command = [item.format(repo=repo, project=project)
                   for item in shlex.split(template)]
    except (ValueError, KeyError) as exc:
        raise production_readiness.ReadinessError(
            "production-readiness launch configuration is malformed") from exc
    if not command:
        raise production_readiness.ReadinessError(
            "production-readiness launch configuration is empty")
    env = dict(os.environ)
    env.setdefault("GITHUB_TOKEN", token)
    result = subprocess.run(command, cwd=os.path.join(HERE, "..", ".."),
                            env=env, capture_output=True, text=True, timeout=360)
    detail = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()[-600:]
    if result.returncode:
        print(f"[sequencer] production readiness failed for Project #{project}: "
              f"{detail}", flush=True)
        return False
    print(f"[sequencer] production readiness evaluated Project #{project}: "
          f"{detail}", flush=True)
    return True


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


def _integrated_revision(repo: str, token: str) -> str | None:
    metadata = dispatcher._api(f"https://api.github.com/repos/{repo}", token)
    branch = metadata.get("default_branch") if isinstance(metadata, dict) else None
    if not isinstance(branch, str) or not branch:
        return None
    integrated = dispatcher._api(
        f"https://api.github.com/repos/{repo}/commits/{branch}", token)
    revision = integrated.get("sha") if isinstance(integrated, dict) else None
    return revision if isinstance(revision, str) else None


def apply_decision(repo: str, decision: Decision, token: str, *,
                   required_revision: str | None = None) -> tuple[bool, str]:
    fresh = dispatcher.fetch_issue(repo, decision.number, token)
    if fresh is None:
        return False, "subject disappeared"
    prefix = (dispatcher.STORY_LIFECYCLE if decision.current.startswith("story:")
              else dispatcher.PROJECT_LIFECYCLE)
    if dispatcher.lifecycle_of(fresh, prefix) != decision.current:
        return False, "state changed before write"
    if (required_revision is not None
            and _integrated_revision(repo, token) != required_revision):
        return False, "integrated revision changed before write"
    labels = dispatcher.labels_of(fresh) - {decision.current}
    labels.add(decision.target)
    dispatcher._api(
        f"https://api.github.com/repos/{repo}/issues/{decision.number}", token,
        method="PATCH", payload={"labels": sorted(labels)})
    return True, f"{decision.current} -> {decision.target}"


def run(repo: str, token: str, apply: bool = True,
        wip_limit: int = dispatcher.WIP_LIMIT,
        commitment: int | None = None) -> list[Decision]:
    issues = fetch_all_issues(repo, token)
    readiness_mode = production_readiness.mode()
    if apply:
        candidates = plan_project_completion(issues, commitment)
        for candidate in candidates:
            project = issues[candidate.number]
            # Legacy Projects predate the envelope contract. Promotion applies
            # to newly planned work and never retrofits hidden requirements.
            if operating_envelope.section(
                    project.get("body") or "", "Operating envelope"):
                try:
                    run_production_readiness(repo, candidate.number, token)
                except Exception as exc:
                    # Warning mode is genuinely advisory. Blocking mode still
                    # fails closed below because no exact ready artifact exists.
                    print(f"[sequencer] production readiness could not evaluate "
                          f"Project #{candidate.number}: {type(exc).__name__}: {exc}",
                          flush=True)
    artifacts = (blocking_readiness_artifacts(repo, token, issues)
                 if readiness_mode == "blocking" else {})
    decisions = plan(issues, wip_limit, commitment,
                     readiness_artifacts=artifacts,
                     readiness_mode=readiness_mode)
    applied = []
    for decision in decisions:
        if not apply:
            print(f"[sequencer] would advance #{decision.number}: "
                  f"{decision.current} -> {decision.target}", flush=True)
            applied.append(decision)
            continue
        artifact = artifacts.get(decision.number)
        required_revision = (artifact.get("revision")
                             if (readiness_mode == "blocking"
                                 and decision.target == AWAITING_ACCEPTANCE
                                 and artifact is not None) else None)
        ok, note = apply_decision(
            repo, decision, token, required_revision=required_revision)
        print(f"[sequencer] #{decision.number}: "
              f"{note if ok else 'skipped — ' + note}", flush=True)
        if ok:
            applied.append(decision)
    return applied
