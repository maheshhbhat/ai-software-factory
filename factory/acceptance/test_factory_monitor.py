#!/usr/bin/env python3
"""Deterministic checks for the shared production-run monitor."""

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PATH = ROOT / ".claude/skills/factory-monitor/scripts/monitor.py"
SPEC = importlib.util.spec_from_file_location("factory_monitor", PATH)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class ProductionRunTests(unittest.TestCase):
    def test_observability_only_run_is_visible_without_harness_state(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            observed = run / "observability"
            observed.mkdir()
            (observed / "operations.jsonl").write_text(json.dumps({
                "timestamp": "2999-01-01T00:00:00Z", "span_id": "span",
                "message": "activity started", "component": "delivery-worker",
                "operation": "engine", "stage": "executing", "story": 20,
            }) + "\n")
            rendered, verdict = monitor.snapshot(run)
        self.assertIn("production observability", rendered)
        self.assertIn("ACTIVE: Story #20", rendered)
        self.assertIsNone(verdict)


if __name__ == "__main__":
    unittest.main()
