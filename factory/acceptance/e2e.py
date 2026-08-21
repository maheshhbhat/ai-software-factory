#!/usr/bin/env python3
"""End-to-end verification — the real repository, the real engine, nothing mocked.

    GITHUB_TOKEN=$(gh auth token) python3 factory/acceptance/e2e.py \
        --repo owner/name --commitment 54 --project 109

## What this is for

Every `LIVE` claim about this factory has, until now, been produced by a person
running `poller.py --once` and reading the output. Those runs are real proof and
they are **not tests**: nothing re-runs them, and nothing catches a regression in
them. Stories #103, #106, #121, #122 and #124 each needed a human to observe the
result and paste it into an issue.

`factory/acceptance/` does not close that gap and was never going to. It replaces
both external systems — `dispatcher._api` for GitHub, `workers.run_observed` for
the CLI engines — so it never touches the real API, the real `claude` binary, the
subprocess boundary in `poller.run_dispatcher`, or any network failure mode. It
is a high-fidelity **integration** suite. Calling it "production-shaped"
described its shape, not its reach.

This is the layer above it. It creates a disposable Story, lets the factory do
the whole thing, and asserts the durable state that comes out.

## What it costs, stated where you will see it

* **It writes to a real repository.** One issue per run, which reaches a terminal
  state by the end. The repository's history is full of disposable verification
  Stories (#33, #36, #44, #51, #91, #99, #121) — this is that pattern, automated.
* **It spends a real engine invocation.** Roughly twelve seconds of `claude`, and
  whatever that costs.

Neither is hidden behind a flag default. An E2E test that pretends to be free
gets run in a loop by someone who did not read this.

## Cleanup is the behaviour under test

§9.3 is explicit that no component may cancel a Story, so this cannot close its
own fixture — a teardown that deleted the evidence would also be a component
doing what the contract reserves to a human.

The only honest exit is the one the factory already has. The fixture is a bounded
no-deliverable assignment: the worker acknowledges it, and the completion path
closes it as `story:completed` under §9.16. So the test cleans up **by the system
working**, and a failure to clean up is a real finding rather than a flaky
teardown. If this leaves a Story behind, something is broken and the Story is the
report.

## Why it never gates a merge

An E2E run depends on live repository state and on an external engine being
reachable. Making it a required check would let someone else's outage block every
merge, and a gate that fails for reasons the author cannot fix is a gate people
learn to route around. It is run deliberately, and its report is evidence.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
for relative in ("..", "../dispatcher", "../runtime", "../gates"):
    sys.path.insert(0, os.path.join(HERE, relative))

import dispatcher  # noqa: E402
import humanqueue  # noqa: E402
import poller  # noqa: E402
import review_link  # noqa: E402

ACK_HEADING = "## Worker acknowledgement"
COMPLETION_HEADING = "## Story completed"

FIXTURE_BODY = """### Spec

**Disposable end-to-end fixture, created by `factory/acceptance/e2e.py`.**

Do not implement anything for this Story. It exists so the factory exercises
itself: {purpose}.

Created at {created}.

### Project

#{project}

### Phase

test

### Depends-on

{depends}

### Hazard

- [ ] Touches hazard path

### Attempt

{attempt}

### Spend cap

$1 / 5 min

### Scope

{scope}

### Acceptance notes

