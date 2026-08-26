#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path

from factory.capacity_pool.executor import CapacityExecutor
from factory.capacity_pool.providers import AttemptResult, ProviderAdapter
from factory.capacity_pool.router import ModelCapacity, RouteRequest, Tier
from factory.capacity_pool.state import CapacityState


CAPS = frozenset({"reason", "json"})


class CapacityExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = CapacityState(Path(self.tmp.name) / "state.sqlite", uri=False)
        for provider, model in (("openai", "terra"), ("anthropic", "sonnet"),
                                ("openai", "sol")):
            self.state.mark_healthy(provider, model)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def request(self, budget=5):
        return RouteRequest("planning", CAPS, Tier.BALANCED, "medium", 100, budget)

    def registry(self):
        return (
            ModelCapacity("terra", "openai", Tier.BALANCED, CAPS,
                          prepaid_or_expiring=True),
            ModelCapacity("sonnet", "anthropic", Tier.BALANCED, CAPS),
            ModelCapacity("sol", "openai", Tier.FLAGSHIP, CAPS),
        )

    def test_retryable_failure_uses_peer_without_tier_or_effort_change(self):
        calls = []
        def openai(**kwargs):
            calls.append(kwargs)
            return AttemptResult("quota", consumed_budget_units=2)
        def anthropic(**kwargs):
            calls.append(kwargs)
            return AttemptResult("success", '{"ok":true}', consumed_budget_units=1)
        executor = CapacityExecutor({
            "openai": ProviderAdapter("openai", openai),
            "anthropic": ProviderAdapter("anthropic", anthropic),
        }, self.state)
        result = executor.execute(task_key="plan-1", request=self.request(),
                                  registry=self.registry(), payload={})
        self.assertEqual("success", result.outcome)
        self.assertEqual(["terra", "sonnet"], [row["model"] for row in result.attempts])
        self.assertEqual(["medium", "medium"], [call["effort"] for call in calls])
        self.assertEqual(3, result.consumed_budget_units)

    def test_unreported_usage_consumes_only_bounded_attempt_reservation(self):
        executor = CapacityExecutor({
            "openai": ProviderAdapter("openai", lambda **_: AttemptResult("quota")),
            "anthropic": ProviderAdapter("anthropic", lambda **_: AttemptResult("success")),
        }, self.state)
        result = executor.execute(task_key="plan-2", request=self.request(2),
                                  registry=self.registry(), payload={})
        self.assertEqual("success", result.outcome)
        self.assertEqual(2, result.consumed_budget_units)

    def test_no_eligible_capacity_is_terminal_result(self):
        result = CapacityExecutor({}, self.state).execute(
            task_key="none", request=self.request(), registry=(), payload={})
        self.assertEqual("no-eligible-capacity", result.outcome)
        self.assertEqual((), result.attempts)

    def test_quality_failure_never_switches_provider(self):
        called = []
        executor = CapacityExecutor({
            "openai": ProviderAdapter("openai", lambda **_: AttemptResult("schema-invalid")),
            "anthropic": ProviderAdapter("anthropic", lambda **_: called.append(True)),
        }, self.state)
        result = executor.execute(task_key="plan-3", request=self.request(),
                                  registry=self.registry(), payload={})
        self.assertEqual("schema-invalid", result.outcome)
        self.assertFalse(called)

    def test_provider_scoped_failure_updates_provider_health(self):
        executor = CapacityExecutor({
            "openai": ProviderAdapter("openai", lambda **_: AttemptResult(
                "unavailable", consumed_budget_units=1, failure_scope="provider")),
            "anthropic": ProviderAdapter("anthropic", lambda **_: AttemptResult(
                "success", "ok", consumed_budget_units=1)),
        }, self.state)
        result = executor.execute(task_key="plan-provider", request=self.request(),
                                  registry=self.registry(), payload={})
        self.assertEqual("success", result.outcome)
        self.assertEqual("cooldown", self.state.health("openai", "*")["state"])

    def test_ambiguous_mutation_never_launches_second_writer(self):
        called = []
        executor = CapacityExecutor({
            "openai": ProviderAdapter("openai", lambda **_: AttemptResult(
                "timeout", mutation_state="ambiguous", consumed_budget_units=1)),
            "anthropic": ProviderAdapter("anthropic", lambda **_: called.append(True)),
        }, self.state)
        result = executor.execute(task_key="delivery-1", request=self.request(),
                                  registry=self.registry(), payload={})
        self.assertEqual("ambiguous-mutation", result.outcome)
        self.assertFalse(called)

    def test_validation_failure_is_a_stop(self):
        executor = CapacityExecutor({
            "openai": ProviderAdapter("openai", lambda **_: AttemptResult(
                "success", "bad", consumed_budget_units=1)),
        }, self.state)
        def reject(_): raise ValueError("bad schema")
        result = executor.execute(task_key="plan-4", request=self.request(),
                                  registry=self.registry()[:1], payload={}, validate=reject)
        self.assertEqual("schema-invalid", result.outcome)

    def test_pre_reserved_route_is_consumed_once_before_model_start(self):
        task = "delivery:o/r:20:1"
        lease = self.state.reserve(task, "anthropic", "sonnet", 2,
                                   ttl_seconds=100)
        order = []
        executor = CapacityExecutor({
            "openai": ProviderAdapter("openai", lambda **_: order.append("openai")),
            "anthropic": ProviderAdapter(
                "anthropic", lambda **_: (order.append("anthropic") or
                                           AttemptResult("success", "ok", 1))),
        }, self.state)
        result = executor.execute(
            task_key=task, request=self.request(), registry=self.registry(),
            payload={}, reservation_id=lease.lease_id,
            on_started=lambda value: order.append(f"start:{value.lease_id}"))
        self.assertEqual("success", result.outcome)
        self.assertEqual([f"start:{lease.lease_id}", "anthropic"], order)
        self.assertEqual("complete", self.state.lease_status(lease.lease_id))

    def test_expired_reservation_never_starts_adapter(self):
        called = []
        lease = self.state.reserve("delivery", "openai", "terra", 1,
                                   ttl_seconds=0.001)
        import time
        time.sleep(0.01)
        result = CapacityExecutor({
            "openai": ProviderAdapter("openai", lambda **_: called.append(True)),
        }, self.state).execute(
            task_key="delivery", request=self.request(), registry=self.registry(),
            payload={}, reservation_id=lease.lease_id)
        self.assertEqual("no-eligible-capacity", result.outcome)
        self.assertFalse(called)

    def test_failed_start_evidence_releases_consumed_reservation(self):
        lease = self.state.reserve("delivery", "openai", "terra", 1,
                                   ttl_seconds=100)
        result = CapacityExecutor({
            "openai": ProviderAdapter("openai", lambda **_: self.fail("must not run")),
        }, self.state).execute(
            task_key="delivery", request=self.request(), registry=self.registry(),
            payload={}, reservation_id=lease.lease_id,
            on_started=lambda _: (_ for _ in ()).throw(RuntimeError("write failed")))
        self.assertEqual("start-evidence-failed", result.outcome)
        self.assertEqual((), result.attempts)
        self.assertEqual("released", self.state.lease_status(lease.lease_id))

    def test_changed_registry_cannot_expand_a_prior_reservation(self):
        task = "delivery"
        lease = self.state.reserve(task, "openai", "terra", 1,
                                   ttl_seconds=20)
        observed = {}
        executor = CapacityExecutor({
            "openai": ProviderAdapter(
                "openai", lambda **kwargs: (
                    observed.update(kwargs) or AttemptResult("success", "ok", 1))),
        }, self.state)
        result = executor.execute(
            task_key=task, request=self.request(budget=5),
            registry=self.registry()[:1], payload={},
            reservation_id=lease.lease_id)
        self.assertEqual("success", result.outcome)
        self.assertEqual(1, observed["budget_units"])
        self.assertLessEqual(observed["timeout_seconds"], 20)


if __name__ == "__main__":
    unittest.main()
