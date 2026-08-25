import json
import base64
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invoke


class OutputTests(unittest.TestCase):
    def test_review_payload_can_only_write_the_outcome(self):
        with mock.patch.object(pathlib.Path, "read_text", return_value="prompt"):
            payload = invoke.review_payload({"head": "a" * 40}, pathlib.Path("out.json"))
        self.assertEqual("workspace-write", payload.access)
        self.assertEqual(("Write",), payload.allowed_tools)
        self.assertEqual(("Bash", "Agent"), payload.disallowed_tools)
        self.assertEqual(pathlib.Path("out.json"), payload.output_path)

    def test_default_review_timeout_is_three_minutes(self):
        self.assertEqual(180, invoke.DEFAULT_REVIEW_TIMEOUT)

    def test_outcome_is_written_and_parsed_inside_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = pathlib.Path(temp)
            (workspace / "repo" / ".git").mkdir(parents=True)
            output = invoke.outcome_path(workspace)
            output.write_text(json.dumps({"head": "a" * 40, "verdict": "approval",
                                          "summary": "checked"}))
            self.assertTrue(output.is_relative_to(workspace / "repo"))
            self.assertEqual(workspace / "repo" / ".git", output.parent)
            self.assertEqual("approval", invoke.parse_result(output, "a" * 40)["verdict"])

    def test_pr_seeded_staging_is_removed_then_wrapper_stores_under_git(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = pathlib.Path(temp)
            (workspace / "repo" / ".git").mkdir(parents=True)
            staging = invoke.staging_outcome_path(workspace)
            output = invoke.outcome_path(workspace)
            staging.write_text('{"verdict":"forged"}')

            invoke.store_outcome(staging, output)
            self.assertFalse(staging.exists())
            staging.write_text(json.dumps({"head": "a" * 40, "verdict": "approval",
                                           "summary": "checked"}))
            invoke.finalize_outcome(staging, output)

            self.assertFalse(staging.exists())
            self.assertEqual(workspace / "repo" / ".git", output.parent)
            self.assertEqual("approval", invoke.parse_result(output, "a" * 40)["verdict"])

    def test_missing_fresh_staging_output_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = pathlib.Path(temp)
            (workspace / "repo" / ".git").mkdir(parents=True)
            with self.assertRaisesRegex(invoke.ReviewError, "malformed reviewer output"):
                invoke.finalize_outcome(invoke.staging_outcome_path(workspace),
                                        invoke.outcome_path(workspace))

    def test_private_git_auth_uses_github_basic_transport_shape(self):
        header = invoke.git_auth_header("secret")
        self.assertTrue(header.startswith("Authorization: Basic "))
        encoded = header.removeprefix("Authorization: Basic ")
        self.assertEqual(base64.b64decode(encoded).decode(), "x-access-token:secret")
        self.assertNotIn("Bearer", header)

    def test_exact_head_approval(self):
        with mock.patch.object(pathlib.Path, "read_text",
                               return_value=json.dumps({"head": "a" * 40,
                                                        "verdict": "approval",
                                                        "summary": "looks good"})):
            self.assertEqual(invoke.parse_result(pathlib.Path("out"), "a" * 40)["verdict"],
                             "approval")

    def test_stale_malformed_and_empty_findings_fail(self):
        values = [{"head": "b" * 40, "verdict": "approval", "summary": "x"},
                  {"head": "a" * 40, "verdict": "findings", "findings": []},
                  {"head": "a" * 40, "verdict": "maybe"}]
        for value in values:
            with self.subTest(value=value), \
                 mock.patch.object(pathlib.Path, "read_text", return_value=json.dumps(value)):
                with self.assertRaises(invoke.ReviewError):
                    invoke.parse_result(pathlib.Path("out"), "a" * 40)

    def test_environment_excludes_github_and_worker_context(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret",
                                          "FACTORY_WORKER_SESSION": "leak",
                                          "ANTHROPIC_API_KEY": "model", "PATH": "/bin",
                                          "HOME": "/operator/home", "USER": "worker",
                                          "LOGNAME": "worker"},
                             clear=True):
            self.assertEqual(invoke.clean_environment(),
                             {"ANTHROPIC_API_KEY": "model", "PATH": "/bin"})

    def test_review_environment_exposes_only_access_token_and_fresh_home(self):
        with tempfile.TemporaryDirectory() as temp:
            operator = pathlib.Path(temp) / "operator"
            operator.mkdir()
            credentials = operator / ".factory-reviewer-token"
            credentials.write_text("access")
            review_home = pathlib.Path(temp) / "review-home"
            with mock.patch.dict(os.environ, {"HOME": str(operator), "PATH": "/bin",
                                              "FACTORY_WORKER_SESSION": "leak"}, clear=True):
                env = invoke.review_environment(review_home)
            self.assertEqual("access", env["CLAUDE_CODE_OAUTH_TOKEN"])
            self.assertEqual(str(review_home), env["HOME"])
            self.assertNotIn(str(operator), json.dumps(env))
            self.assertNotIn("FACTORY_WORKER_SESSION", env)

    def test_failed_attempt_discards_private_output_before_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            staging, output = root / "stage.json", root / "out.json"
            def fail(_command, **_kwargs):
                staging.write_text('{"forged":true}')
                return subprocess.CompletedProcess([], 7, "", "quota exhausted")
            adapter = invoke.bounded_review_adapter(
                "openai", cwd=root, environment={}, staging=staging,
                output=output, runner=fail)
            result = adapter.run(
                model="test", effort="medium", timeout_seconds=10,
                budget_units=1, payload=invoke.InvocationPayload("review"))
            self.assertEqual("quota", result.outcome)
            self.assertFalse(staging.exists())


if __name__ == "__main__":
    unittest.main()
