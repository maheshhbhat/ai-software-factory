import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import observability as obs
import status as factory_status


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {
            "FACTORY_RUN_DIR": str(self.directory),
            "FACTORY_LOG_LEVEL": "CRITICAL",
        })
        self.env.start(); self.addCleanup(self.env.stop)

    def test_three_record_types_are_physically_separate(self):
        obs.process_event("story.transition", story=7, from_state="ready", to_state="claimed")
        obs.operational_log("INFO", "checking eligibility", story=7)
        obs.telemetry("engine.tokens", value=10, unit="tokens", story=7)
        self.assertEqual("story.transition", obs.read_records("process")[0]["event"])
        self.assertEqual("INFO", obs.read_records("operation")[0]["level"])
        self.assertEqual("engine.tokens", obs.read_records("telemetry")[0]["metric"])

    def test_schemas_reject_cross_stream_fields(self):
        with self.assertRaisesRegex(ValueError, "diagnostic"):
            obs.process_event("story.transition", level="INFO")
        with self.assertRaisesRegex(ValueError, "process"):
            obs.operational_log("INFO", "message", event="story.transition")
        with self.assertRaisesRegex(ValueError, "diagnostic"):
            obs.telemetry("tokens", stack_trace="forbidden")

    def test_trace_is_stable_per_claim_and_changes_for_new_claim(self):
        first = obs.trace_id("owner/repo", 7, "2026-01-01T00:00:00Z")
        self.assertEqual(first, obs.trace_id("owner/repo", 7, "2026-01-01T00:00:00Z"))
        self.assertNotEqual(first, obs.trace_id("owner/repo", 7, "2026-01-02T00:00:00Z"))
        self.assertEqual(32, len(first))

    def test_exception_keeps_deep_stack_and_source_line(self):
        def inner():
            raise RuntimeError("deep failure")
        def outer():
            inner()
        try:
            outer()
        except RuntimeError as error:
            obs.operational_log("ERROR", "operation failed", exc=error,
                                component="test", stage="deep")
        record = obs.read_records("operation")[0]
        self.assertEqual("RuntimeError", record["exception_type"])
        self.assertIn("inner", record["stack_trace"])
        self.assertIn("outer", record["stack_trace"])
        self.assertIn("raise RuntimeError", record["stack_trace"])

    def test_activity_emits_start_progress_and_completion(self):
        with obs.Activity("worker", "deliver", "starting", repo="owner/repo",
                          story=7, project=8, trace_id="a" * 32) as activity:
            activity.progress("testing")
        metrics = [row["metric"] for row in obs.read_records("telemetry")]
        self.assertEqual(["activity.started", "activity.progress", "activity.completed"], metrics)
        for row in obs.read_records("telemetry"):
            self.assertEqual(7, row["story"])
            self.assertEqual("a" * 32, row["trace_id"])

    def test_activity_failure_preserves_exception(self):
        with self.assertRaisesRegex(ValueError, "bad"):
            with obs.Activity("reviewer", "review", "engine", story=9):
                raise ValueError("bad")
        self.assertEqual("FAILED", obs.read_records("telemetry")[-1]["status"])
        failure = obs.read_records("operation")[-1]
        self.assertEqual("ValueError", failure["exception_type"])
        self.assertIn("raise ValueError", failure["stack_trace"])

    def test_supervisor_emits_heartbeats_while_component_is_busy(self):
        with mock.patch.dict(os.environ, {"FACTORY_HEARTBEATS":"1"}), \
             mock.patch.object(obs, "HEARTBEAT_SECONDS", 0.01):
            with obs.Activity("worker", "deliver", "engine", story=9):
                time.sleep(0.035)
        metrics = [row["metric"] for row in obs.read_records("telemetry")]
        self.assertGreaterEqual(metrics.count("activity.heartbeat"), 2)

    def test_logging_failure_does_not_replace_primary_exception(self):
        with mock.patch.object(obs, "_append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "primary"):
                with obs.Activity("worker", "deliver", "engine", story=9):
                    raise RuntimeError("primary")

    def test_status_distinguishes_running_no_progress_stuck_and_terminal(self):
        current = datetime(2026, 1, 1, tzinfo=timezone.utc)
        def row(metric, seconds, status="RUNNING", span="1"):
            return {"record_type":"telemetry","metric":metric,"component":"worker",
                    "span_id":span,"timestamp":(current-timedelta(seconds=seconds)).isoformat(),
                    "status":status,"stage":"engine","elapsed_seconds":seconds}
        rows = obs.activity_status([
            row("activity.heartbeat", 4, "RUNNING", "running"),
            row("activity.heartbeat", 4, "ALIVE_NO_PROGRESS", "quiet"),
            row("activity.heartbeat", 16, "RUNNING", "stuck"),
            row("activity.completed", 100, "COMPLETED", "done"),
            row("activity.failed", 100, "FAILED", "failed"),
        ], current)
        self.assertEqual({"running":"RUNNING","quiet":"ALIVE_NO_PROGRESS",
                          "stuck":"STUCK","done":"COMPLETED","failed":"FAILED"},
                         {item["span_id"]:item["status"] for item in rows})

    def test_status_table_names_story_work(self):
        text = factory_status.render([{"component":"worker","story":7,"stage":"testing",
                                       "elapsed_seconds":2,"status":"RUNNING"}])
        self.assertIn("Story #7", text); self.assertIn("RUNNING", text)

    def test_status_keeps_only_the_latest_activity_per_component(self):
        rows = [{"component":"worker","span_id":"old","timestamp":"2026-01-01T00:00:00Z"},
                {"component":"worker","span_id":"new","timestamp":"2026-01-01T00:00:01Z"},
                {"component":"reviewer","span_id":"review","timestamp":"2026-01-01T00:00:00Z"}]
        current = factory_status.current_components(rows)
        self.assertEqual({"new", "review"}, {row["span_id"] for row in current})

    def test_secrets_are_redacted_from_stack_and_message(self):
        secret = "ghp_" + "a" * 36
        with mock.patch.dict(os.environ, {"GH_TOKEN": secret}):
            obs.operational_log("ERROR", f"token {secret}", stack_trace=f"trace {secret}")
        serialised = json.dumps(obs.read_records("operation"))
        self.assertNotIn(secret, serialised); self.assertIn("[redacted]", serialised)


if __name__ == "__main__":
    unittest.main()
