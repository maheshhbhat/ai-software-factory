# Factory Retrospective

Use this skill when reviewing a completed, stopped, failed, rescued, cancelled, or materially replanned factory Project/Story, or when Mahesh asks for a retrospective, lessons learned, what went well, what did not go well, or what to improve next.

The purpose is not to produce a ceremonial postmortem. The purpose is to identify the smallest evidence-backed improvement that makes the factory better without contaminating an active qualification or adding machinery unnecessarily.

## 1. Read the actual record

Do not retrospect from summaries alone. Read the Project, planning digest and ADRs, relevant Stories and dependencies, delivery attempts, PRs and automated checks, independent review findings, cancellation/rescue/replanning decisions, final assurance/UAT/acceptance evidence, and runtime/monitor/poller evidence when factory behavior is in scope.

Build a short factual timeline before drawing conclusions. Separate facts from inference.

## 2. Evaluate each factory layer independently

Assess at least:

- **Requirements / product promise** — Was the user outcome clear, bounded and falsifiable?
- **Planning** — Was the proposed design feasible and sufficiently proven before Delivery?
- **Human approval** — Did the decision surface expose the real risks understandably?
- **Delivery** — Did workers stay in scope and implement the authorized Story?
- **Automated testing** — What did green tests establish, and what did they not establish?
- **Independent review** — Did review find defects tests missed?
- **Retry behavior** — Did retries address implementation defects, or repeat a defective contract?
- **Failure classification** — Was failure correctly classified as implementation, planning/design, infrastructure/capacity, scope, product ambiguity, or other?
- **Rescue/replanning** — Was rescue appropriate? Was replanning materially different rather than a disguised retry?
- **Governance/state** — Were terminal states, dependencies and provenance preserved?
- **Final assurance** — Did end-user/UAT evidence validate the actual promised outcome?

Do not average away a serious failure because other layers worked.

## 3. Ask the counterfactual question

For every material failure or waste, ask:

> What is the earliest factory layer that had enough information to prevent this?

Prefer fixing that layer. Move defect discovery earlier only when the earlier layer could realistically have detected it.

## 4. Proof-obligation check for strong claims

When a Project or Story promises `maximum`, `minimum`, `highest`, `lowest`, `optimal`, `exact`, `exhaustive`, `all`, `every`, `guaranteed`, monotonicity, completeness, or an equivalent global claim, explicitly ask:

1. What exactly was claimed?
2. What was the complete domain?
3. What invariant, monotonicity, or structural property justified bounded reasoning?
4. Why could skipped/untested values not invalidate the result?
5. What was the derived work/search bound?
6. What counterexample, oracle, or boundary test could falsify the assumption?

Green tests on selected values are not proof of a global claim. Checking a returned candidate and its adjacent candidate is not proof of a global maximum unless the plan establishes why all skipped values are excluded.

If this proof was missing from the approved Story, classify the root cause as Planning/design even if Delivery also contained implementation bugs.

## 5. Distinguish implementation failure from contract failure

- **Implementation defect:** the Story contract describes a sound, achievable method/outcome, but the worker implemented it incorrectly.
- **Planning/design defect:** faithfully satisfying the Story contract would still not establish the promised outcome, or the Story lacks the reasoning needed to know that it can.

Retries are for implementation defects. Do not recommend more retries, a stronger coding model, or rescue when the contract itself is unsound. Recommend replanning or narrowing the claim.

## 6. Check terminal-state integrity

Explicitly verify that terminal artifacts stayed terminal. A `story:cancelled`, `story:merged`, or otherwise terminal Story must not be silently rewritten, reopened, or reused as a fresh attempt. A materially redesigned contract should create a replacement Story with provenance linking it to the old one.

Call out any violation even if a later correction repaired it.

## 7. Measurement Integrity Check — mandatory before action

Before recommending, creating, posting, implementing, or dispatching any factory improvement, determine whether an official qualification, benchmark, acceptance run, rung measurement, comparison, or other controlled measurement is currently active.

Do not rely on Mahesh to tell you. Check available factory/GitHub/runtime evidence yourself when tools permit.

If an official measurement is active:

- **Freeze behavior.** Do not modify the measured factory configuration.
- Record proposed improvements as queued/post-measurement work only if recording them cannot affect the run.
- Do not merge, dispatch, or activate the improvement until the measurement is complete.
- State clearly that the improvement is valid but deferred to preserve measurement integrity.
- Evaluate the measurement using the configuration that actually produced it; do not retroactively credit a later fix.

If measurement state cannot be established, say so and ask before initiating a behavior-changing factory modification.

This check happens **before action**, not as an afterthought.

## 8. Choose the smallest high-leverage improvement

Rank improvements by recurrence prevention, earlier/cheaper detection, evidence, low architectural cost, and ability to validate in the next qualification.

Default to one primary improvement. Add secondary improvements only for independent material defects.

Do not automatically add another agent, reviewer, orchestration layer, retries, larger model, generalized framework, service, or broad test harness. First ask whether a prompt/contract/rule/test change solves the demonstrated problem.

## 9. Preserve successful controls

Name controls that worked and should not be weakened: independent review, bounded retries, human bells, scope enforcement, cancellation, no-rescue decisions, UAT, merge gates, or others supported by evidence.

Do not optimize cycle time by removing the mechanism that caught the defect.

## 10. Required output

Lead with a concise executive summary suitable for a factory operator.

### Executive summary
Overall outcome and most important lesson.

### What went well
Only meaningful controls/capabilities that materially contributed.

### What did not go well
Root causes and meaningful waste. Distinguish symptoms from causes.

### Root cause
Name the earliest layer that could have prevented the primary failure and why.

### Next improvement
Recommend the single highest-leverage change, why it is next, and what **not** to change yet.

### Measurement integrity
State one of:
- `FREEZE — improvement deferred until measurement completes`
- `CLEAR — implementation can proceed`
- `UNKNOWN — do not modify factory behavior until confirmed`

### Validation
State how the next qualification/run will demonstrate whether the improvement worked.

Keep the operator-facing version short and plain. Provide deeper evidence only when asked or when a decision requires it.

## 11. Action rule

A retrospective may recommend work. It does not automatically authorize implementation.

Before changing factory code/configuration:

1. Run the Measurement Integrity Check.
2. Confirm the change will not contaminate an active measurement.
3. Prefer normal factory governance unless Mahesh explicitly authorizes a direct maintenance path.
4. If Mahesh asks to proceed, make/dispatch only the smallest authorized increment.

## Project #76 regression lesson

Project #76 is the canonical example for two checks:

- Story #78 passed automated tests but independent review showed its bounded candidate checks could not prove that an untested cent was not the true maximum. Root cause: Planning/design, not lack of retries. The improvement is an earlier proof obligation for exact/optimal/global claims.
- An official Rung 2 measurement was already running when the improvement was proposed. The retrospective should have checked measurement state before moving toward factory modification. Improvements discovered during a controlled measurement must be queued until the run finishes.

Use these as regression examples, not special-case rules.