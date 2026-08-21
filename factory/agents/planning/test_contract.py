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
                "repository": {"files": ["product.md"]}}

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
    def test_campaign_schema_excludes_project_artifacts(self):
        value = {"altitude": "campaign", "project": {}, "rationale": "why",
                 "risks": []}
        self.assertIs(value, contract.validate_output(contract.Altitude.CAMPAIGN, value))
        value["stories"] = []
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.CAMPAIGN, value)

    def test_project_schema_excludes_campaign_proposal(self):
        value = {"altitude": "project", "adr": {}, "stories": [],
                 "expected_bells": 2, "digest": "plan"}
        self.assertIs(value, contract.validate_output(contract.Altitude.PROJECT, value))
        value["project"] = {}
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.PROJECT, value)

    def test_missing_or_mismatched_output_fails(self):
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.CAMPAIGN, {"altitude": "campaign"})
        project = {"altitude": "campaign", "adr": {}, "stories": [],
                   "expected_bells": 2, "digest": "plan"}
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.PROJECT, project)


class CanonicalPathTests(unittest.TestCase):
    def test_prompt_has_required_contract_language(self):
        prompt = pathlib.Path(__file__).with_name("prompt.md").read_text()
        for phrase in ("product.md", "existing ADR", "repository",
                       "type:roadmap-commitment", "type:project",
                       "risk-ordered", "expected bells", "Depends-on",
                       "hazard", "falsifiable", "read-back"):
            self.assertIn(phrase, prompt)

    def test_no_competing_root_agents_tree(self):
        root = pathlib.Path(__file__).resolve().parents[3]
        self.assertFalse((root / "agents" / "planning").exists())


if __name__ == "__main__":
    unittest.main()
