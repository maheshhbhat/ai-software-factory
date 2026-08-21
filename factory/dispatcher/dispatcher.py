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
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gates"))
import merge_gate  # noqa: E402  — single source of truth for the §9.6 Scope dialect

WIP_LIMIT = 2                      # §9.10 — a contract value, not a runtime tweak
ATTEMPT_MAX = 3                    # §4.3.5 — checked at dispatch time, before incrementing
CLAIM_LEASE = timedelta(minutes=60)  # §9.4

# §9.4.1 — how many times a single story may be recovered from an expired claim
# before the dispatcher stops trying and asks a human.
#
# This exists because durable evidence cannot distinguish a worker that died
# before starting from one that ran correctly and produced no pull request:
# both leave an expired claim and no PR. Recovery restores the attempt it
# refunds, so without a cap the pair (dispatch, expiry) cycles forever and the
# poison threshold is never reached — observed live on story #64 (#65).
#
# The policy is deliberately dumb rather than clever: recover a fixed number of
# times, then stop and route to a human, instead of inferring which case
# occurred. A story therefore reaches a terminal or human-queue state after at
# most RECOVERY_MAX + 1 dispatches through the expiry path.
RECOVERY_MAX = 2

# §4.3.7 — how many times a poisoned story may be rescued by a human before the
# factory stops asking. Counted as the number of times `story:blocked:poison`
# has been applied in the issue timeline, because GitHub's timeline is
# authoritative history and no local counter may contradict it.
#
# The number decides *closure*, not the poisoning itself. §9.3 keeps a poisoned
# story **open** until this cap is spent: it is waiting for a human, and an
# issue closed as `not planned` is not waiting for anyone. Closing on the first
# poisoning — which is what this module did until #110 — made the §4.3.6 rescue
# unreachable, since only a human may reopen a closed issue (§9.3) and no
# component may. It also hid the story from every queue built on open issues,
# which is the population the human queue enumerates.
RESCUE_MAX = 2

SUPPORTED_SCHEMA_MAJOR = merge_gate.SUPPORTED_SCHEMA_MAJOR

# §9.9: only these author associations may have their issues treated as factory
# state. Everything else is public input — readable, never authorizing.
TRUSTED_ASSOCIATIONS = {"OWNER", "COLLABORATOR", "MEMBER"}

STORY_LIFECYCLE = "story:"
PROJECT_LIFECYCLE = "project:"
READY = "story:ready"
CLAIMED = "story:claimed"
IN_REVIEW = "story:in-review"
MERGED = "story:merged"
COMPLETED = "story:completed"     # §9.16 — succeeded, nothing to merge
CANCELLED = "story:cancelled"     # §9.3 — deliberately stopped; human decision only
POISON = "story:blocked:poison"
PROJECT_ACTIVE = "project:active"

# The two ways a story can be *done and successful*. They differ only in whether
# the work produced something to merge, which is a fact about the assignment and
# not about whether it succeeded — so a dependency is satisfied by either
# (§9.10). Nothing here decides that a story is finished; both labels are
# written elsewhere and this only reads them.
TERMINAL_SUCCESS = frozenset({MERGED, COMPLETED})

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
    COMPLETED = "COMPLETED"
    DEPENDENCY_UNMET = "DEPENDENCY_UNMET"
    DEPENDS_ON_MALFORMED = "DEPENDS_ON_MALFORMED"
    SCOPE_INVALID = "SCOPE_INVALID"
    ATTEMPT_INVALID = "ATTEMPT_INVALID"
    ATTEMPT_EXHAUSTED = "ATTEMPT_EXHAUSTED"
    CANCELLED = "CANCELLED"


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


@dataclass(frozen=True)
class RecoveryDecision:
    """A deterministic reconciliation derived only from durable evidence."""

    number: int
    action: str
    reason: str
    detail: str = ""


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def linked_delivery_prs(number: int, pull_requests: list[dict]) -> tuple[list[dict], str | None]:
    """Return valid §9.5 links, failing closed on duplicate canonical lines."""
    pattern = re.compile(rf"(?m)^Story: #{number}$")
    linked = []
    for pr in pull_requests:
        body = (pr.get("body") or "").replace("\r\n", "\n")
        count = len(pattern.findall(body))
        if count > 1:
            return [], f"PR #{pr.get('number')} has {count} Story: #{number} lines"
        if count == 1:
            linked.append(pr)
    return linked, None


