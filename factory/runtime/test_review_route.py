import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import review_route as rr


def pull(head="a" * 40, body="Story: #215\n", state="open", draft=False):
    return {"number": 8, "body": body, "state": state, "draft": draft,
            "head": {"sha": head}}


def story(labels=("type:story", "story:in-review")):
    return {"number": 215, "labels": [{"name": x} for x in labels]}


class ReviewRouteTests(unittest.TestCase):
    def test_initial_head_routes_once(self):
        self.assertEqual(rr.target(pull(), story(), []),
                         rr.ReviewTarget(215, 8, "a" * 40))
        comment = {"body": rr.marker(8, "a" * 40, "approval")}
        self.assertIsNone(rr.target(pull(), story(), [comment]))

    def test_new_head_routes_after_prior_head(self):
        old = {"body": rr.marker(8, "a" * 40, "findings")}
        self.assertEqual(rr.target(pull("b" * 40), story(), [old]).head, "b" * 40)

    def test_duplicate_current_head_outcomes_fail_closed(self):
        comments = [{"body": rr.marker(8, "a" * 40, verdict)}
                    for verdict in ("findings", "approval")]
        with self.assertRaisesRegex(rr.RouteError, "duplicate"):
            rr.target(pull(), story(), comments)

    def test_malformed_or_wrong_state_does_not_advance(self):
        with self.assertRaises(rr.RouteError):
            rr.target(pull(head="not-sha"), story(), [])
        self.assertIsNone(rr.target(pull(state="closed"), story(), []))
        self.assertIsNone(rr.target(pull(), story(("type:story", "story:ready")), []))

    def test_story_link_is_exact_and_unique(self):
        for body in ("Story #215", "Story: #215\nStory: #215\n", ""):
            with self.assertRaises(rr.RouteError):
                rr.story_number(pull(body=body))
