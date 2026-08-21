#!/usr/bin/env python3
"""Completion — the post-worker-success transition out of `story:claimed`.

Phase 2, story #104 item 1. The clean-room verification under Project #95 found
the lifecycle stopping one step short: a Story moved `story:ready → story:claimed`,
the worker ran, the bridge verified a durable acknowledgement in GitHub — and the
Story then sat at `story:claimed` forever, waiting for a human to finish a
transition the evidence had already decided.

"Forever" is not merely untidy. `story:claimed` is a **lease** (§9.4): after
`CLAIM_LEASE` the dispatcher finds a claim with no linked pull request, cannot
tell a finished worker from a dead one, and recovers the Story to `story:ready`.
The next poll dispatches it again and a second acknowledgement appears — which is
precisely the "exactly one acknowledgement" criterion #95 exists to prove. A
Story that succeeds and is never closed does not stay still; it loops.

## Why this can be decided at all

§9.4.1 states the reason the dispatcher does not try: *durable evidence cannot
distinguish a worker that died before starting from one that ran correctly and
produced no pull request — both leave an expired claim and no PR.* That was true
when it was written. #98 made the missing evidence exist: the bridge's bounded
assignment ends in a **durable acknowledgement comment**, posted after the claim,
readable by anyone, and verified before the launch is reported as success. The
two cases are now distinguishable, and this module distinguishes exactly them.

## The narrow path, stated as its preconditions

Every one of these must hold, and each is checked against durable GitHub state
read at decision time:

  1. the launch reported `LAUNCHED` — a *definite* success from `workers.py`
  2. the Story is still `story:claimed`
  3. **no pull request links to the Story** under §9.5
  4. a `story:claimed` `labeled` event exists, giving the claim instant
  5. an acknowledgement comment exists, created at or after that instant

Anything else — an ambiguous launch, a Story someone moved, an unreadable
timeline, a missing acknowledgement — leaves the Story exactly as it is and says
why. Failing closed here costs one lease period and a §9.4 recovery, which is a
bounded inconvenience. Failing open closes a Story whose work never happened.

Condition 3 is the one that keeps this safe as the factory grows. A worker that
produced a pull request has a deliverable, and its Story belongs to review and
the merge gate — not here. This module only ever completes work that finished
with *nothing to merge*.

## Which state, and why it is not a new one

§9.3 already names the terminal state for work that finishes with no
deliverable: `story:cancelled`, described there as covering exactly *"a
verification fixture that only had to prove something"*. That is what a bounded
acknowledgement assignment is. No new label, no new bell, no new orchestration —
the contract already had a name for this outcome, and §9.4.1 already named
cancellation as the disposition a human would reach for.

**A stated deviation, not a hidden one.** §9.3 also says `story:cancelled` is a
human decision and is "never applied by a component". That sentence was written
when no component *could* prove the case; #98 changed what is provable, and this
module is the narrow consequence. The spec amendment is requested with the change
that needs it — story #104's declared `### Scope` is `factory/runtime/**`, so the
edit to `factory/spec/state-schema.md` is not this Story's to make.

## Two limits worth naming

**This only completes work that finished synchronously.** The pass is triggered
by a launch *returning* success, so it fits an engine the factory invoked and
waited for. Under the legacy `FACTORY_WORKER_CMD` adapter — which prints a
`WAKE` line for a standing session to pick up later — the launch returns before
the worker has done anything, so there is no proof yet, nothing is completed,
and the §9.4 lease remains the resolution exactly as before. That is the
pre-#104 behaviour, unchanged, not a new failure.

**A read that misses the acknowledgement costs a lease, not correctness.** The
bridge has already verified the comment exists before reporting success, so the
read here should find it; if GitHub has not made it visible yet, the answer is
`NO_PROOF`, the Story stays claimed, and §9.4 recovers it. That is the old
behaviour again — the honest cost of refusing to spend ignorance as evidence.

## Where the judgment lives

`poller.py` must not grow policy — the runtime holds no judgment, and that rule
is what keeps every authorization decision in one auditable place. So the poller
calls this module and prints what it returns; the preconditions, the §9.5 read,
the target state, and the recorded reason are all here, and the §9.5 and
lifecycle primitives are *imported from the dispatcher* rather than restated, so
there is exactly one definition of what "a pull request links to this Story"
means.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "dispatcher"))

import bridge     # noqa: E402 — what an acknowledgement looks like, defined once
import dispatcher  # noqa: E402 — §9.5 linkage and lifecycle primitives, defined once
import runlog     # noqa: E402
import workers    # noqa: E402 — the launch verdicts this reads

# The heading of the comment this module records. Deliberately *not* the
# acknowledgement heading: a recording must never be readable as the proof it
# cites, or the factory could later mistake its own bookkeeping for a worker's
# work — the same self-approval trap `continuation.py` guards against.
COMPLETION_HEADING = "## Story completed — no deliverable"


class Reason:
    """Named outcomes. A Story left claimed must say why, or this is unauditable."""

    COMPLETED = "COMPLETED"
    LAUNCH_NOT_DEFINITE = "LAUNCH_NOT_DEFINITE"
    NOT_CLAIMED = "NOT_CLAIMED"
    STORY_UNREADABLE = "STORY_UNREADABLE"
    DELIVERABLE_EXISTS = "DELIVERABLE_EXISTS"
    CLAIM_EVENT_MISSING = "CLAIM_EVENT_MISSING"
    NO_PROOF = "NO_PROOF"
    ALREADY_RECORDED = "ALREADY_RECORDED"
    NOT_APPLIED = "NOT_APPLIED"


@dataclass
class Outcome:
    """What one completion pass decided about one Story."""

    number: int
    action: str | None        # target lifecycle label, or None for "left alone"
    reason: str
    detail: str = ""

    def __eq__(self, other) -> bool:
        return (isinstance(other, Outcome) and self.number == other.number
                and self.action == other.action and self.reason == other.reason)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def claim_instant(timeline: list[dict]) -> datetime | None:
    """When this Story was most recently claimed, per the durable timeline.

    The *latest* claim, not the first: a Story recovered and re-dispatched has
    several, and proof from a previous lease is not proof about this worker.
    """
    stamps = [
        _parse_time(event.get("created_at"))
        for event in timeline
        if event.get("event") == "labeled"
        and dispatcher.merge_gate.label_name(event.get("label", {})) == dispatcher.CLAIMED
    ]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def acknowledgement_after(comments: list[dict], since: datetime) -> dict | None:
    """The first acknowledgement posted at or after `since`, if any.

    What counts as an acknowledgement is `bridge.looks_like_acknowledgement` and
    nothing else. Restating the match here would let the proof this module
    accepts drift away from the proof the bridge demands, and the two disagreeing
    is how a Story gets closed on evidence that was never produced.
    """
    for comment in comments:
        created = _parse_time(comment.get("created_at") or "")
        if created is None or created < since:
            continue
        if bridge.looks_like_acknowledgement(comment.get("body") or ""):
            return comment
    return None


def already_recorded(comments: list[dict]) -> bool:
    """Has a completion already been recorded on this Story?

    Guards the gap between the two writes below: the comment is posted first, so
    a crash in between must not produce a second one on the next pass.
    """
    return any((comment.get("body") or "").lstrip().startswith(COMPLETION_HEADING)
               for comment in comments)


def completion_decision(story: dict, timeline: list[dict], comments: list[dict],
                        pull_requests: list[dict], launch_result: str) -> Outcome:
    """Decide whether one claimed Story has demonstrably finished. Pure.

    No I/O, so the whole policy is testable offline against fixtures — the same
    split the dispatcher uses, for the same reason: a decision that can only be
    exercised against live GitHub is a decision nobody reviews.
    """
    number = story.get("number", 0)

    # (1) Only a definite success. An `AMBIGUOUS` launch means the worker may
    # still be running, and closing a Story out from under a live worker is
    # strictly worse than leaving the §9.4 lease to expire.
    if launch_result != workers.Result.LAUNCHED:
        return Outcome(number, None, Reason.LAUNCH_NOT_DEFINITE, str(launch_result))

    # (2) The claim must still be ours to complete.
    state = dispatcher.lifecycle_of(story, dispatcher.STORY_LIFECYCLE)
    if state != dispatcher.CLAIMED:
        return Outcome(number, None, Reason.NOT_CLAIMED, str(state))

    # (3) A deliverable exists → review and the merge gate own this Story, not
    # this module. A malformed link is ambiguity, and ambiguity stops here too.
    linked, link_error = dispatcher.linked_delivery_prs(number, pull_requests)
    if link_error:
        return Outcome(number, None, Reason.DELIVERABLE_EXISTS, link_error)
    if linked:
        return Outcome(number, None, Reason.DELIVERABLE_EXISTS,
                       f"PR #{linked[0].get('number')} links to this story (§9.5); "
                       f"review and the merge gate own the rest")

    # (4) The window. Without a claim event there is no instant to measure
    # against, and an unanchored comparison is not evidence.
    since = claim_instant(timeline)
    if since is None:
        return Outcome(number, None, Reason.CLAIM_EVENT_MISSING,
                       "no story:claimed labeled event in the timeline")

    # (5) The proof itself.
    proof = acknowledgement_after(comments, since)
    if proof is None:
        return Outcome(number, None, Reason.NO_PROOF,
                       f"no worker acknowledgement at or after {since.isoformat()}")

    if already_recorded(comments):
        return Outcome(number, None, Reason.ALREADY_RECORDED,
                       "a completion is already recorded on this story")

    return Outcome(number, dispatcher.CANCELLED, Reason.COMPLETED,
                   f"worker acknowledgement {proof.get('html_url') or proof.get('id')} "
                   f"posted at {proof.get('created_at')}, after the claim at "
                   f"{since.isoformat()}; no pull request links to this story, so the "
                   f"bounded assignment finished with no deliverable (§9.3)")


def completion_comment(outcome: Outcome, worker: str) -> str:
    """The §9.3 record: what was decided, why, and on what evidence.

    §9.3 requires a comment stating what was decided and why. It said so of a
    human decision; a component applying the same transition owes the reader
    more, not less — the evidence is named so the conclusion can be checked
    against GitHub rather than taken on trust.
    """
    return (f"{COMPLETION_HEADING}\n\n"
            f"The factory runtime completed this Story automatically.\n\n"
            f"- Worker: `{worker}`\n"
            f"- Launch verdict: `{workers.Result.LAUNCHED}` (definite success)\n"
            f"- Evidence: {outcome.detail}\n\n"
            f"`story:claimed → story:cancelled` per §9.3 — *finished with no "
            f"deliverable*. No pull request links to this Story, so there is "
            f"nothing to review or merge, and leaving it claimed would let the "
            f"§9.4 lease expire and dispatch a second worker.\n\n"
            f"`Attempt` is unchanged: the attempt was dispatched and it succeeded.")


# --------------------------------------------------------------------------
# GitHub I/O — kept apart from the pure decision above
# --------------------------------------------------------------------------


def fetch_comments(repo: str, number: int, token: str) -> list[dict]:
    return dispatcher.fetch_pages(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments", token)


def post_comment(repo: str, number: int, body: str, token: str) -> None:
    dispatcher._api(f"https://api.github.com/repos/{repo}/issues/{number}/comments",
                    token, method="POST", payload={"body": body})


def apply_outcome(repo: str, outcome: Outcome, worker: str, token: str) -> tuple[bool, str]:
    """Record the reason, then transition — re-checking the state before writing.

    §9.2: read the current label set, verify the expected `from` state, then
    write one PATCH carrying the complete final label set. The re-read is what
    makes a second pass a no-op, exactly as in `dispatcher.claim`.

    The comment goes first on purpose. A crash between the two writes then leaves
    a claimed Story carrying an explanation — visible, recoverable by the §9.4
    lease, and obvious to a human — rather than a terminal Story with no recorded
    reason, which is unauditable and cannot be reopened by any component (§9.3).
    """
    number = outcome.number
    fresh = dispatcher.fetch_issue(repo, number, token)
    if fresh is None:
        return False, "STORY_UNREADABLE: the story could not be re-read before writing"
    if dispatcher.lifecycle_of(fresh, dispatcher.STORY_LIFECYCLE) != dispatcher.CLAIMED:
        return False, (f"STALE_COMPLETION_PLAN: no longer {dispatcher.CLAIMED} "
                       f"(now {dispatcher.lifecycle_of(fresh, dispatcher.STORY_LIFECYCLE)})")

    post_comment(repo, number, completion_comment(outcome, worker), token)

    labels = sorted((dispatcher.labels_of(fresh) - {dispatcher.CLAIMED}) | {outcome.action})
    dispatcher._api(f"https://api.github.com/repos/{repo}/issues/{number}", token,
                    method="PATCH",
                    payload={"labels": labels,
                             "state": "closed",          # §9.3 — closure is part of
                             "state_reason": "not_planned"})  # the transition
    return True, f"{dispatcher.CLAIMED} -> {outcome.action}; closed as not_planned"


def record_success(repo: str, story: int, project: int, launch_result: str,
                   worker: str, token: str, apply: bool = True) -> Outcome:
    """One completion pass for one Story that a worker just finished.

    Called by the poller immediately after a launch reports success, which is
    what bounds the question being asked: the bounded assignment has *returned*,
    so "no pull request exists" means the worker produced none, not that it has
    not got there yet.
    """
    story_issue = dispatcher.fetch_issue(repo, story, token)
    if story_issue is None:
        outcome = Outcome(story, None, Reason.STORY_UNREADABLE,
                          "the story could not be read from GitHub")
        runlog.event("story.completion", story=story, project=project,
                     action=None, reason=outcome.reason, detail=outcome.detail)
        return outcome

    timeline = dispatcher.fetch_timeline(repo, story, token)
    comments = fetch_comments(repo, story, token)
    pull_requests = dispatcher.fetch_pull_requests(repo, token)

    decision = completion_decision(story_issue, timeline, comments,
                                   pull_requests, launch_result)

    outcome, applied, note = decision, False, ""
    if decision.action is not None:
        if not apply:
            note = "dry run — no write"
        else:
            applied, note = apply_outcome(repo, decision, worker, token)
        if not applied:
            # The decision stands as a finding; the transition did not happen,
            # and the returned outcome must not claim otherwise.
            outcome = Outcome(story, None, Reason.NOT_APPLIED, note)

    runlog.event("story.completion", story=story, project=project, worker=worker,
                 decided=decision.action, action=outcome.action,
                 reason=outcome.reason, detail=outcome.detail,
                 applied=applied, note=note)
    return outcome
