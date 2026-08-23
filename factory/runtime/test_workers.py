#!/usr/bin/env python3
"""Tests for the standard worker contract. Standard library only.

Run: python3 -m unittest discover -s factory/runtime -p 'test_*.py' -v

`TestFailoverSafety` is the class that matters. Failover is the first mechanism
in the factory that could put two workers on one Story, and the property
defending against that — a definite failure permits fallback, an ambiguous
outcome does not — is easy to "simplify" away later. These tests exist to make
that regression loud.

`TestAdaptersShareTheContract` runs Claude and Codex through identical
assertions, which is what "swappable" has to mean if it means anything.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

import workers as w


def setUpModule():
    """Operational logging is a side effect of these tests, not their subject."""
    os.environ["FACTORY_RUNTIME_LOG_STDERR"] = "0"
    os.environ.setdefault("FACTORY_RUNTIME_LOG",
                          os.path.join(tempfile.gettempdir(), "factory-runtime-test.jsonl"))


def spec(name="claude-delivery", launch="/usr/bin/true", health="",
         caps=("delivery",)):
    return w.WorkerSpec(name=name, launch=launch, health=health,
                        capabilities=frozenset(caps))


def runner_returning(code=0, stdout="", stderr="", raises=None):
    """A fake subprocess.run with a fixed outcome."""
    def _run(cmd, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)
    return _run


def runner_by_command(mapping, default_code=0):
    """Outcome chosen by the first token of the command — lets one fake stand in
    for several different workers in the same test."""
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        key = cmd[0]
        outcome = mapping.get(key, default_code)
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(cmd, outcome, "", "")
    _run.calls = calls
    return _run


class TestWorkerSpec(unittest.TestCase):
    def test_valid_spec(self):
        self.assertIsNone(spec().validate())

    def test_name_must_be_a_valid_agent_id(self):
        for bad in ("", "X", "Claude-Delivery", "claude delivery", "a" * 40):
            self.assertIsNotNone(spec(name=bad).validate(), bad)

    def test_launch_is_required(self):
        self.assertIsNotNone(spec(launch="").validate())


class TestHealth(unittest.TestCase):
    def test_no_probe_means_assumed_healthy(self):
        report = w.check_health(spec(health=""))
        self.assertTrue(report.healthy)
        self.assertEqual(report.reason, "NO_PROBE_CONFIGURED")

    def test_zero_exit_is_healthy(self):
        report = w.check_health(spec(health="/bin/probe"), runner=runner_returning(0))
        self.assertTrue(report.healthy)

    def test_nonzero_exit_is_unhealthy(self):
        report = w.check_health(spec(health="/bin/probe"),
                                runner=runner_returning(1, stderr="no auth"))
        self.assertFalse(report.healthy)
        self.assertEqual(report.reason, "PROBE_FAILED")

    def test_missing_probe_binary_is_unhealthy_not_an_error(self):
        report = w.check_health(spec(health="/nope/probe"),
                                runner=runner_returning(raises=OSError("no such file")))
        self.assertFalse(report.healthy)
        self.assertEqual(report.reason, "PROBE_UNRUNNABLE")

    def test_slow_probe_is_unhealthy_not_ambiguous(self):
        """A probe runs nothing, so a timeout is safe to treat as unusable."""
        report = w.check_health(
            spec(health="/bin/probe"),
            runner=runner_returning(raises=subprocess.TimeoutExpired("probe", 20)))
        self.assertFalse(report.healthy)
        self.assertEqual(report.reason, "PROBE_TIMEOUT")


class TestCapability(unittest.TestCase):
    def test_declared_capability_is_required(self):
        self.assertTrue(w.is_capable(spec(caps=("delivery",)), "delivery"))
        self.assertFalse(w.is_capable(spec(caps=("review",)), "delivery"))

    def test_empty_declaration_claims_nothing(self):
        self.assertFalse(w.is_capable(spec(caps=()), "delivery"))


class TestSelection(unittest.TestCase):
    """§84 item 4 — deterministic, from configuration, no judgment."""

    def test_preference_order_is_configuration_not_scoring(self):
        specs = [spec("claude-delivery"), spec("codex-delivery")]
        eligible, _ = w.select(specs, runner=runner_returning(0))
        self.assertEqual([s.name for s in eligible], ["claude-delivery", "codex-delivery"])

        reversed_specs = [spec("codex-delivery"), spec("claude-delivery")]
        eligible, _ = w.select(reversed_specs, runner=runner_returning(0))
        self.assertEqual([s.name for s in eligible], ["codex-delivery", "claude-delivery"])

    def test_incapable_worker_is_excluded_with_a_reason(self):
        specs = [spec("claude-delivery", caps=("review",)), spec("codex-delivery")]
        eligible, reports = w.select(specs, runner=runner_returning(0))
        self.assertEqual([s.name for s in eligible], ["codex-delivery"])
        self.assertEqual(reports[0].reason, w.Result.INCAPABLE)

    def test_unhealthy_worker_is_excluded(self):
        specs = [spec("claude-delivery", health="/bin/a"), spec("codex-delivery", health="/bin/b")]
        eligible, _ = w.select(specs, runner=runner_by_command({"/bin/a": 1, "/bin/b": 0}))
        self.assertEqual([s.name for s in eligible], ["codex-delivery"])

    def test_selection_is_reproducible(self):
        specs = [spec("claude-delivery"), spec("codex-delivery")]
        first, _ = w.select(specs, runner=runner_returning(0))
        second, _ = w.select(specs, runner=runner_returning(0))
        self.assertEqual([s.name for s in first], [s.name for s in second])


class TestFailoverSafety(unittest.TestCase):
    """The correctness property. At most one worker per Story, ever."""

    def setUp(self):
        self.claude = spec("claude-delivery", launch="/bin/claude {story}")
        self.codex = spec("codex-delivery", launch="/bin/codex {story}")

    def test_healthy_preferred_worker_wins_and_nobody_else_runs(self):
        run = runner_by_command({"/bin/claude": 0, "/bin/codex": 0})
        report, trail = w.dispatch_to_worker([self.claude, self.codex], 64, 55, runner=run)
        self.assertEqual(report.worker, "claude-delivery")
        launched = [r for r in trail if isinstance(r, w.LaunchReport)]
        self.assertEqual(len(launched), 1, "only the selected worker may be launched")

    def test_definite_failure_falls_back(self):
        """Non-zero exit proves the worker did not start, so fallback is safe."""
        run = runner_by_command({"/bin/claude": 3, "/bin/codex": 0})
        report, trail = w.dispatch_to_worker([self.claude, self.codex], 64, 55, runner=run)
        self.assertEqual(report.worker, "codex-delivery")
        self.assertEqual([r.result for r in trail if isinstance(r, w.LaunchReport)],
                         [w.Result.FAILED, w.Result.LAUNCHED])

    def test_missing_binary_falls_back(self):
        run = runner_by_command({"/bin/claude": FileNotFoundError("no claude"),
                                 "/bin/codex": 0})
        report, _ = w.dispatch_to_worker([self.claude, self.codex], 64, 55, runner=run)
        self.assertEqual(report.worker, "codex-delivery")

    def test_ambiguous_outcome_never_falls_back(self):
        """THE test. A timeout means the worker may be running; starting a second
        would put two workers on one Story. Fail closed instead."""
        run = runner_by_command({"/bin/claude": subprocess.TimeoutExpired("claude", 60),
                                 "/bin/codex": 0})
        report, trail = w.dispatch_to_worker([self.claude, self.codex], 64, 55, runner=run)
        self.assertIsNone(report, "must not report success")
        launched = [r for r in trail if isinstance(r, w.LaunchReport)]
        self.assertEqual([r.result for r in launched], [w.Result.AMBIGUOUS])
        self.assertNotIn("/bin/codex", [c[0] for c in run.calls],
                         "the fallback worker must never be launched after an ambiguity")

    def test_ambiguous_report_does_not_permit_fallback(self):
        self.assertFalse(w.LaunchReport("x", w.Result.AMBIGUOUS).may_fall_back)
        self.assertTrue(w.LaunchReport("x", w.Result.FAILED).may_fall_back)

    def test_all_workers_failing_launches_nobody(self):
        run = runner_by_command({"/bin/claude": 1, "/bin/codex": 1})
        report, trail = w.dispatch_to_worker([self.claude, self.codex], 64, 55, runner=run)
        self.assertIsNone(report)
        self.assertEqual(len([r for r in trail if isinstance(r, w.LaunchReport)]), 2)

    def test_no_eligible_worker_launches_nothing(self):
        run = runner_by_command({})
        unhealthy = spec("claude-delivery", launch="/bin/claude", health="/bin/probe")
        report, trail = w.dispatch_to_worker(
            [unhealthy], 64, 55, runner=runner_by_command({"/bin/probe": 1}))
        self.assertIsNone(report)
        self.assertEqual([r for r in trail if isinstance(r, w.LaunchReport)], [])

    def test_every_attempt_is_recorded_for_audit(self):
        run = runner_by_command({"/bin/claude": 1, "/bin/codex": 1})
        _, trail = w.dispatch_to_worker([self.claude, self.codex], 64, 55, runner=run)
        self.assertTrue(all(r.detail or r.reason for r in trail),
                        "a Story that ran nowhere must say why, per engine")


class TestAdaptersShareTheContract(unittest.TestCase):
    """Claude and Codex under identical assertions — that is what swappable means."""

    def setUp(self):
        self._env = dict(os.environ)
        for key in list(os.environ):
            if key.startswith("FACTORY_WORKER"):
                del os.environ[key]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def configure_both(self):
        os.environ["FACTORY_WORKER_ORDER"] = "claude-delivery,codex-delivery"
        os.environ["FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH"] = "/bin/claude-wake {story}"
        os.environ["FACTORY_WORKER_CLAUDE_DELIVERY_HEALTH"] = "/bin/claude-probe"
        os.environ["FACTORY_WORKER_CODEX_DELIVERY_LAUNCH"] = "/bin/codex-wake {story}"
        os.environ["FACTORY_WORKER_CODEX_DELIVERY_HEALTH"] = "/bin/codex-probe"

    def test_both_adapters_load_and_validate(self):
        self.configure_both()
        specs = w.configured_workers()
        self.assertEqual([s.name for s in specs], ["claude-delivery", "codex-delivery"])
        for s in specs:
            self.assertIsNone(s.validate(), s.name)
            self.assertTrue(w.is_capable(s, "delivery"), s.name)

    def test_both_adapters_pass_the_same_health_assertions(self):
        self.configure_both()
        for s in w.configured_workers():
            self.assertTrue(w.check_health(s, runner=runner_returning(0)).healthy, s.name)
            self.assertFalse(w.check_health(s, runner=runner_returning(1)).healthy, s.name)

    def test_both_adapters_receive_routing_identity_only(self):
        self.configure_both()
        for s in w.configured_workers():
            cmd = " ".join(w.launch_command(s, 64, 55))
            self.assertIn("64", cmd, s.name)
            for leaked in ("Spec", "Scope", "Acceptance", "Depends-on", "criteria"):
                self.assertNotIn(leaked, cmd, f"{s.name} received business context")

    def test_either_worker_can_be_preferred_without_code_change(self):
        self.configure_both()
        os.environ["FACTORY_WORKER_ORDER"] = "codex-delivery,claude-delivery"
        self.assertEqual([s.name for s in w.configured_workers()],
                         ["codex-delivery", "claude-delivery"])

    def test_unconfigured_worker_is_simply_absent(self):
        os.environ["FACTORY_WORKER_ORDER"] = "claude-delivery,ghost-delivery"
        os.environ["FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH"] = "/bin/claude {story}"
        self.assertEqual([s.name for s in w.configured_workers()], ["claude-delivery"])


class TestNoPolicyInAdapters(unittest.TestCase):
    """#84 item 1: the factory owns policy; workers execute. Asserted by
    inspection, because a policy check smuggled into an adapter is invisible in
    behavioural tests until it disagrees with GitHub."""

    def test_module_contains_no_lifecycle_or_authorization_logic(self):
        with open(w.__file__, encoding="utf-8") as handle:
            source = handle.read()
        body = source.split('"""', 2)[-1]
        for leaked in ("story:ready", "story:claimed", "project:active", "type:story",
                       "author_association", "WIP", "Attempt", "RECOVERY_MAX",
                       "story:blocked", "merge-gate"):
            self.assertNotIn(leaked, body,
                             f"worker contract must not contain factory policy ({leaked})")

    def test_module_makes_no_github_calls(self):
        with open(w.__file__, encoding="utf-8") as handle:
            body = handle.read().split('"""', 2)[-1]
        for leaked in ("api.github.com", "urllib", "GITHUB_TOKEN", "gh "):
            self.assertNotIn(leaked, body,
                             f"worker contract must not touch GitHub ({leaked})")


