# State Schema — Phase 1

> Authoritative definition of GitHub as system of record for the factory.
> This document decides label names, issue fields, and legal transitions.
> All state lives in GitHub (issues + labels + PRs). Any cache is derivable and disposable.
> No component other than GitHub is authoritative. Ref: `architecture-v2.1.md` §1, §4; `implementation-plan-v1.md` Phase 1.

---

## 1. Issue types

Exactly three issue types. Each issue carries exactly one type label (`type:roadmap-commitment`, `type:project`, `type:story`) so type can be queried via the GitHub search API without parsing body.

| Type | Type label | Who creates | Purpose |
|---|---|---|---|
| Roadmap commitment | `type:roadmap-commitment` | Human (CTO/CEO) | Direction pebble; input to planning. |
| Project | `type:project` | Planning output (manually in Phase 1) | Bounded delivery unit (5–10 stories) with falsifiable acceptance criteria approved at `project:awaiting-ready`. |
| Story | `type:story` | Planning output (manually in Phase 1) | Single bounded work unit implemented by one worker invocation. |

Only `product.md` (not an issue) and roadmap-commitment issues are human-authored (per `PROGRESS.md` Decisions). Projects and stories are generated from the planning step; Phase 1 simulates this by hand from templates.

---

## 2. Label taxonomy

### 2.1 Lifecycle labels — exactly one per issue

Every `type:project` issue carries exactly one `project:*` label. Every `type:story` issue carries exactly one `story:*` label. The lifecycle label is the issue's state. No issue may carry zero or two lifecycle labels.

**Project lifecycle:**

```
project:queued
  → project:ready-for-planning
  → project:planning
  → project:awaiting-ready
  → project:active
  → project:awaiting-acceptance
  → project:accepted
```

Canonical name for the approval state is `project:awaiting-ready` (resolves `READY`/`awaiting-ready` drift).

Suggested GitHub label definitions:

| Label | Color | Description |
|---|---|---|
| `project:queued` | `ededed` | Project queued, not yet routed for planning |
| `project:ready-for-planning` | `fbca04` | Routed — eligible for planning invocation |
| `project:planning` | `fef2c0` | Planning in progress |
| `project:awaiting-ready` | `ff6b6b` | Awaiting human plan-approval bell |
| `project:active` | `0e8a16` | Approved; stories being worked |
| `project:awaiting-acceptance` | `d93f0b` | All stories merged; awaiting outcome acceptance |
| `project:accepted` | `0e8a16` | Acceptance passed; terminal |

**Story lifecycle:**

```
story:blocked ─┬─→ story:ready → story:claimed → story:in-review → story:merged
               ├─→ story:blocked:poison   (terminal exception; attempt >= 3)
               └─→ story:blocked:scope    (scope dispute, returns to blocked on resolution)
```

| Label | Description |
|---|---|
| `story:blocked` | Dependencies not satisfied or WIP-limited |
| `story:ready` | Unblocked; eligible for dispatch/assignment |
| `story:claimed` | Assigned to a worker (dispatcher-assigned, never self-claimed) |
| `story:in-review` | PR open, awaiting review |
| `story:merged` | PR merged; terminal success |
| `story:blocked:poison` | Attempt threshold exceeded; human rescue required; terminal until human intervenes |
| `story:blocked:scope` | Scope dispute; human decision required |

**Story `in-review → ready` retry** is a legal transition (findings posted). The story retains `story:ready` and `attempt` increments.

### 2.2 Non-lifecycle labels

| Label | Purpose |
|---|---|
| `type:roadmap-commitment` / `type:project` / `type:story` | Issue type |
| `phase:<name>` | Story phase (e.g. `phase:build`, `phase:ship`). Enumerate in project plan; template requires at least one. |
| `hazard` | Pre-flag that story touches a hazard path (Phase 2 enforces via CODEOWNERS). Body field is source of truth. |
| `test-change` | Distinct class for test weakening/deletion (Phase 2 gate). |

Phase scope labels are not state; they coexist with the single lifecycle label.

---

## 3. Structured fields — in the issue body, not a separate database

Per decision, `depends-on`, `hazard`, `attempt`, `spend-cap`, `scope`, `project-ref` are structured sections in the issue body (as generated from templates). They are authoritative via issue body/comments, never via an external DB. Labels `hazard`/`phase:*` mirror body fields for queryability but body is canonical.

### 3.1 Roadmap commitment fields

Rendered from `.github/ISSUE_TEMPLATE/roadmap-commitment.yml`:

* `## Background` — free text
* `## Constraints` — free text
* `## Non-goals` — free text
* `## Success hints` — free text

No state machine; open/closed only.

### 3.2 Project fields

Rendered from `.github/ISSUE_TEMPLATE/project.yml`:

* `## Goal` — one sentence
* `## Falsifiable acceptance criteria` — checklist, each item a yes/no test (e.g. "50+ page report exports to valid PDF in <30s" not "export works well")
* `## Stories` — list of ` #<story issue>` links (filled after planning)
* `## Expected bells` — integer estimate
* `## Risks / notes` — free text

### 3.3 Story fields

Rendered from `.github/ISSUE_TEMPLATE/story.yml`:

```
## Spec
<bounded spec>

## Project
#<project number>

## Phase
phase:<name>

## Depends-on
- #12
- #34
(none → leave empty / "- none")

## Hazard
- [x] touches hazard path  /  - [ ] no hazard

## Attempt
0

## Spend cap
$40 / 60 min (example; include unit)

## Scope
- src/reports/**
- tests/reports/**

## Acceptance notes
<how to verify this story in isolation>
```

