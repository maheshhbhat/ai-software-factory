# State Schema — Phase 1

> Authoritative definition of GitHub as system of record for the factory.
> This document decides label names, issue fields, and legal transitions.
> All state lives in GitHub (issues + labels + PRs + issue comments). Any cache is derivable and disposable.
> No component other than GitHub is authoritative. Ref: `architecture-v2.1.md` §1, §4; `implementation-plan-v1.md` Phase 1.
>
> **`SCHEMA_VERSION = 2.2.0`** — §1–§8 are the Phase 1 state model, verified and unchanged in substance. §9 freezes the executable contracts Phase 2 implements (§9.1 defines versioning and compatibility).

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

Every `type:project` issue carries exactly one `project:*` label. Every `type:story` issue carries exactly one `story:*` label. The lifecycle label is the issue's state. No issue may carry zero or two lifecycle labels. A transition is applied as **one complete label-set replacement** — see §9.2, which is binding for every automated actor. (Phase 1 applied transitions by hand as paired unlabel/label edits; that is what produced the transient windows §9.2 now prohibits.)

**Project lifecycle:**

```
project:queued
  → project:ready-for-planning
  → project:planning
  → project:awaiting-ready
  → project:active
  → project:awaiting-acceptance
  → project:accepted

standing branch: project:awaiting-ready → project:standing  (§4.1.1: continuous work; no acceptance edge)

correction edges:
  project:active   → project:awaiting-ready   (§4.1: criteria amended after approval)
  project:standing → project:awaiting-ready   (§4.1: criteria amended after approval)
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
| `project:standing` | `5319e7` | Approved and continuous; accepts stories, never reaches acceptance (§4.1.1) |

**Story lifecycle:**

```
story:blocked → story:ready → story:claimed → story:in-review → story:merged
                     ↑                             │
                     └───────── findings ──────────┘        (no attempt increment; §4.3)

  story:claimed → story:completed    (the bounded assignment succeeded and required
                                      no deliverable; terminal success, §9.16)

exceptions:
  story:ready → story:blocked:poison        (attempt budget exhausted at dispatch time; §4.3)
  * → story:blocked:scope → story:blocked | story:ready
  story:blocked:poison → story:ready        (human rescue; §4.3)
```

Two terminal successes, and they are not interchangeable. `story:merged` is
success **through the PR/merge path**. `story:completed` is success where the
assignment required no deliverable, so there is nothing to review or merge
(§9.16). Neither may stand in for the other, and neither is `story:cancelled` —
that state means work was deliberately stopped, which is a different fact about
the world and stays a human decision.

| Label | Description |
|---|---|
| `story:blocked` | Dependencies not satisfied or WIP-limited |
| `story:ready` | Unblocked; eligible for dispatch/assignment |
| `story:claimed` | Assigned to a worker (dispatcher-assigned, never self-claimed) |
| `story:in-review` | PR open, awaiting review |
| `story:merged` | PR merged; terminal success through the delivery path |
| `story:completed` | Bounded assignment succeeded and required no deliverable; terminal success (§9.3, §9.16). Applied only on durable proof — never on an assumption |
| `story:cancelled` | Work deliberately stopped, with or without a deliverable; terminal (§9.3). Human decision only — never applied by a component |
| `story:blocked:poison` | Attempt budget exhausted; human rescue required; terminal until a human rescues per §4.3 |
| `story:blocked:scope` | Scope dispute; human decision required |

### 2.2 Non-lifecycle labels

| Label | Purpose |
|---|---|
| `type:roadmap-commitment` / `type:project` / `type:story` | Issue type |
| `phase:<value>` | Story phase, mirroring the story form's Phase value (e.g. `phase:build`). Body is canonical. |
| `hazard` | Pre-flag that story touches a hazard path. **Not mechanically enforced** — the CODEOWNERS deliverable is withdrawn (§9.17); the label routes and reports, and the advisory `merge-gate-surface` check classifies the diff. Body field is source of truth. |
| `test-change` | Distinct class for test weakening/deletion. **Not enforced** — gate check (d) is withdrawn (§9.17), because a label that turns a red verdict green is a trust anchor the agent's own credential can write (§9.14). |

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
* **`### Scope`** — path globs, one per line. Under `SCHEMA_VERSION 2.0.0` the dialect is machine-readable and bullets are **not** permitted: see §9.6, which is the contract the merge gate consumes. (Phase 1 stories were authored under `1.x`, where a leading `- ` was accepted.)
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
| `project:active` | `project:awaiting-acceptance` | human (manual in P1) / sequencer | every story reached a terminal success — `story:merged` or `story:completed`; after readiness promotion, an exact-revision Production Readiness artifact is also `ready` | no | human queue |
| `project:awaiting-acceptance` | `project:accepted` | human | acceptance comment records **pass for every criterion** (§5.3) | **yes** `acceptance` / `decision` | terminal |
| `project:awaiting-acceptance` | `project:active` | human | acceptance comment records **any criterion failed** (§5.3) | **yes** `acceptance` / `rescue` or `decision` | new story or re-planning spawned; returns to `awaiting-acceptance` when merged |
| `project:accepted` | — | — | terminal — issue **closed as completed** (§9.3) | — | — |
| `project:awaiting-ready` | `project:standing` | human | approves the criteria of a **standing** Project; **approval comment posted first** (§5.1) | **yes** `plan-approval` / `decision` | sequencer may mark stories ready |
| `project:standing` | `project:awaiting-ready` | human | **criteria amended after approval** — the standing approval is superseded (§5.2) | no (no human bell rung; supersession comment required) | human re-approval gate |
| `project:standing` | — | — | **no completion edge** — a standing Project never reaches `project:awaiting-acceptance` or `project:accepted` (§4.1.1) | — | — |

Production Readiness is independent of Delivery and Review. For Projects created
under the operating-envelope contract, the evaluator checks integrated `main`
and publishes one digest-bound pass/fail result per envelope ID. The artifact is
bound to the repository, Project, exact commit SHA, timestamps, bounded external
observations, and the Project envelope digest. In `warning` mode its result is
recorded but does not change this lifecycle. Promotion to `blocking` is a
reviewed configuration change; then missing, stale, malformed, `not-ready`, or
incomplete evidence leaves the Project `project:active`. Projects predating the
operating-envelope section are not silently retrofitted with this gate.

