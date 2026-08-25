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

## Checked-in model-capacity registry

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

Provider/model names are data. Unverified installed model identifiers remain disabled until a bounded adapter probe verifies them. The initial policy includes disabled Codex Spark capacity, balanced Terra, economy Luna, flagship Sol, and independently authenticated Anthropic capacity classes.

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

## Shared execution and lifecycle boundary

`executor.py` is the sole intended model execution boundary. `providers/` is the only package allowed to construct provider CLI commands. `state.py` stores health, cooldown/probe recovery, reservations, and leases transactionally in SQLite. Unreported usage consumes the reserved budget; provider failure cannot raise tier or effort; malformed or unsafe output stops rather than switching providers.

`inventory.json` records the current direct-invocation debt. The architecture acceptance test rejects a new production bypass immediately. Planning, Delivery, Review, bridge, doctor, and live-harness debt is removed by their separately gated migration Stories.

Partial write-capable work/resume, live quota scraping, ML quality prediction, and automatic effort escalation remain deferred. No agent has been migrated by the shared-boundary increment alone.

## Run tests

From the repository root:

```sh
python3 -m unittest discover -s factory/acceptance -p 'test_capacity*.py'
```
