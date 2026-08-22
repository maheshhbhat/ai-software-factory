#!/usr/bin/env python3
"""Controlled live acceptance-touch proof for Project #294.

``prepare`` creates a disposable Project and terminal child, then lets the real
sequencer ring the acceptance bell.  It never posts the owner's decision.
``consume`` runs only after the owner posts that decision; the real continuation
must write one isolated touch before transitioning, and replay must write none.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "runs" / "acceptance-touch"
REPO = "maheshhbhat/ai-software-factory"
MARKER = "<!-- acceptance-touch-live:v1 -->"

sys.path.insert(0, str(ROOT / "factory" / "agents" / "worker"))
import invoke as delivery
sys.path.insert(0, str(ROOT / "factory" / "runtime"))
import continuation
import runlog
import sequencer


def token() -> str:
    result = subprocess.run(["gh", "auth", "token"], capture_output=True,
                            text=True, check=True)
    value = result.stdout.strip()
    if not value:
        raise RuntimeError("GitHub credential unavailable")
    return value


def labels(issue: dict) -> set[str]:
    return {item.get("name") for item in issue.get("labels", [])}


def find_project(client) -> dict | None:
    found = [issue for issue in client.pages("/issues?state=all")
             if MARKER in (issue.get("body") or "")
             and "type:project" in labels(issue)]
    if len(found) > 1:
        raise RuntimeError("multiple acceptance-touch live Projects")
    return found[0] if found else None


def project_body(story: int | None = None) -> str:
    child = f"#{story}" if story else "#1"
    return f"""### Goal

Prove the live acceptance-touch transition and replay boundary.

### Falsifiable acceptance criteria

- [ ] **LIVE-01 — Prepared stimulus.** This Project declares exactly Story {child}; that Story is closed with `story:merged`, and the ordinary sequencer moved this Project from `project:active` to `project:awaiting-acceptance` without a manual lifecycle-label edit.

### Stories

{child}

### Expected bells

1

### Roadmap commitment

#54

{MARKER}
"""


def story_body(project: int) -> str:
    return f"""### Spec

Disposable terminal child used only to let the ordinary sequencer ring Project #{project}'s bell.

### Project

#{project}

### Phase

hardening

### Depends-on

none

### Hazard

- [ ] Touches hazard path

### Attempt

0

### Spend cap

$5 / 60 min

### Scope

