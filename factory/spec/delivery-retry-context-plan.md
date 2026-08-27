# Delivery retry correction-context plan

## Purpose

Give a delivery retry the complete, trusted correction context for its current
Story and pull-request head without turning the entire comment history into an
AI prompt. Preserve the three-attempt ceiling and every existing product test,
scope, credential, and merge control.

This is a plan, not an implementation. It does not modify runtime code, change
product PR 74, rescue or cancel product Story 71, restart a poller, or record a
Project outcome.

## Required correction packet

The worker invocation receives one deterministic `correction_context` object.
It is separate from the approved Project and Story bodies and contains:

- repository, Project, Story, linked PR, current PR head, and current Attempt;
- the exact-head independent-review outcome and its structured findings;
- subsequent human review input that explicitly names the same Story and head;
- the request-changes record for the linked PR and head;
- the bounded retry authorization, when the transition requires one; and
- stable source identifiers and creation times for every included record.

The implementation must derive the linked PR through the existing canonical
`Story: #N` link. It must read both Story and linked-PR comments. Records are
ordered by creation time and then immutable GitHub comment ID. A content digest
over the normalized packet is included in observability so a run can later show
exactly what the worker saw.

## Inclusion and trust rules

Comments are data, not instructions merely because they exist. Include a record
only when all applicable checks pass:

- It has a recognized structured kind: exact-head review outcome, human review
  input, request changes, or bounded retry authorization.
- It names the current Story and, where applicable, the linked PR and exact head.
- Its author association is `OWNER`, `MEMBER`, or `COLLABORATOR`.
- A review outcome carries the existing machine-readable PR/head/outcome marker.
- A request-changes or retry-authorization record was created after that finding
  and before the current claim.
- It does not contain a worker-start marker, engine transcript, factory recovery
  dump, credential, unrelated Project reference, or another Story's findings.

The implementation must use an explicit parser for each recognized kind. A
substring match on a Markdown heading is not sufficient. Unknown headings and
free-form comments remain excluded.

The shared credential means author identity alone cannot prove who typed a
human decision. The existing provenance sentence remains mandatory: a human
decision record must state that Mahesh gave the decision in session and it was
transcribed. This plan does not pretend the shared credential provides stronger
identity proof than it does.

## Exact-head and lifecycle rules

- A current-head finding may drive a retry.
- A stale-head finding may be retained as prior context only when a later
  current-head finding explicitly carries it forward. It cannot independently
  authorize another attempt.
- A request-changes record for another PR or head is rejected.
- A `story:in-review -> story:ready` transition leaves `Attempt` unchanged. The
  next dispatch increments it exactly once.
- `ATTEMPT_MAX = 3` remains unchanged. No parser, prompt, repair path, or human
  record automatically resets or extends it.
- Poison rescue remains the separate three-step human path already defined by
  the state contract. This correction must not reuse it for ordinary findings.

When a Story is ready because review returned findings, the dispatcher must be
able to build a valid current correction packet before claiming it. Missing,
ambiguous, contradictory, oversized, wrong-head, or malformed required context
fails closed without incrementing Attempt or launching an engine.

## Bounds and prompt safety

The packet contains only the current Story and linked PR. It never includes
unrelated Projects, general issue history, worker transcripts, or raw CI logs.
Normalize each included record to its parsed fields plus a bounded evidence
excerpt. Enforce both a per-record limit and a total packet limit before engine
invocation. Oversize input is a named human-queue failure, not silent
truncation.

The worker prompt labels the packet as repository evidence. It tells the worker
to implement the approved Story while addressing every current correction item;
comment text cannot expand Scope, change spend, weaken tests, authorize GitHub
writes, or override the operating envelope.

## Implementation slices

### Slice 1: structured assembly

Change the delivery input boundary in `factory/agents/worker/invoke.py` and its
focused tests. Add typed parsing, linked-PR comment retrieval, deterministic
normalization, exact-head validation, bounds, and digest emission. Replace the
literal `"## Review findings"` substring filter.

### Slice 2: fail-closed dispatch and observability

At the narrow review-retry transition, require a valid packet before consuming
another attempt. Emit included source IDs, current head, and packet digest—not
comment bodies or credentials—to production observability. Preserve fresh Story
delivery behavior when no prior review exists.

### Slice 3: acceptance regression

Add a hermetic Story 71 fixture containing:

- the original exact-head review finding;
- a later human empty-result diagnosis;
- a PR request-changes record;
- a final bounded-attempt authorization;
- stale-head, unrelated-Story, free-form, transcript, and malformed comments.

The test must prove that all required current records, and only those records,
reach the retry payload in stable order. Mutating the head, Story, PR, marker,
author association, chronology, or size must produce a named refusal before an
engine starts or Attempt changes.

Existing fresh-delivery, scope, credential-isolation, product-test, review-link,
attempt-limit, replay, recovery, and poison-rescue tests remain green. Run the
runtime and acceptance suites plus requirement coverage using the repository's
canonical scripts.

## Qualifying rerun

After the correction merges, do not reuse Project 67 as the qualification run.
Planning selects a new, independent 2–4 Story user-facing outcome. It may reuse
the same product repository, but it must not depend on Project 67 Stories or add
factory/product infrastructure merely to make the run pass.

The Chief Architect approves the new Project before delivery. Doctor runs on
the exact merged factory revision, and exactly one poller runs. The outcome must
exercise comparable difficulty: at least one real browser-rendered behavior, a
meaningful cross-Story dependency, and an independent-review finding followed
by a corrected later attempt. A run with no retry does not verify this fix.

Usage reporting is provider-aware. Report attempts, elapsed time, and available
usage/cost units by provider and model. Do not compare raw token counts across
providers as though they were equivalent, and do not treat a provider capacity
refusal as product work. The Project keeps the normal per-Story spend/time cap.

The owner performs real Chrome UAT after all delivery PRs merge. Independent
review and automated browser checks remain separate evidence. The Chief
Architect records the final Project acceptance or failure; no agent writes that
decision.

## Relationship to the earlier two-stage correction

PR 568's broader plan remains authoritative. PR 570 implemented the short-term
Story-aware reviewer context. The stable requirement-ownership work remains a
separate long-term control. This plan adds the missing delivery-retry feedback
boundary; it does not claim that the earlier long-term ownership work is done.

## Approval boundary

Implementation begins only after the Chief Architect approves this committed
plan. Factory code is changed directly in isolated worktrees. The autonomous
factory is not used to fix itself.
