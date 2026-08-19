# State Schema — Phase 1

> Authoritative definition of GitHub as system of record for the factory.
> This document decides label names, issue fields, and legal transitions.
> All state lives in GitHub (issues + labels + PRs + issue comments). Any cache is derivable and disposable.
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

Issues that are not one of these three types (e.g. build-task or review issues) carry no type label and no lifecycle label; they are outside this schema and are not routed.

---

## 2. Label taxonomy

### 2.1 Lifecycle labels — exactly one per issue

Every `type:project` issue carries exactly one `project:*` label. Every `type:story` issue carries exactly one `story:*` label. The lifecycle label is the issue's state. No issue may carry zero or two lifecycle labels. A transition is applied as one paired unlabel/label edit.

**Project lifecycle:**

```
project:queued
  → project:ready-for-planning
  → project:planning
  → project:awaiting-ready
  → project:active
  → project:awaiting-acceptance
  → project:accepted

correction edge: project:active → project:awaiting-ready   (§4.1: criteria amended after approval)
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
story:blocked → story:ready → story:claimed → story:in-review → story:merged
                     ↑                             │
                     └───────── findings ──────────┘        (no attempt increment; §4.3)

exceptions:
  story:ready → story:blocked:poison        (attempt budget exhausted at dispatch time; §4.3)
  * → story:blocked:scope → story:blocked | story:ready
  story:blocked:poison → story:ready        (human rescue; §4.3)
```

| Label | Description |
|---|---|
| `story:blocked` | Dependencies not satisfied or WIP-limited |
| `story:ready` | Unblocked; eligible for dispatch/assignment |
| `story:claimed` | Assigned to a worker (dispatcher-assigned, never self-claimed) |
| `story:in-review` | PR open, awaiting review |
| `story:merged` | PR merged; terminal success |
| `story:blocked:poison` | Attempt budget exhausted; human rescue required; terminal until a human rescues per §4.3 |
| `story:blocked:scope` | Scope dispute; human decision required |

### 2.2 Non-lifecycle labels

| Label | Purpose |
|---|---|
| `type:roadmap-commitment` / `type:project` / `type:story` | Issue type |
| `phase:<value>` | Story phase, mirroring the story form's Phase value (e.g. `phase:build`). Body is canonical. |
| `hazard` | Pre-flag that story touches a hazard path (Phase 2 enforces via CODEOWNERS). Body field is source of truth. |
| `test-change` | Distinct class for test weakening/deletion (Phase 2 gate). |

Phase scope labels are not state; they coexist with the single lifecycle label.

---

## 3. Structured fields — the rendered issue-form contract

Structured fields (`depends-on`, `hazard`, `attempt`, `spend-cap`, `scope`, `project-ref`) live in the issue body, never in an external DB. Labels `hazard` / `phase:*` mirror body fields for queryability; **the body is canonical**.

### 3.0 What a GitHub issue form actually writes

Issues of all three types are created **through the GitHub issue form** (`.github/ISSUE_TEMPLATE/*.yml`). This section is the contract; it describes what the committed forms produce, and it is what any future parser must accept. It is not a free-form convention.

For every field except `type: markdown` (which is never written to the body), the form writes:

```
### <field label>

<value>
```

with a blank line between the heading and the value and a blank line between sections. Headings are **`###`**, and the heading text is the field's `label` verbatim.

Value rendering, by control type:

| Control | Value written |
|---|---|
| `input` | the entered text, on one line |
| `textarea` | the entered text, verbatim, multi-line |
| `dropdown` | the **bare** selected option (`build` — not `phase:build`) |
| `checkboxes` | every option as `- [ ] <option label>` or `- [X] <option label>` |
| any optional field left empty | the literal `_No response_` |

Parsers must treat `_No response_` as empty, and must not assume a field's section exists in a body written before this contract.

### 3.1 Roadmap commitment — `.github/ISSUE_TEMPLATE/roadmap-commitment.yml`

Sections, in order: `### Background` (required), `### Constraints`, `### Non-goals`, `### Success hints`, `### product.md ref`.

No state machine; open/closed only.

### 3.2 Project — `.github/ISSUE_TEMPLATE/project.yml`

Sections, in order:

