import pathlib
import unittest

import contract


class AltitudeTests(unittest.TestCase):
    def test_trigger_type_selects_altitude(self):
        self.assertEqual(contract.Altitude.CAMPAIGN,
                         contract.select_altitude({"type:roadmap-commitment"}))
        self.assertEqual(contract.Altitude.PROJECT,
                         contract.select_altitude({"type:project"}))

    def test_missing_unsupported_or_conflicting_type_fails(self):
        for labels in (set(), {"type:story"},
                       {"type:project", "type:roadmap-commitment"}):
            with self.subTest(labels=labels), self.assertRaises(contract.ContractError):
                contract.select_altitude(labels)


class InputTests(unittest.TestCase):
    def valid(self):
        return {"trigger": {"labels": ["type:project"]},
                "product": "# Product", "adrs": [],
                "repository": {"files": ["product.md"]}, "review_comments": [],
                "existing_plan": {}}

    def test_all_grounding_inputs_are_required(self):
        for key in contract.REQUIRED_INPUTS:
            value = self.valid()
            del value[key]
            with self.subTest(key=key), self.assertRaises(contract.ContractError):
                contract.validate_input(value)

    def test_empty_product_or_repository_fails(self):
        for key, value in (("product", ""), ("repository", {})):
            data = self.valid()
            data[key] = value
            with self.subTest(key=key), self.assertRaises(contract.ContractError):
                contract.validate_input(data)


class OutputTests(unittest.TestCase):
    DIGEST = """## Plan in plain language

Plain explanation.

## How the plan works

```mermaid
flowchart LR
  a --> b
```

Text fallback.

## Story dependencies

```mermaid
flowchart LR
  story_a --> story_b
```

Text fallback."""

    def test_campaign_schema_excludes_project_artifacts(self):
        value = {"altitude": "campaign", "project": {}, "rationale": "why",
                 "risks": []}
        self.assertIs(value, contract.validate_output(contract.Altitude.CAMPAIGN, value))
        value["stories"] = []
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.CAMPAIGN, value)

    def test_project_schema_excludes_campaign_proposal(self):
        value = {"altitude": "project", "adr": {}, "stories": [],
                 "expected_bells": 2, "digest": self.DIGEST}
        self.assertIs(value, contract.validate_output(contract.Altitude.PROJECT, value))
        value["project"] = {}
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.PROJECT, value)

    def test_missing_or_mismatched_output_fails(self):
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.CAMPAIGN, {"altitude": "campaign"})
        project = {"altitude": "campaign", "adr": {}, "stories": [],
                   "expected_bells": 2, "digest": self.DIGEST}
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.PROJECT, project)

    def test_project_digest_requires_plain_language_and_two_diagrams(self):
        value = {"altitude": "project", "adr": {}, "stories": [],
                 "expected_bells": 2, "digest": "plan"}
        with self.assertRaisesRegex(contract.ContractError, "Plan in plain language"):
            contract.validate_output(contract.Altitude.PROJECT, value)

    def test_project_digest_requires_text_after_each_diagram(self):
        for missing, digest in (
            ("How the plan works", self.DIGEST.replace(
                "\n\nText fallback.\n\n## Story dependencies",
                "\n\n## Story dependencies")),
            ("Story dependencies", self.DIGEST.rsplit("\n\nText fallback.", 1)[0]),
        ):
            value = {"altitude": "project", "adr": {}, "stories": [],
                     "expected_bells": 2, "digest": digest}
            with self.subTest(section=missing), self.assertRaisesRegex(
                    contract.ContractError, f"{missing!r} lacks a textual fallback"):
                contract.validate_output(contract.Altitude.PROJECT, value)

    def test_code_block_or_following_section_text_is_not_a_fallback(self):
        digest = self.DIGEST.replace(
            "Text fallback.\n\n## Story dependencies",
            "```text\nnot prose\n```\n\n## Story dependencies")
        value = {"altitude": "project", "adr": {}, "stories": [],
                 "expected_bells": 2, "digest": digest}
        with self.assertRaisesRegex(contract.ContractError,
                                    "How the plan works.*textual fallback"):
            contract.validate_output(contract.Altitude.PROJECT, value)


class CanonicalPathTests(unittest.TestCase):
    def test_prompt_has_required_contract_language(self):
        prompt = pathlib.Path(__file__).with_name("prompt.md").read_text()
        for phrase in ("product.md", "existing ADR", "repository",
                       "type:roadmap-commitment", "type:project",
                       "risk-ordered", "expected bells", "Depends-on",
                       "hazard", "falsifiable", "read-back"):
            self.assertIn(phrase, prompt)
        for phrase in ("Plan in plain language", "How the plan works",
                       "Story dependencies", "```mermaid"):
            self.assertIn(phrase, prompt)

    def test_no_competing_root_agents_tree(self):
        root = pathlib.Path(__file__).resolve().parents[3]
        self.assertFalse((root / "agents" / "planning").exists())


if __name__ == "__main__":
    unittest.main()
