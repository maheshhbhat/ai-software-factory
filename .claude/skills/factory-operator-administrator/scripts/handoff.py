#!/usr/bin/env python3
"""Write or check a bounded, engine-neutral factory operator handoff."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
HANDOFF = ROOT / ".factory-operator" / "handoff.json"
REQUIRED = {"schema_version", "written_at", "objective", "status", "next_action",
            "forbidden", "git", "references", "decision_needed", "processes"}
PROCESS_MARKERS = {
    "poller": ("factory/runtime/poller.py", "poll.sh"),
    "worker": ("factory/agents/worker/invoke.py",),
    "qualification": ("factory/acceptance/e2e", "live-e2e"),
}


def command(*args: str) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def processes() -> dict:
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True)
    except OSError as exc:
        return {"status": "unavailable", "reason": type(exc).__name__, "items": []}
    if result.returncode != 0:
        return {"status": "unavailable", "reason": "ps failed", "items": []}
    found = []
    for line in result.stdout.splitlines():
        pid, _, raw = line.strip().partition(" ")
        for kind, markers in PROCESS_MARKERS.items():
            if any(marker in raw for marker in markers) and "handoff.py" not in raw:
                found.append({"pid": int(pid), "kind": kind})
                break
    return {"status": "available", "items": found}


def write(args) -> int:
    value = {
        "schema_version": 1,
        "written_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "objective": args.objective.strip(), "status": args.status.strip(),
        "next_action": args.next_action.strip(), "forbidden": args.forbidden,
        "git": {"branch": command("git", "branch", "--show-current"),
                "commit": command("git", "rev-parse", "HEAD")},
        "references": {"projects": args.project, "stories": args.story,
                       "pull_requests": args.pr},
        "decision_needed": args.decision_needed.strip(), "processes": processes(),
    }
    HANDOFF.parent.mkdir(parents=True, exist_ok=True)
    HANDOFF.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(HANDOFF)
    return 0


def check(_args) -> int:
    if not HANDOFF.is_file():
        print(f"NO HANDOFF — {HANDOFF}")
        return 2
    try:
        value = json.loads(HANDOFF.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"INVALID HANDOFF — {exc}")
        return 3
    missing = sorted(REQUIRED - set(value))
    if missing or value.get("schema_version") != 1:
        print("INVALID HANDOFF — missing or unsupported fields: " + ", ".join(missing))
        return 3
    current = {"branch": command("git", "branch", "--show-current"),
               "commit": command("git", "rev-parse", "HEAD")}
    stale = [key for key in ("branch", "commit")
             if value["git"].get(key) != current[key]]
    print(json.dumps({"handoff": value, "current_git": current,
                      "current_processes": processes(), "stale": stale},
                     indent=2, sort_keys=True))
    return 1 if stale else 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="mode", required=True)
    create = sub.add_parser("write")
    for name in ("objective", "status", "next-action"):
        create.add_argument(f"--{name}", required=True)
    for name in ("forbidden", "project", "story", "pr"):
        create.add_argument(f"--{name}", action="append", default=[])
    create.add_argument("--decision-needed", default="")
    sub.add_parser("check")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    return write(args) if args.mode == "write" else check(args)


if __name__ == "__main__":
    sys.exit(main())
