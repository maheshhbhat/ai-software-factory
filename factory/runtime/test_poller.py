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
import io
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest import mock

import poller
import workers


def setUpModule():
    """Operational logging is a side effect of these tests, not their subject."""
    os.environ["FACTORY_RUNTIME_LOG_STDERR"] = "0"
    os.environ.setdefault("FACTORY_RUNTIME_LOG",
                          os.path.join(tempfile.gettempdir(), "factory-runtime-test.jsonl"))


CANONICAL = "DISPATCH story=#64 project=#55 agent=claude-delivery"

REPORT = f"""Dispatcher — 4 issue(s) considered, WIP 0/2

  #10   skip      NOT_READY (story:merged)
  #64   ELIGIBLE  ELIGIBLE

Selected (claimed, in order): #64
  #64: Attempt 0 -> 1; labels phase:test story:claimed type:story
{CANONICAL}
"""


class TestParsing(unittest.TestCase):
    def test_idle_interval_doubles_to_a_five_minute_cap(self):
        self.assertEqual(30, poller.adaptive_interval(15, 15, False))
        self.assertEqual(300, poller.adaptive_interval(15, 240, False))
        self.assertEqual(15, poller.adaptive_interval(15, 300, True))

    def test_rate_limit_delay_obeys_reset_and_has_a_sixty_second_floor(self):
        self.assertEqual(101, poller.rate_limit_delay(
            poller.RateLimitExhausted(200), now=100))
        self.assertEqual(60, poller.rate_limit_delay(
            poller.RateLimitExhausted(110), now=100))
    def test_review_routing_ignores_an_unrelated_pull_request(self):
        unrelated = {"number": 25,
                     "body": "ordinary repository work mentioning Story: in prose",
                     "state": "open", "draft": False}
        self.assertEqual([], poller.review_targets([unrelated], {}, {}))

    def test_review_routing_ignores_a_pull_for_an_absent_story(self):
        stale = {"number": 29, "body": "Story: #999", "state": "open", "draft": False}
        self.assertEqual([], poller.review_targets([stale], {}, {}))

    def test_phase4_review_pass_reads_the_live_substrate(self):
        """The production pass imports its dispatcher dependency itself."""
        with mock.patch.object(poller.dispatcher, "fetch_issues", return_value={}), \
             mock.patch.object(poller.dispatcher, "fetch_pull_requests", return_value=[]), \
             mock.patch.object(poller, "review_targets", return_value=[]) as targets:
            self.assertEqual([], poller.run_phase4_reviews("o/r"))
        targets.assert_called_once_with([], {}, {})

    def test_review_decision_is_read_again_after_reviewer_finishes(self):
        """An external reviewer write must not be hidden by an earlier read."""
        head = "a" * 40
        target = poller.review_route.ReviewTarget(10, 9, head)
        fresh_pull = {"number": 9, "head": {"sha": head}}
        fresh_comments = [
            {"body": f"<!-- review-outcome:9:{head}:approval -->"}]
        with mock.patch.object(poller.dispatcher, "fetch_issues",
                               return_value={10: {"number": 10}}), \
             mock.patch.object(poller.dispatcher, "fetch_pull_requests",
                               return_value=[]), \
             mock.patch.object(poller.dispatcher, "_api",
                               side_effect=[[], fresh_pull, fresh_comments]) as api, \
             mock.patch.object(poller, "review_targets", return_value=[target]), \
             mock.patch.object(poller, "wake_reviewer"), \
             mock.patch.object(poller, "route_merge") as merge:
            poller.run_phase4_reviews("o/r")
        self.assertEqual(3, api.call_count)
        merge.assert_called_once_with("o/r", fresh_pull, fresh_comments, apply=True)

    def test_review_target_is_once_per_exact_head(self):
        head = "a" * 40
        pull = {"number": 9, "state": "open", "draft": False,
                "body": "Story: #10\n", "head": {"sha": head}}
        story = {"number": 10, "labels": [{"name": "story:in-review"}]}
        targets = poller.review_targets([pull], {10: story}, {10: []})
        self.assertEqual([(x.pull_request, x.head) for x in targets], [(9, head)])
        marker = {"body": f"<!-- review-outcome:9:{head}:approval -->"}
        self.assertEqual(poller.review_targets([pull], {10: story}, {10: [marker]}), [])

    def test_merge_route_requires_current_head_approval(self):
        pull = {"number": 9, "head": {"sha": "b" * 40}}
        stale = {"body": "<!-- review-outcome:9:" + "a" * 40 + ":approval -->"}
        exact = {"body": "<!-- review-outcome:9:" + "b" * 40 + ":approval -->"}
        self.assertFalse(poller.route_merge("o/r", pull, [stale], apply=False))
        self.assertTrue(poller.route_merge("o/r", pull, [exact], apply=False))

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
        # A declared worker in the ambient environment would route these through
        # the contract instead of the legacy path they are written for.
        for key in list(os.environ):
            if key.startswith("FACTORY_WORKER"):
                del os.environ[key]
        os.environ["FACTORY_WORKER_CMD"] = "/usr/bin/true"
        # Reconciliation, continuation, sequencing, the human queue and completion are
        # separate concerns with their own tests; stub all five so these
        # exercise dispatch only and make no network call.
        self._passes = [
            mock.patch.object(poller, "run_review_link", return_value=[]),
            mock.patch.object(poller, "run_continuation", return_value=[]),
            mock.patch.object(poller, "run_sequencer", return_value=[]),
            mock.patch.object(poller, "run_planning_route", return_value=[]),
            mock.patch.object(poller, "run_human_queue", return_value=[]),
        ]
        for patcher in self._passes:
            patcher.start()
        self._completion = mock.patch.object(poller, "complete_story")
        self.completion = self._completion.start()
        # The launch bound is derived from the Story body over the network;
        # its derivation has its own tests. None selects the fallback.
        self._bound = mock.patch.object(poller, "story_launch_bound",
                                        return_value=None)
        self._bound.start()
        self._issues = mock.patch.object(poller.dispatcher, "fetch_issues",
                                         return_value={})
        self._issues.start()

    def tearDown(self):
        self._issues.stop()
        self._bound.stop()
        self._completion.stop()
        for patcher in reversed(self._passes):
            patcher.stop()
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

    def test_rate_limit_aborts_remaining_passes(self):
        limited = urllib.error.HTTPError(
            "url", 403, "Forbidden",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "200"}, None)
        with mock.patch.object(poller, "run_review_link", side_effect=limited), \
             mock.patch.object(poller, "run_continuation") as continuation, \
             mock.patch.object(poller, "run_dispatcher") as dispatch:
            with self.assertRaises(poller.RateLimitExhausted):
                poller.poll_once("o/r", 54, set())
        continuation.assert_not_called()
        dispatch.assert_not_called()

    def test_review_link_failure_does_not_block_dispatch(self):
        """A reconciliation failure must not halt authorized work, for the same
        reason a continuation failure must not: they answer different questions,
        and coupling their failure modes lets one bad pull request stop the
        factory."""
        seen: set[int] = set()
        with mock.patch.object(poller, "run_review_link",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            woken = poller.poll_once("o/r", 54, seen)
        self.assertEqual([d["story"] for d in woken], [64])

    def test_human_queue_failure_does_not_block_dispatch(self):
        seen: set[int] = set()
        with mock.patch.object(poller, "run_human_queue",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            woken = poller.poll_once("o/r", 54, seen)
        self.assertEqual([d["story"] for d in woken], [64])

    def test_sequencer_failure_does_not_block_dispatch(self):
        seen: set[int] = set()
        with mock.patch.object(poller, "run_sequencer",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            woken = poller.poll_once("o/r", 54, seen)
        self.assertEqual([d["story"] for d in woken], [64])

    def test_planning_route_invokes_wrapper_with_identity_only(self):
        with mock.patch.object(poller, "run_planning_route", return_value=[70]), \
             mock.patch.object(poller, "wake_planner") as wake, \
             mock.patch.object(poller, "run_dispatcher",
                               return_value="Dispatcher — no eligible work"):
            poller.poll_once("o/r", 54, set())
        wake.assert_called_once_with("o/r", 70)

    def test_planning_failure_does_not_block_story_dispatch(self):
        with mock.patch.object(poller, "run_planning_route", return_value=[70]), \
             mock.patch.object(poller, "wake_planner", side_effect=RuntimeError("boom")), \
             mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            woken = poller.poll_once("o/r", 54, set())
        self.assertEqual([64], [item["story"] for item in woken])

    def test_every_poll_says_what_is_waiting_on_a_human(self):
        """Not once per change — once per poll. A queue that reports only
        transitions goes quiet about a problem precisely because it is old."""
        seen: set[int] = set()
        with mock.patch.object(poller, "run_human_queue", return_value=[]) as queue, \
             mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            poller.poll_once("o/r", 54, seen)
            poller.poll_once("o/r", 54, seen)
        self.assertEqual(queue.call_count, 2)

    def test_reconciliation_runs_before_the_dispatcher(self):
        """A story whose delivery merged must leave `story:claimed` before WIP is
        counted, or finished work keeps a worker slot it no longer needs."""
        order = []
        with mock.patch.object(poller, "run_review_link",
                               side_effect=lambda *a, **k: order.append("review-link")), \
             mock.patch.object(poller, "run_dispatcher",
                               side_effect=lambda *a, **k: (order.append("dispatch"), REPORT)[1]):
            poller.poll_once("o/r", 54, set())
        self.assertEqual(order, ["review-link", "dispatch"])

    def test_dispatch_wakes_the_worker_once(self):
        seen: set[int] = set()
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT):
            woken = poller.poll_once("o/r", 54, seen)
        self.assertEqual([d["story"] for d in woken], [64])
        self.assertEqual(seen, {64})

    def test_sequencer_runs_before_the_dispatcher(self):
        order = []
        with mock.patch.object(poller, "run_sequencer",
                               side_effect=lambda *a, **k: order.append("sequencer")), \
             mock.patch.object(poller, "run_dispatcher",
                               side_effect=lambda *a, **k: (order.append("dispatch"), REPORT)[1]):
            poller.poll_once("o/r", 54, set())
        self.assertEqual(order, ["sequencer", "dispatch"])

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

    def test_confirmed_worker_failure_is_released_immediately(self):
        failure = poller.WorkerLaunchFailed("worker exited 1", definite=True)
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT), \
             mock.patch.object(poller, "wake_worker", side_effect=failure), \
             mock.patch.object(poller.dispatcher, "release_definite_failure",
                               return_value=(True, "released")) as release:
            result = poller.poll_once("o/r", 54, set())
        self.assertEqual([], result)
        release.assert_called_once()

    def test_ambiguous_worker_failure_keeps_the_claim(self):
        failure = poller.WorkerLaunchFailed("timeout", definite=False)
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT), \
             mock.patch.object(poller, "wake_worker", side_effect=failure), \
             mock.patch.object(poller.dispatcher,
                               "release_definite_failure") as release:
            with self.assertRaises(poller.WorkerLaunchFailed):
                poller.poll_once("o/r", 54, set())
        release.assert_not_called()

    def test_malformed_output_wakes_nobody(self):
        seen: set[int] = set()
        with mock.patch.object(poller, "run_dispatcher",
                               return_value="DISPATCH story=#64 project=bad agent=x"):
            with self.assertRaises(poller.MalformedDispatch):
                poller.poll_once("o/r", 54, seen)
        self.assertEqual(seen, set())


