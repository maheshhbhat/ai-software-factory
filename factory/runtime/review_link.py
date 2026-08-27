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

If a linked PR opens and merges between two reconciliation intervals, this pass
still records both legal lifecycle edges: first `claimed → in-review`, then
`in-review → merged` on the next pass. It never compresses those observations
into the illegal `claimed → merged` edge seen on Stories #214 and #215.

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
import observability as obs  # noqa: E402
from review_route import outcomes as review_outcomes  # noqa: E402

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
    CORRECTION_IN_PROGRESS = "CORRECTION_IN_PROGRESS"
    PR_CLOSED_UNMERGED = "PR_CLOSED_UNMERGED"
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


def reconcile(story: dict, pull_requests: list[dict],
              story_comments: list[dict] | None = None) -> Outcome:
    """Decide what one story's delivery pull request implies. Pure — no I/O.

    The whole policy is exercisable offline against fixtures, the same split the
    dispatcher and `completion.py` use, and for the same reason: a decision that
    can only be run against live GitHub is a decision nobody reviews.

    `story_comments`, when supplied, lets the claimed→in-review edge tell a
    fresh delivery from a redispatched correction: a claimed story whose linked
    PR's *current* head already carries a findings outcome is mid-correction —
    the worker was just re-dispatched to fix that exact head — and moving it to
    `story:in-review` again truncates the attempt (it reclaimed #328 sixty-two
    seconds after its claim on 2026-08-23). The hold releases itself: the
    correction pushes a new head, the marker no longer matches, and the move
    proceeds. With `story_comments=None` the behaviour is exactly the old one —
    fail-open to the status quo, never a stuck story.
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
            return Outcome(number, dispatcher.IN_REVIEW, Reason.OPENED,
                           f"PR #{pr_number} opened and merged between reconciliation "
                           f"intervals; record `{dispatcher.IN_REVIEW}` before the next "
                           f"pass records `{dispatcher.MERGED}`", pr=pr_number)
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
        head = (pull_request.get("head") or {}).get("sha", "")
        if story_comments is not None and head and \
                "findings" in review_outcomes(story_comments, pr_number, head):
            return Outcome(number, None, Reason.CORRECTION_IN_PROGRESS,
                           f"PR #{pr_number} head {head[:8]} was already reviewed "
                           f"with findings; this claim is a redispatched "
                           f"correction — hold until the head changes",
                           pr=pr_number)
        return Outcome(number, dispatcher.IN_REVIEW, Reason.OPENED,
                       f"PR #{pr_number} is open and carries `Story: #{number}` (§9.5)",
                       pr=pr_number)

    return Outcome(number, None, Reason.PR_STILL_OPEN,
                   f"PR #{pr_number} is open; review and the merge gate own it",
                   pr=pr_number)


def reconcile_all(stories: dict, pull_requests: list[dict],
                  comments_by_story: dict[int, list[dict]] | None = None) -> list[Outcome]:
    """Every reconcilable story, in issue order. Deterministic, like §9.10."""
    supplied = comments_by_story or {}
    return [reconcile(stories[number], pull_requests, supplied.get(number))
            for number in sorted(stories)]


def exact_head_approved(pull_request: dict, story_comments: list[dict]) -> bool:
    """Advisory routing prerequisite, never a deterministic gate input."""
    head = (pull_request.get("head") or {}).get("sha", "")
    values = review_outcomes(story_comments, pull_request["number"], head)
    if len(values) > 1:
        raise RuntimeError("duplicate exact-head review outcomes fail closed")
    return values == ["approval"]


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


def authorized_story_numbers(issues: dict[int, dict], commitment: int) -> set[int]:
    """Stories whose canonical Project chain reaches ``commitment``.

    The dispatcher already defines the strict canonical-section parser.  Reuse
    it here so review and delivery agree about authorization.  Missing,
    duplicate, or malformed links return an error from ``section_ref`` and are
    excluded; scope ambiguity must never broaden what a poller may touch.
    """
    def unique_ref(issue: dict, section: str) -> tuple[int | None, str | None]:
        body = issue.get("body") or ""
        heading = f"### {section}"
        if sum(line.strip() == heading for line in body.splitlines()) != 1:
            return None, dispatcher.Reason.PROJECT_LINK_MALFORMED
        return dispatcher.section_ref(body, section)

    projects = {
        number for number, issue in issues.items()
        if "type:project" in dispatcher.labels_of(issue)
        and unique_ref(issue, "Roadmap commitment") == (commitment, None)
    }
    authorized = set()
    for number, issue in issues.items():
        if "type:story" not in dispatcher.labels_of(issue):
            continue
        project, error = unique_ref(issue, "Project")
        if error is None and project in projects:
            authorized.add(number)
    return authorized


def run(repo: str, token: str, apply: bool = True,
        commitment: int | None = None) -> list[Outcome]:
    """One reconciliation pass. Returns the outcomes that moved a story."""
    issues = dispatcher.fetch_issues(repo, token)
    allowed = (authorized_story_numbers(issues, commitment)
               if commitment is not None else set(issues))
    stories = {
        number: issue for number, issue in issues.items()
        if number in allowed
        and "type:story" in dispatcher.labels_of(issue)
        and dispatcher.lifecycle_of(issue, dispatcher.STORY_LIFECYCLE) in RECONCILABLE
    }
    if not stories:
        return []
    pull_requests = dispatcher.fetch_pull_requests(repo, token)

    # Comments are fetched only for claimed stories — the only state where the
    # correction-in-progress discriminator applies — and WIP (§9.10) bounds how
    # many that can be. A fetch failure degrades to the old behaviour.
    comments_by_story: dict[int, list[dict]] = {}
    for number, issue in stories.items():
        if dispatcher.lifecycle_of(issue, dispatcher.STORY_LIFECYCLE) != dispatcher.CLAIMED:
            continue
        try:
            comments_by_story[number] = dispatcher._api(
                f"https://api.github.com/repos/{repo}/issues/{number}"
                f"/comments?per_page=100", token) or []
        except Exception:  # noqa: BLE001 — fail-open to the status quo
            pass

    moved = []
    for outcome in reconcile_all(stories, pull_requests, comments_by_story):
        attempt_trace = None
        try:
            attempt_trace = obs.story_trace_id(
                repo, outcome.number,
                dispatcher.fetch_timeline(repo, outcome.number, token))
        except Exception as trace_error:  # noqa: BLE001 - transition remains independent
            obs.operational_log("ERROR", "review-link trace could not be read back",
                                exc=trace_error, component="review-link",
                                operation="reconcile", repo=repo,
                                story=outcome.number, pull_request=outcome.pr)
        if outcome.action is None:
            # Silence only for the states that are somebody else's business by
            # design. Everything else names itself — §9.11's "no silent drops"
            # is about routes, and a reconciliation that quietly declines is the
            # same failure wearing a different hat.
            if outcome.reason not in (Reason.NOT_RECONCILABLE, Reason.NO_LINKED_PR,
                                      Reason.PR_STILL_OPEN,
                                      Reason.CORRECTION_IN_PROGRESS):
                print(f"[review-link] #{outcome.number} not reconciled: "
                      f"{outcome.reason} ({outcome.detail})", flush=True)
                runlog.event("review_link.declined", trace_id=attempt_trace,
                             story=outcome.number,
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
            runlog.event("review_link.transition", trace_id=attempt_trace,
                         story=outcome.number,
                         to=outcome.action, reason=outcome.reason, pr=outcome.pr)
            moved.append(outcome)
        else:
            print(f"[review-link] #{outcome.number} skipped: {note}", flush=True)
            runlog.event("review_link.skipped", trace_id=attempt_trace,
                         story=outcome.number, detail=note)
    return moved