class TestLaunchIsObservable(unittest.TestCase):
    """#104 — a run must be diagnosable afterwards with no live session. None of
    this may change a verdict; it only records what produced one."""

    def test_a_successful_launch_records_what_was_observed(self):
        report = w.launch(spec(), 104, 95,
                          runner=runner_returning(0, stdout="ok", stderr="noise"))
        self.assertEqual(0, report.exit_code)
        self.assertEqual("ok", report.stdout)
        self.assertIsNotNone(report.elapsed_ms)

    def test_both_streams_are_kept_whichever_way_it_exits(self):
        """The verdict used to decide which stream survived — a launched
        worker's stderr and a failed worker's stdout were discarded, and for a
        hang that was the half with the explanation in it."""
        launched = w.launch(spec(), 104, 95,
                            runner=runner_returning(0, stdout="out", stderr="err"))
        failed = w.launch(spec(), 104, 95,
                          runner=runner_returning(3, stdout="out", stderr="err"))
        for report in (launched, failed):
            self.assertEqual("out", report.stdout)
            self.assertEqual("err", report.stderr)

    def test_a_timeout_keeps_whatever_the_worker_managed_to_print(self):
        """The ambiguous case is the one that most needs its output kept."""
        timeout = subprocess.TimeoutExpired(cmd="w", timeout=1,
                                            output="half a line", stderr="")
        report = w.launch(spec(), 104, 95, runner=runner_returning(raises=timeout))
        self.assertEqual(w.Result.AMBIGUOUS, report.result)
        self.assertEqual("half a line", report.stdout)

    def test_observation_never_changes_a_verdict(self):
        """The routing contract is `result`; the rest is evidence."""
        for code, expected in ((0, w.Result.LAUNCHED), (1, w.Result.FAILED)):
            self.assertEqual(expected,
                             w.launch(spec(), 104, 95,
                                      runner=runner_returning(code)).result)

    def test_a_real_launch_reports_the_worker_pid(self):
        """The first question about a hang is which process to look at, and
        `subprocess.run` never exposes it."""
        report = w.launch(spec(launch="/bin/echo hello"), 104, 95)
        self.assertEqual(w.Result.LAUNCHED, report.result)
        self.assertIsInstance(report.pid, int)
        self.assertGreater(report.pid, 0)

    def test_run_observed_matches_subprocess_run_on_success(self):
        completed = w.run_observed(["/bin/echo", "hi"], timeout=30)
        self.assertEqual(0, completed.returncode)
        self.assertEqual("hi\n", completed.stdout)

    def test_run_observed_kills_the_child_and_raises_like_run(self):
        """Timeout semantics must be identical, or the caller's ambiguous-outcome
        handling — the rule that stops two workers on one Story — changes."""
        with self.assertRaises(subprocess.TimeoutExpired):
            w.run_observed(["/bin/sleep", "5"], timeout=0.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestLaunchCapComesFromTheStory(unittest.TestCase):
    """#345 — the launcher must not kill the worker before the bound the Story
    set. A fixed 60s cap bounded the Phase 2 acknowledgement bridge; applied to
    a real delivery it killed every worker at sixty seconds and recorded
    AMBIGUOUS (#316 took ~7 minutes)."""

    BODY = ("### Spec\n\nwork\n\n### Spend cap\n\n$5 / 60 min\n\n"
            "### Scope\n\nsomething.py\n")

    def test_the_declared_cap_plus_grace_is_the_bound(self):
        self.assertEqual(60 * 60 + w.LAUNCH_GRACE_SECONDS,
                         w.launch_timeout(self.BODY))

    def test_a_missing_or_unparseable_cap_selects_the_fallback(self):
        for body in (None, "", "### Spec\n\nno cap section\n",
                     "### Spend cap\n\nunbounded\n"):
            self.assertEqual(w.fallback_timeout(), w.launch_timeout(body))

    def test_the_fallback_is_operator_adjustable_at_call_time(self):
        os.environ["FACTORY_LAUNCH_TIMEOUT_SECONDS"] = "90"
        try:
            self.assertEqual(90, w.fallback_timeout())
            self.assertEqual(90, w.launch_timeout("no sections at all"))
        finally:
            del os.environ["FACTORY_LAUNCH_TIMEOUT_SECONDS"]

    def test_a_worker_is_not_killed_before_the_story_bound(self):
        """The runner must be asked to wait the Story's bound, not 60s."""
        waited = []

        def recording(cmd, **kwargs):
            waited.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        w.launch(spec(), 104, 95, runner=recording, timeout_s=3720)
        self.assertEqual([3720], waited)

    def test_the_bound_travels_through_dispatch(self):
        waited = []

        def recording(cmd, **kwargs):
            waited.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        report, _ = w.dispatch_to_worker([spec()], 104, 95,
                                         runner=recording, timeout_s=3720)
        self.assertEqual(w.Result.LAUNCHED, report.result)
        self.assertIn(3720, waited)

    def test_a_genuine_overrun_is_still_ambiguous_with_failover_suppressed(self):
        """Raising the bound must not soften the verdict past it."""
        timeout = subprocess.TimeoutExpired(cmd="w", timeout=3720)
        report, trail = w.dispatch_to_worker(
            [spec(), spec(name="second-worker")], 104, 95,
            runner=runner_returning(raises=timeout), timeout_s=3720)
        self.assertIsNone(report)
        launches = [r for r in trail if isinstance(r, w.LaunchReport)]
        self.assertEqual(1, len(launches), "no second worker after AMBIGUOUS")
        self.assertEqual(w.Result.AMBIGUOUS, launches[0].result)


class TestATimeoutKillsTheWholeProcessGroup(unittest.TestCase):
    """#345 — the 2h56m hang. `kill()` alone left the engine grandchild alive
    holding the inherited pipes, and the bare `communicate()` that followed
    blocked until the grandchild exited, freezing the single-threaded poller.
    The cap must surface within seconds of expiring, grandchildren included."""

    def test_timeout_surfaces_despite_a_pipe_holding_grandchild(self):
        import sys as _sys
        import time as _time
        child = (
            "import subprocess, time, sys\n"
            "subprocess.Popen(['sleep', '60'])\n"   # inherits our pipes
            "time.sleep(60)\n"
        )
        started = _time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            w.run_observed([_sys.executable, "-c", child],
                           capture_output=True, text=True, timeout=1)
        elapsed = _time.monotonic() - started
        self.assertLess(elapsed, 10,
                        f"timeout took {elapsed:.1f}s to surface; the poller "
                        f"would have been frozen behind the grandchild's pipes")
