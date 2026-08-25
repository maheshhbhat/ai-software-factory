import json
from pathlib import Path
import tempfile
import unittest

from factory.acceptance import rung2_report as report


def fixture():
    evidence = {
        "repository": "owner/product", "commitment": 17, "project": 18,
        "project_ref": "owner/product#18", "product_outcome": "accepted",
        "stories": [
            {"number": 20, "merged": True, "poisoned": True, "human_recovery": True},
            {"number": 21, "merged": True, "poisoned": True, "human_recovery": True},
            {"number": 22, "merged": True, "poisoned": False, "human_recovery": False},
            {"number": 23, "merged": True, "poisoned": False, "human_recovery": False},
        ],
        "decisions": [
            {"bell_type": "plan-approval", "result": "changes-requested",
             "timestamp": "2026-01-01T00:00:00Z"},
            {"bell_type": "plan-approval", "result": "changes-requested",
             "timestamp": "2026-01-01T00:01:00Z"},
            {"bell_type": "plan-approval", "result": "approved",
             "timestamp": "2026-01-01T00:02:00Z"},
            {"bell_type": "poison-rescue", "result": "approved",
             "timestamp": "2026-01-01T00:03:00Z"},
            {"bell_type": "poison-rescue", "result": "approved",
             "timestamp": "2026-01-01T00:04:00Z"},
            {"bell_type": "acceptance", "result": "pass",
             "timestamp": "2026-01-01T01:02:00Z"},
        ],
        "operator_actions": [
            {"action": "claim-recovery", "classification": "operation"}],
        "quality_observations": [],
        "acceptance": {"criteria": [{"criterion": "works", "result": "pass"}]},
    }
    attempts = {20: 4, 21: 5, 22: 1, 23: 1}
    process = []
    for story, count in attempts.items():
        for index in range(count):
            row = {"event": "story.claimed", "story": story,
                   "event_id": f"{story}-{index}"}
            process.extend([row, dict(row)])
    telemetry = [
        {"metric": "engine.usage", "story": 20, "engine": "claude",
         "usage_reported": True, "usage": {"total_cost_usd": 4.0}},
        {"metric": "engine.usage", "story": 21, "engine": "codex",
         "usage_reported": False, "usage": "engine reported no usage"},
        {"metric": "engine.usage", "story": 22, "engine": "claude",
         "usage_reported": True, "usage": {"total_cost_usd": 2.0}},
    ]
    touches = [
        {"project": "owner/product#18", "bell_type": row["bell_type"],
         "classification": "rescue" if row["bell_type"] == "poison-rescue" else "decision"}
        for row in evidence["decisions"]
    ]
    return evidence, process, telemetry, touches


class Rung2ReportTests(unittest.TestCase):
    def test_reconstructs_failed_rung_without_hiding_recovery_or_cost_gap(self):
        result = report.build(*fixture())
        self.assertTrue(result["measurement_integrity"]["passed"])
        self.assertEqual("FAIL", result["rung_verdict"])
        self.assertEqual(.5, result["kpis"]["autonomy"]["rate"])
        self.assertEqual(11, result["kpis"]["worker_attempts_retry_rate"]["attempts"])
        self.assertEqual(.5, result["kpis"]["poison_rate"]["rate"])
        self.assertEqual(0, result["kpis"]["human_touches"]["relay"])
        self.assertEqual("lower-bound", result["kpis"]["engine_usage_cost"]["cost_status"])
        self.assertEqual("unavailable",
                         result["kpis"]["engine_usage_cost"]["cost_per_accepted_story_usd"])
        self.assertEqual(3600, result["kpis"]["cycle_time"]["seconds"])

    def test_missing_touch_receipt_fails_integrity(self):
        values = fixture(); values[3].pop()
        result = report.build(*values)
        self.assertFalse(result["measurement_integrity"]["passed"])
        self.assertIn("acceptance decisions (1) and touch receipts (0) differ",
                      result["measurement_integrity"]["findings"])

    def test_thresholds_can_pass_but_integrity_is_independent(self):
        values = fixture()
        for story in values[0]["stories"]: story["human_recovery"] = False
        values[0]["stories"][0]["poisoned"] = False
        values[0]["stories"][1]["poisoned"] = False
        result = report.build(*values)
        self.assertEqual("PASS", result["rung_verdict"])
        self.assertTrue(result["measurement_integrity"]["passed"])

    def test_cli_accepts_multiple_run_fragments_and_is_deterministic(self):
        evidence, process, telemetry, touches = fixture()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence_path = root / "evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            process_paths = []
            for index, part in enumerate((process[:10], process[10:])):
                path = root / f"process-{index}.jsonl"
                path.write_text("".join(json.dumps(row) + "\n" for row in part))
                process_paths.append(path)
            telemetry_paths = []
            for index, part in enumerate((telemetry[:1], telemetry[1:])):
                path = root / f"telemetry-{index}.jsonl"
                path.write_text("".join(json.dumps(row) + "\n" for row in part))
                telemetry_paths.append(path)
            touch_path = root / "touch.jsonl"
            touch_path.write_text("".join(json.dumps(row) + "\n" for row in touches))
            base = ["--evidence", str(evidence_path), "--touchlog", str(touch_path)]
            for path in process_paths: base += ["--process", str(path)]
            for path in telemetry_paths: base += ["--telemetry", str(path)]
            first, second = root / "first", root / "second"
            self.assertEqual(0, report.main(base + ["--output", str(first)]))
            self.assertEqual(0, report.main(base + ["--output", str(second)]))
            self.assertEqual((first / "report.json").read_bytes(),
                             (second / "report.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
