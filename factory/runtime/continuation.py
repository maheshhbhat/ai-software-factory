#!/usr/bin/env python3
"""Continuation — consume human decision comments on projects waiting at bells.

Phase 2 Runtime 2 (#71). Closes a gap observed three times live: the CTO
recorded a valid approval on #55, #61, and #66, and each time the lifecycle sat
still until a human typed `work #N`. The monitor watched for *new issues*; a
bell is rung as a *comment on an existing issue*, which that watch cannot see by
construction.

This pass looks at projects sitting at `project:awaiting-ready` or
`project:awaiting-acceptance`, finds a valid owner-authored decision comment,
and advances the lifecycle canonically.

**What this is not, stated plainly.** Under the accepted single-credential
threat model (§9.7) the agent writes through the CTO's own account, so **every
comment this factory posts is `OWNER`-authored too**. Author association
therefore cannot distinguish a human decision from an agent-written one, and
this module must not pretend otherwise. It is convenience over a decision a
human already made — never a security control.

What actually separates them is a **heading convention**: a decision must carry
the exact §5 heading (`## Plan approval`, `## Acceptance`), while the recording
comments this factory writes use different headings (`## Approval recorded`,
`## Superseded`). That is a convention held up by discipline, and a test pins
the real recording format so a future edit cannot quietly turn it into a
self-approval.

The one real check is the **§5.1 binding rule**: an approval is valid only while
the project's `### Falsifiable acceptance criteria` section still matches what
the approval quoted, so criteria edited after approval visibly void it. That is
checkable from repository data and holds regardless of who typed what.

Everything ambiguous fails closed with a named reason and no transition.

Consumption needs no cursor: advancing a project moves it off the bell, so the
next pass does not see it. The state write is the duplicate suppressor
(`architecture-v2.1.md` §4), exactly as in the dispatcher.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gates"))
import merge_gate  # noqa: E402

AWAITING_READY = "project:awaiting-ready"
AWAITING_ACCEPTANCE = "project:awaiting-acceptance"
ACTIVE = "project:active"
ACCEPTED = "project:accepted"

# Only the repository owner rings a bell. COLLABORATOR is deliberately excluded:
# §5 names the CTO as the decision authority, and a wider set would mean any
# write-access identity could advance a project.
DECIDING_ASSOCIATIONS = {"OWNER"}

APPROVAL_HEADING = re.compile(r"^\s*##\s+Plan approval\s*$", re.M)
ACCEPTANCE_HEADING = re.compile(r"^\s*##\s+Acceptance\s*$", re.M)
DECISION_RE = re.compile(r"^decision:\s*(?P<value>.+?)\s*$", re.M | re.I)
RESULT_RE = re.compile(r"^result:\s*(?P<value>pass|fail)\s*$", re.M | re.I)
CRITERION_RE = re.compile(r"^- \[[ xX]\] .+$", re.M)


class Reason:
    """Named outcomes. Every project left alone must say why."""

    CONTINUED = "CONTINUED"
    NOT_AT_A_BELL = "NOT_AT_A_BELL"
    NO_DECISION = "NO_DECISION"
    NOT_OWNER_AUTHORED = "NOT_OWNER_AUTHORED"
    CRITERIA_CHANGED = "CRITERIA_CHANGED"
    AMBIGUOUS_DECISION = "AMBIGUOUS_DECISION"
    MALFORMED_DECISION = "MALFORMED_DECISION"
    CONFLICTING_DECISIONS = "CONFLICTING_DECISIONS"
    UNSUPPORTED_BELL = "UNSUPPORTED_BELL"
    BELL_TIME_UNAVAILABLE = "BELL_TIME_UNAVAILABLE"


class Outcome:
    """What a continuation pass decided about one project."""

    def __init__(self, number: int, action: str | None, reason: str, detail: str = ""):
        self.number = number
        self.action = action          # target lifecycle label, or None
        self.reason = reason
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Outcome(#{self.number}, {self.action}, {self.reason})"

    def __eq__(self, other) -> bool:
        return (isinstance(other, Outcome) and self.number == other.number
                and self.action == other.action and self.reason == other.reason)


def labels_of(issue: dict) -> set[str]:
    return {merge_gate.label_name(x) for x in issue.get("labels", [])}


def lifecycle_of(issue: dict) -> str | None:
    found = sorted(x for x in labels_of(issue) if x.startswith("project:"))
    return found[0] if len(found) == 1 else None


def is_owner(comment: dict) -> bool:
    return (comment.get("authorAssociation")
            or comment.get("author_association") or "").upper() in DECIDING_ASSOCIATIONS


def timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def criteria_of(body: str) -> str | None:
    """The `### Falsifiable acceptance criteria` section — the thing an approval
    binds to (§5.1)."""
    section = merge_gate.parse_section((body or "").replace("\r\n", "\n"),
                                       "Falsifiable acceptance criteria")
    return section.strip() if section else None


def quoted_criteria(comment_body: str) -> str | None:
    """The checklist a decision comment quotes, if any.

    Returns None when the comment quotes nothing — that is not malformed, it
    just means the binding check has no anchor and must be resolved elsewhere.
    """
    lines = CRITERION_RE.findall((comment_body or "").replace("\r\n", "\n"))
    return "\n".join(lines).strip() if lines else None


def normalize(text: str | None) -> str:
    """Compare criteria on content, not incidental whitespace or checkbox state.

    Checkbox state is deliberately ignored: ticking a box while running the
    criteria is progress, not an amendment. Wording changes are amendments.
    """
    if not text:
        return ""
    out = []
    for line in text.split("\n"):
        line = re.sub(r"^- \[[ xX]\]\s*", "- ", line.strip())
        line = re.sub(r"\s+", " ", line)
        if line:
            out.append(line)
    return "\n".join(out)


def classify_approval(comment: dict) -> tuple[str | None, str | None]:
    """Read a `## Plan approval` comment. Returns (verdict, error)."""
    body = (comment.get("body") or "").replace("\r\n", "\n")
    match = DECISION_RE.search(body)
    if not match:
        return None, Reason.MALFORMED_DECISION
    value = match.group("value").strip().strip("*").strip().lower()
    if value.startswith("approved"):
        return "approved", None
    if "changes-requested" in value or value.startswith("rejected"):
        return "changes-requested", None
    return None, Reason.AMBIGUOUS_DECISION


def classify_acceptance(comment: dict) -> tuple[str | None, str | None]:
    """Read an `## Acceptance` comment. Returns (verdict, error)."""
    body = (comment.get("body") or "").replace("\r\n", "\n")
    match = RESULT_RE.search(body)
    if not match:
        return None, Reason.MALFORMED_DECISION
    return match.group("value").strip().lower(), None


def owner_decisions(comments: list[dict], heading: re.Pattern) -> list[dict]:
    """Owner-authored comments carrying the given §5 heading.

    The association filter is weak by construction — the agent shares the
    owner's credential — so the heading is what actually separates a decision
    from a recording. Held exact for that reason: `## Approval recorded` is not
    `## Plan approval`, and must never be read as one.
    """
    return [c for c in comments
            if is_owner(c) and heading.search((c.get("body") or "").replace("\r\n", "\n"))]


def evaluate_project(project: dict, comments: list[dict]) -> Outcome:
    """Decide whether one project at a bell may advance."""
    number = project.get("number", 0)
    state = lifecycle_of(project)

    if state == AWAITING_READY:
        heading, classify = APPROVAL_HEADING, classify_approval
    elif state == AWAITING_ACCEPTANCE:
        heading, classify = ACCEPTANCE_HEADING, classify_acceptance
    else:
        return Outcome(number, None, Reason.NOT_AT_A_BELL, str(state))

    decisions = owner_decisions(comments, heading)
    if not decisions:
        approval_shaped = [c for c in comments
                           if heading.search((c.get("body") or "").replace("\r\n", "\n"))]
        if approval_shaped:
            return Outcome(number, None, Reason.NOT_OWNER_AUTHORED,
                           f"{len(approval_shaped)} decision-shaped comment(s), none owner-authored")
        return Outcome(number, None, Reason.NO_DECISION)

    if state == AWAITING_ACCEPTANCE:
        bell_at = timestamp(project.get("_bell_at"))
        if bell_at is None:
            return Outcome(number, None, Reason.BELL_TIME_UNAVAILABLE,
                           "latest awaiting-acceptance transition time is unavailable")
        current = []
        for decision in decisions:
            created_at = timestamp(decision.get("created_at") or decision.get("createdAt"))
            if created_at is None:
                return Outcome(number, None, Reason.BELL_TIME_UNAVAILABLE,
                               "an acceptance decision has a missing or malformed timestamp")
            if created_at >= bell_at:
                current.append((created_at, decision))
        decisions = [decision for _, decision in sorted(current, key=lambda item: item[0])]
        if not decisions:
            return Outcome(number, None, Reason.NO_DECISION,
                           "no owner acceptance decision belongs to the current bell")

    verdicts = []
    for comment in decisions:
        verdict, err = classify(comment)
        if err:
            return Outcome(number, None, err, "unreadable decision comment")
        verdicts.append((verdict, comment))

    # Decisions have already been scoped to the current bell. Any disagreement
    # within that bell is ambiguous and fails closed. An earlier failed bell is
    # absent from this set but remains immutable GitHub audit evidence.
    distinct = {v for v, _ in verdicts}
    if len(distinct) > 1:
        return Outcome(number, None, Reason.CONFLICTING_DECISIONS,
                       f"{sorted(distinct)} across {len(verdicts)} owner comments")

    verdict, comment = verdicts[-1]

    if state == AWAITING_READY:
        if verdict != "approved":
            return Outcome(number, None, Reason.NO_DECISION,
                           f"latest owner decision is {verdict!r}")
        # §5.1 binding rule.
        quoted = quoted_criteria(comment.get("body") or "")
        live = criteria_of(project.get("body") or "")
        if quoted is not None and normalize(quoted) != normalize(live):
            return Outcome(number, None, Reason.CRITERIA_CHANGED,
                           "the criteria section no longer matches the checklist quoted in "
                           "the approval; §5.1 voids the approval")
        return Outcome(number, ACTIVE, Reason.CONTINUED,
                       "owner plan approval" + ("" if quoted is not None
                                                else " (no criteria quoted — binding unanchored)"))

    if verdict == "pass":
        return Outcome(number, ACCEPTED, Reason.CONTINUED, "owner acceptance, all criteria pass")
    return Outcome(number, ACTIVE, Reason.CONTINUED,
                   "owner acceptance recorded a failure; §5.3 returns the project to active")


def evaluate_all(projects: dict, comments_by_project: dict) -> list[Outcome]:
    """Evaluate every known project. Ordered by issue number for reproducibility."""
    return [evaluate_project(projects[n], comments_by_project.get(n, []))
            for n in sorted(projects)]


def continuation_line(outcome: Outcome) -> str:
    """One line per advanced project, mirroring the dispatcher's DISPATCH line.

    Identity and target only: no criteria, no decision text. Worker-neutral by
    construction — this names no agent, because continuing a lifecycle is not
    work assigned to anyone.
    """
    return f"CONTINUE project=#{outcome.number} to={outcome.action}"


# --------------------------------------------------------------------------
# GitHub I/O — kept apart from the pure decision logic above
# --------------------------------------------------------------------------


def fetch_projects(repo: str, token: str) -> dict:
    """Open project issues sitting at a human bell."""
    import json
    import urllib.request

    def api(url):
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "factory-continuation",
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    projects, page = {}, 1
    while True:
        batch = api(f"https://api.github.com/repos/{repo}/issues"
                    f"?state=open&per_page=100&page={page}")
        if not batch:
            break
        for issue in batch:
            if "pull_request" in issue:
                continue
            if "type:project" not in labels_of(issue):
                continue
            if lifecycle_of(issue) in (AWAITING_READY, AWAITING_ACCEPTANCE):
                projects[issue["number"]] = issue
        if len(batch) < 100:
            break
        page += 1
    return projects


def fetch_comments(repo: str, number: int, token: str) -> list[dict]:
    import json
    import urllib.request

    comments, page = [], 1
    while True:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues/{number}/comments"
            f"?per_page=100&page={page}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "factory-continuation"})
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.loads(response.read().decode("utf-8"))
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return comments


def fetch_bell_at(repo: str, number: int, token: str, label: str) -> str | None:
    """Latest GitHub timeline event that put this project at its current bell."""
    import json
    import urllib.request

    events, page = [], 1
    while True:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues/{number}/timeline"
            f"?per_page=100&page={page}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "factory-continuation"})
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.loads(response.read().decode("utf-8"))
        events.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    labeled = [event.get("created_at") for event in events
               if event.get("event") == "labeled"
               and (event.get("label") or {}).get("name") == label]
    valid = [(timestamp(value), value) for value in labeled]
    valid = [(parsed, value) for parsed, value in valid if parsed is not None]
    return max(valid, key=lambda item: item[0])[1] if valid else None


def apply_outcome(repo: str, project: dict, outcome: Outcome, token: str) -> tuple[bool, str]:
    """Advance one project, re-checking its state immediately before writing.

    Idempotent by that re-read: a project already moved off the bell is left
    alone, so two passes cannot both transition it. §9.2 requires the write to
    replace the complete label set in one call.
    """
    import json
    import urllib.request

    number = project["number"]

    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues/{number}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "factory-continuation"})
    with urllib.request.urlopen(request, timeout=30) as response:
        fresh = json.loads(response.read().decode("utf-8"))

    current = lifecycle_of(fresh)
    if current not in (AWAITING_READY, AWAITING_ACCEPTANCE):
        return False, f"no longer at a bell (now {current}) — another pass consumed it"

    labels = sorted((labels_of(fresh) - {AWAITING_READY, AWAITING_ACCEPTANCE})
                    | {outcome.action})
    payload = {"labels": labels}
    if outcome.action == ACCEPTED:
        payload.update({"state": "closed", "state_reason": "completed"})  # §9.3

    data = json.dumps(payload).encode()
    write = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues/{number}",
        data=data, method="PATCH",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "factory-continuation"})
    with urllib.request.urlopen(write, timeout=30):
        pass

    return True, f"{current} -> {outcome.action}"


def run(repo: str, token: str, apply: bool = True) -> list[Outcome]:
    """One continuation pass. Returns the outcomes that advanced a project."""
    projects = fetch_projects(repo, token)
    if not projects:
        return []
    for number, project in projects.items():
        if lifecycle_of(project) == AWAITING_ACCEPTANCE:
            project["_bell_at"] = fetch_bell_at(
                repo, number, token, AWAITING_ACCEPTANCE)
    comments = {n: fetch_comments(repo, n, token) for n in projects}
    advanced = []
    for outcome in evaluate_all(projects, comments):
        if outcome.action is None:
            if outcome.reason not in (Reason.NOT_AT_A_BELL, Reason.NO_DECISION):
                print(f"[continuation] #{outcome.number} not advanced: "
                      f"{outcome.reason} ({outcome.detail})", flush=True)
            continue
        if not apply:
            print(f"[continuation] would advance {continuation_line(outcome)}", flush=True)
            advanced.append(outcome)
            continue
        ok, note = apply_outcome(repo, projects[outcome.number], outcome, token)
        if ok:
            print(f"[continuation] #{outcome.number}: {note} ({outcome.detail})", flush=True)
            print(continuation_line(outcome), flush=True)
            advanced.append(outcome)
        else:
            print(f"[continuation] #{outcome.number} skipped: {note}", flush=True)
    return advanced
