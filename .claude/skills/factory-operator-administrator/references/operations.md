# Operator playbooks

## Status

Read GitHub lifecycle labels and timelines, open PRs and checks, local run state,
and live processes. Report what is complete, active, blocked, who owns the next
action, and the next safe step. Do not infer activity from a label alone.

## Start, resume, pause, or stop

Confirm measurement intent, read the run instructions, run Doctor, and check for
a live poller. Start one poller only when none exists. Use `factory-monitor` for
observation. When stopping, target only the requested run, confirm its workers
and poller are gone, and preserve evidence and partial work.

## Review and human decisions

Run `bell-check` before recommending approval, acceptance, rescue, cancellation,
hazard acknowledgement, or another human decision. When Mahesh states a verdict,
transcribe it with provenance: comment first, label second. Never author it.

## Failed Story

Build the timeline first. Retry an implementation defect only when the Story
contract remains sound. Replan an impossible contract. Repair Runtime or
environment faults directly only when authorized. Preserve attempts, cancelled
Stories, review findings, and partial-work checkpoints.

## Pull requests and closeout

Read the actual diff, linked Story, scope, checks, review evidence, dependency
order, and head commit. Merge only when authorized and green. Confirm remote
`main`, lifecycle, dependent bases, worktrees, and processes afterward. For
qualification closeout, freeze evidence before running the existing reporter,
verify deterministic regeneration, publish both report forms, then stop.
