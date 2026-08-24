import json
from pathlib import Path
import tempfile
import unittest

from factory.acceptance import rung1_report as report


def fixture():
    evidence = {
        "passed": True, "project": 500, "run": "r1", "entrypoint": "poll.sh",
        "stories": [
            {"number": 501, "merged": True, "walk": ["story:ready", "story:merged"]},
            {"number": 502, "merged": True,
             "walk": ["story:ready", "story:blocked:poison", "story:merged"]}],
        "decisions": [
            {"bell_type": "plan-approval", "timestamp": "2026-01-01T00:00:00Z"},
            {"bell_type": "acceptance", "timestamp": "2026-01-01T00:02:00Z"}],
        "acceptance": {"criteria": [{"criterion": "health works", "result": "pass"}]},
        "quality_observations": [], "human_code_interventions": [],
    }
    process = [
        {"event": "story.claimed", "story": 501, "event_id": "a"},
        {"event": "story.claimed", "story": 501, "event_id": "a"},
        {"event": "story.claimed", "story": 501, "event_id": "b"},
        {"event": "story.claimed", "story": 502, "event_id": "c"}]
    telemetry = [
        {"metric": "engine.usage", "story": 501, "engine": "claude",
         "usage_reported": True, "usage": {"input_tokens": 10, "total_cost_usd": .2}},
        {"metric": "engine.usage", "story": 502, "engine": "codex",
         "usage_reported": True, "usage": {"input_tokens": 20}}]
    touches = [
        {"project": "#500", "bell_type": "plan-approval", "classification": "decision"},
        {"project": "#500", "bell_type": "acceptance", "classification": "decision"},
        {"project": "#500", "bell_type": "sampling", "classification": "relay"},
        {"project": "#999", "bell_type": "acceptance", "classification": "decision"}]
    return evidence, process, telemetry, touches