- Reaches a terminal state through the factory, with no human lifecycle edit.
- Creates no branch, commit, pull request or file change unless the scenario says so.
"""


@dataclass
class Check:
    name: str
    passed: bool
    evidence: str = ""

    def line(self) -> str:
        return f"  {'PASS' if self.passed else 'FAIL'}  {self.name}\n          · {self.evidence}"


def observed(value) -> str:
    """Render what was actually seen.

    Evidence is written from the observation, never from the expectation. The
    first version of this file passed a fixed string like "appears once", which
    a *failing* check then printed verbatim — so the report cheerfully asserted
    the opposite of its own verdict. A failing check has to say what it found.
    """
    return str(value)


@dataclass
class Run:
    repo: str
    checks: list[Check] = field(default_factory=list)
    aborted: str = ""
    fixtures: list[int] = field(default_factory=list)
    exercised: set = field(default_factory=set)
    attested: set = field(default_factory=set)

    def check(self, name: str, passed: bool, evidence: str = "") -> bool:
        self.checks.append(Check(name, bool(passed), evidence))
        return bool(passed)

    @property
    def ok(self) -> bool:
        return not self.aborted and all(c.passed for c in self.checks)


def api(repo: str, path: str, token: str, method="GET", payload=None):
    return dispatcher._api(f"https://api.github.com/repos/{repo}{path}", token,
                           method=method, payload=payload)


def timeline(repo: str, number: int, token: str) -> list[dict]:
    return dispatcher.fetch_timeline(repo, number, token)


def lifecycle_events(events: list[dict]) -> list[dict]:
    return [e for e in events
            if e.get("event") in ("labeled", "unlabeled")
            and (dispatcher.merge_gate.label_name(e.get("label", {}))
                 .startswith(dispatcher.STORY_LIFECYCLE))]


# --------------------------------------------------------------------------
# Fixtures — created by the test, terminated by the factory
# --------------------------------------------------------------------------


class Fixture:
    """A disposable Story, and the guarantee that the factory ends it.

    Deliberately not a teardown. §9.3 forbids any component cancelling a Story,
    and a test that closed its own fixture would both break that rule and delete
    the evidence of a failure. So a fixture ends when the factory ends it, and
    one still open at the end of a default-tier scenario is reported as a
    finding — the fixture *is* the report.
    """

    def __init__(self, repo: str, token: str, project: int, purpose: str,
                 depends: str = "none", attempt: str = "0",
                 scope: str = "no repository file changes",
                 body_overrides: dict | None = None):
        self.repo, self.token, self.project = repo, token, project
        self.purpose = purpose
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = FIXTURE_BODY.format(project=project, created=created, purpose=purpose,
                                   depends=depends, attempt=attempt, scope=scope)
        for old, new in (body_overrides or {}).items():
            body = body.replace(old, new)
        issue = api(repo, "/issues", token, method="POST", payload={
            "title": f"[Verification] Disposable — {purpose}",
            "body": body,
            "labels": ["type:story", "story:ready", "phase:test"],
        })
        self.number: int = issue["number"]

    def issue(self) -> dict:
        return dispatcher.fetch_issue(self.repo, self.number, self.token)

    def lifecycle(self) -> str | None:
        return dispatcher.lifecycle_of(self.issue(), dispatcher.STORY_LIFECYCLE)

    def state(self) -> tuple[str, str | None]:
        issue = self.issue()
        return issue.get("state"), issue.get("state_reason")

    def section(self, name: str) -> str:
        return (dispatcher.merge_gate.parse_section(
            self.issue().get("body") or "", name) or "").strip()

    def timeline(self) -> list[dict]:
        return timeline(self.repo, self.number, self.token)

    def applied_labels(self) -> list[str]:
        return [dispatcher.merge_gate.label_name(e["label"])
                for e in lifecycle_events(self.timeline()) if e["event"] == "labeled"]

    def comments(self) -> list[dict]:
        return dispatcher.fetch_pages(
            f"https://api.github.com/repos/{self.repo}/issues/{self.number}/comments",
            self.token)

    def terminal(self) -> bool:
        return self.lifecycle() in (dispatcher.COMPLETED, dispatcher.MERGED)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


@dataclass
class Cost:
    issues: int = 0
    engine_calls: int = 0

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(self.issues + other.issues,
                    self.engine_calls + other.engine_calls)


class Scenario:
    """One requirement, proven against the live repository.

    `key` must name a requirement in `e2e_requirements.py`; the registry asserts
    it, so a scenario cannot drift away from the thing it claims to prove.
    """

    key = ""
    proves: tuple[str, ...] = ()      # requirements this scenario covers; defaults to (key,)
    attests: tuple[str, ...] = ()     # requirements verified from durable history, not caused here
    cost = Cost()

    @classmethod
    def covers(cls) -> tuple[str, ...]:
        return cls.proves or (cls.key,)

    def __init__(self, repo: str, commitment: int, project: int, token: str):
        self.repo, self.commitment, self.project, self.token = repo, commitment, project, token
        self.fixtures: list[Fixture] = []

    # -- helpers ---------------------------------------------------------

    def fixture(self, purpose: str, **kwargs) -> Fixture:
        made = Fixture(self.repo, self.token, self.project, purpose, **kwargs)
        self.fixtures.append(made)
        return made

    def wait_until(self, predicate, what: str, timeout: float = 20.0) -> bool:
        """Wait for a bounded time for GitHub's issue listing to catch up.

        **Measured, not assumed.** `GET /issues?state=open` lags writes by roughly
        three seconds in both directions: a newly created issue does not appear
        at once, and a newly closed one lingers. A probe run on 2026-08-21 saw
        both take three polls to settle.

        This is a property of the substrate, and the factory is unharmed by it
        because every component that *writes* re-reads its subject **by number**
        immediately beforehand — `dispatcher.claim`, `poison`, `apply_recovery`,
        `completion.apply_outcome`, `review_link.apply_outcome` all do — and a
        read by number is strongly consistent. A stale listing therefore costs a
        poll of latency and can never cause a duplicate write. The one direction
        it could bias, WIP accounting, over-counts rather than under-counts, so
        it under-dispatches: the safe way to be wrong.

        A test asserting immediately after a write has no such protection, which
        is why this exists. It is a bounded wait with a named failure, never a
        blind sleep.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(1.0)
        return False

    def visible(self, numbers: list[int]) -> bool:
        listing = dispatcher.fetch_issues(self.repo, self.token)
        return all(n in listing for n in numbers)

    def claimed_now(self) -> int:
        return sum(1 for issue in dispatcher.fetch_issues(self.repo, self.token).values()
                   if dispatcher.lifecycle_of(issue, dispatcher.STORY_LIFECYCLE)
                   == dispatcher.CLAIMED)

    def dispatch(self, claim: bool = True) -> str:
        """The real dispatcher, in-process, against the live repository."""
        buffer = io.StringIO()
        argv = ["--repo", self.repo, "--commitment", str(self.commitment)]
        with redirect_stdout(buffer):
            dispatcher.main(argv + (["--claim"] if claim else []))
        return buffer.getvalue()

    def poll(self) -> str:
        return run_poll(self.repo, self.commitment)

    def run(self, run: Run) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def settle(self, run: Run) -> None:
        """Poll until every fixture is terminal, or report the ones that are not.

        Bounded: a fixture that will not terminate must surface as a finding
        rather than spin. WIP_LIMIT caps how many can be in flight, so the bound
        is the number of fixtures plus a margin.
        """
        # Bound generously: with real work in flight the free capacity can be one
        # slot, so draining N fixtures takes N polls. Bounded, never unbounded —
        # a fixture that will not terminate must surface, not spin.
        for _ in range(len(self.fixtures) * 2 + 3):
            if all(f.terminal() for f in self.fixtures):
                break
            self.poll()
        stuck = [f for f in self.fixtures if not f.terminal()]
        run.check(f"[{self.key}] every fixture reached a terminal state",
                  not stuck,
                  observed("all terminal" if not stuck else
                           ", ".join(f"#{f.number} is {f.lifecycle()}" for f in stuck)
                           + " — left as-is; §9.3 reserves disposal to a human "
                             "and tidying it would delete the evidence"))


