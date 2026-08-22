"""Hermetic acceptance proof for the bounded Phase 4 delivery worker."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

WORKER = pathlib.Path(__file__).resolve().parents[1] / "agents" / "worker"
sys.path.insert(0, str(WORKER))
import invoke


STORY_BODY = """### Project

#212

### Spend cap

$5 / 10 min

### Scope

product/**
"""


class FakeGitHub:
    def __init__(self, origin: pathlib.Path):
        self.origin = origin
        self.events = [{"event": "labeled", "label": {"name": "story:claimed"},
                        "id": 100}]
        self.comments = []
        self.pulls = []
        self.writes = 0
        self.readable = True

    def api(self, path, **_kwargs):
        if not self.readable:
            raise invoke.DeliveryError("repository access constraint failed")
        if path == "":
            return {"default_branch": "main"}
        raise AssertionError(path)

    def issue(self, number):
        if number == 214:
            return {"number": 214, "body": STORY_BODY, "labels": []}
        if number == 212:
            return {"number": 212, "body": "approved project", "labels": []}
        raise AssertionError(number)

    def pages(self, path):
        if path == "/issues/214/timeline":
            return self.events
        if path == "/issues/214/comments":
            return self.comments
        if path == "/issues?state=all":
            return [{"number": 90, "body": "decision", "labels": [{"name": "type:adr"}]}]
        raise AssertionError(path)

    def pull_requests(self):
        return self.pulls

    def _head(self, branch):
        result = subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", f"refs/heads/{branch}"],
            check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def create_pr(self, _title, head, _base, body):
        self.writes += 1
        pull = {"number": 1, "body": body,
                "head": {"ref": head, "sha": self._head(head)}}
        self.pulls[:] = [pull]
        return pull

    def update_pr(self, number, body):
        self.writes += 1
        pull = self.pulls[0]
        self.pulls[0] = {"number": number, "body": body,
                         "head": {"ref": pull["head"]["ref"],
                                  "sha": self._head(pull["head"]["ref"])}}
        return self.pulls[0]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.origin = root / "origin.git"
        self.checkout = root / "checkout"
        git(root, "init", "--bare", str(self.origin))
        git(root, "clone", str(self.origin), str(self.checkout))
        git(self.checkout, "config", "user.name", "Factory test")
        git(self.checkout, "config", "user.email", "factory@example.invalid")
        (self.checkout / "README.md").write_text("seed\n")
        git(self.checkout, "add", "README.md")
        git(self.checkout, "commit", "-m", "seed")
        git(self.checkout, "branch", "-M", "main")
        git(self.checkout, "push", "-u", "origin", "main")
        self.client = FakeGitHub(self.origin)
        self.counter = root / "counter"
        self.model = root / "model.sh"
        self.model.write_text(
            "#!/bin/sh\nset -eu\n"
            f"n=$(cat '{self.counter}' 2>/dev/null || echo 0)\n"
            f"n=$((n+1)); echo $n > '{self.counter}'\n"
            "mkdir -p product; echo $n > product/result.txt\n")
        self.model.chmod(0o755)
        self.environment = {"FACTORY_DELIVERY_MODEL_CMD": str(self.model),
                            "FACTORY_DELIVERY_TEST_CMD": "/usr/bin/true"}

    def tearDown(self):
        self.temp.cleanup()

    def deliver(self):
        with mock.patch.dict(os.environ, self.environment, clear=False):
            return invoke.execute("owner/repo", 214, "token", self.checkout,
                                  client=self.client)

    def test_first_delivery_replay_and_retry_use_one_durable_pr(self):
        first = self.deliver()
        self.assertFalse(first.replay)
        self.assertEqual((first.pull_request, self.client.writes), (1, 1))
        first_head = first.head

        replay = self.deliver()
        self.assertTrue(replay.replay)
        self.assertEqual((replay.pull_request, replay.head, self.client.writes),
                         (1, first_head, 1))

        self.client.events.append(
            {"event": "labeled", "label": {"name": "story:claimed"}, "id": 101})
        self.client.comments.append({"body": "## Review findings\nfix the result"})
        retry = self.deliver()
        self.assertFalse(retry.replay)
        self.assertEqual((retry.pull_request, self.client.writes, len(self.client.pulls)),
                         (1, 2, 1))
        self.assertNotEqual(first_head, retry.head)
        self.assertIn("worker-artifact:214:101", self.client.pulls[0]["body"])

    def test_repository_access_failure_happens_before_any_write(self):
        self.client.readable = False
        with self.assertRaisesRegex(invoke.DeliveryError, "access constraint"):
            self.deliver()
        self.assertEqual(self.client.writes, 0)

    def test_out_of_scope_model_change_creates_no_artifact(self):
        self.model.write_text("#!/bin/sh\nset -eu\necho bad > forbidden.txt\n")
        with self.assertRaisesRegex(invoke.DeliveryError, "outside Story scope"):
            self.deliver()
        self.assertEqual((self.client.writes, self.client.pulls), (0, []))


if __name__ == "__main__":
    unittest.main()
