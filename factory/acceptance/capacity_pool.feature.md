# AI Capacity Pool v1 — executable acceptance pack

Issue: #498

This pack defines **externally observable routing behavior before agent integration**. It is intentionally implementation-agnostic: it describes what the Capacity Pool must decide, not how its router is internally structured.

The executable counterpart is `factory/acceptance/test_capacity_pool_acceptance.py` and must be run by the repository's required acceptance/merge gate.

## Scenario 1 — use underused prepaid coding capacity

**Given** a bounded coding task that requires code/write/test capability,
**and** Spark has eligible prepaid/expiring capacity,
**and** Terra and Sonnet are also eligible,
**when** the Capacity Pool routes the task,
**then** Spark is selected as primary capacity,
**and** a provider-diverse fallback is available.

## Scenario 2 — exhausted capacity is not selected

**Given** Spark has no remaining capacity,
**when** a bounded coding task is routed,
**then** Spark is not selected,
**and** an eligible same-capability alternative is chosen.

## Scenario 3 — specialization matters

**Given** Spark is registered only for coding capability,
**when** a planning task requiring reasoning/JSON capability is routed,
**then** Spark is not selected merely because it has spare capacity.

## Scenario 4 — high-risk work requires the minimum approved capability tier

**Given** an architecture/high-risk request requires flagship capability,
**when** balanced and flagship models are available,
**then** a balanced model is not selected,
**and** an eligible flagship model is selected.

## Scenario 5 — provider outage/quota/auth/timeout permits fallback

**Given** a route plan with primary and fallback capacity,
**when** the primary is unavailable, quota-exhausted, rate-limited, times out, or cannot authenticate,
**then** policy permits advancing to the next route step.

## Scenario 6 — bad output fails closed rather than shopping for another answer

**Given** a model invocation returns malformed, schema-invalid, unsafe, or scope-violating output,
**when** the outcome is classified,
**then** the Capacity Pool marks it as a stop condition,
**and** it is not treated as an availability fallback trigger.

## Scenario 7 — one logical task has one resource envelope

**Given** a logical task has a total timeout and budget,
**and** its primary consumes part of both,
**when** fallback capacity is used,
**then** fallback receives only the unconsumed remainder,
**and** neither timeout nor budget is reset.

## Scenario 8 — operator override is authoritative

**Given** the operator explicitly selects an eligible model,
**and** does not authorize fallback for the override,
**when** the task is routed,
**then** only that model appears in the route plan.

## Scenario 9 — experimental capacity is opt-in

**Given** Muse or another experimental model is registered and otherwise attractive,
**when** a task does not opt into experimental capacity,
**then** the experimental model is ineligible,
**and when** the task explicitly opts in,
**then** it may be selected according to normal ranking policy.

## Scenario 10 — provider concentration is reduced in fallback

**Given** the primary and multiple fallback candidates include more than one provider,
**when** the fallback chain is constructed,
**then** the next fallback prefers a different provider when an eligible alternative exists.

## Scenario 11 — prior-attempt replay is avoided

**Given** a model already attempted the same logical task,
**and** another eligible model is available,
**when** the task is routed again,
**then** the previously attempted model is deprioritized.

## Scenario 12 — no eligible model means stop

**Given** no registered capacity satisfies the task's capability/tier/effort constraints,
**when** routing is requested,
**then** the Capacity Pool returns a clear no-eligible-capacity failure,
**and** does not choose an unsafe substitute.

## Scenario 13 — routing is deterministic for a fixed snapshot

**Given** the same request and identical model-capacity snapshot,
**when** routing is evaluated repeatedly,
**then** the selected route plan is identical.

## Experiment boundary

Planner/acceptance owns these functional scenarios. Implementation workers may add unit/component tests for internal data structures, scoring helpers, adapters, and edge cases, but those tests do not replace this acceptance pack.
