#!/usr/bin/env python3
"""Tests for the §9.15 replay acceptance test.

Run: python3 -m unittest discover -s factory/dispatcher -p 'test_*.py' -v

Two kinds of test live here and they answer different questions. The first kind
uses small hand-written timelines to pin the reconstruction rules — what counts
as a transition, what counts as a transient, what an undeclared route does. The
second runs the real captured fixtures, because §9.15 is not a statement about
synthetic input: it names four specific issues whose recorded history is the
evidence, and a replay that passes only on fixtures written to make it pass
proves nothing.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import dispatcher as dp
import replay as rp


def labeled(name, at, index=0):
    return {"event": "labeled", "created_at": at, "label": {"name": name}, "_index": index}


def unlabeled(name, at, index=0):
    return {"event": "unlabeled", "created_at": at, "label": {"name": name}, "_index": index}


class TestTransitionReconstruction(unittest.TestCase):
    """A transition is a settled change of the single lifecycle label."""

    def walk(self, events, prefix=dp.STORY_LIFECYCLE):
        return rp.transitions(10, events, prefix)

    def test_an_atomic_replacement_is_one_transition(self):
        found, transients = self.walk([
            labeled("story:ready", "t1"),
            unlabeled("story:ready", "t2", 0), labeled("story:claimed", "t2", 1)])
        self.assertEqual([(t.frm, t.to) for t in found],
                         [(None, "story:ready"), ("story:ready", "story:claimed")])
        self.assertEqual(len(transients), 1)

    def test_the_order_within_one_instant_does_not_matter(self):
        """§9.2's write is one PATCH; GitHub reports its two label events in
        either order, and the recorded history contains both orders."""
        add_first, _ = self.walk([
            labeled("story:ready", "t1"),
            labeled("story:claimed", "t2", 0), unlabeled("story:ready", "t2", 1)])
        remove_first, _ = self.walk([
            labeled("story:ready", "t1"),
            unlabeled("story:ready", "t2", 0), labeled("story:claimed", "t2", 1)])
        self.assertEqual([(t.frm, t.to) for t in add_first],
                         [(t.frm, t.to) for t in remove_first])

    def test_a_zero_label_window_does_not_invent_two_transitions(self):
        """The #10 case: `story:claimed` removed at 20:24:22, `story:in-review`
        applied at 20:24:23. One second with no lifecycle label at all. Pairing
        by timestamp would read that as `claimed → nothing` and `nothing →
        in-review` — a state change the factory never made."""
        found, transients = self.walk([
            labeled("story:claimed", "t1"),
            unlabeled("story:claimed", "t2"),
            labeled("story:in-review", "t3")])
        self.assertEqual([(t.frm, t.to) for t in found],
                         [(None, "story:claimed"), ("story:claimed", "story:in-review")])
        self.assertEqual(transients[0][2], (), "the empty window is recorded as a finding")

    def test_a_two_label_window_is_a_transient_not_a_state(self):
        found, transients = self.walk([
            labeled("story:blocked", "t1"),
            labeled("story:ready", "t2"),
            unlabeled("story:blocked", "t3")])
        self.assertEqual([(t.frm, t.to) for t in found],
                         [(None, "story:blocked"), ("story:blocked", "story:ready")])
        self.assertIn(("story:blocked", "story:ready"),
                      [tuple(t[2]) for t in transients])

    def test_non_lifecycle_labels_are_ignored(self):
        found, transients = self.walk([
            labeled("type:story", "t1"), labeled("phase:build", "t1", 1),
            labeled("hazard", "t1", 2), labeled("story:ready", "t1", 3)])
        self.assertEqual([(t.frm, t.to) for t in found], [(None, "story:ready")])
        self.assertEqual(transients, [])

    def test_relabelling_the_same_state_emits_nothing(self):
        """A label edit ending on the same label emits no transition and cannot
        be routed — the reason §4.1 replaced both project self-loops."""
        found, _ = self.walk([
            labeled("story:ready", "t1"),
            unlabeled("story:ready", "t2", 0), labeled("story:ready", "t2", 1)])
        self.assertEqual([(t.frm, t.to) for t in found], [(None, "story:ready")])


class TestRouting(unittest.TestCase):
    def route(self, frm, to):
        return rp.route_for(rp.Transition(10, "t", frm, to))

    def test_a_live_route_is_routed_exactly_once(self):
        routing = self.route("story:ready", "story:claimed")
        self.assertTrue(routing.routed)
        self.assertEqual(routing.route.status, rp.LIVE)

    def test_a_deferred_route_is_surfaced_not_dropped(self):
        routing = self.route("project:ready-for-planning", "project:planning")
        self.assertFalse(routing.routed)
        self.assertEqual(routing.route.status, rp.DEFERRED)
        self.assertIn("Phase 3", routing.route.action)

    def test_an_undeclared_transition_is_a_failure_not_a_shrug(self):
        """§9.11: an unrouted transition is a contract gap, and silence hides it.
        A table that quietly ignores what it does not recognise is that silence."""
        report = rp.Report(routings=[self.route("story:ready", "story:invented")])
        self.assertEqual(len(report.unknown), 1)
        self.assertFalse(report.passed())

    def test_entering_a_human_queue_state_rings_a_bell(self):
        for frm, to in (("story:ready", "story:blocked:poison"),
                        ("story:ready", "story:blocked:scope"),
                        ("project:planning", "project:awaiting-ready"),
                        ("project:active", "project:awaiting-acceptance")):
            self.assertTrue(self.route(frm, to).rings_human_queue, to)

    def test_a_bell_is_independent_of_whether_the_route_is_live(self):
        """`planning → awaiting-ready` is deferred *and* rings a bell. Reading
        the two as one thing is how a bell gets lost."""
        routing = self.route("project:planning", "project:awaiting-ready")
        self.assertFalse(routing.routed)
        self.assertTrue(routing.rings_human_queue)

    def test_leaving_a_human_queue_state_does_not_ring_again(self):
        self.assertFalse(self.route("story:blocked:poison", "story:ready").rings_human_queue)

    def test_issue_creation_is_never_a_dispatchable_route(self):
        """§9.12 — creation authorizes nothing, however the issue is labelled."""
        for to in ("story:ready", "story:blocked", "project:queued"):
            routing = self.route(None, to)
            self.assertEqual(routing.route.status, rp.CREATION, to)
            self.assertFalse(routing.routed, to)

    def test_every_declared_route_names_a_status_the_report_can_render(self):
        for (frm, to), route in rp.ROUTES.items():
            self.assertIn(route.status, (rp.LIVE, rp.DEFERRED, rp.HUMAN, rp.CREATION),
                          f"{frm} -> {to}")
            self.assertTrue(route.action.strip(), f"{frm} -> {to} has no stated action")

    def test_the_table_uses_the_dispatchers_lifecycle_vocabulary(self):
        """A route naming a label the dispatcher does not know is a route nothing
        will ever match — and the drift would be silent."""
        known = {dp.READY, dp.CLAIMED, dp.IN_REVIEW, dp.MERGED, dp.COMPLETED,
                 dp.CANCELLED, dp.POISON, "story:blocked", "story:blocked:scope",
                 "project:queued", "project:ready-for-planning", "project:planning",
                 "project:awaiting-ready", dp.PROJECT_ACTIVE,
                 "project:awaiting-acceptance", "project:accepted"}
        for frm, to in rp.ROUTES:
            self.assertIn(to, known, f"unknown target {to}")
            if frm is not None:
                self.assertIn(frm, known, f"unknown source {frm}")


class TestExactlyOnce(unittest.TestCase):
    def test_a_transition_resolved_twice_fails(self):
        transition = rp.Transition(10, "t", "story:ready", "story:claimed")
        report = rp.Report(routings=[rp.route_for(transition), rp.route_for(transition)])
        self.assertEqual(len(report.duplicates()), 1)
        self.assertFalse(report.passed())

    def test_nothing_is_dropped_between_transitions_and_routings(self):
        records = rp.load_fixtures(rp.FIXTURE_DIR)
        report = rp.replay(records)
        counted = sum(
            len(rp.transitions(r["issue"], r["events"],
                               rp.lifecycle_prefix(r.get("type", "story")))[0])
            for r in records)
        self.assertEqual(counted, len(report.routings))


class TestTheRecordedHistory(unittest.TestCase):
    """§9.15 names four issues. This runs against what they actually recorded."""

    @classmethod
    def setUpClass(cls):
        cls.records = rp.load_fixtures(rp.FIXTURE_DIR)
        cls.report = rp.replay(cls.records)

    def test_the_fixtures_are_the_four_issues_the_contract_names(self):
        self.assertEqual(sorted(r["issue"] for r in self.records),
                         sorted(rp.DEFAULT_ISSUES))

    def test_the_replay_passes(self):
        self.assertTrue(self.report.passed(), rp.render(self.report))

    def test_every_transition_has_a_declared_route(self):
        self.assertEqual(self.report.unknown, [], rp.render(self.report))

    def test_no_transition_is_routed_twice(self):
        self.assertEqual(self.report.duplicates(), [])

    def test_the_exception_paths_are_exercised(self):
        """§9.15 chose this history because it is not the happy path: the retry
        sequence, the poison threshold, the rescue, and the scope detour."""
        pairs = {(r.transition.frm, r.transition.to) for r in self.report.routings}
        for pair in (("story:in-review", "story:ready"),          # retry on findings
                     ("story:ready", "story:blocked:poison"),     # threshold
                     ("story:blocked:poison", "story:ready"),     # rescue
                     ("story:ready", "story:blocked:scope"),      # scope detour
                     ("story:blocked:scope", "story:ready")):
            self.assertIn(pair, pairs, pair)

    def test_the_dispatch_route_fires_once_per_recorded_dispatch(self):
        """#11 was dispatched four times in the recorded walk — three attempts
        then the post-rescue one. Exactly-once means four, not three, not five."""
        dispatches = [r for r in self.report.routings
                      if r.transition.issue == 11
                      and (r.transition.frm, r.transition.to) == (dp.READY, dp.CLAIMED)]
        self.assertEqual(len(dispatches), 4)

    def test_restart_from_an_empty_cursor_decides_identically(self):
        again = rp.replay(rp.load_fixtures(rp.FIXTURE_DIR))
        self.assertEqual([r.transition for r in again.routings],
                         [r.transition for r in self.report.routings])
        self.assertEqual(rp.render(again), rp.render(self.report))

    def test_the_walk_recorded_the_non_atomic_writes_that_produced_9_2(self):
        """Not a decoration: §9.2 exists because the Phase 1 walk left windows
        with zero or two lifecycle labels, and the replay rediscovering them from
        the raw events is what makes it a replay rather than a re-assertion."""
        self.assertGreater(len(self.report.transients), 0)
        self.assertIn((), [tuple(t[2]) for t in self.report.transients],
                      "at least one window had no lifecycle label at all")

    def test_the_project_lifecycle_is_replayed_too(self):
        project_routings = [r for r in self.report.routings if r.transition.issue == 1]
        self.assertIn(("project:awaiting-acceptance", "project:accepted"),
                      [(r.transition.frm, r.transition.to) for r in project_routings])


class TestReadOnly(unittest.TestCase):
    """§9.15: the replay asserts decisions; it must not write to any issue."""

    def test_replaying_fixtures_touches_no_api(self):
        with patch.object(dp, "_api") as api:
            rp.replay(rp.load_fixtures(rp.FIXTURE_DIR))
        api.assert_not_called()

    def test_the_cli_touches_no_api(self):
        import contextlib, io
        with patch.object(dp, "_api") as api, contextlib.redirect_stdout(io.StringIO()):
            code = rp.main(["--fixtures", rp.FIXTURE_DIR])
        api.assert_not_called()
        self.assertEqual(code, 0)

    def test_capture_issues_only_reads(self):
        seen = []

        def recording_api(url, token, method="GET", payload=None):
            seen.append((method, url))
            if "/timeline" in url:
                return []
            return {"number": 10, "labels": [{"name": "type:story"}]}

        import tempfile
        with patch.object(dp, "_api", recording_api), tempfile.TemporaryDirectory() as tmp:
            rp.capture("o/r", [10], "token", tmp)
        self.assertTrue(seen)
        self.assertEqual({method for method, _ in seen}, {"GET"},
                         "capture must never issue a write")

    def test_the_module_declares_no_write_helper(self):
        source = open(rp.__file__, encoding="utf-8").read()
        for forbidden in ('method="PATCH"', 'method="POST"', 'method="PUT"',
                          'method="DELETE"'):
            self.assertNotIn(forbidden, source)


class TestFixtureIntegrity(unittest.TestCase):
    def test_fixtures_are_valid_json_with_the_expected_shape(self):
        for path in sorted(os.listdir(rp.FIXTURE_DIR)):
            if not path.startswith("issue-"):
                continue
            with open(os.path.join(rp.FIXTURE_DIR, path), encoding="utf-8") as handle:
                record = json.load(handle)
            self.assertIn(record["type"], ("story", "project"), path)
            self.assertTrue(record["events"], path)
            for event in record["events"]:
                self.assertIn(event["event"], ("labeled", "unlabeled"), path)
                self.assertTrue(event["created_at"], path)

    def test_a_missing_fixture_directory_is_a_clean_failure(self):
        with self.assertRaises(FileNotFoundError):
            rp.load_fixtures(os.path.join(rp.FIXTURE_DIR, "does-not-exist"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