* `### Goal` — one sentence (required)
* `### Falsifiable acceptance criteria` — checklist, each item a yes/no test (required). "50+ page report exports to valid PDF in <30s", not "export works well". This section is the Project's definition of done and is what a human approves at `project:awaiting-ready` (§5).
* `### Stories` — one `#N` per line (filled after planning)
* `### Expected bells` — integer estimate
* `### Risks / notes` — free text
* `### Roadmap commitment` — parent commitment `#N`, or free text if none

### 3.3 Story — `.github/ISSUE_TEMPLATE/story.yml`

Sections, in order, exactly as the form writes them:

```
### Spec

<bounded spec>

### Project

#1

### Phase

build

### Depends-on

#12
#34

### Hazard

- [X] Touches hazard path

### Attempt

0

### Spend cap

$40 / 60 min

### Scope

- src/reports/**
- tests/reports/**

### Acceptance notes

<how to verify this story in isolation>
```

Parsing rules — one canonical form per field, no alternates:

* **`### Phase`** — the bare dropdown value (`build` | `ship` | `shadow` | `cutover` | `hardening`). Mirror as the label `phase:<value>`. The body never contains the `phase:` prefix.
* **`### Depends-on`** — either the single token `none`, or one bare `#<number>` per line, no bullets, no other text. `none` means unblocked. Dependencies must be explicitly declared at authoring time (sequencer invariant).
* **`### Hazard`** — the single checkbox option `Touches hazard path`, rendered `- [ ]` or `- [X]`. Checked → also apply the `hazard` label.
* **`### Attempt`** — integer, starts `0`. Semantics and the poison threshold are defined in §4.3; nothing else in this document may redefine them.
* **`### Spend cap`** — amount plus unit (e.g. `$40`, `60 min`, `200k tokens`). Enforced per worker invocation in Phase 4; informational in Phase 1.
* **`### Scope`** — path globs, one per line, `- ` bullets permitted (free-form textarea). Future merge gate checks the diff is within scope.
* **`### Project`** — `#N` link to the parent project issue.

`Attempt` and `Scope` are updated by editing the issue body in place; history is preserved by the GitHub issue timeline.

> **Fixture note (Phase 1):** issues created before this contract was written may use `##` headings and other spellings. They do not conform and must be recreated through the issue form before they are used as verification evidence.

---

## 4. Transition tables

All transitions are effected by editing the single lifecycle label (Phase 1: by hand; Phase 2+: by dispatcher/CI/human). Route on transitions, not states. Record claims as state (dispatcher assigns, workers never self-claim).

### 4.1 Project transitions

| From | To | Actor | Cause | Bell logged | Triggers next |
|---|---|---|---|---|---|
| `project:queued` | `project:ready-for-planning` | human (CTO) / dispatcher | label edit | no | planning invocation eligible |
| `project:ready-for-planning` | `project:planning` | human (manual in P1) / dispatcher | label edit | no | — |
| `project:planning` | `project:awaiting-ready` | planning output (manual in P1) | ADR + stories + criteria written | no | human queue |
| `project:awaiting-ready` | `project:active` | human | approves the criteria; **approval comment posted first** (§5.1) | **yes** `plan-approval` / `decision` | sequencer may mark stories ready |
| `project:awaiting-ready` | `project:planning` | human | requests changes to the plan | yes `plan-approval` / `decision` (note explains) | planning re-invoked; returns to `awaiting-ready` |
| `project:active` | `project:awaiting-ready` | human | **criteria amended after approval** — the standing approval is superseded (§5.2) | no (no human bell rung; supersession comment required) | human re-approval gate |
| `project:active` | `project:awaiting-acceptance` | human (manual in P1) / sequencer | every story reached `story:merged` | no | human queue |
| `project:awaiting-acceptance` | `project:accepted` | human | acceptance comment records **pass for every criterion** (§5.3) | **yes** `acceptance` / `decision` | terminal |
| `project:awaiting-acceptance` | `project:active` | human | acceptance comment records **any criterion failed** (§5.3) | **yes** `acceptance` / `rescue` or `decision` | new story or re-planning spawned; returns to `awaiting-acceptance` when merged |
| `project:accepted` | — | — | terminal | — | — |

Both former self-loops (`awaiting-ready → awaiting-ready`, `awaiting-acceptance → awaiting-acceptance`) are replaced by real edges above: a label edit that ends on the same label emits no transition and therefore cannot be routed (`architecture-v2.1.md` §4, "route on transitions, not states").

### 4.2 Story transitions

