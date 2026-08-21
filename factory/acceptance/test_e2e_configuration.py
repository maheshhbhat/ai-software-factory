#!/usr/bin/env python3
"""Regression tests for the live E2E worker-boundary preflight."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import e2e


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
            FACTORY_WORKER_ORDER="claude-delivery",
            FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH="python3 factory/runtime/bridge.py")
        self.assertIn("placeholders", error)

    def test_refuses_the_wrong_engine(self):
        error = self.error_for(
            FACTORY_WORKER_ORDER="claude-delivery",
            FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH=(
                "python3 factory/runtime/bridge.py --engine codex "
                "--story {story} --project {project}"))
        self.assertIn("expected 'claude'", error)

    def test_accepts_both_real_bridge_declarations(self):
        error = self.error_for(
            FACTORY_WORKER_ORDER="claude-delivery,codex-delivery",
            FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH=(
                "python3 factory/runtime/bridge.py --engine claude "
                "--story {story} --project {project}"),
            FACTORY_WORKER_CODEX_DELIVERY_LAUNCH=(
                "python3 factory/runtime/bridge.py --engine codex "
                "--story {story} --project {project}"))
        self.assertEqual("", error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
