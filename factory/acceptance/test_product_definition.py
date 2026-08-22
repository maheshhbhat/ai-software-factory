import hashlib
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
PRODUCT = ROOT / "product.md"
APPROVED_SHA256 = "4dc340915bec0575cc5a9e4d02e8126c4a5ded87b08dd4126f8170ceafec2cca"

# These are the owner-approved PD-01 through PD-05 promises. Keeping the map
# explicit makes a removed promise fail by name instead of merely changing a
# broad document snapshot.
PD_REQUIREMENTS = {
    "PD-01": ("# AI Software Factory — Product Definition", "## Purpose",
              "## Primary user", "## Product outcomes"),
    "PD-02": ("Missing access fails loudly", "before any partial planning or delivery write"),
    "PD-03": ("Rung 1 proves", "Rung 2 applies", "notifications-extraction strangler",
              "touches by classification", "autonomous merges divided by total stories",
              "actual cost per accepted story", "cycle time from project plan approval"),
    "PD-04": ("cost-per-story ceiling is intentionally unresolved here",
              "before approving the Rung 3 plan"),
    "PD-05": ("Coverage is measured reproducibly", "enforced as a percentage threshold",
              "Workers cannot weaken", "The worker never merges or approves its own work",
              "Phase work advances only after the preceding bounded project is accepted"),
}


def missing_promises(text):
    return {criterion: [phrase for phrase in phrases if phrase not in text]
            for criterion, phrases in PD_REQUIREMENTS.items()
            if any(phrase not in text for phrase in phrases)}


class ProductDefinitionTests(unittest.TestCase):
    def test_pd_01_through_pd_05_match_the_approved_product(self):
        text = PRODUCT.read_text(encoding="utf-8")
        self.assertEqual({}, missing_promises(text))
        self.assertEqual(APPROVED_SHA256, hashlib.sha256(PRODUCT.read_bytes()).hexdigest())

    def test_each_required_section_boundary_kpi_and_constraint_can_fail(self):
        original = PRODUCT.read_text(encoding="utf-8")
        for criterion, phrases in PD_REQUIREMENTS.items():
            for phrase in phrases:
                with self.subTest(criterion=criterion, removed=phrase):
                    mutated = original.replace(phrase, "", 1)
                    self.assertIn(criterion, missing_promises(mutated))

    def test_phase_5_has_no_coverage_threshold_and_leaves_rung_3_cost_unresolved(self):
        text = PRODUCT.read_text(encoding="utf-8")
        phase5 = text.split("## Phase 5 measurement policy", 1)[1].split("## Non-goals", 1)[0]
        self.assertNotRegex(phase5, r"coverage.{0,40}(?:>=|at least|minimum)\s*\d+%")
        self.assertIn("cost-per-story ceiling is intentionally unresolved here", phase5)


if __name__ == "__main__":
    unittest.main()
