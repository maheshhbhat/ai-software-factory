# State Schema — Phase 1

> Authoritative definition of GitHub as system of record for the factory.
> This document decides label names, issue fields, and legal transitions.
> All state lives in GitHub (issues + labels + PRs + issue comments). Any cache is derivable and disposable.
> No component other than GitHub is authoritative. Ref: `architecture-v2.1.md` §1, §4; `implementation-plan-v1.md` Phase 1.
>
> **`SCHEMA_VERSION = 2.0.0`** — §1–§8 are the Phase 1 state model, verified and unchanged in substance. §9 freezes the executable contracts Phase 2 implements (§9.1 defines versioning and compatibility).

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
| `project:active` | `project:awaiting-acceptance` | human (manual in P1) / sequencer | every story reached `story:merged` | no | human queue |
| `project:awaiting-acceptance` | `project:accepted` | human | acceptance comment records **pass for every criterion** (§5.3) | **yes** `acceptance` / `decision` | terminal |
| `project:awaiting-acceptance` | `project:active` | human | acceptance comment records **any criterion failed** (§5.3) | **yes** `acceptance` / `rescue` or `decision` | new story or re-planning spawned; returns to `awaiting-acceptance` when merged |
| `project:accepted` | — | — | terminal — issue **closed as completed** (§9.3) | — | — |

Both former self-loops (`awaiting-ready → awaiting-ready`, `awaiting-acceptance → awaiting-acceptance`) are replaced by real edges above: a label edit that ends on the same label emits no transition and therefore cannot be routed (`architecture-v2.1.md` §4, "route on transitions, not states").

### 4.2 Story transitions

| From | To | Actor | Cause | Notes |
|---|---|---|---|---|
| `story:blocked` | `story:ready` | human (manual in P1) / sequencer | dependencies satisfied, WIP allows | explicit `depends-on` already declared |
| `story:ready` | `story:claimed` | human (manual in P1) / dispatcher assigns | worker attempt dispatched | workers never self-claim; **`Attempt` increments here** (§4.3) |
| `story:claimed` | `story:in-review` | worker (human in P1) | PR opened referencing story | PR links to the story per §9.5 |
| `story:claimed` | `story:ready` | dispatcher | **claim lease expired** with no linked PR | **`Attempt` decrements by 1** (restores the pre-dispatch value); see §9.4 |
| `story:in-review` | `story:merged` | merge gate (human in P1) | PR merged | terminal success; the issue is **closed as completed** (§9.3) |
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

`poison-rescue` (§4.3.6), `scope-decision` (§4.2), `hazard-ack`, `cutover-approval`, and `sampling` are recorded as comments on the affected story or PR, each stating the decision and the actor, and each accompanied by exactly one touch-log line.

---

## 6. Touch log (substrate for measurement)

Every human bell is logged to `factory/touchlog/touchlog.jsonl` via `factory/touchlog/append.py`. Classifications are exactly `decision | audit | rescue | relay`. Bell types: `plan-approval`, `hazard-ack`, `poison-rescue`, `scope-decision`, `cutover-approval`, `acceptance`, `sampling`. This list is canonical and extends the six named in `implementation-plan-v1.md` core rule 6: `scope-decision` was added because §4.2 requires a bell for the `story:blocked:scope` decision and no existing type covered it. See `factory/touchlog/README.md` for the JSONL schema and helper usage. Only `relay` should trend to zero; other touches are expected.

The touch log measures bells; §5 records what was decided. A bell with a touch-log line and no comment is an incomplete record, and so is a comment with no touch-log line.

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
SCHEMA_VERSION = 2.0.0
```

Semantics: **major** changes break a component that has not been updated (a lifecycle label renamed, a field's meaning changed); **minor** adds contract that older components can ignore; **patch** is editorial. Phase 1 was authored under an implicit `1.x`.

Every Phase 2 component pins the major version it implements and, on encountering state written under a different major version, **halts and routes to the human queue** — it must never guess. Fail-closed is the rule wherever §9 is silent.

Version is asserted, not stamped on every issue: issue bodies are rendered forms and carry no room for a marker. The dispatcher records the pinned version in its own run log, and any artifact it writes that has a free-text field carries `schema-version: 2.0.0`.

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
| `story:blocked:poison` after the §4.3.7 rescue cap | closed | not planned |
| `project:accepted` | closed | completed |
| every other state | open | — |

Closure is part of the transition, written in the same operation wherever the API allows. The dispatcher's work queue is therefore **open issues carrying a `type:story` label**, which makes "is this story still live?" a single cheap query rather than a label scan.

A closed issue is never reopened by a component; only a human reopens, which is itself a decision.

### 9.4 Claim lease and expiry

`story:claimed` is a lease, not a permanent assignment.

* **Lease duration:** `CLAIM_LEASE = 60 minutes`, measured from the `labeled` timeline event that applied `story:claimed`. The timeline is durable and needs no cache.
* **Invariant:** `CLAIM_LEASE` **must exceed** the maximum wall-clock any worker invocation can consume under its `### Spend cap`. If a spend cap ever allows a longer run, the lease rises with it — otherwise expiry races live workers by construction.
* **Expiry condition:** the lease has elapsed **and** no pull request links to the story per §9.5.
* **Recovery:** transition `story:claimed → story:ready` and **decrement `Attempt` by 1**, restoring its pre-dispatch value. A worker that died without producing a PR consumed no attempt — this is §4.3.4 (infrastructure failures do not count) applied to the death case. Without the decrement, expiry silently burns the retry budget and a story can poison on infrastructure alone.
* **Recovery is a transition** and obeys §9.2, so a second dispatcher pass observing `story:ready` simply finds nothing to expire.

