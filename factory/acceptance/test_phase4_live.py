import importlib.util
import base64
import pathlib
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("phase4_live", HERE / "phase4_live.py")
live = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(live)

class Phase4LiveHarnessTests(unittest.TestCase):
    def test_story_is_bounded_isolated_and_deliberately_two_pass(self):
        body = live.story_body()
        self.assertIn("#212", body)
        self.assertIn("### Spend cap\n\n$5 / 60 min", body)
        self.assertIn("Attempt 1 only", body)
        self.assertIn("Attempt 2", body)
        self.assertIn("runs/phase4/live_product/**", body)
        self.assertNotIn("income-portfolio", body)

    def test_runtime_worker_launch_is_an_async_handoff(self):
        with mock.patch.object(live.subprocess, "Popen") as started, \
             mock.patch.object(live.pathlib.Path, "open", mock.mock_open()):
            self.assertEqual(live.launch_worker(230), 0)
        self.assertTrue(started.call_args.kwargs["start_new_session"])

    def test_private_deploy_clone_uses_token_header_not_ssh(self):
        environment = live.clone_environment("secret")
        self.assertEqual("http.extraHeader", environment["GIT_CONFIG_KEY_0"])
        header = environment["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
        self.assertEqual("x-access-token:secret", base64.b64decode(header).decode())

    def test_lifecycle_evidence_requires_exact_order_and_repeat_counts(self):
        expected = ["story:ready", "story:claimed", "story:in-review",
                    "story:ready", "story:claimed", "story:in-review", "story:merged"]
        timeline = [{"event": "labeled", "label": label} for label in expected]
        timeline.insert(2, {"event": "labeled", "label": "phase:build"})
        self.assertEqual(expected, live.lifecycle_walk(timeline))
        self.assertNotEqual(expected, live.lifecycle_walk(timeline[:4]))

    def test_live_loop_delegates_review_and_merge_to_runtime(self):
        environment = live.poll_environment("token", pathlib.Path("runtime.jsonl"))
        self.assertEqual("1", environment["FACTORY_PHASE4_REVIEWS"])
        self.assertEqual("capacity-delivery", environment["FACTORY_WORKER_ORDER"])
        self.assertEqual(".", environment["FACTORY_RUN_DIR"])
        self.assertNotIn("FACTORY_REVIEW_MODEL_CMD", environment)
        self.assertNotIn("FACTORY_DELIVERY_MODEL_CMD", environment)
        source = pathlib.Path(live.__file__).read_text()
        self.assertNotIn('command(["gh", "pr", "ready"', source)
        self.assertNotIn('command(["gh", "pr", "merge"', source)
        self.assertNotIn("def run_review(", source)

    def test_live_wiring_criteria_are_exactly_the_checker_requirements(self):
        requirement_path = HERE / "requirement_coverage.py"
        spec = importlib.util.spec_from_file_location("phase4_requirements", requirement_path)
        requirements = importlib.util.module_from_spec(spec); spec.loader.exec_module(requirements)
        expected = {key for key, (_evidence, requires_live) in requirements.PHASE4.items()
                    if requires_live}
        self.assertEqual(expected, live.LIVE_CRITERIA)

    def test_live_harness_is_explicitly_classified(self):
        coverage_path = HERE.parents[1] / "factory" / "coverage_report.py"
        spec = importlib.util.spec_from_file_location("coverage_report", coverage_path)
        coverage = importlib.util.module_from_spec(spec); spec.loader.exec_module(coverage)
        self.assertIn("acceptance/test_phase4_live.py", coverage.ACCEPTANCE)

if __name__ == "__main__": unittest.main()
