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

## The kill criterion

If a scenario cannot be made to pass without changing a frozen contract in
`factory/spec/`, **stop and escalate**. A green suite bought by amending the rule
it was meant to check is worth less than a red one.
