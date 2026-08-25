#!/usr/bin/env python3
"""Hermetic contract for the controlled real provider failover proof."""

import json
import pathlib
import tempfile
import unittest

from factory.acceptance import capacity_failover_live as live
from factory.capacity_pool.providers import AttemptResult, ProviderAdapter


class ControlledFailoverTests(unittest.TestCase):
    def adapters(self, fallback_output=None, *, primary_cost=0):
        observed = []

        def primary(**_kwargs):
            return AttemptResult(
                "unavailable", consumed_budget_units=primary_cost,
                diagnostic="controlled outage", failure_scope="provider")

        def fallback(**kwargs):
            observed.append(kwargs)
            return AttemptResult(
                "success", fallback_output or
                json.dumps({"sentinel": live.SENTINEL}),
                consumed_budget_units=kwargs["budget_units"])

        return ({"anthropic": ProviderAdapter(
                    "anthropic", primary, probe=lambda **_kwargs: True),
                 "openai": ProviderAdapter("openai", fallback)}, observed)

    def run_exercise(self, adapters):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = pathlib.Path(temporary.name) / "evidence.json"
        return live.exercise(output, adapters=adapters, clock=[1000.0]), output

    def test_primary_failure_falls_back_to_sol_and_recovers_by_probe(self):
        adapters, observed = self.adapters()
        evidence, output = self.run_exercise(adapters)
        self.assertEqual("success", evidence["outcome"])
        self.assertEqual(["claude-fable-5", "gpt-5.6-sol"],
                         [item["model"] for item in evidence["attempts"]])
        self.assertTrue(all(item["tier"] == "flagship" and
                            item["effort"] == "medium"
                            for item in evidence["attempts"]))
        self.assertEqual("cooldown", evidence["recovery"]["state_after_failure"])
        self.assertEqual("healthy", evidence["recovery"]["final_state"])
        transitions = [item["new_state"] for item in
                       evidence["recovery"]["transitions"]]
        self.assertIn("probe", transitions)
        self.assertEqual({"success": True, "timeout_seconds": 30,
                          "effort": "medium", "kind": "controlled-adapter"},
                         evidence["recovery"]["probe"])
        self.assertTrue(output.exists())
        self.assertEqual(1, len(observed))

    def test_fallback_receives_only_the_remaining_combined_budget(self):
        adapters, observed = self.adapters(primary_cost=0.4)
        evidence, _output = self.run_exercise(adapters)
        self.assertAlmostEqual(0.6, observed[0]["budget_units"])
        self.assertAlmostEqual(1.0,
                               evidence["combined_envelope"]["consumed_budget_units"])

    def test_malformed_fallback_is_a_quality_stop(self):
        adapters, _observed = self.adapters("not-json")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "schema-invalid"):
                live.exercise(pathlib.Path(temp) / "evidence.json",
                              adapters=adapters, clock=[1000.0])

    def test_absent_fallback_fails_and_writes_no_evidence(self):
        output = pathlib.Path(tempfile.mkdtemp()) / "evidence.json"
        self.addCleanup(__import__("shutil").rmtree, output.parent, True)
        with self.assertRaisesRegex(RuntimeError, "controlled fallback failed"):
            live.exercise(
                output, adapters={"anthropic": live.controlled_primary()},
                clock=[1000.0])
        self.assertFalse(output.exists())

    def test_evidence_contains_no_prompt_or_credential(self):
        adapters, _observed = self.adapters()
        evidence, output = self.run_exercise(adapters)
        serialized = output.read_text()
        self.assertNotIn("Return exactly", serialized)
        self.assertNotIn("API_KEY", serialized)
        self.assertNotIn("TOKEN", serialized)
        self.assertIn("controlled adapter", evidence["limitations"][0])

    def test_failed_recovery_probe_cannot_restore_health(self):
        adapters, _observed = self.adapters()
        adapters["anthropic"] = ProviderAdapter(
            "anthropic", adapters["anthropic"]._invoke,
            probe=lambda **_kwargs: False)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "recover"):
                live.exercise(pathlib.Path(temp) / "evidence.json",
                              adapters=adapters, clock=[1000.0])


if __name__ == "__main__":
    unittest.main()
