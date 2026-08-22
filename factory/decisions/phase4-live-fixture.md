# ADR: Phase 4 live health fixture

Status: accepted by the approved boundary of Project #212

## Decision

The canonical fixture source is `factory/fixtures/phase4_health/` in the factory
repository and is owned by the factory maintainers. The live proof may copy this
small module to a dedicated temporary fixture repository, but must never use the
private retirement-modeling product repository or its Stories #5–#12.

The executable interface is an HTTP server whose `GET /health` response is JSON
`{"build_sha":"<sha>"}` with status 200. Other paths return 404. The sole
authoritative build identity is the `BUILD_SHA` environment variable injected
by the deployment from the exact deployed Git commit. It must be 40 lowercase
hexadecimal characters; missing, abbreviated, uppercase, or otherwise malformed
values fail startup rather than producing invented health data.

## Reset and supersession

Every live run uses a newly named branch and fixture Story. Reset means starting
a successor run from the canonical baseline commit; it never rewrites shared
history, force-pushes, deletes repositories, weakens protections, or cleans up
product data. Old runs remain as audit evidence and may be closed/superseded by
a link to their successor. Temporary local processes and directories may be
stopped and discarded after evidence is captured.

## Consequences

The fixture is self-contained and has no external data dependency. A returned
SHA can be compared directly with the deployed commit. Repository-level
protection and zero-relay behavior remain live-E2E concerns owned by Story #220,
not claims made by this hermetic fixture.
