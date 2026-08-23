#!/usr/bin/env python3
"""Tests for the delivery-PR lifecycle reconciliation.

Run: python3 -m unittest discover -s factory/runtime -p 'test_*.py' -v

The interesting cases are not the two happy transitions. They are the ones where
evidence is incomplete or contradictory, because this module writes a *terminal
success* — and a terminal state reached on thin evidence is worse than a story
left waiting, which is what the factory did before this existed.
"""

from __future__ import annotations

import unittest
from unittest import mock

import review_link as rl  # noqa: E402 — inserts the dispatcher path
import dispatcher  # noqa: E402


def labels(*names):
    return [{"name": n} for n in names]


def story(number=10, lifecycle="story:claimed", extra=()):
    return {"number": number, "state": "open", "author_association": "OWNER",
            "labels": labels("type:story", "phase:build", *( [lifecycle] if lifecycle else [] ), *extra),
            "body": "### Spec\n\nx\n"}


def pull(number=77, story_number=10, state="open", merged_at=None, body=None, head=None):
    return {"number": number, "state": state, "merged_at": merged_at,
            "head": {"sha": head} if head else {},
            "body": body if body is not None else f"Story: #{story_number}\nAgent-ID: claude-delivery\n"}


class TestTheTwoTransitions(unittest.TestCase):
    def test_only_exact_head_approval_permits_merge_routing(self):
        current = pull(number=200, head="b" * 40)
        stale = {"body": "<!-- review-outcome:200:" + "a" * 40 + ":approval -->"}
        exact = {"body": "<!-- review-outcome:200:" + "b" * 40 + ":approval -->"}
        self.assertFalse(rl.exact_head_approved(current, [stale]))
        self.assertTrue(rl.exact_head_approved(current, [exact]))
    def test_an_open_linked_pr_moves_a_claimed_story_to_review(self):
        outcome = rl.reconcile(story(), [pull()])
        self.assertEqual(outcome.action, dispatcher.IN_REVIEW)
        self.assertEqual(outcome.reason, rl.Reason.OPENED)
        self.assertEqual(outcome.pr, 77)

    def test_a_merged_pr_moves_an_in_review_story_to_merged(self):
        outcome = rl.reconcile(story(lifecycle="story:in-review"),
                               [pull(state="closed", merged_at="2026-08-21T03:24:16Z")])
        self.assertEqual(outcome.action, dispatcher.MERGED)
        self.assertEqual(outcome.reason, rl.Reason.MERGED)

    def test_the_merge_verdict_is_named_as_evidence_the_worker_cannot_forge(self):
        """The reason these transitions may be mechanical at all: GitHub's merge
        state is the outcome of the required gate, not an assertion by the
        credential that opened the pull request (§9.14)."""
        outcome = rl.reconcile(story(lifecycle="story:in-review"),
                               [pull(merged_at="2026-08-21T03:24:16Z")])
        self.assertIn("cannot fabricate", outcome.detail)

    def test_merged_is_recognised_from_either_field(self):
        for pr in (pull(merged_at="2026-08-21T03:24:16Z"), pull()  | {"merged": True}):
            outcome = rl.reconcile(story(lifecycle="story:in-review"), [pr])
            self.assertEqual(outcome.action, dispatcher.MERGED)

    def test_an_open_pr_on_an_in_review_story_changes_nothing(self):
        outcome = rl.reconcile(story(lifecycle="story:in-review"), [pull()])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, rl.Reason.PR_STILL_OPEN)


class TestLegalTransitionFallbacks(unittest.TestCase):
    def test_pr_opened_and_merged_within_one_interval_still_records_in_review(self):
        outcome = rl.reconcile(story(), [pull(merged_at="2026-08-21T03:24:16Z")])
        self.assertEqual(outcome.action, dispatcher.IN_REVIEW)
        self.assertEqual(outcome.reason, rl.Reason.OPENED)
        self.assertIn("between reconciliation intervals", outcome.detail)

        second = rl.reconcile(story(lifecycle="story:in-review"),
                              [pull(merged_at="2026-08-21T03:24:16Z")])
        self.assertEqual(second.action, dispatcher.MERGED)

    def test_dispatcher_recovery_cannot_skip_in_review_if_this_pass_failed(self):
        from datetime import datetime, timezone
        merged = pull(merged_at="2026-08-21T03:24:16Z")
        decision = dispatcher.recovery_decision(
            story(), [], [merged], datetime(2026, 8, 21, tzinfo=timezone.utc))
        self.assertEqual(decision.action, "in-review")
        self.assertEqual(decision.reason, "MERGED_DELIVERY_PR")


