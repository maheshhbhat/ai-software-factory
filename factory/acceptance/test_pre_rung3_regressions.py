import copy
import unittest

from factory.acceptance import pre_rung3_regressions as regressions


CONTROL = {
    "project47_scale": {
        "portfolio_usd": 1_000_000, "elapsed_ms": 900, "bound_ms": 2_000,
        "responsive": True, "result_rendered": True, "work_units": 240,
        "work_bound": 300,
        "work_bound_kind": "allocation-regimes-and-logarithmic-refinement",
        "portfolio_cent_iterations": 0,
        "evidence": "browser/project47-control.json",
    },
    "project30_provider": {
        "offline": {"parsed": True, "deterministic": True},
        "live_observations": [
            {"provider": provider, "started_at": "2026-08-26T12:00:00Z",
             "completed_at": "2026-08-26T12:00:01Z", "bounded_by_seconds": 30,
             "read_only": True, "compatible": True, "current": True,
             "stale_fallback_presented": False,
             "evidence": f"provider/{provider}.json"}
            for provider in ("vanguard", "fidelity")],
    },
    "capacity_recovery": {
        "zero_capacity_mutations": 0, "zero_capacity_attempt_delta": 0,
        "recovered_claims": 1, "worker_starts": 1,
        "reservation_reused": False, "evidence": "capacity/recovery.json",
    },
    "adapter_contract": {
        "enabled_routes": ["openai/terra", "anthropic/sonnet"],
        "probes": [
            {"route": route, "live": True, "read_only": True,
             "bounded_by_seconds": 30,
             "checks": {name: "pass" for name in regressions.REQUIRED_PROBE_CHECKS},
             "evidence": f"adapters/{route}.json"}
            for route in ("openai/terra", "anthropic/sonnet")],
    },
}


class PreRung3RegressionTests(unittest.TestCase):
    def test_complete_production_shaped_control_passes(self):
        result = regressions.evaluate(CONTROL)
        self.assertEqual("pass", result["overall"])
        self.assertEqual(list(regressions.CLASSES),
                         [item["class"] for item in result["results"]])

    def assert_class_fails(self, evidence, name, text):
        result = regressions.evaluate(evidence)
        row = next(item for item in result["results"] if item["class"] == name)
        self.assertEqual("fail", row["result"])
        self.assertIn(text, row["detail"])

    def test_project47_toy_unbounded_and_unresponsive_shapes_fail(self):
        cases = (("portfolio_usd", 20, "portfolio_usd"),
                 ("elapsed_ms", 2_001, "response bound"),
                 ("responsive", False, "responsive"),
                 ("portfolio_cent_iterations", 100_000_000, "cent-sized"))
        for field, value, message in cases:
            evidence = copy.deepcopy(CONTROL)
            evidence["project47_scale"][field] = value
            with self.subTest(field=field):
                self.assert_class_fails(evidence, "project47_scale", message)

    def test_project30_fixture_only_stale_and_incompatible_shapes_fail(self):
        evidence = copy.deepcopy(CONTROL)
        evidence["project30_provider"]["live_observations"] = []
        self.assert_class_fails(evidence, "project30_provider", "fixture-only")
        for field, value, message in (("current", False, "stale"),
                                      ("compatible", False, "incompatible"),
                                      ("stale_fallback_presented", True, "stale")):
            evidence = copy.deepcopy(CONTROL)
            evidence["project30_provider"]["live_observations"][0][field] = value
            with self.subTest(field=field):
                self.assert_class_fails(evidence, "project30_provider", message)

    def test_capacity_zero_mutation_or_duplicate_start_fails(self):
        for field, value, message in (("zero_capacity_mutations", 1, "zero_capacity_mutations"),
                                      ("worker_starts", 2, "worker_starts"),
                                      ("reservation_reused", True, "reused")):
            evidence = copy.deepcopy(CONTROL)
            evidence["capacity_recovery"][field] = value
            with self.subTest(field=field):
                self.assert_class_fails(evidence, "capacity_recovery", message)

    def test_each_live_adapter_contract_dimension_can_fail(self):
        for check in regressions.REQUIRED_PROBE_CHECKS:
            evidence = copy.deepcopy(CONTROL)
            evidence["adapter_contract"]["probes"][0]["checks"][check] = "fail"
            with self.subTest(check=check):
                self.assert_class_fails(evidence, "adapter_contract", check)

    def test_missing_class_is_invalid_not_a_partial_pass(self):
        evidence = copy.deepcopy(CONTROL)
        evidence.pop("capacity_recovery")
        with self.assertRaisesRegex(regressions.RegressionError, "exactly four"):
            regressions.evaluate(evidence)


if __name__ == "__main__":
    unittest.main()
