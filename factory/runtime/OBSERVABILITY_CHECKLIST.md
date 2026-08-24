# Factory observability implementation checklist

This is a manual repair. The factory is not being used to improve itself.

- [x] Keep process events, operational diagnostics, and telemetry in separate JSONL files.
- [x] Add stable delivery-attempt trace IDs from the durable Story claim.
- [x] Add component activities, five-second heartbeats, and live status rendering.
- [x] Preserve full stack traces and prevent logging failures from hiding primary failures.
- [x] Wire dispatcher, poller, workers, reviewer, gates, and reconciliation components.
- [x] Emit supervisor heartbeats while child processes run, without waiting for completion.
- [x] Persist E2E evidence from start through failure and render nested engine usage.
- [x] Add failure-path, trace-continuity, heartbeat, and status tests.
- [x] Stop a harness-owned poller on success, failure, interruption, termination, or terminal disconnect.
- [x] Abort the remaining cycle after the first explicit GitHub rate-limit response.
- [x] Remove the broad GitHub cache; review decisions are always read fresh after a reviewer finishes.
- [x] Back idle polling off to five minutes and return to the configured interval on activity.
- [x] Run the full test suite and both canonical coverage reports.
- [ ] Obtain approval for a fresh disposable E2E Project, then run it.
- [ ] Commit and open one PR only after the live E2E succeeds.