class TestAmbiguityFailsClosed(unittest.TestCase):
    def test_two_linked_pull_requests_mutate_nothing(self):
        outcome = rl.reconcile(story(), [pull(70), pull(71)])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, rl.Reason.AMBIGUOUS_LINKED_PRS)

    def test_a_duplicate_story_line_is_an_invalid_link(self):
        outcome = rl.reconcile(story(), [pull(body="Story: #10\nStory: #10\n")])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, rl.Reason.INVALID_PR_LINK)

    def test_a_pr_closed_without_merging_is_a_humans_decision(self):
        """Delivered and rejected. The factory has no rule for what happens
        next, and inventing one here would be inventing policy."""
        outcome = rl.reconcile(story(lifecycle="story:in-review"),
                               [pull(state="closed", merged_at=None)])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, rl.Reason.PR_CLOSED_UNMERGED)

    def test_a_pr_for_another_story_does_not_count(self):
        outcome = rl.reconcile(story(10), [pull(story_number=99)])
        self.assertEqual(outcome.reason, rl.Reason.NO_LINKED_PR)

    def test_a_claimed_story_with_no_pr_is_left_to_the_lease(self):
        outcome = rl.reconcile(story(), [])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, rl.Reason.NO_LINKED_PR)

    def test_ambiguous_lifecycle_labels_are_not_reconcilable(self):
        outcome = rl.reconcile(story(extra=("story:blocked",)), [pull()])
        self.assertEqual(outcome.reason, rl.Reason.NOT_RECONCILABLE)

    def test_states_outside_the_delivery_path_are_untouched(self):
        for lifecycle in ("story:ready", "story:blocked", "story:merged",
                          "story:completed", "story:cancelled", "story:blocked:poison"):
            outcome = rl.reconcile(story(lifecycle=lifecycle), [pull()])
            self.assertIsNone(outcome.action, lifecycle)
            self.assertEqual(outcome.reason, rl.Reason.NOT_RECONCILABLE, lifecycle)


class FakeGitHub:
    def __init__(self, issues):
        self.issues = {i["number"]: dict(i) for i in issues}
        self.patched = []

    def __call__(self, url, token, method="GET", payload=None):
        import re
        number = int(re.search(r"/issues/(\d+)", url).group(1))
        if method == "PATCH":
            self.patched.append((number, payload))
            issue = self.issues[number]
            issue["labels"] = labels(*payload["labels"])
            if "state" in payload:
                issue["state"] = payload["state"]
            return dict(issue)
        return dict(self.issues[number])


class TestApplyingTheTransition(unittest.TestCase):
    def apply(self, outcome, issues):
        hub = FakeGitHub(issues)
        with mock.patch.object(dispatcher, "_api", hub):
            ok, note = rl.apply_outcome("o/r", outcome, "token")
        return ok, note, hub

    def test_review_is_a_label_only_write(self):
        outcome = rl.Outcome(10, dispatcher.IN_REVIEW, rl.Reason.OPENED, "", 77)
        ok, _, hub = self.apply(outcome, [story()])
        self.assertTrue(ok)
        _, payload = hub.patched[0]
        self.assertNotIn("state", payload, "opening a PR does not close the story")
        self.assertIn("story:in-review", payload["labels"])
        self.assertNotIn("story:claimed", payload["labels"])

    def test_merged_closes_as_completed_in_one_write(self):
        """§9.2 — one PATCH carrying the complete final label set, and §9.3's
        closure is part of the same operation."""
        outcome = rl.Outcome(10, dispatcher.MERGED, rl.Reason.MERGED, "", 77)
        ok, _, hub = self.apply(outcome, [story(lifecycle="story:in-review")])
        self.assertTrue(ok)
        self.assertEqual(len(hub.patched), 1)
        _, payload = hub.patched[0]
        self.assertEqual(payload["state"], "closed")
        self.assertEqual(payload["state_reason"], "completed")
        self.assertIn("story:merged", payload["labels"])

    def test_unrelated_labels_survive_the_write(self):
        outcome = rl.Outcome(10, dispatcher.IN_REVIEW, rl.Reason.OPENED, "", 77)
        _, _, hub = self.apply(outcome, [story(extra=("hazard",))])
        _, payload = hub.patched[0]
        for kept in ("type:story", "phase:build", "hazard"):
            self.assertIn(kept, payload["labels"])

    def test_a_story_moved_between_decision_and_write_is_left_alone(self):
        """Idempotent by re-reading: the state write is the duplicate
        suppressor, exactly as in `dispatcher.claim`."""
        outcome = rl.Outcome(10, dispatcher.IN_REVIEW, rl.Reason.OPENED, "", 77)
        ok, note, hub = self.apply(outcome, [story(lifecycle="story:merged")])
        self.assertFalse(ok)
        self.assertIn(rl.Reason.STALE_PLAN, note)
        self.assertEqual(hub.patched, [])

    def test_a_second_pass_over_the_same_state_writes_nothing(self):
        outcome = rl.Outcome(10, dispatcher.MERGED, rl.Reason.MERGED, "", 77)
        hub = FakeGitHub([story(lifecycle="story:in-review")])
        with mock.patch.object(dispatcher, "_api", hub):
            first, _ = rl.apply_outcome("o/r", outcome, "token")
            second, note = rl.apply_outcome("o/r", outcome, "token")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(hub.patched), 1)

    def test_a_decision_to_do_nothing_never_writes(self):
        outcome = rl.Outcome(10, None, rl.Reason.PR_STILL_OPEN, "", 77)
        ok, _, hub = self.apply(outcome, [story()])
        self.assertFalse(ok)
        self.assertEqual(hub.patched, [])


