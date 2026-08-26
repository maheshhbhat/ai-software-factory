# Phase 5 pre-Rung-3 factory improvement plan

**Status: proposed plan only. No implementation is authorized by this file.**

The smallest useful change is one shared project risk contract, one independent
readiness verdict, and three poller admission safeguards. This addresses the
Rung 2 findings without adding infrastructure or copying the same checklist into
every agent prompt.

## Outcome and boundary

Before another product run, the factory must:

1. carry representative scale, live-dependency, and responsiveness expectations
   from Planning through Delivery and Review;
2. evaluate the merged product independently before asking for acceptance;
3. refuse mutable polling without a matching doctor result;
4. permit only one poller for a repository and commitment on the operator host;
5. avoid claiming work when no eligible model route is reserved; and
6. classify every invocation outcome and produce complete usage receipts; and
7. produce one deterministic KPI bundle for a fresh independent 2–4 Story Rung
   2 project of **comparable difficulty** to the failed Rung 2, using the same
   success thresholds and a new user-facing outcome rather than the exact failed
   feature.

This plan does not add a database, event bus, distributed scheduler, dashboard,
or new human decision. It does not change Project #30 or #47 product code,
rescue Story #58, or start Rung 3.

## Design

### 1. One operating-envelope artifact

Planning adds a structured `Operating envelope` to each Project. It is empty of
invented requirements: fields are required only when the proposed outcome has
the corresponding risk.

The artifact contains stable IDs and these optional groups:

- representative minimum, normal, and upper-bound inputs;
- response-time and UI-responsiveness expectations;
- external providers, freshness expectations, offline behavior, and whether a
  live read-only check is required;
- bounded resource or work expectations; and
- refusal/degradation behavior when a bound or dependency cannot be satisfied.

The Project writer stores the complete artifact once. Every generated Story has
a named `### Operating-envelope obligations` section containing the Project
envelope digest and only the IDs that Story must satisfy. Delivery, Review, the
merge gate, and readiness can therefore verify inheritance without copying the
full prose. A project cannot move to owner review when an identified risk has no
falsifiable bound, a Story omits its applicable IDs, or a Story contradicts the
envelope.

### 2. Delivery feasibility evidence

Before implementation, Delivery reads the approved envelope and records a
bounded feasibility note for every referenced ID:

- expected algorithmic/work bound;
- how the design avoids work proportional to an unbounded monetary unit or
  provider response;
- the test or measurement that will exercise the representative input; and
- any conflict that requires returning the Story to scope review.

Delivery does not invent a weaker bound. If the envelope cannot be met within
Story scope and spend, it produces a scope-blocked result before editing code.

### 3. Independent Review verifies the same IDs

Review receives the Project envelope and Delivery evidence as inputs. It must
map each relevant ID to an executable failure-producing check. Static inspection
alone cannot satisfy a runtime performance or live-provider claim. A finding
names the unmet ID so the Project contract remains the single source.

This is a contract change, not four copied prompt checklists. The Project
artifact schema, writer, delivery input, review input, and read-back validators
must agree on the same IDs.

### 4. Independent production-readiness evaluation

After all Project Stories merge and before `project:awaiting-acceptance`, a
separate readiness evaluator checks the integrated `main` revision against the
operating envelope. It uses no delivery conversation or review conclusion.

It emits a machine-readable artifact containing:

- repository, Project, and exact tested revision;
- one pass/fail result and evidence pointer per envelope ID;
- timestamps and bounded live-provider observations when applicable; and
- an overall `ready` or `not-ready` result.

`not-ready` leaves the Project out of the acceptance queue and supplies bounded
input to the existing planning lifecycle for a narrow corrective Story. It does
not create or approve that Story by itself, ring a human acceptance bell, or
edit acceptance criteria.

The evaluator starts in warning-only probation. During implementation it must
run against the preserved Project #30 and #47 cases plus failure-injected
fixtures. Its verdicts are compared with the already recorded human outcomes.
It earns blocking authority only after deterministic tests pass and those known
bad cases are rejected without rejecting the known-good controls. Promotion is
a reviewed factory configuration change, not a model decision. The fresh Rung 2
repeat cannot start until promotion; after promotion, transition to
`project:awaiting-acceptance` requires an exact-revision `ready` artifact.

### 5. Doctor becomes an enforced startup gate

Doctor writes a credential-free, short-lived JSON receipt only after all checks
pass. The receipt binds:

- canonical repository;
- roadmap commitment;
- factory revision;
- dispatch-affecting configuration fingerprint;
- enabled primary and fallback adapter probes;
- issue timestamp and expiry; and
- a digest of the check results.

The mutable poller validates the receipt internally before its first GitHub
write. Wrapper-only enforcement is insufficient because direct invocation would
bypass it. A receipt mismatch or failure exits with a stable status and no
lifecycle mutation. Dry-run may diagnose readiness without a receipt because it
is read-only; it must clearly report that live startup remains blocked.

Default receipt lifetime is 15 minutes. The implementation may choose a shorter
value if real preflight measurement supports it, but may not accept an unbounded
or reusable receipt.

### 6. One local poller owns one queue key

At process start, the poller acquires an operating-system advisory lock keyed by
canonical repository and roadmap commitment. The lock is held for the entire
process and released by the operating system on normal exit or crash.

A second same-key process exits with a stable duplicate-poller status before
doctor validation or GitHub mutation. Different commitment keys remain allowed,
subject to global WIP rules. The lock file contains only diagnostic PID, start
time, and key information; it is not lifecycle state.

A distributed multi-host lease is `deferred-with-reason`: current Phase 5
operation is one host, and no evidence justifies new coordination infrastructure.
The existing GitHub Story claim remains the cross-process item mutex.

