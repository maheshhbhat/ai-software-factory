#!/usr/bin/env python3
import unittest

from factory.capacity_pool import policy
from factory.capacity_pool.router import ModelCapacity, Tier, route


class CapacityPolicyTests(unittest.TestCase):
    def test_normal_workloads_do_not_request_flagship(self):
        self.assertEqual(Tier.BALANCED, policy.POLICIES["delivery"].request().minimum_tier)
        self.assertEqual(Tier.BALANCED, policy.POLICIES["review"].request().minimum_tier)
        self.assertEqual(Tier.BALANCED,
                         policy.POLICIES["production-readiness"].request().minimum_tier)
        self.assertEqual(Tier.ECONOMY, policy.POLICIES["bridge"].request().minimum_tier)
        self.assertEqual(Tier.ECONOMY, policy.POLICIES["readiness"].request().minimum_tier)

    def test_planning_requests_flagship_by_design(self):
        """Planning moved to Flagship 2026-09-20: its Balanced-tier
        candidates proved too thin in practice, so it routes to Flagship
        (Opus, GPT-5.6-sol) directly rather than escalating per-invocation.
        Unlike the other workloads, it has no escalation trigger at all —
        it already requests the top tier normally."""
        request = policy.POLICIES["planning"].request()
        self.assertEqual(Tier.FLAGSHIP, request.minimum_tier)
        with self.assertRaisesRegex(ValueError, "unsupported escalation"):
            policy.POLICIES["planning"].request(triggers={"architecture"})

    def test_checked_in_trigger_is_required_for_flagship(self):
        request = policy.POLICIES["review"].request(triggers={"architecture"})
        self.assertEqual(Tier.FLAGSHIP, request.minimum_tier)
        with self.assertRaisesRegex(ValueError, "unsupported escalation"):
            policy.POLICIES["review"].request(triggers={"provider-failed"})

    def test_failure_route_cannot_climb_tier(self):
        request = policy.POLICIES["review"].request()
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
        # anthropic-economy and anthropic-balanced were filled in with real,
        # verified model_ids on 2026-09-20 (Haiku, Sonnet) and no longer go
        # through this env-var-configured path; codex-spark remains the
        # checked-in-but-unverified placeholder this test exercises.
        class Health:
            def __call__(self, provider, model):
                return {"state": "healthy" if model == "verified-spark" else "unknown"}
        registry = policy.resolved_registry(
            {"FACTORY_CAPACITY_OPENAI_SPARK_MODEL": "verified-spark"},
            health=Health())
        spark = next(item for item in registry if item.name == "verified-spark")
        self.assertTrue(spark.available)


if __name__ == "__main__":
    unittest.main()
