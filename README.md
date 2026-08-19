# AI Software Factory

An experiment in building **deterministic delivery rails around AI software agents**, with GitHub as the durable source of truth.

The thesis: AI agents can write code, but the hard part is the machinery around them — knowing what to build next, checking the work with something the worker cannot influence, and keeping humans at the decisions that actually need judgment instead of relaying status between steps. This repository builds that machinery first, and measures whether it holds.

## Current status

**Phase 1 is verified and complete. Phase 2 has not started.**

This repository currently contains a **proven state and governance model — not a running automated factory.** There is no dispatcher, no merge gate, no CI enforcement, no worker, and no AI agent in this repository. Every gate described in the specs is *designed and specified*, and none of it is *implemented or enforced* yet.

| Phase | What it delivers | State |
|---|---|---|
| 1 — State schema and touch log | Issue types, label state machines, legal transitions, human-touch log | **VERIFIED** |
| 2 — Deterministic rails | Merge gate, hazard CODEOWNERS, dispatcher, notifications | Not started |
| 3 — Planning agent | Turns direction into projects and dependency-ordered stories | Not started |
| 4 — Workers and review | Story implementation and independent review on PR-open | Not started |
| 5 — Test ladder and KPIs | Three rungs of increasing realism, measured | Not started |

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
| [`factory/touchlog/`](factory/touchlog/) | Append-only human-touch log and its helper |
| [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) | The three issue types the model recognizes |
| [`PROGRESS.md`](PROGRESS.md) | Current phase, status, and decisions log |

## Built in public

This is a working experiment published as a reference implementation, not a finished product or a library to depend on. The interesting artifact is the paper trail as much as the code: the issues in this repository carry the design review that found the defects, the repairs, the state-machine walk, and each human decision with its evidence.

It may well turn out that some of this is wrong. That is what the phases and their kill criteria are for.
