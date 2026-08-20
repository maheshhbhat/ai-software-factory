#!/usr/bin/env python3
"""Tests for decision-comment continuation. Standard library only.

Run: python3 -m unittest discover -s factory/runtime -p 'test_*.py' -v

The load-bearing tests are TestSelfApprovalGuard and TestBindingRule. This
module lets a *comment* move a project's lifecycle, and the agent writes
comments through the same credential as the human — so the interesting question
is not "does a valid approval work" but "can anything the factory itself writes
be mistaken for one".
"""

from __future__ import annotations

import unittest

import continuation as ct
from continuation import Reason as R

CRITERIA = "- [ ] does the thing\n- [ ] does not do the other thing"


def project(number=100, state=ct.AWAITING_READY, criteria=CRITERIA):
    body = (f"### Goal\n\ng\n\n### Falsifiable acceptance criteria\n\n{criteria}\n\n"
            f"### Stories\n\n#1\n\n### Roadmap commitment\n\n#54\n")
    return {"number": number, "state": "OPEN",
            "labels": [{"name": "type:project"}, {"name": state}], "body": body}


def comment(body, assoc="OWNER"):
    return {"body": body, "authorAssociation": assoc}


def approval(criteria=CRITERIA, decision="approved"):
    return comment(f"## Plan approval\n\ndecision: {decision}\nactor: @maheshhbhat\n\n"
                   f"Approved criteria:\n\n{criteria}\n")


def acceptance(result="pass"):
    return comment(f"## Acceptance\n\nresult: {result}\nactor: @maheshhbhat\n\n"
                   f"- criterion 1 — {result}\n\nfollow-up: none\n")


class TestApprovalContinuation(unittest.TestCase):
    def test_valid_owner_approval_advances(self):
        outcome = ct.evaluate_project(project(), [approval()])
        self.assertEqual(outcome.action, ct.ACTIVE)
        self.assertEqual(outcome.reason, R.CONTINUED)

    def test_no_decision_leaves_state_untouched(self):
        outcome = ct.evaluate_project(project(), [comment("looks good to me!")])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, R.NO_DECISION)

    def test_no_comments_at_all(self):
        self.assertEqual(ct.evaluate_project(project(), []).reason, R.NO_DECISION)

    def test_non_owner_approval_is_ignored(self):
        for assoc in ("NONE", "CONTRIBUTOR", "COLLABORATOR", "MEMBER", ""):
            outcome = ct.evaluate_project(project(), [approval()] and
                                          [comment(approval()["body"], assoc)])
            self.assertIsNone(outcome.action, assoc)
            self.assertEqual(outcome.reason, R.NOT_OWNER_AUTHORED, assoc)

    def test_changes_requested_does_not_advance(self):
        outcome = ct.evaluate_project(project(), [approval(decision="changes-requested")])
        self.assertIsNone(outcome.action)

    def test_project_not_at_a_bell_is_skipped(self):
        for state in (ct.ACTIVE, ct.ACCEPTED, "project:queued", "project:planning"):
            outcome = ct.evaluate_project(project(state=state), [approval()])
            self.assertEqual(outcome.reason, R.NOT_AT_A_BELL, state)


class TestSelfApprovalGuard(unittest.TestCase):
    """The agent shares the CTO's credential, so its comments are OWNER-authored
    too. Only the heading separates a decision from a recording — pin it."""

    def test_the_factorys_own_recording_comment_does_not_approve(self):
        """This is the exact shape this factory posts when recording a bell. It
        is OWNER-authored and quotes the criteria verbatim; it must never be
        read as the decision itself."""
        recording = comment(
            "## Approval recorded — criteria anchored verbatim\n\n"
            "This comment **records** the CTO plan-approval above; it is not itself a "
            "decision (§9.7 — attribution, not authentication).\n\n"
            f"{CRITERIA}\n\n"
            "Bell: `plan-approval` / `decision`, actor `@maheshhbhat`.\n")
        outcome = ct.evaluate_project(project(), [recording])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, R.NO_DECISION)

    def test_superseded_notice_does_not_approve(self):
        superseded = comment("## Approval superseded\n\nreason: criteria corrected\n"
                             "actor: @claude (agent)\n")
        self.assertIsNone(ct.evaluate_project(project(), [superseded]).action)

    def test_narrative_mentioning_approval_does_not_approve(self):
        for body in ("The CTO approved this. decision: approved",
                     "### Plan approval\n\ndecision: approved",
                     "> ## Plan approval\n> decision: approved",
                     "Earlier I said `## Plan approval` — decision: approved"):
            outcome = ct.evaluate_project(project(), [comment(body)])
            self.assertIsNone(outcome.action, body[:40])

    def test_heading_must_be_exact(self):
        for heading in ("## plan approval", "##Plan approval", "## Plan Approval"):
            body = f"{heading}\n\ndecision: approved\n"
            outcome = ct.evaluate_project(project(), [comment(body)])
            # Case-insensitive headings are not accepted; only the canonical form.
            if outcome.action is not None:
                self.assertEqual(heading, "## Plan approval", f"{heading} advanced")


