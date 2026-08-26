# Phase 2 acceptance suite

**The question this answers: does Phase 2 work?**

The rest of the repository answers a different one. 423 component tests ask *is
this function correct against inputs it was handed* — necessary, and not a
substitute. The dependency defect #107 fixed passed every one of them, because
every one of them handed the evaluator a pre-built map instead of making it go
and look.

```bash
python3 factory/acceptance/run_acceptance.py            # everything, with evidence
python3 factory/acceptance/run_acceptance.py --quiet     # verdicts only
python3 factory/acceptance/run_acceptance.py --scenario S11
python3 -m unittest discover -s factory/acceptance -p 'test_*.py'
```

Exit status is the verdict. `report.json` is written beside the runner, because
a suite whose result exists only in a terminal is not evidence.

Before the fresh Phase 5 repeat, combine the four real evidence producers into
one JSON object and validate it with:

```bash
python3 factory/acceptance/pre_rung3_regressions.py EVIDENCE.json \
  --json runs/pre-rung3-regressions.json
```

The four keys are `project47_scale`, `project30_provider`,
`capacity_recovery`, and `adapter_contract`. The command does not manufacture
evidence. It rejects toy portfolio checks, fixture-only or stale provider
claims, capacity recovery that mutated before admission or started twice, and
live adapter probes missing any contract dimension. Exit `0` means all four
real evidence records passed; exit `1` means at least one failed; exit `2`
means the bundle itself was absent or malformed.

Run the readiness doctor before starting a mutable poller. Its default
`rehearsal` mode remains for disposable harnesses and requires an empty
test-only commitment plus a fresh target. For an approved Project whose planned
Stories already exist, use the separate fail-closed mode:

```bash
python3 factory/acceptance/e2e_doctor.py \
  --mode preplanned --repo owner/product --commitment 59 --project 60
```

`preplanned` requires clean factory code at the exact local `origin/main`, one
open Roadmap Commitment containing only the named Project, and an exact match
between the Project's declared Stories and its open, not-yet-started Story
issues. It does not use the disposable fresh-file check. Both modes probe real
capacity, merge controls, worktree creation, poller exclusivity, observability,
and the normal read-only `poll.sh --dry-run` path before issuing the same
short-lived receipt.

## What a scenario is

Three rules, each there because dropping it turns the suite back into unit tests
wearing a different name.

**Drive a real entry point.** `poller.poll_once`, `poller.main`,
`dispatcher.main`, `merge_gate.evaluate`, `review_link.run`, `humanqueue.run`,
`replay.replay`. Never a helper reached around them, never a re-implementation
of a rule that already has a home.

**Assert on durable state.** Labels, open/closed, `state_reason`, comments, the
timeline — plus stdout and the runlog, which are the factory's only other
outputs. Not on return values: a return value is not something the factory keeps.

**Say what the evidence was.** Every scenario records the observations its
verdict rests on and the report prints them. A green suite that cannot show its
working is an assertion, not evidence.

## The double, and the one thing that makes this a test

`github_double.FakeGitHub` sits at `dispatcher._api`, the single choke point
every read and write passes through. Two of its behaviours are load-bearing:

* The open-issue listing **refuses to serve closed issues**, and asserts the
  caller asked for `state=open`. A double that served them would let the
  dependency scenario pass against the dispatcher as it stood before #107.
* Label writes **append `labeled` and `unlabeled` timeline events**, because the
  factory reads history rather than snapshots for every decision it cares about.
  The claim instant a lease turns on is a timeline event; a double that skipped
  them would test a lifecycle that does not exist.

One substitution is worth naming: `poller.run_dispatcher` calls
`dispatcher.main` **in-process** instead of as a subprocess. The subprocess
boundary is real and a suite that skipped it would miss a dispatcher that cannot
start — it is covered by the dispatcher's CLI tests and by the live runs in the
acceptance evidence. What the substitution buys is that the dispatcher's
decisions and the poller's reactions meet over one repository, deterministically.

