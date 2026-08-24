# Phase 5 plan — prevent factory self-dispatch

## Decision requested

Approve a fail-closed rule: the factory may deliver product work and disposable
product-like UAT work, but it may never dispatch, execute, or merge an automated
worker change to the factory control plane. Codex implements factory changes
directly.

This is a plan-only document. It changes no runtime behavior.

## Evidence and problem

Run `20260824T154808Z` for Project #428 — Phase 5 Rung 1 fresh `/health`
KPI UAT used the normal `poll.sh` path under shared test Roadmap Commitment
#384. Recovery found old Story #425 — Add a `/health` endpoint returning the
build SHA from Project #422 — Phase 5 Rung 1 real `/health` KPI UAT. The poller
then claimed both old Story #425 and intended Story #429 — Add a `/health`
endpoint returning the build SHA.

The runtime delivered old Story #425 through PR #430 — Story #425 bounded
delivery and delivered intended Story #429 through PR #431 — Story #429 bounded
delivery. The harness was stopped and froze a failing bundle. This proves two
separate gaps:

- the dispatcher has no explicit invariant preventing factory implementation
  Stories from reaching automated workers; and
- one shared test commitment is not an isolated UAT queue.

The second gap does not excuse the first. A clean queue must still refuse
factory self-modification.

## Protected factory surface

The automated production path must treat any Story scope intersecting these
paths as factory implementation:

- `factory/**`
- `poll.sh`
- `.github/**`
- `AGENTS.md`
- `CLAUDE.md`
- `.claude/**`

A mixed scope containing product and protected paths is protected in full. A
Story cannot make self-modification acceptable by adding one product path.

The list is deliberately narrow and repository-specific. It protects the
factory runtime, prompts, rules and CI controls without pretending every root
file is factory implementation.

## Enforcement

### Dispatcher — prevent the claim

After parsing the mandatory Story scope and before selection or claim, the
dispatcher checks it against the protected surface. Any intersection produces
the named decision `FACTORY_SELF_MODIFICATION_FORBIDDEN`. The Story remains in
its current lifecycle state, no attempt is consumed, and no worker is launched.
The dry-run report names the rejected paths.

### Delivery worker — prevent engine invocation

The delivery worker repeats the same check before constructing or launching an
engine command. This is a backstop for dispatcher defects or direct worker
invocation. Refusal is a definite no-engine-start outcome with structured
diagnostics; it is not evidence that Codex failed.

### Merge gate — prevent automated delivery

The merge gate identifies automated delivery by the existing canonical
`worker-artifact` marker. If such a PR changes a protected path, the gate fails
with the same named reason. A direct implementation PR without that worker
marker remains eligible for normal scope, test and review checks. This keeps
Codex-direct factory work possible while making worker self-modification fail
closed at the final boundary.

All three enforcement points use one shared protected-path classifier. Three
independent copies would drift.

## Backlog treatment

Open work is reported in plain language as one of:

- factory implementation — never dispatch;
- non-dispatchable scope record — no lifecycle label and never dispatch;
- product or disposable product-like UAT delivery — may dispatch when normally
  authorized; or
- blocked historical work — visible, but not dispatchable until its normal
  dependencies and the self-dispatch rule permit it.

The implementation does not rewrite historical lifecycle labels to create a
green test. Existing artifacts remain evidence of what happened.

## UAT isolation remains separate

The next `/health` UAT receives a fresh test-only roadmap commitment containing
only its target Project and, after launch, its one fixture Story. Readiness
fails before artifact creation if that commitment contains any other Project,
Story, recoverable claim or dispatchable work.

The harness also watches durable `dispatch.received` events. A dispatch naming
another Project or Story records `FAIL` and stops the poller. This guard detects
contamination; the fresh commitment prevents it.

No project filter, fake dispatcher, alternate entrypoint or test-only worker is
introduced. Delivery still enters through normal `poll.sh` with real
integrations.

## Deterministic verification

Tests must prove:

- a ready Story scoped only to a protected path is rejected before claim;
- a mixed product/protected scope is rejected in full;
- a normal product scope remains eligible;
- the disposable `runs/rung1/live_product/**` scope remains eligible;
- direct worker invocation refuses protected scope before engine launch;
- an automated worker PR touching a protected path fails the merge gate;
- a direct Codex implementation PR touching a protected path is not rejected
  merely for being direct;
- the named reason and protected paths are observable;
- replay does not consume an attempt or add a duplicate transition; and
- a foreign dispatch causes the Rung 1 harness to freeze a failed report with
  all eight KPI names and honest unavailable values.

Run the repository requirement-coverage script first. Then run the classified
coverage script twice with its deterministic check. Coverage remains reported,
never threshold-gated.

## Black-box proof and completion

After deterministic verification, create a fresh test-only roadmap commitment
and fresh Rung 1 Project, obtain normal owner plan approval, and run through the
host `poll.sh` path with real GitHub, Codex delivery, independent review, merge
checks, sequencing and runtime merge.

Rung 1 passes only if exactly its one disposable product Story is dispatched,
the endpoint returns the exact merged SHA, the evidence bundle and all eight KPI
results are trustworthy, relay is zero, replay is inert, and no manual glue is
used. A real dependency failure is INCONCLUSIVE. An executed run that misses
the expected outcome is FAIL.

Record every finding and local disposition in
`factory/spec/phase5-issue-log.md`. Keep implementation fixes local during the
rung. After Rung 1 is complete, check the complete reviewed batch into `main`
once and stop before Rung 2.
