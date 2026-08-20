#!/usr/bin/env python3
"""Tests for the deterministic merge gate. Standard library only.

Run: python3 -m unittest discover -s factory/gates -p 'test_*.py' -v
"""

from __future__ import annotations

import unittest

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


class TestSelfProtection(unittest.TestCase):
    """§9.14 workflow boundary."""

    def test_detects_gate_and_workflow_edits(self):
        for path in (".github/workflows/merge-gate.yml",
                     "factory/gates/merge_gate.py",
                     "factory/gates/nested/thing.py"):
            self.assertEqual(mg.self_modifying_paths([path]), [path], path)

    def test_ignores_unrelated_paths(self):
        self.assertEqual(mg.self_modifying_paths(["src/a.py", "README.md"]), [])


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

    def test_self_modification_fails(self):
        verdict = self.evaluate(changed_paths=["factory/gates/merge_gate.py"])
        self.assertIn(V.SELF_MODIFICATION, verdict.codes())

    def test_failing_tests_fail(self):
        self.assertIn(V.TESTS_FAILED, self.evaluate(tests_passed=False).codes())

    def test_no_changes_fails(self):
        self.assertIn(V.NO_CHANGES, self.evaluate(changed_paths=[]).codes())

    def test_unreadable_story_fails_closed(self):
        verdict = self.evaluate(story=None, story_fetch_error="HTTP 500")
        self.assertIn(V.INPUT_UNAVAILABLE, verdict.codes())

    def test_violations_are_independent(self):
        """A red result names every cause, so one failure cannot mask another."""
        verdict = self.evaluate(changed_paths=["factory/gates/merge_gate.py"],
                                tests_passed=False)
        self.assertIn(V.SELF_MODIFICATION, verdict.codes())
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
