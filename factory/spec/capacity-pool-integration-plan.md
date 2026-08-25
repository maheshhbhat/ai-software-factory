# Capacity Pool factory-wide integration plan

Status: **Chief Architect review required; implementation has not started.**

Enhancement: #498. Planning Story: #505.

## Decision requested

Make Capacity Pool the only production boundary allowed to select or invoke a
model/provider. Agents own their prompt, sandbox, input contract, and output
validation. Capacity Pool owns route selection, provider adapters, effort,
availability, fallback, the combined logical-task envelope, and routing
telemetry.

Product Project #30 must remain inactive until this plan is approved, the
critical production migrations below are implemented, and the controlled
failover exercise passes.

## Audit method and scope

The audit inspected production Python and shell entrypoints for concrete model
CLI names, model-command overrides, engine selectors, subprocess boundaries,
and indirect launch configuration. It then traced every match to the process
that actually runs it. Tests, specifications, and historical evidence were not
treated as production merely because they mention a model. Live verification
harnesses are listed separately because they spend real model calls even though
they are not part of the steady-state poll loop.

The required classifications mean:

- `capacity-pool-routed`: a model-backed capability obtains and executes its
  route only through Capacity Pool.
- `deterministic / no model`: the component performs no model inference and
  must remain outside Capacity Pool.
- `violation: direct model invocation`: the component selects a provider/model,
  constructs a provider CLI command, or launches that command outside the
  approved Capacity Pool adapter boundary.

## Production inventory

| Capability or component | Actual boundary | Current classification | Evidence and finding |
|---|---|---|---|
| Capacity policy | `factory/capacity_pool/router.py` | `deterministic / no model` | Purely ranks declared capacity and computes remaining envelopes. It deliberately does not execute models. |
| Planning | `factory/agents/planning/invoke.py` | `violation: direct model invocation` | It calls the router, but also declares Claude/GPT models, constructs both provider CLI commands, classifies provider failures, divides the envelope, and launches both processes. Routing is only partially centralized. |
| Delivery worker | `factory/agents/worker/invoke.py` | `violation: direct model invocation` | The `--engine` argument and `FACTORY_DELIVERY_MODEL_CMD` select an engine; this module constructs and launches Claude or Codex commands and maintains provider-specific credential rules. |
| Worker selection and launch | `poll.sh`, `factory/dispatcher/dispatcher.py`, `factory/runtime/workers.py`, `factory/runtime/poller.py` | `violation: direct model invocation` | Provider-specific worker IDs and launch commands are ordered outside Capacity Pool. `FACTORY_WORKER_ORDER` and the dispatcher choose Claude/Codex identities before the model workload is evaluated. |
| Independent review | `factory/agents/review/invoke.py` | `violation: direct model invocation` | It constructs and directly launches Claude, has a Claude-specific credential home, and has no independently authenticated fallback. Its exact-head and output-validation boundaries are otherwise suitable adapters. |
| Legacy bounded worker bridge | `factory/runtime/bridge.py`, configured by `live-e2e.sh` | `violation: direct model invocation` | It maps `--engine` directly to Claude/Codex commands. The normal product poll does not use it, but it remains a production-callable live entrypoint. |
| Readiness engine inference probe | `factory/acceptance/e2e_doctor.py` | `violation: direct model invocation` | Authentication/version checks are diagnostics, but `worker_engine_start()` performs a real inference and selects its CLI from `FACTORY_WORKER_ORDER` outside Capacity Pool. |
| Planning route discovery | `factory/runtime/planning_route.py` | `deterministic / no model` | Selects a GitHub planning artifact only; it does not select or invoke a model. |
| Review routing/linking | `factory/runtime/review_route.py`, `factory/runtime/review_link.py` | `deterministic / no model` | Selects PR review work and applies verified lifecycle consequences; inference occurs only in the violating review wrapper above. |
| Dispatch, sequencing, completion, continuation | `factory/dispatcher/**`, `factory/runtime/sequencer.py`, `completion.py`, `continuation.py` | `deterministic / no model` | Except for the provider-specific worker identity named above, these are GitHub state machines and deterministic checks. They must not become model-backed. |
| Human queue, acceptance, sampling, rescue/repair, status | `factory/runtime/humanqueue.py`, `sampling.py`, `repair_claim.py`, `status.py`, approval/acceptance wrappers | `deterministic / no model` | Acceptance is a human bell. Sampling selects evidence. Rescue applies an explicit human decision. No hidden acceptance, supervisor, or rescue model invocation exists. |
| Merge/scope gates | `factory/gates/**` and repository workflows | `deterministic / no model` | Trusted-main code and tests decide scope/test eligibility. Model output cannot replace these gates. |
| Observability and subprocess streaming | `factory/runtime/observability.py`, `runlog.py`, `streaming.py` | `deterministic / no model` | Record and transport bounded events. They may be reused by Capacity Pool but do not choose a route. |
| Coverage and KPI reporting | `factory/coverage_report.py`, `factory/acceptance/rung*_report.py` | `deterministic / no model` | Run tests and derive reports from evidence. |

