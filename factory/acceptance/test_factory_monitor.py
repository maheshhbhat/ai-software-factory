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
            }) + "\n" + json.dumps({
                "timestamp": "2999-01-01T00:00:01Z", "message": "engine output",
                "component": "delivery-worker", "operation": "engine-stream",
                "story": 20, "engine_output_tail": json.dumps({
                    "type": "system", "subtype": "thinking_tokens",
                    "estimated_tokens": 250}),
            }) + "\n")
            rendered, verdict = monitor.snapshot(run)
        self.assertIn("production observability", rendered)
        self.assertIn("ACTIVE: Story #20", rendered)
        self.assertIn("progress: Story #20", rendered)
        self.assertIn("thinking; estimated tokens=250", rendered)
        self.assertIsNone(verdict)

    def test_unstructured_progress_is_bounded(self):
        self.assertEqual(200, len(monitor.progress_summary("x" * 300)))


if __name__ == "__main__":
    unittest.main()
