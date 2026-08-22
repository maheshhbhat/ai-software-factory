#!/usr/bin/env python3
"""Project #294 requirement-to-test ladder.

The registry is deliberately static.  A named test must exist, and every claim
that depends on GitHub composition must also appear in the controlled live
ledger written by ``acceptance_touch_live.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "runs" / "acceptance-touch" / "evidence.json"

AT = {
    "AT-01": (("test_acceptance_touch.py",
               "test_freshness_check_precedes_evidence_and_evidence_precedes_state_write"), False),
    "AT-02": (("test_acceptance_touch.py",
               "test_committed_phase4_and_repair_bell_backfills_are_exact"), False),
    "AT-03": (("test_acceptance_touch.py",
               "test_pass_fail_distinct_correction_and_replay_are_exactly_once"), True),
    "AT-04": (("test_acceptance_touch.py",
               "test_comment_id_prose_and_timestamp_do_not_change_identity"), False),
    "AT-05": (("test_acceptance_touch.py",
               "test_corrupt_and_duplicate_evidence_fail_closed"), True),
    "AT-06": (("test_acceptance_touch.py",
               "test_timestamp_actor_seconds_and_project_are_canonical"), True),
    "AT-07": (("test_acceptance_touch_live.py",
               "test_evidence_requires_one_transition_one_touch_and_zero_replay_writes"), True),
    "AT-08": (("test_acceptance_touch_requirements.py",
               "test_delivery_evidence_requires_merged_story_and_both_checks"), True),
}


def load_evidence(path: Path = EVIDENCE) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def live_passes(key: str, evidence: dict) -> bool:
    if (evidence.get("criteria") or {}).get(key) != "pass":
        return False
    if key == "AT-07":
        fixture = evidence.get("fixture") or {}
        replay = evidence.get("replay") or {}
        return (fixture.get("before") == "project:awaiting-acceptance"
                and fixture.get("after") == "project:accepted"
                and replay.get("new_entries") == 0
                and replay.get("transitions") == 0
                and bool(evidence.get("touch")))
    if key == "AT-08":
        delivery = evidence.get("delivery") or {}
        checks = delivery.get("checks") or {}
        return (delivery.get("story") == 296 and delivery.get("story_state") == "story:merged"
                and delivery.get("pr_state") == "MERGED"
                and checks.get("merge-gate") == "SUCCESS"
                and checks.get("merge-gate-surface") == "SUCCESS")
    return True


def build(evidence_path: Path = EVIDENCE) -> dict:
    evidence = load_evidence(evidence_path)
    live = evidence.get("criteria") or {}
    rows, stale, missing_live = [], [], []
    here = Path(__file__).resolve().parent
    for key, ((filename, test_name), needs_live) in AT.items():
        path = here / filename
        present = path.exists() and f"def {test_name}(" in path.read_text()
        if not present:
            stale.append(f"{key}:{filename}::{test_name}")
        live_pass = live_passes(key, evidence)
        if needs_live and not live_pass:
            missing_live.append(key)
        rows.append({"criterion": key, "test": f"{filename}::{test_name}",
                     "test_present": present, "live_required": needs_live,
                     "live": "pass" if live_pass else None})
    return {"project": 294, "requirements": rows, "stale": stale,
            "missing_live": missing_live}


def main() -> int:
    report = build()
    for row in report["requirements"]:
        print(f"{row['criterion']}  test={'yes' if row['test_present'] else 'MISSING'}  "
              f"live={row['live'] or ('MISSING' if row['live_required'] else 'n/a')}  "
              f"{row['test']}")
    if report["stale"]:
        print("STALE: " + ", ".join(report["stale"]))
    if report["missing_live"]:
        print("MISSING LIVE: " + ", ".join(report["missing_live"]))
    return 1 if report["stale"] or report["missing_live"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
