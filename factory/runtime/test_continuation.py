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

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import continuation as ct
from continuation import Reason as R

CRITERIA = "- [ ] does the thing\n- [ ] does not do the other thing"


def project(number=100, state=ct.AWAITING_READY, criteria=CRITERIA):
    body = (f"### Goal\n\ng\n\n### Falsifiable acceptance criteria\n\n{criteria}\n\n"
            f"### Stories\n\n#1\n\n### Roadmap commitment\n\n#54\n")
    value = {"number": number, "state": "OPEN",
            "labels": [{"name": "type:project"}, {"name": state}], "body": body}
    if state in (ct.AWAITING_READY, ct.AWAITING_ACCEPTANCE):
        value["_bell_at"] = "2026-08-22T01:00:00Z"
    return value


def comment(body, assoc="OWNER", created_at="2026-08-22T01:01:00Z"):
    return {"body": body, "authorAssociation": assoc, "created_at": created_at}


def approval(criteria=CRITERIA, decision="approved"):
    return comment(f"## Plan approval\n\ndecision: {decision}\nactor: @maheshhbhat\n\n"
                   f"Approved criteria:\n\n{criteria}\n")


def standing_project(number=200):
    """A project holding §4.1.1's standing state — never at a bell by definition."""
    return project(number=number, state=ct.STANDING)


def acceptance(result="pass", created_at="2026-08-22T01:01:00Z"):
    return comment(f"## Acceptance\n\nresult: {result}\nactor: @maheshhbhat\n\n"
                   f"- criterion 1 — {result}\n\nfollow-up: none\n",
                   created_at=created_at)


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


