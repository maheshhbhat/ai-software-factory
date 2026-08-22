import pathlib
import sys
import unittest

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import poller
import review_route
import review_link

class DeliveryLoopAcceptance(unittest.TestCase):
    def test_reject_retry_same_pr_new_head_then_advisory_approval(self):
        old, new = "a" * 40, "b" * 40
        pull = {"number": 50, "state": "open", "draft": False,
                "body": "Story: #218\n", "head": {"sha": old}}
        story = {"number": 218, "labels": [{"name": "story:in-review"}]}
        self.assertEqual(review_route.target(pull, story, []).head, old)
        findings = {"body": review_route.marker(50, old, "findings")}
        pull["head"]["sha"] = new
        self.assertEqual(review_route.target(pull, story, [findings]).head, new)
        approval = {"body": review_route.marker(50, new, "approval")}
        self.assertTrue(review_link.exact_head_approved(pull, [findings, approval]))
        self.assertFalse(review_link.exact_head_approved(
            {**pull, "head": {"sha": "c" * 40}}, [findings, approval]))
        self.assertEqual(pull["number"], 50)

if __name__ == "__main__": unittest.main()
