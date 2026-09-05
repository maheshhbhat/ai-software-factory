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
            trace = f"trace-{story}-{index}"
            row = {"event": "story.claimed", "story": story,
                   "repo": evidence["repository"],
                   "event_id": f"{story}-{index}", "trace_id": trace}
            process.extend([row, dict(row)])
            process.extend([
                {"event": "worker.launch.start", "story": story,
                 "repo": evidence["repository"],
                 "event_id": f"launch-start-{story}-{index}",
                 "trace_id": trace, "span_id": f"span-{story}-{index}",
                 "worker": "factory-worker"},
                {"event": "worker.launch.end", "story": story,
                 "repo": evidence["repository"],
                 "event_id": f"launch-end-{story}-{index}",
                 "trace_id": trace, "span_id": f"span-{story}-{index}",
                 "worker": "factory-worker", "result": "LAUNCHED", "exit": 0}])
            process.append({"event": "worker.outcome", "story": story,
                            "repo": evidence["repository"],
                            "event_id": f"outcome-{story}-{index}",
                            "trace_id": trace, "worker": "factory-worker",
                            "result": "LAUNCHED", "exit": 0})
    for story in evidence["stories"]:
        pr, head = 100 + story["number"], f"{story['number']:040x}"
        story["pull_request"] = pr
        story["head"] = head
        for outcome in process:
            if (outcome.get("event") == "worker.outcome" and
                    outcome.get("story") == story["number"]):
                outcome["detail"] = json.dumps(
                    {"pull_request": pr, "head": head})
                process.append({"event": "delivery.pull-request.written",
                                "story": story["number"],
                                "repo": evidence["repository"],
                                "trace_id": outcome["trace_id"],
                                "pull_request": pr, "head": head,
                                "event_id": f"delivery-{outcome['trace_id']}"})
        process.append({"event": "review.outcome.published",
                        "story": story["number"], "repo": evidence["repository"],
                        "pull_request": pr,
                        "head": head, "verdict": "approval",
                        "event_id": f"review-{pr}-{head}"})
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

    def test_missing_terminal_worker_outcome_is_inconclusive(self):
        values = list(fixture())
        values[1] = [row for row in values[1]
                     if not (row.get("event") == "worker.outcome" and
                             row.get("trace_id") == "trace-20-0")]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertEqual("unavailable", result["kpis"]["autonomy"]["status"])
        self.assertEqual("unavailable",
                         result["kpis"]["worker_attempts_retry_rate"]["status"])

    def test_launched_outcome_without_launch_evidence_is_inconclusive(self):
        values = list(fixture())
        trace = "trace-20-0"
        values[1] = [row for row in values[1]
                     if not (row.get("trace_id") == trace and
                             row.get("event") in
                             ("worker.launch.start", "worker.launch.end"))]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        values[0]["evidence_unavailable"] = [{
            "kind": "attempt-launches", "story": 20, "identity": trace,
            "reason": "legacy launch evidence unavailable"}]
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])
        ledger = next(row for row in result["attempt_ledger"]
                      if row["trace_id"] == trace)
        self.assertEqual("legacy launch evidence unavailable",
                         ledger["launch_evidence_unavailable"]["reason"])

    def test_missing_attempt_ledger_is_inconclusive(self):
        values = list(fixture())
        values[1] = [row for row in values[1] if row.get("story") != 20]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertIn("Story 20 has no durable attempt claim evidence",
                      result["measurement_integrity"]["findings"])

    def test_worker_attempt_trace_without_claim_is_inconclusive(self):
        values = list(fixture())
        values[1].extend([
            {"event": "worker.launch.start", "story": 20,
             "repo": values[0]["repository"], "trace_id": "orphan-trace",
             "span_id": "orphan-span", "worker": "factory-worker",
             "event_id": "orphan-start"},
            {"event": "worker.launch.end", "story": 20,
             "repo": values[0]["repository"], "trace_id": "orphan-trace",
             "span_id": "orphan-span", "worker": "factory-worker",
             "event_id": "orphan-end", "result": "LAUNCHED", "exit": 0},
            {"event": "worker.outcome", "story": 20,
             "repo": values[0]["repository"], "trace_id": "orphan-trace",
             "worker": "factory-worker", "event_id": "orphan-outcome",
             "result": "LAUNCHED", "exit": 0}])
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("has no matching durable claim" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))

    def test_failover_trace_without_claim_is_inconclusive(self):
        values = list(fixture())
        values[1].append({
            "event": "worker.failover", "story": 20,
            "repo": values[0]["repository"], "trace_id": "orphan-failover",
            "event_id": "orphan-failover-event", "worker": "factory-worker",
            "decision": "EXHAUSTED", "result": "FAILED"})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("orphan-failover" in finding and
                            "no matching durable claim" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))

    def test_delivery_trace_without_claim_is_inconclusive(self):
        values = list(fixture())
        values[1].extend([
            {"event": "delivery.pull-request.written", "story": 20,
             "repo": values[0]["repository"], "trace_id": "orphan-delivery",
             "pull_request": 999, "head": "f" * 40,
             "event_id": "orphan-delivery-event"},
            {"event": "review.outcome.published", "story": 20,
             "repo": values[0]["repository"], "pull_request": 999,
             "head": "f" * 40, "verdict": "approval",
             "event_id": "orphan-delivery-review"}])
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("orphan-delivery" in finding and
                            "no matching durable claim" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))

    def test_explicit_terminal_evidence_unavailable_completes_ledger(self):
        values = list(fixture())
        values[1] = [row for row in values[1]
                     if not (row.get("event") == "worker.outcome" and
                             row.get("trace_id") == "trace-20-0")]
        values[0]["evidence_unavailable"] = [{
            "kind": "attempt-terminal-outcome", "story": 20,
            "identity": "trace-20-0", "reason": "legacy log unavailable"}]
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_overlapping_fragments_deduplicate_terminal_outcome_by_trace_and_event(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        values[1].append(dict(outcome))
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertEqual(11, len(result["attempt_ledger"]))

    def test_conflicting_terminal_copies_fail_deterministically(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        conflict = dict(outcome)
        conflict.update({"result": "FAILED", "exit": 1,
                         "recovery_ref": "durable-recovery"})
        forward = list(values[1]) + [conflict]
        reverse = list(reversed(forward))
        first = report.build(values[0], forward, values[2], values[3])
        second = report.build(values[0], reverse, values[2], values[3])
        self.assertEqual(first, second)
        self.assertEqual("INCONCLUSIVE", first["rung_verdict"])
        self.assertTrue(any("conflicting durable copies" in finding
                            for finding in
                            first["measurement_integrity"]["findings"]))

    def test_terminal_outcome_requires_recognized_consistent_result(self):
        for mutation in ({"result": ""},
                         {"result": []},
                         {"result": "UNKNOWN"},
                         {"result": "LAUNCHED", "exit": 1},
                         {"result": "LAUNCHED", "exit": False},
                         {"result": "FAILED", "exit": "1"},
                         {"result": "AMBIGUOUS", "exit": 1},
                         {"result": "TERMINAL_FAILURE", "exit": None}):
            with self.subTest(mutation=mutation):
                values = list(fixture())
                outcome = next(row for row in values[1]
                               if row.get("event") == "worker.outcome")
                outcome.update(mutation)
                result = report.build(*values)
                self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
                self.assertTrue(any(
                    "recognized result with a consistent worker and exit status"
                    in finding for finding in
                    result["measurement_integrity"]["findings"]))

    def test_terminal_outcome_requires_string_event_id(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome["event_id"] = 123
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("nonempty string event ID" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))

    def test_failed_terminal_outcome_requires_nonzero_integer_exit(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome.update({"result": "FAILED", "exit": None,
                        "diagnostic_ref": "durable failure"})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("recognized result with a consistent worker" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))

    def test_attempt_trace_requires_nonempty_string_identity(self):
        for invalid in (123, "   "):
            with self.subTest(invalid=invalid):
                values = list(fixture())
                trace = "trace-20-0"
                for row in values[1]:
                    if row.get("trace_id") == trace:
                        row["trace_id"] = invalid
                result = report.build(*values)
                self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
                self.assertTrue(any("nonempty string attempt trace ID" in finding
                                    for finding in result[
                                        "measurement_integrity"]["findings"]))

    def test_no_eligible_worker_uses_terminal_detail_as_diagnostic(self):
        values = list(fixture())
        trace = "trace-20-0"
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome" and
                       row.get("trace_id") == trace)
        outcome.update({"result": "NO_ELIGIBLE_WORKER", "worker": None,
                        "exit": None,
                        "detail": "no configured worker is capable and healthy"})
        values[1] = [row for row in values[1]
                     if not (row.get("trace_id") == trace and
                             row.get("event") in
                             ("worker.launch.start", "worker.launch.end"))]
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])
        ledger = next(row for row in result["attempt_ledger"]
                      if row["trace_id"] == trace)
        self.assertEqual("no configured worker is capable and healthy",
                         ledger["diagnostic"])

    def test_exhausted_worker_outcome_requires_launch_evidence(self):
        values = list(fixture())
        trace = "trace-20-0"
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome" and
                       row.get("trace_id") == trace)
        outcome.update({"result": "NO_WORKER_LAUNCHED", "worker": None,
                        "exit": None, "diagnostic_ref": "worker-exhaustion"})
        values[1] = [row for row in values[1]
                     if not (row.get("trace_id") == trace and
                             row.get("event") in
                             ("worker.launch.start", "worker.launch.end"))]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("needs durable launch evidence" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))

        values[0]["evidence_unavailable"] = [{
            "kind": "attempt-launches", "story": 20, "identity": trace,
            "reason": "legacy launch records unavailable"}]
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_attempt_ledger_is_independent_of_fragment_order(self):
        values = list(fixture())
        first = report.build(*values)
        reversed_process = list(reversed(values[1]))
        second = report.build(values[0], reversed_process, values[2], values[3])
        self.assertEqual(first, second)

    def test_conflicting_claim_copies_fail_deterministically(self):
        values = list(fixture())
        claim = next(row for row in values[1]
                     if row.get("event") == "story.claimed")
        conflict = dict(claim)
        conflict["trace_id"] = "conflicting-trace"
        forward = list(values[1]) + [conflict]
        reverse = list(reversed(forward))
        first = report.build(values[0], forward, values[2], values[3])
        second = report.build(values[0], reverse, values[2], values[3])
        self.assertEqual(first, second)
        self.assertEqual("INCONCLUSIVE", first["rung_verdict"])
        self.assertTrue(any("conflicting trace identities" in finding
                            for finding in
                            first["measurement_integrity"]["findings"]))

    def test_claim_event_id_must_be_nonempty_string(self):
        for invalid in ("", 7):
            with self.subTest(invalid=invalid):
                values = list(fixture())
                for row in values[1]:
                    if (row.get("event") == "story.claimed" and
                            row.get("trace_id") == "trace-20-0"):
                        row["event_id"] = invalid
                result = report.build(*values)
                self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
                self.assertTrue(any("nonempty string event ID" in finding
                                    for finding in
                                    result["measurement_integrity"]["findings"]))

    def test_one_attempt_trace_cannot_have_multiple_claim_ids(self):
        values = list(fixture())
        claim = next(row for row in values[1]
                     if row.get("event") == "story.claimed")
        duplicate = dict(claim)
        duplicate["event_id"] = "different-claim-id"
        values[1].append(duplicate)
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("has multiple claim IDs" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))
        self.assertEqual(
            1, sum(row["trace_id"] == claim["trace_id"]
                   for row in result["attempt_ledger"]))

    def test_produced_pr_without_exact_head_review_is_inconclusive(self):
        values = list(fixture())
        values[1] = [row for row in values[1]
                     if not (row.get("event") == "review.outcome.published" and
                             row.get("story") == 20)]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertFalse(result["qualification_series"]["eligible"])
        self.assertEqual(0, result["qualification_series"]["pass_samples"])
        self.assertEqual(0, result["qualification_series"]["fail_samples"])
        self.assertIn("| Attempts | unavailable |", report.render(result))

    def test_mutable_head_name_is_not_exact_head_evidence(self):
        values = list(fixture())
        story = values[0]["stories"][0]
        old_head = story["head"]
        story["head"] = "main"
        for row in values[1]:
            if row.get("pull_request") == story["pull_request"] and row.get("head") == old_head:
                row["head"] = "main"
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("needs a full commit SHA" in finding for finding in
                            result["measurement_integrity"]["findings"]))

    def test_null_object_id_is_not_exact_head_evidence(self):
        values = list(fixture())
        story = values[0]["stories"][0]
        old_head = story["head"]
        story["head"] = "0" * 40
        for row in values[1]:
            if row.get("pull_request") == story["pull_request"] and row.get("head") == old_head:
                row["head"] = "0" * 40
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_commit_head_must_be_a_string(self):
        values = list(fixture())
        story = values[0]["stories"][0]
        old_head = story["head"]
        numeric_head = int("1" * 40)
        story["head"] = numeric_head
        for row in values[1]:
            if row.get("story") != story["number"]:
                continue
            if row.get("head") == old_head:
                row["head"] = numeric_head
            if row.get("event") == "worker.outcome":
                row["detail"] = json.dumps({
                    "pull_request": story["pull_request"],
                    "head": numeric_head})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("full commit SHA" in finding for finding in
                            result["measurement_integrity"]["findings"]))

    def test_pull_request_identity_must_be_positive_integer(self):
        for invalid in ("not-a-pr", -1, True):
            with self.subTest(invalid=invalid):
                values = list(fixture())
                story = values[0]["stories"][0]
                old_pr = story["pull_request"]
                story["pull_request"] = invalid
                for row in values[1]:
                    if row.get("pull_request") == old_pr:
                        row["pull_request"] = invalid
                result = report.build(*values)
                self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
                self.assertTrue(any("positive integer PR number" in finding
                                    for finding in
                                    result["measurement_integrity"]["findings"]))

    def test_falsy_declared_pr_is_rejected_even_with_valid_process_evidence(self):
        for invalid in (0, False):
            with self.subTest(invalid=invalid):
                values = list(fixture())
                values[0]["stories"][0]["pull_request"] = invalid
                result = report.build(*values)
                self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
                self.assertTrue(any("declares an invalid pull-request" in finding
                                    for finding in
                                    result["measurement_integrity"]["findings"]))

    def test_unhashable_declared_delivery_identity_is_inconclusive(self):
        for field, invalid in (("pull_request", []), ("head", {})):
            with self.subTest(field=field):
                values = list(fixture())
                values[0]["stories"][0][field] = invalid
                result = report.build(*values)
                self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
                self.assertTrue(any("declares an invalid" in finding
                                    for finding in result[
                                        "measurement_integrity"]["findings"]))

    def test_foreign_repository_cannot_supply_delivery_or_review_evidence(self):
        values = list(fixture())
        for row in values[1]:
            if (row.get("story") == 20 and row.get("event") in
                    ("delivery.pull-request.written", "review.outcome.published")):
                row["repo"] = "other/product"
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("production event" in finding for finding in
                            result["measurement_integrity"]["findings"]))

    def test_foreign_repository_cannot_supply_attempt_evidence(self):
        values = list(fixture())
        for row in values[1]:
            if (row.get("story") == 20 and row.get("event") in
                    ("story.claimed", "worker.launch.start",
                     "worker.launch.end", "worker.outcome")):
                row["repo"] = "other/product"
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertIn("Story 20 has no durable attempt claim evidence",
                      result["measurement_integrity"]["findings"])
        self.assertFalse(any(row["story"] == 20
                             for row in result["attempt_ledger"]))

    def test_missing_repository_cannot_scope_legacy_evidence(self):
        values = list(fixture())
        values[0].pop("repository")
        for row in values[1]:
            row.pop("repo", None)
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertEqual([], result["attempt_ledger"])
        self.assertEqual([], result["review_ledger"])
        self.assertIn(
            "qualification evidence needs a valid measured owner/name repository",
            result["measurement_integrity"]["findings"])

    def test_declared_delivery_identity_must_belong_to_same_story(self):
        values = list(fixture())
        story_20, story_21 = values[0]["stories"][:2]
        story_20["pull_request"] = story_21["pull_request"]
        story_20["head"] = story_21["head"]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("production event" in finding for finding in
                            result["measurement_integrity"]["findings"]))
        declared = next(row for row in result["review_ledger"]
                        if row["story"] == 20 and
                        row["pull_request"] == story_21["pull_request"])
        self.assertIsNone(declared["review_outcome"])

    def test_merged_story_requires_durable_delivered_head(self):
        values = list(fixture())
        story = values[0]["stories"][0]
        story.pop("pull_request")
        story.pop("head")
        values[1] = [row for row in values[1]
                     if not (row.get("story") == story["number"] and
                             row.get("event") in
                             ("delivery.pull-request.written",
                              "review.outcome.published"))]
        for row in values[1]:
            if row.get("story") == story["number"] and row.get("event") == "worker.outcome":
                row.update({"result": "FAILED", "exit": 1,
                            "diagnostic_ref": "durable-diagnostic"})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertIn("merged Story 20 lacks a durable PR/head production event",
                      result["measurement_integrity"]["findings"])

    def test_declared_pr_without_production_head_is_inconclusive(self):
        values = list(fixture())
        values[1] = [row for row in values[1]
                     if not (row.get("event") == "delivery.pull-request.written"
                             and row.get("story") == 20)]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("production event" in finding for finding in
                            result["measurement_integrity"]["findings"]))

    def test_failed_attempt_requires_durable_diagnostic_or_unavailable_record(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome.update({"result": "FAILED", "exit": 1})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        values[0]["evidence_unavailable"] = [{
            "kind": "attempt-diagnostics", "story": outcome["story"],
            "identity": outcome["trace_id"], "reason": "legacy log missing"}]
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])
        ledger = next(row for row in result["attempt_ledger"]
                      if row["trace_id"] == outcome["trace_id"])
        self.assertEqual("legacy log missing",
                         ledger["diagnostic_evidence_unavailable"]["reason"])

    def test_failed_worker_outcome_requires_launch_evidence(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome.update({"result": "FAILED", "exit": 1,
                        "recovery_ref": "durable-recovery"})
        trace = outcome["trace_id"]
        values[1] = [
            row for row in values[1]
            if not (row.get("trace_id") == trace and row.get("event") in
                    ("worker.launch.start", "worker.launch.end"))]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("durable launch evidence" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))
        values[0]["evidence_unavailable"] = [{
            "kind": "attempt-launches", "story": outcome["story"],
            "identity": trace, "reason": "legacy launch evidence unavailable"}]
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_failed_attempt_accepts_matching_durable_launch_diagnostic(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome.update({"result": "FAILED", "exit": 1,
                        "detail": "generic summary is not evidence"})
        launch_end = next(
            row for row in values[1]
            if row.get("event") == "worker.launch.end" and
            row.get("trace_id") == outcome["trace_id"])
        launch_end.update({"event_id": "launch-failure-20", "result": "FAILED",
                           "exit": 1, "stderr": "durable diagnostic"})
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])
        ledger = next(row for row in result["attempt_ledger"]
                      if row["trace_id"] == outcome["trace_id"])
        self.assertEqual("launch-failure-20",
                         ledger["diagnostic"][0]["event_id"])

    def test_failed_launch_end_requires_matching_durable_start(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome.update({"result": "FAILED", "exit": 1})
        trace = outcome["trace_id"]
        values[1] = [
            row for row in values[1]
            if not (row.get("event") == "worker.launch.start" and
                    row.get("trace_id") == trace)]
        launch_end = next(
            row for row in values[1]
            if row.get("event") == "worker.launch.end" and
            row.get("trace_id") == trace)
        launch_end.update({"result": "FAILED", "exit": 1,
                           "stderr": "durable diagnostic"})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("matching durable launch start" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))

    def test_every_failed_launch_requires_its_own_diagnostic(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome.update({"result": "FAILED", "exit": 1})
        for index in (1, 2):
            values[1].append({
                "event": "worker.launch.start", "story": outcome["story"],
                "repo": values[0]["repository"],
                "trace_id": outcome["trace_id"], "event_id": f"start-{index}",
                "span_id": f"span-{index}", "worker": f"worker-{index}"})
        values[1].append({
            "event": "worker.launch.end", "story": outcome["story"],
            "repo": values[0]["repository"],
            "trace_id": outcome["trace_id"], "event_id": "end-1",
            "span_id": "span-1", "worker": "worker-1",
            "result": "FAILED", "exit": 1, "stderr": "failed"})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        values[0]["evidence_unavailable"] = [{
            "kind": "attempt-launch-diagnostics", "story": outcome["story"],
            "identity": f"{outcome['trace_id']}:start-2",
            "reason": "second launch log unavailable"}]
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])
        ledger = next(row for row in result["attempt_ledger"]
                      if row["trace_id"] == outcome["trace_id"])
        unavailable = next(row for row in ledger["launch_ledger"]
                           if row["start"]["event_id"] == "start-2")
        self.assertEqual("second launch log unavailable",
                         unavailable["evidence_unavailable"]["reason"])

    def test_failed_launch_diagnostics_match_worker_with_shared_span(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome.update({"result": "FAILED", "exit": 1})
        for worker in ("claude", "codex"):
            values[1].append({
                "event": "worker.launch.start", "story": outcome["story"],
                "repo": values[0]["repository"],
                "trace_id": outcome["trace_id"], "span_id": "shared-span",
                "worker": worker, "event_id": f"start-{worker}"})
        values[1].append({
            "event": "worker.launch.end", "story": outcome["story"],
            "repo": values[0]["repository"],
            "trace_id": outcome["trace_id"], "span_id": "shared-span",
            "worker": "claude", "event_id": "end-claude",
            "result": "FAILED", "exit": 1, "stderr": "failed"})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        values[1].append({
            "event": "worker.launch.end", "story": outcome["story"],
            "repo": values[0]["repository"],
            "trace_id": outcome["trace_id"], "span_id": "shared-span",
            "worker": "codex", "event_id": "end-codex",
            "result": "FAILED", "exit": 1, "stderr": "failed"})
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_failed_launch_is_reconciled_when_fallback_succeeds(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        for worker in ("claude", "codex"):
            values[1].append({
                "event": "worker.launch.start", "story": outcome["story"],
                "repo": values[0]["repository"],
                "trace_id": outcome["trace_id"], "span_id": "shared-span",
                "worker": worker, "event_id": f"start-{worker}"})
        values[1].append({
            "event": "worker.launch.end", "story": outcome["story"],
            "repo": values[0]["repository"],
            "trace_id": outcome["trace_id"], "span_id": "shared-span",
            "worker": "codex", "event_id": "end-codex", "exit": 0,
            "result": "LAUNCHED"})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        values[1].append({
            "event": "worker.launch.end", "story": outcome["story"],
            "repo": values[0]["repository"],
            "trace_id": outcome["trace_id"], "span_id": "shared-span",
            "worker": "claude", "event_id": "end-claude", "exit": 1,
            "result": "FAILED", "stderr": "failed before fallback"})
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_launched_outcome_requires_successful_matching_launch_end(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        trace = outcome["trace_id"]
        for row in values[1]:
            if row.get("event") == "worker.launch.end" and row.get("trace_id") == trace:
                row.update({"result": "FAILED", "exit": 1,
                            "detail": "worker rejected assignment"})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("successful worker-specific launch" in finding
                            for finding in result["measurement_integrity"]["findings"]))

    def test_successful_launch_end_requires_matching_start(self):
        values = list(fixture())
        trace = "trace-20-0"
        values[1] = [row for row in values[1]
                     if not (row.get("event") == "worker.launch.start" and
                             row.get("trace_id") == trace)]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("successful worker-specific launch" in finding
                            for finding in result["measurement_integrity"]["findings"]))

    def test_failed_fallback_launch_end_requires_diagnostic_content(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        values[1].extend([
            {"event": "worker.launch.start", "story": outcome["story"],
             "repo": values[0]["repository"],
             "trace_id": outcome["trace_id"], "span_id": "fallback-span",
             "worker": "failed-worker", "event_id": "fallback-start"},
            {"event": "worker.launch.end", "story": outcome["story"],
             "repo": values[0]["repository"],
             "trace_id": outcome["trace_id"], "span_id": "fallback-span",
             "worker": "failed-worker", "event_id": "fallback-end",
             "result": "FAILED", "exit": 1}])
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        values[1][-1]["stderr"] = "worker rejected assignment"
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_launch_records_require_worker_identity(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        values[1].extend([
            {"event": "worker.launch.start", "story": outcome["story"],
             "repo": values[0]["repository"],
             "trace_id": outcome["trace_id"], "span_id": "shared-span",
             "event_id": "anonymous-start"},
            {"event": "worker.launch.end", "story": outcome["story"],
             "repo": values[0]["repository"],
             "trace_id": outcome["trace_id"], "span_id": "shared-span",
             "event_id": "anonymous-end", "exit": 0, "result": "LAUNCHED"}])
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("needs a worker identity" in finding for finding in
                            result["measurement_integrity"]["findings"]))

    def test_nested_launch_ledger_is_independent_of_fragment_order(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome.update({"result": "FAILED", "exit": 1})
        for index in (2, 1):
            values[1].extend([
                {"event": "worker.launch.start", "story": outcome["story"],
                 "repo": values[0]["repository"],
                 "trace_id": outcome["trace_id"], "span_id": f"span-{index}",
                 "worker": f"worker-{index}", "event_id": f"start-{index}"},
                {"event": "worker.launch.end", "story": outcome["story"],
                 "repo": values[0]["repository"],
                 "trace_id": outcome["trace_id"], "span_id": f"span-{index}",
                 "worker": f"worker-{index}", "event_id": f"end-{index}",
                 "exit": 1, "stderr": "failed"}])
        first = report.build(*values)
        second = report.build(values[0], list(reversed(values[1])),
                              values[2], values[3])
        self.assertEqual(first, second)

    def test_conflicting_launch_copies_fail_deterministically(self):
        values = list(fixture())
        start = next(row for row in values[1]
                     if row.get("event") == "worker.launch.start")
        conflict = dict(start)
        conflict["worker"] = "conflicting-worker"
        forward = list(values[1]) + [conflict]
        reverse = list(reversed(forward))
        first = report.build(values[0], forward, values[2], values[3])
        second = report.build(values[0], reverse, values[2], values[3])
        self.assertEqual(first, second)
        self.assertEqual("INCONCLUSIVE", first["rung_verdict"])
        self.assertTrue(any("worker launch start" in finding and
                            "conflicting durable copies" in finding
                            for finding in
                            first["measurement_integrity"]["findings"]))

    def test_ambiguous_launch_accepts_matching_durable_diagnostic(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome.update({"result": "AMBIGUOUS", "exit": None})
        launch_end = next(
            row for row in values[1]
            if row.get("event") == "worker.launch.end" and
            row.get("trace_id") == outcome["trace_id"])
        launch_end.update({"event_id": "timeout-diagnostic", "exit": None,
                           "result": "AMBIGUOUS",
                           "stderr": "timed out with partial output"})
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_launch_end_requires_recognized_consistent_result(self):
        values = list(fixture())
        launch_end = next(row for row in values[1]
                          if row.get("event") == "worker.launch.end")
        launch_end.update({"result": "GARBAGE", "exit": 0,
                           "detail": "looks diagnostic"})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("worker launch end" in finding and
                            "recognized result" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))

    def test_malformed_launch_end_is_validated_before_filtering(self):
        values = list(fixture())
        launch_end = next(row for row in values[1]
                          if row.get("event") == "worker.launch.end")
        malformed = dict(launch_end)
        malformed["event_id"] = "malformed-launch-end"
        malformed.pop("result")
        malformed.pop("exit")
        values[1].append(malformed)
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("malformed-launch-end" in finding and
                            "recognized result" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))

    def test_launch_span_requires_nonempty_string_identity(self):
        for invalid in (123, "   "):
            with self.subTest(invalid=invalid):
                values = list(fixture())
                trace = "trace-20-0"
                for row in values[1]:
                    if (row.get("trace_id") == trace and row.get("event") in
                            ("worker.launch.start", "worker.launch.end")):
                        row["span_id"] = invalid
                result = report.build(*values)
                self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
                self.assertTrue(any("nonempty string span ID" in finding
                                    for finding in result[
                                        "measurement_integrity"]["findings"]))

    def test_reused_pr_requires_review_of_each_successful_delivered_head(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome" and
                       row.get("story") == 20)
        final_head = "f" * 40
        outcome["detail"] = json.dumps(
            {"pull_request": 120, "head": final_head})
        values[1] = [row for row in values[1]
                     if not (row.get("event") == "delivery.pull-request.written"
                             and row.get("trace_id") == outcome["trace_id"])]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        values[1].extend([
            {"event": "delivery.pull-request.written", "story": 20,
             "repo": values[0]["repository"],
             "trace_id": outcome["trace_id"], "pull_request": 120,
             "head": final_head, "event_id": "delivery-final-head"},
            {"event": "review.outcome.published", "story": 20,
             "repo": values[0]["repository"],
             "pull_request": 120, "head": final_head, "verdict": "findings",
             "event_id": "review-final-head"}])
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_successful_outcome_identity_must_match_production_event(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome["detail"] = json.dumps({
            "pull_request": 999, "head": "f" * 40})
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any(
            "conflicting terminal and production PR/head identities" in finding
            for finding in result["measurement_integrity"]["findings"]))

    def test_production_event_without_durable_id_is_inconclusive(self):
        values = list(fixture())
        production = next(row for row in values[1]
                          if row.get("event") == "delivery.pull-request.written")
        production.pop("event_id")
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_production_event_requires_string_event_id(self):
        for invalid in ("   ", 123):
            with self.subTest(invalid=invalid):
                values = list(fixture())
                production = next(
                    row for row in values[1]
                    if row.get("event") == "delivery.pull-request.written")
                production["event_id"] = invalid
                result = report.build(*values)
                self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
                self.assertTrue(any("production event" in finding and
                                    "nonempty string event ID" in finding
                                    for finding in result[
                                        "measurement_integrity"]["findings"]))

    def test_pr_only_declaration_uses_durable_process_head(self):
        values = list(fixture())
        values[0]["stories"][0].pop("head")
        result = report.build(*values)
        self.assertNotEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_mixed_legacy_and_durable_production_duplicates_are_order_independent(self):
        values = list(fixture())
        production = next(row for row in values[1]
                          if row.get("event") == "delivery.pull-request.written")
        legacy = dict(production)
        legacy.pop("event_id")
        forward = list(values[1]) + [legacy]
        reverse = [legacy] + list(values[1])
        first = report.build(values[0], forward, values[2], values[3])
        second = report.build(values[0], reverse, values[2], values[3])
        self.assertEqual(first, second)
        self.assertNotEqual("INCONCLUSIVE", first["rung_verdict"])

    def test_mixed_legacy_and_durable_review_duplicates_are_order_independent(self):
        values = list(fixture())
        reviewed = next(row for row in values[1]
                        if row.get("event") == "review.outcome.published")
        legacy = dict(reviewed)
        legacy.pop("event_id")
        forward = list(values[1]) + [legacy]
        reverse = [legacy] + list(values[1])
        first = report.build(values[0], forward, values[2], values[3])
        second = report.build(values[0], reverse, values[2], values[3])
        self.assertEqual(first, second)
        self.assertNotEqual("INCONCLUSIVE", first["rung_verdict"])

    def test_conflicting_review_copies_fail_deterministically(self):
        values = list(fixture())
        reviewed = next(row for row in values[1]
                        if row.get("event") == "review.outcome.published")
        conflict = dict(reviewed)
        conflict["reviewer"] = "conflicting-reviewer"
        forward = list(values[1]) + [conflict]
        reverse = list(reversed(forward))
        first = report.build(values[0], forward, values[2], values[3])
        second = report.build(values[0], reverse, values[2], values[3])
        self.assertEqual(first, second)
        self.assertEqual("INCONCLUSIVE", first["rung_verdict"])
        self.assertTrue(any("scoped review event" in finding and
                            "conflicting durable copies" in finding
                            for finding in
                            first["measurement_integrity"]["findings"]))

    def test_review_copy_conflict_is_detected_before_head_filtering(self):
        values = list(fixture())
        reviewed = next(row for row in values[1]
                        if row.get("event") == "review.outcome.published")
        conflict = dict(reviewed)
        conflict["head"] = "f" * 40
        forward = list(values[1]) + [conflict]
        reverse = list(reversed(forward))
        first = report.build(values[0], forward, values[2], values[3])
        second = report.build(values[0], reverse, values[2], values[3])
        self.assertEqual(first, second)
        self.assertEqual("INCONCLUSIVE", first["rung_verdict"])
        self.assertTrue(any("scoped review event" in finding and
                            "conflicting durable copies" in finding
                            for finding in
                            first["measurement_integrity"]["findings"]))

    def test_exact_head_review_requires_string_event_id(self):
        values = list(fixture())
        reviewed = next(row for row in values[1]
                        if row.get("event") == "review.outcome.published")
        reviewed["event_id"] = 123
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])
        self.assertTrue(any("exact-head review outcome" in finding and
                            "nonempty string event ID" in finding
                            for finding in
                            result["measurement_integrity"]["findings"]))

    def test_non_object_delivery_detail_yields_inconclusive(self):
        values = list(fixture())
        outcome = next(row for row in values[1]
                       if row.get("event") == "worker.outcome")
        outcome["detail"] = "[]"
        values[1] = [row for row in values[1]
                     if not (row.get("event") == "delivery.pull-request.written"
                             and row.get("trace_id") == outcome["trace_id"])]
        result = report.build(*values)
        self.assertEqual("INCONCLUSIVE", result["rung_verdict"])

    def test_thresholds_can_pass_but_integrity_is_independent(self):
        values = list(fixture())
        for story in values[0]["stories"]: story["human_recovery"] = False
        values[0]["stories"][0]["poisoned"] = False
        values[0]["stories"][1]["poisoned"] = False
        result = report.build(*values)
        self.assertEqual("PASS", result["rung_verdict"])
        self.assertTrue(result["measurement_integrity"]["passed"])

        values[3].pop()
        result = report.build(*values)
        self.assertEqual("FAIL", result["rung_verdict"])
        self.assertFalse(result["threshold_results"]["measurement_integrity"])

    def test_governance_does_not_reduce_autonomy_but_recovery_does(self):
        values = list(fixture())
        for story in values[0]["stories"]:
            story["human_recovery"] = False
            story["poisoned"] = False
        values[0]["operator_actions"] = [
            {"action": "owner Chrome observation", "classification": "governance",
             "story": 23},
            {"action": "manual stale-worker repair", "classification": "recovery",
             "story": 20},
        ]
        result = report.build(*values)
        self.assertEqual([21, 22, 23],
                         result["kpis"]["autonomy"]["autonomous_story_numbers"])
        self.assertEqual([20],
                         result["kpis"]["autonomy"]["non_autonomous_story_numbers"])

    def test_capacity_receipts_cover_every_route_and_unpriced_usage_is_complete(self):
        values = list(fixture())
        values[2] = [
            {"metric": "capacity.route.attempt", "story": 20,
             "invocation_id": "delivery-20", "model": "gpt-5.4",
             "usage_receipt": {"normalized_capacity_units": 2.5,
                               "capacity_unit_basis": "reconciled-reservation",
                               "exact_cost_usd": None,
                               "dollar_cost_unavailable_reason": "subscription-backed",
                               "reported_usage": {"input_tokens": 100}}},
            {"metric": "capacity.route.attempt", "story": 21,
             "invocation_id": "review-21-timeout", "model": "gpt-5.4",
             "usage_receipt": {"normalized_capacity_units": 1.0,
                               "capacity_unit_basis": "reconciled-reservation",
                               "exact_cost_usd": None,
                               "dollar_cost_unavailable_reason": "subscription-backed",
                               "reported_usage": None}},
        ]
        result = report.build(*values)
        usage = result["kpis"]["engine_usage_cost"]
        self.assertTrue(result["measurement_integrity"]["passed"])
        self.assertEqual("complete", usage["usage_receipts_status"])
        self.assertEqual(2, usage["route_invocations"])
        self.assertEqual(3.5, usage["normalized_capacity_units"])
        self.assertEqual("unavailable-provider-pricing", usage["cost_status"])
        self.assertEqual("unavailable", usage["known_reported_cost_usd"])

    def test_superseded_approval_does_not_hide_the_active_cycle_boundary(self):
        values = fixture()
        values[0]["decisions"].insert(2, {
            "bell_type": "plan-approval", "result": "approved",
            "timestamp": "2026-01-01T00:01:30Z", "superseded": True})
        values[3].insert(2, {
            "project": "owner/product#18", "bell_type": "plan-approval",
            "classification": "decision"})
        result = report.build(*values)
        self.assertTrue(result["measurement_integrity"]["passed"])
        self.assertEqual(3600, result["kpis"]["cycle_time"]["seconds"])

    def test_missing_normalized_capacity_receipt_fails_measurement_integrity(self):
        values = list(fixture())
        values[2] = [{"metric": "capacity.route.attempt", "story": 20,
                      "invocation_id": "broken", "model": "gpt-5.4",
                      "usage_receipt": {"exact_cost_usd": None}}]
        result = report.build(*values)
        self.assertFalse(result["measurement_integrity"]["passed"])
        self.assertIn("complete reproducible usage receipt",
                      " ".join(result["measurement_integrity"]["findings"]))

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
