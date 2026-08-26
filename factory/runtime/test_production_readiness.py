from __future__ import annotations

import unittest

import production_readiness as readiness

SHA = "a" * 40
STAMP = "2026-08-26T12:00:00Z"
ENVELOPE = [
    {"id": "OE-SCALE", "category": "representative-input",
     "requirement": "Support a $1M portfolio", "failure_condition": "blocks"},
    {"id": "OE-LIVE", "category": "external-provider",
     "requirement": "Read the live provider", "failure_condition": "stale"},
]


def result(identifier, outcome="pass"):
    return {"id": identifier, "result": outcome,
            "evidence": f"runs/readiness/{identifier}.json",
            "detail": "failed check" if outcome == "fail" else "within bound"}


def artifact(results=None, observations=None):
    return readiness.build(
        repo="Owner/Repo", project=47, revision=SHA, envelope=ENVELOPE,
        results=results or [result("OE-SCALE"), result("OE-LIVE")],
        observations=(observations if observations is not None else
                      [{"id": "OE-LIVE", "started_at": STAMP,
                        "completed_at": STAMP, "bounded_by_seconds": 30,
                        "detail": "live read returned current data"}]),
        started_at=STAMP, completed_at=STAMP)


class ProductionReadinessTests(unittest.TestCase):
    def test_exact_ready_artifact_round_trips_and_blocks_only_after_promotion(self):
        value = artifact()
        parsed = readiness.latest([{"body": readiness.render(value)}], repo="owner/repo",
                                  project=47, revision=SHA, envelope=ENVELOPE)
        self.assertEqual("ready", parsed["overall"])
        self.assertTrue(readiness.permits_completion(None, "warning"))
        self.assertTrue(readiness.permits_completion(parsed, "blocking"))

    def test_failure_produces_not_ready_and_blocks(self):
        value = artifact([result("OE-SCALE", "fail"), result("OE-LIVE")])
        self.assertEqual("not-ready", value["overall"])
        self.assertFalse(readiness.permits_completion(value, "blocking"))

    def test_toy_only_missing_id_stale_revision_and_tampering_are_rejected(self):
        cases = []
        with self.assertRaises(readiness.ReadinessError):
            artifact([result("OE-SCALE")])
        value = artifact()
        self.assertIsNone(readiness.latest([{"body": readiness.render(value)}],
                                          repo="owner/repo", project=47,
                                          revision="b" * 40, envelope=ENVELOPE))
        value["overall"] = "not-ready"
        self.assertIsNone(readiness.latest([{"body": readiness.render(value)}],
                                          repo="owner/repo", project=47,
                                          revision=SHA, envelope=ENVELOPE))

    def test_newer_malformed_artifact_never_falls_back_to_an_old_pass(self):
        valid = artifact()
        comments = [{"body": readiness.render(valid)},
                    {"body": readiness.MARKER + "\n\nmalformed"}]
        self.assertIsNone(readiness.latest(
            comments, repo="owner/repo", project=47, revision=SHA,
            envelope=ENVELOPE))

    def test_live_provider_requires_bounded_observation(self):
        with self.assertRaisesRegex(readiness.ReadinessError, "observation"):
            artifact(observations=[])
        with self.assertRaisesRegex(readiness.ReadinessError, "exceeded"):
            artifact(observations=[{
                "id": "OE-LIVE", "started_at": "2026-08-26T12:00:00Z",
                "completed_at": "2026-08-26T12:01:01Z",
                "bounded_by_seconds": 60, "detail": "timed out"}])

    def test_invalid_promotion_mode_fails_closed(self):
        with self.assertRaises(readiness.ReadinessError):
            readiness.mode({"FACTORY_PRODUCTION_READINESS_MODE": "maybe"})


if __name__ == "__main__":
    unittest.main()
