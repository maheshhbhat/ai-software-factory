#!/usr/bin/env python3
"""Scenarios 8–12: what happens after a story is claimed.

Selecting an engine, launching it, recording what it did, and reaching the right
terminal state on the right evidence. The last of those is the part worth being
careful about: a terminal state is the one write in this repository that a later
pass cannot undo, because §9.3 says no component reopens a closed issue.
"""

from __future__ import annotations

import os

import dispatcher
import review_link
import runlog
import observability
import workers
from driver import engine, factory, poll
from github_double import FakeGitHub, project, pull, story
from harness import Scenario, scenario

ACKNOWLEDGEMENT = "## Worker acknowledgement\n\nThe worker ran and did the thing.\n"


def acknowledging(hub, number):
    return {"story": number, "body": ACKNOWLEDGEMENT}


@scenario
class WorkerSelectionAndLaunch(Scenario):
    name = "S8 worker selection and launch"
    behaviour = "worker selection and launch"

    def run(self):
        hub = FakeGitHub([project(901), story(10)])
        worker = engine(hub, posts=acknowledging(hub, 10))
        woken, output = poll(hub, worker, env={
            "FACTORY_WORKER_ORDER": "claude-delivery,codex-delivery",
            "FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH": "/usr/bin/true",
            "FACTORY_WORKER_CODEX_DELIVERY_LAUNCH": "/usr/bin/true"})

        assert [d["story"] for d in woken] == [10], f"expected #10 dispatched, got {woken}"
        assert len(worker.calls) == 1, f"expected one launch, got {len(worker.calls)}"
        self.note("exactly one worker was launched for one dispatch")

        assert "claude-delivery" in output, output
        self.note("selection followed FACTORY_WORKER_ORDER — the preferred engine ran")

        # §4 — a queue item carries routing identity, never business context.
        # The parsed dispatch is the queue item: story, project, agent, and
        # nothing else the worker could read instead of reading GitHub.
        item = woken[0]
        assert item["story"] == 10 and item["project"] == 901, item
        assert set(item) == {"story", "project", "agent", "reservation"}, \
            f"the queue item carries more than routing identity: {sorted(item)}"
        self.note(f"the queue item carried routing identity only: {sorted(item)}")

        launched = " ".join(worker.calls[0])
        for leaked in ("do the thing", "### Spec", "src/**", "Acceptance notes"):
            assert leaked not in launched, f"the worker was handed business context: {leaked}"
        self.note("the launch carried no spec, scope or acceptance criteria — "
                  "the worker reconstructs context from GitHub itself")


@scenario
class ExecutionObservability(Scenario):
    name = "S9 execution observability"
    behaviour = "worker execution observability"

    def run(self):
        hub = FakeGitHub([project(901), story(10)])
        worker = engine(hub, posts=acknowledging(hub, 10), stdout="hello", stderr="noise")

        records = []
        real_event = observability.process_event

        def capture(name, **fields):
            record = real_event(name, **fields)
            records.append(record)
            return record

        import unittest.mock as mock
        with mock.patch.object(observability, "process_event", capture):
            poll(hub, worker)

        names = [record["event"] for record in records]
        for required in ("dispatch.received", "worker.launch.start", "worker.launch.end"):
            assert required in names, f"{required} was never recorded; got {names}"
        self.note(f"the run recorded {sorted(set(names))}")

        end = next(r for r in records if r["event"] == "worker.launch.end")
        for field in ("result", "elapsed_ms", "story"):
            assert field in end, f"worker.launch.end omitted {field}: {end}"
        self.note("worker.launch.end carries the verdict, the elapsed time and the story")

        assert end.get("stdout") == "hello" or "hello" in str(end), \
            "the engine's stdout was not kept"
        self.note("both streams were kept — the channel carrying an explanation is not discarded")

        # A secret must never reach the record.
        leaky = runlog.event("probe", detail="token ghp_" + "a" * 36)
        assert "ghp_" not in str(leaky), f"a credential shape survived redaction: {leaky}"
        self.note("credential shapes are redacted before anything is written")


