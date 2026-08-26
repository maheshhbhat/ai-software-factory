"""End-to-end planning tests through `invoke.execute`, writer, and read-back."""

import base64
import copy
import json
import os
import subprocess
import unittest
import urllib.error
from unittest import mock

import artifacts
import contract
import invoke
from test_artifacts import FakeStore, campaign_output, project_output
from test_invoke import Result, capacity


class GitHubFixture(FakeStore):
    """Isolated GitHub-compatible substrate; every durable read hits this store."""

    def __init__(self):
        super().__init__([{"number": 1, "labels": ["type:roadmap-commitment"],
                           "body": "Model retirement outcomes", "title": "Retirement"}])
        self.repo, self.token = "product/repo", "token"
        self.timeline = {}
        self.read_failure = None

    def _api(self, path, method="GET", payload=None):
        if self.read_failure in (403, 404):
            raise urllib.error.HTTPError("url", self.read_failure, "denied", {}, None)
        if path == "":
            return {"default_branch": "main", "private": True}
        if path.startswith("/git/trees/"):
            return {"tree": [{"path": "product.md", "type": "blob"},
                              {"path": "docs/decisions/0001-existing.md", "type": "blob"},
                              {"path": "src/model.py", "type": "blob"}]}
        if path.startswith("/contents/"):
            text = ("# Product\nRetirement outputs must be reproducible."
                    if path.endswith("product.md") else "# Existing ADR\nUse annual periods.")
            return {"content": base64.b64encode(text.encode()).decode()}
        raise AssertionError(path)

    def _pages(self, path):
        if path.endswith("/timeline"):
            number = int(path.split("/")[2])
            return copy.deepcopy(self.timeline.get(number, []))
        return []

    def activate_for_planning(self, number):
        issue = next(item for item in self.issues if item["number"] == number)
        issue["labels"] = ["type:project", "project:planning"]
        self.timeline[number] = [
            {"id": 1000 + number, "event": "labeled",
             "label": {"name": "project:planning"}}]


