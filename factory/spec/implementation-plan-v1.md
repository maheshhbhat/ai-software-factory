# Software Factory — Implementation Plan v1.0

This document is the build spec for an AI software delivery factory. It is written
to be handed to Claude Code in a fresh repository. Build it in the phase order
below; each phase ends with a runnable verification. The architecture reference is
`factory/spec/architecture-v2.1.md`; where this plan is silent, that document
decides — except where `factory/spec/state-schema.md` states a canonical rule,
which overrides both (see §4.3, §9).

## Core design rules (bind every component)

1. **All state lives in GitHub.** Issues, labels, and PRs are the only authoritative
   state. Any cache must be rebuildable from GitHub and never authoritative.
2. **No persistent AI processes.** Every AI component is invoked on a state
   transition, reads what it needs from GitHub, writes new state, and exits.
   No heartbeats, no liveness, no long-running agent daemons.
3. **Communication is write → transition → route → invoke → read → write.**
   Components never call each other. The dispatcher routes transitions to
   components via a static routing table.
4. **Judgment is always checked by something it cannot influence.** Workers are
   checked by review; review is triggered by PR-open (never invoked by the
   worker); merges are gated by deterministic CI the worker's identity cannot
   affect; review itself is audited by human sampling.
5. **Idempotency everywhere.** Invocations are keyed on artifact + state version;
   duplicate deliveries must no-op. Route on transitions, not states. Workers
   never self-claim — the dispatcher assigns.
6. **Humans appear only at bells.** Bell types: plan approval (READY), hazard ack,
   poison rescue, scope decision, cutover approval, acceptance, sampling.
   (`scope-decision` was added during Phase 1: `state-schema.md` §4.2 requires a
   bell for the `story:blocked:scope` decision that the original six could not
   express.) Everything else runs
   silent. Every bell is logged to the touch log with a classification.

## Repository layout

```
factory/
  spec/               this plan + the v2.1 proposal + state-schema.md
  dispatcher/         cron-invoked router (phase 2)
  gates/              merge-gate CI workflow, hazard CODEOWNERS (phase 2)
  agents/
    planning/         prompt + invocation wrapper (phase 3)
    worker/           invocation wrapper around Claude Code headless (phase 4)
    review/           prompt + invocation wrapper (phase 4)
  touchlog/           touch log format + append script (phase 1)
  runs/               per-run KPI reports (phase 5)
```

## Phase 1 — State schema and touch log (no AI, no automation)

Deliverables:
- `spec/state-schema.md` defining:
  - Issue types: roadmap commitment, project, story. Story issues carry:
    spec, `phase:` label, `depends-on: [#...]` list, hazard flag, attempt
    counter, spend cap.
  - Label state machines. Project: `queued → ready-for-planning → planning →
    awaiting-ready → active → awaiting-acceptance → accepted`. Story:
    `blocked → ready → claimed → in-review → merged`, exceptions
    `blocked:poison`, `blocked:scope`.
  - Transition table: every legal transition, what causes it, what it triggers.
- Issue templates for the three types.
- `touchlog/`: an append-only log (one JSON line per human touch) with fields:
  timestamp, project, story, bell-type, classification
  (decision | audit | rescue | relay), seconds-spent, note.

Verification: create one project and three story issues by hand from the
templates; walk them through every legal transition manually; log the touches.

## Phase 2 — Deterministic rails (no AI)

