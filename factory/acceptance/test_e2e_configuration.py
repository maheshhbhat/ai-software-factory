#!/usr/bin/env python3
"""Regression tests for the live E2E worker-boundary preflight."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import e2e
import e2e_scenarios


class LiveWorkerConfiguration(unittest.TestCase):
    def error_for(self, **environment: str) -> str:
        with mock.patch.dict(os.environ, environment, clear=True):
            return e2e.worker_configuration_error()

    def test_refuses_the_legacy_wake_stub(self):
        error = self.error_for(
            FACTORY_WORKER_ORDER="claude-delivery",
            FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH=(
                "printf WAKE worker=claude-delivery story=#{story} project=#{project}"))
        self.assertIn("does not launch factory/runtime/bridge.py", error)

    def test_refuses_an_incomplete_bridge_declaration(self):
        error = self.error_for(
            FACTORY_WORKER_ORDER="capacity-delivery",
            FACTORY_WORKER_CAPACITY_DELIVERY_LAUNCH="python3 factory/runtime/bridge.py")
        self.assertIn("placeholders", error)

    def test_refuses_a_provider_specific_worker(self):
        error = self.error_for(
            FACTORY_WORKER_ORDER="claude-delivery",
            FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH=(
                "python3 factory/runtime/bridge.py "
                "--story {story} --project {project}"))
        self.assertIn("bypasses the capacity-delivery boundary", error)

    def test_accepts_the_capacity_pool_bridge_declaration(self):
        error = self.error_for(
            FACTORY_WORKER_ORDER="capacity-delivery",
            FACTORY_WORKER_CAPACITY_DELIVERY_LAUNCH=(
                "python3 factory/runtime/bridge.py "
                "--story {story} --project {project}"))
        self.assertEqual("", error)

    def test_failover_scenario_breaks_the_configured_primary(self):
        with mock.patch.dict(os.environ, {
                "FACTORY_WORKER_ORDER": "capacity-delivery"}, clear=True):
            self.assertEqual("FACTORY_WORKER_CAPACITY_DELIVERY_LAUNCH",
                             e2e_scenarios.primary_worker_launch_key())


if __name__ == "__main__":
    unittest.main(verbosity=2)
