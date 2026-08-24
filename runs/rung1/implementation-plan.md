# Phase 5 Rung 1 — KPI plumbing implementation plan

This is a plan for review. It contains no implementation and records no owner
decision.

## Reconciliation against current evidence

The original Phase 5 plan asked Rung 1 to prove the complete `/health` delivery
loop and KPI plumbing. The delivery rails no longer need rebuilding.

- **Delivery path.** The original gap was an unproven full worker/reviewer/merge
  loop. The current production path enters through `poll.sh`. Project #410 —
  Final black-box two-Story factory UAT exercised two dependent Stories using
  real GitHub, Codex delivery, fresh-context review, both merge checks,
  dependency sequencing, runtime merge, Project completion, observability and
  inert replay. PR #415 — Repair observable black-box factory delivery placed
  that path on `main`. This gap no longer exists and does not block KPI plumbing.

- **Engine usage.** The original gap was that cost could not be measured.
  Story #337 — Capture per-invocation engine usage in the runtime log added
  durable engine-reported usage. Claude reports monetary cost; Codex currently
  reports token usage without a monetary cost. Usage is measurable, but total
  cost per accepted Story is unavailable unless every invocation in the rung
  reports cost. Missing cost must never be treated as zero or priced from a
  hand-maintained rate table.

- **KPI report.** No reproducible Rung 1 report exists on `main`. Story #338 —
  Build the reproducible Rung 1 KPI report remains open and is the minimum
  implementation gap.

- **Black-box evidence.** Project #410 proves the normal production path, but it
  predates the Rung 1 reporter and its durable touch log contains plan approval
  without the later acceptance decision. It cannot prove that Rung 1's complete
  measurement path is correct. A fresh black-box `/health` run is required after
  the reporter exists.

Open Projects are not implementation requirements by themselves. Project #335 —
Do not approve a Project that has no Stories is not part of the KPI implementation.
The temporary dependency from Story #338 to that Project remains unchanged until
the owner or factory lifecycle restores it.

## Minimum implementation

### Recorded evidence snapshot

Add a deterministic, read-only capture command for Project #322 — Phase 5 Rung
1 — toy /health rung with a reproducible KPI report. It captures the GitHub
artifacts needed by the report: Project and Story timelines, lifecycle labels,
comments containing canonical decisions, linked pull requests, exact heads,
merge timestamps and check results. It also captures the relevant runtime
process and telemetry records and touch-log entries.

The capture command never changes GitHub state. Its output is a versioned JSON
snapshot under `runs/rung1/`. Report reproducibility is defined over this
snapshot, not repeated live API reads.

### KPI report

Extend `factory/acceptance/rung1_report.py` so one named command reads the
recorded snapshot and writes byte-stable `runs/rung1/report.json` and
`runs/rung1/report.md`.

The report contains these eight measurements and their evidence boundaries:

1. **Human touches.** Enumerate touch-log records for the Project. Report count,
   classification and seconds. Report relay separately. Reconcile plan approval
   and acceptance decisions against touch receipts; a missing or duplicate
   receipt fails measurement integrity.
2. **Autonomy.** Report autonomous merges over total Stories only when durable
   evidence distinguishes factory-authored delivery from human code involvement.
   The shared GitHub principal does not establish that absence by itself. If no
   independent evidence exists, report unavailable and state why.
3. **Worker attempts and retry rate.** Count unique durable `story.claimed`
   transitions. Deduplicate replay by transition event identity. Report total
   attempts, attempts per Story, retries beyond the first attempt and retry rate.
4. **Poison rate.** Count Stories whose timeline entered
   `story:blocked:poison`, divided by all rung Stories.
5. **Escaped defects.** Count only defects explicitly recorded after merge by a
   sampling audit or outcome acceptance. Otherwise report unavailable. Absence
   of a defect comment is not proof of zero.
6. **Acceptance catches.** Count criteria explicitly recorded as failed at
   outcome acceptance. A canonical passing checklist supports zero catches;
   missing or malformed acceptance evidence produces unavailable.
7. **Engine usage and cost.** Sum actual engine-reported usage by engine and
   phase. Compute cost per accepted Story only when every included invocation
   reports monetary cost. Otherwise report known partial cost and mark the total
   and cost per Story unavailable.
8. **Cycle time.** Compute elapsed time from the canonical plan-approval comment
   timestamp to the canonical outcome-acceptance comment timestamp. Reject
   missing, duplicate or reversed boundaries.

The JSON report includes the source artifact identifiers and observation cutoff.
The Markdown report presents the same values and unavailable states without
adding interpretations not present in the JSON.

## Deterministic verification

Tests use recorded fixtures, never live GitHub. They prove:

- two report generations from the same snapshot are byte-identical;
- duplicate replay records do not increase attempt counts;
- retries, poison transitions and relay touches change their respective KPIs;
- touches belonging to other Projects are excluded;
- missing or duplicate decision receipts fail integrity;
- malformed or incomplete acceptance evidence cannot produce zero catches;
- missing post-merge observations cannot produce zero escaped defects;
- missing monetary cost from one invocation makes total cost per Story
  unavailable while preserving known usage and partial cost;
- a failed or incomplete black-box run cannot produce a successful Rung 1
  report; and
- no coverage threshold or monetary ceiling is introduced.

Run the repository-owned checks rather than ad-hoc substitutes:

```bash
python3 factory/acceptance/requirement_coverage.py
python3 factory/coverage_report.py --python /tmp/factory-cov/bin/python --check
```

The coverage command requires a throwaway environment outside the repository
with `coverage.py` installed. A missing dependency is reported as unavailable;
it is not replaced with another measurement.

## Black-box Rung 1 proof

After the KPI implementation is merged, run one fresh toy `/health` Project
through the same normal external path used for real work. The fixture remains
under `runs/rung1/live_product/**`, as approved on Project #322.

The run must use `poll.sh`, real GitHub, real Codex delivery, and the configured
fresh-context reviewer. It must not invoke internal components directly, use
engine overrides, mock an integration, move lifecycle labels by hand, or add a
test-only production route. Monitor it only with the repository's
`factory-monitor` script.

The UAT is PASS only if the intended `/health` outcome is achieved without
manual glue and the generated KPI report passes its evidence-integrity checks.
An unavailable real dependency makes the verdict INCONCLUSIVE. A completed run
that misses the outcome, records relay, or has an incorrect touch log is FAIL.

Owner plan approval and outcome acceptance remain real human decisions. They
must be stated by the owner before being transcribed to GitHub. Stop after the
Rung 1 verdict; do not begin Rung 2.

## Deliverables and exclusions

The implementation deliverables are the snapshot capture, deterministic KPI
reporter, fixtures and tests, the two generated reports, and the final evidence
digest. No dashboard, supervisor, webhook runtime, event bus, generalized
observability system, agent redesign, coverage threshold or cost ceiling is
added.
