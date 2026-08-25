#!/usr/bin/env python3
"""Composition checks for Review and bridge Capacity Pool routing."""

import pathlib
import subprocess
import tempfile
import unittest

from factory.agents.review import invoke
from factory.capacity_pool.executor import CapacityExecutor
from factory.capacity_pool.policy import POLICIES
from factory.capacity_pool.providers import InvocationPayload
from factory.capacity_pool.router import ModelCapacity, Tier, route
from factory.capacity_pool.state import CapacityState

ROOT = pathlib.Path(__file__).resolve().parents[2]
REVIEW_CAPS = frozenset({"code", "reason", "json"})


class ReviewCapacityPoolAcceptance(unittest.TestCase):
    def setUp(self):
        self.state = CapacityState()
        self.models = (
            ModelCapacity("review-openai", "openai", Tier.BALANCED,
                          REVIEW_CAPS, prepaid_or_expiring=True),
            ModelCapacity("review-anthropic", "anthropic", Tier.BALANCED,
                          REVIEW_CAPS),
            ModelCapacity("review-flagship", "openai", Tier.FLAGSHIP,
                          REVIEW_CAPS),
        )
        for model in self.models:
            self.state.mark_healthy(model.provider, model.name, "test")

    def tearDown(self):
        self.state.close()

    def test_review_is_balanced_medium_and_risk_is_deliberate_flagship(self):
        normal = route(POLICIES["review"].request(), self.models)
        self.assertEqual(Tier.BALANCED, normal.primary.tier)
        self.assertTrue(all(step.effort == "medium" for step in normal.steps))
        escalated = route(
            POLICIES["review"].request(triggers={"security"}), self.models)
        self.assertEqual(Tier.FLAGSHIP, escalated.primary.tier)

    def test_retryable_failure_discards_private_output_before_peer(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            staging, output = root / "stage.json", root / "out.json"

            def runner(command, **_kwargs):
                calls.append(command[0])
                if command[0] == "codex":
                    staging.write_text('{"private":"failed"}')
                    return subprocess.CompletedProcess(
                        command, 7, "", "quota exhausted")
                self.assertFalse(staging.exists())
                staging.write_text('{"head":"ok","verdict":"approval"}')
                return subprocess.CompletedProcess(command, 0, "done", "")

            adapters = {provider: invoke.bounded_review_adapter(
                provider, cwd=root, environment={}, staging=staging,
                output=output, runner=runner)
                for provider in ("openai", "anthropic")}
            result = CapacityExecutor(adapters, self.state).execute(
                task_key="review-fallback", request=POLICIES["review"].request(),
                registry=self.models, payload=InvocationPayload("review"),
                validate=lambda _value: self.assertTrue(staging.exists()))
        self.assertEqual("success", result.outcome)
        self.assertEqual(["codex", "claude"], calls)

    def test_malformed_success_stops_without_switching_provider(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            staging, output = root / "stage.json", root / "out.json"

            def runner(command, **_kwargs):
                calls.append(command[0])
                staging.write_text("not-json")
                return subprocess.CompletedProcess(command, 0, "", "")

            adapters = {provider: invoke.bounded_review_adapter(
                provider, cwd=root, environment={}, staging=staging,
                output=output, runner=runner)
                for provider in ("openai", "anthropic")}

            def reject(_value):
                raise invoke.ReviewError("malformed reviewer output")

            result = CapacityExecutor(adapters, self.state).execute(
                task_key="review-malformed", request=POLICIES["review"].request(),
                registry=self.models, payload=InvocationPayload("review"),
                validate=reject)
        self.assertEqual("schema-invalid", result.outcome)
        self.assertEqual(["codex"], calls)

    def test_no_review_or_bridge_provider_selection_remains(self):
        for relative in ("factory/agents/review/invoke.py",
                         "factory/runtime/bridge.py",
                         "factory/acceptance/phase4_live.py", "live-e2e.sh"):
            source = (ROOT / relative).read_text()
            self.assertNotIn("FACTORY_REVIEW_MODEL_CMD", source)
            self.assertNotIn("--engine", source)


if __name__ == "__main__":
    unittest.main()
