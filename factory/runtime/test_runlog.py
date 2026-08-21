#!/usr/bin/env python3
"""Tests for the runtime's operational record. Standard library only.

Run: python3 -m unittest discover -s factory/runtime -p 'test_*.py' -v

Two classes carry the weight, and neither is about formatting.

`TestSecretsNeverReachTheLog` is the one that matters most: this module writes
worker output to a file, and worker output is arbitrary text produced by an
engine that has a token in its environment. A log that captures the credential
it was launched with is worse than no log.

`TestLoggingNeverCosts` pins the other property — a logging failure must never
stop a dispatch. Every write here is best-effort by design, and a future edit
that lets an exception escape would turn an unwritable disk into a factory that
does no work.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import runlog


class LogToTemp(unittest.TestCase):
    """Every test writes to its own file and never to stderr."""

    def setUp(self):
        self._env = dict(os.environ)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "runtime.jsonl")
        os.environ["FACTORY_RUNTIME_LOG"] = self.path
        os.environ["FACTORY_RUNTIME_LOG_STDERR"] = "0"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def lines(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class TestRecordShape(LogToTemp):
    def test_an_event_is_one_json_object_per_line(self):
        runlog.event("worker.launch.start", story=104, worker="claude-delivery")
        runlog.event("worker.launch.end", story=104, exit=0)
        records = self.lines()
        self.assertEqual(["worker.launch.start", "worker.launch.end"],
                         [r["event"] for r in records])
        self.assertEqual(104, records[0]["story"])

    def test_every_record_carries_a_timestamp_and_a_run_id(self):
        """Events from the poller and from the bridge subprocess it launched
        must be stitchable back together after the fact."""
        record = runlog.event("process.started", pid=1234)
        self.assertTrue(record["ts"].endswith("Z"))
        self.assertEqual(runlog.RUN_ID, record["run"])

    def test_absent_fields_are_omitted_not_nulled(self):
        """A PID the runtime could not observe must not be recorded as a PID."""
        record = runlog.event("worker.launch.end", pid=None, exit=0)
        self.assertNotIn("pid", record)
        self.assertIn("exit", record)

    def test_an_empty_output_field_is_kept_as_evidence(self):
        """'The engine printed nothing' is a finding — #96 was exactly that."""
        runlog.event("worker.launch.end", stdout="", exit=0)
        self.assertEqual("", self.lines()[0]["stdout"])


class TestSecretsNeverReachTheLog(LogToTemp):
    def test_the_live_token_is_removed_by_value(self):
        os.environ["GITHUB_TOKEN"] = "s3cret-token-value"
        runlog.event("worker.launch.end",
                     stderr="fatal: bad credentials for s3cret-token-value")
        written = json.dumps(self.lines())
        self.assertNotIn("s3cret-token-value", written)
        self.assertIn(runlog.REDACTED, written)

    def test_token_shapes_are_removed_even_when_never_held(self):
        """A worker can echo somebody else's token; value-redaction cannot see it."""
        for secret in ("ghp_" + "A" * 36, "github_pat_" + "B" * 22,
                       "Authorization: Bearer abcdef123456"):
            self.assertNotIn(secret.split()[-1], runlog.tail(f"log line {secret} end"))

    def test_redaction_reaches_command_lines_too(self):
        os.environ["GH_TOKEN"] = "gh-token-1234"
        rendered = runlog.command(["worker", "--token", "gh-token-1234"])
        self.assertNotIn("gh-token-1234", rendered)

    def test_the_prompt_is_elided_rather_than_stored(self):
        """Not a secret — long, identical every launch, and the reviewable part
        of an invocation is its shape (#96 was a wrong permission flag)."""
        rendered = runlog.command(["claude", "-p", "a very long prompt",
                                   "--allowedTools", "Bash(x)"], elide="a very long prompt")
        self.assertIn("<prompt>", rendered)
        self.assertNotIn("a very long prompt", rendered)
        self.assertIn("--allowedTools", rendered)


class TestOutputIsBounded(LogToTemp):
    def test_long_output_keeps_the_tail(self):
        """When a process dies, what it said last is what explains it."""
        text = "".join(f"line{i}\n" for i in range(5000))
        kept = runlog.tail(text)
        self.assertLessEqual(len(kept), runlog.MAX_FIELD_CHARS + 1)
        self.assertTrue(kept.endswith("line4999"))

    def test_the_file_rotates_rather_than_growing_without_bound(self):
        """A long-running loop appending forever is a loop without a bound."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("x" * (runlog.MAX_LOG_BYTES + 1))
        runlog.event("process.started", pid=1)
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertEqual(1, len(self.lines()))


class TestLoggingNeverCosts(LogToTemp):
    def test_an_unwritable_log_does_not_raise(self):
        os.environ["FACTORY_RUNTIME_LOG"] = "/proc/nonexistent/runtime.jsonl"
        runlog.event("worker.launch.start", story=104)  # must not raise

    def test_a_serialisation_failure_does_not_raise(self):
        class Unserialisable:
            def __str__(self):
                raise RuntimeError("boom")

        runlog.event("worker.launch.end", detail=Unserialisable())  # must not raise

    def test_nothing_is_ever_written_to_stdout(self):
        """One stdout line is one notification under this repository's monitor,
        so a chatty log there would page a human per event."""
        os.environ["FACTORY_RUNTIME_LOG_STDERR"] = "1"
        with mock.patch("sys.stdout") as stdout:
            runlog.event("worker.launch.start", story=104)
        stdout.write.assert_not_called()

    def test_stderr_mirroring_can_be_switched_off(self):
        os.environ["FACTORY_RUNTIME_LOG_STDERR"] = "0"
        self.assertFalse(runlog.stderr_enabled())


class TestNoReasoningIsRecorded(unittest.TestCase):
    """#104: observable worker output only. Nothing here asks an engine for its
    chain of thought, and a future edit that starts to must be visible."""

    def test_module_never_requests_or_stores_model_reasoning(self):
        with open(runlog.__file__, encoding="utf-8") as handle:
            body = handle.read().split('"""', 2)[-1]
        for leaked in ("thinking", "reasoning_content", "chain_of_thought",
                       "--verbose-reasoning"):
            self.assertNotIn(leaked, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