class TestStandingProjectUniqueness(unittest.TestCase):
    """#314 SM-06 / §4.1.1 — at most one standing Project, enforced at approval.

    Approval is the only moment a Project becomes standing, so it is the only
    place the rule can be enforced without inventing a state to police. A second
    one is refused outright: the Project keeps its `project:awaiting-ready` label
    and nothing is written, rather than the transition landing beside a warning.

    The target label is `project:standing` because that is the state §4.1.1
    defines for continuous work — the Story's `### Acceptance notes` say
    `project:active`, which is the edge the guard sits on, not the state an
    approved standing Project ends in. Ending it in `project:active` would leave
    the standing state unreachable and this rule with nothing to enforce.
    """

    def approval_standing(self, criteria=CRITERIA):
        return approval(criteria=criteria, decision="approved (standing)")

    def test_the_first_standing_approval_is_applied(self):
        outcome = ct.evaluate_project(project(), [self.approval_standing()], standing=[])
        self.assertEqual(ct.STANDING, outcome.action)
        self.assertEqual(R.CONTINUED, outcome.reason)

    def test_a_second_standing_approval_is_refused_and_names_the_first(self):
        outcome = ct.evaluate_project(project(), [self.approval_standing()], standing=[200])
        self.assertIsNone(outcome.action, "no transition at all, not a partial one")
        self.assertEqual(R.SECOND_STANDING_PROJECT, outcome.reason)
        self.assertIn("#200", outcome.detail)

    def test_every_holder_is_named_not_just_a_count(self):
        outcome = ct.evaluate_project(project(), [self.approval_standing()],
                                      standing=[201, 200])
        self.assertIn("#200", outcome.detail)
        self.assertIn("#201", outcome.detail)

    def test_the_refusal_is_the_same_shape_as_the_other_approval_refusals(self):
        """It blocks the transition; it does not decorate one."""
        refused = ct.evaluate_project(project(), [self.approval_standing()], standing=[200])
        criteria = ct.evaluate_project(project(criteria="- [ ] changed"), [approval()])
        for outcome in (refused, criteria):
            self.assertIsNone(outcome.action)
            self.assertIsNone(outcome.decision)
            self.assertNotEqual(R.CONTINUED, outcome.reason)
            self.assertTrue(outcome.detail)

    def test_the_project_the_approval_belongs_to_is_not_its_own_rival(self):
        """Re-evaluating a project already counted as standing must not refuse
        itself — the guard is about a *second* project, not a replay."""
        outcome = ct.evaluate_project(project(), [self.approval_standing()], standing=[100])
        self.assertEqual(ct.STANDING, outcome.action)

    def test_replaying_the_refused_approval_refuses_identically(self):
        args = (project(), [self.approval_standing()], [200])
        first, second = ct.evaluate_project(*args), ct.evaluate_project(*args)
        self.assertEqual(first, second)
        self.assertEqual(first.detail, second.detail)

    def test_an_ordinary_approval_is_untouched_by_the_rule(self):
        """A standing project existing does not block bounded projects."""
        outcome = ct.evaluate_project(project(), [approval()], standing=[200])
        self.assertEqual(ct.ACTIVE, outcome.action)

    def test_prose_about_standing_work_does_not_designate_a_standing_project(self):
        """Only the decision line designates. Otherwise every approval that
        discusses maintenance would silently claim the state."""
        body = ("## Plan approval\n\ndecision: approved\nactor: @maheshhbhat\n\n"
                f"This is standing maintenance work.\n\n{CRITERIA}\n")
        outcome = ct.evaluate_project(project(), [comment(body)], standing=[200])
        self.assertEqual(ct.ACTIVE, outcome.action)

    def test_a_standing_approval_disagreeing_with_a_bounded_one_fails_closed(self):
        outcome = ct.evaluate_project(project(), [approval(), self.approval_standing()])
        self.assertIsNone(outcome.action)
        self.assertEqual(R.CONFLICTING_DECISIONS, outcome.reason)

    def test_a_mislabelled_project_still_counts_as_holding_the_state(self):
        """§2.1 leaves a two-label project with no lifecycle. For uniqueness that
        must fail closed — it is still claiming the state it is labelled with."""
        broken = standing_project(200)
        broken["labels"].append({"name": ct.ACTIVE})
        self.assertIsNone(ct.lifecycle_of(broken))
        issues = {100: project(), 200: broken}
        self.assertEqual([200], ct.standing_holders(issues))
        self.assertEqual(R.SECOND_STANDING_PROJECT,
                         ct.evaluate_project(issues[100], [self.approval_standing()],
                                             ct.standing_holders(issues)).reason)

    def test_only_projects_at_a_bell_are_evaluated_and_standing_is_not_one(self):
        issues = {100: project(), 200: standing_project()}
        self.assertEqual([100], sorted(ct.bell_projects(issues)))
        self.assertEqual([200], ct.standing_holders(issues))


class TestStandingApprovalIsAnOrdinaryApproval(unittest.TestCase):
    """§4.1.1(1) / #314 SM-03 — approved once, superseded only by a criteria edit."""

    STANDING_APPROVAL = "approved (standing)"

    def test_adding_or_finishing_stories_does_not_ring_another_bell(self):
        """A standing project is not at a bell, whatever its stories do, so
        continuation never re-consumes or re-requires an approval for it."""
        for stories in ("#1", "#1\n#2\n#3", "", "_No response_"):
            with self.subTest(stories=stories):
                held = standing_project()
                held["body"] = held["body"].replace(
                    "### Stories\n\n#1", f"### Stories\n\n{stories}")
                outcome = ct.evaluate_project(
                    held, [approval(decision=self.STANDING_APPROVAL)], standing=[200])
                self.assertIsNone(outcome.action)
                self.assertEqual(R.NOT_AT_A_BELL, outcome.reason)
                self.assertEqual(ct.STANDING, outcome.detail)

    def test_editing_a_standing_projects_criteria_still_voids_its_approval(self):
        """§5.2 returns it to `awaiting-ready`; re-approval then meets §5.1's
        binding rule unchanged — the amended criteria do not match what was
        approved, so nothing advances."""
        amended = project(criteria="- [ ] does the thing\n- [ ] and something new")
        outcome = ct.evaluate_project(
            amended, [approval(criteria=CRITERIA, decision=self.STANDING_APPROVAL)],
            standing=[])
        self.assertIsNone(outcome.action)
        self.assertEqual(R.CRITERIA_CHANGED, outcome.reason)

    def test_re_approval_after_a_criteria_edit_is_allowed_back_in(self):
        outcome = ct.evaluate_project(
            project(), [approval(decision=self.STANDING_APPROVAL)], standing=[])
        self.assertEqual(ct.STANDING, outcome.action)