class TestNoPolicyIsInvented(unittest.TestCase):
    def test_linkage_is_the_dispatchers_definition_not_a_local_one(self):
        """§9.5 linkage is defined once. A second implementation would drift,
        and the drift would be silent."""
        with open(rl.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("dispatcher.linked_delivery_prs", source)
        # Naming the link in prose is fine; *matching* it here is not. A second
        # implementation of §9.5 would drift, and the drift would be silent.
        self.assertNotIn("re.compile", source)
        self.assertNotIn("import re", source)

    def test_the_terminal_state_comes_from_the_authority(self):
        with open(rl.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("dispatcher.MERGED", source)
        self.assertNotIn('"story:merged"', source)

    def test_the_module_holds_no_attempt_or_wip_logic(self):
        with open(rl.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("Attempt", "WIP_LIMIT", "ATTEMPT_MAX"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden} is policy, and policy does not live here")

    def test_reconciliation_is_deterministic_and_ordered(self):
        stories = {12: story(12), 10: story(10), 11: story(11)}
        pulls = [pull(70, 10), pull(71, 11), pull(72, 12)]
        first = rl.reconcile_all(stories, pulls)
        second = rl.reconcile_all(stories, pulls)
        self.assertEqual([o.number for o in first], [10, 11, 12])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestARedispatchedCorrectionIsNotReclaimed(unittest.TestCase):
    """#342 — reconcile moved a claimed story straight back to in-review on the
    sole basis of an open linked PR, without asking whether the claim was a
    redispatched correction. It reclaimed #328 sixty-two seconds after its
    claim on 2026-08-23, well inside the minutes a correction takes."""

    HEAD = "6147e002" + "0" * 32

    def findings(self, pr=77, head=None):
        return {"body": f"<!-- review-outcome:{pr}:{head or self.HEAD}:findings -->"}

    def test_a_findings_reviewed_head_holds_the_claimed_story(self):
        outcome = rl.reconcile(story(), [pull(head=self.HEAD)],
                               story_comments=[self.findings()])
        self.assertIsNone(outcome.action)
        self.assertEqual(rl.Reason.CORRECTION_IN_PROGRESS, outcome.reason)

    def test_a_new_head_releases_the_hold(self):
        """The correction pushes; the marker no longer matches; review may run."""
        new_head = "9f4252cd" + "0" * 32
        outcome = rl.reconcile(story(), [pull(head=new_head)],
                               story_comments=[self.findings()])
        self.assertEqual(dispatcher.IN_REVIEW, outcome.action)

    def test_a_fresh_delivery_with_no_outcomes_moves_as_before(self):
        outcome = rl.reconcile(story(), [pull(head=self.HEAD)],
                               story_comments=[])
        self.assertEqual(dispatcher.IN_REVIEW, outcome.action)

    def test_no_comments_supplied_preserves_the_old_behaviour_exactly(self):
        """Fail-open to the status quo — a comment fetch failure must degrade
        to today's transition, never to a stuck story."""
        outcome = rl.reconcile(story(), [pull(head=self.HEAD)])
        self.assertEqual(dispatcher.IN_REVIEW, outcome.action)

    def test_a_malformed_marker_is_ignored_not_fatal(self):
        junk = {"body": "<!-- review-outcome:77:not-a-sha:findings -->"}
        outcome = rl.reconcile(story(), [pull(head=self.HEAD)],
                               story_comments=[junk])
        self.assertEqual(dispatcher.IN_REVIEW, outcome.action)

    def test_an_approval_on_the_current_head_does_not_hold(self):
        """Only findings mean a correction is owed; an approval means review is
        done and routing owns what happens next."""
        approval = {"body": f"<!-- review-outcome:77:{self.HEAD}:approval -->"}
        outcome = rl.reconcile(story(), [pull(head=self.HEAD)],
                               story_comments=[approval])
        self.assertEqual(dispatcher.IN_REVIEW, outcome.action)

    def test_an_in_review_story_is_untouched_by_the_discriminator(self):
        outcome = rl.reconcile(story(lifecycle="story:in-review"),
                               [pull(head=self.HEAD)],
                               story_comments=[self.findings()])
        self.assertEqual(rl.Reason.PR_STILL_OPEN, outcome.reason)
