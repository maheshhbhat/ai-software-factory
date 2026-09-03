#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path

from factory.capacity_pool.state import CapacityState, default_state_path


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

    def test_default_state_path_is_shared_by_primary_and_linked_worktrees(self):
        root = Path(self.tmp.name) / "primary"
        common = root / ".git"
        linked = Path(self.tmp.name) / "linked"
        worktree_git = common / "worktrees" / "linked"
        worktree_git.mkdir(parents=True)
        linked.mkdir()
        (linked / ".git").write_text(f"gitdir: {worktree_git}\n")
        (worktree_git / "commondir").write_text("../..\n")

        expected = common.resolve() / "factory" / "capacity-pool.sqlite"
        self.assertEqual(expected, default_state_path(root, {}))
        self.assertEqual(expected, default_state_path(linked, {}))

    def test_default_state_path_honors_override_and_non_git_fallback(self):
        root = Path(self.tmp.name) / "plain"
        root.mkdir()
        self.assertEqual(
            Path("/tmp/operator-capacity.sqlite"),
            default_state_path(
                root, {"FACTORY_CAPACITY_STATE": "/tmp/operator-capacity.sqlite"}))
        self.assertEqual(root / "runs" / "capacity-pool.sqlite",
                         default_state_path(root, {}))

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

    def test_models_for_task_prefix_preserves_retry_history(self):
        self.state.mark_healthy("anthropic", "fable")
        self.state.mark_healthy("openai", "sol")
        first = self.state.reserve(
            "delivery:owner/repo:107:1", "anthropic", "fable", 1,
            ttl_seconds=30)
        self.state.consume(first.lease_id, task_key="delivery:owner/repo:107:1")
        second = self.state.reserve(
            "delivery:owner/repo:107:2", "openai", "sol", 1,
            ttl_seconds=30)
        self.state.consume(second.lease_id, task_key="delivery:owner/repo:107:2")
        released = self.state.reserve(
            "delivery:owner/repo:107:3", "openai", "sol", 1,
            ttl_seconds=30)
        self.state.release(released.lease_id)

        self.assertEqual(
            ("fable",),
            self.state.models_for_task_prefix(
                "delivery:owner/repo:107:",
                exclude_task_key="delivery:owner/repo:107:2"))
        self.assertEqual(
            ("fable", "sol"),
            self.state.models_for_task_prefix("delivery:owner/repo:107:"))
        self.assertNotEqual(first.lease_id, second.lease_id)

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

    def test_reservation_is_single_use_task_bound_and_releasable_before_start(self):
        self.state.mark_healthy("openai", "terra")
        lease = self.state.reserve("delivery:o/r:20:1", "openai", "terra", 1,
                                   ttl_seconds=5)
        with self.assertRaisesRegex(RuntimeError, "task does not match"):
            self.state.consume(lease.lease_id, task_key="delivery:o/r:21:1")
        consumed = self.state.consume(
            lease.lease_id, task_key="delivery:o/r:20:1")
        self.assertEqual("terra", consumed.model)
        self.assertEqual("consumed", self.state.lease_status(lease.lease_id))
        with self.assertRaisesRegex(RuntimeError, "not active"):
            self.state.consume(lease.lease_id, task_key="delivery:o/r:20:1")
        self.assertFalse(self.state.release(lease.lease_id))

        releasable = self.state.reserve(
            "delivery:o/r:22:1", "openai", "terra", 1, ttl_seconds=5)
        self.assertTrue(self.state.release(releasable.lease_id))
        self.assertEqual("released", self.state.lease_status(releasable.lease_id))

    def test_expired_or_newly_unhealthy_reservation_cannot_be_consumed(self):
        self.state.mark_healthy("openai", "terra")
        expired = self.state.reserve("expired", "openai", "terra", 1,
                                     ttl_seconds=2)
        self.clock.advance(2)
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.state.consume(expired.lease_id, task_key="expired")
        self.assertEqual("expired", self.state.lease_status(expired.lease_id))

        self.state.mark_healthy("openai", "terra")
        unavailable = self.state.reserve("unavailable", "openai", "terra", 1,
                                         ttl_seconds=2)
        self.state.mark_failure("openai", "terra", "unavailable")
        with self.assertRaisesRegex(RuntimeError, "became unavailable"):
            self.state.consume(unavailable.lease_id, task_key="unavailable")
        self.assertTrue(self.state.release(unavailable.lease_id))


if __name__ == "__main__":
    unittest.main()