class Rung1ReportTests(unittest.TestCase):
    def test_all_eight_kpis_are_derived_and_partial_cost_stays_unavailable(self):
        result = report.build(*fixture())
        kpi = result["kpis"]
        self.assertEqual(8, len(kpi))
        self.assertTrue(result["measurement_integrity"]["passed"])
        self.assertEqual(3, kpi["human_touches"]["count"])
        self.assertEqual(1, kpi["human_touches"]["relay"])
        self.assertEqual(3, kpi["worker_attempts_retry_rate"]["attempts"])
        self.assertEqual(.5, kpi["worker_attempts_retry_rate"]["retry_rate"])
        self.assertEqual(.5, kpi["poison_rate"]["rate"])
        self.assertEqual(0, kpi["escaped_defects"]["count"])
        self.assertEqual(0, kpi["acceptance_catches"]["count"])
        self.assertEqual("partial", kpi["engine_usage_cost"]["cost_status"])
        self.assertEqual("unavailable", kpi["engine_usage_cost"]["cost_per_accepted_story_usd"])
        self.assertEqual(120, kpi["cycle_time"]["seconds"])

    def test_missing_receipt_fails_integrity_but_unavailable_metric_does_not(self):
        evidence, process, telemetry, touches = fixture()
        del evidence["human_code_interventions"]
        touches = touches[:1]
        result = report.build(evidence, process, telemetry, touches)
        self.assertFalse(result["measurement_integrity"]["passed"])
        self.assertEqual("unavailable", result["kpis"]["autonomy"]["status"])
        self.assertIn("acceptance touch receipt must appear exactly once",
                      result["measurement_integrity"]["findings"])

    def test_absence_never_becomes_zero_quality_evidence(self):
        evidence, process, telemetry, touches = fixture()
        del evidence["quality_observations"]
        del evidence["acceptance"]
        result = report.build(evidence, process, telemetry, touches)
        self.assertEqual("unavailable", result["kpis"]["escaped_defects"]["status"])
        self.assertEqual("unavailable", result["kpis"]["acceptance_catches"]["status"])
        self.assertTrue(result["measurement_integrity"]["passed"])

    def test_pre_acceptance_report_is_complete_without_forging_the_decision(self):
        evidence, process, telemetry, touches = fixture()
        evidence["report_phase"] = "pre-acceptance"
        evidence["decisions"] = evidence["decisions"][:1]
        del evidence["acceptance"]
        touches = [row for row in touches if row.get("bell_type") != "acceptance"]
        result = report.build(evidence, process, telemetry, touches)
        self.assertEqual(8, len(result["kpis"]))
        self.assertTrue(result["measurement_integrity"]["passed"])
        self.assertEqual("unavailable", result["kpis"]["acceptance_catches"]["status"])
        self.assertEqual("outcome acceptance is pending",
                         result["kpis"]["acceptance_catches"]["reason"])
        self.assertEqual("unavailable", result["kpis"]["cycle_time"]["status"])

    def test_operator_actions_are_counted_without_becoming_relay(self):
        values = fixture()
        values[0]["operator_actions"] = [{
            "action": "fixture-launch", "actor": "@owner",
            "classification": "operation", "timestamp": "2026-01-01T00:00:10Z"}]
        result = report.build(*values)
        self.assertEqual(4, result["kpis"]["human_touches"]["count"])
        self.assertEqual(1, result["kpis"]["human_touches"]["relay"])
        self.assertEqual(1, result["kpis"]["human_touches"]["by_classification"]["operation"])

    def test_malformed_operator_actions_fail_integrity(self):
        values = fixture()
        values[0]["operator_actions"] = [{"actor": "@owner"}]
        result = report.build(*values)
        self.assertFalse(result["measurement_integrity"]["passed"])
        self.assertIn("operator_actions must be a list of classified named actions",
                      result["measurement_integrity"]["findings"])

    def test_unknown_report_phase_fails_closed(self):
        values = fixture()
        values[0]["report_phase"] = "almost-final"
        with self.assertRaisesRegex(ValueError, "report_phase"):
            report.build(*values)

    def test_bad_acceptance_evidence_fails_integrity(self):
        values = fixture()
        values[0]["acceptance"]["criteria"][0]["result"] = "maybe"
        result = report.build(*values)
        self.assertFalse(result["measurement_integrity"]["passed"])

    def test_failed_black_box_run_reports_eight_kpis_without_inventing_success(self):
        values = fixture(); values[0]["passed"] = False
        values[0]["reason"] = "worker could not create its worktree"
        result = report.build(*values)
        self.assertFalse(result["black_box_uat"]["passed"])
        self.assertEqual(8, len(result["kpis"]))
        self.assertEqual("unavailable", result["kpis"]["autonomy"]["status"])
        self.assertEqual("unavailable", result["kpis"]["escaped_defects"]["status"])
        self.assertEqual("unavailable", result["kpis"]["acceptance_catches"]["status"])
        self.assertEqual("unavailable",
                         result["kpis"]["engine_usage_cost"]["cost_per_accepted_story_usd"])

    def test_failed_run_without_a_created_story_still_renders_unavailable_rates(self):
        evidence, _process, _telemetry, touches = fixture()
        evidence.update({"passed": False, "stories": [], "stories_created": []})
        result = report.build(evidence, [], [], touches)
        self.assertEqual("unavailable",
                         result["kpis"]["worker_attempts_retry_rate"]["retry_rate"])
        self.assertIn("unavailable", report.render(result))

    def test_same_frozen_inputs_write_byte_identical_reports(self):
        evidence, process, telemetry, touches = fixture()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = {}
            for name, value in (("evidence", evidence), ("process", process),
                                ("telemetry", telemetry), ("touches", touches)):
                path = root / f"{name}.jsonl"
                if name == "evidence": path.write_text(json.dumps(value))
                else: path.write_text("".join(json.dumps(row) + "\n" for row in value))
                paths[name] = path
            args = ["--evidence", str(paths["evidence"]), "--process", str(paths["process"]),
                    "--telemetry", str(paths["telemetry"]), "--touchlog", str(paths["touches"])]
            first, second = root / "one", root / "two"
            self.assertEqual(0, report.main(args + ["--output", str(first)]))
            self.assertEqual(0, report.main(args + ["--output", str(second)]))
            self.assertEqual((first / "report.json").read_bytes(),
                             (second / "report.json").read_bytes())
            self.assertEqual((first / "report.md").read_bytes(),
                             (second / "report.md").read_bytes())


if __name__ == "__main__": unittest.main()
