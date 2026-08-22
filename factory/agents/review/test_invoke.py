import json
import base64
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invoke


class OutputTests(unittest.TestCase):
    def test_default_command_can_only_write_the_outcome(self):
        with mock.patch.object(pathlib.Path, "read_text", return_value="prompt"):
            cmd = invoke.command(pathlib.Path("input.json"), pathlib.Path("out.json"))
        self.assertIn("acceptEdits", cmd)
        self.assertEqual("Write", cmd[cmd.index("--allowedTools") + 1])
        self.assertEqual("Bash", cmd[cmd.index("--disallowedTools") + 1])
        self.assertIn("--no-session-persistence", cmd)

    def test_private_git_auth_uses_github_basic_transport_shape(self):
        header = invoke.git_auth_header("secret")
        self.assertTrue(header.startswith("Authorization: Basic "))
        encoded = header.removeprefix("Authorization: Basic ")
        self.assertEqual(base64.b64decode(encoded).decode(), "x-access-token:secret")
        self.assertNotIn("Bearer", header)

    def test_exact_head_approval(self):
        with mock.patch.object(pathlib.Path, "read_text",
                               return_value=json.dumps({"head": "a" * 40,
                                                        "verdict": "approval",
                                                        "summary": "looks good"})):
            self.assertEqual(invoke.parse_result(pathlib.Path("out"), "a" * 40)["verdict"],
                             "approval")

    def test_stale_malformed_and_empty_findings_fail(self):
        values = [{"head": "b" * 40, "verdict": "approval", "summary": "x"},
                  {"head": "a" * 40, "verdict": "findings", "findings": []},
                  {"head": "a" * 40, "verdict": "maybe"}]
        for value in values:
            with self.subTest(value=value), \
                 mock.patch.object(pathlib.Path, "read_text", return_value=json.dumps(value)):
                with self.assertRaises(invoke.ReviewError):
                    invoke.parse_result(pathlib.Path("out"), "a" * 40)

    def test_environment_excludes_github_and_worker_context(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret",
                                          "FACTORY_WORKER_SESSION": "leak",
                                          "ANTHROPIC_API_KEY": "model", "PATH": "/bin",
                                          "HOME": "/safe/home", "USER": "reviewer",
                                          "LOGNAME": "reviewer"},
                             clear=True):
            self.assertEqual(invoke.clean_environment(),
                             {"ANTHROPIC_API_KEY": "model", "PATH": "/bin",
                              "HOME": "/safe/home", "USER": "reviewer",
                              "LOGNAME": "reviewer"})

    def test_unavailable_reviewer_fails(self):
        with mock.patch.object(subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 7, "", "offline")):
            with self.assertRaisesRegex(invoke.ReviewError, "unavailable"):
                invoke.run(["review"], cwd=".")


if __name__ == "__main__":
    unittest.main()
