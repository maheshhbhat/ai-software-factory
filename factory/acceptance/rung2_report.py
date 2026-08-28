#!/usr/bin/env python3
"""Generate a deterministic Phase 5 Rung 2 KPI report from frozen evidence."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from factory.acceptance import rung1_report as shared

UNAVAILABLE = shared.UNAVAILABLE
STORY_BELLS = ("plan-approval", "poison-rescue", "acceptance")
AUTONOMY_BREAKING_ACTIONS = {"recovery", "code-intervention"}


def unavailable(reason):
    return shared.unavailable(reason)


def unique(rows, key):
    seen = set()
    result = []
    for row in rows:
        marker = key(row)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(row)
    return result


def build(evidence, process, telemetry, touches):
    stories = evidence.get("stories") or []
    numbers = {row.get("number") for row in stories if isinstance(row.get("number"), int)}
    integrity = []
    if not numbers or len(numbers) != len(stories):
        integrity.append("every measured Story needs one distinct integer number")
    if any(not row.get("merged") for row in stories):
        integrity.append("every measured Story must be merged")

    project_ref = evidence.get("project_ref")
    project_touches = [row for row in touches if row.get("project") == project_ref]
    decisions = evidence.get("decisions") or []
    for bell in STORY_BELLS:
        observed = [row for row in decisions if row.get("bell_type") == bell]
        receipts = [row for row in project_touches if row.get("bell_type") == bell]
        if len(observed) != len(receipts):
            integrity.append(
                f"{bell} decisions ({len(observed)}) and touch receipts ({len(receipts)}) differ")
    actions = evidence.get("operator_actions") or []
    if not isinstance(actions, list) or any(
            not isinstance(row, dict) or not row.get("action")
            or not row.get("classification") for row in actions):
        integrity.append("operator_actions must be classified named records")
        actions = []
    classes = Counter(row.get("classification") for row in project_touches)

    claims = unique(
        [row for row in process if row.get("event") == "story.claimed"
         and row.get("story") in numbers],
        lambda row: (row.get("story"), row.get("event_id")),
    )
    if any(row.get("event_id") is None for row in claims):
        integrity.append("every claim needs a durable event ID")
    attempts_by_story = {
        number: sum(row.get("story") == number for row in claims)
        for number in sorted(numbers)
    }
    attempts = sum(attempts_by_story.values())
    retries = max(0, attempts - len(numbers))

    recovered = {row.get("number") for row in stories if row.get("human_recovery")}
    recovered.update(
        row.get("story") for row in actions
        if row.get("classification") in AUTONOMY_BREAKING_ACTIONS
        and row.get("story") in numbers)
    autonomous = numbers - recovered
    autonomy = {
        "autonomous_stories": len(autonomous), "stories": len(numbers),
        "rate": len(autonomous) / len(numbers) if numbers else UNAVAILABLE,
        "autonomous_story_numbers": sorted(autonomous),
        "non_autonomous_story_numbers": sorted(recovered),
        "definition": ("merged without human recovery or human code intervention; "
                       "required governance is measured separately"),
    }

    poisoned_numbers = {row.get("number") for row in stories if row.get("poisoned")}
    poison = {
        "poisoned_stories": len(poisoned_numbers), "stories": len(numbers),
        "rate": len(poisoned_numbers) / len(numbers) if numbers else UNAVAILABLE,
        "story_numbers": sorted(poisoned_numbers),
    }

    observations = evidence.get("quality_observations")
    if not isinstance(observations, list):
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

    telemetry = unique(
        telemetry,
        lambda row: (row.get("timestamp"), row.get("component"), row.get("story"),
                     row.get("pull_request"), row.get("metric")),
    )
    usage = shared.engine_usage_cost(telemetry, numbers, len(numbers))
    integrity.extend(usage.get("usage_integrity_findings") or [])

    approved = [row for row in decisions if row.get("bell_type") == "plan-approval"
                and row.get("result") == "approved"]
    accepted = [row for row in decisions if row.get("bell_type") == "acceptance"
                and row.get("result") == "pass"]
    if len(approved) == len(accepted) == 1:
        elapsed = (shared.instant(accepted[0]["timestamp"]) -
                   shared.instant(approved[0]["timestamp"])).total_seconds()
        if elapsed < 0:
            integrity.append("acceptance precedes approved plan")
            cycle = unavailable("cycle boundaries are reversed")
        else:
            cycle = {"start": approved[0]["timestamp"], "end": accepted[0]["timestamp"],
                     "seconds": elapsed}
    else:
        cycle = unavailable("one approved plan and one passing acceptance are required")

    thresholds = evidence.get("thresholds") or {
        "autonomy_minimum": .75, "relay_maximum": 0, "escaped_defects_maximum": 0}
    threshold_results = {
        "autonomy": isinstance(autonomy["rate"], (int, float))
                    and autonomy["rate"] >= thresholds["autonomy_minimum"],
        "relay": classes["relay"] <= thresholds["relay_maximum"],
        "escaped_defects": isinstance(escaped, dict) and "count" in escaped
                           and escaped["count"] <= thresholds["escaped_defects_maximum"],
    }
    verdict = "PASS" if all(threshold_results.values()) else "FAIL"

    return {
        "schema_version": 1,
        "rung": 2,
        "report_phase": "final",
        "repository": evidence.get("repository"),
        "commitment": evidence.get("commitment"),
        "project": evidence.get("project"),
        "product_outcome": evidence.get("product_outcome"),
        "rung_verdict": verdict,
        "thresholds": thresholds,
        "threshold_results": threshold_results,
        "measurement_integrity": {"passed": not integrity, "findings": sorted(integrity)},
        "kpis": {
            "human_touches": {
                "canonical_bells": len(project_touches),
                "by_classification": dict(sorted(classes.items())),
                "relay": classes["relay"], "records": project_touches,
                "operator_actions": actions,
            },
            "autonomy": autonomy,
            "worker_attempts_retry_rate": {
                "attempts": attempts, "stories": len(numbers),
                "attempts_by_story": attempts_by_story,
                "attempts_per_story": attempts / len(numbers) if numbers else UNAVAILABLE,
                "retries": retries,
                "retry_share_of_attempts": retries / attempts if attempts else UNAVAILABLE,
            },
            "poison_rate": poison,
            "escaped_defects": escaped,
            "acceptance_catches": catches,
            "engine_usage_cost": usage,
            "cycle_time": cycle,
        },
        "sources": evidence.get("sources") or [],
    }


def render(report):
    k = report["kpis"]
    percent = lambda value: f"{value:.2%}" if isinstance(value, (int, float)) else str(value)
    return "\n".join([
        "# Phase 5 Rung 2 final report", "",
        f"**Verdict: {report['rung_verdict']}.** Product outcome: {report['product_outcome']}.", "",
        "| KPI | Result |", "|---|---|",
        f"| Human touches | {k['human_touches']['canonical_bells']} canonical bells; relay {k['human_touches']['relay']} |",
        f"| Autonomy | {k['autonomy']['autonomous_stories']}/{k['autonomy']['stories']} = {percent(k['autonomy']['rate'])} |",
        f"| Attempts | {k['worker_attempts_retry_rate']['attempts']} ({k['worker_attempts_retry_rate']['attempts_per_story']} per Story) |",
        f"| Poison rate | {percent(k['poison_rate']['rate'])} |",
        f"| Escaped defects | {k['escaped_defects'].get('count', UNAVAILABLE)} |",
        f"| Acceptance catches | {k['acceptance_catches'].get('count', UNAVAILABLE)} |",
        f"| Cost / accepted Story | {k['engine_usage_cost']['cost_per_accepted_story_usd']} (known lower bound: {k['engine_usage_cost']['known_reported_cost_per_accepted_story_usd']}) |",
        f"| Cycle time | {k['cycle_time'].get('seconds', UNAVAILABLE)} seconds |", "",
        f"Measurement integrity: {'PASS' if report['measurement_integrity']['passed'] else 'FAIL'}", "",
    ])


def read_many(paths):
    rows = []
    for path in paths:
        rows.extend(shared.read_jsonl(path))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--process", action="append", required=True)
    parser.add_argument("--telemetry", action="append", required=True)
    parser.add_argument("--touchlog", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    report = build(evidence, read_many(args.process), read_many(args.telemetry),
                   shared.read_jsonl(args.touchlog))
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "report.md").write_text(render(report), encoding="utf-8")
    return 0 if report["measurement_integrity"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
