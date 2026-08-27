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
from factory.capacity_pool.router import ModelCapacity, Tier
from factory.capacity_pool.state import CapacityState
from factory.runtime import poller as runtime_poller


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
            return {"number": 212, "title": "Comparison Project", "state": "open",
                    "body": ("### Goal\n\nCompare scenarios.\n\n"
                             "### Falsifiable acceptance criteria\n\n"
                             "- exact labels\n- owner Chrome after all PRs merge\n"),
                    "labels": [{"name": "project:active"}]}
        if path == f"/commits/{self.head}/check-runs":
            return {"check_runs": [
                {"name": "tests", "status": "completed", "conclusion": "success",
                 "details_url": "https://checks.test/tests"},
                {"name": "merge-gate", "status": "completed", "conclusion": "success",
                 "details_url": "https://checks.test/gate"},
            ]}
        raise AssertionError((path, method))

    def story(self):
        return {"number": 215, "title": "Render exact labels", "state": "open",
                "body": ("### Project\n\n#212\n\n### Phase\n\nbuild\n\n"
                         "### Depends-on\n\n#214\n\n### Acceptance notes\n\n"
                         "- Render exactly 2%, 3%, and 4%.\n"),
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
            return [
                {"number": 1, "title": "ADR", "body": "decision", "state": "open",
                 "labels": [{"name": "type:adr"}]},
                {"number": 214, "title": "Calculate", "state": "closed",
                 "body": ("### Project\n\n#212\n\n### Phase\n\nbuild\n\n"
                          "### Depends-on\n\nnone\n"),
                 "labels": [{"name": "type:story"}, {"name": "story:completed"}]},
                self.story(),
                {"number": 216, "title": "Owner Chrome assurance", "state": "open",
                 "body": ("### Project\n\n#212\n\n### Phase\n\nhardening\n\n"
                          "### Depends-on\n\n#215\n\n### Acceptance notes\n\n"
                          "- Owner records Chrome evidence after all PRs merge.\n"),
                 "labels": [{"name": "type:story"}, {"name": "story:blocked"}]},
                {"number": 999, "title": "Unrelated Project Story", "state": "open",
                 "body": ("### Project\n\n#998\n\n### Phase\n\nbuild\n\n"
                          "### Depends-on\n\nnone\n"),
                 "labels": [{"name": "type:story"}]},
            ]
        raise AssertionError(path)


class ReviewAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeGitHub()
        self.serialized = None
        self.workspace_entries = None
        self.state = CapacityState()
        self.capacity = ModelCapacity(
            "review-test", "openai", Tier.BALANCED,
            frozenset({"code", "reason", "json"}))
        self.state.mark_healthy("openai", "review-test", "acceptance-fixture")
        self.addCleanup(self.state.close)

    def fake_subprocess(self, cmd, **kwargs):
        cwd = pathlib.Path(kwargs["cwd"])
        if cmd[:2] == ["git", "clone"]:
            (cwd / "repo" / ".git").mkdir(parents=True)
            self.assertIn("GIT_CONFIG_VALUE_0", kwargs["env"])
        else:
            self.assertNotIn("GH_TOKEN", kwargs["env"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_review(self, cmd, **kwargs):
        root = pathlib.Path(kwargs["cwd"]).parent
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertNotIn("shared-token", " ".join(cmd))
        self.assertNotIn("--output-last-message", cmd)
        self.serialized = json.loads(cmd[-1].split("Input: ", 1)[1])
        self.workspace_entries = sorted(x.name for x in root.iterdir())
        staging = cmd[-1].split("Write the JSON outcome to: ", 1)[1].splitlines()[0]
        pathlib.Path(staging).write_text(json.dumps(
            {"head": self.client.head, "verdict": "approval", "summary": "safe"}))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def deliver(self, runner=None):
        with mock.patch.object(invoke.subprocess, "run", side_effect=self.fake_subprocess), \
             mock.patch.dict(os.environ, {"OPENAI_API_KEY": "model-token"}):
            return invoke.execute(
                "owner/private", 9, "shared-token", client=self.client,
                state=self.state, registry=(self.capacity,),
                runner=runner or self.fake_review)

    def test_allowlisted_fresh_input_exact_head_and_replay(self):
        first = self.deliver()
        self.assertEqual(first["status"], "approval")
        self.assertEqual(set(self.serialized),
                         {"head", "diff", "story_spec", "project_criteria", "adrs",
                          "operating_envelope_obligations", "project_plan",
                          "trusted_checks", "prior_findings"})
        self.assertEqual(SHA, self.serialized["head"])
        stories = self.serialized["project_plan"]["stories"]
        self.assertEqual([214, 215, 216], [item["number"] for item in stories])
        self.assertEqual(["completed", "current", "future"],
                         [item["relation"] for item in stories])
        self.assertIn("exactly 2%, 3%, and 4%", stories[1]["body"])
        self.assertIn("Owner records Chrome evidence", stories[2]["body"])
        self.assertNotIn("Unrelated Project Story", json.dumps(self.serialized))
        self.assertEqual(["merge-gate", "tests"],
                         [item["name"] for item in self.serialized["trusted_checks"]])
        self.assertEqual(self.workspace_entries, ["repo", "reviewer-home"])
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
        def stale(cmd, **kwargs):
            invoke.staging_outcome_path(pathlib.Path(kwargs["cwd"]).parent).write_text(json.dumps(
                {"head": SHA, "verdict": "approval", "summary": "old head"}))
            self.client.head = "b" * 40
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with self.assertRaisesRegex(invoke.ReviewError, "stale-head"):
            self.deliver(stale)
        self.assertEqual(self.client.writes, [])

    def test_reviewed_old_head_cannot_merge_new_head(self):
        """Regression: Project #60 Story #62 escaped this exact boundary."""
        old_head = "a" * 40
        new_head = "b" * 40
        pull = {"number": 9, "mergeable_state": "clean",
                "head": {"sha": new_head}}
        old_approval = {
            "body": f"<!-- review-outcome:9:{old_head}:approval -->"}
        new_approval = {
            "body": f"<!-- review-outcome:9:{new_head}:approval -->"}
        with mock.patch.object(runtime_poller.subprocess, "run") as merge:
            self.assertFalse(runtime_poller.route_merge(
                "owner/private", pull, [old_approval]))
            merge.assert_not_called()
        completed = subprocess.CompletedProcess([], 0, "merged", "")
        with mock.patch.object(runtime_poller.subprocess, "run",
                               return_value=completed) as merge:
            self.assertTrue(runtime_poller.route_merge(
                "owner/private", pull, [old_approval, new_approval]))
        self.assertIn("--match-head-commit", merge.call_args.args[0])
        self.assertIn(new_head, merge.call_args.args[0])
        self.assertNotIn("--auto", merge.call_args.args[0])

    def test_merged_pr_legacy_auto_merge_payload_is_ignored(self):
        """Regression: merged Project #60 PR #65 retains this payload."""
        merged = {"number": 65, "state": "closed", "merged": True,
                  "body": "Story: #62\n",
                  "auto_merge": {"enabled_by": {"login": "factory"}}}
        with mock.patch.object(runtime_poller.subprocess, "run") as command:
            self.assertEqual(set(), runtime_poller.disable_legacy_auto_merge(
                "owner/private", [merged]))
        command.assert_not_called()

    def test_findings_are_bound_to_head_and_return_story_ready(self):
        def findings(cmd, **kwargs):
            invoke.staging_outcome_path(pathlib.Path(kwargs["cwd"]).parent).write_text(json.dumps(
                {"head": SHA, "verdict": "findings", "findings": ["fix defect"]}))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        result = self.deliver(findings)
        self.assertEqual(result["status"], "findings")
        self.assertIn("story:ready", self.client.story_labels)
        self.assertIn(SHA, self.client.comments[0]["body"])


if __name__ == "__main__":
    unittest.main()
