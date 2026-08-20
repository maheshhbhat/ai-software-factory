#!/usr/bin/env python3
"""Tests for the persistent dispatcher runtime. Standard library only.

Run: python3 -m unittest discover -s factory/runtime -p 'test_*.py' -v

The parsing tests carry the weight here. That one line is the boundary between
deciding and doing, so the near-miss cases matter more than the happy path: a
runtime that accepts a slightly-wrong line is a runtime that launches work
nobody authorized.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

import poller


CANONICAL = "DISPATCH story=#64 project=#55 agent=claude-delivery"

REPORT = f"""Dispatcher — 4 issue(s) considered, WIP 0/2

  #10   skip      NOT_READY (story:merged)
  #64   ELIGIBLE  ELIGIBLE

Selected (claimed, in order): #64
  #64: Attempt 0 -> 1; labels phase:test story:claimed type:story
{CANONICAL}
"""


class TestParsing(unittest.TestCase):
    def test_canonical_line_is_parsed(self):
        self.assertEqual(poller.parse_dispatches(REPORT),
                         [{"story": 64, "project": 55, "agent": "claude-delivery"}])

    def test_report_without_dispatch_yields_nothing(self):
        quiet = "Dispatcher — 4 issue(s) considered, WIP 2/2\n\nCapacity exhausted"
        self.assertEqual(poller.parse_dispatches(quiet), [])

    def test_multiple_dispatches_preserve_order(self):
        text = (f"{CANONICAL}\n"
                "DISPATCH story=#70 project=#55 agent=claude-delivery")
        self.assertEqual([d["story"] for d in poller.parse_dispatches(text)], [64, 70])

    def test_near_miss_lines_fail_closed(self):
        """Anything DISPATCH-shaped but not canonical is an error, not a shrug."""
        for bad in (
            "DISPATCH story=64 project=#55 agent=claude-delivery",     # no # on story
            "DISPATCH story=#64 project=#55",                          # no agent
            "DISPATCH story=#64 agent=claude-delivery project=#55",    # reordered
            "DISPATCH story=#64 project=#55 agent=claude delivery",    # space in agent
            "DISPATCH story=#64 project=#55 agent=Claude-Delivery",    # uppercase
            "DISPATCH  story=#64 project=#55 agent=claude-delivery",   # double space
            "DISPATCH story=#64 project=#55 agent=claude-delivery ; rm -rf /",
            "DISPATCHER story=#64 project=#55 agent=claude-delivery extra",
        ):
            with self.assertRaises(poller.MalformedDispatch, msg=bad):
                poller.parse_dispatches(bad)

    def test_injected_line_in_otherwise_normal_output_is_caught(self):
        """A malformed line anywhere aborts the whole poll — no partial trust."""
        with self.assertRaises(poller.MalformedDispatch):
            poller.parse_dispatches(REPORT + "\nDISPATCH story=#99 project=x agent=y")


class TestWorkerAdapter(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_default_adapter_announces_identity_only(self):
        os.environ.pop("FACTORY_WORKER_CMD", None)
        cmd = poller.worker_command({"story": 64, "project": 55, "agent": "claude-delivery"})
        joined = " ".join(cmd)
        self.assertIn("story=#64", joined)
        self.assertIn("project=#55", joined)

    def test_custom_adapter_substitutes_placeholders(self):
        os.environ["FACTORY_WORKER_CMD"] = "/bin/echo start {story} for {agent} in {project}"
        cmd = poller.worker_command({"story": 64, "project": 55, "agent": "codex-delivery"})
        self.assertEqual(cmd, ["/bin/echo", "start", "64", "for", "codex-delivery", "in", "55"])

    def test_adapter_receives_no_business_context(self):
        """§4: a queue item carries routing metadata and an artifact link, never
        business context. The worker reads the substrate itself."""
        os.environ.pop("FACTORY_WORKER_CMD", None)
        cmd = " ".join(poller.worker_command(
            {"story": 64, "project": 55, "agent": "claude-delivery"}))
        for leaked in ("Spec", "Scope", "Acceptance", "Depends-on"):
            self.assertNotIn(leaked, cmd)


class TestPollOnce(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ["FACTORY_WORKER_CMD"] = "/usr/bin/true"
        # Continuation is a separate concern with its own tests; stub it so
        # these exercise dispatch only and make no network call.
        self._continuation = mock.patch.object(poller, "run_continuation",
                                               return_value=[])
        self._continuation.start()

    def tearDown(self):
        self._continuation.stop()
        os.environ.clear()
        os.environ.update(self._env)

    def test_continuation_failure_does_not_block_dispatch(self):
        """A malformed decision comment must not halt authorized work."""
        seen: set[int] = set()
        with mock.patch.object(poller, "run_continuation",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            woken = poller.poll_once("o/r", 54, seen)
        self.assertEqual([d["story"] for d in woken], [64])

    def test_dispatch_wakes_the_worker_once(self):
        seen: set[int] = set()
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            woken = poller.poll_once("o/r", 54, seen)
        self.assertEqual([d["story"] for d in woken], [64])
        self.assertEqual(seen, {64})

    def test_replay_does_not_wake_twice(self):
        """Two polls, same output. The second must not launch a worker again —
        and in production GitHub prevents it earlier, by not re-offering a
        claimed story."""
        seen: set[int] = set()
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            first = poller.poll_once("o/r", 54, seen)
            second = poller.poll_once("o/r", 54, seen)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_poll_with_no_dispatch_does_nothing(self):
        seen: set[int] = set()
        with mock.patch.object(poller, "run_dispatcher",
                               return_value="Dispatcher — no eligible work"):
            self.assertEqual(poller.poll_once("o/r", 54, seen), [])
        self.assertEqual(seen, set())

    def test_dispatcher_failure_propagates_and_wakes_nobody(self):
        seen: set[int] = set()
        with mock.patch.object(poller, "run_dispatcher",
                               side_effect=poller.DispatcherFailed("exit 1")):
            with self.assertRaises(poller.DispatcherFailed):
                poller.poll_once("o/r", 54, seen)
        self.assertEqual(seen, set())

    def test_worker_launch_failure_is_loud_and_does_not_mark_seen(self):
        """The claim already landed in GitHub. A silent failure here would leave
        a story claimed with nothing working it."""
        seen: set[int] = set()
        os.environ["FACTORY_WORKER_CMD"] = "/nonexistent/worker"
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            with self.assertRaises(poller.WorkerLaunchFailed):
                poller.poll_once("o/r", 54, seen)
        self.assertEqual(seen, set())

    def test_worker_nonzero_exit_is_a_launch_failure(self):
        seen: set[int] = set()
        os.environ["FACTORY_WORKER_CMD"] = "/usr/bin/false"
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            with self.assertRaises(poller.WorkerLaunchFailed):
                poller.poll_once("o/r", 54, seen)

    def test_malformed_output_wakes_nobody(self):
        seen: set[int] = set()
        with mock.patch.object(poller, "run_dispatcher",
                               return_value="DISPATCH story=#64 project=bad agent=x"):
            with self.assertRaises(poller.MalformedDispatch):
                poller.poll_once("o/r", 54, seen)
        self.assertEqual(seen, set())


class TestNoLocalAuthority(unittest.TestCase):
    """§9.12 / architecture §4: GitHub is the source of truth. Local process
    state is a convenience and must never decide what may run."""

    def test_seen_set_is_not_persisted_anywhere(self):
        with open(poller.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for persistence in ("open(", "json.dump", "sqlite", ".write(", "pickle"):
            self.assertNotIn(persistence, source.split('"""', 2)[-1],
                             f"runtime must not persist local state ({persistence})")

    def test_restart_with_empty_state_redispatches_nothing_extra(self):
        """A fresh process re-derives behaviour from the dispatcher alone: if the
        story is claimed, the dispatcher stops offering it, so a restart is
        latency, not a duplicate."""
        with mock.patch.object(poller, "run_continuation", return_value=[]), \
             mock.patch.object(poller, "run_dispatcher",
                               return_value="Dispatcher — no eligible work"):
            self.assertEqual(poller.poll_once("o/r", 54, set()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWorkerContractRouting(unittest.TestCase):
    """#84 — the poller routes through the contract when workers are declared,
    and keeps the legacy path when they are not."""

    def setUp(self):
        self._env = dict(os.environ)
        for key in list(os.environ):
            if key.startswith("FACTORY_WORKER"):
                del os.environ[key]
        self._continuation = mock.patch.object(poller, "run_continuation", return_value=[])
        self._continuation.start()

    def tearDown(self):
        self._continuation.stop()
        os.environ.clear()
        os.environ.update(self._env)

    def test_declared_workers_route_through_the_contract(self):
        os.environ["FACTORY_WORKER_ORDER"] = "claude-delivery"
        os.environ["FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH"] = "/usr/bin/true"
        seen: set[int] = set()
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            woken = poller.poll_once("o/r", 54, seen)
        self.assertEqual([d["story"] for d in woken], [64])

    def test_contract_failure_surfaces_as_a_launch_failure(self):
        """A Story claimed with no worker started must be loud, not silent."""
        os.environ["FACTORY_WORKER_ORDER"] = "claude-delivery"
        os.environ["FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH"] = "/usr/bin/false"
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            with self.assertRaises(poller.WorkerLaunchFailed):
                poller.poll_once("o/r", 54, set())

    def test_no_declared_workers_keeps_the_legacy_path(self):
        os.environ["FACTORY_WORKER_CMD"] = "/usr/bin/true"
        seen: set[int] = set()
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            woken = poller.poll_once("o/r", 54, seen)
        self.assertEqual([d["story"] for d in woken], [64])

    def test_failover_reaches_the_secondary_worker(self):
        os.environ["FACTORY_WORKER_ORDER"] = "claude-delivery,codex-delivery"
        os.environ["FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH"] = "/usr/bin/false"
        os.environ["FACTORY_WORKER_CODEX_DELIVERY_LAUNCH"] = "/usr/bin/true"
        seen: set[int] = set()
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            woken = poller.poll_once("o/r", 54, seen)
        self.assertEqual([d["story"] for d in woken], [64])
