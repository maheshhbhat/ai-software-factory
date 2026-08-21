import base64
import json
import os
import subprocess
import unittest
import urllib.error
from unittest import mock

import invoke
import contract
from test_artifacts import FakeStore, campaign_output, project_issue, project_output


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Client(FakeStore):
    def __init__(self):
        super().__init__([{"number": 1, "labels": ["type:roadmap-commitment"],
                           "body": "Retirement direction"}])
        self.repo, self.token = "o/r", "token"

    def _api(self, path, method="GET", payload=None):
        if path == "":
            return {"default_branch": "main"}
        if path.startswith("/git/trees/"):
            return {"tree": [{"path": "product.md", "type": "blob"},
                              {"path": "docs/decisions/0001.md", "type": "blob"}]}
        if path.startswith("/contents/"):
            text = "# Product" if path.endswith("product.md") else "# ADR"
            return {"content": base64.b64encode(text.encode()).decode()}
        raise AssertionError(path)

    def _pages(self, path):
        return []


class ProjectClient(Client):
    def __init__(self):
        FakeStore.__init__(self, [project_issue()])
        self.issues[0]["labels"] = ["type:project", "project:planning"]
        self.repo, self.token = "o/r", "token"

    def _pages(self, path):
        if path.endswith("/timeline"):
            return [{"id": 99, "event": "labeled", "label": {"name": "project:planning"}}]
        return []


class InvocationTests(unittest.TestCase):
    def test_campaign_executes_headlessly_then_reads_back(self):
        client = Client()
        runner = mock.Mock(return_value=Result(stdout=json.dumps(campaign_output())))
        with mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client), \
             mock.patch.dict(os.environ, {"FACTORY_PLANNING_MODEL_CMD":
                                          "fake --input {input_file} --budget {max_usd}"}):
            result = invoke.execute("o/r", 1, "token", 30, 2.5, runner=runner)
        self.assertEqual("campaign", result.altitude.value)
        command = runner.call_args.args[0]
        self.assertIn("2.5", command)
        self.assertEqual(2, len(client.issues))

    def test_project_finishes_only_after_verified_readback(self):
        client = ProjectClient()
        runner = mock.Mock(return_value=Result(stdout=json.dumps(project_output())))
        with mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client), \
             mock.patch.dict(os.environ, {"FACTORY_PLANNING_MODEL_CMD": "fake {input_file}"}):
            result = invoke.execute("o/r", 10, "token", 30, 2.5, runner=runner)
        self.assertEqual((12, 13), result.stories)
        self.assertIn("project:awaiting-ready", client.get_issue(10)["labels"])
        self.assertNotIn("project:planning", client.get_issue(10)["labels"])

    def test_malformed_project_output_leaves_project_planning(self):
        client = ProjectClient()
        runner = mock.Mock(return_value=Result(stdout="{}"))
        with mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client), \
             mock.patch.dict(os.environ, {"FACTORY_PLANNING_MODEL_CMD": "fake {input_file}"}), \
             self.assertRaises(contract.ContractError):
            invoke.execute("o/r", 10, "token", 30, 2.5, runner=runner)
        self.assertIn("project:planning", client.get_issue(10)["labels"])

    def test_403_and_404_fail_before_any_write(self):
        for code in (403, 404):
            client = Client()
            with mock.patch.object(client, "get_issue", side_effect=urllib.error.HTTPError(
                    "url", code, "denied", {}, None)), \
                 mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client):
                with self.subTest(code=code), self.assertRaisesRegex(
                        invoke.InvocationError, "no planning artifacts were written"):
                    invoke.execute("o/r", 1, "token", 30, 2.5)
            self.assertEqual(1, len(client.issues))
            self.assertEqual({}, client.comments)

    def test_timeout_is_named_and_nonzero_path(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        with mock.patch.dict(os.environ, {"FACTORY_PLANNING_MODEL_CMD": "fake {input_file}"}):
            with self.assertRaisesRegex(invoke.InvocationError, "timeout exhausted"):
                invoke.run_model({"input": 1}, 7, 1.0, runner=timeout)

    def test_malformed_or_failed_model_is_named(self):
        with mock.patch.dict(os.environ, {"FACTORY_PLANNING_MODEL_CMD": "fake {input_file}"}):
            with self.assertRaisesRegex(invoke.InvocationError, "malformed JSON"):
                invoke.run_model({}, 7, 1.0,
                                 runner=lambda *a, **k: Result(stdout="not-json"))
            with self.assertRaisesRegex(invoke.InvocationError, r"failed \(3\)"):
                invoke.run_model({}, 7, 1.0,
                                 runner=lambda *a, **k: Result(3, stderr="budget"))

    def test_campaign_state_version_ignores_comments_and_updated_at(self):
        client = Client()
        issue = client.get_issue(1)
        first = invoke.state_version(client, {**issue, "updated_at": "one"})
        second = invoke.state_version(client, {**issue, "updated_at": "two"})
        self.assertEqual(first, second)

    def test_default_model_command_embeds_input_and_binds_json_schema(self):
        value = {"trigger": {"labels": ["type:roadmap-commitment"]},
                 "product": "# Product", "adrs": [], "repository": {"files": ["product.md"]}}
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(value, handle)
            handle.flush()
            with mock.patch.dict(os.environ, {}, clear=True):
                command = invoke.model_command(handle.name, 30, 2.5)
        self.assertIn("--json-schema", command)
        self.assertIn("--max-budget-usd", command)
        self.assertIn('"product": "# Product"', command[2])

    def test_prompt_version_changes_when_prompt_changes(self):
        with mock.patch.object(invoke.pathlib.Path, "read_bytes", return_value=b"one"):
            first = invoke.prompt_version()
        with mock.patch.object(invoke.pathlib.Path, "read_bytes", return_value=b"two"):
            second = invoke.prompt_version()
        self.assertNotEqual(first, second)

    def test_readback_retries_eventual_consistency_then_passes(self):
        expected = object()
        with mock.patch.object(invoke.artifacts, "verify", side_effect=[
                invoke.artifacts.ArtifactError("missing"), expected]) as verify:
            sleeps = []
            actual = invoke.verify_with_retry(None, {}, "key", contract.Altitude.CAMPAIGN,
                                               sleeper=sleeps.append)
        self.assertIs(expected, actual)
        self.assertEqual([1], sleeps)
        self.assertEqual(2, verify.call_count)


if __name__ == "__main__":
    unittest.main()
