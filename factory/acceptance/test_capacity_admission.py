#!/usr/bin/env python3
import unittest

from factory.capacity_pool import admission
from factory.capacity_pool.router import ModelCapacity, RouteRequest, Tier
from factory.capacity_pool.state import CapacityState


class CapacityAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.state = CapacityState()
        self.request = RouteRequest(
            "delivery", frozenset({"code"}), Tier.BALANCED, "medium", 60, 3)
        self.models = (
            ModelCapacity("terra", "openai", Tier.BALANCED,
                          frozenset({"code"})),
            ModelCapacity("sonnet", "anthropic", Tier.BALANCED,
                          frozenset({"code"})),
        )

    def tearDown(self):
        self.state.close()

    def test_zero_capacity_is_not_admitted(self):
        self.assertIsNone(admission.reserve(
            task_key="delivery:o/r:20:1", request=self.request,
            registry=self.models, state=self.state))

    def test_capacity_pool_hides_route_behind_single_use_identity(self):
        self.state.mark_healthy("openai", "terra")
        value = admission.reserve(
            task_key="delivery:o/r:20:1", request=self.request,
            registry=self.models, state=self.state)
        self.assertIsNotNone(value)
        self.assertEqual({"reservation_id"}, set(value.__dict__))
        lease = self.state.consume(
            value.reservation_id, task_key="delivery:o/r:20:1")
        self.assertEqual(("openai", "terra"), (lease.provider, lease.model))

    def test_unhealthy_primary_uses_healthy_peer_without_exposing_it(self):
        self.state.mark_healthy("anthropic", "sonnet")
        value = admission.reserve(
            task_key="delivery:o/r:20:1", request=self.request,
            registry=self.models, state=self.state)
        self.assertIsNotNone(value)
        lease = self.state.consume(
            value.reservation_id, task_key="delivery:o/r:20:1")
        self.assertEqual("anthropic", lease.provider)

    def test_repeated_admission_replays_the_same_active_reservation(self):
        self.state.mark_healthy("openai", "terra")
        first = admission.reserve(task_key="delivery:o/r:20:1",
                                  request=self.request, registry=self.models,
                                  state=self.state)
        second = admission.reserve(task_key="delivery:o/r:20:1",
                                   request=self.request, registry=self.models,
                                   state=self.state)
        self.assertEqual(first, second)

    def test_preclaim_and_claimed_story_compute_the_same_logical_task(self):
        body = "### Attempt\n\n0\n\n### Spend cap\n\n$5 / 60 min\n"
        preclaim = {"number": 20, "body": body}
        claimed = {"number": 20, "body": body.replace("\n0\n", "\n1\n")}
        self.assertEqual(
            admission.delivery_task_key("Owner/Repo", preclaim),
            admission.delivery_task_key(
                "owner/repo", claimed, next_attempt=False))

    def test_delivery_request_carries_prior_story_models(self):
        story = {
            "number": 20,
            "body": "### Attempt\n\n1\n\n### Spend cap\n\n$5 / 60 min\n",
            "labels": [],
        }
        request = admission.delivery_request(story, prior_models=("terra",))
        self.assertEqual(("terra",), request.prior_models)
        self.assertEqual("delivery:owner/repo:20:",
                         admission.delivery_task_prefix("Owner/Repo", story))

    def test_prior_story_model_is_not_selected_for_the_next_attempt(self):
        self.state.mark_healthy("openai", "terra")
        self.state.mark_healthy("anthropic", "sonnet")
        request = RouteRequest(
            "delivery", frozenset({"code"}), Tier.BALANCED, "medium", 60, 3,
            prior_models=("terra",))

        value = admission.reserve(
            task_key="delivery:o/r:20:2", request=request,
            registry=self.models, state=self.state)
        lease = self.state.consume(
            value.reservation_id, task_key="delivery:o/r:20:2")

        self.assertEqual("sonnet", lease.model)


if __name__ == "__main__":
    unittest.main()
