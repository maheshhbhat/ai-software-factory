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
import runlog


class OutputTests(unittest.TestCase):
    def test_default_command_can_only_write_the_outcome(self):
        with mock.patch.object(pathlib.Path, "read_text", return_value="prompt"):
            cmd = invoke.command(pathlib.Path("input.json"), pathlib.Path("out.json"))
        self.assertIn("acceptEdits", cmd)
        self.assertEqual("Write", cmd[cmd.index("--tools") + 1])
        self.assertEqual("Write", cmd[cmd.index("--allowedTools") + 1])
        self.assertEqual("Bash,Agent", cmd[cmd.index("--disallowedTools") + 1])
        self.assertIn("--safe-mode", cmd)
        self.assertIn("--no-session-persistence", cmd)

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

    def test_unavailable_reviewer_fails(self):
        with mock.patch.object(subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 7, "", "offline")):
            with self.assertRaisesRegex(invoke.ReviewError, "unavailable"):
                invoke.run(["review"], cwd=".")


class EngineUsageTests(unittest.TestCase):
    """The review half of Project #322 criterion 5: a story's cost is the
    worker's launch plus the reviewer's, so the reviewer must report too."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = pathlib.Path(self.dir.name)
        self.env = mock.patch.dict(os.environ,
                                   {"FACTORY_RUN_DIR": str(self.log),
                                    "FACTORY_RUNTIME_LOG_STDERR": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def records(self):
        records=[]
        for name in ("process-events.jsonl","telemetry.jsonl"):
            path=self.log/name
            if path.exists(): records += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return sorted(records,key=lambda row:row["timestamp"])

    def usage_records(self):
        return [record for record in self.records()
                if record.get("metric") == runlog.USAGE_EVENT]

    def launch(self, result):
        with mock.patch.object(subprocess, "run", return_value=result):
            invoke.launch_reviewer(["claude", "-p", "x"], cwd=".", timeout=10,
                                   env={"PATH": "/bin"}, story=337, pull_request=9)

    def test_the_reviewer_usage_is_recorded_against_the_story(self):
        reported = json.dumps({"total_cost_usd": 0.11,
                               "usage": {"input_tokens": 400, "output_tokens": 20}})
        self.launch(subprocess.CompletedProcess([], 0, reported, ""))
        record = self.usage_records()[0]
        self.assertEqual(runlog.USAGE_EVENT, record["metric"])
        self.assertEqual((337, 9, "review", "claude", "completed"),
                         (record["story"], record["pull_request"], record["phase"],
                          record["engine"], record["launch"]))
        self.assertEqual({"input_tokens": 400, "output_tokens": 20,
                          "total_cost_usd": 0.11}, record["usage"])

    def test_reviewer_start_and_finish_are_visible_before_usage(self):
        reported = json.dumps({"usage": {"output_tokens": 20}})
        self.launch(subprocess.CompletedProcess([], 0, reported, ""))
        self.assertEqual(["review.engine.started", "review.engine.finished",
                          runlog.USAGE_EVENT],
                         [record.get("event",record.get("metric")) for record in self.records()])

    def test_a_reviewer_reporting_nothing_is_not_recorded_as_zero(self):
        self.launch(subprocess.CompletedProcess([], 0, "wrote the outcome", ""))
        record = self.usage_records()[0]
        self.assertFalse(record["usage_reported"])
        self.assertEqual(runlog.USAGE_UNAVAILABLE, record["usage"])

    def test_a_failed_reviewer_launch_still_records_what_it_spent(self):
        failed = subprocess.CompletedProcess(
            [], 7, json.dumps({"usage": {"output_tokens": 2}}), "offline")
        with self.assertRaisesRegex(invoke.ReviewError, "unavailable"):
            self.launch(failed)
        self.assertEqual([("failed", {"output_tokens": 2})],
                         [(r["launch"], r["usage"]) for r in self.usage_records()])

    def test_the_default_command_asks_the_reviewer_to_report_its_usage(self):
        with mock.patch.object(pathlib.Path, "read_text", return_value="prompt"):
            cmd = invoke.command(pathlib.Path("input.json"), pathlib.Path("out.json"))
        self.assertEqual("stream-json", cmd[cmd.index("--output-format") + 1])
        self.assertIn("--verbose", cmd)
        self.assertEqual("claude", invoke.engine_name(cmd))

    def test_usage_capture_grants_the_reviewer_no_tool_and_no_identity(self):
        """The fresh-identity construction is what this story must not touch:
        stdout format is not a permission and not a credential."""
        with mock.patch.object(pathlib.Path, "read_text", return_value="prompt"):
            cmd = invoke.command(pathlib.Path("input.json"), pathlib.Path("out.json"))
        self.assertEqual("Write", cmd[cmd.index("--allowedTools") + 1])
        self.assertEqual("Bash,Agent", cmd[cmd.index("--disallowedTools") + 1])
        self.assertIn("--safe-mode", cmd)


if __name__ == "__main__":
    unittest.main()
