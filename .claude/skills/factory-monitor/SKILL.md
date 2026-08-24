---
name: factory-monitor
description: Monitor a running factory black-box UAT from local evidence, report transitions and timings, detect stalled activity, and verify the final verdict and poller shutdown without adding GitHub API traffic.
---

# Factory monitor

Use the bundled deterministic script instead of inventing `tail`, polling, or
JSON parsing commands for each run.

```bash
python3 .claude/skills/factory-monitor/scripts/monitor.py --follow
```

The newest `runs/two-story-real/*` run is selected by default. Use `--run PATH`
for a specific run, or `--once` for one snapshot.

The monitor is read-only. It reads `run-state.json`, `evidence.json`, and the
observability JSONL streams. It must not query GitHub, change labels, restart a
poller, or repair a run. This keeps monitoring from consuming API capacity or
altering the evidence it observes.

Report only meaningful changes in plain language: Story lifecycle, active
component and stage, completed component duration, review/check/merge evidence,
the newest bounded engine-progress event, component failures, terminal verdict, and whether the
harness stopped its poller. An active span is
`STUCK` when its latest heartbeat is older than forty-five seconds. The normal
heartbeat is every thirty seconds, so a healthy silent component remains
active between heartbeats. Do not call a
run successful until `evidence.json` says `passed: true`; inspect and explain a
failed reason instead.

When a human decision follows, apply the repository's bell-check skill to the
durable evidence before recommending approval.
