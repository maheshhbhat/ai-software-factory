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


class HarnessHygieneTests(unittest.TestCase):
    """#355 — the first two live runs failed on the harness, not the factory:
    a fixture the dispatcher refused as malformed, and a poller.log growing
    inside the tree the worker's scope check was watching."""

    def test_the_fixture_declares_no_dependencies_in_the_dispatcher_dialect(self):
        sys.path.insert(0, str(ROOT / "factory" / "dispatcher"))
        import dispatcher
        refs, error = dispatcher.parse_depends_on(
            ph.fixture_body("r", 322, "require"))
        self.assertEqual(([], None), (refs, error),
                         "#354 sat unclaimable behind DEPENDS_ON_MALFORMED")

    def test_the_runtime_workspace_is_outside_the_repository(self):
        import shutil
        workspace = ph.runtime_workspace("hygiene-test")
        try:
            self.assertNotIn(ROOT, workspace.parents,
                             "run 20260823T130637Z: the harness's own log, "
                             "growing in-tree, read as an out-of-scope change "
                             "and the delivery was correctly refused")
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_evidence_enters_the_repository_only_after_teardown(self):
        source = (HERE / "phase4_real.py").read_text()
        copy_at = source.index("shutil.copytree(run.directory, run.final_directory)")
        teardown_at = source.index("run.teardown()")
        self.assertGreater(copy_at, teardown_at,
                           "the copy into runs/phase4-real must follow teardown")



def fail_ledger(**overrides):
    """A fail-path run that refused, bounded, and poisoned."""
    base = {
        "transitions": ["story:ready", "story:claimed", "story:ready",
                        "story:claimed", "story:ready", "story:claimed"],
        "outcomes": [],
        "pr_merged": False,
        "merged_head": "",
        "story_closed": False,
        "story_labels": ["story:blocked:poison", "type:story"],
        "attempt_value": 3,
        "aborted": None,
    }
    base.update(overrides)
    return base


class FailScenarioTests(unittest.TestCase):
    """#362 — the fail path: the rails must refuse, stop after the budget,
    and never let out-of-scope work merge."""

    def test_the_fail_fixture_orders_work_outside_its_own_scope(self):
        body = ph.fixture_fail_body("RUN", 322)
        scope = body.split("### Scope")[1].split("###")[0]
        self.assertIn("runs/phase4-real/RUN/product/**", scope)
        self.assertIn("escape/proof.txt", body)
        self.assertNotIn("escape", scope)

    def test_the_fail_fixture_parses_clean_for_the_dispatcher(self):
        sys.path.insert(0, str(ROOT / "factory" / "dispatcher"))
        import dispatcher
        self.assertEqual(([], None), dispatcher.parse_depends_on(
            ph.fixture_fail_body("r", 322)))

    def test_refused_bounded_poisoned_passes(self):
        passed, detail = ph.verdict_fail(fail_ledger())
        self.assertTrue(passed)
        self.assertEqual(3, detail["attempts"])

    def test_a_merge_is_a_boundary_breach_not_a_mere_failure(self):
        passed, detail = ph.verdict_fail(fail_ledger(pr_merged=True))
        self.assertFalse(passed)
        self.assertTrue(detail.get("breach"))
        self.assertIn("BOUNDARY BREACH", detail["reason"])

    def test_no_poison_means_the_factory_did_not_stop_properly(self):
        passed, detail = ph.verdict_fail(
            fail_ledger(story_labels=["type:story", "story:claimed"]))
        self.assertFalse(passed)
        self.assertIn("never poisoned", detail["reason"])

    def test_an_early_poison_is_named_not_excused(self):
        passed, detail = ph.verdict_fail(fail_ledger(attempt_value=1))
        self.assertFalse(passed)
        self.assertIn("attempt 1", detail["reason"])



if __name__ == "__main__":
    unittest.main()