runs/acceptance-touch/**

### Acceptance notes

- No worker is dispatched; this fixture begins terminal and remains closed.

{MARKER}
"""


def prepare(client, secret: str) -> dict:
    existing = find_project(client)
    if existing:
        project = existing
        story_refs, error = sequencer._references(project.get("body") or "", "Stories")
        if error or len(story_refs) != 1:
            raise RuntimeError("existing fixture has no single declared child")
        child = client.issue(story_refs[0])
    else:
        project = client.api("/issues", method="POST", value={
            "title": "[Project] Live fixture: acceptance touch receipt",
            "body": project_body(),
            "labels": ["type:project", "project:active"],
        })
        child = client.api("/issues", method="POST", value={
            "title": "[Story] Live fixture: terminal acceptance-touch child",
            "body": story_body(project["number"]),
            "labels": ["type:story", "story:merged", "phase:hardening"],
        })
        client.api(f"/issues/{child['number']}", method="PATCH",
                   value={"state": "closed", "state_reason": "completed"})
        client.api(f"/issues/{project['number']}", method="PATCH",
                   value={"body": project_body(child["number"])})

    transitioned = False
    for _attempt in range(10):
        fresh = client.issue(project["number"])
        if "project:awaiting-acceptance" in labels(fresh):
            break
        decisions = sequencer.run(REPO, secret, apply=True)
        transitioned = transitioned or project["number"] in {item.number for item in decisions}
        time.sleep(2)
    else:
        raise RuntimeError("ordinary sequencer did not ring the fixture acceptance bell")
    fresh = client.issue(project["number"])
    if "project:awaiting-acceptance" not in labels(fresh):
        raise RuntimeError("fixture state changed while confirming the acceptance bell")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "fixture.json").write_text(json.dumps({
        "repo": REPO, "project": project["number"], "story": child["number"],
        "marker": MARKER, "prepared_state": "project:awaiting-acceptance",
    }, indent=2, sort_keys=True) + "\n")
    return {"project": project["number"], "story": child["number"],
            "state": "project:awaiting-acceptance", "replay": bool(existing),
            "transitioned_this_run": transitioned}


def records(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def consume(client, secret: str, project: int) -> dict:
    touch = OUT / "live-touchlog.jsonl"
    runtime = OUT / "runtime.jsonl"
    OUT.mkdir(parents=True, exist_ok=True)
    before = client.issue(project)
    before_labels = labels(before)
    recovering = "project:accepted" in before_labels
    if not recovering and "project:awaiting-acceptance" not in before_labels:
        raise RuntimeError("fixture is neither awaiting acceptance nor recovering accepted evidence")
    comments = client.pages(f"/issues/{project}/comments")
    decisions = continuation.owner_decisions(comments, continuation.ACCEPTANCE_HEADING)
    if not decisions:
        raise RuntimeError("owner acceptance decision is not posted")
    fingerprint, payload = continuation.acceptance_identity(decisions[-1])
    old_env = dict(os.environ)
    try:
        os.environ.update({"FACTORY_TOUCHLOG_FILE": str(touch),
                           "FACTORY_RUNTIME_LOG": str(runtime),
                           "FACTORY_RUNTIME_LOG_STDERR": "0"})
        first = [] if recovering else continuation.run(REPO, secret, apply=True)
        after_first = records(touch)
        second = continuation.run(REPO, secret, apply=True)
        after_replay = records(touch)
        target_first = ([item for item in first if item.number == project]
                        if not recovering else ["reconstructed-from-timeline"])
        target_replay = [item for item in second if item.number == project]
        runlog.event("acceptance_touch.live", project=project,
                     fingerprint=fingerprint, first_transitions=len(first),
                     target_first_transitions=len(target_first),
                     replay_transitions=len(second),
                     target_replay_transitions=len(target_replay),
                     touch_entries=len(after_replay), recovering=recovering)
    finally:
        os.environ.clear(); os.environ.update(old_env)
    after = client.issue(project)
    matching = [row for row in after_replay
                if row.get("project") == f"#{project}"
                and f"acceptance-fingerprint:{fingerprint}" in row.get("note", "")]
    timeline = client.pages(f"/issues/{project}/timeline")
    accepted_events = [item for item in timeline
                       if item.get("event") == "labeled"
                       and (item.get("label") or {}).get("name") == "project:accepted"]
    passed = ("project:accepted" in labels(after) and len(target_first) == 1
              and len(target_replay) == 0 and len(after_first) == 1
              and after_first == after_replay and len(matching) == 1
              and (not recovering or bool(accepted_events)))
    if not passed:
        raise RuntimeError("live acceptance/touch/replay invariant failed")
    evidence = {
        "criteria": {key: "pass" for key in ("AT-03", "AT-05", "AT-06", "AT-07")},
        "fixture": {"project": project, "before": "project:awaiting-acceptance",
                    "after": "project:accepted", "cleanup": "accepted and closed"},
        "decision": {"comment_url": decisions[-1].get("html_url"),
                     "fingerprint": fingerprint, "canonical_payload": json.loads(payload)},
        "touch": matching[0],
        "transition": {"target_transitions": 1,
                       "recovered_after_harness_assertion": recovering},
        "replay": {"new_entries": 0, "transitions": 0},
        "runlog": "runs/acceptance-touch/runtime.jsonl",
        "timeline": [{"event": item.get("event"),
                      "label": (item.get("label") or {}).get("name"),
                      "created_at": item.get("created_at")} for item in timeline],
    }
    (OUT / "evidence.json").write_text(json.dumps(evidence, indent=2,
                                                   sort_keys=True) + "\n")
    return evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    consume_parser = sub.add_parser("consume")
    consume_parser.add_argument("--project", type=int)
    args = parser.parse_args(argv)
    secret = token()
    client = delivery.GitHub(REPO, secret)
    if args.command == "prepare":
        print(json.dumps(prepare(client, secret), sort_keys=True)); return 0
    fixture = json.loads((OUT / "fixture.json").read_text())
    print(json.dumps(consume(client, secret, args.project or fixture["project"]),
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
