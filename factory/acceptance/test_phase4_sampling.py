import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "runtime"))
import sampling

SHA = "a" * 40

class Hub:
    def __init__(self):
        self.issues = {}
        self.comments = {30: []}
        self.next = 100
        self.writes = []
        self.issues[20] = {"number": 20, "body": "### Project\n\n#212\n\n### Scope\n\nproduct/**\n",
                           "labels": [{"name": "story:merged"}]}
    def pages(self, path):
        if path == "/issues?state=all": return list(self.issues.values())
        number = int(path.split("/")[2]); return self.comments.setdefault(number, [])
    def api(self, path, method="GET", value=None):
        if method == "GET": return self.issues[int(path.split("/")[2])]
        self.writes.append((method, path, value))
        if path == "/issues":
            number = self.next; self.next += 1
            issue = {**value, "number": number}
            issue["labels"] = [{"name": x} for x in value["labels"]]
            self.issues[number] = issue; self.comments[number] = []
            return issue
        number = int(path.split("/")[2])
        if path.endswith("/comments"):
            comment = {"body": value["body"]}; self.comments[number].append(comment); return comment
        updated = dict(value)
        if "labels" in updated:
            updated["labels"] = [{"name": x} for x in updated["labels"]]
        self.issues[number].update(updated); return self.issues[number]

def pull():
    return {"number": 30, "merged_at": "now", "head": {"sha": SHA}, "body": "Story: #20\n"}

class SamplingLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.touchlog = pathlib.Path(self.temp.name) / "touchlog.jsonl"
        patch = mock.patch.dict(os.environ, {"FACTORY_TOUCHLOG_FILE": str(self.touchlog)})
        patch.start(); self.addCleanup(patch.stop)

    def touches(self):
        if not self.touchlog.exists(): return []
        return [json.loads(line) for line in self.touchlog.read_text().splitlines()]

    def test_unselected_has_no_bell_and_replay_no_write(self):
        hub = Hub()
        self.assertEqual(sampling.process(hub, pull(), draw=lambda _: 2), "unselected")
        count = len(hub.writes)
        self.assertEqual(sampling.process(hub, pull(), draw=lambda _: 0), "unselected")
        self.assertEqual(len(hub.writes), count)
        self.assertEqual(self.touches(), [])

    def test_selected_pass_closes_once_and_logs_one_touch(self):
        hub = Hub()
        self.assertEqual(sampling.process(hub, pull(), draw=lambda _: 0), "awaiting:100")
        hub.comments[100].append({"body": "## Sampling audit\n\ndecision: pass\nseconds-spent: 9",
                                  "author_association": "OWNER", "user": {"login": "owner"}})
        self.assertEqual(sampling.process(hub, pull()), "pass")
        touch = sampling.touch_marker(30, SHA)
        self.assertEqual(sum(touch in x["body"] for x in hub.comments[100]), 1)
        self.assertEqual(len(self.touches()), 1)
        self.assertEqual(self.touches()[0]["project"], "#212")
        self.assertEqual(self.touches()[0]["story"], "#20")
        self.assertEqual(self.touches()[0]["bell_type"], "sampling")
        self.assertEqual(self.touches()[0]["classification"], "audit")
        before = len(hub.writes)
        self.assertEqual(sampling.process(hub, pull()), "pass")
        self.assertEqual(len(hub.writes), before)
        self.assertEqual(len(self.touches()), 1)

    def test_replay_after_touch_append_but_before_marker_appends_nothing(self):
        hub = Hub(); sampling.process(hub, pull(), draw=lambda _: 0)
        hub.comments[100].append({"body": "## Sampling audit\n\ndecision: pass\nseconds-spent: 9",
                                  "author_association": "OWNER", "user": {"login": "owner"}})
        sampling.process(hub, pull())
        marker = sampling.touch_marker(30, SHA)
        hub.comments[100] = [x for x in hub.comments[100] if marker not in x.get("body", "")]
        sampling.process(hub, pull())
        self.assertEqual(len(self.touches()), 1)
        self.assertEqual(sum(marker in x.get("body", "") for x in hub.comments[100]), 1)

    def test_old_github_marker_without_canonical_entry_is_backfilled_once(self):
        hub = Hub(); sampling.process(hub, pull(), draw=lambda _: 0)
        hub.comments[100].extend([
            {"body": "## Sampling audit\n\ndecision: pass\nseconds-spent: 9",
             "author_association": "OWNER", "user": {"login": "owner"}},
            {"body": sampling.touch_marker(30, SHA)},
        ])
        self.assertEqual(sampling.process(hub, pull()), "pass")
        self.assertEqual(len(self.touches()), 1)
        self.assertEqual(sampling.process(hub, pull()), "pass")
        self.assertEqual(len(self.touches()), 1)

    def test_findings_create_one_ready_bounded_correction(self):
        hub = Hub(); sampling.process(hub, pull(), draw=lambda _: 0)
        hub.comments[100].append({"body": "## Sampling audit\n\ndecision: findings\nseconds-spent: 4\n\n- fix defect",
                                  "author_association": "OWNER", "user": {"login": "owner"}})
        self.assertEqual(sampling.process(hub, pull()), "findings")
        corrections = [x for x in hub.issues.values() if "sampling-correction" in x.get("body", "")]
        self.assertEqual(len(corrections), 1)
        self.assertIn("story:ready", {x["name"] for x in corrections[0]["labels"]})
        self.assertIn("product/**", corrections[0]["body"])
        sampling.process(hub, pull())
        self.assertEqual(len([x for x in hub.issues.values()
                             if "sampling-correction" in x.get("body", "")]), 1)

if __name__ == "__main__": unittest.main()