`continuation.py` keeps its own HTTP client rather than going through
`dispatcher._api`, so the choke point does not reach it; `driver.factory` routes
its three I/O functions to the same repository. That is a shim, not a stub — the
decision logic under test is still continuation's own. **Worth recording as a
finding:** the "single I/O choke point" property holds for every module except
that one.

## The sixteen scenarios

| # | Scenario | Phase 2 behaviour |
|---|---|---|
| S1 | Deterministic dispatch | ordering by (project, story), WIP, atomic claim, `Attempt` |
| S2 | Authorization chain | every broken link refused **and named** |
| S3 | Trust boundary | §9.9 — a stranger's issue, a stranger's project, persuasive prose |
| S4 | Dependencies | closed `merged`/`completed` satisfy; `cancelled`/poison/untrusted do not |
| S5 | WIP and attempt limits | capacity, terminal states releasing it, the §4.3.5 threshold |
| S6 | Claim recovery | fresh, expired, merged delivery, ambiguity, the §9.4.1 budget |
| S7 | Replay and restart | one claim across two polls; the §9.15 replay of recorded history |
| S8 | Worker selection and launch | `FACTORY_WORKER_ORDER`, routing identity only |
| S9 | Execution observability | launch records, both streams, redaction |
| S10 | No-deliverable completion | §9.16, and a worker proving nothing reaching no terminal state |
| S11 | PR and merge reconciliation | `claimed → in-review → merged`, ambiguity failing closed |
| S12 | Worker failure and failover | definite failure falls back, ambiguity never does |
| S13 | Scope enforcement | each gate violation class independently red |
| S14 | Fail closed | no credential, crash, malformed dispatch — loud, non-zero, no writes |
| S15 | Human queue | §9.11's no-silent-drops, and the repetition property |
| S16 | Contract conformance | every routed label and contract value matches the schema |

## The layer above this one — `e2e.py`

Everything above is hermetic. `dispatcher._api` and `workers.run_observed` are
both replaced, so this suite never touches the real GitHub API, the real `claude`
binary, the subprocess boundary in `poller.run_dispatcher`, or any network
failure mode. It is a **high-fidelity integration suite that asserts acceptance
criteria** — calling it "production-shaped" describes its shape, not its reach.

`e2e.py` is the layer that does reach:

```bash
./live-e2e.sh --only dispatch      # one requirement
./live-e2e.sh --list               # the requirement map; runs nothing
./live-e2e.sh                      # every reachable requirement
```

