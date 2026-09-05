# Phase 5 Stabilization Architecture — Independent Review Prompt

You have now completed the independent Phase 5 Rung 2 root-cause analysis in:

`factory/learning/phase5-rung2-independent-rca-2026-09-03.md`

I now want you to review a stabilization architecture that was developed separately before I shared your RCA conclusions.

Do not assume this proposal is correct. Act as the same independent Principal Software Architect / Production Systems Root-Cause Investigator.

Your job is to test this architecture against the actual evidence from Projects #18, #60, #76, #89 and #100 and your RC-1 through RC-6 findings.

Do NOT implement anything yet.

## Proposed stabilization architecture

### 1. Planning owns the complete engineering contract
Planning defines FRs, NFRs, ADRs, Story decomposition and scope, acceptance criteria, required test cases for every FR/NFR, positive/negative/boundary cases, verification type, and required execution environment/executor. Planning decides what must be built and what tests must prove it. Delivery should not be expected to invent the required test plan. Every requirement must have stable IDs and required verification. Planning must not create a requirement the factory has no verified way to test.

### 2. Planning must validate against the repository
Before approval, validate that required files exist; Story scope covers promised behavior; dependencies are permitted; existing tests do not contradict the approach; terminal Stories will not be modified; required test infrastructure/executor exists; and realistic operating-envelope requirements are feasible.

### 3. Story complexity / decomposition gate
Planning evaluates whether a Story contains too many independent correctness, proof, integration or assurance obligations. Split it before Delivery when necessary.

### 4. Independent Plan Review before Human Plan Approval
Independent Reviewer validates FRs, NFRs, ADR technical soundness, assumptions/tradeoffs, proof strategies, required tests and boundaries, requirement-to-test completeness, acceptance-to-scope mapping, repository compatibility, executor availability, Story decomposition and dependencies. Planning authors ADRs; Independent Reviewer verifies them. Only then does the Human Operator receive Plan Approval.

### 5. Delivery owns implementation
Delivery implements the approved Story and every Planning-required test, runs all executable required tests, preserves requirement-to-test mapping, stays within scope, and performs adversarial self-review. Delivery decides how to implement product/tests, not which tests are optional.

### 6. 100% requirements-test coverage gate
Before Review: 100% FR and NFR mapping, 100% required tests implemented, all executable tests passing, no required silent skip. Unexecutable verification reports `NOT VERIFIED` and blocks completion. Line/branch coverage is secondary.

### 7. Delivery completion gate
Require 100% requirements coverage, positive/negative/boundary cases, required integration/E2E, no required skips, scope compliance, full applicable suite passing, and adversarial self-review before QA.

### 8. Independent Delivery Review / QA
Reviewer validates implementation and required tests, checks that tests prove mapped requirements, detects weakened/skipped/gamed tests, verifies ADR compliance, independently searches for counterexamples, creates falsification probes, and performs/validates black-box E2E/UAT. Return the complete reasonably discoverable finding set in one cycle where possible.

### 9. Defect classification and routing
A finding must not automatically consume a Delivery attempt.

- `DELIVERY_DEFECT`: clear achievable approved contract violated by implementation/test implementation → Delivery.
- `PLANNING_DEFECT`: missing/ambiguous/contradictory requirement, inadequate test design, missing scope, invalid ADR/proof, repo conflict, decomposition issue, no viable executor → Planning; do not charge Delivery.
- `RUNTIME_FAILURE`: worker/model/capacity/session/worktree/transport/wrapper/environment/tool failure → Runtime recovery; do not charge Delivery.
- `HUMAN_DECISION_REQUIRED`: approved artifacts cannot determine among legitimate product/governance choices → Human.
- `UNKNOWN / CONFLICTING_EVIDENCE`: fail safely and investigate.

Reviewer may initiate classification but need not be sole classifier; use Reviewer evidence, Planning context, Runtime telemetry and deterministic state.

### 10. Attempt accounting must follow classification
Consume a Delivery attempt only when evidence establishes Delivery received actionable work and the failure belongs to Delivery. Worker-not-started, session/capacity, worktree, transport, wrong wrapper command, environment inability, or impossible/incomplete Planning contract must not consume Delivery attempts. Measure Runtime and Planning failures separately.

### 11. Runtime recovery / failover
Define bounded deterministic recovery: transient retry, allowed alternate model, transport recovery, trusted alternate environment, GitHub Actions/container/dedicated runner for assurance. Evaluate moving Delivery away from the current nested Mac/Codex environment.

### 12. Hard lifecycle invariants
Make terminal and historical guarantees deterministic: `story:merged` terminal; Planning cannot reopen/relabel completed Stories; attempt history append-only; preserve review evidence and human decisions; replacement cannot erase history; Delivery cannot silently expand scope; required tests cannot silently disappear.

### 13. Qualification freeze
After bounded stabilization and regression testing, freeze the factory. Run one fresh Rung 2 with no factory modifications, mid-run patches, threshold changes or history rewriting. If a systemic defect appears, fail and preserve that qualification, fix a new factory version, then run a new qualification. Do not patch-and-resume.

## Review tasks
Compare this proposal directly against your RCA RC-1 through RC-6. For every proposed control: identify RCA causes addressed, actual incidents prevented/shortened/rerouted, autonomy impact, existing partial implementation/duplication, overengineering risk, and remaining failure modes.

Answer specifically:

### A. Coverage of the RCA
Does this provide a control for every material root cause? Identify missing controls.

### B. Planning ownership
Is Planning the correct owner for FRs, NFRs, ADRs, decomposition, required test cases/boundaries, and required executor/environment? Propose a better boundary if not.

### C. Independent Reviewer
Should Reviewer validate both Planning before Delivery and Delivery afterward? Identify cost/conflict and simpler alternatives.

### D. Requirements coverage
Would 100% requirements-to-test coverage materially reduce observed Review → Delivery loops? Cite actual incidents.

### E. Classification and routing
Estimate from five-run evidence how many attempts/recoveries could have been avoided/reclassified.

### F. Runtime environment
Recommend the smallest evidence-backed choice among improved Mac adapters, Linux/container/GitHub Actions Delivery, assurance-only trusted runner, or hybrid.

### G. Doctor complexity
What remains in Doctor? What moves upstream to Planning validation or Runtime health? What should be removed/simplified?

### H. Measurement
Define separate metrics for Planning quality, Delivery quality, Runtime reliability, Reviewer effectiveness, human intervention and overall autonomy. Runtime failures must not masquerade as Delivery failures.

### I. Minimum stabilization set
Do NOT turn every proposal into a project. Categorize as `MUST HAVE BEFORE NEXT RUNG 2`, `SHOULD HAVE BUT CAN WAIT`, and `DEFER / OVERENGINEERING`. For every MUST HAVE, tie it to RCA evidence.

### J. Finish line
Define the concrete condition when Phase 5 stabilization is complete and the factory should be frozen.

## Required output
1. Executive verdict.
2. RCA → proposed-control mapping.
3. Disagreements with the proposal.
4. Missing controls.
5. Minimum stabilization plan.
6. Proposed next-qualification metrics.
7. Explicit Phase 5 stabilization finish line.

Challenge the proposal aggressively. I want the smallest architecture that solves the observed failures, not the most sophisticated architecture we can design.