### 7. Capacity reservation precedes claim accounting

Dispatch becomes a bounded admission transaction:

1. evaluate Story eligibility without mutation;
2. ask the Capacity Pool for a time-limited reservation on an eligible primary
   or fallback route;
3. re-read the Story and atomically claim it using existing state-version rules;
4. start the reserved worker and write a durable worker-start event; and
5. consume the reservation or release it on every pre-start failure.

No reservation means no claim, no Attempt increment, and no recovery event. If
the Story changes before claim, the reservation is released. If the reserved
route becomes unavailable before process start, the claim is returned to ready
and Attempt restored under the existing infrastructure-failure rule. Once the
worker-start event exists, normal attempt, lease, and poison rules apply.

The Capacity Pool remains an in-process admission component. This plan adds no
remote queue or capacity service.

### 8. Invocation outcomes and usage receipts become complete

Every adapter writes one terminal outcome tied to its invocation ID and, when
applicable, its durable worker-start event. The normalized outcomes are:

- `not-admitted` — no eligible route or reservation;
- `launch-failed` — reserved but the model process did not start;
- `started-mid-work-failed` — the model started and failed before validation;
- `limit-stopped` — the running model hit its time, spend, token, or session
  bound;
- `validation-failed` — model work completed but repository validation failed;
  and
- `completed` — the bounded assignment and validation completed.

Each terminal record must include a complete reproducible usage receipt. Exact
vendor dollar cost is required only when the provider exposes it. When exact
cost is unavailable because capacity is subscription-backed, prepaid, or
otherwise unpriced per invocation, the receipt must include normalized
reproducible usage/capacity units plus a named reason that dollar cost is
unavailable. Absence of both reproducible usage and an applicable exact cost is
a measurement-integrity failure. The reporter must never fabricate a dollar
equivalent, and it attributes each retry to one outcome.

## Evaluation plan

Implementation is not complete until deterministic tests prove a failure mode,
not merely the happy path.

| Evaluation | Must fail when | Must pass when |
|---|---|---|
| Project #47 realistic scale | only toy inputs are checked, the work bound is unbounded, the response exceeds the approved budget, or the browser stops responding | representative $1,000,000 input stays responsive and completes within the approved envelope |
| Project #30 live provider | the live response is incompatible, stale fallback is presented as current, or the network claim has only fixture proof | offline parsing is deterministic and the bounded read-only live contract is current |
| Capacity recovery/re-entry | zero capacity changes lifecycle or Attempt, an expired reservation launches, or recovery poisons unstarted work | zero capacity is a no-op and restored capacity produces one claim plus one worker start |
| Real model-adapter contract | authentication, stream shape, permissions, capacity, or terminal result differs from the normalized contract | every enabled primary/fallback route passes a bounded read-only live probe |
| Doctor enforcement | receipt is absent, failed, expired, or mismatched | one matching fresh receipt permits startup |
| Poller singleton | a same-key second poller reaches mutation | one owner runs, duplicates refuse, other keys run, and crash releases the lock |
| Readiness independence | evaluator consumes a delivery/review verdict instead of exact-revision evidence | fresh evaluator checks map to all risk IDs on integrated `main` |
| Worker outcome stages | never-started, mid-work, limit, and validation failures collapse to the same reason | every invocation has one stage-correct terminal outcome tied to start evidence |
| Usage completeness | an invocation lacks reproducible usage, or a priced invocation cannot reconcile its exposed exact cost | all invocation receipts reconcile; normalized usage is present for every route, and exact dollar cost is reported only where the provider exposes it |

The Project #30 and #47 evaluations may use preserved production-shaped
fixtures for deterministic runs, but any claim about a current provider or real
browser must also execute the bounded live check it names.

## Implementation sequence after approval

1. Add the operating-envelope schema, writer/read-back validation, and planning
   tests.
2. Thread envelope IDs into Delivery and Review, with failure-producing
   feasibility and realism tests.
3. Add the independent readiness artifact in warning-only mode, evaluate it on
   the preserved good/bad cases, then separately review promotion to blocking.
4. Add doctor receipts and internal poller validation.
5. Add the local singleton lock.
6. Move capacity reservation before Story claim and add durable worker-start
   evidence.
7. Add normalized terminal outcomes, retry attribution, and complete usage
   receipts.
8. Run the four regression classes plus doctor, singleton, readiness, outcome,
   and usage/cost tests.
9. Run the repository's existing requirement-coverage and full test commands.
10. Run doctor, then execute a fresh independent 2–4 Story Rung 2 project using
    a new user-facing outcome of **comparable difficulty** to the failed Rung 2.
    Do not reuse Project #30, Project #47, or their corrective Stories as the
    qualifying repeat. Apply the same Rung 2 success thresholds.
11. Generate and freeze one Rung 2 report. Proceed to Rung 3 only if autonomy is
    at least 75%, relay is zero, escaped defects are zero, and measurement
    integrity passes. The report must attribute every retry and provide complete
    normalized usage per accepted Story across all routes; it must also provide
    exact dollar cost per accepted Story for the subset of routes where the
    provider exposes exact pricing. Unavailable dollar pricing on a
    subscription/prepaid route is not itself a progression failure when its
    normalized usage receipt is complete and reproducible.

All factory implementation must be performed directly by the Chief Architect
workflow. The factory must not dispatch Stories that modify its own controls.

## Decision requested

Approve or request changes to this bounded pre-Rung-3 design. Approval would
authorize implementation of the listed factory changes only. It would not
approve Project #30 or #47, rescue Story #58, accept a future Rung 2 repeat, or
authorize Rung 3.
