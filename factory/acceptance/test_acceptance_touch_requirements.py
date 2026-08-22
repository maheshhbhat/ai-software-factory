import json
import pathlib
import tempfile
import unittest

import acceptance_touch_requirements as requirements


class AcceptanceTouchRequirementTests(unittest.TestCase):
    def evidence(self, value):
        temp = tempfile.NamedTemporaryFile(mode="w", delete=False)
        json.dump(value, temp); temp.close()
        self.addCleanup(pathlib.Path(temp.name).unlink, missing_ok=True)
        return pathlib.Path(temp.name)

    def test_every_at_criterion_has_a_named_existing_test(self):
        value = {"criteria": {key: "pass" for key in requirements.AT},
                 "fixture": {"before": "project:awaiting-acceptance",
                             "after": "project:accepted"},
                 "replay": {"new_entries": 0, "transitions": 0}, "touch": {"x": 1},
                 "delivery": {"story": 296, "story_state": "story:merged",
                              "pr_state": "MERGED", "checks": {
                                  "merge-gate": "SUCCESS",
                                  "merge-gate-surface": "SUCCESS"}}}
        path = self.evidence(value)
        self.assertEqual(requirements.build(path)["stale"], [])

    def test_missing_live_evidence_fails_the_ladder(self):
        report = requirements.build(self.evidence({"criteria": {}}))
        expected = {key for key, (_, live) in requirements.AT.items() if live}
        self.assertEqual(set(report["missing_live"]), expected)

    def test_delivery_evidence_requires_merged_story_and_both_checks(self):
        base = {"criteria": {"AT-08": "pass"}, "delivery": {
            "story": 296, "story_state": "story:merged", "pr_state": "MERGED",
            "checks": {"merge-gate": "SUCCESS", "merge-gate-surface": "SUCCESS"}}}
        self.assertTrue(requirements.live_passes("AT-08", base))
        for mutation in (
                {"story_state": "story:in-review"}, {"pr_state": "OPEN"},
                {"checks": {"merge-gate": "SUCCESS"}}):
            changed = json.loads(json.dumps(base))
            changed["delivery"].update(mutation)
            self.assertFalse(requirements.live_passes("AT-08", changed))

    def test_live_fixture_evidence_fails_without_transition_touch_or_clean_replay(self):
        base = {"criteria": {"AT-07": "pass"},
                "fixture": {"before": "project:awaiting-acceptance",
                            "after": "project:accepted"},
                "replay": {"new_entries": 0, "transitions": 0}, "touch": {"x": 1}}
        self.assertTrue(requirements.live_passes("AT-07", base))
        base["replay"]["new_entries"] = 1
        self.assertFalse(requirements.live_passes("AT-07", base))


if __name__ == "__main__":
    unittest.main()
