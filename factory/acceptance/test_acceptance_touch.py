import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "runtime"))
import continuation as ct


def decision(result="fail", failed="AT-01", follow="none", created="2026-08-22T15:00:00Z"):
    statuses = {"AT-01": "pass", "AT-02": "pass"}
    if result == "fail":
        statuses[failed] = "fail"
    rows = "\n".join(f"- {key} — {value}" for key, value in statuses.items())
    return {"body": f"## Acceptance\n\nresult: {result}\nactor: @maheshhbhat\n\n"
                    f"{rows}\n\nfollow-up: {follow}\n",
            "authorAssociation": "OWNER", "created_at": created,
            "user": {"login": "maheshhbhat"}}


class AcceptanceTouchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp.name) / "touchlog.jsonl"
        self.environment = mock.patch.dict(os.environ,
                                           {"FACTORY_TOUCHLOG_FILE": str(self.path)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def records(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_pass_fail_distinct_correction_and_replay_are_exactly_once(self):
        first = decision(follow="#289")
        repeat = {**first, "created_at": "2026-08-22T15:01:00Z"}
        correction = decision(failed="AT-02", follow="none",
                              created="2026-08-22T16:00:00Z")
        passed = decision(result="pass", created="2026-08-22T17:00:00Z")
        for item in (first, repeat, correction, passed, passed):
            ct.ensure_acceptance_touch(212, item)
        self.assertEqual(len(self.records()), 3)
        self.assertEqual({x["bell_type"] for x in self.records()}, {"acceptance"})
        self.assertEqual({x["classification"] for x in self.records()}, {"decision"})

    def test_comment_id_prose_and_timestamp_do_not_change_identity(self):
        base = decision(follow="#289")
        changed = {**base, "id": 999, "created_at": "2026-08-23T00:00:00Z",
                   "body": base["body"] + "\nNarrative mentions #999.\n"}
        self.assertEqual(ct.acceptance_identity(base)[0], ct.acceptance_identity(changed)[0])

    def test_changed_follow_up_is_a_new_decision(self):
        one, two = decision(follow="#289"), decision(follow="#292")
        self.assertNotEqual(ct.acceptance_identity(one)[0], ct.acceptance_identity(two)[0])

    def test_corrupt_and_duplicate_evidence_fail_closed(self):
        item = decision()
        self.path.write_text("not-json\n")
        with self.assertRaisesRegex(ct.TouchEvidenceError, "unreadable"):
            ct.ensure_acceptance_touch(212, item)
        fingerprint = ct.acceptance_identity(item)[0]
        record = {"project": "#212", "bell_type": "acceptance",
                  "note": f"acceptance-fingerprint:{fingerprint}"}
        self.path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n")
        with self.assertRaisesRegex(ct.TouchEvidenceError, "duplicate"):
            ct.ensure_acceptance_touch(212, item)

    def test_append_and_readback_failure_are_observable(self):
        failed = mock.Mock(returncode=1, stderr="disk full", stdout="")
        with self.assertRaisesRegex(ct.TouchEvidenceError, "append failed"):
            ct.ensure_acceptance_touch(212, decision(), runner=lambda *a, **k: failed)
        self.assertFalse(self.path.exists())

        success_without_write = mock.Mock(returncode=0, stderr="", stdout="")
        with self.assertRaisesRegex(ct.TouchEvidenceError, "read-back failed"):
            ct.ensure_acceptance_touch(
                212, decision(), runner=lambda *a, **k: success_without_write)

    def test_timestamp_actor_seconds_and_project_are_canonical(self):
        item = decision(result="pass")
        ct.ensure_acceptance_touch(294, item)
        record = self.records()[0]
        self.assertEqual(record["timestamp"], item["created_at"])
        self.assertEqual(record["project"], "#294")
        self.assertIsNone(record["story"])
        self.assertEqual(record["actor"], "@maheshhbhat")
        self.assertEqual(record["seconds_spent"], 0)
        self.assertIn("time unavailable", record["note"])

    def test_freshness_check_precedes_evidence_and_evidence_precedes_state_write(self):
        source = pathlib.Path(ct.__file__).read_text()
        apply_body = source.split("def apply_outcome", 1)[1].split("def run", 1)[0]
        fresh = apply_body.index("fresh = json.loads")
        evidence = apply_body.index("ensure_acceptance_touch")
        write = apply_body.index('method="PATCH"')
        self.assertLess(fresh, evidence)
        self.assertLess(evidence, write)

    def test_committed_phase4_and_repair_bell_backfills_are_exact(self):
        path = HERE.parent / "touchlog" / "touchlog.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        phase4 = [row for row in rows
                  if row.get("project") == "#212" and row.get("bell_type") == "acceptance"]
        self.assertEqual(len(phase4), 3)
        self.assertEqual({row["classification"] for row in phase4}, {"decision"})
        self.assertEqual({row["timestamp"] for row in phase4}, {
            "2026-08-22T15:51:12Z", "2026-08-22T17:31:01Z", "2026-08-22T17:42:13Z"})
        self.assertEqual(len({row["note"].split(";", 1)[0] for row in phase4}), 3)
        for reference in ("#289", "#292", "16 of 16"):
            self.assertEqual(sum(reference in row["note"] for row in phase4), 1)

        plan = [row for row in rows if row.get("project") == "#294"
                and row.get("bell_type") == "plan-approval"]
        hazard = [row for row in rows if row.get("project") == "#294"
                  and row.get("story") == "#295" and row.get("bell_type") == "hazard-ack"]
        self.assertEqual(len(plan), 1)
        self.assertEqual(len(hazard), 1)


if __name__ == "__main__":
    unittest.main()
