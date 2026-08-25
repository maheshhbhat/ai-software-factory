# ADR — AI Capacity Pool v1

Status: proposed
Issue: #498

## Context

Factory model selection is currently embedded in agent wrappers. Planning, Worker, and Review are therefore coupled to provider/model choices, and isolated fallback fixes risk duplicating policy while resetting resource bounds. The Phase 5 run also showed that prepaid/expiring capacity such as Codex Spark can go unused while another provider is quota-blocked.

## Decision

Introduce a standalone deterministic **AI Capacity Pool** control-plane component before integrating fallback into individual agents.

Agents request capabilities and bounds. Capacity Pool returns a routing plan. Provider/model selection, effort, fallback ordering, capacity preference, and logical-task resource bounds belong to the pool rather than to Planning, Worker, or Review.

Capacity Pool is developed directly/external to the autonomous factory. The autonomous factory may consume the component but may not claim or modify it.

## v1 contract

Input:

- task type
- required capabilities
- minimum capability tier
- effort
- total logical-task timeout
- total logical-task budget units
- live registry snapshot (availability, remaining capacity, prepaid/expiring status, experimental status, latency, recent success)
- prior attempts
- optional provider/model override

Output:

- ordered primary/fallback model steps
- selected effort
- total logical-task resource envelope
- allowed fallback conditions
- stop/quality conditions
- routing rationale

## Routing principles

1. Lowest sufficient capability tier wins.
2. Prepaid/expiring capacity is preferred when it can safely perform the task.
3. Capability mismatch, exhausted capacity, unsupported effort, and unavailable providers are ineligible.
4. Provider diversity is preferred in fallback chains.
5. Prior-attempt models are deprioritized to avoid pointless replay.
6. Explicit overrides do not silently gain fallback.
7. Experimental providers/models are opt-in until promoted by evidence.
8. Fallback is for availability/resource failures; malformed/schema-invalid/unsafe/scope-violating output fails closed instead of silently switching provider.
9. The resource envelope belongs to the logical task. Fallback receives only remaining time/budget; it never resets the full allowance.

## Model names are registry data

The router is provider/model agnostic. Spark, Terra, Sol, Luna, Claude-family models, Muse, and future models are registered as capacity sources with declared capabilities and policy metadata. Adding a model should not require changing agent code.

## v1 non-goals

- execute model CLIs
- scrape provider quota portals
- ML/predictive routing
- automatic quality scoring
- persistent usage database
- agent integration
- partial-work resume/lease persistence

These follow after the deterministic router and scenario suite are accepted.

## Next integration sequence

1. Planning consumes Capacity Pool and PR #497 is refactored away from hardcoded Claude→Codex fallback.
2. Worker consumes Capacity Pool, with Spark preferred for suitable bounded coding while capacity is underused.
3. Review consumes Capacity Pool.
4. Run a controlled end-to-end failover exercise with the primary provider deliberately unavailable.

## Consequences

Positive: one routing policy, provider independence, capacity-aware utilization, bounded failover, easier addition of Muse/future models, and consistent observability semantics.

Tradeoff: Capacity Pool becomes control-plane infrastructure whose correctness affects all model-backed agents. It therefore needs independent deterministic testing and must fail closed when no eligible capacity exists.
