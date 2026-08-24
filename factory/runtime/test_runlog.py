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
import observability


class LogToTemp(unittest.TestCase):
    """Every test writes to its own file and never to stderr."""

    def setUp(self):
        self._env = dict(os.environ)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        os.environ["FACTORY_RUN_DIR"] = self.dir.name
        self.path = str(observability.stream_path("process"))
        os.environ["FACTORY_RUNTIME_LOG_STDERR"] = "0"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def lines(self) -> list[dict]:
        records = []
        for kind in ("process", "telemetry"):
            records.extend(observability.read_records(kind))
        return sorted(records, key=lambda row: row["timestamp"])


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
        self.assertTrue(record["timestamp"].endswith("Z"))
        self.assertEqual("process", record["record_type"])

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
            handle.write("x" * (observability.MAX_STREAM_BYTES + 1))
        runlog.event("process.started", pid=1)
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertEqual(1, len(self.lines()))


class TestLoggingNeverCosts(LogToTemp):
    def test_an_unwritable_log_does_not_raise(self):
        os.environ["FACTORY_RUN_DIR"] = "/proc/nonexistent"
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


class TestEngineUsageIsCapturedNotEstimated(LogToTemp):
    """Project #322 criterion 5: cost per accepted story must be computed from
    what the engines reported, and an engine that reported nothing must be
    visible as such rather than quietly counted as free."""

    def test_a_reported_usage_is_recorded_as_the_engine_gave_it(self):
        output = json.dumps({"type": "result", "total_cost_usd": 0.42,
                             "num_turns": 3,
                             "usage": {"input_tokens": 1200, "output_tokens": 85}})
        record = runlog.engine_usage(story=337, engine="claude", phase="worker",
                                     output=output)
        self.assertTrue(record["usage_reported"])
        self.assertEqual({"input_tokens": 1200, "output_tokens": 85,
                          "total_cost_usd": 0.42, "num_turns": 3},
                         record["usage"])
        self.assertEqual(record, self.lines()[0])

    def test_reporting_nothing_is_distinguishable_from_reporting_zero(self):
        """The distinction the whole KPI rests on. A silent engine must never
        be summed as a zero — that would make an unmeasured run look free."""
        silent = runlog.engine_usage(story=337, engine="codex", phase="worker",
                                     output="delivered the story, said nothing else")
        zero = runlog.engine_usage(story=337, engine="claude", phase="worker",
                                   output=json.dumps({"usage": {"output_tokens": 0},
                                                      "total_cost_usd": 0.0}))
        self.assertFalse(silent["usage_reported"])
        self.assertEqual(runlog.USAGE_UNAVAILABLE, silent["usage"])
        self.assertNotIsInstance(silent["usage"], dict)
        self.assertTrue(zero["usage_reported"])
        self.assertEqual({"output_tokens": 0, "total_cost_usd": 0.0}, zero["usage"])
        self.assertIsNone(runlog.parse_usage("said nothing"))
        self.assertEqual({"output_tokens": 0},
                         runlog.parse_usage('{"usage":{"output_tokens":0}}'))

    def test_usage_sums_per_story_from_the_runtime_log_alone(self):
        """No second source: the record names the story and carries the numbers."""
        runlog.engine_usage(story=337, engine="claude", phase="worker",
                            output=json.dumps({"usage": {"output_tokens": 10}}))
        runlog.engine_usage(story=337, engine="claude", phase="review",
                            output=json.dumps({"usage": {"output_tokens": 4}}))
        runlog.engine_usage(story=338, engine="claude", phase="worker",
                            output=json.dumps({"usage": {"output_tokens": 99}}))
        runlog.engine_usage(story=337, engine="codex", phase="worker", output="quiet")
        mine = [r for r in self.lines()
                if r["metric"] == runlog.USAGE_EVENT and r["story"] == 337]
        self.assertEqual(14, sum(r["usage"]["output_tokens"]
                                 for r in mine if r["usage_reported"]))
        self.assertEqual(1, len([r for r in mine if not r["usage_reported"]]))
        self.assertEqual({"worker", "review"}, {r["phase"] for r in mine})
        self.assertEqual({"claude", "codex"}, {r["engine"] for r in mine})

    def test_a_streaming_engine_is_read_from_its_final_totals(self):
        stream = "\n".join([json.dumps({"type": "assistant",
                                        "usage": {"output_tokens": 5}}),
                            json.dumps({"type": "result",
                                        "usage": {"output_tokens": 31}})])
        self.assertEqual({"output_tokens": 31}, runlog.parse_usage(stream))

    def test_a_claim_that_is_not_a_number_is_not_recorded_as_one(self):
        """Engine output is text the engine controls. Only declared numeric
        fields become fields a report will add up."""
        record = runlog.engine_usage(
            story=337, engine="claude", phase="worker",
            output=json.dumps({"usage": {"input_tokens": "lots", "spent": "$3"},
                               "note": "ghp_" + "C" * 36}))
        self.assertFalse(record["usage_reported"])
        written = json.dumps(self.lines())
        self.assertNotIn("lots", written)
        self.assertNotIn("$3", written)
        self.assertNotIn("ghp_" + "C" * 36, written)

    def test_unparsable_engine_output_still_produces_a_record(self):
        record = runlog.engine_usage(story=337, engine="claude", phase="review",
                                     output="{not json at all")
        self.assertFalse(record["usage_reported"])
        self.assertEqual(1, len(self.lines()))

    def test_no_rate_ceiling_or_conversion_is_introduced(self):
        """`product.md` leaves the monetary cost-per-story ceiling to the owner.
        Capture records what an engine said; it never prices what it counted."""
        with open(runlog.__file__, encoding="utf-8") as handle:
            body = handle.read().split('"""', 2)[-1]
        for invented in ("price", "pricing", "per_token", "usd_per", "rate_per",
                         "budget", "ceiling", "estimate"):
            self.assertNotIn(invented, body.lower())


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


class TestEngineCredentialsAreValueRedacted(unittest.TestCase):
    """#330 — engine stderr rides into errors and launch records now, and an
    engine refused mid-authentication is exactly the case that echoes its
    credential back."""

    def test_the_oauth_token_and_api_key_are_removed_exactly(self):
        import runlog
        from unittest import mock
        secrets = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "a" * 40,
                   "ANTHROPIC_API_KEY": "sk-ant-api03-" + "b" * 40}
        with mock.patch.dict(os.environ, secrets):
            for value in secrets.values():
                cleaned = runlog.redact(f"engine said: token {value} rejected")
                self.assertNotIn(value, cleaned)
                self.assertIn(runlog.REDACTED, cleaned)
