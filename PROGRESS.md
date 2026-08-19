# PROGRESS.md — Software Factory Build Tracker

> Lightweight human-readable progress summary derived from `factory/spec/implementation-plan-v1.md` (build spec) and `factory/spec/architecture-v2.1.md` (architecture reference). **Not authoritative.** GitHub issues, labels, PRs, and branch protection are the system of record.

## Current Position

- **Current phase:** Phase 1 — State schema and touch log
- **Current status:** NOT STARTED
- **Next action:** Begin Phase 1 deliverables — author `spec/state-schema.md` (issue types, label state machines, transition table), issue templates for roadmap commitment / project / story, and `touchlog/` append-only log + script.
- **Blocker:** None
- **Last verified milestone:** None — no phase has been verified yet.

## Phase Tracker

States are limited to `NOT STARTED` | `IN PROGRESS` | `VERIFYING` | `VERIFIED`.

| Phase | State | Verification / Proof Required |
|---|---|---|
| **Phase 1 — State schema and touch log** (no AI, no automation) | NOT STARTED | Create one project + three story issues by hand from the templates; manually walk every legal Project transition (`queued → ready-for-planning → planning → awaiting-ready → active → awaiting-acceptance → accepted`) and Story transition (`blocked → ready → claimed → in-review → merged`, plus `blocked:poison` / `blocked:scope`); log touches. Pass = transitions and touch log match `spec/state-schema.md`. |
| **Phase 2 — Deterministic rails** (no AI) | NOT STARTED | With a fake worker script (opens trivial PR on invoke, no AI): prove dispatch is idempotent per artifact + state version (once and only once per transition); merge gate blocks without exact-head review-approval label; hazard-path edit blocks without human ack label from allowed identity; 3 failed attempts → `blocked:poison` + human notification; tests-green, scope, and `test-change` label checks enforce. |
| **Phase 3 — Planning agent** | NOT STARTED | Run planning agent against a real module of the real product. Single gate: **is the plan digest one you would sign?** Must include ADR, stories with `phase:` labels + explicit `depends-on:` + hazard pre-flags, falsifiable acceptance criteria, expected-bells count, and human-readable digest (campaign vs. project altitude selected by trigger type). Iterate prompt until yes. Do not start Phase 4 on an unsigned plan. |
| **Phase 4 — Workers and review** | NOT STARTED | One toy story `add a /health endpoint returning build SHA` through the full loop with zero manual steps between READY sign-off and merge. Worker reads spec+ADRs+findings, branches, PRs, exits with spend cap; review fires on PR-open in fresh context (diff+spec+ADRs only), posts findings→`ready` or approval label bound to head SHA; sampling hook applies 1-in-N lottery (start N=3); merge gate auto-merges. |
| **Phase 5 — Test ladder and KPIs** (overall) | NOT STARTED | Three rungs executed through the identical loop; per-rung KPI report written to `runs/` with Touches (classified), Autonomy, Retry rate, Poison rate, Escaped defects, Acceptance catches, Cost/story, Cycle time (READY→acceptance). Each rung re-verifies affected phases after prompt/plan edits. |
| **Phase 5 · Rung 1 — Toy story** (proves plumbing) | NOT STARTED | Re-run the `/health` toy story. Pass: full loop, zero manual glue, touch log correct, `runs/` report written. |
| **Phase 5 · Rung 2 — Small real feature** (proves gates) | NOT STARTED | Genuine 2–4 story feature on the real product. Pass: autonomy ≥ 75%, `relay = 0`, no escaped defect, `runs/` report written. |
| **Phase 5 · Rung 3 — Notifications extraction** (proves thesis) | NOT STARTED | 10-story strangler plan including shadow mode and cutover bells. Pass: `relay = 0`, `rescues ≤ 30%` of stories, no comparator-visible defect reaching cutover, cost/story within ceiling; `runs/` report written. Kill criteria (any of `relay > 0`, rescues > 30%, defect at cutover, cost ceiling exceeded) → stop and RETHINK before further use. |

