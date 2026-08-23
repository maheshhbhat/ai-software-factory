#!/usr/bin/env python3
"""Hermetic tests for the no-substitution Phase 4 harness.

The harness's live run is operator-invoked and costs real tokens; everything
judged *about* a run — the fixture body, the forbidden-environment preflight,
the verdict predicate — is pure and is pinned here, offline.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "factory" / "runtime"))
sys.path.insert(0, str(ROOT / "factory" / "agents" / "worker"))

import phase4_real as ph  # noqa: E402


class FixtureBodyTests(unittest.TestCase):
    def test_the_spend_cap_parses_under_both_parsers(self):
        import invoke
        import workers
        body = ph.fixture_body("20260823T000000Z", 325, "require")
        bounds = invoke.parse_bounds(body)
        self.assertEqual(3.0, bounds.max_usd)
        self.assertEqual(900, bounds.timeout)
        self.assertEqual(15 * 60 + workers.LAUNCH_GRACE_SECONDS,
                         workers.launch_timeout(body))

    def test_the_planted_defect_is_present_only_when_wanted(self):
        with_plant = ph.fixture_body("r", 325, "require")
        without = ph.fixture_body("r", 325, "skip")
        self.assertIn("Attempt-sensitive requirement", with_plant)
        self.assertIn("`defective`", with_plant)
        self.assertNotIn("Attempt-sensitive requirement", without)

    def test_the_scope_is_confined_to_the_run_directory(self):
        body = ph.fixture_body("RUN-ID", 325, "require")
        section = body.split("### Scope")[1].split("###")[0]
        self.assertIn("runs/phase4-real/RUN-ID/product/**", section)
        self.assertNotIn("factory/", section)

    def test_each_run_gets_a_fresh_marker_bearing_story(self):
        self.assertIn(ph.MARKER, ph.fixture_body("r", 325, "require"))


class ForbiddenEnvironmentTests(unittest.TestCase):
    def test_substitution_overrides_are_named(self):
        environ = {"FACTORY_DELIVERY_MODEL_CMD": "python3 stub.py",
                   "FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH": "stub",
                   "PATH": "/usr/bin"}
        found = ph.forbidden_overrides(environ)
        self.assertIn("FACTORY_DELIVERY_MODEL_CMD", found)
        self.assertIn("FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH", found)
        self.assertNotIn("PATH", found)

    def test_a_clean_environment_passes(self):
        self.assertEqual([], ph.forbidden_overrides({"PATH": "/usr/bin",
                                                     "HOME": "/tmp"}))

    def test_empty_values_do_not_count_as_overrides(self):
        self.assertEqual([], ph.forbidden_overrides(
            {"FACTORY_DELIVERY_MODEL_CMD": ""}))


def ledger(**overrides):
    """A run that exercised the full retry walk and merged."""
    base = {
        "transitions": ["story:ready", "story:claimed", "story:in-review",
                        "story:ready", "story:claimed", "story:in-review",
                        "story:merged"],
        "outcomes": [{"pull": 9, "head": "a" * 40, "verdict": "findings"},
                     {"pull": 9, "head": "b" * 40, "verdict": "approval"}],
        "pr_merged": True,
        "merged_head": "b" * 40,
        "story_closed": True,
        "aborted": None,
    }
    base.update(overrides)
    return base


class VerdictTests(unittest.TestCase):
    def test_the_full_walk_passes_in_require_mode(self):
        passed, detail = ph.verdict(ledger(), "require")
        self.assertTrue(passed)
        self.assertEqual("exercised", detail["findings_leg"])

    def test_an_abort_fails_with_its_reason(self):
        passed, detail = ph.verdict(ledger(aborted="poller exited 1"), "require")
        self.assertFalse(passed)
        self.assertIn("poller exited 1", detail["reason"])

    def test_an_unmerged_pr_fails(self):
        passed, detail = ph.verdict(ledger(pr_merged=False), "require")
        self.assertFalse(passed)
        self.assertIn("did not merge", detail["reason"])

    def test_no_exact_head_approval_on_the_merged_head_fails(self):
        stale = [{"pull": 9, "head": "a" * 40, "verdict": "findings"},
                 {"pull": 9, "head": "c" * 40, "verdict": "approval"}]
        passed, detail = ph.verdict(ledger(outcomes=stale), "require")
        self.assertFalse(passed)
        self.assertIn("exact-head approval", detail["reason"])

    def test_a_first_pass_approval_fails_require_mode_by_name(self):
        """The reviewer excusing the planted defect is itself a finding."""
        smooth = ledger(
            transitions=["story:ready", "story:claimed", "story:in-review",
                         "story:merged"],
            outcomes=[{"pull": 9, "head": "b" * 40, "verdict": "approval"}])
        passed, detail = ph.verdict(smooth, "require")
        self.assertFalse(passed)
        self.assertEqual("not-exercised", detail["findings_leg"])

    def test_allow_mode_passes_the_same_run_but_names_the_gap(self):
        smooth = ledger(
            transitions=["story:ready", "story:claimed", "story:in-review",
                         "story:merged"],
            outcomes=[{"pull": 9, "head": "b" * 40, "verdict": "approval"}])
        passed, detail = ph.verdict(smooth, "allow")
        self.assertTrue(passed)
        self.assertEqual("not-exercised", detail["findings_leg"])

    def test_findings_without_the_label_walk_are_not_a_retry(self):
        """Markers alone do not prove the story travelled the retry road."""
        no_walk = ledger(
            transitions=["story:ready", "story:claimed", "story:in-review",
                         "story:merged"])
        passed, detail = ph.verdict(no_walk, "require")
        self.assertFalse(passed)
        self.assertEqual("not-exercised", detail["findings_leg"])

    def test_skip_mode_asks_only_for_the_delivery_leg(self):
        smooth = ledger(
            transitions=["story:ready", "story:claimed", "story:in-review",
                         "story:merged"],
            outcomes=[{"pull": 9, "head": "b" * 40, "verdict": "approval"}])
        passed, detail = ph.verdict(smooth, "skip")
        self.assertTrue(passed)
        self.assertEqual("skipped", detail["findings_leg"])


class HarnessDisciplineTests(unittest.TestCase):
    """The harness must stay runnable only on purpose."""

    def test_import_has_no_side_effects_and_discover_never_runs_it(self):
        source = (HERE / "phase4_real.py").read_text()
        self.assertIn('if __name__ == "__main__":', source)
        self.assertNotIn("class TestPhase4Real", source,
                         "the harness must not be collectable as a test")

    def test_the_docstring_states_the_three_prohibitions(self):
        doc = ph.__doc__ or ""
        for promise in ("never CI", "scheduled", "required check"):
            self.assertIn(promise, doc)


if __name__ == "__main__":
    unittest.main()
