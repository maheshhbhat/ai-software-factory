"""Regression tests for the canonical coverage measurement's Phase 3 scope."""

import importlib.util
import pathlib
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "coverage_report", ROOT / "factory" / "coverage_report.py")
coverage_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage_report)


class PlanningCoverageClassificationTests(unittest.TestCase):
    def test_phase4_suites_are_explicit_and_risks_are_reported(self):
        grouped = coverage_report.classify()
        for path in coverage_report.PHASE4_UNIT:
            self.assertIn(path, grouped["unit"])
        for path in coverage_report.ACCEPTANCE:
            if "phase4" in path:
                self.assertIn(path, grouped["acceptance"])
        self.assertIn("agents/worker", coverage_report.SUITES)
        self.assertIn("agents/review", coverage_report.SUITES)
        self.assertTrue(coverage_report.PHASE4_MODULES)
        self.assertTrue(coverage_report.UNCOVERED_RISKS)

    def test_planning_suite_and_every_test_are_explicitly_classified(self):
        self.assertIn("agents/planning", coverage_report.SUITES)
        found = {path for path in coverage_report.test_files()
                 if path.startswith("agents/planning/")}
        declared = coverage_report.PLANNING_UNIT | {
            path for path in coverage_report.INTEGRATION
            if path.startswith("agents/planning/")
        }
        self.assertEqual(found, declared)
        self.assertEqual("integration", coverage_report.layer_of(
            "agents/planning/test_e2e.py"))
        for path in coverage_report.PLANNING_UNIT:
            self.assertEqual("unit", coverage_report.layer_of(path))

    def test_nested_suite_runs_from_its_actual_directory(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(coverage_report.subprocess, "run", return_value=completed) as run:
            passed = coverage_report.run_layer(
                "python", pathlib.Path("/tmp/phase3-test-data"),
                ["agents/planning/test_contract.py"], {})
        self.assertTrue(passed)
        command = run.call_args.args[0]
        self.assertEqual(str(ROOT / "factory" / "agents" / "planning"),
                         command[command.index("-s") + 1])
        self.assertEqual("test_contract.py", command[command.index("-p") + 1])

    def test_unclassified_planning_test_is_a_hard_error(self):
        discovered = coverage_report.test_files() + ["agents/planning/test_new.py"]
        with mock.patch.object(coverage_report, "test_files", return_value=discovered), \
             self.assertRaisesRegex(SystemExit, "explicitly classified"):
            coverage_report.classify()


if __name__ == "__main__":
    unittest.main()
