#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

from factory.capacity_pool import enforcement


class CapacityArchitectureTests(unittest.TestCase):
    def test_live_repository_matches_exact_temporary_debt(self):
        self.assertEqual([], enforcement.validate_inventory())

    def test_new_direct_invocation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "factory" / "agents" / "new" / "invoke.py"
            path.parent.mkdir(parents=True)
            path.write_text('command = ["codex", "exec", "do work"]\n')
            inventory = root / "inventory.json"
            inventory.write_text(json.dumps({
                "components": [], "temporary_direct_invocation_debt": []}))
            errors = enforcement.validate_inventory(root, inventory)
            self.assertEqual(
                ["unapproved direct model invocation: factory/agents/new/invoke.py"],
                errors)

    def test_debt_must_be_classified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "factory" / "agents" / "old" / "invoke.py"
            path.parent.mkdir(parents=True)
            path.write_text('command = ["claude", "-p", "work"]\n')
            inventory = root / "inventory.json"
            inventory.write_text(json.dumps({
                "components": [],
                "temporary_direct_invocation_debt": ["factory/agents/old/invoke.py"]}))
            self.assertIn("debt path absent from inventory",
                          enforcement.validate_inventory(root, inventory)[0])


if __name__ == "__main__":
    unittest.main()
