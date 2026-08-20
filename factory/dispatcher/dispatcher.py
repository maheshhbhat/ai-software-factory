#!/usr/bin/env python3
"""Dispatcher — authorization, eligibility, claim, dispatch.

Implements `factory/spec/state-schema.md` §9.9 (trust boundary), §9.10 (WIP and
deterministic selection), §9.11 (live routes) and §9.12 (discovery implies
dispatch). Phase 2, Dispatcher increment 1.

What it does: reads durable GitHub state, decides which stories are authorized
and eligible, claims at most `WIP_LIMIT` of them with one atomic label-set
replacement each, and emits a dispatch line the local monitor turns into a
worker wake-up. That last part is the point of the increment — it removes the
relay where a human reads an issue number aloud to start authorized work.

What authorizes work: **a collaborator-set lifecycle label on an artifact whose
whole chain checks out** — story → project → standing roadmap commitment. Never
issue creation, never body prose, never a comment, never `Agent-ID`. On a public
repository anyone can open an issue and write persuasive text; none of it
reaches a code path here.

Fail closed everywhere: anything ambiguous, malformed, legacy, or unreadable is
ineligible with a named reason, never dispatched.

Usage:
    dispatcher.py --repo owner/name --commitment 47            # dry run (default)
    dispatcher.py --repo owner/name --commitment 47 --claim    # claim + dispatch
    dispatcher.py --fixture path.json                          # offline evaluation
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gates"))
import merge_gate  # noqa: E402  — single source of truth for the §9.6 Scope dialect

WIP_LIMIT = 2                      # §9.10 — a contract value, not a runtime tweak
ATTEMPT_MAX = 3                    # §4.3.5 — checked at dispatch time, before incrementing
SUPPORTED_SCHEMA_MAJOR = merge_gate.SUPPORTED_SCHEMA_MAJOR

# §9.9: only these author associations may have their issues treated as factory
# state. Everything else is public input — readable, never authorizing.
TRUSTED_ASSOCIATIONS = {"OWNER", "COLLABORATOR", "MEMBER"}

STORY_LIFECYCLE = "story:"
PROJECT_LIFECYCLE = "project:"
READY = "story:ready"
CLAIMED = "story:claimed"
MERGED = "story:merged"
PROJECT_ACTIVE = "project:active"

ISSUE_REF_RE = re.compile(r"^#(\d+)$")


class Reason:
    """Named ineligibility reasons. Every rejection is attributable — a story
    that does not run must say why, or the dispatcher is unauditable."""

    ELIGIBLE = "ELIGIBLE"
    NOT_A_STORY = "NOT_A_STORY"
    UNTRUSTED_AUTHOR = "UNTRUSTED_AUTHOR"
    NOT_READY = "NOT_READY"
    AMBIGUOUS_LIFECYCLE = "AMBIGUOUS_LIFECYCLE"
    ISSUE_CLOSED = "ISSUE_CLOSED"
    PROJECT_LINK_MISSING = "PROJECT_LINK_MISSING"
    PROJECT_LINK_MALFORMED = "PROJECT_LINK_MALFORMED"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    PROJECT_WRONG_TYPE = "PROJECT_WRONG_TYPE"
    PROJECT_UNTRUSTED_AUTHOR = "PROJECT_UNTRUSTED_AUTHOR"
    PROJECT_NOT_ACTIVE = "PROJECT_NOT_ACTIVE"
    COMMITMENT_LINK_MISSING = "COMMITMENT_LINK_MISSING"
    COMMITMENT_MISMATCH = "COMMITMENT_MISMATCH"
    DEPENDENCY_UNMET = "DEPENDENCY_UNMET"
    DEPENDS_ON_MALFORMED = "DEPENDS_ON_MALFORMED"
    SCOPE_INVALID = "SCOPE_INVALID"
    ATTEMPT_INVALID = "ATTEMPT_INVALID"
    ATTEMPT_EXHAUSTED = "ATTEMPT_EXHAUSTED"


@dataclass
class Decision:
    number: int
    eligible: bool
    reason: str
    project: int | None = None
    detail: str = ""


@dataclass
class Plan:
    """What a poll decided. Pure data — nothing has been written yet."""

    decisions: list[Decision] = field(default_factory=list)
    selected: list[Decision] = field(default_factory=list)
    wip_in_use: int = 0
    capacity: int = 0

    def eligible(self) -> list[Decision]:
        return [d for d in self.decisions if d.eligible]


# --------------------------------------------------------------------------
# Pure evaluation — no I/O, fully testable offline
# --------------------------------------------------------------------------


def labels_of(issue: dict) -> set[str]:
    return {merge_gate.label_name(x) for x in issue.get("labels", [])}


def lifecycle_of(issue: dict, prefix: str) -> str | None:
    """Exactly one lifecycle label, or None. Two is ambiguous, not 'the first'."""
    found = sorted(x for x in labels_of(issue) if x.startswith(prefix))
    return found[0] if len(found) == 1 else None


def section_ref(body: str, name: str) -> tuple[int | None, str | None]:
    """Parse a `### <name>` section holding exactly one `#N`."""
    raw = merge_gate.parse_section(body or "", name)
    if raw is None:
        return None, Reason.PROJECT_LINK_MISSING
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    if len(lines) != 1:
        return None, Reason.PROJECT_LINK_MALFORMED
    match = ISSUE_REF_RE.match(lines[0])
    if not match:
        return None, Reason.PROJECT_LINK_MALFORMED
    return int(match.group(1)), None


def parse_depends_on(body: str) -> tuple[list[int], str | None]:
    """§3.3: the single token `none`, or one bare `#N` per line."""
    raw = merge_gate.parse_section(body or "", "Depends-on")
    if raw is None:
        return [], Reason.DEPENDS_ON_MALFORMED
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    if lines == ["none"]:
        return [], None
    refs = []
    for line in lines:
        match = ISSUE_REF_RE.match(line)
        if not match:
            return [], Reason.DEPENDS_ON_MALFORMED
        refs.append(int(match.group(1)))
    return refs, None


def is_trusted(issue: dict) -> bool:
    """§9.9. An issue authored by anyone else is public input, not factory state."""
    return (issue.get("author_association") or "").upper() in TRUSTED_ASSOCIATIONS


def evaluate_story(story: dict, projects: dict, stories: dict,
                   commitment: int) -> Decision:
    """Decide whether one story is authorized and eligible for dispatch.

    Order matters only for the quality of the reported reason; every check is
    independent and any failure means no dispatch.
    """
    number = story.get("number", 0)

    if "type:story" not in labels_of(story):
        return Decision(number, False, Reason.NOT_A_STORY)

    # §9.9 — the trust boundary, checked before anything in the body is read.
    if not is_trusted(story):
        return Decision(number, False, Reason.UNTRUSTED_AUTHOR,
                        detail=f"author_association={story.get('author_association')}")

    if (story.get("state") or "OPEN").upper() != "OPEN":
        return Decision(number, False, Reason.ISSUE_CLOSED)

    lifecycle = lifecycle_of(story, STORY_LIFECYCLE)
    if lifecycle is None:
        return Decision(number, False, Reason.AMBIGUOUS_LIFECYCLE,
                        detail="zero or multiple story:* labels")
    if lifecycle != READY:
        return Decision(number, False, Reason.NOT_READY, detail=lifecycle)

    body = story.get("body") or ""

    project_number, err = section_ref(body, "Project")
    if err:
        return Decision(number, False, err)

    project = projects.get(project_number)
    if project is None:
        return Decision(number, False, Reason.PROJECT_NOT_FOUND, project=project_number)
    if "type:project" not in labels_of(project):
        return Decision(number, False, Reason.PROJECT_WRONG_TYPE, project=project_number)
    if not is_trusted(project):
        return Decision(number, False, Reason.PROJECT_UNTRUSTED_AUTHOR, project=project_number)

    project_state = lifecycle_of(project, PROJECT_LIFECYCLE)
    if project_state != PROJECT_ACTIVE:
        return Decision(number, False, Reason.PROJECT_NOT_ACTIVE, project=project_number,
                        detail=str(project_state))

    # The chain must reach the standing commitment through the existing §3.2
    # field — no maintenance special case (#38, #53).
    commitment_ref, err = section_ref(project.get("body") or "", "Roadmap commitment")
    if err:
        return Decision(number, False, Reason.COMMITMENT_LINK_MISSING, project=project_number,
                        detail=f"project #{project_number} carries no usable "
                               f"`### Roadmap commitment` link ({err})")
    if commitment_ref != commitment:
        return Decision(number, False, Reason.COMMITMENT_MISMATCH, project=project_number,
                        detail=f"project points at #{commitment_ref}, expected #{commitment}")

    deps, err = parse_depends_on(body)
    if err:
        return Decision(number, False, err, project=project_number)
    for dep in deps:
        dep_issue = stories.get(dep)
        if dep_issue is None or lifecycle_of(dep_issue, STORY_LIFECYCLE) != MERGED:
            return Decision(number, False, Reason.DEPENDENCY_UNMET, project=project_number,
                            detail=f"#{dep}")

    patterns, scope_err = merge_gate.parse_scope(body)
    if scope_err:
        return Decision(number, False, Reason.SCOPE_INVALID, project=project_number,
                        detail=scope_err)

    attempt_raw = (merge_gate.parse_section(body, "Attempt") or "").strip()
    if not attempt_raw.isdigit():
        return Decision(number, False, Reason.ATTEMPT_INVALID, project=project_number,
                        detail=repr(attempt_raw))
    if int(attempt_raw) >= ATTEMPT_MAX:
        return Decision(number, False, Reason.ATTEMPT_EXHAUSTED, project=project_number,
                        detail=f"Attempt={attempt_raw}; §4.3.5 routes to poison, not dispatch")

    return Decision(number, True, Reason.ELIGIBLE, project=project_number)


def plan_dispatch(stories: dict, projects: dict, commitment: int,
                  wip_limit: int = WIP_LIMIT) -> Plan:
    """Evaluate every story, then select deterministically (§9.10)."""
    plan = Plan(capacity=wip_limit)

    plan.wip_in_use = sum(
        1 for s in stories.values()
        if lifecycle_of(s, STORY_LIFECYCLE) == CLAIMED
        and (s.get("state") or "OPEN").upper() == "OPEN"
    )

    for number in sorted(stories):
        plan.decisions.append(
            evaluate_story(stories[number], projects, stories, commitment))

    # Total ordering by (project number, story number): no ties, no randomness.
    ordered = sorted(plan.eligible(), key=lambda d: (d.project or 0, d.number))
    free = max(0, wip_limit - plan.wip_in_use)
    plan.selected = ordered[:free]
    return plan


# --------------------------------------------------------------------------
# GitHub I/O
# --------------------------------------------------------------------------


def _api(url: str, token: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "factory-dispatcher",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_issues(repo: str, token: str) -> dict:
    """All open issues. The dispatcher's queue is open issues (§9.3)."""
    issues, page = {}, 1
    while True:
        batch = _api(f"https://api.github.com/repos/{repo}/issues"
                     f"?state=open&per_page=100&page={page}", token)
        if not batch:
            break
        for issue in batch:
            if "pull_request" not in issue:
                issues[issue["number"]] = issue
        if len(batch) < 100:
            break
        page += 1
    return issues


