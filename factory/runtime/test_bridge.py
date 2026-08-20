#!/usr/bin/env python3
"""Tests for the worker launch bridge. Standard library only.

Run: python3 -m unittest discover -s factory/runtime -p 'test_*.py' -v

`TestProofOfAction` is the class that matters. #96 ran the Claude engine
unattended for the first time and it ended cleanly having posted nothing, which
the runtime recorded as `LAUNCHED` — a definite success, so failover was
correctly suppressed and the silent no-op became terminal.

The property these tests pin is that an exit code is never read as proof of work
*in either direction*, and that the three answers this module can hold — it
happened, it did not happen, I cannot tell — never collapse into each other.
Collapsing them is not a tidiness problem: reporting ignorance as failure sends a
second engine to a Story that already has one.
"""

from __future__ import annotations

import contextlib
import io
import shlex
import subprocess
import unittest
from unittest import mock

import bridge
import workers


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


def run_main(engine="claude", story=96, project=95, run=None, answers=None,
             since="2026-08-20T23:00:00Z"):
    """Drive `main` with the engine and both GitHub reads faked.

    `answers` is the sequence `acknowledged_since` returns: `True` (it happened),
    `False` (it did not), `None` (cannot tell).
    """
    if not isinstance(answers, list):
        answers = [answers] * (bridge.ACK_CHECK_ATTEMPTS + 1)
    with mock.patch.object(bridge.subprocess, "run", run or runner()), \
         mock.patch.object(bridge, "server_now", return_value=since), \
         mock.patch.object(bridge, "acknowledged_since", side_effect=list(answers)), \
         mock.patch.object(bridge.time, "sleep"):
        return bridge.main(["--engine", engine, "--story", str(story),
                            "--project", str(project),
                            "--repo", "owner/name"])


class TestEngineCommand(unittest.TestCase):
    def test_claude_may_run_the_command_the_prompt_names(self):
        allowed = bridge.engine_command("claude", "p")[-1]
        self.assertIn("Bash(gh issue comment:*)", allowed)

    def test_claude_may_read_the_clock_the_prompt_demands(self):
        # Without this the engine invents a timestamp or refuses to post: the
        # prompt requires the current UTC time in the acknowledgement.
        self.assertIn("Bash(date:*)", bridge.CLAUDE_ALLOWED_TOOLS)

    def test_every_value_the_prompt_demands_is_obtainable(self):
        # The pairing itself, so that adding a requirement to the prompt without
        # granting the means to satisfy it fails here rather than in the world.
        prompt = bridge.task_prompt("owner/name", 96, 95)
        if "UTC time" in prompt:
            self.assertIn("Bash(date:*)", bridge.CLAUDE_ALLOWED_TOOLS)
        if "gh issue comment" in prompt:
            self.assertIn("Bash(gh issue comment:*)", bridge.CLAUDE_ALLOWED_TOOLS)

    def test_claude_is_not_granted_a_blanket_permission_mode(self):
        cmd = bridge.engine_command("claude", "p")
        self.assertNotIn("--permission-mode", cmd)
        self.assertNotIn("bypassPermissions", " ".join(cmd))

    def test_claude_may_not_run_arbitrary_bash(self):
        self.assertNotIn("Bash(*)", bridge.CLAUDE_ALLOWED_TOOLS)
        self.assertNotIn("Bash(gh:*)", bridge.CLAUDE_ALLOWED_TOOLS)

    def test_codex_keeps_its_network_access(self):
        # Removed once already, which is how #92 finished successfully having
        # posted nothing.
        self.assertIn("sandbox_workspace_write.network_access=true",
                      bridge.engine_command("codex", "p"))

    def test_unknown_engine_is_refused(self):
        with self.assertRaises(ValueError):
            bridge.engine_command("gpt-9", "p")

    def test_prompt_asks_for_exactly_the_heading_the_bridge_checks_for(self):
        prompt = bridge.task_prompt("owner/name", 96, 95)
        self.assertIn(bridge.ACK_HEADING, prompt)
        self.assertIn("#96", prompt)
        self.assertIn("owner/name", prompt)


