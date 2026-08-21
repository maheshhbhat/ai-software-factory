import unittest
from unittest import mock

import sequencer as sq


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
                 f"### Stories\n\n{stories}\n")


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


if __name__ == "__main__":
    unittest.main()
