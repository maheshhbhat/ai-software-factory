import pathlib
import sys
import unittest

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
    def test_unselected_has_no_bell_and_replay_no_write(self):
        hub = Hub()
        self.assertEqual(sampling.process(hub, pull(), draw=lambda _: 2), "unselected")
        count = len(hub.writes)
        self.assertEqual(sampling.process(hub, pull(), draw=lambda _: 0), "unselected")
        self.assertEqual(len(hub.writes), count)

    def test_selected_pass_closes_once_and_logs_one_touch(self):
        hub = Hub()
        self.assertEqual(sampling.process(hub, pull(), draw=lambda _: 0), "awaiting:100")
        hub.comments[100].append({"body": "## Sampling audit\n\ndecision: pass\nseconds-spent: 9",
                                  "author_association": "OWNER", "user": {"login": "owner"}})
        self.assertEqual(sampling.process(hub, pull()), "pass")
        touch = sampling.touch_marker(30, SHA)
        self.assertEqual(sum(touch in x["body"] for x in hub.comments[100]), 1)
        before = len(hub.writes)
        self.assertEqual(sampling.process(hub, pull()), "pass")
        self.assertEqual(len(hub.writes), before)

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
