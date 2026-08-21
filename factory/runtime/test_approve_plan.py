import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "approve-plan.sh"


class ApprovalWrapperTests(unittest.TestCase):
    def run_script(self, confirmation="approved", body=None, fail_view=False):
        body = body if body is not None else textwrap.dedent("""\
            ### Goal

            Ship it.

            ### Falsifiable acceptance criteria

            - [ ] first exact criterion
            - [ ] second `exact` criterion

            ### Stories

            _No response_
            """)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name)
        log = root / "posted"
        fake = root / "gh"
        fake.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            if [ "$1 $2" = "issue view" ]; then
              if [ "$FAKE_VIEW_FAILURE" = "1" ]; then exit 3; fi
              printf '%s' "$FAKE_ISSUE_BODY"
              exit 0
            fi
            if [ "$1 $2" = "api user" ]; then
              printf '%s\\n' 'maheshhbhat'
              exit 0
            fi
            if [ "$1 $2" = "issue comment" ]; then
              while [ "$#" -gt 0 ]; do
                if [ "$1" = "--body-file" ]; then shift; cp "$1" "{log}"; exit 0; fi
                shift
              done
            fi
            exit 4
            """))
        fake.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{root}:{env['PATH']}"
        env["FAKE_ISSUE_BODY"] = body
        env["FAKE_VIEW_FAILURE"] = "1" if fail_view else "0"
        result = subprocess.run(
            [str(SCRIPT), "186", "owner/repo"], input=f"{confirmation}\n",
            text=True, capture_output=True, env=env)
        return result, log.read_text() if log.exists() else None

    def test_confirmation_posts_exact_schema_comment(self):
        result, posted = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(textwrap.dedent("""\
            ## Plan approval

            decision: approved
            actor: @maheshhbhat

            Approved criteria (verbatim copy of the Falsifiable acceptance criteria section at approval time):


            - [ ] first exact criterion
            - [ ] second `exact` criterion
            """), posted)

    def test_any_other_confirmation_posts_nothing(self):
        result, posted = self.run_script("yes")
        self.assertNotEqual(0, result.returncode)
        self.assertIsNone(posted)
        self.assertIn("not posted", result.stderr)

    def test_missing_criteria_posts_nothing(self):
        result, posted = self.run_script(body="### Goal\n\nNo criteria\n")
        self.assertNotEqual(0, result.returncode)
        self.assertIsNone(posted)
        self.assertIn("no rendered", result.stderr)

    def test_github_read_failure_posts_nothing(self):
        result, posted = self.run_script(fail_view=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIsNone(posted)

    def test_invalid_project_number_fails_before_github(self):
        result = subprocess.run([str(SCRIPT), "not-a-number"], text=True,
                                capture_output=True)
        self.assertEqual(2, result.returncode)


if __name__ == "__main__":
    unittest.main()
