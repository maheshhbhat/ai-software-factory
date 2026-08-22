import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

import append


class AtomicMarkerTests(unittest.TestCase):
    def arguments(self, path, marker="decision:abc"):
        return ["--project", "#1", "--bell-type", "acceptance",
                "--classification", "decision", "--seconds-spent", "0",
                "--actor", "@owner", "--note", marker, "--file", str(path),
                "--unique-note-marker", marker]

    def test_repeated_marker_appends_exactly_once(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "touchlog.jsonl"
            self.assertEqual(append.main(self.arguments(path)), 0)
            self.assertEqual(append.main(self.arguments(path)), 0)
            self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_preexisting_duplicate_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "touchlog.jsonl"
            row = {"note": "decision:abc"}
            path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
            self.assertEqual(append.main(self.arguments(path)), 1)

    def test_concurrent_processes_append_one_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "touchlog.jsonl"
            command = [sys.executable, str(pathlib.Path(append.__file__)),
                       *self.arguments(path)]
            first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(first.communicate()[0], b"")
            self.assertEqual(second.communicate()[0], b"")
            self.assertEqual((first.returncode, second.returncode), (0, 0))
            self.assertEqual(len(path.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
