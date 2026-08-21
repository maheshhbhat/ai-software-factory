#!/usr/bin/env python3
"""End-to-end lifecycle coverage: ready → claimed → worker → completed.

Run: python3 -m unittest discover -s factory/runtime -p 'test_*.py' -v

Every other test file here checks one component against fixtures it was handed.
This one wires the real dispatcher, the real worker contract and the real
completion path together against one in-memory GitHub, and asserts on the
*durable state* that comes out — the labels, the issue's open/closed state, the
comments — because that is the only thing the factory actually keeps.

Two runs matter, and they are the same run with one difference:

  * the worker posts its acknowledgement → the Story reaches `story:completed`
    on the first poll, and nothing is ever dispatched for it again
  * the worker posts nothing → the Story stays claimed, the §9.4 lease expires,
    and the dispatcher hands it to a second worker

The second is not a regression test. It is the behaviour #104 exists to bound,
kept here so the first is read as the fix for something real: before the
completion path, *both* runs looked like the second one, and Project #95's
"exactly one acknowledgement" criterion could not hold for longer than a lease.

Standard library only, no network: `dispatcher._api` is the single I/O choke
point every read and write in this flow passes through, and the double below
implements it.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

import completion
import dispatcher
import poller
import workers


def setUpModule():
    os.environ["FACTORY_RUNTIME_LOG_STDERR"] = "0"
    os.environ.setdefault("FACTORY_RUNTIME_LOG",
                          os.path.join(tempfile.gettempdir(), "factory-runtime-test.jsonl"))


REPO = "o/r"
COMMITMENT = 54
PROJECT = 95
STORY = 103

STORY_BODY = """### Spec

Verify the worker launch bridge from a clean baseline.

### Project

#{project}

### Phase

build

### Depends-on

none

### Attempt

0

### Scope

no repository file changes

### Acceptance notes

Exactly one worker acknowledgement.
""".format(project=PROJECT)

PROJECT_BODY = f"""### Goal

Prove the runtime launches a delivery worker itself.

### Falsifiable acceptance criteria

- [ ] Exactly one worker acknowledgement is produced for the Story

### Roadmap commitment

