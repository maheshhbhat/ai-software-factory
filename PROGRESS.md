# PROGRESS.md — Software Factory Build Tracker

> Lightweight human-readable progress summary derived from `factory/spec/implementation-plan-v1.md` (build spec) and `factory/spec/architecture-v2.1.md` (architecture reference). **Not authoritative.** GitHub issues, labels, PRs, and branch protection are the system of record.

## Current Position

- **Current phase:** Phase 2 — Deterministic rails
- **Current status:** **BUILT — in acceptance.** The rails run themselves: a `story:ready` story is claimed, launched, verified and closed with no human touching a label. Project #109 (Phase 2 Closeout) is `project:active`; five predecessor projects sit at `project:awaiting-acceptance` with per-criterion evidence recorded.
- **Next action:** the outstanding §5.3 acceptance bells on #55, #61, #66, #72 and #95, then Phase 2 acceptance itself.
- **Blocker:** none mechanical. Acceptance is a human bell by design (§5.3) and is the only check in the architecture pointed at the factory's own work.
- **Last verified milestone:** **Phase 1 — verified 2026-08-19.**

### What runs today, with evidence

| Component | Evidence |
|---|---|
| Dispatcher — authorization, WIP, deterministic order, atomic claim | 120 tests; live on every story since #56 |
| §9.15 replay of Phase 1's recorded history | 37 transitions, 19 routed live, 0 unknown, 0 duplicates |
| Merge gate, required, trusted-`main` verdict | 46 tests; green on every PR; `bypass_actors: []` |
| Runtime poller — five passes per cycle | 263 tests |
| Worker contract, health, capability, failover | live Claude→Codex failover proven under #85 and again in E2E |
| Launch bridge + proof-of-action | #106, #128 and every E2E fixture |
| Completion (§9.16) | #103, #106, and 15 E2E fixtures |
| PR/merge reconciliation (§9.11) | #97, #107, #110–#114, #122, #124, #126, #130 |
| Human queue (§9.11 no silent drops) | 6 waiting artifacts enumerated every poll |
| Acceptance suite | 16 scenarios, 16/16 |
| End-to-end suite | 12/14 reachable requirements, real engine |

### Known limitations, recorded rather than implied

