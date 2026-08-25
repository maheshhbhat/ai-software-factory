#!/usr/bin/env python3
"""Gate-discovered composition checks for Planning's Capacity Pool adapter."""

import json
import os
import subprocess
import unittest
from unittest import mock

from factory.agents.planning import invoke
from factory.agents.planning.test_artifacts import campaign_output
from factory.capacity_pool.router import ModelCapacity, Tier
from factory.capacity_pool.state import CapacityState


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


VALUE = {"trigger": {"labels": ["type:roadmap-commitment"]}}
OUTPUT = campaign_output()


class Clock:
    def __init__(self, *values):
        self.values = list(values)
        self.last = self.values[-1] if self.values else 0

    def __call__(self):
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class PlanningCapacityPoolAcceptance(unittest.TestCase):
    def run_quietly(self, *args, **kwargs):
        state = CapacityState()
        capabilities = frozenset({"reason", "json"})
        registry = (
            ModelCapacity("anthropic-balanced", "anthropic", Tier.BALANCED,
                          capabilities, prepaid_or_expiring=True),
            ModelCapacity("gpt-5.6-terra", "openai", Tier.BALANCED, capabilities),
        )
        for item in registry:
            state.mark_healthy(item.provider, item.name, "test-probe")
        try:
            with mock.patch.object(invoke.obs, "operational_log"):
                return invoke.run_model(*args, **kwargs, state=state, registry=registry)
        finally:
            state.close()

    def test_fast_quota_failure_preserves_full_remainder_for_gpt_5_6_sol(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs["timeout"]))
            if command[0] == "claude":
                usage = json.dumps({"type": "result", "total_cost_usd": 0})
                return Result(1, usage, "You've hit your session limit")
            return Result(stdout=json.dumps(OUTPUT))

        with mock.patch.dict(os.environ, {}, clear=True):
            result = self.run_quietly(
                VALUE, 100, 10, runner=runner, clock=Clock(0, 0))
        self.assertEqual(OUTPUT, result)
        self.assertEqual([50, 100], [timeout for _, timeout in calls])
        self.assertIn("anthropic-balanced", calls[0][0])
        self.assertIn("gpt-5.6-terra", calls[1][0])
        self.assertIn('model_reasoning_effort="medium"', calls[1][0])

    def test_primary_timeout_leaves_only_reserved_logical_task_remainder(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs["timeout"]))
            if command[0] == "claude":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return Result(stdout=json.dumps(OUTPUT))

        with mock.patch.dict(os.environ, {}, clear=True):
            result = self.run_quietly(
                VALUE, 100, 10, runner=runner, clock=Clock(0, 0, 80))
        self.assertEqual(OUTPUT, result)
        self.assertEqual([50, 20], [timeout for _, timeout in calls])
        primary = calls[0][0]
        self.assertEqual("5.0", primary[primary.index("--max-budget-usd") + 1])

    def test_missing_primary_executable_consumes_no_provider_budget(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs["timeout"]))
            if command[0] == "claude":
                raise FileNotFoundError("claude")
            return Result(stdout=json.dumps(OUTPUT))

        with mock.patch.dict(os.environ, {}, clear=True):
            result = self.run_quietly(
                VALUE, 100, 10, runner=runner, clock=Clock(0, 0))
        self.assertEqual(OUTPUT, result)
        self.assertEqual([50, 100], [timeout for _, timeout in calls])

    def test_successful_malformed_output_stops_without_provider_switch(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return Result(stdout="not-json")

        with mock.patch.dict(os.environ, {}, clear=True), \
             self.assertRaisesRegex(invoke.InvocationError, "schema-invalid"):
            self.run_quietly(VALUE, 100, 10, runner=runner)
        self.assertEqual(["claude"], [command[0] for command in calls])

    def test_unknown_nonzero_primary_failure_stops_without_provider_switch(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return Result(3, stderr="invalid command option")

        with mock.patch.dict(os.environ, {}, clear=True), \
             self.assertRaisesRegex(invoke.InvocationError, "unknown-failure"):
            self.run_quietly(VALUE, 100, 10, runner=runner)
        self.assertEqual(["claude"], [command[0] for command in calls])


if __name__ == "__main__":
    unittest.main()