def count_expiry_recoveries(timeline: list[dict]) -> int:
    """How many times this story has already been recovered from an expired claim.

    Derived entirely from the issue timeline — GitHub is the source of record and
    no local counter may contradict it (§9.12). A recovery leaves a distinctive
    trace: `story:claimed` removed and `story:ready` applied, with no
    `story:in-review` between them. A findings-driven return to ready goes
    through `story:in-review` instead, so the two are distinguishable without
    storing anything.
    """
    lifecycle = [
        (event.get("created_at"), event.get("event"),
         merge_gate.label_name(event.get("label", {})))
        for event in timeline
        if event.get("event") in ("labeled", "unlabeled")
        and merge_gate.label_name(event.get("label", {})).startswith(STORY_LIFECYCLE)
    ]
    lifecycle.sort(key=lambda item: (item[0] or "", item[2]))

    recoveries = 0
    seen_claimed = False
    seen_in_review = False
    for _, event, label in lifecycle:
        if event != "labeled":
            continue
        if label == CLAIMED:
            seen_claimed, seen_in_review = True, False
        elif label == IN_REVIEW:
            seen_in_review = True
        elif label == READY and seen_claimed and not seen_in_review:
            # claimed -> ready without passing through review: an expiry recovery.
            recoveries += 1
            seen_claimed = False
    return recoveries


def count_poisonings(timeline: list[dict]) -> int:
    """How many times `story:blocked:poison` has already been applied (§4.3.7).

    Derived from the issue timeline for the same reason `count_expiry_recoveries`
    is: durable history is the record, and a story that has been rescued twice
    must read as rescued twice after any restart, on any machine, with no local
    state involved.
    """
    return sum(
        1 for event in timeline
        if event.get("event") == "labeled"
        and merge_gate.label_name(event.get("label", {})) == POISON
    )


def poison_closure(timeline: list[dict]) -> tuple[bool, str]:
    """Does *this* poisoning close the issue? §9.3 and §4.3.7 decide together.

    §9.3 lists `story:blocked:poison` as terminal-and-closed only "after the
    §4.3.7 rescue cap"; before that the story is open, because it is waiting for
    a human rescue and closure would end the wait rather than serve it.

    Returns (close, reason). The reason is written into the operator note either
    way, so a poisoning always says which of the two things it was.
    """
    already = count_poisonings(timeline)
    if already >= RESCUE_MAX:
        return True, (f"poisoning {already + 1} with {already} rescue(s) already spent "
                      f"(cap {RESCUE_MAX}); §4.3.7 closes this as not planned and "
                      f"returns it to planning rather than asking for a {already + 1}th rescue")
    return False, (f"poisoning {already + 1} of {RESCUE_MAX + 1}; the issue stays open "
                   f"awaiting the §4.3.6 rescue, which needs a rescue comment, "
                   f"Attempt reset to 0, and a poison-rescue touch")


