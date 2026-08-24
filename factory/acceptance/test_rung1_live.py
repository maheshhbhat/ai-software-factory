import unittest
from unittest import mock
from pathlib import Path
import subprocess
import tempfile
import json
from types import SimpleNamespace

from factory.acceptance import rung1_live as live


class Rung1LiveTests(unittest.TestCase):
    def test_story_is_bounded_health_work(self):
        body = live.story_body(700)
        self.assertIn("#700", body)
        self.assertIn("runs/rung1/live_product/project-700/app.py", body)
        self.assertIn("runs/rung1/live_product/project-700/**", body)
        self.assertIn("make_server(host, port, build_sha)", body)
        self.assertIn("40 lowercase hexadecimal", body)

    def test_each_project_gets_a_distinct_product_target(self):
        self.assertNotEqual(live.product_path(700), live.product_path(701))

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

    def test_terminal_worker_launch_failure_is_detected_from_durable_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "process-events.jsonl"
            path.write_text(json.dumps({
                "event": "worker.outcome", "story": 701,
                "result": "NO_WORKER_LAUNCHED",
                "detail": "every eligible worker failed"}) + "\n")
            failure = live.terminal_worker_failure(directory, [701])
        self.assertIn("Story #701", failure)
        self.assertIn("NO_WORKER_LAUNCHED", failure)

    def test_nonterminal_or_unrelated_worker_events_do_not_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "process-events.jsonl"
            path.write_text(json.dumps({
                "event": "worker.outcome", "story": 999,
                "result": "NO_WORKER_LAUNCHED"}) + "\n")
            self.assertIsNone(live.terminal_worker_failure(directory, [701]))

    def test_foreign_dispatch_is_detected_from_process_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "process-events.jsonl"
            path.write_text(json.dumps({
                "event": "dispatch.received", "project": 699, "story": 700,
            }) + "\n")
            failure = live.foreign_dispatch(directory, 701, [702])
        self.assertIn("Project #699", failure)
        self.assertIn("Story #700", failure)

    def test_intended_dispatch_does_not_trigger_foreign_alarm(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "process-events.jsonl"
            path.write_text(json.dumps({
                "event": "dispatch.received", "project": 701, "story": 702,
            }) + "\n")
            self.assertIsNone(live.foreign_dispatch(directory, 701, [702]))

    def test_operator_progress_names_project_story_and_lifecycle(self):
        summary = live.progress_summary({
            "project_state": "project:active",
            "stories": [{"number": 702,
                         "walk": ["story:ready", "story:claimed"]}],
        })
        self.assertEqual(
            "Project: project:active; Story #702: story:claimed", summary)

    def test_fixture_dry_run_requires_exact_story_selection(self):
        output = ("Dispatcher — 1 issue(s) considered, WIP 0/2\n"
                  "Selected (would claim, in order): #702\n")
        self.assertEqual((True, "normal dry-run selected only Story #702"),
                         live.fixture_selection(output, 701, 702))

    def test_fixture_dry_run_rejects_full_capacity_or_wrong_story(self):
        full = "Dispatcher — 1 issue(s) considered, WIP 2/2\nCapacity exhausted"
        self.assertFalse(live.fixture_selection(full, 701, 702)[0])
        wrong = "Selected (would claim, in order): #999\n"
        passed, detail = live.fixture_selection(wrong, 701, 702)
        self.assertFalse(passed)
        self.assertIn("Story #702", detail)

    def test_failed_run_artifacts_are_retired_after_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            class FakeRun:
                story = [702]
                args = SimpleNamespace(project=701)
                commitment = 700
                tmp = Path(directory)
                def api(self, path, method="GET", payload=None):
                    calls.append((path, method, payload))
                    if path.startswith("/pulls?"):
                        return [{"number": 703, "body": "Story: #702"}]
                    if path == "/issues/702" and method == "GET":
                        return {"state": "open", "body":
                                live.MARKER.replace("PROJECT", "701")}
                    if path == "/issues/701" and method == "GET":
                        return {"state": "open",
                                "title": "[Project] Phase 5 Rung 1 clean test",
                                "labels": [{"name": "type:project"}],
                                "body": "### Roadmap commitment\n\n#700\n"}
                    if path == "/issues/700" and method == "GET":
                        return {"state": "open",
                                "title": "[Commitment] Isolate Phase 5 Rung 1 clean test",
                                "labels": [{"name": "type:roadmap-commitment"}]}
                    return {}
                def persist(self, _state=None):
                    pass
            evidence = {"passed": False}
            retired = live.cleanup_failed_run(FakeRun(), evidence)
        self.assertEqual(["Pull request #703", "Story #702", "Project #701",
                          "Commitment #700"], retired)
        self.assertIn(("/pulls/703", "PATCH", {"state": "closed"}), calls)
        self.assertIn(("/issues/702", "PATCH",
                       {"state": "closed", "state_reason": "not_planned"}), calls)
        self.assertIn(("/issues/701", "PATCH",
                       {"state": "closed", "state_reason": "not_planned"}), calls)
        self.assertIn(("/issues/700", "PATCH",
                       {"state": "closed", "state_reason": "not_planned"}), calls)
        self.assertEqual("complete",
                         evidence["failed_artifact_retirement"]["status"])

    def test_passing_run_never_retires_artifacts(self):
        run = SimpleNamespace(story=[702])
        self.assertEqual([], live.cleanup_failed_run(run, {"passed": True}))

    def test_accepted_run_retires_its_one_run_commitment(self):
        calls = []
        class FakeRun:
            def api(self, path, method="GET", payload=None):
                calls.append((path, method, payload))
                if method == "GET":
                    return {"state": "open",
                            "title": "[Commitment] Isolate Phase 5 Rung 1 accepted",
                            "labels": [{"name": "type:roadmap-commitment"}]}
                return {}
        evidence = {"passed": True, "commitment": 700,
                    "acceptance": {"result": "pass"}}
        self.assertEqual(["Commitment #700"],
                         live.cleanup_accepted_run(FakeRun(), evidence))
        self.assertIn(("/issues/700", "PATCH",
                       {"state": "closed", "state_reason": "completed"}), calls)

    def test_rung1_installs_interrupt_preservation_and_specific_diagnostic(self):
        source = Path(live.__file__).read_text(encoding="utf-8")
        self.assertIn("except KeyboardInterrupt", source)
        self.assertIn("## Rung 1 UAT diagnostic", source)
        self.assertIn("runs/rung1/{self.run}/evidence.json", source)
        self.assertNotIn("## Two-story E2E diagnostic", source)


if __name__ == "__main__": unittest.main()
