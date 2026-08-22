# ADR: Temporary shared credential for Phase 4

Status: accepted for Project #212 only

## Context

Phase 4 needs a delivery worker and a fresh-context reviewer. The repository
currently has one GitHub principal with write access. The owner chose not to
provision separate private GitHub Apps during this increment and explicitly
accepted the resulting risk. Enforceable identity separation is retained as
future Project #221.

Fresh processes and allowlisted context prevent accidental session leakage.
They do not create different GitHub security principals. Anything holding the
shared credential can impersonate either role in repository artifacts.

## Decision

Project #212 may build and verify the worker/reviewer workflow using the shared
GitHub principal under these constraints:

1. Worker and reviewer run in separate, non-persistent model contexts with
   allowlisted inputs. This is behavioral isolation only.
2. Exact-head review evidence is required for routing merge automation, but it
   is not a `state-schema.md` §9.14 trust input and cannot turn a failing
   deterministic merge-gate verdict green.
3. The required merge gate continues to decide only from the diff, linked
   Story scope, CI-computed tests, trusted-main gate code, and repository
   configuration.
4. Review comments, labels, `test-change`, hazard acknowledgements, Agent-ID,
   and PR authorship remain routing, reporting, or audit artifacts. Their
   presence is not proof that a different principal acted.
5. `state-schema.md` §§9.14 and 9.17, the required checks, repository rulesets,
   and bypass actors remain unchanged throughout this Story.
6. Phase 4 acceptance must repeat this limitation. It may claim fresh-context
   review behavior and deterministic CI enforcement; it may not claim
   independent security identities or unforgeable reviewer judgment.

## Risks accepted temporarily

- **Impersonation:** the worker can create repository artifacts that appear to
  come from the reviewer, and the reviewer can appear to act as the worker.
- **Self-certification:** a faulty or compromised worker can forge the review
  routing prerequisite after producing its own change.
- **Audit attribution:** GitHub records one principal, so role attribution is
  derived from process logs and is not security evidence.
- **Shared compromise:** disclosure or misuse of the credential compromises
  both roles simultaneously and broadens revocation impact.
- **Prompt injection:** hostile repository content reaching either model may
  attempt actions available to the shared principal, even when its role prompt
  forbids them.
- **Hazard acknowledgement:** under §§9.14/9.17, a present acknowledgement is
  forgeable. Its absence remains useful evidence that the required human record
  was not made.

## Compensating controls

- Required trusted-main CI fails closed on test failure, malformed scope,
  unavailable inputs, and out-of-scope changes regardless of review artifacts.
- Model sessions are fresh and receive only role-specific context.
- Review evidence is bound to the PR head, making stale results detectable even
  though the issuer is not independently trustworthy.
- Every mutation is reconstructable from GitHub and structured logs.
- Sampling and outcome acceptance can detect errors but do not retroactively
  make the shared credential independent.

## Exit condition

Project #221 provisions and proves distinct least-privilege identities, performs
adversarial live tests, and deliberately revisits §§9.14/9.17. Until #221 is
accepted, no factory documentation or report may describe worker/reviewer
identity separation as mechanically enforced.

