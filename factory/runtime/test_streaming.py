import json
import os
import pathlib
import sys
import tempfile
import subprocess
import unittest
from unittest import mock

import streaming


class StreamingTests(unittest.TestCase):
    def test_timeout_retains_output_and_stops_the_process_group(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"FACTORY_RUN_DIR": directory,
                             "FACTORY_RUNTIME_LOG_STDERR": "0"}, clear=True):
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                streaming.run(
                    [sys.executable, "-c",
                     "import time; print('started', flush=True); time.sleep(10)"],
                    cwd=directory, env={}, timeout=0.1, component="worker",
                    operation="engine-stream")
        self.assertIn("started", raised.exception.stdout)

    def test_each_engine_line_is_logged_with_credentials_redacted(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"FACTORY_RUN_DIR": directory,
                             "FACTORY_RUNTIME_LOG_STDERR": "0"}, clear=True):
            result = streaming.run(
                [sys.executable, "-c", "print('working ghp_1234567890abcdef')"],
                cwd=directory, env={}, timeout=5, component="reviewer",
                operation="engine-stream", story=20)
            rows = [json.loads(line) for line in pathlib.Path(
                directory, "operations.jsonl").read_text().splitlines()]
        self.assertEqual(0, result.returncode)
        self.assertEqual(1, len(rows))
        self.assertEqual("reviewer", rows[0]["component"])
        self.assertIn("[redacted]", rows[0]["engine_output_tail"])
        self.assertNotIn("ghp_1234567890abcdef", json.dumps(rows))

    def test_supplied_stdin_reaches_the_child_intact(self):
        """Story #676: the prompt provider adapters now pass via `input=`
        must actually reach the subprocess — it silently vanished before."""
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"FACTORY_RUN_DIR": directory,
                             "FACTORY_RUNTIME_LOG_STDERR": "0"}, clear=True):
            result = streaming.run(
                [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                cwd=directory, env={}, timeout=5, component="reviewer",
                operation="engine-stream", input="the exact prompt text")
        self.assertEqual(0, result.returncode)
        self.assertEqual("the exact prompt text", result.stdout)

    def test_large_stdin_payload_completes_without_deadlock(self):
        """The child writes enough stdout to fill a pipe buffer *before*
        reading stdin. If stdin were written sequentially rather than on
        its own concurrent thread, this would hang until the bounded
        timeout below turns it into a clear test failure instead of a
        wedged process."""
        big_input = "x" * 5_000_000
        script = (
            "import sys\n"
            "sys.stdout.write('y' * 2_000_000)\n"
            "sys.stdout.flush()\n"
            "data = sys.stdin.read()\n"
            "sys.stdout.write('|' + str(len(data)))\n"
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"FACTORY_RUN_DIR": directory,
                             "FACTORY_RUNTIME_LOG_STDERR": "0"}, clear=True):
            result = streaming.run(
                [sys.executable, "-c", script],
                cwd=directory, env={}, timeout=15, component="reviewer",
                operation="engine-stream", input=big_input)
        self.assertEqual(0, result.returncode)
        self.assertTrue(result.stdout.endswith(f"|{len(big_input)}"))

    def test_stdin_content_never_appears_in_operational_logs(self):
        marker = "UNIQUE_STDIN_MARKER_MUST_NEVER_BE_LOGGED_9f8e7d"
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"FACTORY_RUN_DIR": directory,
                             "FACTORY_RUNTIME_LOG_STDERR": "0"}, clear=True):
            result = streaming.run(
                [sys.executable, "-c",
                 "import sys; sys.stdin.read(); print('done')"],
                cwd=directory, env={}, timeout=5, component="reviewer",
                operation="engine-stream", input=marker)
            log_text = pathlib.Path(directory, "operations.jsonl").read_text()
        self.assertEqual(0, result.returncode)
        self.assertNotIn(marker, log_text)

    def test_timeout_and_cleanup_unchanged_when_stdin_is_supplied(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"FACTORY_RUN_DIR": directory,
                             "FACTORY_RUNTIME_LOG_STDERR": "0"}, clear=True):
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                streaming.run(
                    [sys.executable, "-c",
                     "import sys, time; print('started', flush=True); "
                     "sys.stdin.read(); time.sleep(10)"],
                    cwd=directory, env={}, timeout=0.5, component="worker",
                    operation="engine-stream", input="irrelevant stdin")
        self.assertIn("started", raised.exception.stdout)


if __name__ == "__main__":
    unittest.main()
