import unittest
from unittest import mock

import planning_route as route


def issue(number, lifecycle="project:ready-for-planning", association="OWNER"):
    return {"number": number, "author_association": association,
            "labels": [{"name": "type:project"}, {"name": lifecycle}]}


class PlanningRouteTests(unittest.TestCase):
    def test_only_trusted_ready_or_planning_projects_are_selected_in_order(self):
        issues = {3: issue(3), 1: issue(1), 2: issue(2, association="NONE"),
                  4: issue(4, "project:planning")}
        self.assertEqual([1, 3, 4], route.select(issues))

    def test_claim_rechecks_and_replaces_lifecycle_atomically(self):
        issues = {1: issue(1)}
        with mock.patch.object(route.dispatcher, "fetch_issues", return_value=issues), \
             mock.patch.object(route.dispatcher, "fetch_issue", return_value=issues[1]), \
             mock.patch.object(route.dispatcher, "_api") as api:
            self.assertEqual([1], route.run("o/r", "token"))
        payload = api.call_args.kwargs["payload"]
        self.assertEqual(["project:planning", "type:project"], payload["labels"])

    def test_planning_state_is_reinvoked_without_another_label_write(self):
        issues = {1: issue(1)}
        with mock.patch.object(route.dispatcher, "fetch_issues", return_value=issues), \
             mock.patch.object(route.dispatcher, "fetch_issue",
                               return_value=issue(1, "project:planning")), \
             mock.patch.object(route.dispatcher, "_api") as api:
            self.assertEqual([1], route.run("o/r", "token"))
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