#{COMMITMENT}
"""

ACKNOWLEDGEMENT = ("## Worker acknowledgement\n\nI am claude-delivery. I received this "
                   "assignment from the factory runtime for story #103 under project "
                   "#95. Acknowledged at 2026-08-21T03:38:35Z.")


class FakeGitHub:
    """The repository, in memory, behind `dispatcher._api`.

    Label writes append `labeled`/`unlabeled` timeline events, because the
    factory reads history rather than snapshots for every routing decision it
    cares about (§9.2) — the claim instant this whole flow turns on is a timeline
    event, so a double that skipped them would test a lifecycle that does not
    exist.
    """

    ISSUE_RE = re.compile(r"/repos/[^/]+/[^/]+/issues/(\d+)$")

    def __init__(self):
        # Anchored to the real clock, because the dispatcher's §9.4 lease check
        # reads `datetime.now` and no fixture can move that.
        self.now = datetime.now(timezone.utc)
        self.issues = {
            STORY: {"number": STORY, "state": "open", "author_association": "OWNER",
                    "body": STORY_BODY,
                    "labels": [{"name": n} for n in
                               ("type:story", "story:ready", "phase:build")]},
            PROJECT: {"number": PROJECT, "state": "open", "author_association": "OWNER",
                      "body": PROJECT_BODY,
                      "labels": [{"name": n} for n in ("type:project", "project:active")]},
        }
        self.timelines: dict[int, list] = {STORY: [], PROJECT: []}
        self.comments: dict[int, list] = {STORY: [], PROJECT: []}
        self.pulls: list = []

    # -- helpers the tests drive it with -----------------------------------

    def stamp(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, **delta) -> None:
        """Move the repository's clock forward for writes that follow."""
        self.now += timedelta(**delta)

    def age(self, **delta) -> None:
        """Push everything already recorded into the past.

        The way to simulate an elapsed lease. `CLAIM_LEASE` is measured against
        `datetime.now`, which a test cannot move, so time passes here by the
        history getting older rather than by the clock advancing.
        """
        shift = timedelta(**delta)

        def older(stamp: str) -> str:
            return (datetime.fromisoformat(stamp.replace("Z", "+00:00")) - shift
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")

        for events in self.timelines.values():
            for event in events:
                event["created_at"] = older(event["created_at"])
        for comments in self.comments.values():
            for comment in comments:
                comment["created_at"] = older(comment["created_at"])

    def labels(self, number: int) -> set[str]:
        return {label["name"] for label in self.issues[number]["labels"]}

    def post_comment(self, number: int, body: str) -> None:
        self.comments[number].append({
            "id": len(self.comments[number]) + 1, "body": body,
            "created_at": self.stamp(),
            "html_url": f"https://github.com/{REPO}/issues/{number}#c{len(self.comments[number])}"})

    def acknowledgements(self, number: int) -> list:
        return [c for c in self.comments[number]
                if c["body"].lstrip().startswith("## Worker acknowledgement")]

    def section(self, number: int, name: str) -> str:
        import merge_gate
        return (merge_gate.parse_section(self.issues[number]["body"], name) or "").strip()

    # -- the API surface ---------------------------------------------------

    def __call__(self, url, token, method="GET", payload=None):
        if method == "PATCH":
            return self._patch(url, payload)
        if method == "POST":
            number = int(re.search(r"/issues/(\d+)/comments", url).group(1))
            self.post_comment(number, payload["body"])
            return {}
        page = int(re.search(r"[?&]page=(\d+)", url).group(1)) if "page=" in url else 1
        if page > 1:
            return []
        if "/pulls" in url:
            return list(self.pulls)
        match = re.search(r"/issues/(\d+)/(timeline|comments)", url)
        if match:
            number, kind = int(match.group(1)), match.group(2)
            return list(self.timelines[number] if kind == "timeline"
                        else self.comments[number])
        single = self.ISSUE_RE.search(url.split("?")[0])
        if single:
            return dict(self.issues[int(single.group(1))])
        return [dict(issue) for issue in self.issues.values() if issue["state"] == "open"]

    def _patch(self, url, payload):
        number = int(self.ISSUE_RE.search(url.split("?")[0]).group(1))
        issue = self.issues[number]
        if "labels" in payload:
            before, after = self.labels(number), set(payload["labels"])
            for name in sorted(before - after):
                self.timelines[number].append(
                    {"event": "unlabeled", "created_at": self.stamp(),
                     "label": {"name": name}})
            for name in sorted(after - before):
                self.timelines[number].append(
                    {"event": "labeled", "created_at": self.stamp(),
                     "label": {"name": name}})
            issue["labels"] = [{"name": name} for name in sorted(after)]
        if "body" in payload:
            issue["body"] = payload["body"]
        if "state" in payload:
            issue["state"] = payload["state"]
            issue["state_reason"] = payload.get("state_reason")
        return dict(issue)


def worker_that(hub: FakeGitHub, posts: bool, exit_code: int = 0):
    """A stand-in for the launched engine.

    A real worker's externally visible effect is a GitHub write, so that is what
    this fake does. `posts=False` is #96's silent no-op: the engine exits
    cleanly having done nothing.
    """
    calls = []

    def _run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if posts:
            hub.advance(minutes=1)
            hub.post_comment(STORY, ACKNOWLEDGEMENT)
        import subprocess
        completed = subprocess.CompletedProcess(cmd, exit_code, "engine finished", "")
        completed.pid = 4242
        return completed

    _run.calls = calls
    return _run


class LifecycleCase(unittest.TestCase):
    """One in-memory repository, the real components, no network."""

    def setUp(self):
        self.hub = FakeGitHub()
        self._env = dict(os.environ)
        for key in list(os.environ):
            if key.startswith("FACTORY_WORKER"):
                del os.environ[key]
        os.environ["GITHUB_TOKEN"] = "test-token"
        # The legacy adapter path: one command, no health probe. The worker
        # contract has its own tests; what this file exercises is the lifecycle.
        os.environ["FACTORY_WORKER_CMD"] = "/usr/bin/true"
        self._api = mock.patch.object(dispatcher, "_api", self.hub)
        self._api.start()
        # Continuation reads projects at bells through its own client and has
        # its own tests; it is not part of this flow.
        self._continuation = mock.patch.object(poller, "run_continuation", return_value=[])
        self._continuation.start()

    def tearDown(self):
        self._continuation.stop()
        self._api.stop()
        os.environ.clear()
        os.environ.update(self._env)

    def dispatcher_stdout(self) -> str:
        """The real dispatcher, in-process, against the fake repository."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            dispatcher.main(["--repo", REPO, "--commitment", str(COMMITMENT), "--claim"])
        return buffer.getvalue()

    def poll(self, worker, seen=None):
        """One real poll cycle, with the engine faked at the process boundary."""
        with mock.patch.object(poller, "run_dispatcher",
                               side_effect=lambda *a, **k: self.dispatcher_stdout()), \
             mock.patch.object(workers, "run_observed", worker):
            return poller.poll_once(REPO, COMMITMENT, seen if seen is not None else set())


class TestSuccessfulStoryReachesATerminalState(LifecycleCase):
    def test_one_poll_takes_a_ready_story_all_the_way(self):
        worker = worker_that(self.hub, posts=True)
        woken = self.poll(worker)

        self.assertEqual([STORY], [d["story"] for d in woken], "the story was dispatched")
        self.assertEqual(1, len(worker.calls), "exactly one worker was launched")
        self.assertEqual(1, len(self.hub.acknowledgements(STORY)),
                         "exactly one acknowledgement exists")
        self.assertIn("story:completed", self.hub.labels(STORY))
        self.assertNotIn("story:claimed", self.hub.labels(STORY))
        self.assertEqual("closed", self.hub.issues[STORY]["state"])
        self.assertEqual("completed", self.hub.issues[STORY]["state_reason"],
                         "a terminal success, not an abandonment")

    def test_it_passes_through_claimed_on_the_way(self):
        """The completion is a transition out of a real claim, not a shortcut
        around one: `Attempt` is incremented by the dispatcher exactly once."""
        events = []
        original = self.hub._patch

        def watch(url, payload):
            result = original(url, payload)
            if "labels" in payload:
                events.append(sorted(n for n in payload["labels"] if n.startswith("story:")))
            return result

        with mock.patch.object(self.hub, "_patch", watch):
            self.poll(worker_that(self.hub, posts=True))
        self.assertEqual([["story:claimed"], ["story:completed"]], events)
        self.assertEqual("1", self.hub.section(STORY, "Attempt"))

    def test_the_reason_is_recorded_on_the_story(self):
        """§9.3 — a terminal transition must say what was decided and why."""
        self.poll(worker_that(self.hub, posts=True))
        recorded = [c for c in self.hub.comments[STORY]
                    if c["body"].startswith(completion.COMPLETION_HEADING)]
        self.assertEqual(1, len(recorded))
        self.assertIn("claude-delivery", recorded[0]["body"])

    def test_a_later_poll_does_not_relaunch_a_completed_story(self):
        self.poll(worker_that(self.hub, posts=True))
        second = worker_that(self.hub, posts=True)
        self.assertEqual([], self.poll(second, seen=set()))
        self.assertEqual([], second.calls)
        self.assertEqual(1, len(self.hub.acknowledgements(STORY)))

    def test_a_restart_after_the_lease_expires_still_does_not_relaunch(self):
        """The point of the transition. A claimed Story with no pull request is
        recovered by §9.4 once the lease elapses; a terminal one is not."""
        self.poll(worker_that(self.hub, posts=True))
        self.hub.age(hours=3)
        second = worker_that(self.hub, posts=True)
        self.assertEqual([], self.poll(second, seen=set()), "fresh process, empty state")
        self.assertEqual([], second.calls)
        self.assertEqual(1, len(self.hub.acknowledgements(STORY)))
        self.assertIn("story:completed", self.hub.labels(STORY))


class TestUnprovenWorkKeepsTheOldSafeBehaviour(LifecycleCase):
    def test_a_worker_that_posts_nothing_leaves_the_story_claimed(self):
        """#96's silent no-op, under the legacy adapter that cannot prove
        anything. No proof, no completion — the claim stands and §9.4 owns it."""
        self.poll(worker_that(self.hub, posts=False))
        self.assertIn("story:claimed", self.hub.labels(STORY))
        self.assertEqual("open", self.hub.issues[STORY]["state"])
        self.assertEqual([], self.hub.acknowledgements(STORY))

    def test_the_lease_still_recovers_an_unproven_claim(self):
        """The bounded loop #104 does not touch: expiry recovers the claim,
        decrements `Attempt`, and the story is dispatched again."""
        self.poll(worker_that(self.hub, posts=False))
        self.assertEqual("1", self.hub.section(STORY, "Attempt"))

        self.hub.age(hours=2)
        second = worker_that(self.hub, posts=True)
        self.assertEqual([STORY], [d["story"] for d in self.poll(second, seen=set())])
        self.assertEqual(1, len(second.calls), "the recovered story reached a worker")
        self.assertIn("story:completed", self.hub.labels(STORY),
                      "and this time it proved the work and completed")

    def test_a_failed_launch_leaves_the_story_claimed_and_is_loud(self):
        """A claim with nothing working it must never be silent, and must never
        be quietly closed by the runtime."""
        worker = worker_that(self.hub, posts=False, exit_code=1)
        with self.assertRaises(poller.WorkerLaunchFailed):
            self.poll(worker)
        self.assertIn("story:claimed", self.hub.labels(STORY))
        self.assertEqual("open", self.hub.issues[STORY]["state"])


class TestADeliveredStoryIsLeftToReview(LifecycleCase):
    def test_a_story_with_a_linked_pull_request_is_not_completed(self):
        """A worker that produced a deliverable belongs to review and the merge
        gate. This is the precondition that keeps the completion path safe as
        worker assignments grow beyond an acknowledgement."""
        def worker_with_a_pr(cmd, capture_output=True, text=True, timeout=None):
            self.hub.advance(minutes=1)
            self.hub.post_comment(STORY, ACKNOWLEDGEMENT)
            self.hub.pulls.append({"number": 200, "state": "open",
                                   "body": f"Story: #{STORY}\n", "merged_at": None})
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, "opened a PR", "")

        self.poll(worker_with_a_pr)
        self.assertIn("story:claimed", self.hub.labels(STORY))
        self.assertEqual("open", self.hub.issues[STORY]["state"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
