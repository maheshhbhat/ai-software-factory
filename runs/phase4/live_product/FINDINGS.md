# Review findings — Story #233, Attempt 1

Fresh-context review of the pull-request head delivered by Attempt 1. Recorded
here as the durable finding the Attempt 2 worker read; the authoritative copy is
the review outcome bound to that head on the pull request.

Verdict: **findings** — the story returns to `story:ready` without incrementing
`Attempt`; the redispatch increments it (`state-schema.md` §4.3).

1. `app.py:build_sha` returns the literal string `defective` and never reads
   `BUILD_SHA`. The endpoint reports invented health data for every deployment,
   so a caller cannot tell which commit is running. The fixture ADR
   (`factory/decisions/phase4-live-fixture.md`) names `BUILD_SHA`, injected from
   the exact deployed Git commit, as the sole authoritative build identity.
2. No validation of the build identity. The ADR requires exactly 40 lowercase
   hexadecimal characters and requires missing, abbreviated, uppercase, or
   otherwise malformed values to fail startup rather than serve an invented or
   partial SHA.
3. `test_health.py` asserts only that the response carries a `build_sha` key, so
   both defects above pass a green suite. The tests must pin the served value to
   the injected identity and cover each malformed-identity class.

Not findings, recorded so the next attempt does not churn them: the response
shape `{"build_sha": "<sha>"}` with status 200, the 404 on every other path, the
standard-library-only implementation, and the scope confinement to
`runs/phase4/live_product/**` all match the Story and the ADR.

Under the accepted shared-credential limitation
(`factory/decisions/phase4-shared-credential.md`), this review outcome is
detective evidence and a routing prerequisite. It is not a §9.14 merge-gate
trust input, and the worker credential could forge it.
