import unittest
from unittest import mock
from pathlib import Path
import subprocess

from factory.acceptance import rung1_live as live


class Rung1LiveTests(unittest.TestCase):
    def test_story_is_bounded_health_work(self):
        body = live.story_body(700)
        self.assertIn("#700", body)
        self.assertIn("runs/rung1/live_product/**", body)
        self.assertIn("make_server(host, port, build_sha)", body)
        self.assertIn("40 lowercase hexadecimal", body)

    def test_every_production_substitution_override_is_rejected(self):
        for name in (*live.FORBIDDEN, "FACTORY_WORKER_TEST_LAUNCH"):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, name):
                live.preflight_environment({name: "not-real"})

    def test_external_guard_only_replaces_unavailable_process_inspection(self):
        unavailable = subprocess.CompletedProcess([], 1, stdout=(
            "PASS  GitHub credential — available\n"
            "FAIL  no competing poller — process inspection unavailable\n"), stderr="")
        self.assertIn("owner reported no pgrep matches",
                      live.doctor_result(unavailable, True))
        with self.assertRaisesRegex(RuntimeError, "preflight failed"):
            live.doctor_result(unavailable, False)
        other = subprocess.CompletedProcess([], 1, stdout=(
            "FAIL  GitHub credential — unavailable\n"
            "FAIL  no competing poller — process inspection unavailable\n"), stderr="")
        with self.assertRaisesRegex(RuntimeError, "preflight failed"):
            live.doctor_result(other, True)

    def test_decisions_require_canonical_owner_headings(self):
        comments = [
            {"body": "## Plan approval\ndecision: approved", "authorAssociation": "OWNER",
             "createdAt": "2026-01-01T00:00:00Z", "url": "plan"},
            {"body": "## Acceptance\nresult: pass", "authorAssociation": "OWNER",
             "createdAt": "2026-01-01T00:01:00Z", "url": "accept"},
            {"body": "## Acceptance\nresult: pass", "authorAssociation": "COLLABORATOR"}]
        self.assertEqual(["plan-approval", "acceptance"],
                         [row["bell_type"] for row in live.decision_rows(comments)])

    def test_acceptance_freeze_rejects_summary_without_per_criterion_results(self):
        comments = [{"body": "## Acceptance\nresult: pass\nactor: @owner\nfollow-up: none",
                     "authorAssociation": "OWNER"}]
        with self.assertRaisesRegex(RuntimeError, "per-criterion"):
            live.acceptance_record(comments)

    def test_start_uses_only_poll_sh_external_entrypoint(self):
        source = Path(live.__file__).read_text(encoding="utf-8")
        self.assertIn('self.poller=subprocess.Popen(["sh",str(ROOT/"poll.sh")',
                      Path(live.base.__file__).read_text(encoding="utf-8"))
        self.assertNotIn("dispatcher.main(", source)
        self.assertNotIn("review_link.run(", source)
        self.assertNotIn("merge_gate.evaluate(", source)


if __name__ == "__main__": unittest.main()
