import pathlib
import unittest

import contract


class AltitudeTests(unittest.TestCase):
    def test_codex_structured_output_discriminators_have_explicit_types(self):
        for altitude in contract.Altitude:
            pending = [contract.json_schema(altitude)]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if "const" in value or "enum" in value:
                        self.assertIn("type", value)
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)

    def test_codex_structured_output_objects_are_closed_and_fully_required(self):
        """The Codex structured-output boundary rejects dynamic object keys."""
        for altitude in contract.Altitude:
            pending = [contract.json_schema(altitude)]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if value.get("type") == "object":
                        self.assertIs(value.get("additionalProperties"), False)
                        self.assertEqual(set(value.get("properties", {})),
                                         set(value.get("required", [])))
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)

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
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [], "adr": {}, "stories": [],
                 "expected_bells": 2, "risks": "risk", "digest": self.DIGEST}
        self.assertIs(value, contract.validate_output(contract.Altitude.PROJECT, value))
        value["project"] = {}
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.PROJECT, value)

    def test_every_envelope_assignment_requires_exact_story_local_check(self):
        story = {"key": "core", "title": "Core", "spec": "bounded",
                 "phase": "build", "depends_on": [], "hazard": False,
                 "acceptance_criteria": ["check"],
                 "operating_envelope_ids": ["OE-BROWSER-1"],
                 "operating_envelope_checks": [], "scope": ["src/core.js"],
                 "spend_cap": "$5 / 60 min"}
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [{"id": "OE-BROWSER-1",
                     "category": "responsiveness", "requirement": "render promptly",
                     "failure_condition": "browser render exceeds bound"}],
                 "adr": {}, "stories": [story], "expected_bells": 2,
                 "risks": "risk", "digest": self.DIGEST}
        with self.assertRaisesRegex(contract.ContractError, "Story-local executable"):
            contract.validate_output(contract.Altitude.PROJECT, value)
        story["title"] = "Browser core"
        story["operating_envelope_checks"] = [{
            "id": "OE-BROWSER-1", "check": "browser render exceeds bound"}]
        self.assertIs(value, contract.validate_output(contract.Altitude.PROJECT, value))
        story["operating_envelope_checks"].append({
            "id": "OE-EXTRA", "check": "unassigned check"})
        with self.assertRaisesRegex(contract.ContractError, "Story-local executable"):
            contract.validate_output(contract.Altitude.PROJECT, value)

    def test_project60_core_story_cannot_own_browser_wide_obligations(self):
        story = {"key": "scenario_engine", "title": "Scenario engine",
                 "spec": "Build a pure deterministic calculation API.",
                 "phase": "build", "depends_on": [], "hazard": False,
                 "acceptance_criteria": ["pure calculation tests pass"],
                 "operating_envelope_ids": ["OE-RESP-1", "OE-DEGRADE-1"],
                 "operating_envelope_checks": [
                     {"id": "OE-RESP-1", "check": "Chrome render exceeds one second"},
                     {"id": "OE-DEGRADE-1", "check": "invalid page leaves stale UI visible"}],
                 "scope": ["src/scenario-engine.js"], "spend_cap": "$5 / 60 min"}
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [
                     {"id": "OE-RESP-1", "category": "responsiveness",
                      "requirement": "browser renders within one second",
                      "failure_condition": "Chrome render exceeds one second"},
                     {"id": "OE-DEGRADE-1", "category": "degradation",
                      "requirement": "invalid browser input clears stale output",
                      "failure_condition": "page leaves stale output visible"}],
                 "adr": {}, "stories": [story], "expected_bells": 2,
                 "risks": "risk", "digest": self.DIGEST}
        with self.assertRaisesRegex(contract.ContractError, "cannot verify browser"):
            contract.validate_output(contract.Altitude.PROJECT, value)

        story["title"] = "Browser scenario assurance"
        story["spec"] = "Exercise the browser page and its rendered UI."
        story["scope"] = ["src/browser-app.js"]
        self.assertIs(value, contract.validate_output(contract.Altitude.PROJECT, value))

    def test_missing_or_mismatched_output_fails(self):
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.CAMPAIGN, {"altitude": "campaign"})
        project = {"altitude": "campaign", "operating_envelope": [],
                   "adr": {}, "stories": [],
                   "expected_bells": 2, "digest": self.DIGEST}
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.PROJECT, project)

    def test_project_digest_requires_plain_language_and_two_diagrams(self):
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [], "adr": {}, "stories": [],
                 "expected_bells": 2, "risks": "risk", "digest": "plan"}
        with self.assertRaisesRegex(contract.ContractError, "Plan in plain language"):
            contract.validate_output(contract.Altitude.PROJECT, value)

    def test_project_digest_requires_text_after_each_diagram(self):
        for missing, digest in (
            ("How the plan works", self.DIGEST.replace(
                "\n\nText fallback.\n\n## Story dependencies",
                "\n\n## Story dependencies")),
            ("Story dependencies", self.DIGEST.rsplit("\n\nText fallback.", 1)[0]),
        ):
            value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                     "operating_envelope": [], "adr": {}, "stories": [],
                     "expected_bells": 2, "risks": "risk", "digest": digest}
            with self.subTest(section=missing), self.assertRaisesRegex(
                    contract.ContractError, f"{missing!r} lacks a textual fallback"):
                contract.validate_output(contract.Altitude.PROJECT, value)

    def test_code_block_or_following_section_text_is_not_a_fallback(self):
        digest = self.DIGEST.replace(
            "Text fallback.\n\n## Story dependencies",
            "```text\nnot prose\n```\n\n## Story dependencies")
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [], "adr": {}, "stories": [],
                 "expected_bells": 2, "risks": "risk", "digest": digest}
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

    def test_prompt_gates_exact_and_optimal_claims_on_a_complete_proof_obligation(self):
        prompt = pathlib.Path(__file__).with_name("prompt.md").read_text()
        section = prompt.split("### Proof obligations for exact or complete claims", 1)[1]
        section = section.split("Return this exact JSON shape", 1)[0]
        normalized = " ".join(section.split())

        for claim in ("maximum", "minimum", "highest", "lowest", "optimal",
                      "exact", "exhaustive", "all", "every", "guaranteed"):
            with self.subTest(claim=claim):
                self.assertIn(f"`{claim}`", section)

        for proof_part in ("**Claim**", "**Domain**",
                           "**Invariant / monotonicity / structural property**",
                           "**Skipped-value justification**", "**Bound**",
                           "**Falsification strategy**"):
            with self.subTest(proof_part=proof_part):
                self.assertIn(proof_part, section)

        self.assertIn(
            "Testing selected candidates and an adjacent candidate is not, by itself, "
            "proof of a global maximum.", normalized)
        self.assertIn(
            "proves that every skipped value cannot be a better feasible result", normalized)
        self.assertIn(
            "narrow the product claim to what the method can establish or fail planning",
            normalized)
        self.assertIn(
            "Do not emit the Project or Story for Delivery with the unsupported claim.",
            normalized)

    def test_no_competing_root_agents_tree(self):
        root = pathlib.Path(__file__).resolve().parents[3]
        self.assertFalse((root / "agents" / "planning").exists())


if __name__ == "__main__":
    unittest.main()
