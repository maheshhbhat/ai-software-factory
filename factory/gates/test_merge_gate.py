#!/usr/bin/env python3
"""Tests for the deterministic merge gate. Standard library only.

Run: python3 -m unittest discover -s factory/gates -p 'test_*.py' -v
"""

from __future__ import annotations

import unittest
from unittest import mock

import merge_gate as mg
from merge_gate import Violation as V


def story(body: str, labels=("type:story",)) -> dict:
    return {"body": body, "labels": [{"name": name} for name in labels]}


def scope_body(*patterns: str) -> str:
    return "### Spec\n\nthing\n\n### Scope\n\n" + "\n".join(patterns) + "\n\n### Attempt\n\n0\n"


VALID_PR = "Does a thing.\n\nStory: #42\n\nAgent-ID: claude-delivery\n"


class TestGlobSemantics(unittest.TestCase):
    """§9.6: `*` does not cross `/`; `**` spans segments."""

    def test_star_does_not_cross_slash(self):
        self.assertTrue(mg.match_path("src/*.py", "src/a.py"))
        self.assertFalse(mg.match_path("src/*.py", "src/nested/a.py"))
        self.assertFalse(mg.match_path("src/*", "src/nested/a.py"))

    def test_doublestar_spans_segments(self):
        self.assertTrue(mg.match_path("src/**", "src/a.py"))
        self.assertTrue(mg.match_path("src/**", "src/deep/nested/a.py"))

    def test_doublestar_matches_zero_segments(self):
        self.assertTrue(mg.match_path("src/**", "src"))
        self.assertTrue(mg.match_path("**/a.py", "a.py"))
        self.assertTrue(mg.match_path("**/a.py", "x/y/a.py"))

    def test_question_matches_single_char_not_slash(self):
        self.assertTrue(mg.match_path("src/?.py", "src/a.py"))
        self.assertFalse(mg.match_path("src/?.py", "src/ab.py"))
        self.assertFalse(mg.match_path("a?c", "a/c"))

    def test_literal_pattern_matches_exactly_one_path(self):
        self.assertTrue(mg.match_path("README.md", "README.md"))
        self.assertFalse(mg.match_path("README.md", "docs/README.md"))
        self.assertFalse(mg.match_path("README.md", "README.md.bak"))

    def test_case_sensitive(self):
        self.assertFalse(mg.match_path("src/**", "SRC/a.py"))

    def test_prefix_is_not_enough(self):
        self.assertFalse(mg.match_path("src/**", "srcfoo/a.py"))


class TestStoryLink(unittest.TestCase):
    """§9.5"""

    def test_exactly_one_link(self):
        self.assertEqual(mg.parse_story_link(VALID_PR), (42, None))

    def test_missing(self):
        self.assertEqual(mg.parse_story_link("no link here")[1], V.LINK_MISSING)
        self.assertEqual(mg.parse_story_link("")[1], V.LINK_MISSING)

    def test_duplicate(self):
        self.assertEqual(mg.parse_story_link("Story: #1\nStory: #2")[1], V.LINK_DUPLICATE)

    def test_malformed_is_not_a_link(self):
        for text in ("Story #42", "story: 42", "Story: 42", "Story: #ab"):
            self.assertEqual(mg.parse_story_link(text)[1], V.LINK_MISSING, text)

    def test_must_be_own_line(self):
        self.assertEqual(mg.parse_story_link("see Story: #42 above")[1], V.LINK_MISSING)


class TestScopeParsing(unittest.TestCase):
    """§9.6 fail-closed parsing."""

    def test_valid(self):
        patterns, err = mg.parse_scope(scope_body("src/**", "tests/**"))
        self.assertIsNone(err)
        self.assertEqual(patterns, ["src/**", "tests/**"])

    def test_missing_section(self):
        self.assertEqual(mg.parse_scope("### Spec\n\nx\n")[1], V.SCOPE_MISSING)

    def test_bulleted_scope_is_a_legacy_artifact(self):
        self.assertEqual(mg.parse_scope(scope_body("- src/**"))[1], V.SCHEMA_LEGACY_ARTIFACT)

    def test_no_response_is_empty(self):
        self.assertEqual(mg.parse_scope(scope_body("_No response_"))[1], V.SCOPE_EMPTY)

    def test_blank_scope_is_empty(self):
        self.assertEqual(mg.parse_scope("### Scope\n\n\n\n### Attempt\n\n0\n")[1], V.SCOPE_EMPTY)

    def test_indentation_is_malformed(self):
        self.assertEqual(mg.parse_scope(scope_body("  src/**"))[1], V.SCOPE_MALFORMED)

    def test_unfrozen_syntax_is_malformed(self):
        for pattern in ("src/{a,b}/**", "src/[ab].py", "!src/**"):
            self.assertEqual(mg.parse_scope(scope_body(pattern))[1], V.SCOPE_MALFORMED, pattern)

    def test_partial_doublestar_segment_is_malformed(self):
        self.assertEqual(mg.parse_scope(scope_body("src/a**b/x"))[1], V.SCOPE_MALFORMED)