## Decisions / Learnings

Captures only decisions already established by `architecture-v2.1.md` and `implementation-plan-v1.md` — no build learnings yet.

- **Substrate:** All authoritative state lives in GitHub (issues/labels = state, webhooks = change feed, identities = access, branch protection + required checks = unforgeable gates, git log + issue timeline = history). Any cache is derivable and disposable, never authoritative.
- **No persistent AI:** Every AI component is invoked on a state transition, reads from GitHub, writes new state, and exits. No heartbeats, liveness, or daemon supervisors. Start with a 60s cron poll + static routing table; Supervisor/webhook runtime deferred until latency data demands it.
- **Communication:** `write → transition → route → invoke → read → write`. Dispatcher is a pure `transition → role` lookup (~10 lines of config), passes only artifact identity. Components are mutually invisible; recovery is re-invocation from the same transition. Poller is the backstop for lost webhooks.
- **Judgment checked:** Every AI-judgment box has an independent check it cannot influence — workers by review, review by PR-open trigger + human sampling, merges by deterministic CI the worker identity cannot affect, planning by outcome acceptance. Route on transitions, not states; record claims as state.
- **Review & merge:** Review triggers only on PR-open in fresh context (diff + story spec + ADRs, never worker reasoning). Findings return story to `ready` with attempt counter increment. Merge gate is required CI checking: exact-head approval, tests green, diff within declared story scope, test deletion/weakening surfaced as distinct `test-change` class, hazard paths. Worker identities cannot merge/skip/re-run it.
- **Hazard paths:** Enumerated — secrets/credentials, dependency manifests, CI/workflow files, branch protection, IAM, data migrations, destructive operations, and `factory/spec/**` + `factory/gates/**` (factory cannot modify its own rules). Enforced via CODEOWNERS with agent identities excluded; one human ack per hazard diff.
- **Idempotency & poison:** Keyed on artifact + state version (at-least-once feed); duplicate deliveries no-op. Workers never self-claim — dispatcher assigns (no label-CAS race). Attempt counter with poison threshold at 3 → `blocked:poison` → human queue with full context. Infrastructure failures do not increment. WIP limits, spend caps, and finite dependency graphs guarantee termination.
- **Human gates (bells only):** Humans appear only at `plan approval (READY)`, `hazard ack`, `poison rescue`, `cutover approval`, `outcome acceptance`, `sampling audit`. Zero stories human-reviewed in the happy path. Every bell logged to touch log as `decision | audit | rescue | relay` with time spent — only `relay` should trend to zero. Outcome acceptance is once per project (falsifiable criteria approved at READY, human tests pass/fail per criterion); failure creates a story or re-planning. Keep projects 5–10 stories; milestone-acceptance checkpoints are the only legitimate Sprint residue.
- **Roles collapsed/deleted:** Planning agent merges Product Manager + Chief Architect (split only on evidence of ≥3 concurrent projects with genuine coupling); Sequencer (deterministic code) replaces Delivery Manager's mechanical half — dependencies must be explicitly declared at authoring; Sprints, Epic layer, Governance-as-skill, Supervisor, heartbeats deleted. Architecture judgment lives as ADR skill + human review for consequential decisions.
- **Deferred until Phase 5 evidence:** Supervisor/webhook runtime, skills library, progress dashboards beyond the digest, multi-project concurrency, epic layer, event bus.
- **Human-authored scope:** Only `product.md` and roadmap commitment issues are human-authored; everything else is generated via the factory.
- **Measurement:** Instrument v1 baseline before migrating — touches classified, autonomous merge rate, rework/reopen rate, escaped defects, acceptance-catch rate, cost/wall-clock per accepted story, poison rate, stuck-work MTTR, sampling findings rate.

---
*Last updated: 2026-08-19 · Position: Phase 1 / NOT STARTED · Source specs: `factory/spec/architecture-v2.1.md`, `factory/spec/implementation-plan-v1.md`*
