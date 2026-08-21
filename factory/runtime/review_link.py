#!/usr/bin/env python3
"""Review link — the transitions a delivery pull request causes.

Phase 2 Closeout (#111). Two routes §4.2 has always specified and nothing has
ever performed:

    story:claimed   -> story:in-review    a §9.5-linked PR is open
    story:in-review -> story:merged       that PR merged; the issue closes

Until now a human moved both labels on every story the factory delivered. That
is the relay Phase 2 exists to delete, and it was not hypothetical: #97 sat open
at `story:in-review` for four days with its delivery PR #98 merged, because the
second route had no implementation to run. #107 and #110 were in the same
position when this module was written.

§9.11 listed `story:in-review → story:merged` as documentation-only, "gated on
§9.13". §9.13 is complete — `merge-gate` is a required check and approvals are
at zero — so the gate that deferred this route has been satisfied.

**What this module is not.** It is not a sweep that decides stories are done.
The only thing it reads is whether a pull request carrying the canonical
`Story: #N` line is open or merged, and GitHub's merge state is not something a
worker asserts: it is the outcome of the required gate the worker's own
credential cannot influence (§9.14). That is the whole reason these two
transitions may be mechanical while `story:completed` needs §9.16's much
narrower proof — there, nothing outside the factory had ruled on the work.

**One writer per transition.** A `story:claimed` story whose linked PR has
*already merged* is deliberately left alone here: the dispatcher's §9.4 recovery
pass already reconciles exactly that case (`MERGED_DELIVERY_PR`), and two
components writing one transition is how a lifecycle stops being auditable. This
module names that owner rather than racing it.

Everything ambiguous fails closed with a named reason and no write — two linked
PRs, a duplicate `Story:` line, a PR closed without merging. The last of those
is not an error the factory can resolve: work was delivered and then rejected,
and what happens next is a human's decision.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "dispatcher"))
sys.path.insert(0, os.path.join(HERE, "..", "gates"))

import dispatcher  # noqa: E402 — §9.5 linkage and the lifecycle vocabulary, defined once
import runlog  # noqa: E402

RECONCILABLE = (dispatcher.CLAIMED, dispatcher.IN_REVIEW)


class Reason:
    """Named outcomes. A story left alone must say why, or the pass is unauditable."""

    OPENED = "PR_OPENED"
    MERGED = "PR_MERGED"
    NOT_RECONCILABLE = "NOT_RECONCILABLE"
    NO_LINKED_PR = "NO_LINKED_PR"
    INVALID_PR_LINK = "INVALID_PR_LINK"
    AMBIGUOUS_LINKED_PRS = "AMBIGUOUS_LINKED_PRS"
    PR_STILL_OPEN = "PR_STILL_OPEN"
    PR_CLOSED_UNMERGED = "PR_CLOSED_UNMERGED"
    RECOVERY_OWNS_THIS = "RECOVERY_OWNS_THIS"
    STALE_PLAN = "STALE_PLAN"


@dataclass(frozen=True)
class Outcome:
    number: int
    action: str | None      # target lifecycle label, or None
    reason: str
    detail: str = ""
    pr: int | None = None


def is_merged(pull_request: dict) -> bool:
    """GitHub reports a merge two ways depending on the endpoint. Both mean merged."""
    return bool(pull_request.get("merged_at") or pull_request.get("merged") is True)


def is_closed(pull_request: dict) -> bool:
    return (pull_request.get("state") or "").lower() == "closed"


def reconcile(story: dict, pull_requests: list[dict]) -> Outcome:
    """Decide what one story's delivery pull request implies. Pure — no I/O.

    The whole policy is exercisable offline against fixtures, the same split the
    dispatcher and `completion.py` use, and for the same reason: a decision that
    can only be run against live GitHub is a decision nobody reviews.
    """
    number = story.get("number", 0)

    state = dispatcher.lifecycle_of(story, dispatcher.STORY_LIFECYCLE)
    if state not in RECONCILABLE:
        return Outcome(number, None, Reason.NOT_RECONCILABLE, str(state))

    linked, link_error = dispatcher.linked_delivery_prs(number, pull_requests)
    if link_error:
        return Outcome(number, None, Reason.INVALID_PR_LINK, link_error)
    if len(linked) > 1:
        return Outcome(number, None, Reason.AMBIGUOUS_LINKED_PRS,
                       f"{len(linked)} pull requests carry `Story: #{number}`; "
                       f"which one delivers this story is a human's call")
    if not linked:
        # A claimed story with no deliverable is §9.4's (lease) or §9.16's
        # (completion) business, and an in-review story with no PR is a state
        # nothing here can explain. Either way: not this module's transition.
        return Outcome(number, None, Reason.NO_LINKED_PR,
                       f"no pull request carries `Story: #{number}` (§9.5)")

    pull_request = linked[0]
    pr_number = pull_request.get("number")

    if is_merged(pull_request):
        if state == dispatcher.CLAIMED:
            # §9.4 already reconciles a merged delivery on a claimed story. One
            # writer per transition — this one names the owner and stands down.
            return Outcome(number, None, Reason.RECOVERY_OWNS_THIS,
                           f"PR #{pr_number} merged while the story is still "
                           f"`{dispatcher.CLAIMED}`; the dispatcher's §9.4 recovery "
                           f"pass reconciles that case (MERGED_DELIVERY_PR)",
                           pr=pr_number)
        return Outcome(number, dispatcher.MERGED, Reason.MERGED,
                       f"PR #{pr_number} merged at {pull_request.get('merged_at')}; "
                       f"the required merge gate passed on it, which is a verdict "
                       f"the delivering credential cannot fabricate (§9.14)",
                       pr=pr_number)

    if is_closed(pull_request):
        # Delivered and rejected. The factory has no rule for what should happen
        # next, and inventing one here would be inventing policy.
        return Outcome(number, None, Reason.PR_CLOSED_UNMERGED,
                       f"PR #{pr_number} was closed without merging; what happens "
                       f"to this story is a human's decision", pr=pr_number)

    if state == dispatcher.CLAIMED:
        return Outcome(number, dispatcher.IN_REVIEW, Reason.OPENED,
                       f"PR #{pr_number} is open and carries `Story: #{number}` (§9.5)",
                       pr=pr_number)

    return Outcome(number, None, Reason.PR_STILL_OPEN,
                   f"PR #{pr_number} is open; review and the merge gate own it",
                   pr=pr_number)


def reconcile_all(stories: dict, pull_requests: list[dict]) -> list[Outcome]:
    """Every reconcilable story, in issue order. Deterministic, like §9.10."""
    return [reconcile(stories[number], pull_requests) for number in sorted(stories)]


# --------------------------------------------------------------------------
# GitHub I/O — kept apart from the decision above
# --------------------------------------------------------------------------


def apply_outcome(repo: str, outcome: Outcome, token: str) -> tuple[bool, str]:
    """Write the transition, re-reading the story immediately before the write.

    Idempotent by re-reading: a second pass finds the story already moved and
    declines. The state write is the duplicate suppressor (§9.10) — no lock, no
    cursor, nothing outside GitHub.
    """
    if outcome.action is None:
        return False, f"{outcome.reason}: {outcome.detail}".rstrip()

    fresh = dispatcher.fetch_issue(repo, outcome.number, token)
    if fresh is None:
        return False, f"{Reason.STALE_PLAN}: story #{outcome.number} vanished"
    current = dispatcher.lifecycle_of(fresh, dispatcher.STORY_LIFECYCLE)
    expected = dispatcher.CLAIMED if outcome.action == dispatcher.IN_REVIEW else dispatcher.IN_REVIEW
    if current != expected:
        return False, (f"{Reason.STALE_PLAN}: expected `{expected}` at write time, "
                       f"found `{current}` — another pass or a human moved it")

    # §9.2 — one PATCH carrying the complete final label set, never add-then-remove.
    labels = sorted((dispatcher.labels_of(fresh) - {expected}) | {outcome.action})
    payload: dict = {"labels": labels}
    if outcome.action == dispatcher.MERGED:
        payload.update({"state": "closed", "state_reason": "completed"})  # §9.3

    dispatcher._api(f"https://api.github.com/repos/{repo}/issues/{outcome.number}",
                    token, method="PATCH", payload=payload)
    return True, f"{expected} -> {outcome.action}"


def fetch_stories(repo: str, token: str) -> dict:
    """Open stories in a reconcilable state. The queue is open issues (§9.3)."""
    issues = dispatcher.fetch_issues(repo, token)
    return {
        number: issue for number, issue in issues.items()
        if "type:story" in dispatcher.labels_of(issue)
        and dispatcher.lifecycle_of(issue, dispatcher.STORY_LIFECYCLE) in RECONCILABLE
    }


def run(repo: str, token: str, apply: bool = True) -> list[Outcome]:
    """One reconciliation pass. Returns the outcomes that moved a story."""
    stories = fetch_stories(repo, token)
    if not stories:
        return []
    pull_requests = dispatcher.fetch_pull_requests(repo, token)

    moved = []
    for outcome in reconcile_all(stories, pull_requests):
        if outcome.action is None:
            # Silence only for the states that are somebody else's business by
            # design. Everything else names itself — §9.11's "no silent drops"
            # is about routes, and a reconciliation that quietly declines is the
            # same failure wearing a different hat.
            if outcome.reason not in (Reason.NOT_RECONCILABLE, Reason.NO_LINKED_PR,
                                      Reason.PR_STILL_OPEN):
                print(f"[review-link] #{outcome.number} not reconciled: "
                      f"{outcome.reason} ({outcome.detail})", flush=True)
                runlog.event("review_link.declined", story=outcome.number,
                             reason=outcome.reason, pr=outcome.pr, detail=outcome.detail)
            continue

        if not apply:
            print(f"[review-link] would move #{outcome.number} to {outcome.action} "
                  f"({outcome.reason})", flush=True)
            moved.append(outcome)
            continue

        ok, note = apply_outcome(repo, outcome, token)
        if ok:
            print(f"[review-link] #{outcome.number}: {note} ({outcome.reason}) "
                  f"— {outcome.detail}", flush=True)
            runlog.event("review_link.transition", story=outcome.number,
                         to=outcome.action, reason=outcome.reason, pr=outcome.pr)
            moved.append(outcome)
        else:
            print(f"[review-link] #{outcome.number} skipped: {note}", flush=True)
            runlog.event("review_link.skipped", story=outcome.number, detail=note)
    return moved
