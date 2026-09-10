# Phase 5 qualification freeze

Rung 2 qualification measures one immutable factory revision. It is not a
window in which the factory may be repaired.

## Start

Before creating any qualification work:

1. Select the reviewed `main` commit and record its exact lowercase
   40-character Git SHA as `factory_revision.start` in the qualification
   evidence.
2. Tag that exact commit as the qualification candidate and retain the tag for
   the whole measurement interval.
3. Run all qualification commands from a clean checkout of that commit.

No factory behavior change may merge or be substituted into the qualification
checkout between start and closeout. Product-repository delivery remains the
subject of the measurement; the factory repository is frozen.

## Closeout

Immediately before generating the final report, record the checked-out
factory commit as `factory_revision.closeout`. Both revision fields must be
exact lowercase 40-character SHAs and must match. The report preserves both
values. A missing, malformed, or changed revision makes measurement integrity
fail and the qualification verdict `INCONCLUSIVE`; it cannot produce a normal
`PASS` or `FAIL` result.

If a systemic factory defect is discovered after qualification starts,
preserve the evidence and end that qualification. Fix the defect as a new
reviewed factory version, choose a new candidate commit and tag, and begin a
new qualification. Never patch and resume the same qualification sample.
