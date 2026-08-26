import unittest
import operating_envelope as envelope


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.entries = [{"id": "OE-SCALE-1", "category": "representative-input",
                         "requirement": "$1M completes in two seconds",
                         "failure_condition": "browser exceeds two seconds"}]
        self.project = ("### Operating envelope\n\n"
                        "- OE-SCALE-1 | representative-input | $1M completes in two seconds "
                        "| FAIL WHEN: browser exceeds two seconds\n")

    def story(self, value=None, identifiers="OE-SCALE-1"):
        return ("### Operating-envelope obligations\n\n"
                f"digest: {value or envelope.digest(self.entries)}\n{identifiers}\n")

    def atomic_story(self):
        return self.story(identifiers=(
            "OE-SCALE-1 | STORY CHECK: representative model test exceeds two seconds"))

    def test_matching_obligations_return_full_contract(self):
        self.assertEqual(self.entries, envelope.obligations(self.story(), self.project))

    def test_story_local_check_is_returned_with_obligation(self):
        expected = [{**self.entries[0], "story_check":
                     "representative model test exceeds two seconds"}]
        self.assertEqual(expected, envelope.obligations(self.atomic_story(), self.project))

    def test_stale_digest_and_unknown_id_fail(self):
        with self.assertRaisesRegex(envelope.EnvelopeError, "stale"):
            envelope.obligations(self.story("0" * 64), self.project)
        with self.assertRaisesRegex(envelope.EnvelopeError, "unknown"):
            envelope.obligations(self.story(identifiers="OE-OTHER"), self.project)
