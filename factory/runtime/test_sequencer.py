import io
import os
import unittest
from unittest import mock

import sequencer as sq

SCHEMA = os.path.join(sq.HERE, "..", "spec", "state-schema.md")


def issue(number, kind, lifecycle, body="", association="OWNER"):
    return {"number": number, "body": body, "author_association": association,
            "state": "OPEN", "labels": [{"name": kind}, {"name": lifecycle}]}


def story(number, lifecycle="story:blocked", deps="none", project=10,
          association="OWNER"):
    return issue(number, "type:story", lifecycle,
                 f"### Project\n\n#{project}\n\n### Depends-on\n\n{deps}\n",
                 association)


def project(number=10, lifecycle="project:active", stories="_No response_"):
    return issue(number, "type:project", lifecycle,
                 f"### Stories\n\n{stories}\n\n### Roadmap commitment\n\n#17\n")


class StoryReadinessTests(unittest.TestCase):
    def test_dependency_free_story_advances(self):
        issues = {10: project(), 20: story(20)}
        self.assertEqual([20], [d.number for d in sq.plan_story_readiness(issues, 2)])

    def test_terminal_success_dependencies_advance(self):
        for terminal in ("story:merged", "story:completed"):
            with self.subTest(terminal=terminal):
                issues = {10: project(), 20: story(20, terminal),
                          21: story(21, deps="#20")}
                self.assertEqual([21], [d.number for d in sq.plan_story_readiness(issues, 2)])

    def test_unmet_and_untrusted_dependencies_do_not_advance(self):
        rejected = (story(20), story(20, "story:cancelled"),
                    story(20, "story:blocked:poison"),
                    story(20, "story:blocked:scope"),
                    story(20, "story:merged", association="NONE"))
        for dependency in rejected:
            with self.subTest(labels=dependency["labels"]):
                issues = {10: project(), 20: dependency, 21: story(21, deps="#20")}
                self.assertNotIn(21, [
                    d.number for d in sq.plan_story_readiness(issues, 2)])

    def test_missing_malformed_and_cyclic_dependencies_do_not_advance(self):
        cases = [
            {10: project(), 21: story(21, deps="#99")},
            {10: project(), 21: story(21, deps="- #20")},
            {10: project(), 20: story(20, deps="#21"), 21: story(21, deps="#20")},
        ]
        for issues in cases:
            with self.subTest(issues=issues):
                self.assertEqual([], sq.plan_story_readiness(issues, 2))

    def test_untrusted_story_or_project_does_not_advance(self):
        untrusted_story = story(20, association="NONE")
        untrusted_project = project()
        untrusted_project["author_association"] = "NONE"
        self.assertEqual([], sq.plan_story_readiness(
            {10: project(), 20: untrusted_story}, 2))
        self.assertEqual([], sq.plan_story_readiness(
            {10: untrusted_project, 20: story(20)}, 2))

    def test_wip_and_order_are_deterministic(self):
        issues = {10: project(), 20: story(20, "story:claimed"),
                  23: story(23), 21: story(21), 22: story(22)}
        self.assertEqual([21], [d.number for d in sq.plan_story_readiness(issues, 2)])

    def test_selected_commitment_does_not_advance_another_projects_story(self):
        selected = project(10)
        other = project(11)
        other["body"] = other["body"].replace("#17", "#99")
        issues = {10: selected, 11: other,
                  20: story(20, project=10), 21: story(21, project=11)}
        self.assertEqual(
            [20],
            [d.number for d in sq.plan_story_readiness(issues, 2, commitment=17)])

    def test_closed_claim_does_not_consume_wip_but_open_claim_does(self):
        closed_claim = story(19, "story:claimed")
        closed_claim["state"] = "CLOSED"
        issues = {10: project(), 19: closed_claim, 20: story(20, "story:claimed"),
                  21: story(21), 22: story(22)}
        self.assertEqual([21], [d.number for d in sq.plan_story_readiness(issues, 2)])
        dispatch_plan = sq.dispatcher.plan_dispatch(
            {n: value for n, value in issues.items() if n != 10}, {10: issues[10]},
            commitment=54, wip_limit=2,
            dependencies=sq.dispatcher.DependencyIndex(issues))
        self.assertEqual(1, dispatch_plan.wip_in_use)


