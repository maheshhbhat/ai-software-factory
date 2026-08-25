#!/usr/bin/env python3
"""Gate-discovered composition checks for Planning's Capacity Pool adapter."""

import json
import os
import subprocess
import unittest
from unittest import mock

from factory.agents.planning import invoke


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


VALUE = {"trigger": {"labels": ["type:roadmap-commitment"]}}
OUTPUT = {"contract": "schema-bound by the production command"}


class PlanningCapacityPoolAcceptance(unittest.TestCase):
    def run_quietly(self, *args, **kwargs):
        with mock.patch.object(invoke.obs, "operational_log"):
            return invoke.run_model(*args, **kwargs)

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
                VALUE, 100, 10, runner=runner, clock=iter((0, 0)).__next__)
        self.assertEqual(OUTPUT, result)
        self.assertEqual([80, 100], [timeout for _, timeout in calls])
        self.assertIn("claude-fable-5", calls[0][0])
        self.assertIn("gpt-5.6-sol", calls[1][0])
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
                VALUE, 100, 10, runner=runner, clock=iter((0, 80)).__next__)
        self.assertEqual(OUTPUT, result)
        self.assertEqual([80, 20], [timeout for _, timeout in calls])
        primary = calls[0][0]
        self.assertEqual("8.0", primary[primary.index("--max-budget-usd") + 1])

    def test_missing_primary_executable_consumes_no_provider_budget(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs["timeout"]))
            if command[0] == "claude":
                raise FileNotFoundError("claude")
            return Result(stdout=json.dumps(OUTPUT))

        with mock.patch.dict(os.environ, {}, clear=True):
            result = self.run_quietly(
                VALUE, 100, 10, runner=runner, clock=iter((0, 0)).__next__)
        self.assertEqual(OUTPUT, result)
        self.assertEqual([80, 100], [timeout for _, timeout in calls])

    def test_successful_malformed_output_stops_without_provider_switch(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return Result(stdout="not-json")

        with mock.patch.dict(os.environ, {}, clear=True), \
             self.assertRaisesRegex(invoke.InvocationError, "malformed JSON"):
            self.run_quietly(VALUE, 100, 10, runner=runner)
        self.assertEqual(["claude"], [command[0] for command in calls])

    def test_unknown_nonzero_primary_failure_stops_without_provider_switch(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return Result(3, stderr="invalid command option")

        with mock.patch.dict(os.environ, {}, clear=True), \
             self.assertRaisesRegex(invoke.InvocationError, "without eligible fallback"):
            self.run_quietly(VALUE, 100, 10, runner=runner)
        self.assertEqual(["claude"], [command[0] for command in calls])


if __name__ == "__main__":
    unittest.main()
