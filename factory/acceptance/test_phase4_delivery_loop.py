import pathlib
import sys
import unittest

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import poller
import review_route
import review_link
import correction_context

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

    def test_story_71_retry_carries_human_correction_context_not_distractors(self):
        head = "9ef7b45c2f7f83ce6e7b0de2be4638229babf132"
        story = {"number": 71, "body": "### Attempt\n\n2\n"}
        pull = {"number": 74, "head": {"sha": head}}

        def record(identifier, created, body, association="OWNER"):
            return {"id": identifier, "created_at": created, "body": body,
                    "author_association": association}

        finding = record(
            1, "2026-08-27T02:31:53Z",
            "## Review findings\n\nThe real-Chrome check skips.\n\n"
            f"<!-- review-outcome:74:{head}:findings -->")

        def human(identifier, created, kind, evidence):
            return record(
                identifier, created,
                f"## {kind}\n\n{evidence}\n\nMahesh gave this decision in the active "
                "session; I transcribed it here.\n\n" + correction_context.marker(
                    kind=kind, story=71, pull_request=74, head=head))

        diagnosis = human(2, "2026-08-27T02:40:28Z", "human-review",
                          "The captured result is empty; wait for completion.")
        request = human(3, "2026-08-27T02:54:11Z", "request-changes",
                        "Replace dump-dom and fail rather than skip.")
        authorization = human(4, "2026-08-27T02:54:12Z", "retry-authorization",
                              "One final bounded correction attempt is authorized.")
        distractors = [
            record(5, "2026-08-27T02:45:00Z", "free-form instructions"),
            record(6, "2026-08-27T02:46:00Z", correction_context.marker(
                kind="human-review", story=99, pull_request=74, head=head)),
            record(7, "2026-08-27T02:47:00Z", correction_context.marker(
                kind="human-review", story=71, pull_request=74, head="a" * 40)),
            record(8, "2026-08-27T02:48:00Z", correction_context.marker(
                kind="human-review", story=71, pull_request=74, head=head), "NONE"),
        ]
        packet = correction_context.assemble(
            repository="maheshhbhat/retirement-withdrawal-planner", project=67,
            story=story, pull_request=pull,
            story_comments=[authorization, *distractors, finding, diagnosis],
            pull_comments=[request])
        self.assertEqual(
            ["review-findings", "human-review", "request-changes",
             "retry-authorization"],
            [item["kind"] for item in packet["records"]])
        self.assertEqual(["1", "2", "3", "4"],
                         [item["comment_id"] for item in packet["records"]])
        self.assertEqual(packet["digest"], correction_context.digest(packet))
        self.assertNotIn("free-form instructions", str(packet))

if __name__ == "__main__": unittest.main()
