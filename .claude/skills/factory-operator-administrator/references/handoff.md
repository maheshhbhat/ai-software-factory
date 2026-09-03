# Engine-neutral handoff

Write a handoff when an engine is near its session limit, Mahesh requests a
takeover, or operational work must pause unfinished.

Use:

```bash
python3 .claude/skills/factory-operator-administrator/scripts/handoff.py write \
  --objective "plain objective" --status "verified current state" \
  --next-action "one safe next action" \
  --forbidden "action the next engine must not take"
```

Add `--project`, `--story`, `--pr`, and `--decision-needed` when relevant. Repeat
`--forbidden` for multiple boundaries. The tool writes
`.factory-operator/handoff.json`, including branch, commit, time, and bounded
process observations. The directory is ignored by Git.

On takeover, run `check`, compare the recorded Git identity with the checkout,
re-read every referenced GitHub artifact and current process state, mark changed
or unverifiable claims stale, and continue only the next action that remains
authorized.

Never include credentials, raw provider output, full process commands, invented
decisions, or unverified claims.
