#!/usr/bin/env python3
"""The acceptance suite, discoverable by `unittest`.

Run: python3 -m unittest discover -s factory/acceptance -p 'test_*.py' -v

This wrapper exists so CI runs the scenarios through the same command it runs
every other suite with, rather than through a second invocation path that could
silently stop being called. Each scenario is one test, so a failure names the
scenario and prints the evidence it had collected before it failed.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import REGISTRY, quiet_runlog  # noqa: E402
import run_acceptance  # noqa: E402,F401 — importing registers every scenario


class AcceptanceScenarios(unittest.TestCase):
    """One test per scenario, generated below."""

    @classmethod
    def setUpClass(cls):
        quiet_runlog()


def _add(cls):
    def test(self):
        result = cls().execute()
        if not result.passed:
            evidence = "\n".join(f"    · {line}" for line in result.evidence)
            self.fail(f"{cls.name} ({cls.behaviour})\n{evidence}\n"
                      f"  ✗ {result.error}\n{result.traceback}")

    test.__doc__ = f"{cls.name} — {cls.behaviour}"
    setattr(AcceptanceScenarios,
            "test_" + cls.name.lower().replace(" ", "_").replace("-", "_"), test)


for _scenario in REGISTRY:
    _add(_scenario)


class TheSuiteCoversPhase2(unittest.TestCase):
    """The suite is only evidence if it covers what Phase 2 promised."""

    def test_every_named_phase_2_behaviour_has_a_scenario(self):
        covered = " ".join(cls.behaviour for cls in REGISTRY).lower()
        for behaviour in ("deterministic dispatch", "authorization",
                          "scope enforcement", "worker selection and launch",
                          "worker execution observability",
                          "no-deliverable completion",
                          "pr/merge lifecycle reconciliation", "dependencies",
                          "replay and restart idempotency", "claim recovery",
                          "worker failure and failover",
                          "wip and attempt limits", "fail-closed"):
            self.assertIn(behaviour, covered,
                          f"no scenario claims to prove {behaviour!r}")

    def test_every_scenario_names_itself_and_a_behaviour(self):
        for cls in REGISTRY:
            self.assertTrue(cls.name.strip(), cls.__name__)
            self.assertTrue(cls.behaviour.strip(), cls.name)

    def test_scenario_names_are_unique(self):
        names = [cls.name for cls in REGISTRY]
        self.assertEqual(len(names), len(set(names)), names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
