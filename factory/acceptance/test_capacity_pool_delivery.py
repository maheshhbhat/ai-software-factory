#!/usr/bin/env python3
"""Composition checks for Delivery's Capacity Pool boundary."""

import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from factory.agents.worker import invoke
from factory.capacity_pool.executor import CapacityExecutor
from factory.capacity_pool.policy import POLICIES, resolved_registry
from factory.capacity_pool.providers import AttemptResult, ProviderAdapter
from factory.capacity_pool.router import ModelCapacity, Tier, route
from factory.capacity_pool.state import CapacityState

ROOT = pathlib.Path(__file__).resolve().parents[2]
CAPS = frozenset({"code", "write", "tests"})


class DeliveryCapacityPoolAcceptance(unittest.TestCase):
    def setUp(self):
        self.state = CapacityState()

    def tearDown(self):
        self.state.close()

    def registry(self):
        values = (
            ModelCapacity("spark-real", "openai", Tier.BALANCED, CAPS,
                          prepaid_or_expiring=True),
            ModelCapacity("terra", "openai", Tier.BALANCED, CAPS),
            ModelCapacity("anthropic-balanced", "anthropic", Tier.BALANCED, CAPS),
            ModelCapacity("sol", "openai", Tier.FLAGSHIP, CAPS,
                          supports_effort=frozenset({"medium", "high", "max"})),
        )
        for item in values:
            self.state.mark_healthy(item.provider, item.name, "test-probe")
        return values

    def test_normal_delivery_prefers_verified_spark_without_tier_raise(self):
        plan = route(POLICIES["delivery"].request(), self.registry())
        self.assertEqual("spark-real", plan.primary.model)
        self.assertTrue(all(step.tier is Tier.BALANCED for step in plan.steps))
        self.assertTrue(all(step.effort == "medium" for step in plan.steps))
        self.assertNotIn("sol", [step.model for step in plan.steps])

    def test_retryable_failure_uses_peer_but_mutation_suppresses_fallback(self):
        calls = []
        executor = CapacityExecutor({
            "openai": ProviderAdapter("openai", lambda **kwargs: (
                calls.append(kwargs["model"]) or AttemptResult(
                    "quota", consumed_budget_units=0))),
            "anthropic": ProviderAdapter("anthropic", lambda **kwargs: (
                calls.append(kwargs["model"]) or AttemptResult(
                    "success", "done", consumed_budget_units=1))),
        }, self.state)
        result = executor.execute(
            task_key="delivery-clean", request=POLICIES["delivery"].request(),
            registry=self.registry(), payload={})
        self.assertEqual("success", result.outcome)
        self.assertEqual(["spark-real", "anthropic-balanced"], calls)

        calls.clear()
        executor = CapacityExecutor({
            "openai": ProviderAdapter("openai", lambda **kwargs: (
                calls.append(kwargs["model"]) or AttemptResult(
                    "quota", consumed_budget_units=0,
                    mutation_state="post-mutation"))),
            "anthropic": ProviderAdapter("anthropic", lambda **kwargs: (
                calls.append(kwargs["model"]) or AttemptResult("success", "done"))),
        }, self.state)
        result = executor.execute(
            task_key="delivery-mutated", request=POLICIES["delivery"].request(),
            registry=self.registry(), payload={})
        self.assertEqual("ambiguous-mutation", result.outcome)
        self.assertEqual(["spark-real"], calls)

    def test_dispatcher_and_worker_contain_no_provider_selection(self):
        worker = (ROOT / "factory/agents/worker/invoke.py").read_text()
        dispatcher = (ROOT / "factory/dispatcher/dispatcher.py").read_text()
        wrapper = (ROOT / "poll.sh").read_text()
        self.assertNotIn("FACTORY_DELIVERY_MODEL_CMD", worker)
        self.assertNotIn("--engine", worker)
        self.assertNotIn("claude-delivery", dispatcher)
        self.assertNotIn("codex-delivery", dispatcher)
        self.assertIn('FACTORY_WORKER_ORDER="capacity-delivery"', wrapper)
        self.assertNotIn("--engine claude", wrapper)
        self.assertNotIn("--engine codex", wrapper)

    def test_unprobed_delivery_capacity_is_ineligible(self):
        registry = resolved_registry(
            {"FACTORY_CAPACITY_OPENAI_SPARK_MODEL": "spark-real"},
            health=self.state.health)
        request = POLICIES["delivery"].request()
        with self.assertRaises(LookupError):
            route(request, registry)
        self.state.mark_healthy("openai", "spark-real", "doctor-probe")
        plan = route(request, resolved_registry(
            {"FACTORY_CAPACITY_OPENAI_SPARK_MODEL": "spark-real"},
            health=self.state.health))
        self.assertEqual("spark-real", plan.primary.model)

    def test_delivery_mutates_validates_tests_then_writes_one_pr(self):
        story_body = ("### Project\n\n#20\n\n### Scope\n\nsrc/app.py\n\n"
                      "### Spend cap\n\n$2 / 5 min\n")

        class Client:
            created = None
            def api(self, path, **_):
                self.assert_path = path
                return {"default_branch": "main"}
            def issue(self, number):
                return ({"number": 21, "body": story_body,
                         "labels": ["type:story", "story:claimed"]}
                        if number == 21 else {"number": 20, "body": "### Goal\n\nx"})
            def pages(self, path):
                if path.endswith("/timeline"):
                    return [{"event": "labeled", "id": 7,
                             "label": {"name": "story:claimed"}}]
                return []
            def pull_requests(self):
                return [] if self.created is None else [self.created]
            def create_pr(self, title, head, base, body):
                self.created = {"number": 9, "body": body,
                                "head": {"ref": head, "sha": "abc123"}}
                return self.created

        mutated, calls = False, []
        def runner(command, **kwargs):
            nonlocal mutated
            calls.append((list(command), kwargs.get("env")))
            stdout = ""
            if command[0] == "codex":
                mutated = True
                stdout = "delivery complete"
            elif command[:3] == ["git", "status", "--porcelain"]:
                stdout = "?? src/app.py\n" if mutated else ""
            elif command[:2] == ["git", "rev-parse"]:
                stdout = "abc123\n"
            return subprocess.CompletedProcess(command, 0, stdout, "")

        model = ModelCapacity("terra", "openai", Tier.BALANCED, CAPS)
        self.state.mark_healthy("openai", "terra", "doctor-probe")
        client = Client()
        with tempfile.TemporaryDirectory() as checkout, mock.patch.dict(
                invoke.os.environ, {"PATH": "/bin", "FACTORY_DELIVERY_TEST_CMD": "true"},
                clear=True):
            result = invoke.execute(
                "owner/product", 21, "token", pathlib.Path(checkout),
                runner=runner, client=client, state=self.state, registry=(model,))
        self.assertEqual(9, result.pull_request)
        self.assertIsNotNone(client.created)
        provider_call = next(item for item in calls if item[0][0] == "codex")
        self.assertIn("workspace-write", provider_call[0])
        self.assertNotIn("GH_TOKEN", provider_call[1])
        test_call = next(item for item in calls if item[0] == ["true"])
        self.assertEqual({"PATH": "/bin"}, test_call[1])


if __name__ == "__main__":
    unittest.main()
