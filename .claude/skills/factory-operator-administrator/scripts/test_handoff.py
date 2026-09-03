import json
import pathlib
import tempfile
import unittest
from unittest import mock

import handoff


class HandoffTests(unittest.TestCase):
    def test_write_records_bounded_state_without_process_commands(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(handoff, "HANDOFF", pathlib.Path(directory) / "handoff.json"), \
             mock.patch.object(handoff, "command", side_effect=["branch", "a" * 40]), \
             mock.patch.object(handoff, "processes", return_value={
                 "status": "available", "items": [{"pid": 7, "kind": "poller"}]}):
            args = handoff.parser().parse_args([
                "write", "--objective", "operate", "--status", "verified",
                "--next-action", "review", "--forbidden", "do not merge",
                "--project", "100", "--pr", "649"])
            self.assertEqual(0, handoff.write(args))
            value = json.loads(handoff.HANDOFF.read_text())
        self.assertEqual(1, value["schema_version"])
        self.assertEqual([{"pid": 7, "kind": "poller"}], value["processes"]["items"])
        self.assertNotIn("command", value["processes"]["items"][0])
        self.assertEqual(["do not merge"], value["forbidden"])

    def test_process_inspection_failure_is_recorded_without_crashing(self):
        with mock.patch.object(handoff.subprocess, "run", side_effect=PermissionError()):
            value = handoff.processes()
        self.assertEqual("unavailable", value["status"])
        self.assertEqual("PermissionError", value["reason"])
        self.assertEqual([], value["items"])

    def test_check_rejects_stale_git_identity(self):
        value = {key: "x" for key in handoff.REQUIRED}
        value.update({"schema_version": 1, "git": {"branch": "old", "commit": "old"}})
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(handoff, "HANDOFF", pathlib.Path(directory) / "handoff.json"), \
             mock.patch.object(handoff, "command", side_effect=["new", "new"]), \
             mock.patch.object(handoff, "processes", return_value=[]):
            handoff.HANDOFF.write_text(json.dumps(value))
            self.assertEqual(1, handoff.check(None))


if __name__ == "__main__":
    unittest.main()