Parsing rules:

* `Depends-on` — list of `#<number>` references. Empty means unblocked. Dependencies must be explicitly declared at authoring time (sequencer invariant).
* `Hazard` — checkbox; checked → also apply `hazard` label.
* `Attempt` — integer starting `0`, increments on each `in-review → ready` (findings) transition. `>=3` triggers `story:blocked:poison`.
* `Spend cap` — string with amount and unit (e.g. `$`, tokens, or wall-clock). Enforced per worker invocation in Phase 4; informational in Phase 1.
* `Scope` — path globs; future merge gate checks diff is within scope.
* `Project` — link to parent project issue.

Attempt and scope are updated via issue edits/comments in place; history is preserved by GitHub issue timeline.

---

## 4. Transition tables

All transitions are effected by editing the single lifecycle label (Phase 1: by hand; Phase 2+: by dispatcher/CI/human). Route on transitions, not states. Record claims as state (dispatcher assigns, workers never self-claim).

### 4.1 Project transitions

| From | To | Actor | Cause | Bell logged | Triggers next |
|---|---|---|---|---|---|
| `project:queued` | `project:ready-for-planning` | human (CTO) / dispatcher | label edit | no | planning invocation eligible |
| `project:ready-for-planning` | `project:planning` | human (manual in P1) / dispatcher | label edit | no | — |
| `project:planning` | `project:awaiting-ready` | planning output (manual in P1) | ADR + stories + criteria written | no | human queue |
| `project:awaiting-ready` | `project:active` | human | approves falsifiable criteria | **yes** `plan-approval` / `decision` | sequencer may mark stories ready |
| `project:awaiting-ready` | `project:awaiting-ready` | human | requests changes | yes `plan-approval` / `decision` (note explains) | stays, planning re-invoked |
| `project:active` | `project:awaiting-acceptance` | human (manual in P1) / sequencer | last story reached `story:merged` | no | human queue |
| `project:awaiting-acceptance` | `project:accepted` | human | runs criteria, all pass | **yes** `acceptance` / `decision` | terminal |
| `project:awaiting-acceptance` | `project:awaiting-acceptance` | human | criteria fail | **yes** `acceptance` / `rescue` or `decision` | new story or re-planning spawned; remains until pass |
| `project:accepted` | — | — | terminal | — | — |

### 4.2 Story transitions

| From | To | Actor | Cause | Notes |
|---|---|---|---|---|
| `story:blocked` | `story:ready` | human (manual in P1) / sequencer | dependencies satisfied, WIP allows | explicit `depends-on` already declared |
| `story:ready` | `story:claimed` | human (manual in P1) / dispatcher assigns | label edit | workers never self-claim; attempt does not increment |
| `story:claimed` | `story:in-review` | human / worker | PR opened referencing story | PR identity recorded in story comments |
| `story:in-review` | `story:merged` | human (manual in P1) / merge gate (Phase 2) | PR merged | terminal success |
| `story:in-review` | `story:ready` | review (manual in P1) / review skill | findings posted | **increment `Attempt` in body**, attach findings as comment |
| `story:in-review` | `story:blocked:poison` | human/dispatcher | `attempt >= 3` after findings | **yes** `poison-rescue` / `rescue` bell |
| `story:ready` / `story:claimed` / `story:in-review` | `story:blocked:poison` | — | same threshold if poison detected elsewhere | same bell |
| `*` | `story:blocked:scope` | human | scope dispute raised | **yes** `decision` or `rescue` as appropriate |
| `story:blocked:scope` | `story:blocked` | human | dispute resolved (scope amended or withdrawn) | re-enters normal flow |
| `story:blocked:scope` | `story:ready` | human | dispute resolved and unblocked | allowed if deps already satisfied |
| `story:blocked:poison` | `story:ready` | human | rescue resolves (new plan/spec) | resets or continues; log `rescue` |

**Poison rule:** attempt counter starts `0`. Each `in-review → ready` increments by 1. At `3`, next transition is to `blocked:poison`, not `ready`. Infrastructure failures do not increment (Phase 2 rule, recorded for completeness). Poison routes to human queue with full failure context.

**Idempotency:** duplicate deliveries keyed on `artifact + state version` no-op; Phase 1 simulates by not re-applying the same label transition twice.

---

## 5. Touch log (substrate for measurement)

Every human bell is logged to `factory/touchlog/touchlog.jsonl` via `factory/touchlog/append.py`. Classifications are exactly `decision | audit | rescue | relay` (per approval). Bell types: `plan-approval`, `hazard-ack`, `poison-rescue`, `cutover-approval`, `acceptance`, `sampling`. See `factory/touchlog/README.md` for JSONL schema and helper usage. Only `relay` should trend to zero; other touches are expected.

---

## 6. Out of scope (Phase 2+)

Dispatcher (cron + routing table), merge gate (required CI check), hazard CODEOWNERS, planning/worker/review agents, sampling lottery, `factory/runs/` KPIs, Supervisor/webhook runtime, skills library, dashboards, multi-project concurrency, epic layer, event bus. None are defined or implemented here.

---

## 7. Conventions

* Label names are lowercase, colon-separated, no spaces.
* A cache over GitHub (if any) is derivable and disposable; GitHub remains authoritative.
* `product.md` and roadmap-commitment issues are the only human-authored artifacts; all other issues are planning-generated (simulated by hand in Phase 1).
