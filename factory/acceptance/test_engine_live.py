#!/usr/bin/env python3
"""Live proof that the default engine path can authenticate and write.

Deliberately opt-in: every test here skips unless FACTORY_ENGINE_LIVE=1,
because the subject is the real `claude` binary under the worker's real
`clean_environment()` — the exact path every hermetic suite substitutes, which
is how a permission mode that denied writes and an environment that could not
authenticate sat unnoticed behind sixteen green criteria (#330, 2026-08-22).

Run deliberately, costs real tokens:

    FACTORY_ENGINE_LIVE=1 python3 -m unittest factory.acceptance.test_engine_live

Never a required check; never part of a routine `unittest discover` run.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory" / "agents" / "worker"))

LIVE = os.environ.get("FACTORY_ENGINE_LIVE") == "1"


@unittest.skipUnless(LIVE, "live engine proof; run deliberately with "
                           "FACTORY_ENGINE_LIVE=1")
class TestTheDefaultEnginePathWorks(unittest.TestCase):
    """The production default, not a stand-in: default command, clean env."""

    def test_the_engine_authenticates_and_writes_under_the_clean_environment(self):
        import invoke
        env = invoke.clean_environment("claude")
        with tempfile.TemporaryDirectory() as workspace:
            marker = pathlib.Path(workspace) / "live-proof.txt"
            result = subprocess.run(
                ["claude", "-p",
                 f"Create a file named {marker.name} containing exactly the "
                 f"word proven. Do nothing else.",
                 "--permission-mode", "acceptEdits",
                 "--max-budget-usd", "0.50",
                 "--no-session-persistence"],
                cwd=workspace, env=env, capture_output=True, text=True,
                timeout=300)
            self.assertEqual(0, result.returncode,
                             f"engine refused under the production environment: "
                             f"stderr={result.stderr[-500:]} "
                             f"stdout={result.stdout[-500:]}")
            self.assertTrue(marker.exists(),
                            "the engine exited 0 but wrote nothing — the "
                            "permission mode is denying writes again")
            self.assertIn("proven", marker.read_text())


if __name__ == "__main__":
    unittest.main()
