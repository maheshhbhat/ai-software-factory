#!/usr/bin/env python3
import unittest

from factory.capacity_pool import policy
from factory.capacity_pool.router import ModelCapacity, Tier, route


class CapacityPolicyTests(unittest.TestCase):
    def test_normal_workloads_do_not_request_flagship(self):
        self.assertEqual(Tier.BALANCED, policy.POLICIES["planning"].request().minimum_tier)
        self.assertEqual(Tier.BALANCED, policy.POLICIES["delivery"].request().minimum_tier)
        self.assertEqual(Tier.BALANCED, policy.POLICIES["review"].request().minimum_tier)
        self.assertEqual(Tier.ECONOMY, policy.POLICIES["bridge"].request().minimum_tier)
        self.assertEqual(Tier.ECONOMY, policy.POLICIES["readiness"].request().minimum_tier)

    def test_checked_in_trigger_is_required_for_flagship(self):
        request = policy.POLICIES["planning"].request(triggers={"architecture"})
        self.assertEqual(Tier.FLAGSHIP, request.minimum_tier)
        with self.assertRaisesRegex(ValueError, "unsupported escalation"):
            policy.POLICIES["planning"].request(triggers={"provider-failed"})

    def test_failure_route_cannot_climb_tier(self):
        request = policy.POLICIES["planning"].request()
        registry = (
            ModelCapacity("terra", "openai", Tier.BALANCED, request.required_capabilities),
            ModelCapacity("sol", "openai", Tier.FLAGSHIP, request.required_capabilities),
            ModelCapacity("sonnet", "anthropic", Tier.BALANCED, request.required_capabilities),
        )
        self.assertEqual({Tier.BALANCED}, {step.tier for step in route(request, registry).steps})

    def test_spark_class_is_checked_in_but_disabled_until_slug_verified(self):
        spark = next(entry for entry in policy.REGISTRY if entry.name == "codex-spark")
        self.assertTrue(spark.prepaid_or_expiring)
        self.assertIsNone(spark.model_id)
        self.assertFalse(spark.capacity().available)

    def test_unverified_placeholders_require_config_and_healthy_probe(self):
        class Health:
            def __call__(self, provider, model):
                return {"state": "healthy" if model == "verified-sonnet" else "unknown"}
        registry = policy.resolved_registry(
            {"FACTORY_CAPACITY_ANTHROPIC_BALANCED_MODEL": "verified-sonnet"},
            health=Health())
        sonnet = next(item for item in registry if item.name == "verified-sonnet")
        self.assertTrue(sonnet.available)


if __name__ == "__main__":
    unittest.main()
