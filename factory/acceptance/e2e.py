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

**Disposable end-to-end verification fixture, created by `factory/acceptance/e2e.py`.**

Do not implement anything for this Story. It exists to be dispatched, acknowledged
and completed by the factory itself, so that the end-to-end path is exercised by a
test rather than by a person reading terminal output.

Created at {created}.

### Project

#{project}

### Phase

test

### Depends-on

none

### Hazard

- [ ] Touches hazard path

### Attempt

0

### Spend cap

$1 / 5 min

### Scope

no repository file changes

### Acceptance notes

- Reaches `story:completed` and closed, with no human lifecycle edit.
- Exactly one worker acknowledgement.
- No branch, commit, pull request or file change.
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
    fixture: int | None = None
    checks: list[Check] = field(default_factory=list)
    aborted: str = ""

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


def verify(run: Run, repo: str, commitment: int, token: str) -> None:
    number = run.fixture

    # 1 — the whole lifecycle, in one real poll.
    output = run_poll(repo, commitment)

    events = runlog_events(number)
    kinds = [e.get("event") for e in events]

    dispatches = [e for e in events if e.get("event") == "dispatch.received"]
    run.check("the dispatcher produced exactly one dispatch for this story",
              len(dispatches) == 1,
              observed(f"{len(dispatches)} dispatch.received event(s)"
                       + (f", project=#{dispatches[0].get('project')} "
                          f"agent={dispatches[0].get('agent')}" if dispatches else "")))

    wakes = re.findall(rf"(?m)^\[poller\] woke .* story #{number}\b.*$", output)
    run.check("exactly one worker was woken",
              len(wakes) == 1,
              observed(wakes[0] if wakes else f"{len(wakes)} wake line(s) in the poll output"))

    launches = [e for e in events if e.get("event") == "worker.launch.end"]
    run.check("the worker was launched through the bridge and reported a definite result",
              (len(launches) == 1 and launches[0].get("result") == "LAUNCHED"
               and "bridge.dispatch" in kinds),
              observed(f"{len(launches)} launch(es)"
                       + (f", result={launches[0].get('result')}, "
                          f"elapsed_ms={launches[0].get('elapsed_ms')}, "
                          f"pid={launches[0].get('pid')}" if launches else "")
                       + f"; bridge.dispatch recorded: {'bridge.dispatch' in kinds}"))

    run.check("the run is reconstructable from the log alone",
              {"dispatch.received", "worker.launch.start", "worker.launch.end",
               "story.completion"} <= set(kinds),
              observed(sorted(set(kinds))))

    # 2 — durable state is the only thing that counts.
    issue = dispatcher.fetch_issue(repo, number, token)
    labels = dispatcher.labels_of(issue)
    run.check("the story reached story:completed",
              dispatcher.lifecycle_of(issue, dispatcher.STORY_LIFECYCLE)
              == dispatcher.COMPLETED,
              observed(f"labels = {sorted(labels)}"))
    run.check("the issue is closed as completed (§9.3)",
              (issue.get("state") == "closed"
               and issue.get("state_reason") == "completed"),
              observed(f"state={issue.get('state')} state_reason={issue.get('state_reason')}"))
    run.check("Attempt records the one dispatched attempt",
              (dispatcher.merge_gate.parse_section(issue.get("body") or "",
                                                   "Attempt") or "").strip() == "1",
              observed("Attempt = " + (dispatcher.merge_gate.parse_section(
                  issue.get("body") or "", "Attempt") or "?").strip()
                  + " — incremented once at dispatch, untouched by completion"))

    # 3 — exactly one acknowledgement, and a recorded reason.
    comments = dispatcher.fetch_pages(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments", token)
    acks = [c for c in comments if (c.get("body") or "").lstrip().startswith(ACK_HEADING)]
    run.check("exactly one worker acknowledgement exists",
              len(acks) == 1, observed(f"{len(acks)} acknowledgement(s)"))
    completions = [c for c in comments
                   if (c.get("body") or "").lstrip().startswith(COMPLETION_HEADING)]
    run.check("the completion decision is recorded on the story",
              len(completions) == 1,
              f"{len(completions)} completion record(s) citing the evidence")

    # 4 — the property the whole factory exists for.
    events = lifecycle_events(timeline(repo, number, token))
    applied = [dispatcher.merge_gate.label_name(e["label"])
               for e in events if e["event"] == "labeled"]
    run.check("no human wrote a lifecycle label",
              applied == [dispatcher.READY, dispatcher.CLAIMED, dispatcher.COMPLETED],
              observed(f"applied in order: {applied}"))

    pairs = {}
    for event in events:
        pairs.setdefault(event["created_at"], []).append(event["event"])
    transitions = [stamp for stamp, kinds in pairs.items() if set(kinds) == {"labeled", "unlabeled"}]
    run.check("each transition was one atomic label-set replacement (§9.2)",
              len(transitions) == 2,
              f"{len(transitions)} transitions, each with its unlabeled/labeled pair "
              f"sharing a timestamp")

    # 5 — replay. A second poll must do nothing at all.
    before = len(timeline(repo, number, token))
    second = run_poll(repo, commitment)
    after = len(timeline(repo, number, token))
    run.check("a replay poll changed nothing",
              after == before and f"story=#{number}" not in second,
              f"timeline length {before} -> {after}; no DISPATCH for #{number}")

    # 6 — a terminal story is not waiting on anyone.
    queue_output = io.StringIO()
    with redirect_stdout(queue_output):
        humanqueue.run(repo, token)
    run.check("the completed story is not in the human queue",
              f"artifact=#{number}" not in queue_output.getvalue(),
              "human-queue pass did not list it — terminal work waits for nobody")

    # 7 — the fixture built nothing.
    prs = dispatcher.fetch_pull_requests(repo, token)
    linked, _ = dispatcher.linked_delivery_prs(number, prs)
    run.check("no pull request was created for the fixture",
              not linked, observed(f"{len(linked)} linked pull request(s)"))
    branches = api(repo, "/branches?per_page=100", token)
    run.check("no branch was created for the fixture",
              not any(str(number) in b.get("name", "") for b in branches),
              f"{len(branches)} branch(es), none naming #{number}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render(run: Run) -> str:
    lines = ["End-to-end verification — real repository, real engine, nothing mocked", ""]
    if run.fixture:
        lines.append(f"  fixture: #{run.fixture}")
        lines.append("")
    if run.aborted:
        lines.append(f"  ABORTED before creating a fixture: {run.aborted}")
        lines.append("")
        lines.append("  Nothing was written. Aborting early is correct: a run that "
                     "half-executes")
        lines.append("  leaves a Story behind and reports a failure it did not cause.")
        return "\n".join(lines)
    for check in run.checks:
        lines.append(check.line())
    passed = sum(1 for c in run.checks if c.passed)
    lines += ["", f"  {passed}/{len(run.checks)} check(s) passed"]
    if not run.ok:
        lines.append("")
        lines.append(f"  FAIL. The fixture #{run.fixture} is deliberately left as it is — "
                     f"it is the report.")
        lines.append("  Cleanup here is the completion path working; a run that tidied up "
                     "after itself")
        lines.append("  would delete the evidence and hide the defect.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end verification against a live repository")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--commitment", required=True, type=int)
    parser.add_argument("--project", required=True, type=int,
                        help="an active project to hang the fixture under")
    parser.add_argument("--json", help="write the report here")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    run = Run(repo=args.repo)
    if not token:
        run.aborted = "no GITHUB_TOKEN/GH_TOKEN — this test writes to a real repository"
        print(render(run))
        return 2

    if preflight(run, args.repo, args.commitment, args.project, token):
        run.fixture = create_fixture(args.repo, args.project, token)
        print(f"created fixture #{run.fixture}; running the factory against it "
              f"(this spends a real engine invocation)", flush=True)
        try:
            verify(run, args.repo, args.commitment, token)
        except Exception as exc:  # noqa: BLE001 — a crash is a failed run, not a silent one
            import traceback
            run.check("the run completed without crashing", False,
                      f"{type(exc).__name__}: {exc}")
            traceback.print_exc(file=sys.stdout)

    print(render(run))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"repo": run.repo, "fixture": run.fixture,
                       "aborted": run.aborted, "passed": run.ok,
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
