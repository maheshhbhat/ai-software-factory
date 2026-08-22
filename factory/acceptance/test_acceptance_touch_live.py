import json
import pathlib
import tempfile
import unittest
from unittest import mock

import acceptance_touch_live as live


class AcceptanceTouchLiveTests(unittest.TestCase):
    def test_prepare_uses_real_sequencer_and_never_posts_acceptance(self):
        source = pathlib.Path(live.__file__).read_text()
        prepare = source.split("def prepare", 1)[1].split("def records", 1)[0]
        self.assertIn("sequencer.run", prepare)
        self.assertNotIn("## Acceptance", prepare)

    def test_evidence_requires_one_transition_one_touch_and_zero_replay_writes(self):
        decision = {"body": "## Acceptance\n\nresult: pass\nactor: @maheshhbhat\n\n"
                             "- LIVE-01 — pass\n\nfollow-up: none\n",
                    "authorAssociation": "OWNER", "created_at": "2026-08-22T20:00:00Z",
                    "html_url": "https://example.test/comment"}
        state = {"accepted": False}

        class Client:
            def issue(self, _number):
                label = "project:accepted" if state["accepted"] else "project:awaiting-acceptance"
                return {"number": 310, "labels": [{"name": "type:project"}, {"name": label}]}
            def pages(self, path):
                if path.endswith("/comments"):
                    return [decision]
                return [{"event": "labeled", "label": {"name": "project:accepted"},
                         "created_at": "2026-08-22T20:01:00Z"}]

        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(live, "OUT", pathlib.Path(temp)), \
             mock.patch.object(live.runlog, "event") as event:
            touch = pathlib.Path(temp) / "live-touchlog.jsonl"
            fingerprint = live.continuation.acceptance_identity(decision)[0]
            touch.write_text(json.dumps({"project": "#304", "bell_type": "acceptance",
                                         "note": "unrelated receipt"}) + "\n")

            def first_run(*_args, **_kwargs):
                if state["accepted"]:
                    return []
                state["accepted"] = True
                with touch.open("a") as handle:
                    handle.write(json.dumps({"project": "#310", "bell_type": "acceptance",
                        "note": f"acceptance-fingerprint:{fingerprint}"}) + "\n")
                return [mock.Mock(number=298), mock.Mock(number=310)]

            with mock.patch.object(live.continuation, "run", side_effect=first_run):
                evidence = live.consume(Client(), "token", 310)
        self.assertEqual(evidence["fixture"]["after"], "project:accepted")
        self.assertEqual(evidence["replay"], {"new_entries": 0, "transitions": 0})
        self.assertEqual(evidence["transition"], {"target_transitions": 1,
                                                   "unrelated_transitions_in_pass": 1})
        event.assert_called_once()

    def test_delivery_recorder_fails_until_story_pr_and_both_checks_are_terminal(self):
        issue = {"state": "CLOSED", "labels": [{"name": "story:merged"}]}
        good = {"state": "MERGED", "mergedAt": "2026-08-22T20:00:00Z",
                "statusCheckRollup": [
                    {"name": "merge-gate", "conclusion": "SUCCESS"},
                    {"name": "merge-gate-surface", "conclusion": "SUCCESS"}]}
        def completed(value):
            return mock.Mock(stdout=json.dumps(value), returncode=0)
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(live, "OUT", pathlib.Path(temp)), \
             mock.patch.object(live.subprocess, "run",
                               side_effect=[completed(issue), completed(good)]):
            record = live.record_delivery(295, 297)
        self.assertEqual(record["checks"], {"merge-gate": "SUCCESS",
                                            "merge-gate-surface": "SUCCESS"})

        bad = {**good, "statusCheckRollup": good["statusCheckRollup"][:1]}
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(live, "OUT", pathlib.Path(temp)), \
             mock.patch.object(live.subprocess, "run",
                               side_effect=[completed(issue), completed(bad)]), \
             self.assertRaisesRegex(RuntimeError, "both required checks"):
            live.record_delivery(295, 297)

    def test_records_rejects_malformed_jsonl(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "touch.jsonl"
            path.write_text("not-json\n")
            with self.assertRaises(json.JSONDecodeError):
                live.records(path)

    def test_prepare_replay_does_not_create_more_fixtures(self):
        project = {"number": 310, "body": live.MARKER,
                   "labels": [{"name": "type:project"},
                              {"name": "project:awaiting-acceptance"}]}
        project["body"] = live.project_body(311)
        child = {"number": 311, "labels": [{"name": "story:merged"}]}
        client = mock.Mock()
        client.issue.side_effect = [child, project, project]
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(live, "OUT", pathlib.Path(temp)), \
             mock.patch.object(live, "find_project", return_value=project), \
             mock.patch.object(live.sequencer, "run") as sequence:
            result = live.prepare(client, "token")
        self.assertTrue(result["replay"])
        client.api.assert_not_called(); sequence.assert_not_called()


if __name__ == "__main__":
    unittest.main()
