#!/usr/bin/env python3
"""Tests for the worker launch bridge. Standard library only.

Run: python3 -m unittest discover -s factory/runtime -p 'test_*.py' -v

`TestProofOfAction` is the class that matters. #96 ran the Claude engine
unattended for the first time and it ended cleanly having posted nothing, which
the runtime recorded as `LAUNCHED` — a definite success, so failover was
correctly suppressed and the silent no-op became terminal. The property these
tests pin is that an exit code is never read as proof of work, and that the two
non-verdicts (timeout, unverifiable) keep their distinct meanings, because
collapsing either into "it failed" is how one Story acquires two workers.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

import bridge


def runner(code=0, stdout="", stderr="", raises=None):
    """A fake subprocess.run with a fixed outcome, recording its calls."""
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)
    _run.calls = calls
    return _run


def run_main(engine="claude", story=96, project=95, run=None, ids=None):
    """Drive `main` with the engine and the GitHub read both faked.

    `ids` is the sequence of acknowledgement-id sets `acknowledgement_ids`
    returns, snapshot first. A single value is reused for every call.
    """
    if not isinstance(ids, list):
        ids = [ids, ids, ids, ids]
    with mock.patch.object(bridge.subprocess, "run", run or runner()), \
         mock.patch.object(bridge, "acknowledgement_ids", side_effect=list(ids)), \
         mock.patch.object(bridge.time, "sleep"):
        return bridge.main(["--engine", engine, "--story", str(story),
                            "--project", str(project),
                            "--repo", "owner/name"])


class TestEngineCommand(unittest.TestCase):
    def test_claude_may_run_the_command_the_prompt_names(self):
        cmd = bridge.engine_command("claude", "p")
        self.assertIn("--allowedTools", cmd)
        allowed = cmd[cmd.index("--allowedTools") + 1]
        self.assertIn("Bash(gh issue comment:*)", allowed)

    def test_claude_is_not_granted_a_blanket_permission_mode(self):
        # The narrow allowlist is the point; a bypass mode would pass the same
        # behavioural test while handing an unattended agent everything.
        cmd = bridge.engine_command("claude", "p")
        self.assertNotIn("--permission-mode", cmd)
        self.assertNotIn("bypassPermissions", " ".join(cmd))

    def test_claude_may_not_run_arbitrary_bash(self):
        allowed = bridge.CLAUDE_ALLOWED_TOOLS
        self.assertNotIn("Bash(*)", allowed)
        self.assertNotIn("Bash(gh:*)", allowed)

    def test_codex_keeps_its_network_access(self):
        # Removed once already, which is how #92 finished successfully having
        # posted nothing.
        cmd = bridge.engine_command("codex", "p")
        self.assertIn("sandbox_workspace_write.network_access=true", cmd)

    def test_unknown_engine_is_refused(self):
        with self.assertRaises(ValueError):
            bridge.engine_command("gpt-9", "p")

    def test_prompt_asks_for_exactly_the_heading_the_bridge_checks_for(self):
        prompt = bridge.task_prompt("owner/name", 96, 95)
        self.assertIn(bridge.ACK_HEADING, prompt)
        self.assertIn("#96", prompt)
        self.assertIn("owner/name", prompt)


class TestProofOfAction(unittest.TestCase):
    def test_new_acknowledgement_is_success(self):
        self.assertEqual(0, run_main(ids=[set(), {1}]))

    def test_clean_exit_with_no_acknowledgement_is_a_definite_failure(self):
        # The #96 defect exactly: the engine ended, posted nothing, and the
        # runtime called it LAUNCHED.
        self.assertEqual(1, run_main(ids=[set(), set(), set(), set()]))

    def test_an_earlier_acknowledgement_is_not_proof_this_run_acted(self):
        self.assertEqual(1, run_main(ids=[{7}, {7}, {7}, {7}]))

    def test_a_slow_acknowledgement_is_still_success(self):
        # GitHub is read-your-writes in practice, not by contract. Calling a
        # worker's success a failure would hand the Story to a second engine.
        self.assertEqual(0, run_main(ids=[set(), set(), {1}]))

    def test_the_check_runs_only_after_a_clean_exit(self):
        run = runner(code=3, stderr="boom")
        self.assertEqual(1, run_main(run=run, ids=[set(), set(), set(), set()]))

    def test_unverifiable_reports_the_engine_exit_status(self):
        # Ignorance is not evidence: with no token or no network the bridge
        # reports what it always did rather than inventing a failure.
        self.assertEqual(0, run_main(ids=[None, None]))
        self.assertEqual(0, run_main(ids=[set(), None]))

    def test_timeout_is_still_not_a_definite_failure(self):
        run = runner(raises=subprocess.TimeoutExpired(cmd="claude", timeout=1))
        self.assertEqual(2, run_main(run=run, ids=set()))

    def test_missing_engine_is_a_definite_failure(self):
        run = runner(raises=FileNotFoundError("no claude"))
        self.assertEqual(1, run_main(run=run, ids=set()))

    def test_dry_run_launches_nothing_and_checks_nothing(self):
        run = runner()
        with mock.patch.object(bridge.subprocess, "run", run), \
             mock.patch.object(bridge, "acknowledgement_ids",
                               side_effect=AssertionError("must not be called")):
            code = bridge.main(["--engine", "claude", "--story", "96",
                                "--project", "95", "--repo", "owner/name",
                                "--dry-run"])
        self.assertEqual(0, code)
        self.assertEqual([], run.calls)


class TestAcknowledgementIds(unittest.TestCase):
    def _ids(self, payload):
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        with mock.patch.object(bridge.json, "load", return_value=payload), \
             mock.patch.object(bridge.urllib.request, "urlopen", return_value=stream):
            return bridge.acknowledgement_ids("owner/name", 96)

    def test_only_acknowledgement_comments_count(self):
        ids = self._ids([
            {"id": 1, "body": "## Worker acknowledgement\n\nI am codex."},
            {"id": 2, "body": "## Verification finding\n\nit failed"},
            {"id": 3, "body": None},
        ])
        self.assertEqual({1}, ids)

    def test_leading_whitespace_does_not_hide_an_acknowledgement(self):
        self.assertEqual({4}, self._ids([{"id": 4, "body": "\n  ## Worker acknowledgement"}]))

    def test_an_unreadable_api_is_ignorance_not_evidence(self):
        with mock.patch.object(bridge.urllib.request, "urlopen",
                               side_effect=OSError("no network")):
            self.assertIsNone(bridge.acknowledgement_ids("owner/name", 96))

    def test_an_unexpected_payload_is_ignorance_not_evidence(self):
        self.assertIsNone(self._ids({"message": "Not Found"}))


if __name__ == "__main__":
    unittest.main()