class ReviewerLaunchBounds(unittest.TestCase):
    def test_outer_bound_has_cleanup_margin_and_is_configurable(self):
        completed = poller.subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(poller.subprocess, "run", return_value=completed) as run, \
             mock.patch.dict(os.environ, {}, clear=True):
            poller.wake_reviewer("o/r", type("Target", (), {"pull_request": 9})())
        self.assertEqual(210, run.call_args.kwargs["timeout"])

        with mock.patch.object(poller.subprocess, "run", return_value=completed) as run, \
             mock.patch.dict(os.environ, {"FACTORY_REVIEW_LAUNCH_TIMEOUT": "240"},
                             clear=True):
            poller.wake_reviewer("o/r", type("Target", (), {"pull_request": 9})())
        self.assertEqual(240, run.call_args.kwargs["timeout"])


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
        with mock.patch.object(poller, "run_review_link", return_value=[]), \
             mock.patch.object(poller, "run_continuation", return_value=[]), \
             mock.patch.object(poller, "run_sequencer", return_value=[]), \
             mock.patch.object(poller, "run_planning_route", return_value=[]), \
             mock.patch.object(poller, "run_human_queue", return_value=[]), \
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
        self._passes = [
            mock.patch.object(poller, "run_review_link", return_value=[]),
            mock.patch.object(poller, "run_continuation", return_value=[]),
            mock.patch.object(poller, "run_sequencer", return_value=[]),
            mock.patch.object(poller, "run_planning_route", return_value=[]),
            mock.patch.object(poller, "run_human_queue", return_value=[]),
        ]
        for patcher in self._passes:
            patcher.start()
        self._issues = mock.patch.object(poller.dispatcher, "fetch_issues",
                                         return_value={})
        self._issues.start()
        self._completion = mock.patch.object(poller, "complete_story")
        self.completion = self._completion.start()
        # The launch bound is derived from the Story body over the network;
        # its derivation has its own tests. None selects the fallback.
        self._bound = mock.patch.object(poller, "story_launch_bound",
                                        return_value=None)
        self._bound.start()

    def tearDown(self):
        self._issues.stop()
        self._bound.stop()
        self._completion.stop()
        for patcher in reversed(self._passes):
            patcher.stop()
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
            with self.assertRaisesRegex(
                    poller.WorkerLaunchFailed,
                    "claude-delivery completed with failure"):
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