**Duplicate-worker rule.** The claim is the mutex: only `story:ready` is dispatchable, so a claimed story is never dispatched twice. The residual race is a live-but-slow worker whose lease expires and whose story is re-dispatched. Both workers must therefore treat the story state as authoritative at the moment they act: a worker **must** re-read the story before opening its PR and **must abort without opening one** if the story is no longer `story:claimed`, or if it is claimed under a later lease than the one it was dispatched with. The late worker's branch is abandoned; nothing merges.

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

**Selection, fully deterministic.** Eligible stories are those that are open, `type:story`, `story:ready`, and whose every `### Depends-on` reference is `story:merged`. Order them by:

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
| `project:awaiting-ready`, `project:awaiting-acceptance`, `story:blocked:poison`, `story:blocked:scope` | notify the human queue; take no other action |

**Documentation-only** until their phase arrives: `project:ready-for-planning → project:planning` (planning agent, Phase 3); PR-open → review (review skill, Phase 4); `story:in-review → story:merged` (merge gate, Increment 2+, gated on §9.13).

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

**Why step 3 is needed at all.** With one identity, an agent PR is authored by the only account with write access, and GitHub refuses an approving review from a PR's own author. Agent PRs are therefore not merely unreviewed — they are **unmergeable** — until the approval requirement is replaced by the deterministic check (#27). Until step 3, a human lands factory PRs manually; that is an accepted interim cost, not the target state.

### 9.14 Gate trust inputs

Under a shared credential the gate must derive its verdict **only** from inputs the agent's token cannot fabricate.

**Permitted:**

* the **diff itself** — changed paths, additions and deletions
* the **`### Scope` contract** of the linked story (§9.5, §9.6), read from the story issue
* **check outputs produced by CI**, not by an actor — test results computed in the workflow run
* the **workflow boundary** — that the agent's credential cannot modify `.github/workflows/**`, so the gate's own definition is outside the agent's reach
* **repository configuration** — ruleset state, CODEOWNERS

**Prohibited as trust anchors** (all forgeable by the credential):

* `Agent-ID` values, `agent:*` labels, any label the agent can set
* comments claiming review, approval, or human authorization
* assertions in the PR body about what was done or verified

`Agent-ID` and labels may be used for **routing and reporting** inside the gate — deciding what to check — but never as the evidence that a check passed.

*Consequence, recorded as a correction:* the Phase 2 readiness review (#20) proposed a review-approval artifact bound to the head SHA as the gate's trust anchor. Under the single-identity decision that artifact is forgeable, so it is **withdrawn**. The gate rests on diff, scope, CI-computed results, and the workflow boundary instead — a simplification the identity decision makes available.

### 9.15 Replay acceptance test

Before the dispatcher is permitted to write anything, it must pass a replay of Phase 1's recorded history:

* **Input:** the `labeled`/`unlabeled` timeline events of issues #1, #10, #11, and #12 — durable, already in GitHub, and the product of a walk verified under #19.
* **Requirement:** every transition routes **exactly once** — no duplicates, no drops. Transitions whose routes are documentation-only (§9.11) must be surfaced as unrouted, not silently ignored.
* **Restart safety:** replay from an empty cursor must produce the identical routing decisions, proving no authoritative state lives outside GitHub.
* **Read-only:** the replay asserts decisions the dispatcher *would* make; it must not write to any issue.

The events include the retry sequence, the poison threshold, the rescue, and the scope detour, so the replay exercises the exception paths and not merely the happy one.