There are currently **zero fully `capacity-pool-routed` model-backed production
capabilities**. Planning consumes the pure router, but the invariant is not met
until selection and invocation both cross the shared execution boundary.

### Current and intended route inventory

The intended routes below are policy, not coding-time choices. A
`capacity-ranked` route means the checked-in registry may change live
availability and ordering, but it may not lower the stated tier, capability,
effort, provider-diversity, fallback, or envelope rules. The initial flagship
pool is Claude Fable 5 (`claude-fable-5`, Anthropic) and GPT-5.6 Sol
(`gpt-5.6-sol`, OpenAI). Adding another eligible model, including Spark or
Muse, requires a reviewed registry change; experimental capacity remains
opt-in.

| Model-backed path | Current engine/model/provider | Intended primary route | Intended fallback route | Effort | Allowed fallback triggers | Stop / no fallback | Combined envelope and escalation |
|---|---|---|---|---|---|---|---|
| Planning | Claude CLI / `claude-fable-5` / Anthropic, then a locally implemented Codex fallback using `gpt-5.6-sol` / OpenAI | Capacity-ranked flagship with `reason + json`; initially prefer available Claude Fable 5 capacity | One provider-diverse flagship peer; initially GPT-5.6 Sol. An explicit model override is single-route unless fallback is explicitly enabled | medium | missing executable, unavailable, quota/session, rate limit, authentication, timeout | malformed or schema-invalid plan, unsafe output, contract violation, unknown failure | Existing 900 seconds and 5 budget units total. No automatic effort/tier escalation |
| Delivery invocation plus worker selection/launch | `poll.sh` currently selects Codex CLI / CLI-default unpinned model / OpenAI; an operator override can select Claude CLI / CLI-default unpinned model / Anthropic | Capacity-ranked flagship with `code + write + tests`; initially the best available eligible GPT-5.6 Sol or Claude Fable 5 capacity, with prepaid/expiring eligible capacity preferred by the router | One provider-diverse flagship peer, but only after a failure proven to precede repository mutation. Explicit overrides remain single-route unless fallback is enabled | medium | missing executable, unavailable, quota/session, rate limit, authentication before mutation | malformed result, unsafe output, scope violation, failed tests, unknown failure, timeout or any ambiguous state after possible mutation | The Story's `### Spend cap` time and normalized budget total. No automatic effort/tier escalation; write-time timeout fallback is deferred pending isolated attempt worktrees |
| Independent review | Claude CLI / CLI-default unpinned model / Anthropic, using the dedicated reviewer identity | Capacity-ranked flagship with `code + reason + json`; initially prefer available Claude Fable 5 capacity under the dedicated reviewer boundary | One independently authenticated, provider-diverse flagship peer; initially GPT-5.6 Sol, with failed-attempt output discarded before retry | medium | missing executable, unavailable, quota/session, rate limit, authentication, timeout with private failed output discarded | malformed or stale-head verdict, unsafe output, review contract violation, unknown failure | Existing 180 seconds plus one explicit normalized review budget total. No automatic effort/tier escalation |
| Bounded acknowledgement bridge | `live-e2e.sh` declares Claude and Codex CLI routes with CLI-default unpinned models; `FACTORY_WORKER_ORDER` chooses the engine outside Capacity Pool | Capacity-ranked economy model with basic tool use and the required narrow GitHub-comment permission | One provider-diverse economy peer, only before the acknowledgement write. If no eligible economy peer exists, fail closed rather than silently use flagship capacity | low | missing executable, unavailable, quota/session, rate limit, authentication before the write | ambiguous comment write, malformed acknowledgement, scope violation, unknown failure | Existing bridge deadline and one small normalized budget total. No tier or effort escalation |
| Readiness inference and provider auth/health checks | Doctor selects the first `FACTORY_WORKER_ORDER` entry; Claude or Codex CLI uses its CLI-default unpinned model/provider. Separate CLI auth/version checks are provider-specific | No productive primary/fallback chain. Probe each configured route independently through its adapter using an economy exact-answer request | None within a route check; failure names that route unhealthy. The doctor continues checking other configured routes so it reports all capacity, not a substituted success | low | Not applicable: each configured provider is tested independently | wrong/malformed answer, auth failure, unavailable route, timeout; one provider's success never hides another's failure | 90 seconds and one small normalized budget per independently reported route. No escalation |

