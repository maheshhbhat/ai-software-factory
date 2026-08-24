"""The direct planning wrapper carries the dedicated Claude credential."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
RUN_SH = ROOT / "factory" / "agents" / "planning" / "run.sh"

PYTHON_STUB = """#!/bin/sh
printf '%s' "${CLAUDE_CODE_OAUTH_TOKEN-unset}" > "$CAPTURED_TOKEN"
printf '%s\\n' "$@" > "$CAPTURED_ARGS"
"""


class PlanningWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="planning-wrapper-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        python = self.bin / "python3"
        python.write_text(PYTHON_STUB, encoding="utf-8")
        python.chmod(0o755)

    def run_wrapper(self, token_file=None, explicit=None):
        captured_token = self.tmp / "captured-token"
        captured_args = self.tmp / "captured-args"
        credential = self.tmp / ".factory-reviewer-token"
        if token_file is not None:
            credential.write_text(token_file, encoding="utf-8")
        env = {
            "HOME": str(self.tmp),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "CAPTURED_TOKEN": str(captured_token),
            "CAPTURED_ARGS": str(captured_args),
        }
        if explicit is not None:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = explicit
        completed = subprocess.run(
            ["sh", str(RUN_SH), "owner/product", "17"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace")
        return completed, captured_token.read_text(), captured_args.read_text().splitlines()

    def test_loads_dedicated_credential_for_direct_planning(self):
        completed, token, arguments = self.run_wrapper(token_file="file-secret\n")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("file-secret", token)
        self.assertEqual([str(RUN_SH.with_name("invoke.py")), "--repo", "owner/product",
                          "--artifact", "17"], arguments)
        self.assertNotIn("file-secret", completed.stdout + completed.stderr)

    def test_explicit_credential_wins(self):
        completed, token, _ = self.run_wrapper(
            token_file="file-secret\n", explicit="caller-secret")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("caller-secret", token)
        self.assertNotIn("caller-secret", completed.stdout + completed.stderr)

    def test_missing_credential_reaches_invoke_without_inventing_one(self):
        completed, token, _ = self.run_wrapper()
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("unset", token)


if __name__ == "__main__":
    unittest.main()