class TestStandingRefusalWritesNothing(unittest.TestCase):
    """SM-06 — "applies no partial transition" has to be true of the write path,
    not only of the decision."""

    def pass_over(self, issues, comments, apply_outcome):
        with mock.patch.object(ct, "fetch_projects", return_value=issues), \
             mock.patch.object(ct, "fetch_comments", side_effect=lambda r, n, t: comments.get(n, [])), \
             mock.patch.object(ct, "fetch_bell_at",
                               return_value="2026-08-22T01:00:00Z"), \
             mock.patch.object(ct, "apply_outcome", side_effect=apply_outcome) as applied:
            advanced = ct.run("owner/repo", "token")
        return advanced, applied

    def test_a_refused_second_standing_project_never_reaches_github(self):
        issues = {100: project(), 200: standing_project()}
        comments = {100: [approval(decision="approved (standing)")]}
        advanced, applied = self.pass_over(issues, comments, lambda *a: (True, "written"))
        self.assertEqual([], advanced)
        self.assertEqual(0, applied.call_count, "the refusal must write nothing")

    def test_replaying_the_pass_creates_no_second_anything(self):
        issues = {100: project(), 200: standing_project()}
        comments = {100: [approval(decision="approved (standing)")]}
        for _ in range(2):
            advanced, applied = self.pass_over(issues, comments, lambda *a: (True, "written"))
            self.assertEqual([], advanced)
            self.assertEqual(0, applied.call_count)
        self.assertEqual({100: ct.AWAITING_READY, 200: ct.STANDING},
                         {n: ct.lifecycle_of(i) for n, i in issues.items()})

    def test_the_first_standing_project_is_applied_in_the_same_pass_shape(self):
        issues = {100: project()}
        comments = {100: [approval(decision="approved (standing)")]}
        advanced, applied = self.pass_over(issues, comments, lambda *a: (True, "written"))
        self.assertEqual([ct.STANDING], [o.action for o in advanced])
        self.assertEqual(1, applied.call_count)

    def test_a_race_lost_between_evaluation_and_write_is_refused_at_the_write(self):
        """Two passes may both decide; only one may write. The label re-read is
        what makes that true, exactly as it is for the lifecycle itself."""
        p = project()
        outcome = ct.evaluate_project(p, [approval(decision="approved (standing)")], [])
        self.assertEqual(ct.STANDING, outcome.action)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(p).encode()
        with mock.patch.object(ct, "fetch_standing", return_value=[200]), \
             mock.patch("urllib.request.urlopen", return_value=response) as github:
            ok, note = ct.apply_outcome("owner/repo", p, outcome, "token")
        self.assertFalse(ok)
        self.assertIn("#200", note)
        self.assertEqual(1, github.call_count, "only the freshness read")
        self.assertEqual("GET", github.call_args.args[0].get_method())

    def test_the_uncontested_write_still_happens_and_carries_the_standing_label(self):
        p = project()
        outcome = ct.evaluate_project(p, [approval(decision="approved (standing)")], [])
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(p).encode()
        with mock.patch.object(ct, "fetch_standing", return_value=[]), \
             mock.patch.object(ct, "ensure_plan_approval_touch"), \
             mock.patch("urllib.request.urlopen", return_value=response) as github:
            ok, note = ct.apply_outcome("owner/repo", p, outcome, "token")
        self.assertTrue(ok)
        self.assertEqual(f"{ct.AWAITING_READY} -> {ct.STANDING}", note)
        patch = [call for call in github.call_args_list
                 if call.args[0].get_method() == "PATCH"]
        self.assertEqual(1, len(patch))
        payload = json.loads(patch[0].args[0].data.decode())
        self.assertIn(ct.STANDING, payload["labels"])
        self.assertNotIn(ct.AWAITING_READY, payload["labels"])
        self.assertNotIn("state", payload, "a standing project is never closed")

    def test_the_continue_line_names_the_standing_target(self):
        line = ct.continuation_line(ct.Outcome(314, ct.STANDING, R.CONTINUED))
        self.assertEqual("CONTINUE project=#314 to=project:standing", line)


