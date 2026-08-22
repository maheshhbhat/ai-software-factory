import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC = importlib.util.spec_from_file_location("phase4_requirement_coverage",
                                              HERE / "requirement_coverage.py")
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)
sys.path.insert(0, str(ROOT / "factory" / "runtime"))
import review_link

LIVE_SPEC = importlib.util.spec_from_file_location("phase4_live_for_requirements",
                                                   HERE / "phase4_live.py")
phase4_live = importlib.util.module_from_spec(LIVE_SPEC)
LIVE_SPEC.loader.exec_module(phase4_live)

class Phase4RequirementCoverageTests(unittest.TestCase):
    def test_phase4_delivery_evidence_requires_project_story_and_both_checks(self):
        # The hermetic half of P4-15 proves that its live evidence collector is
        # tied to this Project and cannot report delivery with only one green
        # check.  The checked-in live ledger supplies the repository observation.
        self.assertIn("#212", phase4_live.story_body())
        both = mock.Mock(returncode=0, stdout=json.dumps([
            {"name": "merge-gate", "bucket": "pass"},
            {"name": "merge-gate-surface", "bucket": "pass"},
        ]))
        with mock.patch.object(phase4_live, "command", return_value=both):
            self.assertEqual(len(phase4_live.checks_pass(280, timeout=1)), 2)

        only_required = mock.Mock(returncode=0, stdout=json.dumps([
            {"name": "merge-gate", "bucket": "pass"},
        ]))
        with mock.patch.object(phase4_live, "command", return_value=only_required), \
             mock.patch.object(phase4_live.time, "time", side_effect=[0, 0, 2]), \
             mock.patch.object(phase4_live.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                phase4_live.checks_pass(280, timeout=1)

    def test_claimed_story_routes_through_linked_pr_before_merge(self):
        merged = {"number": 50, "state": "closed", "merged_at": "now",
                  "body": "Story: #218\n", "head": {"sha": "a" * 40}}
        claimed = {"number": 218, "labels": [{"name": "story:claimed"}]}
        first = review_link.reconcile(claimed, [merged])
        self.assertEqual(first.action, "story:in-review")
        reviewed = {"number": 218, "labels": [{"name": first.action}]}
        self.assertEqual(review_link.reconcile(reviewed, [merged]).action, "story:merged")

    def test_every_criterion_has_named_hermetic_evidence(self):
        report = coverage.build_phase4(pathlib.Path("/definitely/missing"))
        self.assertEqual(set(coverage.PHASE4), {f"P4-{n:02d}" for n in range(1, 17)})
        self.assertEqual(report["missing_acceptance"], [])
        self.assertEqual(report["stale_declarations"], [])

    def test_wiring_claims_fail_until_live_evidence_exists(self):
        report = coverage.build_phase4(pathlib.Path("/definitely/missing"))
        expected = {key for key, (_, required) in coverage.PHASE4.items() if required}
        self.assertEqual(set(report["missing_live"]), expected)

    def test_complete_live_ledger_satisfies_wiring_claims(self):
        required = {key: "pass" for key, (_, live) in coverage.PHASE4.items() if live}
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "evidence.json"
            path.write_text(json.dumps({"criteria": required, "limitations": {}}))
            self.assertEqual(coverage.build_phase4(path)["missing_live"], [])

    def test_limitation_requires_owner_approval_reason_substitute_and_risk(self):
        key = next(key for key, (_, live) in coverage.PHASE4.items() if live)
        limitation = {"owner_approved": True, "why": "unreachable",
                      "substitute_evidence": "hermetic test", "residual_risk": "wiring"}
        required = {item: "pass" for item, (_, live) in coverage.PHASE4.items()
                    if live and item != key}
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "evidence.json"
            path.write_text(json.dumps({"criteria": required, "limitations": {key: limitation}}))
            self.assertNotIn(key, coverage.build_phase4(path)["missing_live"])
            del limitation["residual_risk"]
            path.write_text(json.dumps({"criteria": required, "limitations": {key: limitation}}))
            self.assertIn(key, coverage.build_phase4(path)["missing_live"])

    def test_shared_credential_boundary_is_explicit(self):
        adr = (ROOT / "factory" / "decisions" / "phase4-shared-credential.md").read_text()
        self.assertIn("behavioral isolation only", adr)
        self.assertIn("Project #221", adr)
        self.assertIn("not a `state-schema.md` §9.14 trust input", adr)

    def test_phase_boundary_excludes_product_stories(self):
        adr = (ROOT / "factory" / "decisions" / "phase4-live-fixture.md").read_text()
        self.assertIn("must never use", adr)
        self.assertIn("Stories #5–#12", adr)
        self.assertFalse((ROOT / "product.md").exists())

if __name__ == "__main__": unittest.main()