class TestCompletionIsDelegated(unittest.TestCase):
    """#104 — the poll asks whether a finished worker ends its Story, and holds
    none of the reasoning that answers it."""

    def setUp(self):
        self._env = dict(os.environ)
        for key in list(os.environ):
            if key.startswith("FACTORY_WORKER"):
                del os.environ[key]
        os.environ["FACTORY_WORKER_CMD"] = "/usr/bin/true"
        self._passes = [
            mock.patch.object(poller, "run_review_link", return_value=[]),
            mock.patch.object(poller, "run_continuation", return_value=[]),
            mock.patch.object(poller, "run_sequencer", return_value=[]),
            mock.patch.object(poller, "run_planning_route", return_value=[]),
            mock.patch.object(poller, "run_human_queue", return_value=[]),
        ]
        for patcher in self._passes:
            patcher.start()
        self._issues = mock.patch.object(poller.dispatcher, "fetch_issues",
                                         return_value={})
        self._issues.start()

    def tearDown(self):
        self._issues.stop()
        for patcher in reversed(self._passes):
            patcher.stop()
        os.environ.clear()
        os.environ.update(self._env)

    def test_a_successful_wake_asks_the_completion_path(self):
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT), \
             mock.patch.object(poller, "complete_story") as complete:
            poller.poll_once("o/r", 54, set())
        self.assertEqual(1, complete.call_count)
        dispatch, report, claim = complete.call_args[0]
        self.assertEqual(64, dispatch["story"])
        self.assertEqual("o/r", dispatch["repo"])
        self.assertEqual(workers.Result.LAUNCHED, report.result)
        self.assertTrue(claim)

    def test_a_failed_wake_never_reaches_the_completion_path(self):
        """Nothing succeeded, so there is nothing to complete."""
        os.environ["FACTORY_WORKER_CMD"] = "/usr/bin/false"
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT), \
             mock.patch.object(poller, "complete_story") as complete:
            with self.assertRaises(poller.WorkerLaunchFailed):
                poller.poll_once("o/r", 54, set())
        complete.assert_not_called()

    def test_a_dry_run_does_not_authorise_a_lifecycle_write(self):
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT), \
             mock.patch.object(poller, "complete_story") as complete:
            poller.poll_once("o/r", 54, set(), claim=False)
        self.assertFalse(complete.call_args[0][2])

    def test_a_dry_run_exposes_capacity_and_selection_through_the_poller(self):
        report = REPORT.replace("Selected (claimed, in order)",
                                "Selected (would claim, in order)")
        output = io.StringIO()
        with mock.patch.object(poller, "run_dispatcher", return_value=report), \
             redirect_stdout(output):
            poller.poll_once("o/r", 54, set(), claim=False)
        visible = output.getvalue()
        self.assertIn("[poller] dispatcher dry-run plan", visible)
        self.assertIn("WIP 0/2", visible)
        self.assertIn("Selected (would claim, in order): #64", visible)

    def test_a_completion_failure_does_not_stop_the_poll(self):
        """The Story stays claimed, which the §9.4 lease already resolves. A
        dispatch that already happened must not be undone by a later failure."""
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT), \
             mock.patch.object(poller, "complete_story",
                               side_effect=RuntimeError("github is down")):
            woken = poller.poll_once("o/r", 54, set())
        self.assertEqual([64], [d["story"] for d in woken])

    def test_the_poller_holds_no_completion_policy(self):
        """Every precondition and the target state live in `completion.py`. A
        check smuggled in here is invisible until it disagrees with GitHub."""
        with open(poller.__file__, encoding="utf-8") as handle:
            body = handle.read().split('"""', 2)[-1]
        for leaked in ("story:completed", "story:cancelled", "story:in-review",
                       "story:merged",
                       "looks_like_acknowledgement", "linked_delivery_prs",
                       "state_reason"):
            self.assertNotIn(leaked, body,
                             f"the runtime must not decide lifecycle ({leaked})")