# --------------------------------------------------------------------------
# Preflight — refuse early, with a reason, rather than half-running
# --------------------------------------------------------------------------


def preflight(run: Run, repo: str, commitment: int, project: int, token: str) -> bool:
    try:
        issues = dispatcher.fetch_issues(repo, token)
    except Exception as exc:  # noqa: BLE001 — an unreachable repo is a clean abort
        run.aborted = f"cannot read {repo}: {type(exc).__name__}: {exc}"
        return False

    parent = issues.get(project)
    if parent is None or "type:project" not in dispatcher.labels_of(parent):
        run.aborted = f"#{project} is not an open project in {repo}"
        return False
    state = dispatcher.lifecycle_of(parent, dispatcher.PROJECT_LIFECYCLE)
    if state != dispatcher.PROJECT_ACTIVE:
        run.aborted = (f"project #{project} is `{state}`, not `{dispatcher.PROJECT_ACTIVE}` — "
                       f"the fixture would be rejected as PROJECT_NOT_ACTIVE")
        return False

    claimed = [n for n, issue in issues.items()
               if dispatcher.lifecycle_of(issue, dispatcher.STORY_LIFECYCLE)
               == dispatcher.CLAIMED]
    if len(claimed) >= dispatcher.WIP_LIMIT:
        run.aborted = (f"WIP is full ({len(claimed)}/{dispatcher.WIP_LIMIT}: "
                       f"{', '.join('#' + str(n) for n in sorted(claimed))}) — "
                       f"the fixture could be authorized and still never dispatch, "
                       f"which would read as a failure it did not cause")
        return False

    run.check("preflight", True,
              f"{repo} reachable, project #{project} is {dispatcher.PROJECT_ACTIVE}, "
              f"WIP {len(claimed)}/{dispatcher.WIP_LIMIT}")
    return True


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def create_fixture(repo: str, project: int, token: str) -> int:
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    issue = api(repo, "/issues", token, method="POST", payload={
        "title": "[Verification] Disposable — automated end-to-end run",
        "body": FIXTURE_BODY.format(project=project, created=created),
        "labels": ["type:story", "story:ready", "phase:test"],
    })
    return issue["number"]