The Delivery row also governs `poll.sh`, dispatcher identity selection,
`runtime/workers.py`, and poller launch. Those files do not receive a separate
model policy: migration removes their provider choice and leaves them with one
logical `delivery` capability request. The bridge row governs the indirect
`factory/acceptance/e2e.py` route. Direct real-call harnesses inherit the route
for the production capability they exercise or are retired as specified below.

## Real-call harness inventory

These files are not steady-state production, but they can spend real model
calls and therefore cannot remain a loophole in architectural enforcement:

| Harness | Current status | Required disposition |
|---|---|---|
| `factory/acceptance/phase4_live.py` | Direct Claude worker and reviewer calls | Migrate to the shared provider-adapter smoke interface or retire after equivalent Capacity Pool coverage exists. |
| `factory/acceptance/test_engine_live.py` | Direct Claude read-only probe | Replace with provider-adapter/Capacity Pool smoke coverage. |
| `factory/acceptance/e2e.py` via `live-e2e.sh` | Indirect direct invocation through `runtime/bridge.py` | Route the bridge workload through Capacity Pool, then keep the harness as end-to-end evidence. |
| `factory/acceptance/reviewer_real.py`, `factory/acceptance/phase4_real.py`, `factory/acceptance/rung1_live.py`, `factory/acceptance/two_story_real.py` | Invoke production wrappers/poller rather than model CLIs | No independent route is needed; they inherit the migrated production boundary. |

Mock commands inside deterministic tests remain allowed. Authentication and
version diagnostics may call provider CLIs only through the provider adapter's
non-inference probe API, so provider knowledge has one audited home.

## Target boundary

Add three layers under `factory/capacity_pool/`:

1. `policy.py` contains the checked-in model-capacity registry and one workload
   policy per capability. No agent declares model names or provider order.
2. `executor.py` accepts a workload request plus capability-specific callbacks,
   asks the existing pure router for a route, runs at most that route, accounts
   for elapsed time and budget after every attempt, classifies provider
   failures, and emits one final outcome.
3. `providers/` contains the only production CLI adapters. Each adapter owns
   command construction, independent authentication environment, usage parsing,
   non-inference health/auth probes, and mapping provider errors into the shared
   failure vocabulary.

Agents remain responsible for material that must not be generalized:

- Planning supplies its prompt and JSON schema and validates the returned plan.
- Delivery supplies its worktree, Story input, write-capable sandbox, declared
  scope verification, tests, and PR creation.
- Review supplies its fresh exact-head checkout, untrusted-diff isolation,
  output location, schema validation, and verdict application.
- The doctor supplies the harmless exact-answer probe request.

The executor never interprets product plans, edits files, approves work, applies
labels, or weakens a gate.

## Workload policies

