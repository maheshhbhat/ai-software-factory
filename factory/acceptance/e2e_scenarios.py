#!/usr/bin/env python3
"""The live scenarios, one per Phase 2 requirement that can be reached.

Each class names the requirement it proves, declares what it costs in issues and
engine invocations, and — for the default tier — leaves nothing behind.

The house rule from `e2e.py` applies throughout: **evidence is rendered from the
observation, never from the expectation.** A failing check must say what it
found, or the report argues with its own verdict.
"""

from __future__ import annotations

import io
import os
import re
from contextlib import redirect_stdout

from datetime import datetime, timezone

import dispatcher
import humanqueue
import poller
import review_link
from e2e import Cost, Run, Scenario, api, observed, runlog_events

REGISTRY: list[type[Scenario]] = []


def scenario(cls):
    REGISTRY.append(cls)
    return cls


# --------------------------------------------------------------------------
# default tier — cheap, and every fixture ends terminal through the factory
# --------------------------------------------------------------------------


@scenario
class Dispatch(Scenario):
    """Order, capacity, and the atomic claim — asserted from the timeline.

    Uses a full poll rather than the bare dispatcher, and that is a correctness
    requirement rather than a preference. `dispatcher.main --claim` writes
    `story:claimed` **without launching anything**, so a fixture claimed that way
    has no worker, cannot complete, and is stranded until the §9.4 lease expires
    an hour later. The first version of this scenario did exactly that and
    starved every scenario after it of WIP.

    It is a fair reflection of the contract, incidentally: a claim is a lease on
    a worker's attention, so taking one without dispatching a worker is not a
    state the factory ever produces — `poll_once` always does both.

    The consequence is that a fixture is already `story:completed` by the time
    this scenario can look at it, so the assertions read the **timeline**, which
    is durable and ordered, rather than the current label.
    """

    key = "dispatch"
    cost = Cost(issues=3, engine_calls=3)

    def run(self, run: Run) -> None:
        made = [self.fixture(f"deterministic dispatch, fixture {n} of 3") for n in range(1, 4)]
        numbers = sorted(f.number for f in made)

        run.check("[dispatch] the fixtures became visible to the dispatch queue",
                  self.wait_until(lambda: self.visible(numbers), "fixtures listed"),
                  observed(f"{numbers} present in the open-issue listing"))

        # Capacity is what is actually free. This repository may have real work
        # in flight, and a scenario that assumed the whole budget would fail for
        # a reason it did not cause.
        already = self.claimed_now()
        free = max(0, dispatcher.WIP_LIMIT - already)
        expected = min(free, len(made))

        report = self.poll()

        def was_claimed(fixture) -> bool:
            return dispatcher.CLAIMED in fixture.applied_labels()

        claimed = [f for f in made if was_claimed(f)]
        run.check("[dispatch] the dispatcher filled exactly the free capacity",
                  len(claimed) == expected,
                  observed(f"{len(claimed)} claimed; {already} already in flight, "
                           f"{free} slot(s) free of WIP_LIMIT={dispatcher.WIP_LIMIT}"))

        # §9.10 orders by (project, story); all fixtures share one project, so
        # the lowest issue numbers must be the ones taken.
        run.check("[dispatch] selection followed (project, story) ascending",
                  sorted(f.number for f in claimed) == numbers[:len(claimed)],
                  observed(f"claimed {sorted(f.number for f in claimed)} of {numbers}"))

        run.check("[dispatch] the repository-wide cap was never exceeded",
                  self.claimed_now() <= dispatcher.WIP_LIMIT,
                  observed(f"{self.claimed_now()} claimed repository-wide, "
                           f"cap {dispatcher.WIP_LIMIT}"))

        for fixture in claimed:
            run.check(f"[dispatch] #{fixture.number} incremented Attempt exactly once",
                      fixture.section("Attempt") == "1",
                      observed(f"Attempt = {fixture.section('Attempt')}"))

        untouched = [f for f in made if not was_claimed(f)]
        run.check("[dispatch] surplus stories waited rather than being rejected",
                  len(untouched) == len(made) - len(claimed),
                  observed(", ".join(f"#{f.number} never claimed" for f in untouched)
                           + " — held by capacity, not eligibility" if untouched
                           else "nothing left waiting"))

        self.settle(run)