def recovery_decision(story: dict, timeline: list[dict], pull_requests: list[dict],
                      now: datetime) -> RecoveryDecision:
    """Reconcile one claim under §9.4; ambiguity always fails closed."""
    number = story.get("number", 0)
    if lifecycle_of(story, STORY_LIFECYCLE) != CLAIMED:
        return RecoveryDecision(number, "none", "NOT_CLAIMED")

    prs, link_error = linked_delivery_prs(number, pull_requests)
    if link_error:
        return RecoveryDecision(number, "bell", "INVALID_PR_LINK", link_error)
    if len(prs) > 1:
        return RecoveryDecision(number, "bell", "AMBIGUOUS_LINKED_PRS",
                                f"{len(prs)} PRs carry Story: #{number}")
    if prs:
        pr = prs[0]
        if pr.get("merged_at") or pr.get("merged") is True:
            return RecoveryDecision(number, "merged", "MERGED_DELIVERY_PR",
                                    f"PR #{pr.get('number')}")
        return RecoveryDecision(number, "none", "LINKED_PR_EXISTS",
                                f"PR #{pr.get('number')} state={pr.get('state')}")

    claimed_events = [
        event for event in timeline
        if event.get("event") == "labeled"
        and merge_gate.label_name(event.get("label", {})) == CLAIMED
        and event.get("created_at")
    ]
    if not claimed_events:
        return RecoveryDecision(number, "bell", "CLAIM_EVENT_MISSING")
    try:
        claimed_at = max(_parse_time(event["created_at"]) for event in claimed_events)
    except (TypeError, ValueError):
        return RecoveryDecision(number, "bell", "CLAIM_EVENT_TIME_INVALID")
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if now - claimed_at < CLAIM_LEASE:
        return RecoveryDecision(number, "none", "CLAIM_FRESH",
                                f"claimed_at={claimed_at.isoformat()}")

    attempt_raw = (merge_gate.parse_section(story.get("body") or "", "Attempt") or "").strip()
    if not attempt_raw.isdigit() or int(attempt_raw) < 1:
        return RecoveryDecision(number, "bell", "RECOVERY_ATTEMPT_INVALID",
                                f"Attempt={attempt_raw!r}")

    # §9.4.1 — the bound. Counted from the timeline, which is durable history
    # no local state can contradict.
    recoveries = count_expiry_recoveries(timeline)
    if recoveries >= RECOVERY_MAX:
        return RecoveryDecision(
            number, "poison", "RECOVERY_BUDGET_EXHAUSTED",
            f"{recoveries} expiry recoveries already used (max {RECOVERY_MAX}); "
            f"the claim expired again with no linked PR. Either the worker keeps "
            f"dying before it starts, or this story legitimately produces no PR "
            f"and should be cancelled (§9.3). A human decides which — the "
            f"dispatcher cannot tell them apart from durable evidence.")

    return RecoveryDecision(number, "ready", "CLAIM_LEASE_EXPIRED",
                            f"claimed_at={claimed_at.isoformat()}; Attempt {attempt_raw} -> "
                            f"{int(attempt_raw) - 1}; recovery {recoveries + 1}/{RECOVERY_MAX}")


def reconcile_claims(stories: dict, timelines: dict[int, list[dict]],
                     pull_requests: list[dict], now: datetime) -> list[RecoveryDecision]:
    """Pure recovery pass. Call before `plan_dispatch`, never after it."""
    return [
        recovery_decision(stories[number], timelines.get(number, []), pull_requests, now)
        for number in sorted(stories)
        if lifecycle_of(stories[number], STORY_LIFECYCLE) == CLAIMED
    ]


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


class DependencyIndex:
    """Resolves `### Depends-on` references to issues, closed ones included.

    The dispatch queue is open issues and stays that way (§9.3): nothing this
    index loads becomes a dispatch candidate, because `plan_dispatch` iterates
    the queue it was handed and never this. The index answers a different
    question — *what happened to the issue this story names* — and that question
    cannot be answered from the queue at all, because both terminal successes
    close the issue (§9.3, §9.16). A dependency that succeeded is therefore
    always missing from the queue, which is why reading dependencies out of the
    queue rejected every satisfied dependency as unmet (#106, #107).

    Resolution is lazy and cached: a story with no dependency outside the queue
    costs no request, and a dependency named by several stories costs one.
    Unreadable is not satisfied — a fetch that fails resolves to `None`, which
    `dependency_satisfied` reports as unmet, so a transport failure delays work
    instead of authorizing it.
    """

    def __init__(self, open_issues: dict, fetch=None):
        self._open = open_issues
        self._fetch = fetch
        self._resolved: dict[int, dict | None] = {}

    def get(self, number: int) -> dict | None:
        if number in self._open:
            return self._open[number]
        if number not in self._resolved:
            self._resolved[number] = self._load(number)
        return self._resolved[number]

    def _load(self, number: int) -> dict | None:
        if self._fetch is None:
            return None
        try:
            return self._fetch(number)
        except Exception:  # noqa: BLE001 — unreadable state is unmet, never satisfied
            return None


def dependency_satisfied(issue: dict | None) -> tuple[bool, str]:
    """Did the issue a story depends on reach a trusted terminal success?

    Every rejection names itself, because "#104 unmet" with no reason is exactly
    what made the live defect hard to see. The trust boundary applies here for
    the same reason it applies to the story itself (§9.9): a dependency is state
    that authorizes work, so a stranger's issue claiming to be `story:merged`
    must not unblock anything.
    """
    if issue is None:
        return False, "no such issue, or it could not be read"
    if "type:story" not in labels_of(issue):
        return False, "not a story"
    if not is_trusted(issue):
        return False, f"untrusted author_association={issue.get('author_association')}"
    lifecycle = lifecycle_of(issue, STORY_LIFECYCLE)
    if lifecycle is None:
        return False, "zero or multiple story:* labels"
    if lifecycle not in TERMINAL_SUCCESS:
        return False, lifecycle
    return True, lifecycle