| From | To | Actor | Cause | Notes |
|---|---|---|---|---|
| `story:blocked` | `story:ready` | human (manual in P1) / sequencer | dependencies satisfied, WIP allows | explicit `depends-on` already declared |
| `story:ready` | `story:claimed` | human (manual in P1) / dispatcher assigns | worker attempt dispatched | workers never self-claim; **`Attempt` increments here** (§4.3) |
| `story:claimed` | `story:in-review` | worker (human in P1) | PR opened referencing story | PR identity recorded in story comments |
| `story:in-review` | `story:merged` | merge gate (human in P1) | PR merged | terminal success |
| `story:in-review` | `story:ready` | review (manual in P1) / review skill | findings posted | **no `Attempt` change**; attach findings as a comment |
| `story:ready` | `story:blocked:poison` | dispatcher (human in P1) | `Attempt >= 3` and another attempt would otherwise be dispatched | **raises** the `poison-rescue` bell; no dispatch occurs. No touch is logged here — the touch belongs to the rescue (§4.3.8) |
| `story:blocked:poison` | `story:ready` | human | rescue per §4.3 | rescue comment + `Attempt` reset required; **yes** — the single `poison-rescue` / `rescue` touch is logged here (§4.3.8) |
| `*` | `story:blocked:scope` | human | scope dispute raised | **yes** `decision` or `rescue` as appropriate |
| `story:blocked:scope` | `story:blocked` | human | dispute resolved (scope amended or withdrawn) | re-enters normal flow |
| `story:blocked:scope` | `story:ready` | human | dispute resolved and unblocked | allowed if deps already satisfied |

### 4.3 Attempt counter and poison — canonical rule

This section is the single definition of attempt and poison behaviour. Where `implementation-plan-v1.md` Phase 2 ("increments on dispatch") and `architecture-v2.1.md` §3 ("the attempt counter increments" on findings) read differently, **this section decides**; the counter named in both documents is this one.

1. **Meaning.** `Attempt` is the number of worker attempts **dispatched** for the story. It is not a count of review findings.
2. **Increment point.** `Attempt` increments by exactly 1 on `story:ready → story:claimed`, written into the issue body by the dispatching actor as part of that transition.
3. **Findings do not increment.** `story:in-review → story:ready` returns the story for another attempt and leaves `Attempt` unchanged; the next dispatch is what increments it.
4. **Infrastructure failures do not count.** If invocation fails before the worker starts, restore the previous `Attempt` value and return the story to `story:ready`. Only an attempt that actually started is counted.
5. **Threshold.** `ATTEMPT_MAX = 3` for v1. The check runs **at dispatch time, before incrementing**: if `Attempt >= ATTEMPT_MAX`, do not dispatch — transition `story:ready → story:blocked:poison`, which raises the `poison-rescue` bell (no touch yet; §4.3.8). A story therefore gets exactly 3 dispatched attempts and reads `Attempt = 3` when poisoned.
6. **Rescue.** Only a human may leave `story:blocked:poison`. A rescue requires all three, in this order: (a) a rescue comment on the story stating what changed (spec, scope, or dependencies amended), (b) `Attempt` reset to `0` in the body, (c) a `poison-rescue` touch logged. Then `story:blocked:poison → story:ready`.
7. **Bounded forward progress.** Rescues per story are capped at **2**, counted as the number of times `story:blocked:poison` has been applied in the issue timeline (GitHub timeline is authoritative history). On the third poisoning the story is **not** rescued: it is closed and returned to planning as a re-planning input. A story therefore consumes at most 9 dispatched worker attempts in its lifetime, and the loop terminates by construction.
8. **One bell, one touch.** Entering `story:blocked:poison` *raises* the bell — it routes the story to the human queue and no human time has been spent yet, so **no touch-log line is written at poisoning**. The single `poison-rescue` / `rescue` line is written exactly once, when the human actually performs or approves the rescue (§4.3.6c), and its `seconds_spent` measures that human's time. A poisoning that is never rescued therefore has no touch, which is correct: the KPI counts human touches, not queue entries.

**Idempotency:** duplicate deliveries keyed on `artifact + state version` no-op; Phase 1 simulates this by not re-applying the same label transition twice.

---

## 5. Decision evidence — where human judgment is recorded

GitHub is the system of record, so a human decision is not recorded until it exists **as a comment on the affected issue**. `factory/touchlog/touchlog.jsonl` is measurement/KPI evidence (how many touches, of what class, costing how long) and is never the decision itself. Every bell produces both: one comment, one touch-log line.