class ProjectCompletionTests(unittest.TestCase):
    def test_all_declared_terminal_successes_advance_project(self):
        issues = {10: project(stories="#20\n#21"),
                  20: story(20, "story:merged"),
                  21: story(21, "story:completed")}
        self.assertEqual([10], [d.number for d in sq.plan_project_completion(issues)])

    def test_readiness_is_advisory_until_promoted_then_fail_closed(self):
        issues = {10: project(stories="#20"),
                  20: story(20, "story:merged")}
        issues[10]["body"] += "\n### Operating envelope\n\nNone identified.\n"
        self.assertEqual([10], [d.number for d in sq.plan_project_completion(
            issues, readiness_mode="warning")])
        self.assertEqual([], sq.plan_project_completion(
            issues, readiness_mode="blocking"))
        self.assertEqual(sq.Skip.READINESS_MISSING, sq.completion_skip(
            issues[10], issues, readiness_mode="blocking"))
        self.assertEqual([10], [d.number for d in sq.plan_project_completion(
            issues, readiness_mode="blocking",
            readiness_artifacts={10: {"overall": "ready"}})])

    def test_selected_commitment_does_not_complete_another_project(self):
        selected = project(10, stories="#20")
        other = project(11, stories="#21")
        other["body"] = other["body"].replace("#17", "#99")
        issues = {10: selected, 11: other,
                  20: story(20, "story:merged", project=10),
                  21: story(21, "story:merged", project=11)}
        self.assertEqual(
            [10],
            [d.number for d in sq.plan_project_completion(issues, commitment=17)])

    def test_nonterminal_cancelled_missing_or_empty_children_do_not_advance(self):
        cases = [
            {10: project(stories="#20"), 20: story(20)},
            {10: project(stories="#20"), 20: story(20, "story:cancelled")},
            {10: project(stories="#20")},
            {10: project()},
        ]
        for issues in cases:
            with self.subTest(issues=issues):
                self.assertEqual([], sq.plan_project_completion(issues))

    def test_repeated_run_is_idempotent_after_first_write(self):
        issues = {10: project(), 20: story(20)}
        with mock.patch.object(sq, "fetch_all_issues", side_effect=[issues, {
                10: project(), 20: story(20, "story:ready")}]), \
             mock.patch.object(sq, "apply_decision", return_value=(True, "ok")) as apply:
            self.assertEqual(1, len(sq.run("o/r", "token")))
            self.assertEqual([], sq.run("o/r", "token"))
            self.assertEqual(1, apply.call_count)

    def test_run_evaluates_enveloped_candidate_before_completion(self):
        issues = {10: project(stories="#20"),
                  20: story(20, "story:merged")}
        issues[10]["body"] += "\n### Operating envelope\n\nNone identified.\n"
        with mock.patch.object(sq, "fetch_all_issues", return_value=issues), \
             mock.patch.object(sq, "run_production_readiness",
                               return_value=True) as evaluate, \
             mock.patch.object(sq, "apply_decision",
                               return_value=(True, "ok")):
            decisions = sq.run("o/r", "token")
        self.assertEqual([10], [item.number for item in decisions])
        evaluate.assert_called_once_with("o/r", 10, "token")

    def test_dry_run_never_invokes_or_publishes_readiness(self):
        issues = {10: project(stories="#20"),
                  20: story(20, "story:merged")}
        issues[10]["body"] += "\n### Operating envelope\n\nNone identified.\n"
        with mock.patch.object(sq, "fetch_all_issues", return_value=issues), \
             mock.patch.object(sq, "run_production_readiness") as evaluate:
            sq.run("o/r", "token", apply=False)
        evaluate.assert_not_called()

    def test_warning_mode_evaluator_crash_is_recorded_but_not_a_hidden_gate(self):
        issues = {10: project(stories="#20"),
                  20: story(20, "story:merged")}
        issues[10]["body"] += "\n### Operating envelope\n\nNone identified.\n"
        with mock.patch.object(sq, "fetch_all_issues", return_value=issues), \
             mock.patch.object(sq, "run_production_readiness",
                               side_effect=TimeoutError("bounded timeout")), \
             mock.patch.object(sq, "apply_decision",
                               return_value=(True, "ok")), \
             mock.patch.dict(os.environ,
                             {"FACTORY_PRODUCTION_READINESS_MODE": "warning"}), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            decisions = sq.run("o/r", "token")
        self.assertEqual([10], [item.number for item in decisions])
        self.assertIn("could not evaluate", output.getvalue())

    def test_blocking_mode_rechecks_integrated_revision_before_project_write(self):
        decision = sq.Decision(10, sq.ACTIVE, sq.AWAITING_ACCEPTANCE, "ready")
        with mock.patch.object(sq.dispatcher, "fetch_issue",
                               return_value=project(10, stories="#20")), \
             mock.patch.object(sq, "_integrated_revision",
                               return_value="b" * 40), \
             mock.patch.object(sq.dispatcher, "_api") as api:
            ok, note = sq.apply_decision(
                "o/r", decision, "token", required_revision="a" * 40)
        self.assertFalse(ok)
        self.assertEqual("integrated revision changed before write", note)
        api.assert_not_called()