def run_poll(repo: str, commitment: int) -> str:
    """One real poll: the real dispatcher subprocess, the real bridge, the real engine."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        poller.poll_once(repo, commitment, set(), claim=True)
    return buffer.getvalue()


def runlog_events(story: int) -> list[dict]:
    """This story's events from the runtime log.

    The `DISPATCH` line is deliberately **not** asserted against the poller's
    stdout: `run_dispatcher` captures the dispatcher subprocess's output and
    parses it, so the line never reaches the terminal. Asserting there tests the
    test, which is what the first version of this file did and why it failed
    while the factory worked.

    The runlog is the right source for a second reason. It is what an operator
    actually reads after the fact, and it is durable — the same standard applied
    to every other check here.
    """
    import runlog
    path = runlog.log_path()
    if not os.path.exists(path):
        return []
    events = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("story") == story or record.get("artifact") == story:
                events.append(record)
    return events


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render(run: Run, requested: list[str]) -> str:
    import e2e_requirements as reqs

    lines = ["End-to-end verification — real repository, real engine, nothing mocked", ""]
    if run.aborted:
        lines += [f"  ABORTED before creating any fixture: {run.aborted}", "",
                  "  Nothing was written. Aborting early is correct: a run that half-executes",
                  "  leaves a Story behind and reports a failure it did not cause."]
        return "\n".join(lines)

    for check in run.checks:
        lines.append(check.line())
    passed = sum(1 for c in run.checks if c.passed)
    lines += ["", f"  {passed}/{len(run.checks)} check(s) passed", ""]

    # Requirement coverage — including what this run did not and cannot reach.
    lines.append("  Phase 2 requirement coverage")
    for requirement in reqs.REQUIREMENTS:
        if requirement.tier == reqs.UNREACHABLE:
            mark = "UNREACHABLE"
        elif requirement.key in run.attested:
            # Verified against durable state the real system produced, but not
            # caused by this run. A weaker claim, and it must read as one.
            mark = "attested"
        elif requirement.key in run.exercised:
            mark = "exercised"
        elif requirement.tier == reqs.DEFAULT:
            mark = "not run"
        else:
            mark = f"not run ({requirement.tier})"
        lines.append(f"    {mark:<18} {requirement.key:<15} {requirement.behaviour} "
                     f"({requirement.reference})")

    summary = reqs.summary()
    exercised = len(run.exercised - run.attested)
    attested = len(run.attested)
    lines += ["",
              f"    {exercised + attested}/{summary['reachable']} reachable requirement(s) "
              f"covered — {exercised} exercised live, {attested} attested from durable "
              f"history; {summary['unreachable']} unreachable"]

    for requirement in reqs.REQUIREMENTS:
        if requirement.tier == reqs.UNREACHABLE:
            lines += ["", f"    UNREACHABLE — {requirement.key}: {requirement.note}"]

    if run.fixtures:
        lines += ["", f"  fixtures created: "
                      + ", ".join(f"#{n}" for n in sorted(run.fixtures))]
    if not run.ok:
        lines += ["",
                  "  FAIL. Fixtures are left exactly as they are — they are the report.",
                  "  §9.3 reserves disposal to a human, and tidying up would delete the "
                  "evidence."]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import e2e_requirements as reqs

    parser = argparse.ArgumentParser(
        description="End-to-end verification against a live repository")
    parser.add_argument("--repo", help="owner/name")
    parser.add_argument("--commitment", type=int)
    parser.add_argument("--project", type=int,
                        help="an active project to hang fixtures under")
    parser.add_argument("--only", help="comma-separated requirement keys to run")
    parser.add_argument("--resume", type=int, metavar="ISSUE",
                        help="second phase of a deferred scenario, against the fixture "
                             "issue the first phase left behind")
    parser.add_argument("--list", action="store_true",
                        help="print the requirement map and exit, touching nothing")
    parser.add_argument("--json", help="write the report here")
    args = parser.parse_args(argv)

    import e2e_scenarios  # noqa: F401 — importing registers the scenarios

    if args.list:
        print("Phase 2 requirements and their end-to-end reach\n")
        for requirement in reqs.REQUIREMENTS:
            available = any(requirement.key in s.covers() for s in e2e_scenarios.REGISTRY)
            print(f"  {requirement.tier:<12} {'scenario' if available else 'none    '}  "
                  f"{requirement.key:<15} {requirement.behaviour}")
            if requirement.note:
                print(f"                                 {requirement.note}")
        summary = reqs.summary()
        print(f"\n  {summary['reachable']}/{summary['total']} reachable, "
              f"{summary['unreachable']} unreachable")
        return 0

    for required in ("repo", "commitment", "project"):
        if getattr(args, required) is None:
            parser.error(f"--{required} is required unless --list is given")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    run = Run(repo=args.repo)
    if not token:
        run.aborted = "no GITHUB_TOKEN/GH_TOKEN — this test writes to a real repository"
        print(render(run, []))
        return 2

    wanted = [k.strip() for k in args.only.split(",")] if args.only else None
    selected = [cls for cls in e2e_scenarios.REGISTRY
                if (wanted is None and reqs.BY_KEY[cls.key].tier == reqs.DEFAULT)
                or (wanted is not None and cls.key in wanted)]
    if not selected:
        print(f"no scenario matches {args.only!r}; --list shows what exists")
        return 2

    total = Cost()
    for cls in selected:
        total = total + cls.cost
    print(f"running {len(selected)} scenario(s): "
          f"{total.issues} issue(s) will be created, "
          f"~{total.engine_calls} engine invocation(s) spent", flush=True)

    if not preflight(run, args.repo, args.commitment, args.project, token):
        print(render(run, []))
        return 2

    for cls in selected:
        instance = cls(args.repo, args.commitment, args.project, token)
        if args.resume is not None:
            if not hasattr(cls, "resume"):
                parser.error(f"--resume means nothing to {cls.key}; only a deferred "
                             f"scenario has a second phase")
            instance.resume = args.resume
        try:
            instance.run(run)
            run.exercised.update(cls.covers())
            run.attested.update(cls.attests)
        except Exception as exc:  # noqa: BLE001 — a crash is a failed scenario
            import traceback
            run.check(f"[{cls.key}] the scenario completed without crashing", False,
                      observed(f"{type(exc).__name__}: {exc}"))
            traceback.print_exc(file=sys.stdout)
        run.fixtures.extend(f.number for f in instance.fixtures)

    print(render(run, wanted or []))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"repo": run.repo, "fixtures": sorted(run.fixtures),
                       "aborted": run.aborted, "passed": run.ok,
                       "exercised": sorted(run.exercised),
                       "requirements": {r.key: {"tier": r.tier,
                                                "behaviour": r.behaviour,
                                                "reference": r.reference,
                                                "note": r.note}
                                        for r in reqs.REQUIREMENTS},
                       "checks": [{"name": c.name, "passed": c.passed,
                                   "evidence": c.evidence} for c in run.checks]},
                      handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"\n  report written to {args.json}")

    if run.aborted:
        return 2
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
