import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude/skills/factory-operator-administrator"


class OperatorRoleTests(unittest.TestCase):
    def test_both_engine_entrypoints_route_to_one_shared_role(self):
        agents = (ROOT / "AGENTS.md").read_text()
        claude = (ROOT / "CLAUDE.md").read_text()
        self.assertIn(".claude/skills/factory-operator-administrator/SKILL.md", agents)
        self.assertIn("factory-operator-administrator", claude)
        self.assertTrue((SKILL / "SKILL.md").is_file())

    def test_role_requires_evidence_measurement_freeze_and_human_authority(self):
        text = (SKILL / "SKILL.md").read_text()
        for requirement in (
            "repository and GitHub are authoritative",
            "controlled measurement or qualification is active",
            "Never invent or post a human verdict",
            "Never start a second poller",
            "Never change factory behavior during a controlled measurement",
        ):
            self.assertIn(requirement, text)

    def test_learning_requires_reviewed_evidence_and_a_falsifying_test(self):
        text = (SKILL / "references/learning.md").read_text()
        self.assertIn("Evidence", text)
        self.assertIn("smallest correction", text)
        self.assertIn("falsifying test", text)
        self.assertIn("Obtain required authorization", text)
        self.assertIn("Revise or remove a rule", text)

    def test_handoff_detects_checkout_drift(self):
        module_path = SKILL / "scripts/handoff.py"
        spec = importlib.util.spec_from_file_location("operator_handoff", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(ROOT, module.ROOT)
        value = {key: "x" for key in module.REQUIRED}
        value.update({"schema_version": 1,
                      "git": {"branch": "old", "commit": "old"}})
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(module, "HANDOFF", pathlib.Path(directory) / "handoff.json"), \
             mock.patch.object(module, "command", side_effect=["new", "new"]), \
             mock.patch.object(module, "processes", return_value=[]):
            module.HANDOFF.write_text(json.dumps(value))
            self.assertEqual(1, module.check(None))


if __name__ == "__main__":
    unittest.main()
