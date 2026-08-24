import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import reviewer_real


class ReviewerRealTests(unittest.TestCase):
    def test_live_diagnostic_uses_only_the_production_wrapper(self):
        source = pathlib.Path(reviewer_real.__file__).read_text()
        self.assertIn('"agents" / "review" / "run.sh"', source)
        self.assertNotIn("import invoke", source)
        self.assertNotIn("FACTORY_REVIEW_MODEL_CMD", source.replace(
            'FORBIDDEN_OVERRIDES = ("FACTORY_REVIEW_CMD", "FACTORY_REVIEW_MODEL_CMD")', ""))

    def test_review_substitution_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "substitution"):
            reviewer_real.validate_environment({"FACTORY_REVIEW_MODEL_CMD": "fake"})

    def test_default_limit_is_one_minute(self):
        self.assertEqual(60, reviewer_real.DEFAULT_TIMEOUT_SECONDS)

    def test_evidence_records_real_wrapper_timing_and_result(self):
        completed = subprocess.CompletedProcess([], 0, '{"status":"approval"}\n', "")
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(reviewer_real, "token", return_value="token"), \
             mock.patch.object(subprocess, "run", return_value=completed) as launched, \
             mock.patch.dict(reviewer_real.os.environ, {}, clear=True):
            code, path = reviewer_real.run("owner/repo", 42, pathlib.Path(temp), 20)
            evidence = json.loads(path.read_text())
        self.assertEqual(0, code)
        self.assertEqual("completed", evidence["status"])
        self.assertEqual(42, evidence["pull_request"])
        self.assertEqual("factory/agents/review/run.sh", evidence["production_entrypoint"])
        self.assertEqual(20, launched.call_args.kwargs["timeout"])
        self.assertIsNone(launched.call_args.kwargs.get("stderr"))

    def test_timeout_is_saved_as_failed_evidence(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(reviewer_real, "token", return_value="token"), \
             mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired([], 4)), \
             mock.patch.dict(reviewer_real.os.environ, {}, clear=True):
            code, path = reviewer_real.run("owner/repo", 43, pathlib.Path(temp), 4)
            evidence = json.loads(path.read_text())
        self.assertEqual(124, code)
        self.assertEqual("timeout", evidence["status"])


if __name__ == "__main__":
    unittest.main()