### 5.1 Plan approval (`project:awaiting-ready → project:active`)

A comment on the Project issue, posted **before** the label edit:

```
## Plan approval

decision: approved
actor: @handle

Approved criteria (verbatim copy of the Falsifiable acceptance criteria section at approval time):

- [ ] <criterion 1>
- [ ] <criterion 2>
...

note: <why this is signable>
```

**Approval-binding rule:** an approval is valid only while the Project's `### Falsifiable acceptance criteria` section is identical to the checklist quoted in the most recent approval comment. Any edit to that section supersedes the standing approval — this is checkable from GitHub alone and requires no revision field. `decision: changes-requested` uses the same comment shape and accompanies `awaiting-ready → planning`.

### 5.2 Superseded approval (`project:active → project:awaiting-ready`)

When criteria are amended after approval, a comment on the Project issue records it before the label edit:

```
## Approval superseded

reason: <what changed in the criteria and why>
superseded-approval: <link to the approval comment now void>
actor: @handle
```

No bell is rung and no touch is logged for the supersession itself — no human judgment was exercised, the standing approval merely became void. The next `plan-approval` bell is rung by the human at the re-approval gate.

### 5.3 Outcome acceptance (`project:awaiting-acceptance → ...`)

A comment on the Project issue recording **pass/fail per criterion**, one line per criterion, posted before the label edit:

```
## Acceptance

result: pass | fail
actor: @handle

- criterion 1 — pass
- criterion 2 — fail: <what was observed>
...

follow-up: <issue link for each failed criterion, or "none">
```

All pass → `project:accepted`. Any fail → `project:active`, with a new story or re-planning input linked from `follow-up`. Acceptance happens once per project (`architecture-v2.1.md` §2.3).

### 5.4 Story-level bells

`poison-rescue` (§4.3.6), `hazard-ack`, `cutover-approval`, and `sampling` are recorded as comments on the affected story or PR, each stating the decision and the actor, and each accompanied by exactly one touch-log line.

---

## 6. Touch log (substrate for measurement)

Every human bell is logged to `factory/touchlog/touchlog.jsonl` via `factory/touchlog/append.py`. Classifications are exactly `decision | audit | rescue | relay`. Bell types: `plan-approval`, `hazard-ack`, `poison-rescue`, `cutover-approval`, `acceptance`, `sampling`. See `factory/touchlog/README.md` for the JSONL schema and helper usage. Only `relay` should trend to zero; other touches are expected.

The touch log measures bells; §5 records what was decided. A bell with a touch-log line and no comment is an incomplete record, and so is a comment with no touch-log line.

---

## 7. Out of scope (Phase 2+)

Dispatcher (cron + routing table), merge gate (required CI check), hazard CODEOWNERS, planning/worker/review agents, sampling lottery, `factory/runs/` KPIs, Supervisor/webhook runtime, skills library, dashboards, multi-project concurrency, epic layer, event bus. None are defined or implemented here.

Known open items deferred to the Phase 2 readiness pass, recorded so they are not lost: claim-expiry/recovery transition out of `story:claimed`; remaining template/schema field drift beyond §3; hazard fixture location; branch-protection availability for unforgeable gates.

Two requirements the Phase 1 walk surfaced, to be satisfied **before** the dispatcher is implemented (requirements here, not implementations):

* **Lifecycle label changes must be atomic.** A transition must replace the label set in one complete-label-set update, so no observer ever sees an issue carrying zero or two lifecycle labels. Adding the new label and removing the old one as two separate calls breaks the §2.1 invariant transiently — observed during the Phase 1 walk as a ~1s zero-label window on one transition, and as a momentary two-label state when the ordering flips. A poller reading mid-swap would see the issue in no state or two states and could mis-route or skip it.
* **Terminal story issues need defined open/closed semantics.** `story:merged`, `story:blocked:poison` after the rescue cap, and any other terminal state must state whether the GitHub issue is closed and with what reason. Nothing defines this today, so a `story:merged` issue stays open — which matters because "open stories" is the obvious dispatcher query.

---

## 8. Conventions

* Label names are lowercase, colon-separated, no spaces.
* A cache over GitHub (if any) is derivable and disposable; GitHub remains authoritative.
* `product.md` and roadmap-commitment issues are the only human-authored artifacts; all other issues are planning-generated (simulated by hand in Phase 1).