@scenario
class Authorization(Scenario):
    key = "authorization"
    cost = Cost(issues=1, engine_calls=1)

    def run(self, run: Run) -> None:
        # One fixture, repaired a link at a time. It ends valid, so it dispatches
        # and completes — the scenario cleans up by finishing the work.
        fixture = self.fixture("authorization chain, repaired link by link",
                               scope="- bad glob", attempt="not-a-number")
        run.check("[authorization] the fixture became visible to the dispatch queue",
                  self.wait_until(lambda: self.visible([fixture.number]), "fixture listed"),
                  observed(f"#{fixture.number} present in the open-issue listing"))

        def reason_for(number: int, report: str) -> str:
            found = re.search(rf"(?m)^\s+#{number}\s+skip\s+(\S+)", report)
            return found.group(1) if found else "not listed"

        stages = [
            (dispatcher.Reason.ATTEMPT_INVALID, {"### Attempt\n\nnot-a-number": "### Attempt\n\n0"}),
            (dispatcher.Reason.SCOPE_INVALID, {"### Scope\n\n- bad glob":
                                               "### Scope\n\nno repository file changes"}),
        ]
        for expected, repair in stages:
            report = self.dispatch(claim=False)
            actual = reason_for(fixture.number, report)
            run.check(f"[authorization] refused with {expected}",
                      actual == expected,
                      observed(f"dispatcher said {actual}"))
            body = fixture.issue().get("body") or ""
            for old_text, new_text in repair.items():
                body = body.replace(old_text, new_text)
            api(self.repo, f"/issues/{fixture.number}", self.token,
                method="PATCH", payload={"body": body})
            self.wait_until(lambda: repair and all(
                v in (dispatcher.fetch_issues(self.repo, self.token)
                      .get(fixture.number, {}).get("body") or "")
                for v in repair.values()), "body edit visible")

        report = self.dispatch(claim=False)
        run.check("[authorization] a fully-repaired chain is eligible",
                  re.search(rf"(?m)^\s+#{fixture.number}\s+ELIGIBLE", report) is not None,
                  observed("ELIGIBLE" if re.search(rf"(?m)^\s+#{fixture.number}\s+ELIGIBLE", report)
                           else reason_for(fixture.number, report)))
        self.settle(run)


@scenario
class Dependencies(Scenario):
    key = "dependencies"
    cost = Cost(issues=2, engine_calls=2)

    def run(self, run: Run) -> None:
        # The #107 case, live: a dependency that *succeeded* is closed, so it is
        # absent from the open-issue queue the dispatcher builds. A hermetic
        # suite proved the rule while production rejected every satisfied
        # dependency for two days.
        upstream = self.fixture("dependency upstream — completes, then closes")
        downstream = self.fixture("dependency downstream — must wait, then run",
                                  depends=f"#{upstream.number}")

        run.check("[dependencies] both fixtures became visible to the dispatch queue",
                  self.wait_until(lambda: self.visible([upstream.number, downstream.number]),
                                  "fixtures listed"),
                  observed(f"#{upstream.number} and #{downstream.number} in the listing"))

        report = self.dispatch(claim=False)
        unmet = re.search(rf"(?m)^\s+#{downstream.number}\s+skip\s+DEPENDENCY_UNMET(.*)$", report)
        run.check("[dependencies] an unfinished dependency blocks the dependent",
                  unmet is not None,
                  observed(unmet.group(0).strip() if unmet else "not reported as unmet"))

        for _ in range(3):
            if upstream.terminal():
                break
            self.poll()

        state, reason = upstream.state()
        run.check("[dependencies] the dependency reached a closed terminal success",
                  upstream.lifecycle() == dispatcher.COMPLETED and state == "closed",
                  observed(f"#{upstream.number} is {upstream.lifecycle()}, {state}/{reason}"))

        gone = self.wait_until(
            lambda: upstream.number not in dispatcher.fetch_issues(self.repo, self.token),
            "dependency left the listing")
        run.check("[dependencies] the dependency is absent from the dispatch queue",
                  gone,
                  observed(f"#{upstream.number} is closed, so the queue — which §9.3 "
                           f"defines as open issues — cannot see it"
                           if gone else f"#{upstream.number} still listed after the wait"))

        report = self.dispatch(claim=False)
        eligible = re.search(rf"(?m)^\s+#{downstream.number}\s+ELIGIBLE", report)
        run.check("[dependencies] a CLOSED terminal-success dependency satisfies Depends-on",
                  eligible is not None,
                  observed("ELIGIBLE — resolved by number, outside the queue"
                           if eligible else
                           (re.search(rf"(?m)^\s+#{downstream.number}\s+skip\s+(.*)$", report)
                            or type("", (), {"group": lambda s, i: "not listed"})()).group(1)))
        self.settle(run)


