#!/usr/bin/env python3
import pathlib, sys, unittest, urllib.error
from unittest import mock
HERE=pathlib.Path(__file__).resolve().parent; sys.path.insert(0,str(HERE))
import two_story_real as ts

def item(n=1, **kw):
    value={"number":n,"pull_count":1,"merged":True,"closed":True,
           "walk":["story:ready","story:claimed","story:in-review","story:merged"],
           "claimed_at":f"2026-01-0{n}T01:00:00Z","merged_at":f"2026-01-0{n}T02:00:00Z",
           "pull":n+100,"head":f"head-{n}","exact_approval":True,
           "checks":["merge-gate","merge-gate-surface"],
           "usage_records":[{"usage":{"input_tokens":100,"output_tokens":10}}],
           "review_timing":{"total_seconds":18.0,"engine_seconds":12.0,
                            "stages":{"review.preparing":"t0","review.clone.started":"t1",
                                      "review.clone.finished":"t2","review.engine.started":"t3",
                                      "review.engine.finished":"t4","review.publishing":"t5",
                                      "review.published":"t6"}}}
    value.update(kw); return value
def ledger(**kw):
    value={"stories":[item(1),item(2)],"project_state":"project:awaiting-acceptance","replay_changed":False,
           "receipts":[{"bell_type":"plan-approval"}],
           "observability":{"valid":True,"stories":[
               {"story":1,"events":sorted(ts.TRACE_EVENTS),"trace_ids":["a"],"components":sorted(ts.LIVE_COMPONENTS),"heartbeat":True},
               {"story":2,"events":sorted(ts.TRACE_EVENTS),"trace_ids":["b"],"components":sorted(ts.LIVE_COMPONENTS),"heartbeat":True}]}}
    value.update(kw); return value

class Body(unittest.TestCase):
    def test_two_scopes_and_dependency(self):
        a=ts.story_body("r",375,1); b=ts.story_body("r",375,2,401)
        self.assertIn("### Depends-on\n\nnone",a); self.assertIn("### Depends-on\n\n#401",b)
        self.assertIn("story-1/**",a); self.assertIn("story-2/**",b)
    def test_project_story_list_uses_the_sequencer_dialect(self):
        self.assertEqual("#376\n#377",ts.story_list([376,377]))
        self.assertNotIn("-",ts.story_list([376,377]))
    def test_substitutions_are_refused(self):
        self.assertEqual(["FACTORY_DELIVERY_MODEL_CMD"],ts.forbidden_overrides({"FACTORY_DELIVERY_MODEL_CMD":"stub","PATH":"x"}))

class Verdict(unittest.TestCase):
    def test_complete_run_passes(self): self.assertTrue(ts.verdict(ledger())[0])
    def test_abort_fails(self): self.assertFalse(ts.verdict(ledger(aborted="timeout"))[0])
    def test_exactly_two_required(self): self.assertFalse(ts.verdict(ledger(stories=[item()]))[0])
    def test_duplicate_pr_fails(self): self.assertFalse(ts.verdict(ledger(stories=[item(pull_count=2),item(2)]))[0])
    def test_incomplete_lifecycle_fails(self): self.assertFalse(ts.verdict(ledger(stories=[item(closed=False),item(2)]))[0])
    def test_exact_head_approval_required(self): self.assertFalse(ts.verdict(ledger(stories=[item(exact_approval=False),item(2)]))[0])
    def test_bounded_observable_review_required(self):
        self.assertFalse(ts.verdict(ledger(stories=[item(review_timing={}),item(2)]))[0])
        self.assertFalse(ts.verdict(ledger(stories=[item(review_timing={"total_seconds":60.1}),item(2)]))[0])
    def test_both_checks_required(self): self.assertFalse(ts.verdict(ledger(stories=[item(checks=["merge-gate"]),item(2)]))[0])
    def test_usage_required(self): self.assertFalse(ts.verdict(ledger(stories=[item(usage_records=[]),item(2)]))[0])
    def test_dependency_order_required(self):
        second=item(2,claimed_at="2026-01-01T01:30:00Z")
        self.assertFalse(ts.verdict(ledger(stories=[item(),second]))[0])
    def test_project_completion_required(self): self.assertFalse(ts.verdict(ledger(project_state="project:active"))[0])
    def test_required_receipt_must_exist_once(self):
        self.assertFalse(ts.verdict(ledger(receipts=[]))[0]); self.assertFalse(ts.verdict(ledger(receipts=[{"bell_type":"plan-approval"}]*2))[0])
    def test_replay_must_be_inert(self): self.assertFalse(ts.verdict(ledger(replay_changed=True))[0])
    def test_observability_and_one_trace_are_required(self):
        self.assertFalse(ts.verdict(ledger(observability={}))[0])
        broken=ledger(); broken["observability"]["stories"][0]["trace_ids"]=["a","b"]
        self.assertFalse(ts.verdict(broken)[0])

class ReplayState(unittest.TestCase):
    def test_new_heartbeats_usage_and_timing_are_not_durable_mutations(self):
        before=ledger()
        after=ledger(observability={"valid":True,"counts":{"telemetry":999}})
        after["stories"][0]["usage_records"].append(
            {"usage":{"input_tokens":1,"output_tokens":1}})
        after["stories"][0]["review_timing"]["total_seconds"]=19.0
        self.assertEqual(ts.durable_replay_state(before),
                         ts.durable_replay_state(after))

    def test_lifecycle_change_is_a_durable_mutation(self):
        before=ledger()
        after=ledger(project_state="project:accepted")
        self.assertNotEqual(ts.durable_replay_state(before),
                            ts.durable_replay_state(after))

