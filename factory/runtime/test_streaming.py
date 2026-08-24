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


if __name__ == "__main__":
    unittest.main()
