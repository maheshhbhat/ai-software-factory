# Project #422 — Phase 5 Rung 1 real /health KPI UAT approval details

This document presents the exact live-test authorization for review. It records
no owner decision by itself.

## What approval authorizes

One disposable black-box factory run will create one real GitHub Story and use
real Codex delivery plus the configured real reviewer. The Story will implement
a toy `/health` endpoint under `runs/rung1/live_product/**`. The normal
`poll.sh` production path owns claim, delivery, review, checks, merge, Story
completion and movement of the Project to its acceptance bell.

The run spends real engine usage and creates durable GitHub issues, a pull
request, commits, comments, checks and logs. The Story spend cap is $5 / 60
minutes. Actual monetary cost will be reported only when the engines report it;
missing cost is never estimated.

Approval does not authorize Phase 5 Rung 2 — Small Real Feature, new factory
infrastructure, manual lifecycle repair, or rescuing a failed run into green.

## Owner actions

Before launch, the owner confirms no competing poller is running:

```bash
pgrep -af 'poll\.sh|factory/runtime/poller.py'
```

No output means the guard passes. Any matching process must be resolved before
launch.

The owner makes two decisions:

1. Approve or reject these UAT criteria before engine spend.
2. After the evidence digest is posted, accept or reject the observed outcome
   criterion by criterion.

Starting the bounded harness is recorded as an operator action. It is not
reported as autonomous software delivery and is not a lifecycle relay.

## Falsifiable acceptance criteria

- [ ] The run enters only through `sh poll.sh --interval 15` and delivers exactly one `/health` Story using real GitHub, real Codex delivery, the real configured reviewer, real merge checks, real lifecycle routing and runtime merge; no integration or dependency in the production path is mocked, stubbed, faked or substituted.
- [ ] The merged `/health` endpoint is executed from fresh `main` and returns JSON containing the exact GitHub merge SHA; a missing merge, wrong SHA, invalid response or edit outside the Story scope fails the run.
- [ ] The frozen run bundle and generated reports name all eight KPIs: human touches with relay explicit, autonomy, worker attempts and retry rate, poison rate, escaped defects, acceptance catches, actual engine usage/cost where measurable, and cycle time.
- [ ] Measurement integrity passes: every claim traces to its named durable source, required decisions and touch receipts appear exactly once, replayed claim events are deduplicated, and missing evidence is never converted to zero.
- [ ] KPI availability is reported separately from integrity; an honestly unavailable monetary cost, autonomy or quality measurement names the missing evidence and is not fabricated.
- [ ] Human intervention is limited to fixture launch, plan approval and outcome acceptance; every lifecycle transition between approval and the acceptance bell is factory-owned, relay is zero, and no failed run is manually rescued into green.
- [ ] Replaying the normal poll after delivery changes no durable delivery state, observability streams are valid, the harness stops its poller, and deterministic tests plus the repository coverage determinism check remain green.
- [ ] The final evidence digest is posted on Project #422 — Phase 5 Rung 1 real /health KPI UAT before acceptance and states what the UAT proved, what remains unproven, every material defect, every human touch or relay, and whether the evidence supports proceeding to Phase 5 Rung 2 — Small Real Feature.

## Verdict rules

- **PASS:** the complete real path achieves the `/health` outcome without
  manual glue, relay, substitution or unsupported measurement claims.
- **FAIL:** the run executes but misses the product outcome, requires manual
  lifecycle repair, records relay, or produces an untrustworthy KPI claim.
- **INCONCLUSIVE:** a real dependency is unavailable, so the complete production
  path cannot run.

An honestly unavailable KPI does not alone fail the rung. The report must state
the unavailable value and the exact missing evidence.

## Decision requested

After reviewing this PR, the owner may state either:

- “I approve all eight criteria for Project #422 — Phase 5 Rung 1 real /health
  KPI UAT.”
- “I reject Project #422,” followed by the criteria or risks requiring changes.

Codex will transcribe an explicit decision to Project #422. This document and
the PR review do not forge or infer that decision.