class TestObservability(unittest.TestCase):
    def test_the_printed_invocation_is_pasteable_shell(self):
        # #90 requires that a human can reproduce the handoff from this line,
        # not merely read it. Unquoted, `Bash(gh issue comment:*)` is a parse
        # error, so the test is that the shell parses it back into argv.
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            bridge.main(["--engine", "claude", "--story", "96", "--project", "95",
                         "--repo", "owner/name", "--dry-run"])
        line = next(l for l in buffer.getvalue().splitlines() if "exec: " in l)
        argv = shlex.split(line.split("exec: ", 1)[1])
        self.assertEqual(["claude", "-p", "<prompt>", "--allowedTools",
                          bridge.CLAUDE_ALLOWED_TOOLS], argv)


class TestCheckBudget(unittest.TestCase):
    def test_the_check_fits_inside_the_launch_budget(self):
        """The whole check is spent inside `workers.LAUNCH_TIMEOUT_SECONDS`,
        which kills this process. If the check can outlast it, a slow engine is
        killed mid-check and reported AMBIGUOUS — which suppresses failover and
        makes the FAILED verdict unreachable exactly when it is needed."""
        worst_case = (bridge.ACK_HTTP_TIMEOUT_SECONDS                      # pre-launch
                      + bridge.ACK_CHECK_ATTEMPTS * bridge.ACK_HTTP_TIMEOUT_SECONDS
                      + (bridge.ACK_CHECK_ATTEMPTS - 1) * bridge.ACK_CHECK_DELAY_SECONDS)
        self.assertLess(worst_case, workers.LAUNCH_TIMEOUT_SECONDS * 0.6,
                        "the acknowledgement check leaves too little of the launch "
                        "budget for the engine itself")


class TestProofOfAction(unittest.TestCase):
    def test_new_acknowledgement_is_success(self):
        self.assertEqual(0, run_main(answers=[True]))

    def test_clean_exit_with_no_acknowledgement_is_a_definite_failure(self):
        # The #96 defect exactly: the engine ended, posted nothing, and the
        # runtime called it LAUNCHED.
        self.assertEqual(1, run_main(answers=False))

    def test_a_slow_acknowledgement_is_still_success(self):
        # GitHub is read-your-writes in practice, not by contract. Calling a
        # worker's success a failure would hand the Story to a second engine.
        self.assertEqual(0, run_main(answers=[False, False, True]))

    def test_one_dropped_response_does_not_forfeit_the_check(self):
        # A blip is not blindness: the pre-launch read already proved the token
        # and network work. Short-circuiting on the first None restores #96.
        self.assertEqual(1, run_main(answers=[None, False, False]))
        self.assertEqual(0, run_main(answers=[None, True]))

    def test_only_total_blindness_is_unverifiable(self):
        self.assertEqual(0, run_main(answers=[None, None, None]))

    def test_no_launch_instant_is_unverifiable(self):
        self.assertEqual(0, run_main(since=None, answers=AssertionError(
            "must not read comments without a launch instant")))

    def test_timeout_is_still_not_a_definite_failure(self):
        run = runner(raises=subprocess.TimeoutExpired(cmd="claude", timeout=1))
        self.assertEqual(2, run_main(run=run, answers=False))

    def test_missing_engine_is_a_definite_failure(self):
        run = runner(raises=FileNotFoundError("no claude"))
        self.assertEqual(1, run_main(run=run, answers=False))

    def test_dry_run_launches_nothing_and_checks_nothing(self):
        run = runner()
        with mock.patch.object(bridge.subprocess, "run", run), \
             mock.patch.object(bridge, "server_now",
                               side_effect=AssertionError("must not be called")):
            code = bridge.main(["--engine", "claude", "--story", "96",
                                "--project", "95", "--repo", "owner/name",
                                "--dry-run"])
        self.assertEqual(0, code)
        self.assertEqual([], run.calls)


