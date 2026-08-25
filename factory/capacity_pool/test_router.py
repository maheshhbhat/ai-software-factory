#!/usr/bin/env python3
import unittest

from router import ModelCapacity, RouteRequest, Tier, remaining_envelope, route


CAPS_CODE = frozenset({"code", "write", "tests"})
CAPS_REASON = frozenset({"reason", "json"})


def model(name, provider, tier, caps, **kwargs):
    return ModelCapacity(name, provider, tier, frozenset(caps), **kwargs)


class CapacityPoolTests(unittest.TestCase):
    def test_underused_expiring_spark_wins_bounded_coding(self):
        registry = [
            model("spark", "openai", Tier.BALANCED, CAPS_CODE,
                  prepaid_or_expiring=True, capacity_remaining=.90, latency_rank=1),
            model("terra", "openai", Tier.BALANCED, CAPS_CODE,
                  capacity_remaining=.80, latency_rank=3),
            model("sonnet", "anthropic", Tier.BALANCED, CAPS_CODE,
                  capacity_remaining=.80, latency_rank=4),
        ]
        request = RouteRequest("coding", CAPS_CODE, Tier.BALANCED, "low", 1200, 10)
        plan = route(request, registry)
        self.assertEqual("spark", plan.primary.model)
        self.assertEqual("anthropic", plan.steps[1].provider)

    def test_exhausted_spark_is_not_selected(self):
        registry = [
            model("spark", "openai", Tier.BALANCED, CAPS_CODE,
                  prepaid_or_expiring=True, capacity_remaining=0),
            model("sonnet", "anthropic", Tier.BALANCED, CAPS_CODE),
        ]
        request = RouteRequest("coding", CAPS_CODE, Tier.BALANCED, "low", 600, 5)
        self.assertEqual("sonnet", route(request, registry).primary.model)

    def test_planning_does_not_use_coding_only_spark(self):
        registry = [
            model("spark", "openai", Tier.BALANCED, CAPS_CODE,
                  prepaid_or_expiring=True),
            model("terra", "openai", Tier.BALANCED, CAPS_REASON),
            model("sonnet", "anthropic", Tier.BALANCED, CAPS_REASON),
        ]
        request = RouteRequest("planning", CAPS_REASON, Tier.BALANCED, "medium", 900, 5)
        self.assertNotEqual("spark", route(request, registry).primary.model)

    def test_high_risk_request_requires_flagship(self):
        registry = [
            model("terra", "openai", Tier.BALANCED, CAPS_REASON),
            model("sol", "openai", Tier.FLAGSHIP, CAPS_REASON),
            model("opus", "anthropic", Tier.FLAGSHIP, CAPS_REASON),
        ]
        request = RouteRequest("architecture", CAPS_REASON, Tier.FLAGSHIP, "high", 1200, 15)
        self.assertIn(route(request, registry).primary.model, {"sol", "opus"})

    def test_experimental_model_is_opt_in(self):
        registry = [
            model("muse", "other", Tier.BALANCED, CAPS_CODE,
                  experimental=True, prepaid_or_expiring=True),
            model("terra", "openai", Tier.BALANCED, CAPS_CODE),
        ]
        request = RouteRequest("coding", CAPS_CODE, Tier.BALANCED, "low", 600, 5)
        self.assertEqual("terra", route(request, registry).primary.model)
        allowed = RouteRequest("coding", CAPS_CODE, Tier.BALANCED, "low", 600, 5,
                               allow_experimental=True)
        self.assertEqual("muse", route(allowed, registry).primary.model)

    def test_explicit_override_has_no_silent_fallback(self):
        registry = [
            model("terra", "openai", Tier.BALANCED, CAPS_REASON),
            model("sonnet", "anthropic", Tier.BALANCED, CAPS_REASON),
        ]
        request = RouteRequest("planning", CAPS_REASON, Tier.BALANCED, "medium", 600, 5,
                               explicit_model="sonnet")
        plan = route(request, registry)
        self.assertEqual(["sonnet"], [step.model for step in plan.steps])

    def test_provider_diversity_in_fallback_chain(self):
        registry = [
            model("spark", "openai", Tier.BALANCED, CAPS_CODE,
                  prepaid_or_expiring=True, capacity_remaining=.9),
            model("terra", "openai", Tier.BALANCED, CAPS_CODE, capacity_remaining=.8),
            model("sonnet", "anthropic", Tier.BALANCED, CAPS_CODE, capacity_remaining=.7),
        ]
        request = RouteRequest("coding", CAPS_CODE, Tier.BALANCED, "low", 600, 5)
        plan = route(request, registry)
        self.assertEqual("openai", plan.steps[0].provider)
        self.assertEqual("anthropic", plan.steps[1].provider)

    def test_prior_model_is_deprioritized(self):
        registry = [
            model("terra", "openai", Tier.BALANCED, CAPS_REASON),
            model("sonnet", "anthropic", Tier.BALANCED, CAPS_REASON),
        ]
        request = RouteRequest("planning", CAPS_REASON, Tier.BALANCED, "medium", 600, 5,
                               prior_models=("terra",))
        self.assertEqual("sonnet", route(request, registry).primary.model)

    def test_combined_envelope_never_resets(self):
        registry = [model("terra", "openai", Tier.BALANCED, CAPS_REASON)]
        request = RouteRequest("planning", CAPS_REASON, Tier.BALANCED, "medium", 900, 5)
        plan = route(request, registry)
        self.assertEqual((360, 2.0), remaining_envelope(
            plan, elapsed_seconds=540, consumed_budget_units=3.0))
        self.assertEqual((0, 0.0), remaining_envelope(
            plan, elapsed_seconds=1200, consumed_budget_units=20))

    def test_malformed_output_is_stop_condition_not_fallback_trigger(self):
        registry = [model("terra", "openai", Tier.BALANCED, CAPS_REASON)]
        request = RouteRequest("planning", CAPS_REASON, Tier.BALANCED, "medium", 600, 5)
        plan = route(request, registry)
        self.assertIn("malformed-output", plan.stop_on)
        self.assertNotIn("malformed-output", plan.fallback_on)

    def test_no_eligible_capacity_fails_clearly(self):
        registry = [model("luna", "openai", Tier.ECONOMY, {"classify"})]
        request = RouteRequest("architecture", CAPS_REASON, Tier.FLAGSHIP, "high", 600, 5)
        with self.assertRaisesRegex(LookupError, "no eligible model capacity"):
            route(request, registry)


if __name__ == "__main__":
    unittest.main()
