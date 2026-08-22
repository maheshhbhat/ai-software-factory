"""Pins the temporary shared-credential boundary approved for Project #212."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory" / "gates"))
import merge_gate as gate


def story(*labels: str) -> dict:
    return {
        "labels": [{"name": "type:story"}, *({"name": item} for item in labels)],
        "body": "### Scope\n\nsrc/**\n\n### Attempt\n\n0\n",
    }


class SharedCredentialBoundaryTests(unittest.TestCase):
    def evaluate(self, *, tests=True, paths=None, labels=()):
        return gate.evaluate(
            pr_body="Story: #222\n",
            changed_paths=paths or ["src/health.py"],
            story=story(*labels),
            tests_passed=tests,
        )

    def test_forgeable_artifacts_cannot_turn_red_tests_green(self):
        verdict = self.evaluate(
            tests=False,
            labels=("review:approved@deadbeef", "test-change", "hazard-ack"),
        )
        self.assertIn(gate.Violation.TESTS_FAILED, verdict.codes())

    def test_forgeable_artifacts_cannot_expand_story_scope(self):
        verdict = self.evaluate(
            paths=["factory/spec/state-schema.md"],
            labels=("review:approved@deadbeef", "test-change", "hazard-ack"),
        )
        self.assertIn(gate.Violation.OUT_OF_SCOPE, verdict.codes())

    def test_gate_does_not_require_review_artifact(self):
        verdict = self.evaluate()
        self.assertTrue(verdict.passed, verdict.codes())

    def test_withdrawal_remains_in_authoritative_spec(self):
        schema = (ROOT / "factory" / "spec" / "state-schema.md").read_text()
        self.assertIn("Under a shared credential the gate must derive its verdict", schema)
        self.assertIn("review-approval artifact bound to the head SHA", schema)
        self.assertIn("so it is **withdrawn**", schema)
        self.assertIn("### 9.17 Withdrawn merge-gate checks", schema)

    def test_adr_names_all_accepted_risks_and_exit_project(self):
        adr = (ROOT / "factory" / "decisions" /
               "phase4-shared-credential.md").read_text()
        for phrase in ("Impersonation", "Self-certification", "Audit attribution",
                       "Shared compromise", "Prompt injection", "Project #221"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, adr)


if __name__ == "__main__":
    unittest.main()