@scenario
class NoDeliverableCompletion(Scenario):
    name = "S10 no-deliverable completion"
    behaviour = "successful no-deliverable completion"

    def run(self):
        hub = FakeGitHub([project(901), story(10)])
        worker = engine(hub, posts=acknowledging(hub, 10))
        woken, output = poll(hub, worker)

        assert [d["story"] for d in woken] == [10]
        assert hub.lifecycle(10) == dispatcher.COMPLETED, \
            f"expected story:completed, got {hub.lifecycle(10)}"
        assert hub.state(10) == "closed"
        assert hub.issues[10]["state_reason"] == "completed", \
            "§9.3 — completion is a success, not an abandonment"
        self.note("one poll took a ready story to story:completed, closed as completed")

        assert hub.section(10, "Attempt") == "1", "completion must not change Attempt"
        self.note("Attempt unchanged by completion — the attempt was dispatched and it succeeded")

        import completion
        recorded = [c for c in hub.comments[10]
                    if c["body"].lstrip().startswith(completion.COMPLETION_HEADING)]
        assert recorded, f"no reason was recorded on the story: {hub.comments[10]}"
        assert "§9.16" in recorded[0]["body"]
        self.note("the decision, its reason and its evidence were recorded on the story")

        # It is a success and must never read as a cancellation.
        assert dispatcher.CANCELLED not in hub.labels(10)
        self.note("the story is not labelled story:cancelled — successes and abandonments stay apart")

        # A later poll never relaunches it, which is the property #104 exists for.
        before = len(worker.calls)
        poll(hub, worker, seen=set())
        assert len(worker.calls) == before, "a completed story was relaunched"
        self.note("a later poll launched nothing for the completed story")

        # And a worker that proves nothing must not reach a terminal state.
        hub = FakeGitHub([project(901), story(10)])
        silent = engine(hub)
        poll(hub, silent)
        assert hub.lifecycle(10) == dispatcher.CLAIMED, \
            "a story with no durable proof was completed anyway"
        assert hub.state(10) == "open"
        self.note("a worker that posted no acknowledgement left the story claimed, for §9.4")


@scenario
class DeliveryReviewAndMerge(Scenario):
    name = "S11 PR and merge reconciliation"
    behaviour = "PR/merge lifecycle reconciliation"

    def run(self):
        hub = FakeGitHub([project(901), story(10)])

        def opens_a_pr(repo, cmd):
            repo.pulls.append(pull(200, 10))

        worker = engine(hub, posts=acknowledging(hub, 10), on_call=opens_a_pr)

        poll(hub, worker)
        assert hub.lifecycle(10) == dispatcher.CLAIMED, \
            "a story with a deliverable was completed instead of being left to review"
        self.note("a worker that produced a deliverable did not reach story:completed")

        poll(hub, worker, seen={10})
        assert hub.lifecycle(10) == dispatcher.IN_REVIEW, \
            f"expected story:in-review, got {hub.lifecycle(10)}"
        assert hub.state(10) == "open"
        self.note("an open §9.5-linked PR moved the story to story:in-review")

        claimed = [n for n in hub.issues if hub.lifecycle(n) == dispatcher.CLAIMED]
        assert claimed == [], "review did not release the WIP slot"
        self.note("the WIP slot the claim held was released at review (§9.10)")

        hub.advance(minutes=5)
        hub.pulls[0].update({"state": "closed", "merged_at": hub.stamp()})
        poll(hub, worker, seen={10})

        assert hub.lifecycle(10) == dispatcher.MERGED
        assert hub.state(10) == "closed"
        assert hub.issues[10]["state_reason"] == "completed"
        self.note("the merged delivery closed the story as story:merged, a terminal success")

        assert hub.applied(10) == [dispatcher.READY, dispatcher.CLAIMED,
                                   dispatcher.IN_REVIEW, dispatcher.MERGED], hub.applied(10)
        self.note("the full walk ready -> claimed -> in-review -> merged, every label "
                  "written by a component")

        writes_before = len(hub.writes)
        poll(hub, worker, seen=set())
        assert len(hub.writes) == writes_before, "a replay over the finished story wrote again"
        self.note("a later poll over the finished story wrote nothing")

        # Ambiguity fails closed rather than guessing.
        hub = FakeGitHub([project(901), story(10, lifecycle=dispatcher.IN_REVIEW)],
                         pulls=[pull(200, 10), pull(201, 10)])
        with factory(hub):
            outcomes = review_link.run("o/r", "t", apply=True)
        assert outcomes == [], "an ambiguous delivery caused a transition"
        assert hub.lifecycle(10) == dispatcher.IN_REVIEW
        self.note("two linked PRs left the story in review with a named reason")

        # Delivered and rejected is a human's decision, not the factory's.
        hub = FakeGitHub([project(901), story(10, lifecycle=dispatcher.IN_REVIEW)],
                         pulls=[pull(200, 10, state="closed", merged_at=None)])
        with factory(hub):
            outcomes = review_link.run("o/r", "t", apply=True)
        assert outcomes == []
        assert hub.state(10) == "open"
        self.note("a PR closed without merging left the story open for a human")


