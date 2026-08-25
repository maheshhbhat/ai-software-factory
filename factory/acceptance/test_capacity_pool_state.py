#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path

from factory.capacity_pool.state import CapacityState


class Clock:
    def __init__(self): self.value = 1000.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class CapacityStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.state = CapacityState(Path(self.tmp.name) / "state.sqlite", uri=False,
                                   clock=self.clock)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_cooldown_requires_probe_and_probe_success_for_health(self):
        value = self.state.mark_failure("openai", "terra", "rate-limited",
                                        retry_after=20)
        self.assertEqual("cooldown", value["state"])
        with self.assertRaisesRegex(RuntimeError, "not eligible"):
            self.state.begin_probe("openai", "terra")
        self.clock.advance(20)
        self.assertEqual("probe", self.state.begin_probe("openai", "terra")["state"])
        self.assertEqual("healthy", self.state.finish_probe(
            "openai", "terra", True)["state"])

    def test_failed_probe_reenters_cooldown_with_bounded_backoff(self):
        self.state.mark_failure("openai", "terra", "unavailable", retry_after=1)
        self.clock.advance(1)
        self.state.begin_probe("openai", "terra")
        value = self.state.finish_probe("openai", "terra", False,
                                        base_cooldown=10, maximum_cooldown=12)
        self.assertEqual("cooldown", value["state"])
        self.assertLessEqual(value["cooldown_until"] - self.clock(), 12)

    def test_duplicate_and_oversubscribed_leases_fail_atomically(self):
        self.state.mark_healthy("openai", "terra")
        self.state.reserve("task-1", "openai", "terra", 3, ttl_seconds=30,
                           capacity_limit=5)
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.state.reserve("task-1", "openai", "terra", 1, ttl_seconds=30)
        with self.assertRaisesRegex(RuntimeError, "oversubscribe"):
            self.state.reserve("task-2", "openai", "terra", 3, ttl_seconds=30,
                               capacity_limit=5)

    def test_separate_connections_share_atomic_reservations(self):
        self.state.mark_healthy("openai", "terra")
        peer = CapacityState(Path(self.tmp.name) / "state.sqlite", uri=False,
                             clock=self.clock)
        try:
            self.state.reserve("task-1", "openai", "terra", 3, ttl_seconds=30,
                               capacity_limit=5)
            with self.assertRaisesRegex(RuntimeError, "oversubscribe"):
                peer.reserve("task-2", "openai", "terra", 3, ttl_seconds=30,
                             capacity_limit=5)
        finally:
            peer.close()

    def test_provider_scope_failure_blocks_other_model(self):
        self.state.mark_healthy("openai", "terra")
        self.state.mark_healthy("openai", "*")
        self.state.mark_failure("openai", "*", "unavailable")
        with self.assertRaisesRegex(RuntimeError, "provider capacity"):
            self.state.reserve("task", "openai", "terra", 1, ttl_seconds=5)

    def test_repeated_contract_failures_degrade_until_probe_recovery(self):
        self.state.mark_healthy("openai", "terra")
        self.assertEqual("healthy", self.state.mark_quality_failure(
            "openai", "terra", "schema-invalid", threshold=2)["state"])
        self.assertEqual("degraded", self.state.mark_quality_failure(
            "openai", "terra", "schema-invalid", threshold=2)["state"])
        with self.assertRaisesRegex(RuntimeError, "not healthy"):
            self.state.reserve("task", "openai", "terra", 1, ttl_seconds=5)

    def test_expired_lease_releases_duplicate_key_but_not_ambiguous_write(self):
        self.state.mark_healthy("openai", "terra")
        lease = self.state.reserve("task-1", "openai", "terra", 1, ttl_seconds=2)
        self.clock.advance(2)
        replacement = self.state.reserve("task-1", "openai", "terra", 1,
                                         ttl_seconds=2)
        self.assertNotEqual(lease.lease_id, replacement.lease_id)

    def test_stale_health_observation_fails_closed(self):
        self.state.mark_healthy("openai", "terra", "probe-success")
        self.clock.advance(31)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.state.reserve("task", "openai", "terra", 1, ttl_seconds=5,
                               max_health_age_seconds=30)


if __name__ == "__main__":
    unittest.main()