class TestSchemaVersion(unittest.TestCase):
    """§9.1"""

    def test_absent_means_current(self):
        self.assertIsNone(mg.parse_schema_version("nothing here"))

    def test_parsed(self):
        self.assertEqual(mg.parse_schema_version("Schema-Version: 2.0.0"), 2)
        self.assertEqual(mg.parse_schema_version("Schema-Version: 3.1.4"), 3)


class TestSurfaceClassification(unittest.TestCase):
    """#39: the enforcement surface splits into a protectable half and a
    non-protectable half, and neither blocks the merge-gate verdict."""

    def test_ordinary_paths_are_clean(self):
        verdict, runner, logic = mg.classify_surface(["src/a.py", "README.md"])
        self.assertEqual((verdict, runner, logic), ("clean", [], []))

    def test_gate_logic_is_advisory(self):
        for path in ("factory/gates/merge_gate.py", "factory/gates/nested/thing.py"):
            verdict, runner, logic = mg.classify_surface([path])
            self.assertEqual(verdict, "logic", path)
            self.assertEqual(logic, [path])
            self.assertEqual(runner, [])

    def test_runner_change_is_loud(self):
        verdict, runner, _ = mg.classify_surface([".github/workflows/merge-gate.yml"])
        self.assertEqual(verdict, "runner")
        self.assertEqual(runner, [".github/workflows/merge-gate.yml"])

    def test_runner_outranks_logic(self):
        verdict, runner, logic = mg.classify_surface(
            ["factory/gates/merge_gate.py", ".github/workflows/merge-gate.yml"])
        self.assertEqual(verdict, "runner")
        self.assertEqual(logic, ["factory/gates/merge_gate.py"])

    def test_other_workflows_are_not_the_runner(self):
        verdict, _, _ = mg.classify_surface([".github/workflows/other.yml"])
        self.assertEqual(verdict, "clean")

    def test_factory_scope_classifier_fails_closed_on_protected_patterns(self):
        protected = (
            "factory/agents/**", "f*/**", "**", "*.sh", "poll.sh",
            "approve-plan.sh", "live-e2e.sh", ".github/**", ".claude/**",
            "AGENTS.md", "CLAUDE.md",
        )
        for pattern in protected:
            with self.subTest(pattern=pattern):
                self.assertEqual([pattern], mg.protected_factory_scope([pattern]))

    def test_product_and_uat_scopes_are_not_factory_scopes(self):
        patterns = ["src/**", "tests/**", "runs/rung1/live_product/**"]
        self.assertEqual([], mg.protected_factory_scope(patterns))