class Discipline(unittest.TestCase):
    def test_opt_in_only(self):
        source=(HERE/"two_story_real.py").read_text(); self.assertIn('if __name__=="__main__"',source)
        for phrase in ("never-CI","never scheduled","never a required check"): self.assertIn(phrase,ts.__doc__)
    def test_interrupt_path_always_stops_the_child_poller(self):
        source=(HERE/"two_story_real.py").read_text()
        self.assertIn("except KeyboardInterrupt",source)
        self.assertIn("finally:\n        run.stop()",source)
    def test_service_signal_stops_child_before_harness_exits(self):
        run=mock.Mock()
        handler=ts.termination_handler(run)
        with self.assertRaises(ts.HarnessInterrupted) as raised:
            handler(ts.signal.SIGTERM, None)
        run.stop.assert_called_once_with()
        self.assertEqual(128 + ts.signal.SIGTERM, raised.exception.exit_code)
    def test_main_registers_terminal_disconnect_cleanup(self):
        source=(HERE/"two_story_real.py").read_text()
        self.assertIn('(\"SIGTERM\", \"SIGHUP\")',source)
    def test_harness_enters_only_through_poll_sh(self):
        source=(HERE/"two_story_real.py").read_text()
        self.assertIn('str(ROOT/"poll.sh")',source)
        self.assertNotIn("import dispatcher",source)
        self.assertNotIn("import review_route",source)
    def test_commitment_is_derived_from_test_project_data(self):
        self.assertEqual(999,ts.roadmap_commitment("### Roadmap commitment\n\n#999\n"))
        with self.assertRaisesRegex(RuntimeError,"Roadmap commitment"):
            ts.roadmap_commitment("### Roadmap commitment\n\nnone\n")
    def test_github_access_failure_names_the_request(self):
        args=mock.Mock(repo="owner/private",project=381,evidence_root="runs")
        run=ts.Run(args); run.token="bad"
        failure=urllib.error.HTTPError("url",403,"Forbidden",{},None)
        with mock.patch.object(ts.urllib.request,"urlopen",side_effect=failure):
            with self.assertRaisesRegex(RuntimeError,"GitHub GET /issues/381 returned 403"):
                run.api("/issues/381")
    def test_evidence_is_published_on_the_project(self):
        body=ts.evidence_comment({"run":"r","passed":True,"reason":"ok",**ledger()})
        for value in ("Story #1 / PR #101","head-1","story:claimed","merge-gate",
                      "exact-head independent review: approval","review timing: 18.0s total",
                      "review.engine.started","input=100, output=10",
                      "plan-approval","Replay changed durable state: `false`"):
            self.assertIn(value,body)
    def test_incomplete_evidence_cannot_be_published(self):
        broken={"run":"r","passed":False,"reason":"missing",**ledger(stories=[item(head=""),item(2)])}
        with self.assertRaisesRegex(ValueError,"head"):
            ts.evidence_comment(broken)

    def test_review_timing_requires_every_stage_and_uses_exact_pull(self):
        rows=[]
        for index,name in enumerate(("review.preparing","review.clone.started",
                                     "review.clone.finished","review.engine.started",
                                     "review.engine.finished","review.publishing",
                                     "review.published")):
            rows.append({"event":name,"pull_request":9,"story":None if index==0 else 7,
                         "head":None if index in (0,3,4) else "abc",
                         "timestamp":f"2026-01-01T00:00:{index:02d}.000000Z"})
        value=ts.review_observation(rows,7,9,"abc")
        self.assertEqual(6.0,value["total_seconds"])
        self.assertEqual(1.0,value["engine_seconds"])
        self.assertEqual({},ts.review_observation(rows[:-1],7,9,"abc"))
    def test_failure_persists_full_traceback_before_any_comment(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            args=mock.Mock(repo="owner/repo",project=395,evidence_root=root)
            run=ts.Run(args)
            try:
                raise RuntimeError("root failure")
            except RuntimeError as error:
                run.fail(error)
            evidence=pathlib.Path(root)/run.run/"evidence.json"
            value=__import__("json").loads(evidence.read_text())
            self.assertFalse(value["passed"])
            self.assertIn("raise RuntimeError",value["exception"])
            self.assertIn("root failure",value["exception"])
    def test_failure_evidence_survives_an_interrupted_github_snapshot(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            args=mock.Mock(repo="owner/repo",project=400,evidence_root=root)
            run=ts.Run(args); run.story=[403]; run.token="token"
            with mock.patch.object(run,"snapshot",side_effect=KeyboardInterrupt()), \
                 mock.patch.object(run,"api"):
                run.fail(RuntimeError("primary failure"))
            value=__import__("json").loads(
                (pathlib.Path(root)/run.run/"evidence.json").read_text())
            self.assertIn("primary failure",value["exception"])
            self.assertEqual("KeyboardInterrupt: ",value["snapshot_error"])
    def test_nested_usage_is_rendered_not_reported_unknown(self):
        body=ts.evidence_comment({"run":"r","passed":True,"reason":"ok",**ledger()})
        self.assertIn("input=100, output=10",body)
if __name__=="__main__": unittest.main()