class TestLaunchBoundComesFromTheStory(unittest.TestCase):
    """#345 — the poller reads the claimed Story's `### Spend cap` and hands
    the derived bound to the launcher; a fetch failure selects the fallback
    rather than blocking the dispatch."""

    BODY = "### Spec\n\nwork\n\n### Spend cap\n\n$5 / 60 min\n"

    def test_the_declared_cap_reaches_the_launcher(self):
        with mock.patch.object(poller.dispatcher, "fetch_issue",
                               return_value={"body": self.BODY}):
            bound = poller.story_launch_bound("o/r", 64)
        self.assertEqual(60 * 60 + poller.workers.LAUNCH_GRACE_SECONDS, bound)

    def test_a_fetch_failure_selects_the_fallback_not_a_blocked_dispatch(self):
        with mock.patch.object(poller.dispatcher, "fetch_issue",
                               side_effect=RuntimeError("network down")):
            self.assertIsNone(poller.story_launch_bound("o/r", 64))

    def test_the_bound_is_threaded_into_the_worker_contract(self):
        recorded = {}

        def recording(specs, story, project, required="delivery",
                      runner=None, timeout_s=None):
            recorded["timeout_s"] = timeout_s
            report = poller.workers.LaunchReport("claude-delivery", "LAUNCHED")
            return report, [report]

        env = dict(os.environ)
        try:
            os.environ["FACTORY_WORKER_ORDER"] = "claude-delivery"
            os.environ["FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH"] = "/usr/bin/true"
            with mock.patch.object(poller.workers, "dispatch_to_worker",
                                   side_effect=recording):
                poller.wake_worker({"story": 64, "project": 54,
                                    "agent": "claude-delivery"}, timeout_s=3720)
        finally:
            os.environ.clear()
            os.environ.update(env)
        self.assertEqual(3720, recorded["timeout_s"])