def fetch_issue(repo: str, number: int, token: str) -> dict | None:
    try:
        return _api(f"https://api.github.com/repos/{repo}/issues/{number}", token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def claim(repo: str, story: dict, token: str) -> tuple[bool, str]:
    """Claim a story: bump `Attempt`, then replace the label set atomically.

    Idempotent by re-reading immediately before writing: a second poll finds the
    story no longer `story:ready` and declines. The state write is the duplicate
    suppressor (§9.10) — no lock, no cursor, nothing outside GitHub.
    """
    number = story["number"]

    fresh = fetch_issue(repo, number, token)
    if fresh is None:
        return False, "story vanished between planning and claiming"
    if lifecycle_of(fresh, STORY_LIFECYCLE) != READY:
        return False, (f"no longer {READY} at claim time "
                       f"(now {lifecycle_of(fresh, STORY_LIFECYCLE)}) — another "
                       f"poll claimed it, or a human moved it")

    body = (fresh.get("body") or "").replace("\r\n", "\n")
    attempt_raw = (merge_gate.parse_section(body, "Attempt") or "").strip()
    if not attempt_raw.isdigit():
        return False, f"Attempt is not an integer: {attempt_raw!r}"
    attempt = int(attempt_raw)

    # §4.3.2 — the counter increments here, on dispatch, and only here.
    new_body = re.sub(r"(### Attempt\n\n)(\d+)(\n)",
                      lambda m: m.group(1) + str(attempt + 1) + m.group(3),
                      body, count=1)
    if new_body == body:
        return False, "could not update the Attempt section"

    labels = sorted((labels_of(fresh) - {READY}) | {CLAIMED})

    # §9.2 — one PATCH carrying the complete final label set, never add-then-remove.
    _api(f"https://api.github.com/repos/{repo}/issues/{number}", token,
         method="PATCH", payload={"body": new_body, "labels": labels})

    return True, f"Attempt {attempt} -> {attempt + 1}; labels {' '.join(labels)}"


# --------------------------------------------------------------------------
# Dispatch — the relay this increment deletes
# --------------------------------------------------------------------------


def dispatch_line(number: int, project: int | None) -> str:
    """One line per claimed story, on stdout.

    The local monitor already turns each stdout line into a notification, so this
    is the whole integration: no second orchestration system, no queue, no daemon.
    GitHub remains the source of truth; this line is a wake-up, not authorization
    (§9.12).
    """
    return f"DISPATCH story=#{number} project=#{project} agent=claude-delivery"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render(plan: Plan, claimed: list[tuple[int, str]], dry_run: bool) -> str:
    lines = [f"Dispatcher — {len(plan.decisions)} issue(s) considered, "
             f"WIP {plan.wip_in_use}/{plan.capacity}", ""]
    for decision in plan.decisions:
        if decision.reason == Reason.NOT_A_STORY:
            continue  # not factory state; silence is correct
        mark = "ELIGIBLE" if decision.eligible else "skip"
        detail = f" ({decision.detail})" if decision.detail else ""
        lines.append(f"  #{decision.number:<4} {mark:<9} {decision.reason}{detail}")
    lines.append("")
    if not plan.eligible():
        lines.append("No eligible work — nothing dispatched.")
    elif not plan.selected:
        lines.append(f"Capacity exhausted ({plan.wip_in_use}/{plan.capacity} claimed); "
                     f"{len(plan.eligible())} eligible story(ies) wait.")
    else:
        verb = "would claim" if dry_run else "claimed"
        lines.append(f"Selected ({verb}, in order): "
                     + ", ".join(f"#{d.number}" for d in plan.selected))
        for number, note in claimed:
            lines.append(f"  #{number}: {note}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Factory dispatcher (§9.9–§9.12)")
    parser.add_argument("--repo", help="owner/name")
    parser.add_argument("--commitment", type=int,
                        help="issue number of the standing roadmap commitment")
    parser.add_argument("--claim", action="store_true",
                        help="actually claim and dispatch (default is a dry run)")
    parser.add_argument("--wip-limit", type=int, default=WIP_LIMIT)
    parser.add_argument("--fixture", help="JSON fixture for offline evaluation")
    args = parser.parse_args(argv)

    if args.fixture:
        with open(args.fixture, encoding="utf-8") as handle:
            data = json.load(handle)
        stories = {int(k): v for k, v in data.get("stories", {}).items()}
        projects = {int(k): v for k, v in data.get("projects", {}).items()}
        plan = plan_dispatch(stories, projects, data["commitment"],
                             data.get("wip_limit", args.wip_limit))
        print(render(plan, [], dry_run=True))
        for decision in plan.selected:
            print(dispatch_line(decision.number, decision.project))
        return 0

    if not (args.repo and args.commitment):
        parser.error("--repo and --commitment are required without --fixture")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        print("Dispatcher — FAIL: no GITHUB_TOKEN; cannot read durable state, "
              "so nothing is dispatched (fail closed).")
        return 1

    issues = fetch_issues(args.repo, token)
    stories = {n: i for n, i in issues.items() if "type:story" in labels_of(i)}
    projects = {n: i for n, i in issues.items() if "type:project" in labels_of(i)}

    plan = plan_dispatch(stories, projects, args.commitment, args.wip_limit)

    claimed: list[tuple[int, str]] = []
    dispatched: list[Decision] = []
    if args.claim:
        for decision in plan.selected:
            ok, note = claim(args.repo, stories[decision.number], token)
            claimed.append((decision.number, note if ok else f"NOT claimed — {note}"))
            if ok:
                dispatched.append(decision)

    print(render(plan, claimed, dry_run=not args.claim))
    for decision in dispatched:
        print(dispatch_line(decision.number, decision.project))
    return 0


def guarded_main(argv: list[str]) -> int:
    """A dispatcher that crashes must not look like 'no work to do'."""
    try:
        return main(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - deliberately total
        import traceback
        print(f"Dispatcher — FAIL: {type(exc).__name__}: {exc}")
        print("Nothing was dispatched. Fail closed.")
        traceback.print_exc(file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main(sys.argv[1:]))