def evaluate_story(story: dict, projects: dict, stories: dict,
                   commitment: int,
                   dependencies: DependencyIndex | None = None) -> Decision:
    """Decide whether one story is authorized and eligible for dispatch.

    Order matters only for the quality of the reported reason; every check is
    independent and any failure means no dispatch.

    `dependencies` resolves `### Depends-on` references; it defaults to the
    dispatch queue alone, which can only answer for issues that are still open.
    Callers reading live GitHub pass an index that can also reach closed issues
    (#107) — see `DependencyIndex`.
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
    if lifecycle == COMPLETED:
        return Decision(number, False, Reason.COMPLETED,
                        detail="terminal: the bounded assignment succeeded and required "
                               "no deliverable (§9.16)")
    if lifecycle == CANCELLED:
        return Decision(number, False, Reason.CANCELLED,
                        detail="terminal: work deliberately stopped (§9.3)")
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
    index = dependencies if dependencies is not None else DependencyIndex(stories)
    for dep in deps:
        satisfied, why = dependency_satisfied(index.get(dep))
        if not satisfied:
            return Decision(number, False, Reason.DEPENDENCY_UNMET, project=project_number,
                            detail=f"#{dep} ({why})")

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
                  wip_limit: int = WIP_LIMIT,
                  dependencies: DependencyIndex | None = None) -> Plan:
    """Evaluate every story, then select deterministically (§9.10).

    `stories` is the dispatch queue and the only source of candidates. A
    `dependencies` index widens what a *dependency reference* can see without
    widening that queue: it is read, never iterated, so an issue it resolves can
    never be selected.
    """
    plan = Plan(capacity=wip_limit)

    plan.wip_in_use = sum(
        1 for s in stories.values()
        if lifecycle_of(s, STORY_LIFECYCLE) == CLAIMED
        and (s.get("state") or "OPEN").upper() == "OPEN"
    )

    for number in sorted(stories):
        plan.decisions.append(
            evaluate_story(stories[number], projects, stories, commitment, dependencies))

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


def fetch_pages(url: str, token: str) -> list[dict]:
    items, page = [], 1
    while True:
        separator = "&" if "?" in url else "?"
        batch = _api(f"{url}{separator}per_page=100&page={page}", token)
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def fetch_timeline(repo: str, number: int, token: str) -> list[dict]:
    return fetch_pages(f"https://api.github.com/repos/{repo}/issues/{number}/timeline", token)


def fetch_pull_requests(repo: str, token: str) -> list[dict]:
    return fetch_pages(f"https://api.github.com/repos/{repo}/pulls?state=all", token)


def apply_recovery(repo: str, story: dict, decision: RecoveryDecision,
                   token: str) -> tuple[bool, str]:
    """Apply a recovery only if the claim is still authoritative."""
    if decision.action not in {"ready", "merged", "poison"}:
        return False, f"{decision.reason}: {decision.detail}".rstrip()
    note_suffix = ""
    fresh = fetch_issue(repo, story["number"], token)
    if fresh is None or lifecycle_of(fresh, STORY_LIFECYCLE) != CLAIMED:
        return False, "STALE_RECOVERY_PLAN: story is no longer story:claimed"

    labels = labels_of(fresh) - {CLAIMED}
    payload: dict = {}
    if decision.action == "ready":
        attempt_raw = (merge_gate.parse_section(fresh.get("body") or "", "Attempt") or "").strip()
        if not attempt_raw.isdigit() or int(attempt_raw) < 1:
            return False, f"RECOVERY_ATTEMPT_INVALID: Attempt={attempt_raw!r}"
        body = (fresh.get("body") or "").replace("\r\n", "\n")
        payload["body"] = re.sub(
            r"(### Attempt\n\n)(\d+)(\n)",
            lambda match: match.group(1) + str(int(attempt_raw) - 1) + match.group(3),
            body, count=1)
        labels.add(READY)
    elif decision.action == "poison":
        # §9.4.1 — the recovery budget is spent. Route to the human queue rather
        # than recovering again; `Attempt` is left exactly as it is, because the
        # question a human must answer is why no PR ever appeared, not how many
        # attempts remain.
        labels.add(POISON)
        close, why = poison_closure(fetch_timeline(repo, story["number"], token))
        if close:
            payload.update({"state": "closed", "state_reason": "not_planned"})
        note_suffix = f"; {why}"
    else:
        labels.add(MERGED)
        payload.update({"state": "closed", "state_reason": "completed"})
    payload["labels"] = sorted(labels)
    _api(f"https://api.github.com/repos/{repo}/issues/{story['number']}", token,
         method="PATCH", payload=payload)
    return True, f"{decision.reason}: {decision.detail}{note_suffix}".rstrip()


def poison(repo: str, story: dict, token: str) -> tuple[bool, str]:
    """§4.3.5: the fourth dispatch opportunity does not dispatch; it poisons.

    The story is **not** closed here unless §4.3.7's rescue cap is spent. A
    poisoned story is waiting for a human, and §9.3 keeps waiting artifacts open
    — which is also what makes it visible to the human queue, and what makes the
    §4.3.6 rescue reachable without a human first reopening the issue.
    """
    fresh = fetch_issue(repo, story["number"], token)
    if fresh is None or lifecycle_of(fresh, STORY_LIFECYCLE) != READY:
        return False, "story no longer ready"
    attempt_raw = (merge_gate.parse_section(fresh.get("body") or "", "Attempt") or "").strip()
    if attempt_raw != str(ATTEMPT_MAX):
        return False, f"Attempt changed to {attempt_raw!r}"

    close, why = poison_closure(fetch_timeline(repo, story["number"], token))
    payload: dict = {"labels": sorted((labels_of(fresh) - {READY}) | {POISON})}
    if close:
        payload.update({"state": "closed", "state_reason": "not_planned"})
    _api(f"https://api.github.com/repos/{repo}/issues/{story['number']}", token,
         method="PATCH", payload=payload)
    return True, f"ATTEMPT_EXHAUSTED: Attempt remains {ATTEMPT_MAX}; {why}"


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
        # `dependencies` holds issues that are not in the queue — closed ones,
        # typically — exactly as the live path resolves them one at a time.
        closed = {int(k): v for k, v in data.get("dependencies", {}).items()}
        plan = plan_dispatch(stories, projects, data["commitment"],
                             data.get("wip_limit", args.wip_limit),
                             DependencyIndex(stories, fetch=closed.get))
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

    # §9.4: recovery precedes WIP accounting and selection. The mutated issue
    # snapshot is fetched again so capacity is calculated from durable state.
    recovery_notes: list[str] = []
    if args.claim:
        timelines = {
            number: fetch_timeline(args.repo, number, token)
            for number, story in stories.items()
            if lifecycle_of(story, STORY_LIFECYCLE) == CLAIMED
        }
        recoveries = reconcile_claims(
            stories, timelines, fetch_pull_requests(args.repo, token), datetime.now(timezone.utc))
        for decision in recoveries:
            if decision.action in {"ready", "merged", "poison"}:
                ok, note = apply_recovery(args.repo, stories[decision.number], decision, token)
                recovery_notes.append(f"RECOVERY #{decision.number}: {'APPLIED' if ok else 'SKIP'} {note}")
            else:
                recovery_notes.append(
                    f"RECOVERY #{decision.number}: {decision.action.upper()} {decision.reason} {decision.detail}".rstrip())
        issues = fetch_issues(args.repo, token)
        stories = {n: i for n, i in issues.items() if "type:story" in labels_of(i)}
        projects = {n: i for n, i in issues.items() if "type:project" in labels_of(i)}

    # §9.3 keeps the queue to open issues; a dependency question is not a queue
    # question. Resolution reads whatever issue a story names, closed included,
    # and nothing it reads is added to `stories` (#107).
    dependencies = DependencyIndex(
        issues, fetch=lambda number: fetch_issue(args.repo, number, token))
    plan = plan_dispatch(stories, projects, args.commitment, args.wip_limit, dependencies)

    claimed: list[tuple[int, str]] = []
    dispatched: list[Decision] = []
    if args.claim:
        for decision in plan.decisions:
            if decision.reason == Reason.ATTEMPT_EXHAUSTED:
                ok, note = poison(args.repo, stories[decision.number], token)
                recovery_notes.append(f"POISON #{decision.number}: {'APPLIED' if ok else 'SKIP'} {note}")
        for decision in plan.selected:
            ok, note = claim(args.repo, stories[decision.number], token)
            claimed.append((decision.number, note if ok else f"NOT claimed — {note}"))
            if ok:
                dispatched.append(decision)

    if recovery_notes:
        print("\n".join(recovery_notes))
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