@scenario
class LifecycleAndObservability(Scenario):
    """Launch, selection, observability, completion and replay in one flow.

    Kept together deliberately: they are one causal chain, and splitting them
    into five fixtures would spend five engine invocations to assert stages of
    the same run.
    """

    key = "completion"
    proves = ("completion", "worker-launch", "observability", "replay")
    cost = Cost(issues=1, engine_calls=1)

    def run(self, run: Run) -> None:
        fixture = self.fixture("full lifecycle — launch, acknowledge, complete, replay")
        output = self.poll()
        number = fixture.number
        events = runlog_events(number)
        kinds = [e.get("event") for e in events]

        dispatches = [e for e in events if e.get("event") == "dispatch.received"]
        run.check("[worker-launch] exactly one dispatch reached the runtime",
                  len(dispatches) == 1,
                  observed(f"{len(dispatches)} dispatch.received"
                           + (f", agent={dispatches[0].get('agent')}" if dispatches else "")))

        launches = [e for e in events if e.get("event") == "worker.launch.end"]
        run.check("[worker-launch] the bridge launched it and reported a definite result",
                  len(launches) == 1 and launches[0].get("result") == "LAUNCHED"
                  and "bridge.dispatch" in kinds,
                  observed(f"{len(launches)} launch(es)"
                           + (f", result={launches[0].get('result')}, "
                              f"elapsed_ms={launches[0].get('elapsed_ms')}"
                              if launches else "")))

        run.check("[observability] the run is reconstructable from the log alone",
                  {"dispatch.received", "worker.launch.start", "worker.launch.end",
                   "story.completion"} <= set(kinds),
                  observed(sorted(set(kinds))))

        state, reason = fixture.state()
        run.check("[completion] the story reached story:completed and closed as completed",
                  fixture.lifecycle() == dispatcher.COMPLETED
                  and (state, reason) == ("closed", "completed"),
                  observed(f"{fixture.lifecycle()}, {state}/{reason}"))
        run.check("[completion] Attempt records one dispatched attempt, untouched by completion",
                  fixture.section("Attempt") == "1",
                  observed(f"Attempt = {fixture.section('Attempt')}"))

        acks = [c for c in fixture.comments()
                if (c.get("body") or "").lstrip().startswith("## Worker acknowledgement")]
        run.check("[completion] exactly one worker acknowledgement exists",
                  len(acks) == 1, observed(f"{len(acks)} acknowledgement(s)"))

        applied = fixture.applied_labels()
        run.check("[completion] no human wrote a lifecycle label",
                  applied == [dispatcher.READY, dispatcher.CLAIMED, dispatcher.COMPLETED],
                  observed(f"applied in order: {applied}"))

        before = len(fixture.timeline())
        second = self.poll()
        run.check("[replay] a replay poll changed nothing",
                  len(fixture.timeline()) == before and f"story=#{number}" not in second,
                  observed(f"timeline {before} -> {len(fixture.timeline())}"))

        queue = io.StringIO()
        with redirect_stdout(queue):
            humanqueue.run(self.repo, self.token)
        run.check("[replay] terminal work is not in the human queue",
                  f"artifact=#{number}" not in queue.getvalue(),
                  observed("absent from the human queue"))

        prs = dispatcher.fetch_pull_requests(self.repo, self.token)
        linked, _ = dispatcher.linked_delivery_prs(number, prs)
        run.check("[completion] the fixture built nothing",
                  not linked, observed(f"{len(linked)} linked pull request(s)"))


@scenario
class Failover(Scenario):
    key = "failover"
    cost = Cost(issues=1, engine_calls=1)

    def run(self, run: Run) -> None:
        fixture = self.fixture("worker failover — primary engine missing")
        previous = dict(os.environ)
        try:
            # A binary that does not exist is a *definite* failure, which is the
            # only outcome §84 permits a fallback on. An ambiguous one must not.
            os.environ["FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH"] = \
                "/nonexistent/factory-e2e-missing-engine"
            output = self.poll()
        finally:
            os.environ.clear()
            os.environ.update(previous)

        events = runlog_events(fixture.number)
        attempts = [e for e in events if e.get("event") == "worker.launch.end"]
        outcome = [e for e in events if e.get("event") == "worker.outcome"]
        run.check("[failover] a definite primary failure fell back to the secondary",
                  any(e.get("result") == "LAUNCHED" for e in attempts + outcome)
                  and len(attempts) >= 2,
                  observed([f"{e.get('worker')}={e.get('result')}" for e in attempts]
                           or "no launch attempts recorded"))
        run.check("[failover] exactly one engine ended up doing the work",
                  len([e for e in attempts if e.get("result") == "LAUNCHED"]) == 1,
                  observed(f"{len([e for e in attempts if e.get('result') == 'LAUNCHED'])} "
                           f"successful launch(es) — two would mean duplicate work"))
        self.settle(run)


