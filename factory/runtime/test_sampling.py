import unittest
import sampling

SHA = "a" * 40
def pull(merged=True):
    return {"number": 7, "merged_at": "now" if merged else None, "head": {"sha": SHA}}

class SamplingTests(unittest.TestCase):
    def test_selection_nonselection_and_replay(self):
        chosen = sampling.select(pull(), [], lambda _: 0)
        self.assertTrue(chosen.selected)
        record = {"body": sampling.selection_comment(chosen)}
        self.assertTrue(sampling.select(pull(), [record], lambda _: 2).replay)
        self.assertFalse(sampling.select(pull(), [], lambda _: 2).selected)

    def test_premerge_and_duplicate_fail_closed(self):
        with self.assertRaises(sampling.SamplingError): sampling.select(pull(False), [])
        record = {"body": sampling.selection_comment(sampling.select(pull(), [], lambda _: 0))}
        with self.assertRaisesRegex(sampling.SamplingError, "duplicate"):
            sampling.select(pull(), [record, record])

    def test_canonical_decisions(self):
        base = {"author_association": "OWNER", "user": {"login": "owner"}}
        passed = dict(base, body="## Sampling audit\n\ndecision: pass\nseconds-spent: 12")
        self.assertEqual(sampling.classify(passed).verdict, "pass")
        found = dict(base, body="## Sampling audit\n\ndecision: findings\nseconds-spent: 2\n\n- repair")
        self.assertEqual(sampling.classify(found).findings, ("repair",))

    def test_bad_decisions_fail(self):
        bad = {"body": "## Sampling audit\n\ndecision: findings\nseconds-spent: 1",
               "author_association": "OWNER"}
        with self.assertRaises(sampling.SamplingError): sampling.classify(bad)
        bad["body"] = "## Sampling audit\n\ndecision: pass\nseconds-spent: 1"
        bad["author_association"] = "NONE"
        with self.assertRaises(sampling.SamplingError): sampling.classify(bad)

    def test_corrective_story_is_bounded_and_durable(self):
        audit = sampling.AuditDecision("findings", 2, "owner", ("repair",))
        value = sampling.corrective_story(212, 214, sampling.Selection(7, SHA, True), audit)
        self.assertIn("$20 / 45 min", value["body"])
        self.assertIn("sampling-correction:7", value["body"])