class PlanningE2E(unittest.TestCase):
    def setUp(self):
        self.store = GitHubFixture()
        self.capacity_state, self.capacity_registry = capacity()
        self.command = mock.patch.dict(
            os.environ, {"FACTORY_PLANNING_MODEL_CMD": "model {input_file} {max_usd}"})
        self.command.start()
        self.client = mock.patch.object(artifacts, "GitHubStore", return_value=self.store)
        self.client.start()

    def tearDown(self):
        self.client.stop()
        self.command.stop()
        self.capacity_state.close()

    def execute(self, number, output, runner=None):
        runner = runner or mock.Mock(return_value=Result(stdout=json.dumps(output)))
        return invoke.execute("product/repo", number, "token", 30, 2.0, runner=runner,
                              state=self.capacity_state,
                              registry=self.capacity_registry)

    def key(self, number):
        feedback = invoke.review_comments(self.store, number)
        return (f"{number}:{1000 + number}:project:prompt-{invoke.prompt_version()}:"
                f"feedback-{invoke.feedback_version(feedback)}")

    def test_01_two_altitudes_write_only_their_authorized_artifacts(self):
        campaign = self.execute(1, campaign_output())
        self.assertEqual(contract.Altitude.CAMPAIGN, campaign.altitude)
        self.assertEqual(2, campaign.project)
        self.assertFalse(any("type:story" in item["labels"] for item in self.store.issues))

        self.store.activate_for_planning(2)
        project = self.execute(2, project_output())
        self.assertEqual(contract.Altitude.PROJECT, project.altitude)
        self.assertEqual(3, project.adr)
        self.assertEqual((4, 5), project.stories)
        self.assertIn("project:awaiting-ready", self.store.get_issue(2)["labels"])
        key = self.key(2)
        self.assertEqual(project, artifacts.verify(
            self.store, self.store.get_issue(2), key, contract.Altitude.PROJECT))

    def test_02_duplicate_campaign_delivery_creates_nothing_new(self):
        first = self.execute(1, campaign_output())
        second = self.execute(1, campaign_output())
        self.assertEqual(first, second)
        self.assertEqual(2, len(self.store.issues))
        self.assertEqual(1, len(self.store.comments[1]))

    def test_03_project_artifact_replay_is_idempotent(self):
        campaign = self.execute(1, campaign_output())
        self.store.activate_for_planning(campaign.project)
        trigger = self.store.get_issue(campaign.project)
        key = self.key(campaign.project)
        first = artifacts.write(self.store, trigger, key, project_output())
        second = artifacts.write(self.store, trigger, key, project_output())
        self.assertEqual(first, second)
        self.assertEqual(5, len(self.store.issues))
        self.assertEqual(1, len(self.store.comments[campaign.project]))

    def test_04_403_and_404_preflight_write_nothing(self):
        for status in (403, 404):
            fixture = GitHubFixture()
            fixture.read_failure = status
            with mock.patch.object(artifacts, "GitHubStore", return_value=fixture), \
                 self.subTest(status=status), self.assertRaises(invoke.InvocationError):
                invoke.execute("product/repo", 1, "token", 30, 2.0)
            self.assertEqual(1, len(fixture.issues))
            self.assertEqual({}, fixture.comments)

    def test_05_timeout_and_budget_failure_write_nothing(self):
        failures = [
            lambda *a, **k: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(a[0], k["timeout"])),
            lambda *a, **k: Result(9, stderr="maximum budget exhausted"),
        ]
        for runner in failures:
            fixture = GitHubFixture()
            with mock.patch.object(artifacts, "GitHubStore", return_value=fixture), \
                 self.subTest(runner=runner), self.assertRaises(invoke.InvocationError):
                invoke.execute("product/repo", 1, "token", 1, 0.01, runner=runner)
            self.assertEqual(1, len(fixture.issues))
            self.assertEqual({}, fixture.comments)

    def test_06_malformed_output_writes_nothing(self):
        runner = lambda *a, **k: Result(stdout="not json")
        with self.assertRaises(invoke.InvocationError):
            invoke.execute("product/repo", 1, "token", 30, 2.0, runner=runner)
        self.assertEqual(1, len(self.store.issues))
        self.assertEqual({}, self.store.comments)

    def test_07_readback_detects_durable_corruption(self):
        campaign = self.execute(1, campaign_output())
        self.store.activate_for_planning(campaign.project)
        trigger = self.store.get_issue(campaign.project)
        key = self.key(campaign.project)
        written = artifacts.write(self.store, trigger, key, project_output())
        story = next(item for item in self.store.issues if item["number"] == written.stories[0])
        story["labels"].remove("phase:build")
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.verify(self.store, trigger, key, contract.Altitude.PROJECT)

    def test_08_feedback_revision_updates_in_place_and_replays_idempotently(self):
        campaign = self.execute(1, campaign_output())
        self.store.activate_for_planning(campaign.project)
        first = self.execute(campaign.project, project_output())
        issue_count = len(self.store.issues)

        self.store.create_comment(campaign.project,
                                  "## Review\n\nDefine the comparison rule before results.")
        self.store.activate_for_planning(campaign.project)
        revised = copy.deepcopy(project_output())
        revised["adr"]["decision"] = "Write product artifacts here and fix the rule first."
        revised["stories"][0]["spec"] = "Calculate a projection using the fixed rule."
        revised["digest"] = revised["digest"].replace(
            "Build the model, then show it.", "Fix the rule, build the model, then show it.")

        second = self.execute(campaign.project, revised)
        self.assertEqual(first, second)
        self.assertEqual(issue_count, len(self.store.issues))
        self.assertIn("fix the rule first", self.store.get_issue(first.adr)["body"])
        self.assertIn("fixed rule", self.store.get_issue(first.stories[0])["body"])
        digests = [item for item in self.store.comments[campaign.project]
                   if "planning-artifact:" in item["body"]]
        self.assertEqual(1, len(digests))
        self.assertIn("Fix the rule", digests[0]["body"])

        self.store.activate_for_planning(campaign.project)
        third = self.execute(campaign.project, revised)
        self.assertEqual(second, third)
        self.assertEqual(issue_count, len(self.store.issues))


if __name__ == "__main__":
    unittest.main()
