from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import invoke
from factory.capacity_pool.router import ModelCapacity, Tier
from factory.capacity_pool.state import CapacityState

SHA = "a" * 40
PROJECT_BODY = """### Stories

#20

### Operating envelope

- OE-SCALE | representative-input | Support $1M | FAIL WHEN: blocks
"""


class Client:
    def __init__(self):
        self.comments = []
        self.project = {"number": 10, "body": PROJECT_BODY,
                        "labels": [{"name": "type:project"},
                                   {"name": "project:active"}]}

    def api(self, path, *, method="GET", value=None):
        if path == "/issues/10":
            return self.project
        if path == "":
            return {"default_branch": "main"}
        if path == "/commits/main":
            return {"sha": SHA}
        if path == "/issues/10/comments" and method == "POST":
            self.comments.append({"body": value["body"]})
            return self.comments[-1]
        raise AssertionError((path, method))

    def pages(self, path):
        if path == "/issues/10/comments":
            return list(self.comments)
        raise AssertionError(path)


def capacity():
    state = CapacityState()
    model = ModelCapacity("gpt-5.6-terra", "openai", Tier.BALANCED,
                          frozenset({"code", "reason", "json"}))
    state.mark_healthy(model.provider, model.name, "test")
    return state, (model,)


class ReadinessInvokeTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.state, self.registry = capacity()

    def tearDown(self):
        self.state.close()

    def fake_clone(self, repo, token, revision, workspace, timeout):
        checkout = workspace / "repo"
        (checkout / ".git").mkdir(parents=True)
        return checkout

    def runner(self, command, **kwargs):
        prompt = " ".join(command)
        output = prompt.split("Write the JSON outcome to: ", 1)[1].splitlines()[0]
        pathlib.Path(output).write_text(json.dumps({
            "revision": SHA,
            "results": [{"id": "OE-SCALE", "result": "pass",
                         "evidence": "test_scale.py::test_million",
                         "detail": "completed within bound"}],
            "observations": [],
        }))
        return subprocess.CompletedProcess(command, 0, "done", "")

    def test_publishes_exact_revision_and_replays(self):
        with mock.patch.object(invoke, "clone_integrated", self.fake_clone), \
                mock.patch.object(invoke, "assert_checkout_unchanged"):
            first = invoke.execute("owner/repo", 10, "token", client=self.client,
                                   state=self.state, registry=self.registry,
                                   runner=self.runner)
            second = invoke.execute("owner/repo", 10, "token", client=self.client,
                                    state=self.state, registry=self.registry,
                                    runner=self.runner)
        self.assertEqual("published", first["status"])
        self.assertEqual("ready", first["overall"])
        self.assertEqual("replay", second["status"])
        self.assertEqual(1, len(self.client.comments))

    def test_stale_or_incomplete_model_output_writes_nothing(self):
        def bad_runner(command, **kwargs):
            prompt = " ".join(command)
            output = prompt.split("Write the JSON outcome to: ", 1)[1].splitlines()[0]
            pathlib.Path(output).write_text(json.dumps(
                {"revision": "b" * 40, "results": [], "observations": []}))
            return subprocess.CompletedProcess(command, 0, "done", "")

        with mock.patch.object(invoke, "clone_integrated", self.fake_clone), \
                mock.patch.object(invoke, "assert_checkout_unchanged"), \
                self.assertRaises(invoke.EvaluationError):
            invoke.execute("owner/repo", 10, "token", client=self.client,
                           state=self.state, registry=self.registry,
                           runner=bad_runner)
        self.assertEqual([], self.client.comments)

    def test_checkout_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            checkout = pathlib.Path(root)
            subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"],
                           cwd=checkout, check=True)
            subprocess.run(["git", "config", "user.name", "Test"],
                           cwd=checkout, check=True)
            tracked = checkout / "app.py"
            tracked.write_text("before\n")
            subprocess.run(["git", "add", "app.py"], cwd=checkout, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=checkout, check=True)
            staging = checkout / ".factory-readiness-out.json"
            staging.write_text("{}")
            invoke.assert_checkout_unchanged(checkout, staging)
            tracked.write_text("after\n")
            with self.assertRaisesRegex(invoke.EvaluationError, "modified"):
                invoke.assert_checkout_unchanged(checkout, staging)


if __name__ == "__main__":
    unittest.main()