`live-e2e.sh` supplies the worker declarations the contract already defines (#84)
and nothing else. It chooses none of the factory's behaviour, and the preflight
below rejects a declaration that could not honour the completion contract rather
than trusting the wrapper to be right. The underlying command is unchanged:

```bash
GITHUB_TOKEN=$(gh auth token) python3 factory/acceptance/e2e.py \
    --repo owner/name --commitment 54 --project 109
```

It creates a disposable Story, runs the real factory against it, and asserts the
durable state that comes out — 16 checks covering dispatch, launch through the
bridge, the acknowledgement, completion, atomicity, replay safety, the human
queue, and that nothing was built. It is named `e2e.py` rather than `test_e2e.py`
precisely so `unittest discover -p 'test_*.py'` cannot collect it by accident.

**It costs a real engine invocation and writes a real issue**, and it says so
before it does either. An E2E test that looks free gets run in a loop by someone
who did not read this.

The Capacity Pool provider failover proof is read-only and writes only its local
evidence file. It deliberately reports Anthropic unavailable before inference,
then requires the real OpenAI GPT-5.6 Sol adapter to complete the same flagship
medium Review-class request inside one combined envelope:

```bash
python3 factory/acceptance/capacity_failover_live.py \
  --output runs/capacity-failover/evidence.json
```

It also advances the failed provider scope through cooldown and a successful
probe before recording it healthy. The command exits non-zero if fallback,
schema validation, envelope accounting, or recovery is not proven.

**Cleanup is the behaviour under test.** §9.3 forbids any component from
cancelling a Story, so this cannot close its own fixture — a teardown would be a
component doing what the contract reserves to a human, and would delete the
evidence besides. The fixture is a bounded no-deliverable assignment, so the
worker acknowledges it and the completion path closes it under §9.16. A fixture
left behind means something is broken, and the fixture is the report.

**It never gates a merge.** An E2E run depends on live repository state and on an
external engine being reachable. A required check that fails for reasons the
author cannot fix is a check people learn to route around.

### Requirement coverage, and its two honest holes

`e2e.py --list` prints the map. Fourteen of fifteen Phase 2 requirements are
reachable against a live repository; the report names every one on every run,
including the ones it did not cover, because a suite that lists only what it
proves reads as complete.

**`trust-boundary` is unreachable and will stay so.** `author_association` is
computed by GitHub from repository membership, so this credential cannot author
an untrusted issue. Decision #27 chose a single identity, which makes this
blocked by a recorded architectural choice rather than by effort. A second
identity (#26) unlocks it; acceptance scenario S3 covers it hermetically
meanwhile.

**`recovery` is deferred, not skipped.** `CLAIM_LEASE` is sixty minutes measured
from a durable timeline event, and timeline events cannot be backdated. Two
invocations an hour apart reach it, with the fixture issue as the only state
that has to survive between them:

```bash
python3 factory/acceptance/e2e.py --only recovery ...            # phase one
python3 factory/acceptance/e2e.py --only recovery --resume 131 ... # 60+ min later
```

Two more are **attested rather than exercised**: `review-open` and
`review-merged` are verified against durable history — #122, #124 and #126 each
walked `claimed → in-review → merged` with every label written by a component —
because causing them live means a commit on `main` per run. That is a weaker
claim than exercising them, and the report marks it as one.

### What building it found



The first live run scored 14/15, and the failure was in the test rather than the
factory: it asserted the canonical `DISPATCH` line against the poller's stdout,
where it never appears — `run_dispatcher` captures the dispatcher subprocess's
output and parses it. The assertion now reads the runlog, which is both the
durable record and what an operator actually reads after the fact.

The same run exposed a worse habit: evidence strings written from the
*expectation* rather than the observation, so a failing check printed
"appears once in the poll output" while reporting FAIL. Evidence is now rendered
from what was seen, which is why a failing check is worth reading.

## The rule this suite keeps relearning

**Assert on the timeline, never on the current label.**

Three defects in this suite have had the same root cause, and all three passed
review at the time:

1. The `DISPATCH` line asserted against the poller's stdout, where it never
   appears — `run_dispatcher` captures and parses it.
2. The Dispatch scenario read `lifecycle == story:claimed` after a poll that had
   already completed the fixture.
3. The Recovery scenario read `lifecycle in (story:ready, story:claimed)` after a
   recovery — a condition true whether or not anything happened, because §9.4
   recovers *before* selection and the same run re-claims:

```
15:22:45  unlabeled story:claimed
15:22:46  labeled   story:ready     <- the recovery
15:22:47  unlabeled story:ready
15:22:47  labeled   story:claimed   <- re-dispatched, same run
```

Net `Attempt 1 -> 1`, final label `story:claimed`, and `count_expiry_recoveries`
= 1. Only the last number says what occurred.

A label is a **current value** in a system whose whole design is that passes move
it. The timeline is durable, ordered, and the thing every other component in this
repository routes on. Assert there.

**And never take a claim without dispatching a worker.** `dispatcher.main --claim`
writes `story:claimed` and launches nothing, so the fixture strands until the
sixty-minute lease expires. It has bitten this suite three times. It is not a
state the factory produces — `poll_once` always dispatches and launches together
— so a scenario that produces it is testing a system that does not exist.

## The kill criterion

If a scenario cannot be made to pass without changing a frozen contract in
`factory/spec/`, **stop and escalate**. A green suite bought by amending the rule
it was meant to check is worth less than a red one.