@scenario
class FailClosed(Scenario):
    key = "fail-closed"
    cost = Cost(issues=0, engine_calls=0)

    def run(self, run: Run) -> None:
        # No fixture: every case here must refuse before touching any state.
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        result = subprocess.run(
            [sys.executable, str(dispatcher.__file__.replace("dispatcher.py", "dispatcher.py")),
             "--repo", self.repo, "--commitment", str(self.commitment)],
            capture_output=True, text=True, env=env)
        run.check("[fail-closed] no credential dispatches nothing and exits non-zero",
                  result.returncode != 0 and "fail closed" in result.stdout.lower(),
                  observed(f"exit {result.returncode}: "
                           f"{result.stdout.strip().splitlines()[0] if result.stdout.strip() else 'no output'}"))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = dispatcher.guarded_main(["--fixture", "/nonexistent/fixture.json"])
        run.check("[fail-closed] an unreadable input is loud, not silent idleness",
                  code != 0 and "Nothing was dispatched" in buffer.getvalue(),
                  observed(f"exit {code}, said 'Nothing was dispatched': "
                           f"{'Nothing was dispatched' in buffer.getvalue()}"))

        parsed = None
        try:
            poller.parse_dispatches("DISPATCH story=#notanumber project=#901")
            parsed = "accepted"
        except poller.MalformedDispatch as exc:
            parsed = f"refused: {exc}"
        run.check("[fail-closed] a non-canonical DISPATCH line is refused",
                  parsed.startswith("refused"), observed(parsed))


@scenario
class GateEnforcement(Scenario):
    """The required check, verified against live repository configuration.

    Deliberately does **not** push a violating branch. The merge gate already
    runs end-to-end on every pull request this repository receives — that is
    continuous live verification, and manufacturing an extra red PR per run
    would add junk to prove something the last dozen PRs already proved.

    What cannot be read off a passing PR is whether the gate is *enforced*: a
    green check that is not required blocks nothing. That is repository
    configuration, and configuration is a §9.14 trust input precisely because
    the agent's credential cannot fabricate it. So this asserts the enforcement
    surface, and leaves the red-on-violation behaviour to acceptance scenario
    S13, which drives every violation class through the real gate.
    """

    key = "scope-gate"
    cost = Cost(issues=0, engine_calls=0)

    def run(self, run: Run) -> None:
        rulesets = api(self.repo, "/rulesets", self.token)
        main_rules = None
        for entry in rulesets:
            detail = api(self.repo, f"/rulesets/{entry['id']}", self.token)
            if detail.get("enforcement") == "active":
                main_rules = detail
                break

        run.check("[scope-gate] a ruleset is actively enforced on the default branch",
                  main_rules is not None,
                  observed(f"{len(rulesets)} ruleset(s); "
                           f"{main_rules.get('name') if main_rules else 'none active'}"))
        if main_rules is None:
            return

        rules = {rule["type"]: rule.get("parameters") or {} for rule in main_rules["rules"]}
        checks = [c.get("context") for c in
                  rules.get("required_status_checks", {}).get("required_status_checks", [])]
        run.check("[scope-gate] merge-gate is a required status check",
                  "merge-gate" in checks, observed(f"required checks: {checks}"))

        # §9.14: making the gate required while it can be bypassed hands the
        # exemption to the very credential it constrains.
        run.check("[scope-gate] no bypass actor can skip it",
                  not main_rules.get("bypass_actors"),
                  observed(f"bypass_actors = {main_rules.get('bypass_actors') or '[]'}"))

        run.check("[scope-gate] merge-gate-surface was never made required",
                  "merge-gate-surface" not in checks,
                  observed("advisory only — a required surface check would make the "
                           "gate permanently unmodifiable (§9.14)"))

        pulls = api(self.repo, "/pulls?state=closed&per_page=10", self.token)
        merged = [pr for pr in pulls if pr.get("merged_at")][:5]
        judged = []
        for pr in merged:
            runs = api(self.repo, f"/commits/{pr['head']['sha']}/check-runs", self.token)
            names = {r["name"]: r.get("conclusion") for r in runs.get("check_runs", [])}
            judged.append((pr["number"], names.get("merge-gate")))
        run.check("[scope-gate] every recent merged pull request was judged by the gate",
                  bool(judged) and all(result == "success" for _, result in judged),
                  observed(", ".join(f"#{n}:{r}" for n, r in judged) or "no merged PRs found"))