class TestAmbiguity(unittest.TestCase):
    def test_conflicting_owner_decisions_fail_closed(self):
        outcome = ct.evaluate_project(
            project(), [approval(), approval(decision="changes-requested")])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, R.CONFLICTING_DECISIONS)

    def test_repeated_identical_decisions_are_not_a_conflict(self):
        outcome = ct.evaluate_project(project(), [approval(), approval()])
        self.assertEqual(outcome.action, ct.ACTIVE)

    def test_prior_bell_changes_request_does_not_conflict_with_current_approval(self):
        earlier = approval(decision="changes-requested")
        earlier["created_at"] = "2026-08-22T00:59:00Z"
        outcome = ct.evaluate_project(project(), [earlier, approval()])
        self.assertEqual(outcome.action, ct.ACTIVE)

    def test_conflicting_plan_decisions_within_current_bell_still_fail_closed(self):
        outcome = ct.evaluate_project(
            project(), [approval(), approval(decision="changes-requested")])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, R.CONFLICTING_DECISIONS)

    def test_missing_or_malformed_plan_bell_time_fails_closed(self):
        missing = project()
        del missing["_bell_at"]
        self.assertEqual(ct.evaluate_project(missing, [approval()]).reason,
                         R.BELL_TIME_UNAVAILABLE)
        malformed = project()
        malformed["_bell_at"] = "not-a-time"
        self.assertEqual(ct.evaluate_project(malformed, [approval()]).reason,
                         R.BELL_TIME_UNAVAILABLE)

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

    def test_corrective_cycle_can_pass_without_deleting_earlier_failure(self):
        """§5.3: fail -> active -> corrective work -> new bell -> pass.

        Both comments remain present. The later result governs the new bell,
        and replay after acceptance is inert because the lifecycle left it.
        """
        comments = [acceptance(result="fail", created_at="2026-08-22T00:59:00Z"),
                    acceptance(result="pass", created_at="2026-08-22T01:01:00Z")]
        outcome = ct.evaluate_project(project(state=ct.AWAITING_ACCEPTANCE), comments)
        self.assertEqual(outcome.action, ct.ACCEPTED)
        self.assertEqual(2, len(comments), "the earlier audit record must remain")
        after = project(state=ct.ACCEPTED)
        self.assertEqual(ct.evaluate_project(after, comments).reason, R.NOT_AT_A_BELL)

    def test_latest_failure_after_an_earlier_pass_returns_to_active(self):
        outcome = ct.evaluate_project(
            project(state=ct.AWAITING_ACCEPTANCE),
            [acceptance(result="pass", created_at="2026-08-22T00:59:00Z"),
             acceptance(result="fail", created_at="2026-08-22T01:01:00Z")])
        self.assertEqual(outcome.action, ct.ACTIVE)

    def test_conflicting_results_within_one_bell_fail_closed(self):
        outcome = ct.evaluate_project(
            project(state=ct.AWAITING_ACCEPTANCE),
            [acceptance(result="fail", created_at="2026-08-22T01:01:00Z"),
             acceptance(result="pass", created_at="2026-08-22T01:02:00Z")])
        self.assertIsNone(outcome.action)
        self.assertEqual(outcome.reason, R.CONFLICTING_DECISIONS)

    def test_missing_or_malformed_bell_and_comment_times_fail_closed(self):
        missing = project(state=ct.AWAITING_ACCEPTANCE)
        del missing["_bell_at"]
        self.assertEqual(ct.evaluate_project(missing, [acceptance()]).reason,
                         R.BELL_TIME_UNAVAILABLE)
        malformed = acceptance(created_at="not-a-time")
        self.assertEqual(ct.evaluate_project(
            project(state=ct.AWAITING_ACCEPTANCE), [malformed]).reason,
            R.BELL_TIME_UNAVAILABLE)

    def test_approval_comment_does_not_satisfy_the_acceptance_bell(self):
        outcome = ct.evaluate_project(project(state=ct.AWAITING_ACCEPTANCE), [approval()])
        self.assertIsNone(outcome.action)

    def test_non_owner_acceptance_ignored(self):
        outcome = ct.evaluate_project(project(state=ct.AWAITING_ACCEPTANCE),
                                      [comment(acceptance()["body"], "NONE")])
        self.assertEqual(outcome.reason, R.NOT_OWNER_AUTHORED)


