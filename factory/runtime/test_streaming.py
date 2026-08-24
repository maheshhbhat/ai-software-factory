import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import streaming


class StreamingTests(unittest.TestCase):
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