class TestBindingRule(unittest.TestCase):
    """§5.1 — the one check that holds regardless of who typed what."""

    def test_criteria_edited_after_approval_fails_closed(self):
        approved_against = CRITERIA
        live = "- [ ] does the thing\n- [ ] and something new nobody approved"
        outcome = ct.evaluate_project(project(criteria=live),
                                      [approval(criteria=approved_against)])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, R.CRITERIA_CHANGED)

    def test_ticking_a_checkbox_is_not_an_amendment(self):
        """Running the criteria ticks boxes; that is progress, not a change."""
        ticked = CRITERIA.replace("- [ ]", "- [x]")
        outcome = ct.evaluate_project(project(criteria=ticked), [approval(criteria=CRITERIA)])
        self.assertEqual(outcome.action, ct.ACTIVE)

    def test_whitespace_differences_are_not_amendments(self):
        spaced = CRITERIA.replace(" does", "  does")
        outcome = ct.evaluate_project(project(criteria=spaced), [approval(criteria=CRITERIA)])
        self.assertEqual(outcome.action, ct.ACTIVE)

    def test_removing_a_criterion_fails_closed(self):
        fewer = "- [ ] does the thing"
        outcome = ct.evaluate_project(project(criteria=fewer), [approval(criteria=CRITERIA)])
        self.assertEqual(outcome.reason, R.CRITERIA_CHANGED)

    def test_approval_quoting_nothing_still_advances_but_is_flagged(self):
        bare = comment("## Plan approval\n\ndecision: approved\nactor: @maheshhbhat\n")
        outcome = ct.evaluate_project(project(), [bare])
        self.assertEqual(outcome.action, ct.ACTIVE)
        self.assertIn("unanchored", outcome.detail)


class TestAmbiguity(unittest.TestCase):
    def test_conflicting_owner_decisions_fail_closed(self):
        outcome = ct.evaluate_project(
            project(), [approval(), approval(decision="changes-requested")])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, R.CONFLICTING_DECISIONS)

    def test_repeated_identical_decisions_are_not_a_conflict(self):
        outcome = ct.evaluate_project(project(), [approval(), approval()])
        self.assertEqual(outcome.action, ct.ACTIVE)

    def test_malformed_decision_line_fails_closed(self):
        outcome = ct.evaluate_project(project(), [comment("## Plan approval\n\nlgtm\n")])
        self.assertEqual(outcome.reason, R.MALFORMED_DECISION)

    def test_unrecognized_verdict_fails_closed(self):
        outcome = ct.evaluate_project(project(), [approval(decision="maybe later")])
        self.assertEqual(outcome.reason, R.AMBIGUOUS_DECISION)


class TestAcceptance(unittest.TestCase):
    def test_pass_accepts_the_project(self):
        outcome = ct.evaluate_project(project(state=ct.AWAITING_ACCEPTANCE), [acceptance()])
        self.assertEqual(outcome.action, ct.ACCEPTED)

    def test_fail_returns_to_active(self):
        """§5.3: a failed criterion does not close the project."""
        outcome = ct.evaluate_project(project(state=ct.AWAITING_ACCEPTANCE),
                                      [acceptance(result="fail")])
        self.assertEqual(outcome.action, ct.ACTIVE)

    def test_approval_comment_does_not_satisfy_the_acceptance_bell(self):
        outcome = ct.evaluate_project(project(state=ct.AWAITING_ACCEPTANCE), [approval()])
        self.assertIsNone(outcome.action)

    def test_non_owner_acceptance_ignored(self):
        outcome = ct.evaluate_project(project(state=ct.AWAITING_ACCEPTANCE),
                                      [comment(acceptance()["body"], "NONE")])
        self.assertEqual(outcome.reason, R.NOT_OWNER_AUTHORED)


class TestIdempotencyAndAuthority(unittest.TestCase):
    def test_advanced_project_is_no_longer_at_a_bell(self):
        """Consumption needs no cursor: the transition itself removes the project
        from the pass's view, so a replay sees nothing to do."""
        after = project(state=ct.ACTIVE)
        self.assertEqual(ct.evaluate_project(after, [approval()]).reason, R.NOT_AT_A_BELL)

    def test_evaluation_is_pure_and_repeatable(self):
        p, c = project(), [approval()]
        self.assertEqual(ct.evaluate_project(p, c), ct.evaluate_project(p, c))

    def test_module_persists_no_local_state(self):
        """No file, cache, or cursor may decide whether a decision was consumed —
        the lifecycle label alone does (§9.12).

        Checks for local *writes* specifically. `json.dumps` for an API payload
        is fine; opening a file for writing, or a database, is not.
        """
        import re as _re

        with open(ct.__file__, encoding="utf-8") as handle:
            source = handle.read()
        body = source.split('"""', 2)[-1]
        # Word-bounded so `urlopen(` and `json.dumps(` — legitimate API calls —
        # are not mistaken for local persistence.
        for pattern in (r"\bopen\s*\(", r"\bjson\.dump\s*\(", r"\bsqlite3?\b",
                        r"\bpickle\b", r"\bshelve\b", r"\bPath\s*\("):
            self.assertIsNone(_re.search(pattern, body),
                              f"continuation must not persist local state ({pattern})")

    def test_continuation_line_is_worker_neutral(self):
        line = ct.continuation_line(ct.Outcome(72, ct.ACTIVE, R.CONTINUED))
        self.assertEqual(line, "CONTINUE project=#72 to=project:active")
        for leaked in ("claude", "codex", "agent"):
            self.assertNotIn(leaked, line.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