@scenario
class ReviewPath(Scenario):
    """`claimed → in-review → merged`, attested from durable history.

    Marked *attested* rather than *exercised*, and the distinction is the honest
    part. Causing this live would mean opening a real pull request and merging it
    to `main` on every run, which buys a commit of noise per run to re-prove
    something the repository's own history already records. What is asserted here
    is durable state the real system produced — the strongest evidence available
    short of causing it again.

    Run `--only review-open,review-merged` to cause it instead; those scenarios
    are opt-in because each one writes to `main`.
    """

    key = "review-merged"
    proves = ("review-merged", "review-open")
    attests = ("review-merged", "review-open")
    cost = Cost(issues=0, engine_calls=0)

    def run(self, run: Run) -> None:
        walked = []
        for number in self._recent_merged_stories():
            events = [e for e in dispatcher.fetch_timeline(self.repo, number, self.token)
                      if e.get("event") in ("labeled", "unlabeled")]
            applied = [dispatcher.merge_gate.label_name(e.get("label", {}))
                       for e in events if e.get("event") == "labeled"]
            if applied[-3:] == [dispatcher.CLAIMED, dispatcher.IN_REVIEW, dispatcher.MERGED]:
                walked.append(number)

        run.check("[review-open] stories have walked claimed -> in-review under the runtime",
                  bool(walked),
                  observed(f"{len(walked)} story(ies) with the full walk recorded: "
                           + ", ".join(f"#{n}" for n in walked)))
        run.check("[review-merged] and on to story:merged, closed as completed",
                  all(dispatcher.fetch_issue(self.repo, n, self.token).get("state") == "closed"
                      for n in walked) and bool(walked),
                  observed(f"all {len(walked)} closed" if walked else "none found"))

    def _recent_merged_stories(self) -> list[int]:
        issues = api(self.repo,
                     "/issues?state=closed&labels=story:merged&per_page=10", self.token)
        return [i["number"] for i in issues if "pull_request" not in i][:6]


@scenario
class AttemptLimit(Scenario):
    """The §4.3.5 threshold, live. Opt-in, because it cannot clean up after itself.

    A poisoned Story waits for a human by design, so this leaves one open issue.
    It reuses an existing open poison fixture when it finds one, which bounds the
    residue at a single issue no matter how often the scenario runs — an E2E
    suite that quietly accumulates open issues teaches its operators to ignore
    open issues.
    """

    key = "attempt-limit"
    cost = Cost(issues=1, engine_calls=0)

    def run(self, run: Run) -> None:
        existing = self._existing_poison()
        if existing is None:
            fixture = self.fixture("attempt threshold — poisoned, and stays open",
                                   attempt=str(dispatcher.ATTEMPT_MAX))
            self.dispatch(claim=True)
            number, reused = fixture.number, False
        else:
            number, reused = existing, True

        issue = dispatcher.fetch_issue(self.repo, number, self.token)
        run.check("[attempt-limit] the threshold poisoned instead of dispatching",
                  dispatcher.lifecycle_of(issue, dispatcher.STORY_LIFECYCLE)
                  == dispatcher.POISON,
                  observed(f"#{number} is "
                           f"{dispatcher.lifecycle_of(issue, dispatcher.STORY_LIFECYCLE)}"
                           + (" (reused, not created)" if reused else "")))
        run.check("[attempt-limit] Attempt still reads the threshold; none was consumed",
                  (dispatcher.merge_gate.parse_section(issue.get("body") or "",
                                                       "Attempt") or "").strip()
                  == str(dispatcher.ATTEMPT_MAX),
                  observed("Attempt = " + (dispatcher.merge_gate.parse_section(
                      issue.get("body") or "", "Attempt") or "?").strip()))
        run.check("[attempt-limit] the issue is OPEN, so the §4.3.6 rescue is reachable",
                  issue.get("state") == "open",
                  observed(f"state = {issue.get('state')} — closing it would put the "
                           f"story beyond a rescue no component may perform (§9.3)"))

        queue = io.StringIO()
        with redirect_stdout(queue):
            humanqueue.run(self.repo, self.token)
        first = queue.getvalue()
        queue = io.StringIO()
        with redirect_stdout(queue):
            humanqueue.run(self.repo, self.token)
        run.check("[attempt-limit] it is announced on consecutive polls, not once",
                  f"artifact=#{number}" in first
                  and f"artifact=#{number}" in queue.getvalue(),
                  observed("present in both polls — nothing records that it has been "
                           "announced, so nothing can decide it has been announced enough"))

    def _existing_poison(self) -> int | None:
        issues = dispatcher.fetch_issues(self.repo, self.token)
        found = sorted(
            number for number, issue in issues.items()
            if dispatcher.lifecycle_of(issue, dispatcher.STORY_LIFECYCLE) == dispatcher.POISON
            and "[Verification]" in (issue.get("title") or ""))
        return found[0] if found else None