Deliverables:
- **Merge gate**: a required CI status check that verifies ~~(a) review approval
  exists for the exact head SHA,~~ (b) tests green, (c) diff paths within the
  story's declared scope, ~~(d) test files not deleted/weakened without a
  distinct `test-change` label, (e) no hazard paths touched — or if touched,
  a human ack label from an allowed identity is present.~~
  **(a), (d) and (e) are withdrawn** — see `state-schema.md` §9.17. All three
  rest on an artifact the agent's own credential can write, which §9.14
  prohibits as a trust anchor; under the single-identity decision (#27) they
  would report enforcement the gate does not have.
- ~~**Hazard paths**: CODEOWNERS covering dependency manifests, CI/workflow
  files, migrations, secrets config, and `factory/spec/**` + `factory/gates/**`
  (the factory may never modify its own rules without human ack). Agent
  identities excluded from ownership.~~ **Withdrawn** with (e), and for the
  same reason: with one identity the owner and the agent are the same account,
  so code-owner review cannot distinguish them. Hazard paths are still
  enumerated, still pre-flagged on stories, and still surfaced by the advisory
  `merge-gate-surface` check; what is withdrawn is the claim that CODEOWNERS
  *enforces* them.
- **Dispatcher**: cron every 60s. Reads current labels via the API (not
  search), diffs against last-seen state versions, applies routing table:
  `ready-for-planning → invoke planning`, `story:ready → assign + invoke
  worker`, `PR opened → invoke review`, `awaiting-* → notify human`.
  Idempotent per artifact + state version — both terms defined in
  `state-schema.md` §9.10 and §9.1. Attempt counter increments on dispatch; the
  threshold check runs **before** the increment, so poison fires when a fourth
  dispatch would occur and `Attempt` reads 3. `state-schema.md` §4.3 is the
  canonical rule and overrides any other wording, here or in the architecture.
  Infrastructure failures (invocation errors) do not increment the counter, and
  a lease expiry restores the pre-dispatch value (§9.4).
- Bell notifications: a simple channel (email/Slack webhook) carrying the
  artifact link and required action.

Verification (still no AI): a fake "worker" script that opens a trivial PR when
invoked. Confirm: dispatch happens once and only once per transition (proven by
the §9.15 replay of Phase 1's recorded events); the merge gate blocks on each
violation class independently; and three *dispatched* attempts followed by a
fourth dispatch attempt produce poison at `Attempt = 3` plus notification — per
`state-schema.md` §4.3.5 the check precedes the increment, so asserting "3
failures then poison" without that distinction would pass against a wrong
implementation. ~~a hazard-path edit blocks without ack~~ is withdrawn with
deliverable (e) above.

The whole of the above is executed as one runnable suite —
`factory/acceptance/run_acceptance.py` — rather than as a checklist walked by
hand. Sixteen scenarios drive the real entry points against one in-memory
repository and assert on durable state. That distinction is not cosmetic: the
dependency defect in #107 passed every component test in the repository, because
every one of them handed the evaluator a pre-built map instead of making it go
and look.

Trust and sequencing constraints, frozen in Increment 1 (#28): the gate derives
its verdict only from inputs the agent's credential cannot fabricate — diff,
story `### Scope`, CI-computed check output, and the workflow boundary — never
from labels, `Agent-ID`, or comments (`state-schema.md` §9.14). `main-protection`
is left untouched while the gate is built and proven as a non-required check;
making it required, and dropping required approvals to zero, is a single atomic
ruleset edit under separate authorization (§9.13).

## Phase 3 — Planning agent

Deliverables:
- `agents/planning/prompt.md`: inputs are the direction/roadmap issue,
  `product.md`, existing ADRs, and repo read access. Output (as issue
  comments + created issues): for campaign-level input, a risk-ordered project
  proposal with rationale; for project-level input, an ADR, story issues with
  phase labels + explicit dependencies + hazard pre-flags, falsifiable
  acceptance criteria, expected-bells count, and a human-readable plan digest.
- Invocation wrapper: dispatcher-invokable, runs Claude Code (or API call) in
  headless mode, exits when artifacts are written.
- Two altitudes, one agent: the triggering artifact type selects campaign vs
  project behavior.

Verification: run it against a real module of the real product. Judge one
question: **is the plan digest one you would sign?** Iterate the prompt here
until yes. Do not proceed to phase 4 on a plan you would not sign.

## Phase 4 — Workers and review

Deliverables:
- **Worker wrapper**: invoked with a story issue ID. Reads story spec + ADRs +
  review findings (if retry). Branches, implements, opens a PR referencing the
  story, exits. Enforced spend cap per invocation. Never merges, never
  approves, never edits gate or spec files (hazard paths enforce this).
- **Review wrapper**: invoked on PR-open transition. Fresh context: sees only
  the diff + story spec + ADRs — never the worker's session. Posts findings
  (→ story back to `ready` with findings attached) or approval label bound to
  the head SHA.
- Sampling hook: every merged PR gets a 1-in-N lottery label (start N=3);
  sampled PRs land on the human bell channel for after-the-fact audit.

Verification: one toy story ("add a /health endpoint returning build sha")
through the full loop with zero manual steps between READY sign-off and merge.

## Phase 5 — Test ladder and KPIs

Run three rungs through the identical loop. Same KPI report per rung, written
to `runs/`:

| KPI | Definition |
|---|---|
| Touches | Count + classification from touch log; **relay must be 0** |
| Autonomy | Stories merged with zero human code involvement / total |
| Retry rate | Worker attempts / stories |
| Poison rate | Poisoned stories / stories |
| Escaped defects | Post-merge defects found (by sampling or acceptance) |
| Acceptance catches | Criteria failures at acceptance ("all green, wrong product") |
| Cost | $ per accepted story |
| Cycle time | READY sign-off → acceptance |

- **Rung 1 — toy story** (proves plumbing): the /health endpoint story.
  Pass: full loop, zero manual glue, touch log correct.
- **Rung 2 — small real feature** (proves gates): a genuine 2–4 story feature
  on the real product. Pass: autonomy ≥ 75%, relay = 0, no escaped defect.
- **Rung 3 — notifications extraction** (proves the thesis): the 10-story
  strangler plan from the spec, including shadow mode and cutover bells.
  Kill criteria, stated now: relay > 0, rescues > 30% of stories,
  any comparator-visible defect reaching a cutover step, or cost/story above
  your ceiling → stop and RETHINK before any further use.

After each rung: findings become edits to this plan and to the prompts;
Claude Code applies them; re-verify the affected phase before the next rung.

## Explicitly deferred (do not build in v1)

Supervisor/webhook runtime (cron is enough until latency data says otherwise) ·
skills library · progress dashboards beyond the digest · multi-project
concurrency · epic layer · event bus. Each requires evidence from phase 5
before it earns implementation.

## The two documents the human authors by hand

`product.md` (standing constraints and quality bars) and each roadmap
commitment issue. Templates for both are in the v2.1 proposal's worked
examples. Nothing else in the factory is human-authored.
