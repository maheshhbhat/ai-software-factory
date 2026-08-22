import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("phase4_live", HERE / "phase4_live.py")
live = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(live)

class Phase4LiveHarnessTests(unittest.TestCase):
    def test_story_is_bounded_isolated_and_deliberately_two_pass(self):
        body = live.story_body()
        self.assertIn("#212", body)
        self.assertIn("Attempt 1 only", body)
        self.assertIn("Attempt 2", body)
        self.assertIn("runs/phase4/live_product/**", body)
        self.assertNotIn("income-portfolio", body)

    def test_review_model_binds_engine_verdict_to_checkout_head(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = pathlib.Path(temp) / "in.json", pathlib.Path(temp) / "out.json"
            source.write_text(json.dumps({"head": "b" * 40,
                                          "diff": [{"patch": "+ defective"}]}))
            responses = [mock.Mock(stdout='{"verdict":"findings","findings":["fix"]}\n')]
            with mock.patch.object(live, "command", side_effect=responses) as command:
                live.review_model(str(source), str(target))
            result = json.loads(target.read_text())
            self.assertEqual(result["head"], "b" * 40)
            self.assertEqual(result["verdict"], "findings")
            review_call = command.call_args_list[0]
            self.assertIn("--safe-mode", review_call.args[0])
            self.assertEqual(dict(live.os.environ), review_call.kwargs["env"])

    def test_worker_model_is_headless_edit_only_and_sessionless(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "in.json"
            source.write_text(json.dumps({"story": {"body": "### Attempt\n\n2\n"}}))
            completed = mock.Mock(returncode=0)
            with mock.patch.object(live.subprocess, "run", return_value=completed) as run:
                self.assertEqual(live.worker_model(str(source)), 0)
            parts = next(call.args[0] for call in run.call_args_list
                         if call.args[0][0] == "claude")
            self.assertIn("acceptEdits", parts)
            self.assertIn("Read,Write,Edit,Glob,Grep", parts)
            self.assertIn("Bash", parts)
            self.assertIn("--no-session-persistence", parts)

    def test_first_attempt_defect_is_deterministic_after_real_engine_edit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "runs/phase4/live_product").mkdir(parents=True)
            source = root / "input.json"
            source.write_text(json.dumps({"story": {"body": "### Attempt\n\n1\n"}}))
            completed = mock.Mock(returncode=0)
            app = root / "runs/phase4/live_product/app.py"
            def engine(parts, **_kwargs):
                if parts[0] == "claude":
                    app.write_text("def build_sha():\n    return value\n")
                return completed
            with mock.patch.object(live.subprocess, "run", side_effect=engine), \
                 mock.patch.object(live.pathlib.Path, "cwd", return_value=root):
                self.assertEqual(live.worker_model(str(source)), 0)
            self.assertIn('return "defective"', app.read_text())
            self.assertIn('"build_sha": "defective"', app.read_text())

    def test_runtime_worker_launch_is_an_async_handoff(self):
        with mock.patch.object(live.subprocess, "Popen") as started, \
             mock.patch.object(live.pathlib.Path, "open", mock.mock_open()):
            self.assertEqual(live.launch_worker(230), 0)
        self.assertTrue(started.call_args.kwargs["start_new_session"])

    def test_live_loop_delegates_review_and_merge_to_runtime(self):
        environment = live.poll_environment("token", pathlib.Path("runtime.jsonl"))
        self.assertEqual("1", environment["FACTORY_PHASE4_REVIEWS"])
        self.assertIn("review-model", environment["FACTORY_REVIEW_MODEL_CMD"])
        source = pathlib.Path(live.__file__).read_text()
        self.assertNotIn('command(["gh", "pr", "ready"', source)
        self.assertNotIn('command(["gh", "pr", "merge"', source)
        self.assertNotIn("def run_review(", source)

    def test_model_auth_context_never_restores_github_credentials(self):
        with mock.patch.dict(live.os.environ, {"GH_TOKEN": "github",
                                               "GITHUB_TOKEN": "github2"}, clear=False):
            environment = live.model_environment()
        self.assertTrue(environment["HOME"])
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)

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
