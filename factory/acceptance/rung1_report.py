#!/usr/bin/env python3
"""Generate the Phase 5 Rung 1 KPI report from one frozen run bundle.

This is measurement logic, not lifecycle logic.  It reads files, writes the two
requested reports, and never calls GitHub or changes factory state.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path

UNAVAILABLE = "unavailable"
BELLS = ("plan-approval", "acceptance")


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def unavailable(reason):
    return {"status": UNAVAILABLE, "reason": reason}


def build(evidence, process, telemetry, touches):
    project = evidence.get("project")
    stories = evidence.get("stories") or []
    numbers = {row.get("number") for row in stories}
    if not evidence.get("passed") or not numbers or None in numbers:
        raise ValueError("a successful black-box run with named Stories is required")
    if any(not row.get("merged") for row in stories):
        raise ValueError("every measured Story must be merged")

    integrity = []
    project_touches = [row for row in touches if row.get("project") == f"#{project}"]
    classes = Counter(row.get("classification") for row in project_touches)
    decisions = evidence.get("decisions") or []
    for bell in BELLS:
        observed = [row for row in decisions if row.get("bell_type") == bell]
        receipts = [row for row in project_touches if row.get("bell_type") == bell]
        if len(observed) != 1:
            integrity.append(f"{bell} decision must appear exactly once")
        if len(receipts) != 1:
            integrity.append(f"{bell} touch receipt must appear exactly once")

    claim_rows = [row for row in process
                  if row.get("event") == "story.claimed" and row.get("story") in numbers]
    claim_keys = {(row.get("story"), row.get("event_id")) for row in claim_rows}
    if any(key[1] is None for key in claim_keys):
        integrity.append("every claim needs a durable event ID")
    attempts = len(claim_keys)
    retries = max(0, attempts - len(numbers))
    poisoned = sum("story:blocked:poison" in (row.get("walk") or []) for row in stories)

    interventions = evidence.get("human_code_interventions", UNAVAILABLE)
    if interventions == UNAVAILABLE:
        autonomy = unavailable(
            "the shared GitHub principal cannot independently prove absence of human code involvement")
    elif isinstance(interventions, list):
        affected = {row.get("story") for row in interventions}
        autonomous = len(numbers - affected)
        autonomy = {"autonomous_stories": autonomous, "stories": len(numbers),
                    "rate": autonomous / len(numbers)}
    else:
        integrity.append("human_code_interventions must be a list or unavailable")
        autonomy = unavailable("human intervention evidence is malformed")

    observations = evidence.get("quality_observations")
    if observations is None:
        escaped = unavailable("no explicit post-merge defect observation was frozen")
    else:
        escaped_rows = [row for row in observations if row.get("kind") == "escaped-defect"]
        escaped = {"count": len(escaped_rows), "observations": escaped_rows}

    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, dict) or not isinstance(acceptance.get("criteria"), list):
        catches = unavailable("canonical outcome-acceptance criterion results are absent")
    else:
        invalid = [row for row in acceptance["criteria"]
                   if row.get("result") not in ("pass", "fail") or not row.get("criterion")]
        if invalid:
            integrity.append("acceptance criterion evidence is malformed")
            catches = unavailable("canonical acceptance criterion evidence is malformed")
        else:
            failed = [row for row in acceptance["criteria"] if row["result"] == "fail"]
            catches = {"count": len(failed), "criteria": failed}

    usage_rows = [row for row in telemetry if row.get("metric") == "engine.usage"
                  and row.get("story") in numbers and row.get("usage_reported")]
    usage_by_engine = defaultdict(lambda: {"invocations": 0})
    total_cost = 0.0
    complete_cost = bool(usage_rows)
    for row in usage_rows:
        name = row.get("engine") or "unknown"
        total = usage_by_engine[name]
        total["invocations"] += 1
        block = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            if isinstance(block.get(field), (int, float)):
                total[field] = total.get(field, 0) + block[field]
        cost = block.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            total["reported_cost_usd"] = round(total.get("reported_cost_usd", 0) + cost, 8)
            total_cost += cost
        else:
            complete_cost = False
    cost_per_story = (round(total_cost / len(numbers), 8) if complete_cost else UNAVAILABLE)

    boundaries = {}
    for bell in BELLS:
        found = [row for row in decisions if row.get("bell_type") == bell]
        if len(found) == 1 and found[0].get("timestamp"):
            boundaries[bell] = found[0]["timestamp"]
    if set(boundaries) == set(BELLS):
        elapsed = (instant(boundaries["acceptance"]) -
                   instant(boundaries["plan-approval"])).total_seconds()
        if elapsed < 0:
            integrity.append("acceptance precedes plan approval")
            cycle = unavailable("cycle boundaries are reversed")
        else:
            cycle = {"start": boundaries["plan-approval"], "end": boundaries["acceptance"],
                     "seconds": elapsed}
    else:
        cycle = unavailable("canonical cycle-time boundaries are absent")

    return {
        "schema_version": 1,
        "project": project,
        "run": evidence.get("run"),
        "black_box_uat": {"passed": True, "entrypoint": evidence.get("entrypoint")},
        "measurement_integrity": {"passed": not integrity, "findings": sorted(integrity)},
        "kpis": {
            "human_touches": {"count": len(project_touches),
                              "by_classification": dict(sorted(classes.items())),
                              "relay": classes["relay"], "records": project_touches},
            "autonomy": autonomy,
            "worker_attempts_retry_rate": {"attempts": attempts, "stories": len(numbers),
                                           "attempts_per_story": attempts / len(numbers),
                                           "retries": retries,
                                           "retry_rate": retries / len(numbers)},
            "poison_rate": {"poisoned_stories": poisoned, "stories": len(numbers),
                            "rate": poisoned / len(numbers)},
            "escaped_defects": escaped,
            "acceptance_catches": catches,
            "engine_usage_cost": {"by_engine": dict(sorted(usage_by_engine.items())),
                                  "known_reported_cost_usd": round(total_cost, 8),
                                  "cost_status": "complete" if complete_cost else "partial",
                                  "cost_per_accepted_story_usd": cost_per_story},
            "cycle_time": cycle,
        },
        "observation_cutoff": evidence.get("observation_cutoff"),
        "sources": evidence.get("sources") or [],
    }


def render(report):
    k = report["kpis"]
    def value(item, key="count"):
        return item.get(key, UNAVAILABLE) if isinstance(item, dict) else item
    return "\n".join([
        f"# Phase 5 Rung 1 KPI report — Project #{report['project']}", "",
        f"Black-box UAT: {'PASS' if report['black_box_uat']['passed'] else 'FAIL'}",
        f"Measurement integrity: {'PASS' if report['measurement_integrity']['passed'] else 'FAIL'}",
        "", "| KPI | Result |", "|---|---|",
        f"| Human touches | {k['human_touches']['count']} (relay: {k['human_touches']['relay']}) |",
        f"| Autonomy | {value(k['autonomy'], 'rate')} |",
        f"| Worker attempts / retry rate | {k['worker_attempts_retry_rate']['attempts']} / {k['worker_attempts_retry_rate']['retry_rate']:.2%} |",
        f"| Poison rate | {k['poison_rate']['rate']:.2%} |",
        f"| Escaped defects | {value(k['escaped_defects'])} |",
        f"| Acceptance catches | {value(k['acceptance_catches'])} |",
        f"| Actual engine cost / accepted Story | {k['engine_usage_cost']['cost_per_accepted_story_usd']} |",
        f"| Cycle time | {value(k['cycle_time'], 'seconds')} seconds |", "",
    ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--process", required=True)
    parser.add_argument("--telemetry", required=True)
    parser.add_argument("--touchlog", required=True)
    parser.add_argument("--output", default="runs/rung1")
    args = parser.parse_args(argv)
    report = build(json.loads(Path(args.evidence).read_text(encoding="utf-8")),
                   read_jsonl(args.process), read_jsonl(args.telemetry),
                   read_jsonl(args.touchlog))
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "report.md").write_text(render(report), encoding="utf-8")
    return 0 if report["measurement_integrity"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
