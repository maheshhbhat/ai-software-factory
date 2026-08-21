#!/usr/bin/env python3
"""Tests for the post-worker-success completion path. Standard library only.

Run: python3 -m unittest discover -s factory/runtime -p 'test_*.py' -v

`TestFailsClosed` is the class to defend in review. This module is the only
thing in the runtime that can move a Story to a terminal state and close the
issue, and §9.3 says no component reopens a closed issue — so a wrong `yes` here
is the one mistake in this repository that a later pass cannot undo. Every test
in that class asserts the same shape of answer: when the evidence is not
complete, the Story is left exactly as it is.

The cost of a wrong `no` is one lease period and a §9.4 recovery. The costs are
not symmetric, and neither are these tests.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import bridge
import completion
import dispatcher
import workers


def setUpModule():
    """Operational logging is a side effect of these tests, not their subject."""
    os.environ["FACTORY_RUNTIME_LOG_STDERR"] = "0"
    os.environ.setdefault("FACTORY_RUNTIME_LOG",
                          os.path.join(tempfile.gettempdir(), "factory-runtime-test.jsonl"))


CLAIMED_AT = "2026-08-21T03:30:00Z"
ACK_AT = "2026-08-21T03:38:35Z"

ACK_BODY = ("## Worker acknowledgement\n\nI am claude-delivery, running as a "
            "delivery worker. Acknowledged at 2026-08-21T03:38:35Z.")


def story(number=103, labels=("type:story", "story:claimed", "phase:build"),
          state="open"):
    return {"number": number, "state": state,
            "labels": [{"name": name} for name in labels],
            "body": "### Project\n\n#95\n\n### Attempt\n\n1\n"}


def timeline(claimed_at=CLAIMED_AT, label="story:claimed"):
    return [{"event": "labeled", "created_at": claimed_at, "label": {"name": label}}]


def comments(*bodies, created_at=ACK_AT):
    return [{"id": 1000 + i, "body": body, "created_at": created_at,
             "html_url": f"https://github.com/o/r/issues/103#issuecomment-{1000 + i}"}
            for i, body in enumerate(bodies)]


def decide(story_issue=None, tl=None, cmts=None, prs=None,
           result=workers.Result.LAUNCHED):
    return completion.completion_decision(
        story_issue if story_issue is not None else story(),
        timeline() if tl is None else tl,
        comments(ACK_BODY) if cmts is None else cmts,
        [] if prs is None else prs,
        result)


class TestTheHappyPathIsNarrow(unittest.TestCase):
    def test_proven_bounded_success_completes_the_story(self):
        outcome = decide()
        self.assertEqual(dispatcher.CANCELLED, outcome.action)
        self.assertEqual(completion.Reason.COMPLETED, outcome.reason)

    def test_the_recorded_reason_names_the_evidence(self):
        """A terminal transition nobody can check is not auditable."""
        outcome = decide()
        self.assertIn(ACK_AT, outcome.detail)
        self.assertIn("no pull request", outcome.detail)

    def test_the_target_state_is_the_one_the_contract_already_names(self):
        """§9.3 names `story:cancelled` for work that finishes with no
        deliverable — no new label, no new bell, no new orchestration."""
        self.assertEqual("story:cancelled", decide().action)

    def test_markdown_around_the_heading_still_counts_as_proof(self):
        outcome = decide(cmts=comments("**## Worker acknowledgement**\n\nclaude here"))
        self.assertEqual(completion.Reason.COMPLETED, outcome.reason)


class TestFailsClosed(unittest.TestCase):
    """Anything short of complete evidence leaves the Story alone."""

    def assertLeftAlone(self, outcome, reason):
        self.assertIsNone(outcome.action, f"expected no transition, got {outcome.action}")
        self.assertEqual(reason, outcome.reason)

    def test_an_ambiguous_launch_never_completes(self):
        """The worker may be running right now. Closing a Story out from under a
        live worker is strictly worse than letting the §9.4 lease expire."""
        self.assertLeftAlone(decide(result=workers.Result.AMBIGUOUS),
                             completion.Reason.LAUNCH_NOT_DEFINITE)

    def test_a_failed_launch_never_completes(self):
        self.assertLeftAlone(decide(result=workers.Result.FAILED),
                             completion.Reason.LAUNCH_NOT_DEFINITE)

    def test_a_story_someone_moved_is_not_completed(self):
        self.assertLeftAlone(decide(story_issue=story(labels=("type:story", "story:in-review"))),
                             completion.Reason.NOT_CLAIMED)

    def test_ambiguous_lifecycle_labels_are_not_completed(self):
        self.assertLeftAlone(
            decide(story_issue=story(labels=("type:story", "story:claimed", "story:ready"))),
            completion.Reason.NOT_CLAIMED)

    def test_a_linked_pull_request_belongs_to_review_not_here(self):
        """A worker that produced a deliverable has a review path. This module
        only ever completes work that finished with nothing to merge."""
        prs = [{"number": 200, "body": "Story: #103\n", "state": "open"}]
        self.assertLeftAlone(decide(prs=prs), completion.Reason.DELIVERABLE_EXISTS)

    def test_a_malformed_pr_link_is_ambiguity_and_stops_here(self):
        prs = [{"number": 200, "body": "Story: #103\nStory: #103\n", "state": "open"}]
        self.assertLeftAlone(decide(prs=prs), completion.Reason.DELIVERABLE_EXISTS)

    def test_a_pr_for_another_story_does_not_block_completion(self):
        prs = [{"number": 200, "body": "Story: #99\n", "state": "open"}]
        self.assertEqual(completion.Reason.COMPLETED, decide(prs=prs).reason)

    def test_no_claim_event_means_no_window_to_measure_against(self):
        self.assertLeftAlone(decide(tl=[]), completion.Reason.CLAIM_EVENT_MISSING)

    def test_no_acknowledgement_leaves_the_story_claimed(self):
        """The dispatched worker returned success but nothing durable exists.
        The §9.4 lease is the right resolution, not a closed issue."""
        self.assertLeftAlone(decide(cmts=comments("Looks good to me")),
                             completion.Reason.NO_PROOF)

    def test_an_acknowledgement_from_before_the_claim_is_not_proof(self):
        """A stale acknowledgement from an earlier lease says nothing about this
        worker — the same rule the bridge applies to its own window (#98)."""
        self.assertLeftAlone(
            decide(cmts=comments(ACK_BODY, created_at="2026-08-20T09:00:00Z")),
            completion.Reason.NO_PROOF)

    def test_the_latest_claim_defines_the_window_not_the_first(self):
        """A recovered and re-dispatched Story has several claim events. Proof
        from the previous lease is not proof about this worker."""
        tl = timeline("2026-08-20T09:00:00Z") + timeline("2026-08-21T04:00:00Z")
        self.assertLeftAlone(decide(tl=tl), completion.Reason.NO_PROOF)

    def test_prose_about_an_acknowledgement_is_not_an_acknowledgement(self):
        self.assertLeftAlone(
            decide(cmts=comments("The worker never posted a `## Worker acknowledgement`.")),
            completion.Reason.NO_PROOF)

    def test_an_already_recorded_completion_is_not_recorded_twice(self):
        cmts = comments(ACK_BODY) + comments(
            completion.COMPLETION_HEADING + "\n\nrecorded earlier")
        self.assertLeftAlone(decide(cmts=cmts), completion.Reason.ALREADY_RECORDED)


class TestTheRecordedComment(unittest.TestCase):
    def test_it_states_the_decision_the_reason_and_the_evidence(self):
        """§9.3 requires a comment saying what was decided and why."""
        body = completion.completion_comment(decide(), "claude-delivery")
        self.assertTrue(body.startswith(completion.COMPLETION_HEADING))
        self.assertIn("claude-delivery", body)
        self.assertIn("story:claimed → story:cancelled", body)
        self.assertIn(ACK_AT, body)

    def test_the_recording_can_never_be_read_as_the_proof_it_cites(self):
        """Otherwise the factory could mistake its own bookkeeping for a
        worker's work — the self-approval trap `continuation.py` guards."""
        body = completion.completion_comment(decide(), "claude-delivery")
        self.assertFalse(bridge.looks_like_acknowledgement(body))

    def test_the_recording_is_not_a_section5_decision_heading(self):
        """A completion must never be consumable by the continuation pass."""
        for heading in ("## Plan approval", "## Acceptance"):
            self.assertNotIn(heading, completion.completion_comment(decide(), "w"))


class FakeGitHub:
    """The subset of the API these paths use, in memory."""

    def __init__(self, issue, tl, cmts, prs):
        self.issue, self.timeline, self.comments, self.prs = issue, tl, cmts, prs
        self.writes = []

    def __call__(self, url, token, method="GET", payload=None):
        if method == "PATCH":
            self.writes.append(("PATCH", url, payload))
            return {}
        if method == "POST":
            self.writes.append(("POST", url, payload))
            return {}
        if "/timeline" in url:
            return self.timeline if "page=1" in url else []
        if "/comments" in url:
            return self.comments if "page=1" in url else []
        if "/pulls" in url:
            return self.prs if "page=1" in url else []
        return self.issue


def run_record(issue=None, tl=None, cmts=None, prs=None,
               result=workers.Result.LAUNCHED, apply=True):
    api = FakeGitHub(issue if issue is not None else story(),
                     timeline() if tl is None else tl,
                     comments(ACK_BODY) if cmts is None else cmts,
                     [] if prs is None else prs)
    with mock.patch.object(dispatcher, "_api", api):
        outcome = completion.record_success(
            repo="o/r", story=103, project=95, launch_result=result,
            worker="claude-delivery", token="t", apply=apply)
    return outcome, api


class TestApplyingTheTransition(unittest.TestCase):
    def test_the_comment_is_posted_before_the_label_write(self):
        """A crash between the two writes must leave a claimed Story carrying an
        explanation, not a terminal Story with no recorded reason."""
        _, api = run_record()
        self.assertEqual(["POST", "PATCH"], [w[0] for w in api.writes])

    def test_the_transition_replaces_the_whole_label_set_and_closes(self):
        """§9.2 — one PATCH carrying the complete final label set; §9.3 —
        closure is part of the transition."""
        _, api = run_record()
        payload = api.writes[-1][2]
        self.assertIn("story:cancelled", payload["labels"])
        self.assertNotIn("story:claimed", payload["labels"])
        self.assertIn("type:story", payload["labels"])
        self.assertEqual("closed", payload["state"])
        self.assertEqual("not_planned", payload["state_reason"])

    def test_the_attempt_counter_is_left_untouched(self):
        """The attempt was dispatched and it succeeded. §4.3 increments only at
        dispatch; nothing here may rewrite the body."""
        _, api = run_record()
        method, _, payload = api.writes[-1]
        self.assertEqual("PATCH", method)
        self.assertNotIn("body", payload)

    def test_a_story_moved_between_decision_and_write_is_left_alone(self):
        """§9.2 precondition check: re-read, verify the expected `from` state."""
        api = FakeGitHub(story(), timeline(), comments(ACK_BODY), [])
        moved = story(labels=("type:story", "story:in-review"))
        reads = []

        def api_with_race(url, token, method="GET", payload=None):
            """Claimed when the decision reads it, moved by the time the
            transition re-reads it — the §9.2 race, staged."""
            if method == "GET" and url.endswith("/issues/103"):
                reads.append(url)
                return story() if len(reads) == 1 else moved
            return api(url, token, method, payload)

        with mock.patch.object(dispatcher, "_api", api_with_race):
            outcome = completion.record_success(
                repo="o/r", story=103, project=95,
                launch_result=workers.Result.LAUNCHED, worker="w", token="t")
        self.assertIsNone(outcome.action)
        self.assertEqual(completion.Reason.NOT_APPLIED, outcome.reason)
        self.assertEqual([], api.writes)

    def test_nothing_is_written_when_the_decision_is_to_leave_it_alone(self):
        _, api = run_record(cmts=comments("no acknowledgement here"))
        self.assertEqual([], api.writes)

    def test_a_dry_run_decides_but_writes_nothing(self):
        outcome, api = run_record(apply=False)
        self.assertEqual(completion.Reason.NOT_APPLIED, outcome.reason)
        self.assertEqual([], api.writes)


class TestNoPolicyIsInvented(unittest.TestCase):
    """The transition is narrow by construction, and stays narrow only if the
    definitions it depends on keep living in one place."""

    def test_section_9_5_linkage_is_the_dispatchers_definition(self):
        """Two definitions of 'a PR links to this Story' disagreeing is how a
        Story with a deliverable gets closed as having none."""
        with open(completion.__file__, encoding="utf-8") as handle:
            body = handle.read().split('"""', 2)[-1]
        self.assertIn("dispatcher.linked_delivery_prs", body)
        self.assertNotIn("Story: #", body)

    def test_what_counts_as_proof_is_the_bridges_definition(self):
        with open(completion.__file__, encoding="utf-8") as handle:
            body = handle.read().split('"""', 2)[-1]
        self.assertIn("bridge.looks_like_acknowledgement", body)

    def test_the_bounded_assignment_still_produces_no_deliverable(self):
        """The coupling this module rests on, pinned deliberately.

        Completing a Story as *finished with no deliverable* is only honest
        while the worker's assignment forbids producing one. If the bridge's
        prompt ever grows a pull request, this test fails and whoever widened it
        must revisit the completion rule instead of silently cancelling Stories
        that were supposed to deliver code.
        """
        prompt = bridge.task_prompt("o/r", 103, 95).lower()
        self.assertIn("do not create branches, commits, or pull requests", prompt)
        self.assertIn("do not modify any file", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
