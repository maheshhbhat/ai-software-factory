# Reviewer Project context and Story-boundary plan

## Outcome

Improve independent Review in two bounded stages, then rescue product Story #70. The first stage supplies enough approved Project context to unblock the current run. The second makes requirement ownership and attempt consumption machine-checkable without weakening final Project acceptance.

This document is a plan. It does not change runtime behavior, rescue Story #70, merge product PR #73, or record a human Project outcome.

## Safety rule

Review remains strict at two levels:

- A Story review may reject defects in the current Story, regressions introduced by its PR, and current changes that make an explicitly identified future requirement impossible.
- Final Project acceptance still requires every Project criterion, all operating-envelope observations, and owner Chrome evidence where declared.

Ordinary work assigned to a future Story is pending. Its absence is not a defect in the current Story.

## Stage 1 — short-term context correction

### Reviewer input

Extend `factory/agents/review/invoke.py` to derive and serialize one `project_plan` object from GitHub's durable product issues. It contains only:

- Project number, title, goal, approved criteria, operating envelope, and current labels;
- every Story that names that Project, with number, title, full body, labels, state, declared dependencies, and phase;
- a deterministic classification of each Story as completed, current, prerequisite, or future relative to the current Story;
- relevant ADRs already supplied today;
- the exact current PR head, diff, trusted check state, and previous findings for the current Story.

Exclude unrelated Projects, worker prompts and transcripts, private reasoning, other repository history, and credentials.

The wrapper, not the AI, derives membership from each Story's `### Project` section and validates that dependency references resolve inside the same Project. Malformed or contradictory topology fails closed before an engine invocation.

### Reviewer instruction

Update `factory/agents/review/prompt.md` with this decision rule:

1. Evaluate all current-Story acceptance notes and assigned operating-envelope obligations.
2. Reject regressions caused by the current PR even when the regression is described at Project level.
3. Treat ordinary requirements owned by future Stories as pending, not as findings.
4. A future requirement may block the current Story only when the finding names that future Story and gives concrete evidence that the current head makes its later implementation impossible.
5. Never demand owner-executed evidence before the approved plan says the owner can perform it.

### Stage 1 regression tests

Add deterministic wrapper and prompt-surface tests proving:

- all and only sibling Stories for the approved Project appear in `project_plan`;
- dependency order and current/prerequisite/future classifications are correct;
- unrelated Project Stories and transcripts do not appear;
- malformed cross-Project or missing dependencies fail before AI invocation;
- the #70/#71 fixture shows that Chrome evidence is visibly owned by future Story #71;
- the prompt contains the current-versus-future decision rule;
- exact-head, fresh-checkout, credential isolation, output validation, and existing review routing remain unchanged.

Stage 1 reduces the immediate reasoning error but does not claim that prompt compliance is deterministic.

## Project #67 recovery after Stage 1

After the Stage 1 PR is reviewed and merged:

1. Post a human-authorized poison-rescue comment on product Story #70 stating that Review now receives the complete approved Story plan.
2. Reset `### Attempt` to `0`, record exactly one `poison-rescue` touch, and move Story #70 to `story:ready`, in the repository-required order.
3. Run doctor and start exactly one poller from the corrected factory main.
4. Require Story #70 to fix its genuine exact-label gap.
5. For the real-browser stale-output gap, require either an in-scope executable observation or a human scope decision; do not silently redefine `browser-flow` as a mock DOM.
6. Do not require Story #71's timing, console, or owner Chrome evidence from Story #70.
7. Allow Story #71 to perform its approved browser assurance after #70 merges.

## Stage 2 — durable structured boundary

### Requirement ownership

Extend Project Planning output so every Project acceptance criterion has a stable requirement ID and exactly one owner Story. Every operating-envelope obligation already has an ID and must likewise name exactly one owner Story. Planning read-back fails when an ID is missing, duplicated, assigned to multiple Stories, or assigned to no Story.

Existing approved Projects remain readable through an explicit legacy path, but a Project must have structured ownership before a new delivery attempt can be authorized. There is no silent inference from prose.

### Structured review findings

Replace free-form finding strings with records containing:

- finding text and concrete artifact evidence;
- finding kind: `current-requirement`, `introduced-regression`, or `future-impossibility`;
- the applicable requirement or operating-envelope ID;
- owner Story number;
- for `future-impossibility`, the future Story number and evidence explaining why the current head prevents it.

The wrapper rejects malformed, unknown, or ownership-mismatched findings before publishing them.

### Attempt protection

A valid current finding may return the Story to delivery and allow the next worker start to consume an attempt. A malformed or ownership-mismatched finding cannot transition the Story to `story:ready`.

An ambiguous `future-impossibility` finding moves the Story to the existing human scope-decision path rather than spending an attempt. The human may confirm the blocker, amend scope, or reject the finding. No automated component decides that ambiguity.

The attempt counter remains tied to durable worker-start evidence. Stage 2 changes which review outcomes may authorize another start; it does not decrement, erase, or rewrite historical attempts.

### Stage 2 regression tests

- Planning rejects zero-owner, multiple-owner, unknown-owner, and duplicate requirement IDs.
- Review rejects findings whose requirement belongs to a future Story unless they use `future-impossibility` with the required evidence.
- A rejected or ambiguous finding does not publish `story:ready` and cannot cause another worker-start attempt.
- A genuine current-Story finding still produces the normal correction route.
- An introduced regression can block even when no current requirement names it.
- Final Project acceptance remains impossible until every requirement ID has durable evidence and its criterion is recorded pass/fail by the owner.
- The exact Story #70/#71 fixture proves that owner Chrome evidence stays pending until Story #71 while exact labels remain reviewable on Story #70.

## Implementation boundaries

Stage 1 is limited to independent-review input construction, its prompt, and their tests. Stage 2 is limited to Planning ownership artifacts, structured review output/validation, continuation into the existing scope-decision path, final-evidence read-back, and their tests.

Neither stage may add a provider, dependency, CI workflow, credential path, autonomous factory self-modification, or a product-code change. Both are implemented directly outside the autonomous factory and receive normal trusted checks plus independent human review.

## Rollout and rollback

- Merge Stage 1 before rescuing Story #70.
- Run its existing review suites plus the #70/#71 regression fixture.
- Complete Project #67 under Stage 1 while Stage 2 is implemented as a separate bounded Story/PR stack.
- If Stage 1 causes topology-read failures, fail closed and revert the Stage 1 PR; do not bypass Review or manually merge a product Story.
- Stage 2 activates only for newly planned or explicitly upgraded Projects. Rollback restores the prior review routing but never deletes durable findings, touches, or attempts.

## Success evidence

Short-term success is proven when rescued Story #70 is reviewed with sibling Story #71 visible, fixes only valid current obligations, and advances without being charged for pending Chrome work.

Long-term success is proven by deterministic ownership/read-back tests, structured finding validation, attempt-protection tests, a fresh comparable factory run, and final Project-level evidence showing that no requirement was lost between Story reviews.
