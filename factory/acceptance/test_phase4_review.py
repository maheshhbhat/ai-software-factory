"""Hermetic acceptance proof of fresh-context review isolation and routing."""

import json
import importlib.util
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

REVIEW = pathlib.Path(__file__).resolve().parents[1] / "agents" / "review"
SPEC = importlib.util.spec_from_file_location("phase4_review_invoke", REVIEW / "invoke.py")
invoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(invoke)


SHA = "a" * 40


class FakeGitHub:
    def __init__(self):
        self.story_labels = {"type:story", "story:in-review"}
        self.comments = []
        self.head = SHA
        self.writes = []

    def pull(self):
        return {"number": 9, "state": "open", "draft": False, "body": "Story: #215\n",
                "head": {"sha": self.head}}

    def api(self, path, method="GET", value=None):
        if method == "POST":
            self.writes.append((method, path, value))
            self.comments.append({"body": value["body"]})
            return self.comments[-1]
        if method == "PATCH":
            self.writes.append((method, path, value))
            self.story_labels = set(value["labels"])
            return self.story()
        if path == "":
            return {"default_branch": "main"}
        if path == "/pulls/9":
            return self.pull()
        if path == "/issues/215":
            return self.story()
        if path == "/issues/212":
            return {"number": 212, "body": "### Falsifiable acceptance criteria\n\n- approved\n",
                    "labels": [{"name": "project:active"}]}
        raise AssertionError((path, method))

    def story(self):
        return {"number": 215, "body": "### Project\n\n#212\n",
                "labels": [{"name": x} for x in sorted(self.story_labels)]}

    def pages(self, path):
        if path == "/issues/215/timeline":
            return [{"event":"labeled","label":{"name":"story:claimed"},
                     "created_at":"2026-01-01T00:00:00Z"}]
        if path == "/issues/215/comments":
            return self.comments
        if path == "/pulls/9/files":
            return [{"filename": "code.py", "status": "modified", "patch": "+safe"}]
        if path == "/issues?state=all":
            return [{"number": 1, "title": "ADR", "body": "decision",
                     "labels": [{"name": "type:adr"}]}]
        raise AssertionError(path)


class ReviewAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeGitHub()
        self.serialized = None
        self.workspace_entries = None

    def fake_subprocess(self, cmd, **kwargs):
        cwd = pathlib.Path(kwargs["cwd"])
        if cmd[:2] == ["git", "clone"]:
            (cwd / "repo" / ".git").mkdir(parents=True)
            self.assertIn("GIT_CONFIG_VALUE_0", kwargs["env"])
        else:
            self.assertNotIn("GH_TOKEN", kwargs["env"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_review(self, cmd, *, cwd, timeout, env=None):
        root = pathlib.Path(cwd).parent
        self.assertEqual(cmd[:2], ["review", "--input"])
        self.assertNotIn("worker", " ".join(cmd).lower())
        self.assertNotIn("shared-token", cmd)
        self.serialized = json.loads((root / "input.json").read_text())
        self.workspace_entries = sorted(x.name for x in root.iterdir())
        pathlib.Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(
            {"head": self.client.head, "verdict": "approval", "summary": "safe"}))

    def deliver(self):
        with mock.patch.object(invoke.subprocess, "run", side_effect=self.fake_subprocess), \
             mock.patch.object(invoke, "run", side_effect=self.fake_review), \
             mock.patch.dict(os.environ, {"FACTORY_REVIEW_MODEL_CMD":
                                          "review --input {input_file} --output {output_file}",
                                          "CLAUDE_CODE_OAUTH_TOKEN": "model-token"}):
            return invoke.execute("owner/private", 9, "shared-token", client=self.client)

    def test_allowlisted_fresh_input_exact_head_and_replay(self):
        first = self.deliver()
        self.assertEqual(first["status"], "approval")
        self.assertEqual(set(self.serialized),
                         {"head", "diff", "story_spec", "project_criteria", "adrs"})
        self.assertEqual(SHA, self.serialized["head"])
        self.assertEqual(self.workspace_entries, ["input.json", "repo", "reviewer-home"])
        self.assertNotIn("worker", json.dumps(self.serialized).lower())
        self.assertEqual(self.deliver()["status"], "replay")
        self.assertEqual(len(self.client.writes), 1)

    def test_head_change_invokes_again(self):
        self.deliver()
        self.client.head = "b" * 40
        result = self.deliver()
        self.assertEqual(result["head"], "b" * 40)
        self.assertEqual(len(self.client.writes), 2)

    def test_head_change_during_review_refuses_stale_result_without_a_write(self):
        def stale(_cmd, *, cwd, timeout, env=None):
            invoke.staging_outcome_path(pathlib.Path(cwd).parent).write_text(json.dumps(
                {"head": SHA, "verdict": "approval", "summary": "old head"}))
            self.client.head = "b" * 40
        with mock.patch.object(invoke.subprocess, "run", side_effect=self.fake_subprocess), \
             mock.patch.object(invoke, "run", side_effect=stale), \
             mock.patch.dict(os.environ, {"FACTORY_REVIEW_MODEL_CMD": "review",
                                          "CLAUDE_CODE_OAUTH_TOKEN": "model-token"}):
            with self.assertRaisesRegex(invoke.ReviewError, "stale-head"):
                invoke.execute("owner/private", 9, "token", client=self.client)
        self.assertEqual(self.client.writes, [])

    def test_findings_are_bound_to_head_and_return_story_ready(self):
        def findings(_cmd, *, cwd, timeout, env=None):
            invoke.staging_outcome_path(pathlib.Path(cwd).parent).write_text(json.dumps(
                {"head": SHA, "verdict": "findings", "findings": ["fix defect"]}))
        with mock.patch.object(invoke.subprocess, "run", side_effect=self.fake_subprocess), \
             mock.patch.object(invoke, "run", side_effect=findings), \
             mock.patch.dict(os.environ, {"FACTORY_REVIEW_MODEL_CMD": "review",
                                          "CLAUDE_CODE_OAUTH_TOKEN": "model-token"}):
            result = invoke.execute("owner/private", 9, "token", client=self.client)
        self.assertEqual(result["status"], "findings")
        self.assertIn("story:ready", self.client.story_labels)
        self.assertIn(SHA, self.client.comments[0]["body"])


if __name__ == "__main__":
    unittest.main()
