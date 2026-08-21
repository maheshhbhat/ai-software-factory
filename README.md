# AI Software Factory

An experiment in building **deterministic delivery rails around AI software agents**, with GitHub as the durable source of truth.

The thesis: AI agents can write code, but the hard part is the machinery around them — knowing what to build next, checking the work with something the worker cannot influence, and keeping humans at the decisions that actually need judgment instead of relaying status between steps. This repository builds that machinery first, and measures whether it holds.

## Current status

**Phase 1 is verified. Phase 2 is built and running; its acceptance is in progress.**

The rails exist and they run themselves. A story labelled `story:ready` by a collaborator is claimed by the dispatcher, launched through a real CLI engine, verified against durable evidence, and closed — with no human touching a label. Story #122 walked the whole delivery path in **two minutes thirty-one seconds**:

```
13:09:29  story:ready   -> story:claimed      dispatcher.py
13:11:20  story:claimed -> story:in-review    review_link.py   (PR opened)
13:11:52  story:in-review -> story:merged     review_link.py   (PR merged, closed)
```

| Phase | What it delivers | State |
|---|---|---|
| 1 — State schema and touch log | Issue types, label state machines, legal transitions, human-touch log | **VERIFIED** (2026-08-19) |
| 2 — Deterministic rails | Merge gate, dispatcher, runtime, worker contract, acceptance suite | **BUILT — in acceptance** |
| 3 — Planning agent | Turns direction into projects and dependency-ordered stories | Not started |
| 4 — Workers and review | Story implementation and independent review on PR-open | Not started |
| 5 — Test ladder and KPIs | Three rungs of increasing realism, measured | Not started |

### What is actually enforced today

`merge-gate` is a **required** status check on `main` with `bypass_actors: []` and
`required_approving_review_count: 0`. Its verdict is computed by the copy of the
gate already on `main`, never by the copy inside the pull request, so a change to
the gate cannot weaken the rules it is judged by. It runs 448 tests across four
suites plus 16 acceptance scenarios on every pull request.

### What is deliberately *not* enforced, and why

Three planned checks were **withdrawn** rather than shipped weak (`state-schema.md`
§9.17): test-deletion gating, hazard-path gating, and CODEOWNERS. All three rest on
a label or a review that the agent's own credential can produce, and a check whose
red can be turned green by the actor it constrains is not a check. Hazard paths are
still enumerated, still pre-flagged, and still surfaced by an advisory check — what
is withdrawn is the claim that any of it is mechanically enforced. A second identity
would restore all three.

There is also **no independent review of code** in this repository yet. That is
Phase 4. Until then the gate checks *form* — scope, links, tests green — and
nothing checks *correctness* except the tests themselves.

### How it is tested

| Layer | | Coverage |
|---|---|---|
| Unit | 383 tests | 80.5% |
| Integration | 46 tests | 53.1% |
| Acceptance — 16 scenarios, real components over an in-memory GitHub | 19 | 65.1% |
| End-to-end — real repository, real engine, nothing mocked | 12/14 requirements | — |
| **Combined** | **448** | **84.1%** |

`factory/acceptance/e2e.py --list` prints the requirement map, including what it
cannot reach: an untrusted-author dispatch needs a second GitHub identity, and
claim expiry needs sixty minutes of wall clock. Both are named on every run rather
than left implicit.

Phase 1 was verified by walking a synthetic project and three stories through every legal state transition by hand — including the failure paths: retry, attempt-limit poisoning, human rescue, and a scope dispute. See [`PROGRESS.md`](PROGRESS.md) for the current position and the evidence trail.

## The idea

Three layers, described in full in the architecture document:

- **Intent and acceptance** — humans appear at intent boundaries and exception branches, not between steps.
- **Coordination loop** — AI judgment roles and deterministic mechanical checks, each invoked on a state transition, reading state and writing state, then exiting.
- **Shared state substrate** — durable state, a change feed, identity, unforgeable gates, and append-only history. GitHub is one binding of it: issues and labels are state, identities are access, branch protection and required checks are the gates, and the git log plus issue timeline are the history.

Two design rules do most of the work. **No component addresses another** — a component writes state and exits, and the transition routes the next one, so the only contract between components is the state schema. And **every AI-judgment component has an independent check it cannot influence** — workers are checked by review, review is triggered by PR-open and audited by human sampling, merges are gated by CI the worker identity cannot affect, and planning is checked by human acceptance of falsifiable criteria.

Human involvement is deliberately reduced to named *bells* — plan approval, hazard acknowledgement, poison rescue, scope decision, cutover approval, acceptance, and sampling — each logged with the time it cost. Pure relay touches are the metric that should trend to zero; defect rescues are not, because suppressing those would hide failure rather than remove it.

## Repository map

| Path | Contents |
|---|---|
| [`factory/spec/architecture-v2.1.md`](factory/spec/architecture-v2.1.md) | The architecture and its rationale |
| [`factory/spec/implementation-plan-v1.md`](factory/spec/implementation-plan-v1.md) | Phased build plan, each phase ending in a runnable verification |
| [`factory/spec/state-schema.md`](factory/spec/state-schema.md) | Authoritative state model — labels, issue fields, legal transitions, bells |
| [`factory/dispatcher/`](factory/dispatcher/) | Authorization, eligibility, claim, dispatch, §9.15 replay |
| [`factory/gates/`](factory/gates/) | The deterministic merge gate |
| [`factory/runtime/`](factory/runtime/) | Poller, worker contract, launch bridge, completion, human queue |
| [`factory/acceptance/`](factory/acceptance/) | Phase 2 acceptance scenarios and the end-to-end suite |
| [`factory/touchlog/`](factory/touchlog/) | Append-only human-touch log and its helper |
| [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) | The three issue types the model recognizes |
| [`PROGRESS.md`](PROGRESS.md) | Current phase, status, and decisions log |

## Built in public

This is a working experiment published as a reference implementation, not a finished product or a library to depend on. The interesting artifact is the paper trail as much as the code: the issues in this repository carry the design review that found the defects, the repairs, the state-machine walk, and each human decision with its evidence.

It may well turn out that some of this is wrong. That is what the phases and their kill criteria are for.
