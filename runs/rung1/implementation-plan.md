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
  Build the reproducible Rung 1 KPI report and Project #322 — Phase 5 Rung 1 —
  toy /health rung with a reproducible KPI report are historical requirements
  and evaluation input. They do not authorize or define this direct Codex
  implementation. The implementation boundary comes from the Phase 5 objective,
  current `main`, and the accepted evidence.

- **Black-box evidence.** Project #410 proves the normal production path, but it
  predates the Rung 1 reporter and its durable touch log contains plan approval
  without the later acceptance decision. It cannot prove that Rung 1's complete
  measurement path is correct. A fresh black-box `/health` run is required after
  the reporter exists.

Open Projects and their dependency state are not implementation requirements by
themselves. Codex implements Phase 5 directly; the factory is the system under
test. No old Story or Project lifecycle is advanced, repaired, or used to add
scope.

## Minimum implementation

### Evidence inputs — reuse before collection

Do not build a generalized snapshot service. Reuse the durable run bundle added
by PR #415 — Repair observable black-box factory delivery: `evidence.json`, the
typed process-event stream, telemetry stream, operation stream and touch-log
receipts. Add a field to the Rung 1 bundle only when the table below identifies
it as missing.

| KPI | Required raw fields | Existing source | Missing collection, if any |
|---|---|---|---|
| Human touches | project, story, bell type, classification, seconds, timestamp | `factory/touchlog/touchlog.jsonl` and run receipts | Canonical acceptance receipt must be present in the frozen bundle |
| Autonomy | merged Story, factory claim trace, delivered head, any human code intervention | process events plus Story/PR evidence | Human code involvement is not independently attributable under the shared credential; report unavailable unless the run produces independent evidence |
| Attempts / retries | Story, claim transition event ID, attempt | process events (`story.claimed`) | None |
| Poison rate | Story lifecycle label walk | `evidence.json` | Preserve `story:blocked:poison` if observed |
| Escaped defects | defect observation, source, discovery time after merge | sampling and acceptance artifacts | Record only explicit observations; otherwise unavailable |
| Acceptance catches | canonical criterion result at outcome acceptance | Project acceptance comment | Freeze the parsed canonical result in the run bundle |
| Usage / cost | engine, phase, Story, reported usage and cost availability | telemetry (`engine.usage`) | None; do not add pricing |
| Cycle time | canonical plan-approval and outcome-acceptance timestamps | Project comments/timeline | Freeze both verified boundaries in the run bundle |

The Rung 1 harness performs the smallest read-only GitHub capture needed for the
three missing acceptance-bound fields: the canonical acceptance receipt,
criterion results and decision timestamp. It writes them into the run's existing
`evidence.json`; it does not introduce a reusable collector, service or new
observability stream. Report reproducibility is defined over that frozen bundle,
not repeated live API reads.

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

Measurement integrity and KPI availability are separate results. Integrity
fails when evidence is missing, contradictory, malformed, duplicated where
uniqueness is required, or cannot be traced to its named source. Availability
states whether a trustworthy numeric value can be derived. An honest
`unavailable` cost, autonomy, escaped-defect or acceptance-catch value does not
by itself fail Rung 1. It is reported with the exact missing evidence. Rung 1
fails only when the production outcome fails, manual glue or relay occurs, or
the measurement mechanism makes an unsupported claim.

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

The exact external entrypoint is:

```bash
sh poll.sh --interval <approved interval>
```

The harness may start and stop that process, but it may not import or directly
invoke dispatcher, worker, reviewer, sequencer or merge-gate internals to make
progress. The tested production path has no mocked, stubbed, faked or substituted
integration or dependency anywhere: GitHub, coding engine, reviewer engine,
merge checks and lifecycle routing are all real. Unit and deterministic tests
may substitute dependencies, but their evidence cannot establish UAT PASS.

Before spending an engine call or creating a Story, preflight fails if any
production substitution override is present, including
`FACTORY_DELIVERY_MODEL_CMD`, `FACTORY_REVIEW_MODEL_CMD`, `FACTORY_WORKER_CMD`,
`FACTORY_REVIEW_CMD`, or any `FACTORY_WORKER_*` override. It also verifies real
GitHub authentication, real coding and reviewer engine authentication, required
merge checks, the approved Project state and the writable evidence location.
The run must not move lifecycle labels by hand, rescue a failure into green, or
add a test-only production route. Monitor it only with the repository's
`factory-monitor` script.

The UAT is PASS only if the intended `/health` outcome is achieved without
manual glue and the generated KPI report passes its evidence-integrity checks.
An unavailable real dependency makes the verdict INCONCLUSIVE. A completed run
that misses the outcome, records relay, or has an incorrect touch log is FAIL.

Owner plan approval and outcome acceptance remain real human decisions. They
must be stated by the owner before being transcribed to GitHub. Stop after the
Rung 1 verdict; do not begin Rung 2.

## Deliverables and exclusions

The implementation deliverables are the small acceptance-bound evidence freeze
inside the existing run bundle, deterministic KPI reporter, fixtures and tests,
the two generated reports, and the final evidence digest. No generalized
snapshot collector, dashboard, supervisor, webhook runtime, event bus,
observability system, agent redesign, coverage threshold or cost ceiling is
added.