| Workload | Minimum capability / tier | Starting effort | Fallback allowed | Quality stop | Overall envelope |
|---|---|---|---|---|---|
| Planning | `reason + json`, flagship | medium | missing executable, unavailable, quota/session, rate limit, authentication, timeout | malformed/schema-invalid plan, unsafe output, contract violation | Existing 900 seconds and 5 budget units across all attempts |
| Delivery | `code + write + tests`, flagship | medium | missing executable, unavailable, quota/session, rate limit, or authentication **before repository mutation** | malformed result, scope violation, unsafe output, failed tests; timeout after possible mutation is ambiguous and stops unless attempt isolation is implemented | Story `### Spend cap` time and budget across all attempts |
| Independent review | `code + reason + json`, flagship | medium | missing executable, unavailable, quota/session, rate limit, authentication, timeout after the failed attempt's private output is discarded | malformed/stale-head verdict, unsafe output, review contract violation | Existing 180 seconds plus a new explicit normalized review budget across all attempts |
| Bounded acknowledgement bridge | basic tool use, economy | low | availability failures before acknowledgement write | ambiguous comment write, malformed acknowledgement, scope violation | Existing bridge timeout and one small normalized budget |
| Readiness inference probe | exact-answer text, economy | low | Probe every configured critical provider route independently; it is evidence, not productive fallback | wrong/malformed answer | One 90-second total probe envelope per route check, with an explicit small budget |

Effort escalation is policy data, never an accidental provider default. A
hazard Story does not silently buy a stronger model; a future escalation policy
requires its own reviewed evidence. Explicit operator/model overrides produce a
single-step route unless the override explicitly opts into fallback.

### Delivery's write-side constraint

A timed-out write-capable process may have changed its worktree. Starting a
second model in that same worktree would mix authorship and make the result
ambiguous. The smallest safe first integration therefore permits delivery
fallback only for failures proven to occur before mutation. Timeout fallback is
fail-closed until a later, separately reviewed design gives every provider
attempt an isolated worktree and promotes only one validated result. The
controlled critical-path exercise uses deliberate primary unavailability, not
a write-side timeout, so it proves provider continuity without weakening this
boundary.

## Failure and resource contract

Provider adapters return a common attempt result containing provider, model,
effort, start/end time, reported usage, mutation status where applicable,
normalized failure reason, and bounded redacted diagnostics.

Only `unavailable`, `quota`, `rate-limit`, `auth`, and policy-safe `timeout`
permit the next route step. `malformed-output`, `schema-invalid`,
`unsafe-output`, `scope-violation`, failed tests, ambiguous mutation, and
unknown non-zero failures stop the logical task.

The logical request owns one deadline and one normalized budget. Before every
attempt the executor derives the remainder from observed elapsed time and
consumption. A missing executable consumes no provider budget. When a provider
does not report cost, the executor charges the entire reserved attempt budget;
it never assumes zero. A provider without a dollar-limit flag receives a
documented deterministic timeout/token surrogate derived from the remaining
budget. No adapter may receive the original full envelope after the first
attempt starts.

Telemetry must record route ID, workload, attempt index, reason, provider,
model, tier, effort, elapsed/remaining time, consumed/remaining normalized
budget, mutation state where relevant, and final outcome. It must not record
credentials, full prompts, or unbounded model output.

## Architectural enforcement

Add a gate-discovered acceptance test backed by a checked-in machine-readable
inventory. It scans production Python ASTs and shell entrypoints and fails when:

- a provider CLI name, provider-specific model name, or provider-specific
  command builder appears outside `factory/capacity_pool/providers/`;
- production code invokes a model command except through
  `capacity_pool.executor`;
- `FACTORY_WORKER_ORDER`, `--engine`, or provider-specific model-command
  overrides select a production provider outside the migration compatibility
  boundary; or
- an executable production file is absent from the inventory.

The first enforcement change carries an exact, reviewed debt allowlist for the
violations in this document so it blocks new bypasses immediately. Each
migration removes its entries. The final integration cannot pass while any
production debt entry remains. Test fixtures may declare mock executable names;
real-call harnesses must use the provider adapter or carry an explicit temporary
debt entry that is removed before Project #30 activation.

## Smallest implementation sequence