class StandingProjectTests(unittest.TestCase):
    """§4.1.1 — a standing project is continuous work and has no acceptance edge.

    The point of these tests is *why* it is excluded, not just that it is. The
    sequencer already skips a project whose `### Stories` section is empty or
    unparseable, so a standing project with an empty Stories section would be
    left alone for the wrong reason — and #298 is removing that skip, which would
    then silently turn the standing state back on. Every case below therefore
    declares real, parseable, finished stories.
    """

    FINISHED = "#20\n#21"

    def shaped(self, lifecycle, stories=FINISHED):
        """One project shape, varying only the lifecycle label."""
        return {10: project(lifecycle=lifecycle, stories=stories),
                20: story(20, "story:merged"), 21: story(21, "story:completed")}

    def test_the_shared_shape_would_otherwise_advance(self):
        """Pins the premise: nothing but the label distinguishes these cases."""
        issues = self.shaped(sq.STANDING)
        self.assertEqual(([20, 21], None),
                         sq._references(issues[10]["body"], "Stories"))
        for number in (20, 21):
            self.assertIn(
                sq.dispatcher.lifecycle_of(issues[number], sq.dispatcher.STORY_LIFECYCLE),
                sq.dispatcher.TERMINAL_SUCCESS)

    def test_standing_project_is_excluded_and_says_it_is_for_being_standing(self):
        issues = self.shaped(sq.STANDING)
        self.assertEqual([], sq.plan_project_completion(issues))
        self.assertEqual(sq.Skip.STANDING, sq.completion_skip(issues[10], issues))

    def test_the_identical_bounded_project_still_advances(self):
        issues = self.shaped("project:active")
        self.assertIsNone(sq.completion_skip(issues[10], issues))
        self.assertEqual([sq.AWAITING_ACCEPTANCE],
                         [d.target for d in sq.plan_project_completion(issues)])

    def test_no_state_of_its_stories_advances_a_standing_project(self):
        """§4.1.1: excluded whatever the stories do — and always for the same reason."""
        for stories in (self.FINISHED, "#20", "#99", "_No response_", "- #20"):
            with self.subTest(stories=stories):
                issues = self.shaped(sq.STANDING, stories)
                self.assertEqual([], sq.plan_project_completion(issues))
                self.assertEqual(sq.Skip.STANDING,
                                 sq.completion_skip(issues[10], issues))

    def test_a_second_project_label_leaves_no_lifecycle_at_all(self):
        """§2.1 — standing is exclusive of the bounded states by construction."""
        for other in ("project:active", "project:awaiting-acceptance",
                      "project:accepted", "project:awaiting-ready"):
            with self.subTest(other=other):
                issues = self.shaped(sq.STANDING)
                issues[10]["labels"].append({"name": other})
                self.assertIsNone(sq.dispatcher.lifecycle_of(
                    issues[10], sq.dispatcher.PROJECT_LIFECYCLE))
                self.assertEqual(sq.Skip.NOT_ACTIVE,
                                 sq.completion_skip(issues[10], issues))
                self.assertEqual([], sq.plan_project_completion(issues))

    def test_the_standing_label_is_the_one_the_schema_defines(self):
        with open(SCHEMA, encoding="utf-8") as handle:
            schema = handle.read()
        self.assertIn(f"`{sq.STANDING}`", schema, "§2.1 must define the label")
        self.assertIn(f"| `{sq.STANDING}` | `project:awaiting-ready` |", schema,
                      "§4.1 must give it a supersession edge")
        self.assertNotIn(f"| `{sq.STANDING}` | `{sq.AWAITING_ACCEPTANCE}` |", schema,
                         "§4.1 must not give it a completion edge")


if __name__ == "__main__":
    unittest.main()