- **No independent code review.** `required_approving_review_count` is 0 and no review agent exists (Phase 4). The gate checks form — scope, links, tests green — and nothing checks correctness but the tests, which share their code's authorship.
- **Three gate checks withdrawn** (§9.17): test-deletion gating, hazard-path gating, CODEOWNERS. All rest on an artifact the agent's credential can write. A second identity restores them.
- **The Phase 2 worker is an acknowledgement bridge.** `bridge.task_prompt` gives every story the same bounded assignment, because Phase 2 has no delivery worker — that is Phase 4. A story needing a pull request, if dispatched through the bridge today, is completed by §9.16 on an assignment nobody asked for. Every real delivery here was made by a directly-invoked worker.
- **GitHub's issue listing is eventually consistent** — measured at ~3s in both directions. Harmless: every writer re-reads its subject by number first, and reads by number are strongly consistent, so a stale listing costs latency and never a duplicate write. The one direction it biases, WIP accounting, over-counts and so under-dispatches.
- **Two E2E requirements are unreachable or deferred**: an untrusted-author dispatch needs a second identity (#26/#27); claim expiry needs sixty minutes of wall clock.

## Phase Tracker

States are limited to `NOT STARTED` | `IN PROGRESS` | `VERIFYING` | `VERIFIED`.

| Phase | State | Verification / Proof Required |
|---|---|---|
| **Phase 1 — State schema and touch log** (no AI, no automation) | **VERIFIED** (2026-08-19) | Create one project + three story issues by hand from the templates; manually walk every legal Project transition (`queued → ready-for-planning → planning → awaiting-ready → active → awaiting-acceptance → accepted`) and Story transition (`blocked → ready → claimed → in-review → merged`, plus `blocked:poison` / `blocked:scope`); log touches. Pass = transitions and touch log match `spec/state-schema.md`. |
| **Phase 2 — Deterministic rails** (no AI) | **BUILT — in acceptance** | With a fake worker script (opens trivial PR on invoke, no AI): prove dispatch is idempotent per artifact + state version (once and only once per transition); merge gate blocks without exact-head review-approval label; hazard-path edit blocks without human ack label from allowed identity; 3 failed attempts → `blocked:poison` + human notification; tests-green, scope, and `test-change` label checks enforce. |
| **Phase 3 — Planning agent** | NOT STARTED | Run planning agent against a real module of the real product. Single gate: **is the plan digest one you would sign?** Must include ADR, stories with `phase:` labels + explicit `depends-on:` + hazard pre-flags, falsifiable acceptance criteria, expected-bells count, and human-readable digest (campaign vs. project altitude selected by trigger type). Iterate prompt until yes. Do not start Phase 4 on an unsigned plan. |
| **Phase 4 — Workers and review** | NOT STARTED | One toy story `add a /health endpoint returning build SHA` through the full loop with zero manual steps between READY sign-off and merge. Worker reads spec+ADRs+findings, branches, PRs, exits with spend cap; review fires on PR-open in fresh context (diff+spec+ADRs only), posts findings→`ready` or approval label bound to head SHA; sampling hook applies 1-in-N lottery (start N=3); merge gate auto-merges. |
| **Phase 5 — Test ladder and KPIs** (overall) | NOT STARTED | Three rungs executed through the identical loop; per-rung KPI report written to `runs/` with Touches (classified), Autonomy, Retry rate, Poison rate, Escaped defects, Acceptance catches, Cost/story, Cycle time (READY→acceptance). Each rung re-verifies affected phases after prompt/plan edits. |
| **Phase 5 · Rung 1 — Toy story** (proves plumbing) | NOT STARTED | Re-run the `/health` toy story. Pass: full loop, zero manual glue, touch log correct, `runs/` report written. |
| **Phase 5 · Rung 2 — Small real feature** (proves gates) | NOT STARTED | Genuine 2–4 story feature on the real product. Pass: autonomy ≥ 75%, `relay = 0`, no escaped defect, `runs/` report written. |
| **Phase 5 · Rung 3 — Notifications extraction** (proves thesis) | NOT STARTED | 10-story strangler plan including shadow mode and cutover bells. Pass: `relay = 0`, `rescues ≤ 30%` of stories, no comparator-visible defect reaching cutover, cost/story within ceiling; `runs/` report written. Kill criteria (any of `relay > 0`, rescues > 30%, defect at cutover, cost ceiling exceeded) → stop and RETHINK before further use. |

## Decisions / Learnings

Decisions established by `architecture-v2.1.md` and `implementation-plan-v1.md`, plus build learnings from the Phase 1 repair (review issue #6, task #7).

**Phase 1 repair (2026-08-19):**

- **Attempt/poison is now single-sourced** in `state-schema.md` §4.3, resolving the schema-vs-plan contradiction: `Attempt` counts *dispatched worker attempts* and increments on `ready → claimed`; review findings return the story to `ready` with **no** increment; at dispatch time `Attempt >= 3` routes to `story:blocked:poison` instead of dispatching. Rescue requires a rescue comment + `Attempt` reset to `0` + a `poison-rescue` touch, and rescues are capped at 2 per story (counted from the issue timeline) — bounding a story at 9 dispatched attempts.
- **Decision evidence lives in GitHub comments** (`state-schema.md` §5), not in the touch log: plan approval quotes the approved criteria verbatim (an approval is void once that section is edited), acceptance records pass/fail per criterion, and the touch log stays measurement-only. Every bell produces both a comment and exactly one touch-log line.
- **The issue-form rendering is the body contract** (`state-schema.md` §3): forms write `### <label>` headings, bare dropdown values, `- [X]`-style checkboxes, and `_No response_` for empty optional fields. `Depends-on` is one bare `#N` per line or the single token `none`; `Phase` is the bare value mirrored as `phase:<value>`. Issues hand-written before this contract do not conform.
- **Phase 1 verified (task #19):** the synthetic walk exercised every legal Story transition — happy path (#10), retry → poison → human rescue → merge (#11), and the `blocked:scope` detour resolved without widening scope (#12) — plus the full Project lifecycle to `project:accepted`. Two things were accepted with their limits stated rather than papered over: acceptance criterion 2's "created through the GitHub issue form" half is unfalsifiable from GitHub data (the fixtures were API-created with byte-identical bodies, flagged in task #9), and the superseded pre-repair `plan-approval` touch stays in the append-only log as historical evidence, excluded from the four bells of the corrected walk per the CTO's recorded interpretation.
- **Fixtures rebuilt to the contract (task #9):** stories #2/#3/#4 were superseded and closed as not planned — their bodies predate §3.3 — and replaced by #10 (no deps), #11 (depends on #10, hazard), #12 (depends on #11), each verified against the rendered-form contract. The hazard fixture now flags a dependency manifest inside the synthetic sandbox rather than `factory/spec/**`, so exercising the flag never writes into the factory's own governance paths.
- **Both project self-loops were replaced by real edges** (`awaiting-ready → planning`, `awaiting-acceptance → active`) because a label edit ending on the same label emits no routable transition; a `project:active → project:awaiting-ready` correction edge exists for criteria amended after approval.

**From the source specs:**

- **Substrate:** All authoritative state lives in GitHub (issues/labels = state, webhooks = change feed, identities = access, branch protection + required checks = unforgeable gates, git log + issue timeline = history). Any cache is derivable and disposable, never authoritative.
- **No persistent AI:** Every AI component is invoked on a state transition, reads from GitHub, writes new state, and exits. No heartbeats, liveness, or daemon supervisors. Start with a 60s cron poll + static routing table; Supervisor/webhook runtime deferred until latency data demands it.
- **Communication:** `write → transition → route → invoke → read → write`. Dispatcher is a pure `transition → role` lookup (~10 lines of config), passes only artifact identity. Components are mutually invisible; recovery is re-invocation from the same transition. Poller is the backstop for lost webhooks.
- **Judgment checked:** Every AI-judgment box has an independent check it cannot influence — workers by review, review by PR-open trigger + human sampling, merges by deterministic CI the worker identity cannot affect, planning by outcome acceptance. Route on transitions, not states; record claims as state.
- **Review & merge:** Review triggers only on PR-open in fresh context (diff + story spec + ADRs, never worker reasoning). Findings return story to `ready`; the attempt counter increments on the next dispatch, not on the findings (canonical rule: `state-schema.md` §4.3). Merge gate is required CI checking: exact-head approval, tests green, diff within declared story scope, test deletion/weakening surfaced as distinct `test-change` class, hazard paths. Worker identities cannot merge/skip/re-run it.
- **Hazard paths:** Enumerated — secrets/credentials, dependency manifests, CI/workflow files, branch protection, IAM, data migrations, destructive operations, and `factory/spec/**` + `factory/gates/**` (factory cannot modify its own rules). Enforced via CODEOWNERS with agent identities excluded; one human ack per hazard diff.
- **Idempotency & poison:** Keyed on artifact + state version (at-least-once feed); duplicate deliveries no-op. Workers never self-claim — dispatcher assigns (no label-CAS race). Attempt counter with poison threshold at 3 checked **at dispatch time** → `blocked:poison` → human queue with full context (`state-schema.md` §4.3). Infrastructure failures do not increment. WIP limits, spend caps, and finite dependency graphs guarantee termination.
- **Human gates (bells only):** Humans appear only at `plan approval (READY)`, `hazard ack`, `poison rescue`, `cutover approval`, `outcome acceptance`, `sampling audit`. Zero stories human-reviewed in the happy path. Every bell logged to touch log as `decision | audit | rescue | relay` with time spent — only `relay` should trend to zero. Outcome acceptance is once per project (falsifiable criteria approved at READY, human tests pass/fail per criterion); failure creates a story or re-planning. Keep projects 5–10 stories; milestone-acceptance checkpoints are the only legitimate Sprint residue.
- **Roles collapsed/deleted:** Planning agent merges Product Manager + Chief Architect (split only on evidence of ≥3 concurrent projects with genuine coupling); Sequencer (deterministic code) replaces Delivery Manager's mechanical half — dependencies must be explicitly declared at authoring; Sprints, Epic layer, Governance-as-skill, Supervisor, heartbeats deleted. Architecture judgment lives as ADR skill + human review for consequential decisions.
- **Deferred until Phase 5 evidence:** Supervisor/webhook runtime, skills library, progress dashboards beyond the digest, multi-project concurrency, epic layer, event bus.
- **Human-authored scope:** Only `product.md` and roadmap commitment issues are human-authored; everything else is generated via the factory.
- **Measurement:** Instrument v1 baseline before migrating — touches classified, autonomous merge rate, rework/reopen rate, escaped defects, acceptance-catch rate, cost/wall-clock per accepted story, poison rate, stuck-work MTTR, sampling findings rate.

---
*Last updated: 2026-08-21 · Position: Phase 1 VERIFIED; Phase 2 BUILT and in acceptance · Source specs: `factory/spec/architecture-v2.1.md`, `factory/spec/implementation-plan-v1.md`*
