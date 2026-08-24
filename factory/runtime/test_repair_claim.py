import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import repair_claim


class RepairClaimTests(unittest.TestCase):
    def evidence(self, result="FAILED", exit_code=1):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = pathlib.Path(directory.name) / "process-events.jsonl"
        path.write_text(json.dumps({
            "event": "worker.launch.end", "repo": "o/r", "story": 20,
            "result": result, "exit": exit_code,
        }) + "\n", encoding="utf-8")
        return path

    def test_confirmed_nonzero_failure_is_accepted(self):
        event = repair_claim.confirmed_failure(self.evidence(), "o/r", 20)
        self.assertEqual((event["result"], event["exit"]), ("FAILED", 1))

    def test_timeout_or_ambiguous_outcome_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not a confirmed"):
            repair_claim.confirmed_failure(
                self.evidence("AMBIGUOUS", None), "o/r", 20)

    def test_missing_story_evidence_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no worker.launch.end"):
            repair_claim.confirmed_failure(self.evidence(), "o/r", 21)

    def test_successful_repair_calls_guarded_release(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "token"}), \
             mock.patch.object(repair_claim.dispatcher,
                               "release_definite_failure",
                               return_value=(True, "released")) as release:
            code = repair_claim.main([
                "--repo", "o/r", "--story", "20",
                "--evidence", str(self.evidence()), "--reason", "broken",
            ])
        self.assertEqual(0, code)
        self.assertEqual(20, release.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
