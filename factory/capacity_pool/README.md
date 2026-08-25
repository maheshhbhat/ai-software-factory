# AI Capacity Pool v1

Capacity Pool is factory control-plane infrastructure. Factory agents request a capability; they do not hard-code a provider or model. The pool returns a deterministic execution plan containing a primary model, bounded fallback chain, effort, and one logical-task resource envelope.

## Boundary

This component is developed directly/external to the autonomous factory. The factory may consume it later, but it must not claim or modify Capacity Pool itself.

## v1 inputs

- task type
- required capabilities
- minimum capability tier
- effort
- total timeout
- total budget units
- provider/model overrides
- prior model attempts
- experimental-model opt-in
- providers temporarily avoided by health/capacity policy

## v1 model-capacity registry

Each capacity source declares:

- model and provider
- capability tier
- capabilities (for example `code`, `write`, `tests`, `reason`, `json`)
- current availability and normalized capacity remaining
- whether capacity is prepaid/expiring
- whether the model is experimental
- supported effort levels
- latency rank
- recent success signal

Provider/model names are data. The router does not contain product-specific rules for Claude, Codex, Muse, or any future engine.

## Deterministic routing policy

1. Reject unavailable, exhausted, incapable, below-tier, disallowed, or unsupported-effort capacity.
2. Use the lowest sufficient tier.
3. Prefer prepaid/expiring capacity when capability is sufficient (for example underused Spark capacity).
4. Prefer remaining capacity, recent success, and lower latency.
5. Deprioritize a model already used for the same logical task.
6. Prefer provider diversity in the fallback chain when another eligible provider exists,
   then fill remaining bounded route slots with the next eligible models.
7. Explicit model overrides never silently gain a fallback unless the caller opts in.
8. Experimental models such as Muse are opt-in until promoted by evidence.

## Failure semantics

Fallback may be attempted for availability failures such as provider unavailable, quota/session exhaustion, rate limiting, timeout, or authentication failure.

Malformed output, schema-invalid output, unsafe output, and scope violations are stop/quality conditions, not automatic provider-switch triggers.

## One resource envelope

The total timeout/budget belongs to the logical task, not each provider call. `remaining_envelope()` returns only the unconsumed remainder; a fallback never receives a fresh full budget.

## Deferred from v1

The standalone router intentionally does **not** execute models, scrape provider quota pages, predict quality with ML, mutate factory agent code, or persist telemetry. Adapters, live usage collection, execution leases/duplicate suppression, partial-work resume, and Planning/Worker/Review integration come after the deterministic policy passes its scenario suite.

## Run tests

From the repository root:

```sh
python3 -m unittest discover -s factory/acceptance -p 'test_capacity_pool.py'
```