class TestIdempotencyAndAuthority(unittest.TestCase):
    AT07_LIMITATION = {
        "timestamp": "2026-08-22T19:41:17Z",
        "project": "#294",
        "story": "#295",
        "bell_type": "scope-decision",
        "classification": "decision",
        "seconds_spent": 0,
        "note": ("Owner-approved temporary AT-07 sequencing limitation; "
                 "decision-comment:5382246368"),
        "actor": "@maheshhbhat",
    }
    STORY_295_RESCUE = {
        "timestamp": "2026-08-22T19:41:18Z",
        "project": "#294",
        "story": "#295",
        "bell_type": "poison-rescue",
        "classification": "rescue",
        "seconds_spent": 0,
        "note": ("Owner-approved rescue 1 for Story #295 after review findings were "
                 "addressed and temporary AT-07 sequencing limitation recorded; exact "
                 "time not supplied."),
        "actor": "@maheshhbhat",
    }

    def assert_at07_decisions_are_distinct(self, rows):
        limitation = [row for row in rows
                      if row.get("project") == "#294"
                      and row.get("story") == "#295"
                      and row.get("bell_type") == "scope-decision"]
        rescue = [row for row in rows
                  if row.get("project") == "#294"
                  and row.get("story") == "#295"
                  and row.get("bell_type") == "poison-rescue"]
        self.assertEqual(limitation, [self.AT07_LIMITATION])
        self.assertEqual(rescue, [self.STORY_295_RESCUE])

    def test_story_295_at07_limitation_and_rescue_have_distinct_canonical_receipts(self):
        path = pathlib.Path(ct.__file__).resolve().parents[1] / "touchlog" / "touchlog.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        self.assert_at07_decisions_are_distinct(rows)

    def test_story_295_receipt_check_rejects_every_claimed_corruption(self):
        good = [dict(self.AT07_LIMITATION), dict(self.STORY_295_RESCUE)]
        corruptions = {
            "missing": good[1:],
            "duplicate": good + [dict(self.AT07_LIMITATION)],
            "conflated": [{**good[1], "bell_type": "scope-decision",
                            "classification": "decision"}],
            "mistyped": [{**good[0], "classification": "rescue"}, good[1]],
            "wrong timestamp": [{**good[0], "timestamp": "2026-08-22T19:41:18Z"},
                                good[1]],
        }
        for name, rows in corruptions.items():
            with self.subTest(name=name), self.assertRaises(AssertionError):
                self.assert_at07_decisions_are_distinct(rows)

    def test_story_296_scope_decision_has_one_canonical_receipt(self):
        path = pathlib.Path(ct.__file__).resolve().parents[1] / "touchlog" / "touchlog.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        found = [row for row in rows
                 if row.get("project") == "#294" and row.get("story") == "#296"
                 and row.get("bell_type") == "scope-decision"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["timestamp"], "2026-08-22T20:42:12Z")
        self.assertIn("decision-comment:5382498474", found[0]["note"])

    def test_identical_decisions_on_two_projects_get_distinct_replay_safe_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "touchlog.jsonl"
            with mock.patch.dict(os.environ, {"FACTORY_TOUCHLOG_FILE": str(path)}):
                decision = acceptance()
                for number in (304, 307, 304, 307):
                    ct.ensure_acceptance_touch(number, decision)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row["project"] for row in rows], ["#304", "#307"])
        self.assertEqual(len({row["note"].split(";", 1)[0] for row in rows}), 2)
        self.assertEqual(len({ct.acceptance_identity(decision)[0]}), 1)

    def test_plan_approval_writes_one_receipt_and_replay_writes_none(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "touchlog.jsonl"
            with mock.patch.dict(os.environ, {"FACTORY_TOUCHLOG_FILE": str(path)}):
                decision = approval()
                for _ in range(2):
                    ct.ensure_plan_approval_touch(375, decision)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(1, len(rows))
        self.assertEqual("plan-approval", rows[0]["bell_type"])
        self.assertEqual("#375", rows[0]["project"])

    def test_missing_plan_approval_receipt_prevents_state_write(self):
        p = project()
        outcome = ct.evaluate_project(p, [approval()])
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(p).encode()
        with mock.patch.object(ct, "ensure_plan_approval_touch",
                               side_effect=ct.TouchEvidenceError("disk full")), \
             mock.patch("urllib.request.urlopen", return_value=response) as github:
            with self.assertRaisesRegex(ct.TouchEvidenceError, "disk full"):
                ct.apply_outcome("owner/repo", p, outcome, "token")
        self.assertEqual(1, github.call_count, "freshness read only; no state write")

    def test_touch_failure_allows_fresh_read_but_prevents_github_write(self):
        p = project(state=ct.AWAITING_ACCEPTANCE)
        outcome = ct.evaluate_project(p, [acceptance()])
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(p).encode()
        with mock.patch.object(ct, "ensure_acceptance_touch",
                               side_effect=ct.TouchEvidenceError("disk full")), \
             mock.patch("urllib.request.urlopen", return_value=response) as github:
            with self.assertRaisesRegex(ct.TouchEvidenceError, "disk full"):
                ct.apply_outcome("owner/repo", p, outcome, "token")
        self.assertEqual(github.call_count, 1)
        self.assertEqual(github.call_args.args[0].get_method(), "GET")

    def test_advanced_project_is_no_longer_at_a_bell(self):
        """Consumption needs no cursor: the transition itself removes the project
        from the pass's view, so a replay sees nothing to do."""
        after = project(state=ct.ACTIVE)
        self.assertEqual(ct.evaluate_project(after, [approval()]).reason, R.NOT_AT_A_BELL)

    def test_evaluation_is_pure_and_repeatable(self):
        p, c = project(), [approval()]
        self.assertEqual(ct.evaluate_project(p, c), ct.evaluate_project(p, c))

    def test_acceptance_evidence_is_not_a_decision_cursor(self):
        """GitHub remains authoritative; the file is only a verified receipt."""
        outcome = ct.evaluate_project(project(state=ct.AWAITING_ACCEPTANCE), [acceptance()])
        self.assertEqual(outcome.action, ct.ACCEPTED)
        self.assertIsNotNone(outcome.decision)

    def test_continuation_line_is_worker_neutral(self):
        line = ct.continuation_line(ct.Outcome(72, ct.ACTIVE, R.CONTINUED))
        self.assertEqual(line, "CONTINUE project=#72 to=project:active")
        for leaked in ("claude", "codex", "agent"):
            self.assertNotIn(leaked, line.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