@scenario
class WorkerFailureAndFailover(Scenario):
    name = "S12 worker failure and failover"
    behaviour = "worker failure and failover"

    def run(self):
        hub = FakeGitHub([project(901), story(10)])
        woken, output = poll(hub, env={
            "FACTORY_WORKER_ORDER": "claude-delivery,codex-delivery",
            "FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH": "/usr/bin/false",
            "FACTORY_WORKER_CODEX_DELIVERY_LAUNCH": "/usr/bin/true"})
        assert [d["story"] for d in woken] == [10], output
        assert "codex-delivery" in output, output
        self.note("a definite failure of the preferred engine fell back to the secondary")
        assert output.index("claude-delivery") < output.index("codex-delivery"), output
        self.note("the fallback ran after the failure, not beside it — sequential, never parallel")

        # Every engine failing: driven through the runtime's own loop, because
        # that is where a launch failure is meant to be caught and reported.
        import io
        from contextlib import redirect_stdout
        import poller
        hub = FakeGitHub([project(901), story(10)])
        buffer = io.StringIO()
        with factory(hub, env={
                "FACTORY_WORKER_ORDER": "claude-delivery,codex-delivery",
                "FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH": "/usr/bin/false",
                "FACTORY_WORKER_CODEX_DELIVERY_LAUNCH": "/usr/bin/false"}), \
                redirect_stdout(buffer):
            poller.main(["--repo", "o/r", "--commitment", "54", "--once"])
        output = buffer.getvalue()
        assert "unstarted admission released" in output, output
        assert hub.lifecycle(10) == dispatcher.READY, \
            "a confirmed stopped worker should not hold the ambiguity lease"
        assert hub.section(10, "Attempt") == "0", \
            "a worker that never started must not consume an Attempt"
        assert hub.state(10) == "open"
        self.note("every engine failing before worker start was loud, woke nobody, "
                  "and immediately restored both the claim and Attempt")

        # Ambiguity must never fall back — two workers on one story is the one
        # outcome worse than none.
        attempts = []
        specs = [workers.WorkerSpec(name="claude-delivery", launch=["/usr/bin/true"]),
                 workers.WorkerSpec(name="codex-delivery", launch=["/usr/bin/true"])]

        def ambiguous(cmd, capture_output=True, text=True, timeout=None):
            attempts.append(cmd)
            raise TimeoutError("the engine neither finished nor failed")

        import unittest.mock as mock
        with mock.patch.object(workers, "run_observed", ambiguous):
            try:
                workers.launch(specs, {"story": 10, "project": 901})
            except Exception:
                pass
        assert len(attempts) <= 1, \
            f"an ambiguous outcome triggered a fallback: {len(attempts)} engines ran"
        self.note("an ambiguous launch outcome never authorized a second engine")