Both former self-loops (`awaiting-ready → awaiting-ready`, `awaiting-acceptance → awaiting-acceptance`) are replaced by real edges above: a label edit that ends on the same label emits no transition and therefore cannot be routed (`architecture-v2.1.md` §4, "route on transitions, not states").

#### 4.1.1 Standing projects — continuous work, no acceptance edge

`project:standing` is a Project lifecycle value for work that is **continuous rather than bounded**: it has no definite end, so there is no point at which "did this deliver what it promised?" can be asked once and answered. Factory maintenance is the case it exists for (#314).

A Project in `project:standing`:

1. **Is approved exactly once, through the ordinary §5.1 plan-approval bell**, at `project:awaiting-ready → project:standing`. Same comment shape, same bell, same approval-binding rule — editing its `### Falsifiable acceptance criteria` supersedes the approval and returns it to `project:awaiting-ready` via §5.2, exactly as it does for `project:active`. Adding or completing Stories never supersedes the approval.
2. **Accepts Stories and dispatches normally.** Stories declare it in `### Project` and move through the unchanged §4.2 Story lifecycle. Each Story is still reviewed at the merge gate. *Not yet live:* `plan_story_readiness` and the dispatcher's §9.9 authorization chain both still require `project:active`, so this clause is contract-only until #314 SM-01 wires them — deliberately, because making stories ready ahead of the dispatcher would park them in `story:ready` consuming WIP with nothing able to claim them.
3. **Never transitions to `project:awaiting-acceptance` or `project:accepted`.** There is no such edge in §4.1, and no actor may synthesise one. `plan_project_completion` (`factory/runtime/sequencer.py`) must recognise this state and exclude it **by name**, not by relying on the empty-or-unparseable-`### Stories` skip — that skip is silent and Project #298 is removing it, so a deliberate design must not be built on it.
4. **Is mutually exclusive with every other project lifecycle value by construction.** §2.1 permits exactly one `project:*` label, and `lifecycle_of` resolves an issue carrying two of them to *no* lifecycle rather than to the first — so a Project cannot be standing and bounded at the same time, and one that is mislabelled is inert rather than ambiguous.

**The bounded lifecycle is unchanged for every other Project.** A Project not carrying `project:standing` follows §4.1 exactly as before: it still reaches `project:awaiting-acceptance` when all its Stories reach a terminal success, and it still gets a criterion-by-criterion §5.3 acceptance bell. Nothing in this section relaxes that.

**What this costs, stated here rather than discovered later.** A standing Project never reaches outcome acceptance, so its work never gets the per-Project, criterion-by-criterion review every bounded Project gets. Per-Story review at the merge gate and the sampling audit carry that weight instead, and neither is a substitute for it. The ADR required by #314 SM-04 is where that trade and its compensating controls are recorded.

### 4.2 Story transitions

| From | To | Actor | Cause | Notes |
|---|---|---|---|---|
| `story:blocked` | `story:ready` | human (manual in P1) / sequencer | dependencies satisfied, WIP allows | explicit `depends-on` already declared |
| `story:ready` | `story:claimed` | human (manual in P1) / dispatcher assigns | worker attempt dispatched | workers never self-claim; **`Attempt` increments here** (§4.3) |
| `story:claimed` | `story:in-review` | **runtime** (worker/human in P1) | PR opened referencing story | PR links to the story per §9.5; live from §9.11 (#114). No `Attempt` change |
| `story:claimed` | `story:ready` | dispatcher | **claim lease expired** with no linked PR | **`Attempt` decrements by 1** (restores the pre-dispatch value); see §9.4 |
| `story:claimed` | `story:completed` | runtime completion path | the dispatched worker returned a **definite success** and durable evidence proves the bounded assignment was carried out, with **no PR linked** per §9.5 | terminal success; the issue is **closed as completed** (§9.3). **No `Attempt` change** — the attempt was dispatched and it succeeded. Every precondition in §9.16 must hold; anything short of all of them leaves the story claimed for §9.4 |
| `story:in-review` | `story:merged` | **runtime**, on the merge gate's verdict (human in P1) | PR merged | terminal success; the issue is **closed as completed** (§9.3). Live from §9.11 (#114). No `Attempt` change |
| `story:in-review` | `story:ready` | review (manual in P1) / review skill | findings posted | **no `Attempt` change**; attach findings as a comment |
| `story:ready` | `story:blocked:poison` | dispatcher (human in P1) | `Attempt >= 3` and another attempt would otherwise be dispatched | **raises** the `poison-rescue` bell; no dispatch occurs. No touch is logged here — the touch belongs to the rescue (§4.3.8) |
| `story:blocked:poison` | `story:ready` | human | rescue per §4.3 | rescue comment + `Attempt` reset required; **yes** — the single `poison-rescue` / `rescue` touch is logged here (§4.3.8) |
| `*` | `story:blocked:scope` | human | scope dispute raised | **raises** the `scope-decision` bell; no touch here — the touch belongs to the resolution (§4.3.8 principle) |
| `story:blocked:scope` | `story:blocked` | human | dispute resolved (scope amended or withdrawn) | **yes** `scope-decision` / `decision`, logged once here; re-enters normal flow |
| `story:blocked:scope` | `story:ready` | human | dispute resolved and unblocked | **yes** `scope-decision` / `decision`, logged once here; allowed if deps already satisfied |

### 4.3 Attempt counter and poison — canonical rule

This section is the single definition of attempt and poison behaviour. Where `implementation-plan-v1.md` Phase 2 ("increments on dispatch") and `architecture-v2.1.md` §3 ("the attempt counter increments" on findings) read differently, **this section decides**; the counter named in both documents is this one.

1. **Meaning.** `Attempt` is the number of worker attempts **dispatched** for the story. It is not a count of review findings.
2. **Increment point.** `Attempt` increments by exactly 1 on `story:ready → story:claimed`, written into the issue body by the dispatching actor as part of that transition.
3. **Findings do not increment.** `story:in-review → story:ready` returns the story for another attempt and leaves `Attempt` unchanged; the next dispatch is what increments it.
4. **Infrastructure failures do not count.** If invocation fails before the worker starts, restore the previous `Attempt` value and return the story to `story:ready`. Only an attempt that actually started is counted.
5. **Threshold.** `ATTEMPT_MAX = 3` for v1. The check runs **at dispatch time, before incrementing**: if `Attempt >= ATTEMPT_MAX`, do not dispatch — transition `story:ready → story:blocked:poison`, which raises the `poison-rescue` bell (no touch yet; §4.3.8). A story therefore gets exactly 3 dispatched attempts and reads `Attempt = 3` when poisoned.
6. **Rescue.** Only a human may leave `story:blocked:poison`. A rescue requires all three, in this order: (a) a rescue comment on the story stating what changed (spec, scope, or dependencies amended), (b) `Attempt` reset to `0` in the body, (c) a `poison-rescue` touch logged. Then `story:blocked:poison → story:ready`.
7. **Bounded forward progress.** Rescues per story are capped at **2**, counted as the number of times `story:blocked:poison` has been applied in the issue timeline (GitHub timeline is authoritative history). On the third poisoning the story is **not** rescued: the issue is closed as *not planned* (§9.3) and returned to planning as a re-planning input. A story therefore consumes at most 9 dispatched worker attempts in its lifetime, and the loop terminates by construction.
8. **One bell, one touch.** Entering `story:blocked:poison` *raises* the bell — it routes the story to the human queue and no human time has been spent yet, so **no touch-log line is written at poisoning**. The single `poison-rescue` / `rescue` line is written exactly once, when the human actually performs or approves the rescue (§4.3.6c), and its `seconds_spent` measures that human's time. A poisoning that is never rescued therefore has no touch, which is correct: the KPI counts human touches, not queue entries.

**Idempotency:** duplicate deliveries keyed on `artifact + state version` no-op; Phase 1 simulates this by not re-applying the same label transition twice.

---

## 5. Decision evidence — where human judgment is recorded

GitHub is the system of record, so a human decision is not recorded until it exists **as a comment on the affected issue**. `factory/touchlog/touchlog.jsonl` is measurement/KPI evidence (how many touches, of what class, costing how long) and is never the decision itself. Every bell produces both: one comment, one touch-log line. Continuation may consume an authoritative acceptance comment and advance Project state only after its canonical touch evidence is durably appended and read back. The log does not decide; it is required evidence for consuming the decision. If append or verification fails, the decision remains unconsumed and the Project remains `project:awaiting-acceptance`.

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

**Decision identity and replay.** Acceptance identity is the normalized tuple of
verdict, complete criterion-key-to-pass/fail checklist, and canonical issue
references on the `follow-up:` line. Comment IDs, timestamps, whitespace, and
incidental issue numbers in prose do not create novelty. An unchanged tuple is
replay/relay and writes no second touch; a changed verdict, checklist result, or
canonical follow-up is a new human decision and receives a distinct touch.

### 5.4 Story-level bells

`poison-rescue` (§4.3.6), `scope-decision` (§4.2), `hazard-ack`, `cutover-approval`, and `sampling` are recorded as comments on the affected story or PR, each stating the decision and the actor, and each accompanied by exactly one touch-log line.

---

## 6. Touch log (substrate for measurement)

Every human bell is logged to `factory/touchlog/touchlog.jsonl` via `factory/touchlog/append.py`. Classifications are exactly `decision | audit | rescue | relay`. Bell types: `plan-approval`, `hazard-ack`, `poison-rescue`, `scope-decision`, `cutover-approval`, `acceptance`, `sampling`. This list is canonical and extends the six named in `implementation-plan-v1.md` core rule 6: `scope-decision` was added because §4.2 requires a bell for the `story:blocked:scope` decision and no existing type covered it. See `factory/touchlog/README.md` for the JSONL schema and helper usage. Only `relay` should trend to zero; other touches are expected.

The touch log measures bells; §5 records what was decided. A bell with a touch-log line and no comment is an incomplete record, and so is a comment with no touch-log line. For acceptance, the latter remains an unconsumed decision at the bell until continuation can append and verify the missing measurement receipt.

---

## 7. Out of scope (Phase 2+)

Dispatcher (cron + routing table), merge gate (required CI check), hazard CODEOWNERS, planning/worker/review agents, sampling lottery, `factory/runs/` KPIs, Supervisor/webhook runtime, skills library, dashboards, multi-project concurrency, epic layer, event bus. None are defined or implemented here.

Known open items deferred to the Phase 2 readiness pass, recorded so they are not lost: claim-expiry/recovery transition out of `story:claimed`; remaining template/schema field drift beyond §3; hazard fixture location; branch-protection availability for unforgeable gates.

Both requirements the Phase 1 walk surfaced are now **specified** in §9 rather than pending: atomic lifecycle-label replacement (§9.2) and terminal open/closed semantics (§9.3). The claim-expiry transition is §9.4. Remaining deferrals: template/schema field drift beyond §3, hazard fixture location.

---

## 8. Conventions

* Label names are lowercase, colon-separated, no spaces.
* A cache over GitHub (if any) is derivable and disposable; GitHub remains authoritative.
* `product.md` and roadmap-commitment issues are the only human-authored artifacts; all other issues are planning-generated (simulated by hand in Phase 1).

---

## 9. Phase 2 execution contracts

Frozen under Phase 2 Increment 1 (#28) so Increment 2 implements policy rather than inventing it. This section is **specification only** — no component described here exists yet.

Nothing in §9 rewrites Phase 1: artifacts created before it (Project #1, stories #10/#11/#12, the five touch-log lines, their timelines) remain valid evidence exactly as recorded, and where a rule below would have applied differently, §9.12 says so explicitly.

### 9.1 Schema version and compatibility

This document is the contract, so the contract carries the version:

```
SCHEMA_VERSION = 2.2.0
```

Semantics: **major** changes break a component that has not been updated (a lifecycle label renamed, a field's meaning changed); **minor** adds contract that older components can ignore; **patch** is editorial. Phase 1 was authored under an implicit `1.x`.

**Version history.**

| Version | Change |
|---|---|
| `2.0.0` | §9 executable contracts frozen for Phase 2 |
| `2.1.0` | `story:completed` added (§9.16), with the §9.10 dependency rule and the §9.3 cancellation description narrowed to match (#104). **Minor:** a component that does not know the label reads it as not-ready and waits, which is the fail-closed direction. Nothing was renamed and no field changed meaning, so the major stays `2` and every pinned component keeps working |
| `2.2.0` | Merge-gate checks (a), (d) and (e) and the CODEOWNERS deliverable withdrawn (§9.17); `story:claimed → story:in-review` and `story:in-review → story:merged` promoted to live routes (§9.11); §9.3's poison closure corrected to match §4.3.7 (#114). **Minor:** nothing is renamed and no field changes meaning. The withdrawal removes checks that were never implemented, so no component loses a behaviour it had. The two promoted routes are additive — a component that does not know them leaves those transitions to a human, which is what every component did until now. The §9.3 correction makes a *first* poisoning leave the issue open; a component that expected it closed would have been reading a state the contract never described |

Every Phase 2 component pins the major version it implements and, on encountering state written under a different major version, **halts and routes to the human queue** — it must never guess. Fail-closed is the rule wherever §9 is silent.

Version is asserted, not stamped on every issue: issue bodies are rendered forms and carry no room for a marker. The dispatcher records the pinned version in its own run log, and any artifact it writes that has a free-text field carries `schema-version: 2.2.0`.

### 9.2 Atomic lifecycle transitions

A transition **must** replace the issue's entire label set in a single API call:

```
PATCH /repos/{owner}/{repo}/issues/{number}   {"labels": [<complete final set>]}
```

The array carries **every** label the issue should end with — lifecycle, `type:*`, `phase:*`, `hazard`, `agent:*` — not just the one changing. Add-then-remove as two calls is prohibited: the Phase 1 walk produced a ~1s window with zero lifecycle labels on one transition, and a momentary two-label state when the call order flipped, either of which a poller can observe and mis-route.

Required behaviour:

* **Precondition check.** Read the current label set, verify the expected `from` state, then write. If the observed state is not the expected `from`, abort and re-derive — another actor moved it.
* **Idempotent.** Writing a label set identical to the current one is a no-op and must not be treated as a transition.
* **Reads prefer events.** Routing decisions read the issue timeline's `labeled`/`unlabeled` events, not a label snapshot, so a read that races a write cannot fabricate a state that never settled.

### 9.3 Terminal states and issue closure

| Terminal state | Issue | Reason |
|---|---|---|
| `story:merged` | closed | completed |
| `story:completed` | closed | completed |
| `story:blocked:poison` after the §4.3.7 rescue cap | closed | not planned |
| `story:blocked:poison` **before** that cap | **open** | — (waiting on a human) |
| `story:cancelled` | closed | not planned |
| `project:accepted` | closed | completed |
| every other state | open | — |

Closure is part of the transition, written in the same operation wherever the API allows. The dispatcher's work queue is therefore **open issues carrying a `type:story` label**, which makes "is this story still live?" a single cheap query rather than a label scan.

**`story:completed` — succeeded with nothing to merge.** Added by #104. Some
assignments are complete when they have been *carried out*, not when something
lands: a transport verification, a probe, an acknowledgement. Their success is
real and it produces no pull request, so neither `story:merged` (nothing merged)
nor `story:cancelled` (nothing was called off) describes it. Before this state
existed, a factory that could prove such an assignment had succeeded had nowhere
honest to record it, and #103 sat at `story:claimed` after a verified success —
which is not a resting state but a lease, so §9.4 would have recovered it and
dispatched a second worker onto finished work.

It is a **terminal success**, closed as completed, and it is the only lifecycle
state a component may reach on evidence of its own dispatch's outcome. §9.16
states the preconditions; they are exhaustive, and every one of them is checked
against durable GitHub state at the moment of the write.

**`story:completed` is not `story:cancelled`.** The distinction is the point of
having both. Completed says the work succeeded; cancelled says it was stopped.
Overloading cancellation to mean success would put a factory's own successes and
its abandonments in one bucket, and no later reader could tell them apart.

**`story:cancelled` — deliberately stopped.** Added by #77, narrowed by #104. Some work legitimately stops: a story overtaken by events, a spike whose answer was "don't build it", work the CTO calls off. Before this state existed such stories had to masquerade as `story:merged` (nothing merged) or `story:blocked:poison` (nothing failed), and #64 sat closed-as-not-planned while still labelled `story:claimed` — honest about the outcome but unnamed by the contract.

A cancellation is a **human decision** and stays one, recorded as a comment on the story saying what was decided and why. No component cancels a story: the dispatcher only recognises the state, treats it as terminal, and stops offering it. Because a cancelled story is not `story:claimed`, it releases its WIP slot immediately.

Work that *succeeded* with no deliverable is `story:completed`, not this. #77 originally described cancellation as "finished with no deliverable", which conflated the two; #104 separates them, because a factory that records its successes as cancellations cannot report on itself.

A closed issue is never reopened by a component; only a human reopens, which is itself a decision.

**A poisoned story is open until the rescue cap is spent.** Added by #114 after #110 found the dispatcher closing it on the *first* poisoning. Two things follow from that closure and both are wrong. The §4.3.6 rescue becomes unreachable — it requires the story to return to `story:ready`, and reaching a closed issue means reopening it, which the paragraph above reserves to a human and forbids to every component. And the story disappears from the human queue, which enumerates open issues (§9.11), so the one artifact most in need of a person's attention is the one nothing mentions again. Poison routes work *to* a human; it does not file it away. Only the third poisoning, with §4.3.7's two rescues spent, closes the issue as *not planned* — there, closure is correct, because the story is going back to planning rather than waiting for anyone.

### 9.4 Claim lease and expiry

`story:claimed` is a lease, not a permanent assignment.

* **Lease duration:** `CLAIM_LEASE = 60 minutes`, measured from the `labeled` timeline event that applied `story:claimed`. The timeline is durable and needs no cache.
* **Invariant:** `CLAIM_LEASE` **must exceed** the maximum wall-clock any worker invocation can consume under its `### Spend cap`. If a spend cap ever allows a longer run, the lease rises with it — otherwise expiry races live workers by construction.
* **Expiry condition:** the lease has elapsed **and** no pull request links to the story per §9.5.
* **Recovery:** transition `story:claimed → story:ready` and **decrement `Attempt` by 1**, restoring its pre-dispatch value. A worker that died without producing a PR consumed no attempt — this is §4.3.4 (infrastructure failures do not count) applied to the death case. Without the decrement, expiry silently burns the retry budget and a story can poison on infrastructure alone.
* **Recovery is a transition** and obeys §9.2, so a second dispatcher pass observing `story:ready` simply finds nothing to expire.

#### 9.4.1 Recovery budget — the bound

`RECOVERY_MAX = 2` expiry recoveries per story.

**Why a bound is required.** Recovery restores the attempt that dispatch consumed, so without a cap the pair (dispatch, expiry) cycles forever: `ready Attempt 0 → claimed Attempt 1 → expiry → ready Attempt 0 → …`. The counter never advances, the poison threshold is never reached, and a worker is woken every lease period indefinitely. `architecture-v2.1.md` §4 states that any loop without a bound is an architecture bug; this was one, and story #64 was a live instance of it (#65).

**Why the policy is deliberately dumb.** Durable evidence **cannot** distinguish a worker that died before starting from one that ran correctly and produced no pull request — both leave an expired claim and no PR. Rather than infer which occurred, the dispatcher recovers a fixed number of times and then asks a human.

*Amended by #104.* That statement is about what an **expired claim** proves, and it remains true: by the time a lease elapses, the two cases are indistinguishable. It is not a claim that the difference can never be known. §9.16 describes the one case where it is: a worker whose launch the factory itself waited on, which returned a definite success and left durable proof of the assignment. That evidence exists at the moment the worker returns, long before any lease expires, and §9.16 spends it then or not at all. A story that reaches expiry is a story with no such proof, and this paragraph governs it unchanged.

**The rule.** Recoveries are counted from the issue timeline: a `story:claimed → story:ready` transition with no `story:in-review` between them is an expiry recovery, while a findings-driven return passes through review and is not counted. Nothing is stored — GitHub is the source of record and no local counter may contradict it (§9.12). When the count reaches `RECOVERY_MAX`, the next expiry transitions `story:claimed → story:blocked:poison` with reason `RECOVERY_BUDGET_EXHAUSTED` instead of recovering. `Attempt` is left untouched: the question for the human is why no PR ever appeared, not how many attempts remain.

**The bound, stated as a number.** A story can be dispatched through the expiry path at most **`RECOVERY_MAX + 1` = 3 times** before reaching a terminal or human-queue state. The human's options are then explicit: rescue it per §4.3.6 if the failure was infrastructural, or cancel it per §9.3 if the work should stop. Reaching this point means no proof of success was ever produced, so `story:completed` is not among the options — §9.16 is not a disposition a human reaches for after the fact.

**Duplicate-worker rule.** The claim is the mutex: only `story:ready` is dispatchable, so a claimed story is never dispatched twice. The residual race is a live-but-slow worker whose lease expires and whose story is re-dispatched. Both workers must therefore treat the story state as authoritative at the moment they act: a worker **must** re-read the story before opening its PR and **must abort without opening one** if the story is no longer `story:claimed`, or if it is claimed under a later lease than the one it was dispatched with. The late worker's branch is abandoned; nothing merges.

#### 9.4.2 Capacity admission and worker-start evidence

Delivery admission is ordered: evaluate eligibility without mutation, reserve one
healthy Capacity Pool route, re-read and claim the Story, then hand the opaque
reservation ID to the worker. No reservation means no claim and no `Attempt`
increment. Dispatcher and poller never receive provider or model identity.

The reservation is bound to repository, Story, and next Attempt; it expires and
is single-use. Claim failure releases it. Before model work, the delivery worker
atomically consumes it and writes a `factory-worker-start:v1` comment binding
repository, Story, claimed-state version, reservation ID, invocation ID, and
start time. An expired, mismatched, unavailable, or reused reservation cannot
launch.

If durable capacity state proves the reservation was never consumed, a definite
launch failure returns the Story to `story:ready` and restores the previous
`Attempt`. Once the reservation is consumed and worker-start evidence exists,
normal attempt, lease, recovery, and poison rules apply. Ambiguous state never
authorizes a refund or a second worker.

#### 9.4.3 Invocation outcomes and usage receipts

Every admitted provider attempt produces exactly one `capacity.route.attempt`
record. It has a unique `invocation_id`, the reservation ID and durable
worker-start invocation ID when Delivery supplied them, and exactly one terminal
outcome: `launch-failed`, `started-mid-work-failed`, `limit-stopped`,
`validation-failed`, or `completed`. A route rejected before any provider
attempt has the final outcome `not-admitted` and no fabricated attempt record.
Provider-specific reasons remain separate routing data; they cannot replace or
collapse the stage outcome.

Each attempt record carries a usage receipt. Provider-reported usage is copied
without pricing it. Exact dollar cost is present only when the provider reports
that value. Otherwise the receipt names why dollar cost is unavailable and
records the reconciled reservation charge as normalized capacity units. The
units are an admission-accounting measure, not dollars. A retry receives its
own invocation ID, outcome, and receipt; no receipt may be reused across
attempts.

### 9.5 PR ↔ Story linkage

A pull request produced by the factory **must** carry, in its body, exactly one line:

```
Story: #<number>
```

Validation, all fail-closed:

* absent, or more than one such line → invalid; the PR is not gate-eligible
* the referenced issue does not carry `type:story` → invalid
* the referenced story is not in `story:claimed`, or its `### Project` does not match → invalid

The body line is canonical. Branch names are conventional only (`story/<number>-<slug>` is suggested) and are never parsed for authority. GitHub's own "closes #N" linkage is not used for this purpose: it mutates issue state on merge, which would bypass §9.2.

### 9.6 Scope dialect

`### Scope` becomes machine-readable. From `SCHEMA_VERSION 2.0.0` a story's Scope section is **one pattern per line, no bullets, no blank lines, no commentary**:

```
src/reports/**
tests/reports/**
```

Matching rules, evaluated against repository-relative POSIX paths:

* `*` matches any characters **except** `/`
* `**` matches any number of path segments, including none
* `?` matches a single character except `/`
* everything else is literal; patterns are case-sensitive; no negation, no braces, no character classes in `2.0.0`
* a pattern with no wildcard matches exactly that one path

A diff satisfies scope when **every** changed path matches **at least one** pattern. Renames are two paths and both must match. An empty Scope section matches nothing, so an empty-scope story can never pass the gate — this is deliberate.

*Phase 1 compatibility:* stories #10–#12 wrote `- synthetic/**` with a bullet, valid under `1.x` and left untouched. A `2.0.0` consumer encountering a leading `- ` must reject the story as malformed rather than silently stripping it, per fail-closed.

### 9.7 Agent-ID — attribution, not authentication

Every factory-produced artifact carries a stable logical agent identity.

**Format.** `Agent-ID: <id>` where `<id>` matches `^[a-z0-9][a-z0-9-]{2,31}$`. Stable per logical role, not per invocation — the same worker role keeps its id across runs. The delivery worker's id is `claude-delivery`.

**Where it must appear:**

| Artifact | Placement |
|---|---|
| commit | an `Agent-ID: <id>` trailer in the message's **trailer block** — the final paragraph, with no blank line separating it from the other trailers, so `git log --format='%(trailers)'` parses it. A blank line above it makes it ordinary body text and it will not be read. |
| pull request | an `Agent-ID: <id>` line in the body, beside the `Story:` line |
| touch-log line | the `actor` field, when the actor is an agent rather than a human handle |
| issue/PR routing (optional) | an `agent:<id>` label, for reporting and queue filtering |

**Validation.** Malformed or missing where required → the artifact is flagged and routed to the human queue. Multiple conflicting `Agent-ID` values in one artifact → invalid.

**Its limits, stated plainly.** The factory operates today under a **single GitHub credential** shared by the human and every agent (CTO decision, #27). Therefore:

* `Agent-ID` is **attribution, not authentication.** Anything holding the credential can write any value.
* `agent:*` labels, bell comments, and every other repository artifact are likewise **forgeable** by that credential — including artifacts that claim to record a human decision.
* Repository data **cannot** distinguish "the CTO decided this" from "an agent wrote text saying the CTO decided this." No component may claim otherwise, and no security control may rest on that distinction.

What `Agent-ID` does buy: legible audit reconstruction, per-agent metrics, and queue routing — real value for reporting and debugging, and **zero** resistance to a dishonest or malfunctioning holder of the credential. §9.8 and §9.14 are written to need no stronger assumption than this.

### 9.8 Human authorization artifact

A human decision is durable only as **an issue authored by the CTO account** or **a lifecycle label set by a collaborator** (§9.9). Both are recorded per §5; the touch log measures them per §6.

**Recognition rule.** A component treats as authorization only:

1. the **body of an issue** whose author is the CTO account — not a comment, since comments are where an agent naturally writes; and
2. **collaborator-set structured state** — a lifecycle label, per §9.9.

**Limitation, restated because it matters here most.** Under one shared credential this is a *convention*, not an enforcement: an agent holding the credential could file an issue as the CTO. It is chosen because it is the narrowest channel that an agent has no routine reason to use, which makes a violation conspicuous in the timeline rather than impossible. **Do not describe it as a security boundary.** The boundary that does hold — the one no repository artifact can forge — is §9.14.

**Routine work needs no human.** Authorization for ordinary delivery is *already* carried by the story's lifecycle label. A human is required at the bells named in §5 and §6, and nowhere else. Waking a worker is not a bell.

### 9.9 Public-repository trust boundary

The repository is public, so **anyone can open issues and post comments.** This is now an input-validation problem, not a governance detail.

| Input | Trust | May it authorize execution? |
|---|---|---|
| Lifecycle label set by a collaborator | trusted | **yes** — this is the only authorizing channel |
| Issue body, author = CTO account | trusted per §9.8 | yes, for decisions |
| Issue/PR free text from a non-collaborator | **untrusted** | **never** |
| Issue creation as an event | **untrusted** | **never** |
| Comment text, any author | untrusted for routing | never |

Rules:

* Dispatch keys on **state a collaborator can set**, never on issue creation and never on parsed free text.
* Before treating any issue as factory state, verify its `author_association` is `OWNER` or `COLLABORATOR`; otherwise ignore it for routing (a human may still triage it).
* Untrusted text may be *read* — for context, for a human's queue — but must never select a code path, name a file, or supply a parameter.

### 9.10 WIP limit and claim selection

```
WIP_LIMIT = 2      concurrent stories in story:claimed, repository-wide
```

Chosen to match `architecture-v2.1.md` §8's "2–3 concurrent workers" at customer-zero scale; raising it is a contract change, not a runtime tweak.

**Selection, fully deterministic.** Eligible stories are those that are open, `type:story`, `story:ready`, and whose every `### Depends-on` reference reached a **terminal success** — `story:merged` or `story:completed` (§9.16). Both mean the depended-on work is done; only the delivery path differs, and a dependency that cares which one it was is a dependency the `### Depends-on` field cannot express. Order them by:

1. parent project issue number, ascending;
2. then story issue number, ascending.

Dispatch from the head of that order until `WIP_LIMIT` claimed stories exist. The ordering is total — issue numbers are unique — so there are no ties and no randomness.

**Idempotency.** A dispatch is keyed on `(story issue number, id of the timeline event that produced story:ready)`. A duplicate delivery of the same event re-derives the same decision and finds the story already `story:claimed`, so it no-ops. The state write is the duplicate suppressor; any cursor or cache is an optimisation whose loss costs latency, never correctness (`architecture-v2.1.md` §4).

### 9.11 Live routes in Phase 2

Executable from Increment 2 onward:

| Transition | Action |
|---|---|
| `story:ready` → `story:claimed` | dispatch a worker, increment `Attempt` (§4.3.2) |
| `story:ready` → `story:blocked:poison` | at the §4.3.5 threshold: do not dispatch, notify |
| `story:claimed` → `story:ready` | claim expiry (§9.4) |
| `story:claimed` → `story:completed` | the runtime completion path (§9.16) |
| `story:claimed` → `story:in-review` | a §9.5-linked pull request is open (#114) |
| `story:in-review` → `story:merged` | that pull request merged; close as completed (#114) |
| `project:awaiting-ready` → `project:active` | consume the §5.1 plan approval |
| `project:awaiting-acceptance` → `project:accepted` / `project:active` | consume the §5.3 acceptance |
| `project:awaiting-ready`, `project:awaiting-acceptance`, `story:blocked:poison`, `story:blocked:scope` | notify the human queue; take no other action |

**The two review routes, and why they may be mechanical.** Both were
documentation-only until #114, the second explicitly "gated on §9.13". §9.13 is
complete — `merge-gate` is required and `required_approving_review_count` is 0 —
so the condition has been met. They are owned by the runtime, not by the worker
§4.2 names: a worker that has exited cannot write the transition its own pull
request caused, and a human writing it is the relay Phase 2 exists to delete.

What makes them safe to automate is *where the evidence comes from*. The only
input is whether a pull request carrying the canonical `Story: #N` line is open
or merged, and a merge is the outcome of the required gate — a verdict the
delivering credential cannot fabricate (§9.14). That is the whole difference
from `story:completed`, which needs §9.16's much narrower proof precisely
because nothing outside the factory has ruled on the work. Ambiguity — two
linked pull requests, a duplicate `Story:` line, a pull request closed without
merging — writes nothing and names its reason; the last of those is a human's
decision, because work delivered and then rejected is not a state the factory
has a rule for.

**The human queue is enumerated, not announced once.** §9.11's last row is a
standing obligation, not an event: every artifact in one of those states is
surfaced on *every* poll, with its link and the action required, for as long as
it waits. A notifier that reports only transitions goes quiet about a problem
precisely because the problem is old — which is what happened to #55, #61 and
#66, each of which sat at a bell until somebody happened to look.

**Documentation-only** until their phase arrives: `project:ready-for-planning → project:planning` (planning agent, Phase 3); `story:blocked → story:ready` (sequencer, Phase 3); `project:active → project:awaiting-acceptance` (sequencer, Phase 3); review *findings* on an open pull request, `story:in-review → story:ready` (review skill, Phase 4).

The §9.15 replay reports every transition it cannot route live under one of these headings rather than dropping it, so this list is checkable against the code instead of maintained by hand.

**No silent drops.** A transition with no live route is logged and surfaced, never discarded — an unrouted transition is a contract gap, and silence would hide it.

### 9.12 Discovery implies dispatch

Discovery of authorized executable work **starts that work**. No human types a command to wake a worker.

* The dispatcher polls (60s cron), finds transitions, and acts on the live routes in §9.11 without further authorization.
* Authorization is **intrinsic to the artifact**: a story carrying `story:ready`, set by a collaborator, *is* the authorization. Nothing else — not issue creation, not a comment, not free text — grants it (§9.9).
* Human involvement remains at the §5/§6 bells. Those pause the affected artifact only; the dispatcher keeps servicing everything not behind them.

A human action that merely conveys "begin" is a **relay touch** — the class `architecture-v2.1.md` §7 requires to trend to zero. This clause exists to delete it.

### 9.13 Merge-gate migration contract

`main` must never be without either the current approval rule or a proven required check. The ordering is mandatory:

1. **Now.** `main-protection` keeps `required_approving_review_count: 1`, `bypass_actors: []`. Verified blocking in #24.
2. **Increment 2.** Build the deterministic gate as an **ordinary, non-required** CI check. Prove it red on each violation class independently and green on a compliant PR. `main-protection` is untouched throughout.
3. **Separately authorized task, one atomic ruleset edit.** Add the proven check as **required** *and* set `required_approving_review_count: 0` in the same operation.
4. Never step 3 before step 2 is proven, never as two edits, and never by adding `bypass_actors` — bypass would hand the exemption to the same credential the agent holds, making the gate decorative for exactly the actor it constrains.

**Prerequisites before step 3 (added by #39, delivered in #40).** Making `merge-gate` required while it also failed on gate changes would freeze the gate at its migration-time version, with no human action able to land a fix — including a fix for a gate bug. Both must be in place first:

* the verdict is computed by the **trusted `main` copy** of the gate logic, so a gate-logic PR can pass on its merits (§9.14);
* enforcement-surface detection lives on the separate advisory check `merge-gate-surface`, which is **never** made required.

**Break-glass, for a totally broken required gate.** If `main`'s gate wrongly fails everything, it also blocks its own fix. The narrowest recovery: an admin removes `merge-gate` from the ruleset's required checks, lands the fix, and restores it immediately. This is a ruleset edit — timestamped in ruleset history, therefore auditable — and it never adds a bypass actor and never weakens the gate's own rules. It requires a post-hoc `hazard-ack` recording what was landed while the check was off. It is a procedure of last resort, not an option: the reflex "turn the gate off" is exactly what a governance system must not make easy.

**Why step 3 is needed at all.** With one identity, an agent PR is authored by the only account with write access, and GitHub refuses an approving review from a PR's own author. Agent PRs are therefore not merely unreviewed — they are **unmergeable** — until the approval requirement is replaced by the deterministic check (#27). Until step 3, a human lands factory PRs manually; that is an accepted interim cost, not the target state.

### 9.14 Gate trust inputs

Under a shared credential the gate must derive its verdict **only** from inputs the agent's token cannot fabricate.

**Permitted:**

* the **diff itself** — changed paths, additions and deletions
* the **`### Scope` contract** of the linked story (§9.5, §9.6), read from the story issue
* **check outputs produced by CI**, not by an actor — test results computed in the workflow run
* the **trusted gate copy** — the verdict is computed by the version of the gate already on `main`, never by the version inside the pull request, so a proposed change cannot weaken the rules it is judged by (#39, #40)
* **repository configuration** — ruleset state, CODEOWNERS

**Prohibited as trust anchors** (all forgeable by the credential):

* `Agent-ID` values, `agent:*` labels, any label the agent can set
* comments claiming review, approval, or human authorization
* assertions in the PR body about what was done or verified

`Agent-ID` and labels may be used for **routing and reporting** inside the gate — deciding what to check — but never as the evidence that a check passed.

**Correction (#39, #40): the enforcement surface is not uniformly protectable.** An earlier version of this section listed "the workflow boundary" as a trust input, on the grounds that the agent's credential cannot modify `.github/workflows/**`. That was a circumstance, not a property of the design, and it does not hold:

| Class | Paths | Protection |
|---|---|---|
| Gate **logic** | `factory/gates/**` | **Mechanical.** The trusted `main` copy computes the verdict, so a change cannot judge itself. Reported as `neutral` on `merge-gate-surface`; it does **not** block. |
| Gate **runner** | `.github/workflows/merge-gate.yml` | **None mechanical.** For same-repo `pull_request` events GitHub executes the workflow file from the PR head, so a PR that rewrites the runner runs its own rewrite. No check can protect the file that defines the check. |

The runner is covered by human review, a `hazard-ack` bell naming the diff (§5.4), and the audit trail. Under the shared credential that is a **convention, not an enforcement** — the ack is forgeable, so its evidential value is asymmetric: a *missing* ack on a landed runner change is unforgeable evidence that no one reviewed it, while a *present* ack proves only that the text exists. Design for the missing case.

**`merge-gate-surface` must never become a required check.** A required check that fails on gate changes, combined with `bypass_actors: []`, makes the gate permanently unmodifiable — no human action turns it green. That is strictly worse than the problem the required gate solves.

**Internal errors fail closed.** A gate that cannot complete an evaluation reports `INTERNAL_ERROR` with a readable reason and a non-zero exit. It never passes on an error, and never fails as an opaque crash.

*Consequence, recorded as a correction:* the Phase 2 readiness review (#20) proposed a review-approval artifact bound to the head SHA as the gate's trust anchor. Under the single-identity decision that artifact is forgeable, so it is **withdrawn**. The gate rests on diff, scope, CI-computed results, and the workflow boundary instead — a simplification the identity decision makes available.

### 9.15 Replay acceptance test

Before the dispatcher is permitted to write anything, it must pass a replay of Phase 1's recorded history:

* **Input:** the `labeled`/`unlabeled` timeline events of issues #1, #10, #11, and #12 — durable, already in GitHub, and the product of a walk verified under #19.
* **Requirement:** every transition routes **exactly once** — no duplicates, no drops. Transitions whose routes are documentation-only (§9.11) must be surfaced as unrouted, not silently ignored.
* **Restart safety:** replay from an empty cursor must produce the identical routing decisions, proving no authoritative state lives outside GitHub.
* **Read-only:** the replay asserts decisions the dispatcher *would* make; it must not write to any issue.

The events include the retry sequence, the poison threshold, the rescue, and the scope detour, so the replay exercises the exception paths and not merely the happy one.

### 9.16 Completion — proving a bounded assignment succeeded

Added by #104. The one path by which a component may reach a terminal state on
the strength of its own dispatch's outcome, and it is deliberately the narrowest
rule in this document.

**Applies to:** a `story:claimed` story whose worker the factory *launched and
waited on*, at the moment that launch returns. Nowhere else. This is not a
reconciliation pass, not a sweep over claimed stories, and not a disposition a
human or a later pass may reach for — a story that has gone quiet is §9.4's, and
a story that reaches expiry is §9.4.1's.

**All of the following must hold**, each read from durable GitHub state at
decision time:

1. the launch reported a **definite success** — an ambiguous outcome means the
   worker may still be running, and closing a story out from under a live worker
   is strictly worse than waiting for the lease
2. the story still carries exactly `story:claimed`
3. **no pull request links to the story** under §9.5 — a worker that produced a
   deliverable belongs to review and the merge gate, and this path must never
   touch it
4. the timeline carries a `story:claimed` `labeled` event, which fixes the
   instant the current lease began — the *latest* such event, since a recovered
   story has several and proof from an earlier lease says nothing about this
   worker
5. durable evidence of the assignment having been carried out exists, created at
   or after that instant

**Effect:** `story:claimed → story:completed`, closed as completed (§9.3), with
`Attempt` unchanged, written per §9.2 — re-read, verify the `from` state, then
one PATCH carrying the complete final label set and the closure.

**A reason comment is required**, naming the worker, the verdict and the
evidence, so the conclusion can be checked against GitHub rather than taken on
trust. It is written **before** the transition: a failure between the two writes
must leave a claimed story carrying an explanation — visible, and recoverable by
§9.4 — rather than a terminal story with no recorded reason, which no component
may reopen (§9.3).

**Anything short of all five leaves the story exactly as it is**, with a named
reason, for §9.4 to resolve. The asymmetry is deliberate and is the whole safety
argument: a wrong refusal costs one lease period and a recovery; a wrong
completion closes a story whose work never happened, and closure is the one
thing this contract does not let a component undo.

**The evidence must belong to the assignment.** What counts as proof is defined
by whatever issues the assignment, so that the two cannot drift apart — a
completion path that accepts weaker evidence than the worker was asked to
produce is a path that closes stories on something else's output. Equally,
precondition 3 is what keeps the rule honest as assignments grow: it is only
ever true that "no deliverable was required" while the assignment forbids
producing one, and that coupling must be asserted mechanically, not remembered.

### 9.17 Withdrawn merge-gate checks

Added by #114. Three Phase 2 deliverables are **withdrawn**, not deferred, and
the difference matters: deferred means "later", withdrawn means "this design
cannot do what it claimed, and pretending otherwise is worse than the gap".

| Withdrawn | Was | Why |
|---|---|---|
| Gate check **(a)** | review approval bound to the exact head SHA | Already withdrawn by §9.14 under the single-identity decision (#27); recorded here so all three sit together |
| Gate check **(d)** | test files not deleted or weakened without a distinct `test-change` label | The label is the release valve, and §9.14 prohibits any label as a trust anchor because the agent's credential can set it. A check whose red can be turned green by the actor it constrains is not a check |
| Gate check **(e)** | hazard paths not touched without a human ack label from an allowed identity | Same defect, plus a second: with one identity there is no "allowed identity" distinct from the agent's |
| **CODEOWNERS** | code-owner review over dependency manifests, CI/workflow files, migrations, secrets config, `factory/spec/**`, `factory/gates/**` | With one credential the owner and the agent are the same account, so a required code-owner review is a review the agent gives itself |

**What is not withdrawn.** Hazard paths remain enumerated, stories still
pre-flag them in `### Hazard`, the `hazard-ack` bell is still rung and still
logged, and the advisory `merge-gate-surface` check still classifies every diff
against the enforcement surface. What is withdrawn is the claim that any of this
is **mechanically enforced**. §9.14 already stated the honest version for the
gate runner and it generalises: *the ack is forgeable, so its evidential value
is asymmetric — a missing ack on a landed hazard change is unforgeable evidence
that no one reviewed it, while a present ack proves only that the text exists.
Design for the missing case.*

**Why withdrawal rather than a weaker implementation.** A gate that reports
`hazard-ack: present` tells a reader the hazard was reviewed. Under a shared
credential it tells them only that a string exists. Shipping it would put a
green light on the one class of change least able to justify one, and every
later reader would have to know the caveat to avoid being misled. An absent
check misleads nobody.

**What would bring them back.** A second identity — a worker credential distinct
from the human's — restores the distinction all three rest on, at which point
(d), (e) and CODEOWNERS become implementable as written. That is a Phase 4+
question (#26, #27); nothing here forecloses it, and the labels stay defined in
§2 so the data is not lost in the meantime.

**What replaces them in Phase 2.** Nothing pretends to. The gate enforces what
it can derive from inputs the credential cannot fabricate — the diff, the story's
`### Scope`, and CI-computed test results across every factory suite (#113) —
and `factory/acceptance/` proves the rest of Phase 2 behaves as specified. The
enforcement surface is covered by human review and the audit trail, which is a
convention, and is now described as one.
