---
name: coverage
description: Measure this repository's test coverage by layer (unit, integration, acceptance) and report what each layer uniquely contributes. Use when asked about test coverage, which code is untested, whether a test layer is earning its keep, or to re-baseline coverage after adding tests. Produces the same numbers on every run.
---

# Coverage by test layer

Run the script. Do not improvise the measurement in the shell — that is the
mistake this skill exists to stop.

```bash
python3 factory/coverage_report.py --python <interpreter-with-coverage>
```

## Two different questions — pick the right one

**"Does every requirement have a test?"** — `factory/acceptance/requirement_coverage.py`.
Fully deterministic: reads declarations, runs nothing, needs no credential. Ask
this **first**. A requirement with zero tests is invisible to any percentage, and
this is the only check that surfaces it.

For Phase 4 run `python3 factory/acceptance/requirement_coverage.py --phase4`.
It requires named hermetic evidence for P4-01 through P4-16 and fails wiring
claims until `runs/phase4/evidence.json` records a passing live result or a
complete owner-approved limitation naming why, substitute evidence, and risk.

**"How many lines do the tests execute?"** — `factory/coverage_report.py`, below.

They were conflated once here, and the cost was a measurement answering a
question nobody asked. Line coverage would not have caught #107: every line was
covered while the dispatcher was rejecting every satisfied dependency in
production for two days.

## Why a script rather than shell commands

The first time this measurement was made here it was done with ad-hoc `coverage`
invocations typed into a session. The numbers were right, and they were useless:
nobody could reproduce them, nothing would notice when they changed, and one
early attempt reported three phantom test failures caused by stale
`__pycache__`. A measurement that only exists in a transcript is an anecdote.

So: the script is the measurement. If it needs to change, change the script.

## Getting an interpreter

This repository has **zero dependencies** by design and `coverage.py` is not
vendored. Create a throwaway environment outside the repository:

```bash
python3 -m venv /tmp/factory-cov
/tmp/factory-cov/bin/pip install coverage
python3 factory/coverage_report.py --python /tmp/factory-cov/bin/python
```

Without it the script prints this and exits `3`. **That is the correct
outcome** — do not substitute a cruder measurement and present it as the same
figure.

## Flags

| Flag | Effect |
|---|---|
| `--python PATH` | interpreter with `coverage` importable (default: current) |
| `--check` | measure twice in separate processes and diff; exit `4` if they disagree |
| `--json PATH` | also write the machine-readable report |
| `--workdir PATH` | keep coverage data files instead of using a temp dir |

Exit codes: `0` clean · `1` tests failed · `3` no `coverage.py` · `4` non-deterministic.

## Reading the output

Three sections, and the second is the one that matters.

**Per layer, measured alone** — what each layer covers by itself.

**Unique contribution** — remove the layer, where does coverage land. A layer
that adds little is re-walking paths another layer already covers. Treat this as
information, not a verdict: line coverage cannot see wiring, and the acceptance
suite exists to prove composition. Every unit test in this repository passed
while the dependency defect in #107 blocked the factory completely — the lines
were covered and the composition was wrong.

**Per module, combined** — sorted worst first, because that is the reading order
that leads somewhere.

## Rules this skill holds to

**The end-to-end layer is opt-in and reported apart.** `--with-e2e REPO
COMMITMENT PROJECT` measures it — but it **writes to a real repository** and
spends real engine invocations, and what it covers depends on what was in that
repository when it ran. That is irreducibly non-deterministic, so it is never
folded into the figure `--check` verifies; `--check` and `--with-e2e` are
mutually exclusive and the error says why.

Non-deterministic does not mean advisory: a failed end-to-end run reaches the
exit code, and the failing checks are printed by name. An earlier version
reported `46/47 check(s) passed`, discarded which one, and exited `0` — a
failure shaped so that nobody would act on it.

**Never gate on a coverage threshold.** Coverage is reported, never enforced. A
required check with a coverage floor is a number the code under test can raise
by writing shallow tests, which is exactly the class of self-certifying control
`factory/spec/state-schema.md` §9.14 rules out. Report it, read it, decide.

**Never quote a number measured against failing tests.** The script warns and
exits `1`. Fix the tests first; a coverage figure from a red suite describes
code that does not work.

**Classify new test files explicitly.** `INTEGRATION` and `ACCEPTANCE` are named
sets in the script; unit is the remainder. A file declared but missing is a hard
error. When a new test file appears, decide which layer it belongs to and say so
in the script — a misfiled test moves a layer's number without changing a line
of code.

Phase 4 also reports worker, reviewer, sampling, review-routing, review-link,
and poller modules as a named group followed by uncovered risks. Story #220
must post the deterministic `--check` result and a later, separate `--with-e2e`
result; never fold the live percentage into the verified figure.

## What determinism costs, and why

`--check` exists because a coverage number that drifts invites arguing with the
measurement instead of the code. Each control in `clean_environment()` answers a
threat observed in this repository, not an imagined one: leaked `FACTORY_WORKER_*`
routes the runtime down a different path; a present `GITHUB_TOKEN` lets an
escaped mock reach the network and makes coverage depend on GitHub being up;
`coverage` appends by default so a stale data file inflates everything; stray
`__pycache__` turns an empty directory into a package and `NO TESTS RAN` into a
failing suite.

If you add a control, add the threat it answers to the module docstring. A
control whose reason is not written down is one a later reader will delete.