class TestTheSkipGuardDiesWithItsCycle(unittest.TestCase):
    """#342 — the `seen` set built once per process skipped every redispatch:
    a Story returned to `story:ready` by review findings was claimed again and
    no worker was ever launched (#328, 2026-08-23, head 6147e002 unchanged).
    The guard is belt-and-braces against a duplicate dispatch inside one
    cycle, and must not survive the cycle it was set in."""

    def setUp(self):
        self._env = dict(os.environ)
        for key in list(os.environ):
            if key.startswith("FACTORY_WORKER"):
                del os.environ[key]
        os.environ["FACTORY_WORKER_CMD"] = "/usr/bin/true"
        self._passes = [
            mock.patch.object(poller, "run_review_link", return_value=[]),
            mock.patch.object(poller, "run_continuation", return_value=[]),
            mock.patch.object(poller, "run_sequencer", return_value=[]),
            mock.patch.object(poller, "run_planning_route", return_value=[]),
            mock.patch.object(poller, "run_human_queue", return_value=[]),
            mock.patch.object(poller, "complete_story"),
            mock.patch.object(poller, "story_launch_bound", return_value=None),
        ]
        for patcher in self._passes:
            patcher.start()
        self._issues = mock.patch.object(poller.dispatcher, "fetch_issues",
                                         return_value={})
        self._issues.start()

    def tearDown(self):
        self._issues.stop()
        for patcher in reversed(self._passes):
            patcher.stop()
        os.environ.clear()
        os.environ.update(self._env)

    def test_a_redispatch_in_a_later_cycle_launches_a_worker_again(self):
        """The #328 defect: the second cycle must launch, not skip."""
        launches = []
        with mock.patch.object(poller, "run_dispatcher", return_value=REPORT), \
             mock.patch.object(poller, "wake_worker",
                               side_effect=lambda d, **kw: launches.append(d["story"])
                               or poller.workers.LaunchReport("claude-delivery",
                                                              "LAUNCHED")):
            poller.cycle("o/r", 54)
            poller.cycle("o/r", 54)
        self.assertEqual([64, 64], launches,
                         "a story redispatched after findings must be launched "
                         "again in the next cycle, not skipped forever")

    def test_a_duplicate_dispatch_inside_one_cycle_still_launches_once(self):
        doubled = REPORT + CANONICAL + "\n"
        launches = []
        with mock.patch.object(poller, "run_dispatcher", return_value=doubled), \
             mock.patch.object(poller, "wake_worker",
                               side_effect=lambda d, **kw: launches.append(d["story"])
                               or poller.workers.LaunchReport("claude-delivery",
                                                              "LAUNCHED")):
            poller.cycle("o/r", 54)
        self.assertEqual([64], launches)