class TestVerdict(unittest.TestCase):
    """End-to-end evaluation. Each violation class must fail on its own."""

    def evaluate(self, **kwargs):
        base = dict(pr_body=VALID_PR, changed_paths=["src/a.py"],
                    story=story(scope_body("src/**")), tests_passed=True)
        base.update(kwargs)
        return mg.evaluate(**base)

    def test_valid_pr_passes(self):
        verdict = self.evaluate()
        self.assertTrue(verdict.passed, verdict.codes())

    def test_missing_link_fails(self):
        self.assertIn(V.LINK_MISSING, self.evaluate(pr_body="no link").codes())

    def test_duplicate_link_fails(self):
        body = "Story: #1\nStory: #2\n"
        self.assertIn(V.LINK_DUPLICATE, self.evaluate(pr_body=body).codes())

    def test_story_not_found_fails(self):
        self.assertIn(V.STORY_NOT_FOUND, self.evaluate(story=None).codes())

    def test_wrong_type_fails(self):
        wrong = story(scope_body("src/**"), labels=("type:project",))
        self.assertIn(V.STORY_WRONG_TYPE, self.evaluate(story=wrong).codes())

    def test_out_of_scope_fails(self):
        verdict = self.evaluate(changed_paths=["src/a.py", "secrets/key.pem"])
        self.assertIn(V.OUT_OF_SCOPE, verdict.codes())
        self.assertIn("secrets/key.pem", verdict.findings[0].detail)

    def test_empty_scope_can_never_pass(self):
        empty = story("### Scope\n\n_No response_\n\n### Attempt\n\n0\n")
        self.assertIn(V.SCOPE_EMPTY, self.evaluate(story=empty).codes())

    def test_legacy_scope_fails(self):
        legacy = story(scope_body("- synthetic/**"))
        self.assertIn(V.SCHEMA_LEGACY_ARTIFACT, self.evaluate(story=legacy).codes())

    def test_incompatible_schema_major_fails(self):
        body = "Schema-Version: 3.0.0\n\nStory: #42\n"
        self.assertIn(V.SCHEMA_INCOMPATIBLE, self.evaluate(pr_body=body).codes())

    def test_gate_logic_change_no_longer_blocks(self):
        """#39: the trusted main copy computes the verdict, so a gate-logic PR
        that is otherwise compliant must PASS. This is the deadlock removal."""
        story_covering_gate = story(scope_body("factory/gates/**"))
        verdict = self.evaluate(changed_paths=["factory/gates/merge_gate.py"],
                                story=story_covering_gate)
        self.assertTrue(verdict.passed, verdict.codes())

    def test_runner_change_no_longer_blocks_the_verdict(self):
        """Reported loudly by merge-gate-surface, but never as a merge-gate
        failure — blocking it would freeze the gate once the check is required."""
        story_covering_runner = story(scope_body(".github/workflows/**"))
        verdict = self.evaluate(changed_paths=[".github/workflows/merge-gate.yml"],
                                story=story_covering_runner)
        self.assertTrue(verdict.passed, verdict.codes())

    def test_worker_artifact_cannot_change_factory_control_plane(self):
        worker_body = VALID_PR + "\n<!-- worker-artifact:42:attempt -->\n"
        paths = (
            "factory/runtime/poller.py", "poll.sh", "approve-plan.sh",
            "live-e2e.sh", ".github/workflows/merge-gate.yml", "AGENTS.md",
            "CLAUDE.md", ".claude/skills/example/SKILL.md",
        )
        for path in paths:
            with self.subTest(path=path):
                verdict = self.evaluate(
                    pr_body=worker_body,
                    changed_paths=[path],
                    story=story(scope_body(path)),
                )
                self.assertIn(V.FACTORY_SELF_MODIFICATION_FORBIDDEN,
                              verdict.codes())

    def test_direct_implementation_pr_can_change_factory_control_plane(self):
        verdict = self.evaluate(
            changed_paths=["approve-plan.sh"],
            story=story(scope_body("approve-plan.sh")),
        )
        self.assertTrue(verdict.passed, verdict.codes())

    def test_failing_tests_fail(self):
        self.assertIn(V.TESTS_FAILED, self.evaluate(tests_passed=False).codes())

    def test_no_changes_fails(self):
        self.assertIn(V.NO_CHANGES, self.evaluate(changed_paths=[]).codes())

    def test_unreadable_story_fails_closed(self):
        verdict = self.evaluate(story=None, story_fetch_error="HTTP 500")
        self.assertIn(V.INPUT_UNAVAILABLE, verdict.codes())

    def test_violations_are_independent(self):
        """A red result names every cause, so one failure cannot mask another."""
        verdict = self.evaluate(changed_paths=["src/a.py", "elsewhere/b.py"],
                                tests_passed=False)
        self.assertIn(V.OUT_OF_SCOPE, verdict.codes())
        self.assertIn(V.TESTS_FAILED, verdict.codes())

    def test_rename_both_paths_must_match(self):
        verdict = self.evaluate(changed_paths=["src/a.py", "old/a.py"])
        self.assertIn(V.OUT_OF_SCOPE, verdict.codes())


class TestTrustBoundary(unittest.TestCase):
    """§9.14: forgeable inputs must not change the verdict."""

    def test_labels_and_agent_id_do_not_influence_verdict(self):
        forged = story(scope_body("src/**"),
                       labels=("type:story", "hazard", "agent:claude-delivery"))
        out_of_scope = ["src/a.py", "elsewhere/b.py"]
        with_labels = mg.evaluate(VALID_PR, out_of_scope, forged, True)
        plain = mg.evaluate(VALID_PR, out_of_scope,
                            story(scope_body("src/**")), True)
        self.assertEqual(with_labels.codes(), plain.codes())

    def test_pr_body_claims_do_not_grant_a_pass(self):
        boastful = ("Story: #42\n\nAgent-ID: claude-delivery\n\n"
                    "Reviewed-By: @maheshhbhat\nApproved: yes\nAll checks passed.\n")
        verdict = mg.evaluate(boastful, ["evil/backdoor.py"],
                              story(scope_body("src/**")), True)
        self.assertIn(V.OUT_OF_SCOPE, verdict.codes())


class TestFailClosed(unittest.TestCase):
    """A gate that cannot evaluate must fail readably, never pass."""

    def test_missing_fixture_is_a_clean_failure(self):
        import contextlib
        import io as _io

        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = mg.guarded_main(["--fixture", "/nonexistent/fixture.json"])
        output = buffer.getvalue()
        self.assertEqual(code, 1)
        self.assertIn(V.INTERNAL_ERROR, output)
        self.assertIn("fails closed", output)
        self.assertNotIn("PASS", output)


class TestTraceContinuity(unittest.TestCase):
    def test_gate_derives_the_attempt_trace_from_the_durable_claim(self):
        timeline=[{"event":"labeled","label":{"name":"story:claimed"},
                   "created_at":"2026-01-01T00:00:00Z"}]
        with mock.patch.object(mg,"_api",return_value=timeline):
            value=mg.story_attempt_trace("owner/repo",42,"token")
        self.assertEqual(mg.obs.trace_id("owner/repo",42,"2026-01-01T00:00:00Z"),value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
