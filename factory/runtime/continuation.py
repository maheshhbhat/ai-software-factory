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

**Standing projects (#314 SM-06).** `state-schema.md` §4.1.1 allows one Project
to be *standing* — continuous work with no acceptance edge — and says at most one
Project may hold that state at a time. That is enforced here, at the §5.1
approval that would create it, because approval is the only moment a Project
becomes standing. A second one is *refused*, not warned about: the Project stays
at `project:awaiting-ready` and the reason names the Project already holding the
state. The check is repeated immediately before the write for the same reason the
lifecycle re-read is: two passes must not both create one.
"""

from __future__ import annotations

import os
import re
import sys
import hashlib
import json
import pathlib
import subprocess
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gates"))
import merge_gate  # noqa: E402

AWAITING_READY = "project:awaiting-ready"
AWAITING_ACCEPTANCE = "project:awaiting-acceptance"
ACTIVE = "project:active"
ACCEPTED = "project:accepted"
# §4.1.1 — continuous work, approved once and never accepted. The label is the
# state, so approving a standing Project ends on this value rather than ACTIVE.
STANDING = "project:standing"

# Approval verdicts. A standing approval is an ordinary §5.1 approval that also
# says which state it ends on; it is not a second kind of bell.
APPROVED = "approved"
APPROVED_STANDING = "approved-standing"

# Only the repository owner rings a bell. COLLABORATOR is deliberately excluded:
# §5 names the CTO as the decision authority, and a wider set would mean any
# write-access identity could advance a project.
DECIDING_ASSOCIATIONS = {"OWNER"}

APPROVAL_HEADING = re.compile(r"^\s*##\s+Plan approval\s*$", re.M)
ACCEPTANCE_HEADING = re.compile(r"^\s*##\s+Acceptance\s*$", re.M)
DECISION_RE = re.compile(r"^decision:\s*(?P<value>.+?)\s*$", re.M | re.I)
RESULT_RE = re.compile(r"^result:\s*(?P<value>pass|fail)\s*$", re.M | re.I)
CRITERION_RE = re.compile(r"^- \[[ xX]\] .+$", re.M)
FOLLOW_UP_RE = re.compile(r"^follow-up:\s*(?P<value>.+?)\s*$", re.M | re.I)
ACTOR_RE = re.compile(r"^actor:\s*@?(?P<value>[A-Za-z0-9-]+)\s*$", re.M | re.I)
SECONDS_RE = re.compile(r"^seconds-spent:\s*(?P<value>[0-9]+)\s*$", re.M | re.I)
STANDING_RE = re.compile(r"\bstanding\b", re.I)
STATUS_RE = re.compile(r"\s(?:—|-)\s+(pass|fail)\b", re.I)
KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]*-[0-9]+)\b")


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
    SECOND_STANDING_PROJECT = "SECOND_STANDING_PROJECT"


class Outcome:
    """What a continuation pass decided about one project."""

    def __init__(self, number: int, action: str | None, reason: str, detail: str = "",
                 decision: dict | None = None):
        self.number = number
        self.action = action          # target lifecycle label, or None
        self.reason = reason
        self.detail = detail
        self.decision = decision

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
    """Read a `## Plan approval` comment. Returns (verdict, error).

    `decision: approved (standing)` approves the Project into §4.1.1's standing
    state. The word is read from the decision line only, never from surrounding
    prose — an approval that *discusses* standing work is an ordinary approval.
    """
    body = (comment.get("body") or "").replace("\r\n", "\n")
    match = DECISION_RE.search(body)
    if not match:
        return None, Reason.MALFORMED_DECISION
    value = match.group("value").strip().strip("*").strip().lower()
    if value.startswith("approved"):
        return (APPROVED_STANDING if STANDING_RE.search(value) else APPROVED), None
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


def acceptance_identity(comment: dict) -> tuple[str, str]:
    """Return the normalized acceptance fingerprint and its canonical payload."""
    body = (comment.get("body") or "").replace("\r\n", "\n")
    verdict, error = classify_acceptance(comment)
    if error:
        raise ValueError(error)
    checklist = {}
    for line in body.splitlines():
        if not line.lstrip().startswith("-"):
            continue
        statuses = STATUS_RE.findall(line)
        if not statuses:
            continue
        key_match = KEY_RE.search(line)
        key = key_match.group(1) if key_match else normalize(line.rsplit("—", 1)[0])
        checklist[key] = statuses[-1].lower()
    follow = FOLLOW_UP_RE.search(body)
    references = sorted(set(re.findall(r"#[1-9][0-9]*", follow.group("value")))) if follow else []
    payload = json.dumps({"result": verdict, "criteria": sorted(checklist.items()),
                          "follow_up": references}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest(), payload


def owner_decisions(comments: list[dict], heading: re.Pattern) -> list[dict]:
    """Owner-authored comments carrying the given §5 heading.

    The association filter is weak by construction — the agent shares the
    owner's credential — so the heading is what actually separates a decision
    from a recording. Held exact for that reason: `## Approval recorded` is not
    `## Plan approval`, and must never be read as one.
    """
    return [c for c in comments
            if is_owner(c) and heading.search((c.get("body") or "").replace("\r\n", "\n"))]


def standing_holders(issues) -> list[int]:
    """Project numbers that carry `project:standing`, lowest first.

    Membership is the *label*, not the resolved lifecycle. A project carrying
    two `project:*` labels resolves to no lifecycle (§2.1) and is inert
    everywhere else — but for a uniqueness rule that would fail open, letting a
    mislabelled project hide the state it is still claiming. It counts here.
    """
    return sorted(number for number, issue in issues.items()
                  if STANDING in labels_of(issue))


def evaluate_project(project: dict, comments: list[dict], standing=()) -> Outcome:
    """Decide whether one project at a bell may advance.

    `standing` is the set of project numbers currently holding `project:standing`
    — the §4.1.1 uniqueness rule needs to see beyond the project in hand. An
    empty default keeps every non-standing evaluation exactly as it was.
    """
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

    if state in (AWAITING_READY, AWAITING_ACCEPTANCE):
        bell_at = timestamp(project.get("_bell_at"))
        if bell_at is None:
            return Outcome(number, None, Reason.BELL_TIME_UNAVAILABLE,
                           f"latest {state} transition time is unavailable")
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
                           "no owner decision belongs to the current bell")

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
        if verdict not in (APPROVED, APPROVED_STANDING):
            return Outcome(number, None, Reason.NO_DECISION,
                           f"latest owner decision is {verdict!r}")
        # §5.1 binding rule. It governs a standing approval identically: the
        # state a Project ends in does not change what an approval binds to.
        quoted = quoted_criteria(comment.get("body") or "")
        live = criteria_of(project.get("body") or "")
        if quoted is not None and normalize(quoted) != normalize(live):
            return Outcome(number, None, Reason.CRITERIA_CHANGED,
                           "the criteria section no longer matches the checklist quoted in "
                           "the approval; §5.1 voids the approval")
        anchor = "" if quoted is not None else " (no criteria quoted — binding unanchored)"
        if verdict == APPROVED_STANDING:
            # §4.1.1 — at most one standing Project. Refused at the transition,
            # so the Project is left whole at its bell rather than half-moved.
            held = [n for n in sorted(standing) if n != number]
            if held:
                return Outcome(number, None, Reason.SECOND_STANDING_PROJECT,
                               f"{', '.join(f'#{n}' for n in held)} already holds "
                               f"{STANDING}; §4.1.1 permits one standing project at a "
                               "time, so this approval is refused, not applied")
            return Outcome(number, STANDING, Reason.CONTINUED,
                           "owner plan approval of a standing project" + anchor, comment)
        return Outcome(number, ACTIVE, Reason.CONTINUED,
                       "owner plan approval" + anchor, comment)

    if verdict == "pass":
        return Outcome(number, ACCEPTED, Reason.CONTINUED,
                       "owner acceptance, all criteria pass", comment)
    return Outcome(number, ACTIVE, Reason.CONTINUED,
                   "owner acceptance recorded a failure; §5.3 returns the project to active",
                   comment)


class TouchEvidenceError(RuntimeError):
    pass


def touchlog_path() -> pathlib.Path:
    configured = os.environ.get("FACTORY_TOUCHLOG_FILE", "").strip()
    return (pathlib.Path(configured) if configured else
            pathlib.Path(__file__).resolve().parents[1] / "touchlog" / "touchlog.jsonl")


def approval_identity(comment: dict) -> str:
    """Stable identity for one plan decision, independent of comment metadata."""
    verdict, error = classify_approval(comment)
    if error:
        raise ValueError(error)
    criteria = quoted_criteria(comment.get("body") or "")
    payload = json.dumps({"decision": verdict, "criteria": normalize(criteria or "")},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def ensure_plan_approval_touch(project: int, comment: dict, *, runner=subprocess.run) -> str:
    """Durably append and read back one plan-approval receipt before consumption."""
    fingerprint = approval_identity(comment)
    fingerprint_marker = f"plan-approval-fingerprint:{fingerprint}"
    receipt_marker = f"plan-approval-receipt:#{project}:{fingerprint}"
    path = touchlog_path()

    def records() -> list[dict]:
        if not path.exists():
            return []
        try:
            return [json.loads(line) for line in path.read_text().splitlines() if line]
        except (OSError, json.JSONDecodeError) as exc:
            raise TouchEvidenceError("canonical touch evidence is unreadable") from exc

    found = [entry for entry in records()
             if entry.get("project") == f"#{project}"
             and entry.get("bell_type") == "plan-approval"
             and fingerprint_marker in (entry.get("note") or "")]
    if len(found) > 1:
        raise TouchEvidenceError("duplicate canonical plan-approval evidence")
    if found:
        return fingerprint

    body = comment.get("body") or ""
    actor_match = ACTOR_RE.search(body)
    actor = (actor_match.group("value") if actor_match else
             ((comment.get("user") or {}).get("login") or "unknown"))
    seconds_match = SECONDS_RE.search(body)
    seconds = int(seconds_match.group("value")) if seconds_match else 0
    note = (f"{receipt_marker}; {fingerprint_marker}; "
            "GitHub plan-approval decision consumed")
    if not seconds_match:
        note += "; time unavailable, recorded as 0"
    script = pathlib.Path(__file__).resolve().parents[1] / "touchlog" / "append.py"
    command = [sys.executable, str(script), "--project", f"#{project}",
               "--bell-type", "plan-approval", "--classification", "decision",
               "--seconds-spent", str(seconds), "--actor", f"@{actor}",
               "--note", note, "--file", str(path),
               "--unique-note-marker", receipt_marker]
    created = comment.get("created_at") or comment.get("createdAt")
    if created:
        command.extend(["--timestamp", created])
    result = runner(command, capture_output=True, text=True)
    if result.returncode:
        raise TouchEvidenceError(
            f"canonical plan-approval append failed: {(result.stderr or result.stdout)[:300]}")
    verified = [entry for entry in records()
                if entry.get("project") == f"#{project}"
                and fingerprint_marker in (entry.get("note") or "")]
    if len(verified) != 1:
        raise TouchEvidenceError("canonical plan-approval evidence read-back failed")
    return fingerprint


def ensure_acceptance_touch(project: int, comment: dict, *, runner=subprocess.run) -> str:
    """Durably append and read back one receipt before decision consumption."""
    fingerprint, _ = acceptance_identity(comment)
    fingerprint_marker = f"acceptance-fingerprint:{fingerprint}"
    # append.py's unique marker is global to the file.  Decision identity is
    # intentionally independent of Project identity, but receipt identity is
    # not: two Projects may make the same normalized decision legitimately.
    receipt_marker = f"acceptance-receipt:#{project}:{fingerprint}"
    path = touchlog_path()

    def records() -> list[dict]:
        if not path.exists():
            return []
        try:
            return [json.loads(line) for line in path.read_text().splitlines() if line]
        except (OSError, json.JSONDecodeError) as exc:
            raise TouchEvidenceError("canonical touch evidence is unreadable") from exc

    found = [entry for entry in records()
             if entry.get("project") == f"#{project}"
             and entry.get("bell_type") == "acceptance"
             and fingerprint_marker in (entry.get("note") or "")]
    if len(found) > 1:
        raise TouchEvidenceError("duplicate canonical acceptance evidence")
    if found:
        return fingerprint

    body = comment.get("body") or ""
    actor_match = ACTOR_RE.search(body)
    actor = (actor_match.group("value") if actor_match else
             ((comment.get("user") or {}).get("login") or "unknown"))
    seconds_match = SECONDS_RE.search(body)
    seconds = int(seconds_match.group("value")) if seconds_match else 0
    note = f"{receipt_marker}; {fingerprint_marker}; GitHub acceptance decision consumed"
    if not seconds_match:
        note += "; time unavailable, recorded as 0"
    script = pathlib.Path(__file__).resolve().parents[1] / "touchlog" / "append.py"
    command = [sys.executable, str(script), "--project", f"#{project}",
               "--bell-type", "acceptance", "--classification", "decision",
               "--seconds-spent", str(seconds), "--actor", f"@{actor}",
               "--note", note, "--file", str(path),
               "--unique-note-marker", receipt_marker]
    created = comment.get("created_at") or comment.get("createdAt")
    if created:
        command.extend(["--timestamp", created])
    result = runner(command, capture_output=True, text=True)
    if result.returncode:
        raise TouchEvidenceError(
            f"canonical acceptance append failed: {(result.stderr or result.stdout)[:300]}")
    verified = [entry for entry in records()
                if entry.get("project") == f"#{project}"
                and fingerprint_marker in (entry.get("note") or "")]
    if len(verified) != 1:
        raise TouchEvidenceError("canonical acceptance evidence read-back failed")
    return fingerprint


def evaluate_all(projects: dict, comments_by_project: dict, standing=()) -> list[Outcome]:
    """Evaluate every known project. Ordered by issue number for reproducibility."""
    return [evaluate_project(projects[n], comments_by_project.get(n, []), standing)
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
    """Every open project issue.

    Wider than the bells this module consumes, because the §4.1.1 uniqueness
    check has to see the standing project — which is by definition not at a bell.
    Filtering is left to `bell_projects` and `standing_holders`, which are pure.
    """
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
            projects[issue["number"]] = issue
        if len(batch) < 100:
            break
        page += 1
    return projects


def bell_projects(issues: dict) -> dict:
    """Those of `issues` sitting at a human bell."""
    return {number: issue for number, issue in issues.items()
            if lifecycle_of(issue) in (AWAITING_READY, AWAITING_ACCEPTANCE)}


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


def fetch_standing(repo: str, token: str) -> list[int]:
    """Open project issues carrying `project:standing`, straight from GitHub."""
    import json
    import urllib.request

    numbers, page = [], 1
    while True:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues"
            f"?state=open&labels=project%3Astanding&per_page=100&page={page}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "factory-continuation"})
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.loads(response.read().decode("utf-8"))
        numbers.extend(issue["number"] for issue in batch
                       if "pull_request" not in issue
                       and "type:project" in labels_of(issue))
        if len(batch) < 100:
            break
        page += 1
    return sorted(numbers)


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

    # §4.1.1 uniqueness, re-checked against live labels for the same reason the
    # lifecycle is: the read that decided this outcome may be stale. Refusing
    # here writes nothing at all, so the project keeps its bell.
    if outcome.action == STANDING:
        held = [n for n in fetch_standing(repo, token) if n != number]
        if held:
            return False, (f"{', '.join(f'#{n}' for n in held)} already holds {STANDING} "
                           "— §4.1.1 permits one standing project at a time")

    # Freshness precedes evidence so a losing stale pass cannot append a receipt
    # for a decision it never consumes. Evidence still precedes the state write.
    if current == AWAITING_READY:
        if outcome.decision is None:
            raise TouchEvidenceError("plan-approval outcome has no authoritative decision")
        ensure_plan_approval_touch(number, outcome.decision)
    elif current == AWAITING_ACCEPTANCE:
        if outcome.decision is None:
            raise TouchEvidenceError("acceptance outcome has no authoritative decision")
        ensure_acceptance_touch(number, outcome.decision)

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
    issues = fetch_projects(repo, token)
    projects = bell_projects(issues)
    standing = standing_holders(issues)
    if not projects:
        return []
    for number, project in projects.items():
        state = lifecycle_of(project)
        if state in (AWAITING_READY, AWAITING_ACCEPTANCE):
            project["_bell_at"] = fetch_bell_at(repo, number, token, state)
    comments = {n: fetch_comments(repo, n, token) for n in projects}
    advanced = []
    for outcome in evaluate_all(projects, comments, standing):
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
