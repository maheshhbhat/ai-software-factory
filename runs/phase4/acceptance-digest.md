# Phase 4 acceptance digest

## In simple terms

The factory took one deliberately broken `/health` change from a ready Story to
a merged pull request without a person relaying delivery steps. A fresh reviewer
rejected the first commit, the dispatcher sent the Story back, the worker fixed
the same pull request, a fresh review approved the new exact head, required gates
passed, the runtime merged and closed the Story, sampling ran once, and the
merged endpoint returned the merge SHA.

```mermaid
flowchart LR
  A[Story #279 ready] --> B[Worker: first head]
  B --> C[PR #280 review findings]
  C --> D[Same PR: corrected head]
  D --> E[Exact-head approval]
  E --> F[Required gates]
  F --> G[Automatic merge]
  G --> H[Sampling: unselected]
  G --> I[/health returns merge SHA]
```

## Live result

- Story: #279
- Pull request: #280
- First head: `b3f97402094ce3f6886cfaa736843b2fcf60c45f`
- Corrected head: `dd46cf8f2ff236ba62e6b37b7249c12f0c1e89eb`
- Merge/build SHA: `1093fec0712b58df000dbbd432ab33aa8c84b572`
- Review sequence: findings, then approval on the new exact head
- Sampling: `unselected`, persisted once; no human sampling bell
- Provider cost: unavailable; not fabricated
- Relay operations between automatic ready and merge: zero
- Machine evidence: `trace.json`, `evidence.json`, and `runtime.jsonl` in this directory

## Criterion-by-criterion evidence

| Criterion | Result | Evidence |
|---|---|---|
| P4-01 | Pass | PR #224; worker replay acceptance test |
| P4-02 | Pass | PR #224; private-access and bounded-failure tests; live evidence ledger |
| P4-03 | Pass with accepted limitation | ADR in PR #223; independent principals deferred to Project #221 |
| P4-04 | Pass with accepted limitation | PRs #224 and #228; protected-path tests and controlled live probes; identity evidence remains detective under the shared principal |
| P4-05 | Pass | PR #225; serialized-input, fresh-workspace, environment, and replay tests; live exact-head routing |
| P4-06 | Pass with accepted limitation | PR #225; malformed, unavailable, duplicate, and stale-head fail-closed tests; exact-head outcomes on PR #236 |
| P4-07 | Pass | Story #279 and PR #280 use one PR with two heads and a findings-driven redispatch |
| P4-08 | Pass | PR #228; deterministic gate tests; both required checks passed on PR #236 |
| P4-09 | Pass | PR #226; selected/unselected/replay/corrective/touch tests; PR #280 persisted one unselected result |
| P4-10 | Pass | Fixture ADR and implementation in PR #227; fresh-clone `/health` execution returned the authoritative merge SHA |
| P4-11 | Pass | `trace.json` reconstructs ready → claimed → in-review → ready → claimed → in-review → merged through runtime-owned review and merge with zero delivery relay |
| P4-12 | Pass | `trace.json` and `runtime.jsonl` record identities, Attempts, heads, verdicts, merge, sampling, elapsed observations, and unavailable cost without sessions or secrets |
| P4-13 | Pass | PR #229 and `requirement_coverage.py --phase4`: P4-01–P4-16 each have named hermetic evidence and all ten wiring criteria have live evidence |
| P4-14 | Pass | Deterministic and live coverage measurements below are separate; no threshold was introduced |
| P4-15 | Pass | Phase 4 Stories #214–#220 delivered through PRs #223–#229 plus the pending Story #220 PR; required checks are retained |
| P4-16 | Pass | Phase 3 remains accepted; no retirement-modeling Story or Phase 5 work was implemented |

## Coverage presented before acceptance

Deterministic `--check` result, from two identical isolated runs:

- unit: 69.7% (22 test files)
- integration: 56.4% (3 test files)
- acceptance: 61.1% (9 test files)
- combined: 81.2%
- unique contribution: unit 15.6 points, integration 1.7 points, acceptance 6.6 points
- Phase 4 modules: worker 75.9%, reviewer 68.4%, sampling 85.0%, review route 94.1%, review link 92.5%, poller 78.1%

Separate nondeterministic `--with-e2e` result:

- 44/45 live checks passed
- E2E alone: 47.7%
- deterministic plus observed live paths: 82.3% (+1.1 points), reported beside and never folded into the deterministic figure
- the one unexecuted check requires a real GitHub artifact stamped with an untrusted association; this private repository and shared owner credential cannot create one
- substitute evidence: hermetic trust-boundary acceptance tests and the live GitHub field wiring
- residual risk: independent identity enforcement is unproven until Project #221

## Named residual risks

1. Worker and reviewer share one GitHub principal. Either can forge the other's
   comments or routing artifacts; deterministic CI remains the merge authority.
2. The private repository has no untrusted-authored artifact, so the live
   cross-phase trust-boundary suite honestly reports 44/45. Owner acceptance of
   this limitation is the reserved fifth bell; Project #221 removes it.
3. Coverage measures executed lines and branches, not semantic correctness,
   independent authorization, or all future live repository states.
4. Superseded failed fixture runs remain in GitHub as evidence. They were closed
   only after explicit human disposition; successful fixtures completed through
   the runtime.