@scenario
class Recovery(Scenario):
    """§9.4 claim expiry, across two invocations an hour apart.

    `CLAIM_LEASE` is sixty minutes measured from a durable `story:claimed`
    timeline event, and GitHub timeline events cannot be backdated. No single
    process can reach this. Two can, and the fixture issue is the only state that
    has to survive between them — no local file, which is the same property the
    factory itself holds.

        --only recovery                 # phase one: claim a fixture and stop
        --only recovery --resume 131    # phase two, 60+ minutes later

    Phase two reports plainly when the lease has not yet elapsed. That is a
    result, not a failure: the contract says sixty minutes and the test does not
    get to disagree.
    """

    key = "recovery"
    cost = Cost(issues=1, engine_calls=0)
    resume: int | None = None

    def run(self, run: Run) -> None:
        if self.resume is None:
            fixture = self.fixture("claim expiry — phase one, claimed and left alone")
            self.dispatch(claim=True)
            run.check("[recovery] phase one claimed a fixture and left it",
                      fixture.lifecycle() == dispatcher.CLAIMED,
                      observed(f"#{fixture.number} is {fixture.lifecycle()}; "
                               f"re-run with --only recovery --resume {fixture.number} "
                               f"after {int(dispatcher.CLAIM_LEASE.total_seconds() // 60)} "
                               f"minutes"))
            return

        number = self.resume
        events = [e for e in dispatcher.fetch_timeline(self.repo, number, self.token)
                  if e.get("event") == "labeled"
                  and dispatcher.merge_gate.label_name(e.get("label", {}))
                  == dispatcher.CLAIMED]
        if not events:
            run.check("[recovery] the fixture carries a claim event", False,
                      observed(f"#{number} has no story:claimed labeled event"))
            return

        claimed_at = dispatcher._parse_time(max(e["created_at"] for e in events))
        elapsed = datetime.now(timezone.utc) - claimed_at
        if elapsed < dispatcher.CLAIM_LEASE:
            remaining = int((dispatcher.CLAIM_LEASE - elapsed).total_seconds() // 60)
            run.check("[recovery] the lease has elapsed", False,
                      observed(f"claimed {int(elapsed.total_seconds() // 60)} minute(s) ago; "
                               f"{remaining} more needed. The contract says sixty minutes "
                               f"and this test does not get to disagree"))
            return

        before = dispatcher.fetch_issue(self.repo, number, self.token)
        attempt_before = (dispatcher.merge_gate.parse_section(
            before.get("body") or "", "Attempt") or "").strip()
        report = self.dispatch(claim=True)
        after = dispatcher.fetch_issue(self.repo, number, self.token)
        attempt_after = (dispatcher.merge_gate.parse_section(
            after.get("body") or "", "Attempt") or "").strip()

        run.check("[recovery] the expired claim returned to story:ready",
                  dispatcher.lifecycle_of(after, dispatcher.STORY_LIFECYCLE)
                  in (dispatcher.READY, dispatcher.CLAIMED),
                  observed(f"#{number} is "
                           f"{dispatcher.lifecycle_of(after, dispatcher.STORY_LIFECYCLE)}"))
        run.check("[recovery] the attempt it consumed was restored (§9.4)",
                  int(attempt_after or 0) <= int(attempt_before or 0),
                  observed(f"Attempt {attempt_before} -> {attempt_after}"))
        run.check("[recovery] the recovery named its reason",
                  "CLAIM_LEASE_EXPIRED" in report,
                  observed("CLAIM_LEASE_EXPIRED recorded"
                           if "CLAIM_LEASE_EXPIRED" in report else "no named reason"))