class TestExitCodeIsNotProofEitherWay(unittest.TestCase):
    """The thesis cuts both ways. An engine that posted and then died in teardown
    has done the work; calling it a definite failure sends a second engine to
    post a second acknowledgement."""

    def test_a_posted_acknowledgement_survives_a_non_zero_exit(self):
        run = runner(code=3, stderr="teardown blew up")
        self.assertEqual(0, run_main(run=run, answers=[True]))

    def test_a_non_zero_exit_with_no_acknowledgement_is_still_a_failure(self):
        run = runner(code=3, stderr="boom")
        self.assertEqual(1, run_main(run=run, answers=[False]))

    def test_ignorance_does_not_rescue_a_non_zero_exit(self):
        # Only evidence overrides the engine's own verdict; not being able to
        # tell leaves the failure standing.
        run = runner(code=3, stderr="boom")
        self.assertEqual(1, run_main(run=run, answers=[None]))


class TestAcknowledgedSince(unittest.TestCase):
    SINCE = "2026-08-20T23:00:00Z"

    def _ask(self, payload):
        with mock.patch.object(bridge, "_api_get", return_value=(payload, {})):
            return bridge.acknowledged_since("owner/name", 96, self.SINCE)

    def test_an_acknowledgement_in_the_window_counts(self):
        self.assertTrue(self._ask([
            {"body": "## Worker acknowledgement\n\nI am codex.",
             "created_at": "2026-08-20T23:00:05Z"}]))

    def test_other_comments_in_the_window_do_not(self):
        self.assertFalse(self._ask([
            {"body": "## Verification finding\n\nit failed",
             "created_at": "2026-08-20T23:00:05Z"},
            {"body": None, "created_at": "2026-08-20T23:00:06Z"}]))

    def test_an_edited_older_acknowledgement_does_not_count(self):
        # `since` filters on update time, so an edited old comment comes back in
        # the response. It is not evidence that this invocation did anything.
        self.assertFalse(self._ask([
            {"body": "## Worker acknowledgement\n\nfrom last week",
             "created_at": "2026-08-14T10:00:00Z"}]))

    def test_leading_whitespace_does_not_hide_an_acknowledgement(self):
        self.assertTrue(self._ask([
            {"body": "\n  ## Worker acknowledgement",
             "created_at": "2026-08-20T23:00:05Z"}]))

    def test_the_query_is_windowed_not_paginated(self):
        # Reading the whole comment list would invert the verdict past 100
        # comments: the new acknowledgement lands on page 2, unseen, and every
        # worker that did post is called a failure.
        seen = {}

        def fake_get(url):
            seen["url"] = url
            return [], {}
        with mock.patch.object(bridge, "_api_get", fake_get):
            bridge.acknowledged_since("owner/name", 96, self.SINCE)
        self.assertIn(f"since={self.SINCE}", seen["url"])

    def test_an_unreadable_api_is_ignorance_not_evidence(self):
        with mock.patch.object(bridge, "_api_get", return_value=None):
            self.assertIsNone(bridge.acknowledged_since("owner/name", 96, self.SINCE))

    def test_an_unexpected_payload_is_ignorance_not_evidence(self):
        self.assertIsNone(self._ask({"message": "Not Found"}))


class TestServerNow(unittest.TestCase):
    def test_the_launch_instant_comes_from_github_not_the_local_clock(self):
        # Local skew would silently hide the acknowledgement, which reads as a
        # definite failure and invites a second engine onto the Story.
        with mock.patch.object(bridge, "_api_get",
                               return_value=([], {"Date": "Thu, 20 Aug 2026 23:28:07 GMT"})):
            self.assertEqual("2026-08-20T23:28:07Z", bridge.server_now("owner/name", 96))

    def test_a_missing_date_header_is_ignorance(self):
        with mock.patch.object(bridge, "_api_get", return_value=([], {})):
            self.assertIsNone(bridge.server_now("owner/name", 96))

    def test_an_unparseable_date_header_is_ignorance(self):
        with mock.patch.object(bridge, "_api_get",
                               return_value=([], {"Date": "not a date"})):
            self.assertIsNone(bridge.server_now("owner/name", 96))

    def test_an_unreadable_api_is_ignorance(self):
        with mock.patch.object(bridge, "_api_get", return_value=None):
            self.assertIsNone(bridge.server_now("owner/name", 96))


if __name__ == "__main__":
    unittest.main()