1. **Shared execution boundary and enforcement.** Add workload-policy data,
   provider adapters, the executor, common attempt results, envelope accounting,
   telemetry, and the inventory-backed architectural test. Preserve current
   behavior behind exact debt entries; do not migrate an agent opportunistically.
2. **Planning and readiness probes.** Move Planning's existing Capacity Pool
   route and both CLI adapters into the shared boundary. Move real inference and
   auth/health smoke checks in the doctor to adapter APIs. Prove both exact
   planning schemas and each configured provider with harmless real calls.
3. **Delivery path.** Replace provider-specific dispatcher/worker IDs with one
   logical delivery capability. Route `poll.sh`, dispatcher, worker launcher,
   and delivery invocation through the executor. Preserve the Story envelope,
   credential isolation, write sandbox, scope checks, test gate, and explicit
   override semantics. Prove fallback on pre-mutation primary unavailability;
   prove ambiguous mutation and quality failures do not switch providers.
4. **Review, bridge, and live harness closure.** Route exact-head review and the
   bounded bridge through the executor, migrate/retire direct live harness
   calls, remove the final debt entries, and run the controlled end-to-end
   exercise with the primary Claude route deliberately unavailable. Require
   Planning, Delivery, and Review to complete through independently
   authenticated fallback routes; require malformed output to stop; verify the
   combined envelopes and telemetry by read-back.

Each item is a separate bounded direct/external Story and gated PR. The
autonomous factory never claims factory control-plane work.

### Proposed Story boundaries

| Story | Declared implementation area | Independent failure proof |
|---|---|---|
| Shared boundary | `factory/capacity_pool/{policy,executor,inventory}.py`, `factory/capacity_pool/providers/**`, Capacity Pool README/ADR, and gate-discovered Capacity Pool architecture/executor tests | Unknown executable boundary, duplicate model identity, unsupported effort, ineligible route, budget exhaustion, forbidden fallback, and a new direct-invocation fixture all fail closed. |
| Planning and probes | Planning invoke/tests, doctor/tests, and only the workload/provider policy entries they consume | Real campaign/project schemas pass through both provider adapters; quota and timeout use only the remainder; malformed output stops; a provider-auth/probe failure is named. |
| Delivery | `poll.sh`, dispatcher worker identity, runtime worker/poller launch, delivery invoke/tests, and delivery Capacity Pool acceptance scenarios | Deliberate pre-mutation primary unavailability reaches one fallback; scope/test/unsafe failures and ambiguous mutation launch no second writer; Story time and budget never reset. |
| Review and closure | Review invoke/tests, bridge/tests, direct real-call harness migrations, enforcement debt removal, and the controlled end-to-end failover scenario | Exact-head fallback succeeds with private failed output discarded; malformed/stale verdict stops; the full critical path completes with primary Claude unavailable and the inventory reports zero production violations. |

No Story may combine a product change with these factory control-plane paths.
The shared-boundary Story may introduce the temporary exact debt manifest, but
the final Story must remove every production debt entry rather than relabel it
as an exception.

## Evidence required before Project #30 activation

- The architectural enforcement inventory contains no production violation.
- Deterministic fallback and stop-condition tests pass for Planning, Delivery,
  Review, bridge, and doctor workloads.
- Explicit overrides are single-provider unless fallback opt-in is present.
- Combined time/budget tests prove no fallback reset.
- Harmless real read-only/authentication smoke checks pass for every configured
  provider adapter.
- The controlled end-to-end run completes Planning, Delivery, and Review with
  primary Claude capacity deliberately unavailable, zero human relay for model
  switching, and no weakened output, scope, security, or merge gate.
- Project #30 is still `project:awaiting-ready` until all preceding evidence is
  reviewed and accepted.

## Deliberately deferred

- Automatic delivery fallback after a write-capable timeout or ambiguous
  process outcome, until isolated-attempt promotion is designed.
- ML quality prediction, automatic effort escalation, background quota
  scraping, persistent scheduling services, or an event bus.
- Product Project #30 implementation and any widening of its scope.

This document is the required planning stop. Approval authorizes the four
bounded integration Stories above; it does not itself authorize implementation
of any additional architecture.
