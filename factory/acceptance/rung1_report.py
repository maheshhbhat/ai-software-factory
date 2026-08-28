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


def engine_usage_cost(telemetry, numbers, accepted_story_count):
    """Report provider-aware normalized usage and only provider-exposed exact cost.

    New runs use ``capacity.route.attempt`` as the canonical invocation record.
    Older frozen bundles contain only ``engine.usage`` rows, so their historical
    behavior remains supported.
    """
    route_rows = [row for row in telemetry if row.get("metric") == "capacity.route.attempt"
                  and row.get("story") in numbers]
    if route_rows:
        route_rows = unique_rows(route_rows, lambda row: row.get("invocation_id"))
        findings = []
        usage_by_engine = defaultdict(
            lambda: {"invocations": 0, "reported_invocations": 0,
                     "unreported_invocations": 0, "normalized_capacity_units": 0.0,
                     "priced_invocations": 0, "unpriced_invocations": 0})
        total_cost = 0.0
        total_units = 0.0
        all_priced = True
        for row in route_rows:
            invocation_id = row.get("invocation_id")
            name = row.get("model") or "unknown"
            total = usage_by_engine[name]
            total["invocations"] += 1
            receipt = row.get("usage_receipt")
            units = receipt.get("normalized_capacity_units") if isinstance(receipt, dict) else None
            basis = receipt.get("capacity_unit_basis") if isinstance(receipt, dict) else None
            exact_cost = receipt.get("exact_cost_usd") if isinstance(receipt, dict) else None
            unavailable_reason = (receipt.get("dollar_cost_unavailable_reason")
                                  if isinstance(receipt, dict) else None)
            valid = (isinstance(invocation_id, str) and bool(invocation_id.strip())
                     and isinstance(units, (int, float)) and units >= 0
                     and isinstance(basis, str) and bool(basis.strip())
                     and ((isinstance(exact_cost, (int, float)) and exact_cost >= 0)
                          or (exact_cost is None and isinstance(unavailable_reason, str)
                              and bool(unavailable_reason.strip()))))
            if not valid:
                total["unreported_invocations"] += 1
                findings.append(
                    f"capacity route {invocation_id or 'without invocation ID'} lacks a "
                    "complete reproducible usage receipt")
                all_priced = False
                continue
            total["reported_invocations"] += 1
            total["normalized_capacity_units"] += units
            total_units += units
            reported = receipt.get("reported_usage")
            if isinstance(reported, dict):
                for field in ("input_tokens", "output_tokens", "total_tokens"):
                    if isinstance(reported.get(field), (int, float)):
                        total[field] = total.get(field, 0) + reported[field]
            if isinstance(exact_cost, (int, float)):
                total["priced_invocations"] += 1
                total["reported_cost_usd"] = round(
                    total.get("reported_cost_usd", 0) + exact_cost, 8)
                total_cost += exact_cost
            else:
                total["unpriced_invocations"] += 1
                all_priced = False
        for total in usage_by_engine.values():
            total["normalized_capacity_units"] = round(
                total["normalized_capacity_units"], 8)
        receipts_complete = not findings
        return {
            "by_engine": dict(sorted(usage_by_engine.items())),
            "route_invocations": len(route_rows),
            "usage_receipts_status": "complete" if receipts_complete else "incomplete",
            "usage_integrity_findings": sorted(findings),
            "normalized_capacity_units": round(total_units, 8),
            "normalized_capacity_units_per_accepted_story": (
                round(total_units / accepted_story_count, 8)
                if accepted_story_count else UNAVAILABLE),
            "known_reported_cost_usd": round(total_cost, 8),
            "cost_status": ("complete" if receipts_complete and all_priced
                            else "provider-pricing-partial" if receipts_complete
                            else "lower-bound"),
            "cost_per_accepted_story_usd": (
                round(total_cost / accepted_story_count, 8)
                if receipts_complete and all_priced and accepted_story_count else UNAVAILABLE),
            "known_reported_cost_per_accepted_story_usd": (
                round(total_cost / accepted_story_count, 8)
                if accepted_story_count else UNAVAILABLE),
        }

    # Compatibility for the Project #18-era frozen schema.
    rows = [row for row in telemetry if row.get("metric") == "engine.usage"
            and row.get("story") in numbers]
    usage_by_engine = defaultdict(
        lambda: {"invocations": 0, "reported_invocations": 0,
                 "unreported_invocations": 0})
    total_cost = 0.0
    complete = bool(rows)
    for row in rows:
        name = row.get("engine") or "unknown"
        total = usage_by_engine[name]
        total["invocations"] += 1
        if row.get("usage_reported") is not True:
            total["unreported_invocations"] += 1
            complete = False
            continue
        total["reported_invocations"] += 1
        block = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            if isinstance(block.get(field), (int, float)):
                total[field] = total.get(field, 0) + block[field]
        cost = block.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            total["reported_cost_usd"] = round(
                total.get("reported_cost_usd", 0) + cost, 8)
            total_cost += cost
        else:
            complete = False
    exact_per_story = (round(total_cost / accepted_story_count, 8)
                       if complete and accepted_story_count else UNAVAILABLE)
    return {
        "by_engine": dict(sorted(usage_by_engine.items())),
        "usage_receipts_status": "legacy-engine-events",
        "usage_integrity_findings": [],
        "known_reported_cost_usd": round(total_cost, 8),
        "cost_status": "complete" if complete else "lower-bound",
        "cost_per_accepted_story_usd": exact_per_story,
        "known_reported_cost_per_accepted_story_usd": (
            round(total_cost / accepted_story_count, 8)
            if accepted_story_count and rows else UNAVAILABLE),
    }


def unique_rows(rows, key):
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
    project = evidence.get("project")
    stories = evidence.get("stories") or []
    numbers = {row.get("number") for row in stories if row.get("number") is not None}
    numbers.update(number for number in (evidence.get("stories_created") or [])
                   if isinstance(number, int))
    passed = bool(evidence.get("passed"))
    report_phase = evidence.get("report_phase", "final")
    if report_phase not in ("pre-acceptance", "final"):
        raise ValueError("report_phase must be pre-acceptance or final")
    if passed and not numbers:
        raise ValueError("a successful black-box run with named Stories is required")
    if passed and any(not row.get("merged") for row in stories):
        raise ValueError("every measured Story must be merged")

    integrity = []
    project_touches = [row for row in touches if row.get("project") == f"#{project}"]
    operator_actions = evidence.get("operator_actions") or []
    if not isinstance(operator_actions, list) or any(
            not isinstance(row, dict) or not row.get("classification")
            or not row.get("action") for row in operator_actions):
        integrity.append("operator_actions must be a list of classified named actions")
        operator_actions = []
    human_touches = project_touches + operator_actions
    classes = Counter(row.get("classification") for row in human_touches)
    decisions = evidence.get("decisions") or []
    required_bells = (BELLS if passed and report_phase == "final"
                      else ("plan-approval",))
    for bell in required_bells:
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
    retries = max(0, attempts - len(numbers)) if numbers else 0
    poisoned = sum("story:blocked:poison" in (row.get("walk") or []) for row in stories)

    interventions = evidence.get("human_code_interventions", UNAVAILABLE)
    if not passed:
        autonomy = unavailable("the black-box run produced no accepted delivery to classify")
    elif interventions == UNAVAILABLE:
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
    if not passed:
        escaped = unavailable("the black-box run produced no accepted delivery to observe post-merge")
    elif observations is None:
        escaped = unavailable("no explicit post-merge defect observation was frozen")
    else:
        escaped_rows = [row for row in observations if row.get("kind") == "escaped-defect"]
        escaped = {"count": len(escaped_rows), "observations": escaped_rows}

    acceptance = evidence.get("acceptance")
    if not passed:
        catches = unavailable("the black-box run did not reach outcome acceptance")
    elif not isinstance(acceptance, dict) or not isinstance(acceptance.get("criteria"), list):
        catches = unavailable(
            "outcome acceptance is pending" if report_phase == "pre-acceptance"
            else "canonical outcome-acceptance criterion results are absent")
    else:
        invalid = [row for row in acceptance["criteria"]
                   if row.get("result") not in ("pass", "fail") or not row.get("criterion")]
        if invalid:
            integrity.append("acceptance criterion evidence is malformed")
            catches = unavailable("canonical acceptance criterion evidence is malformed")
        else:
            failed = [row for row in acceptance["criteria"] if row["result"] == "fail"]
            catches = {"count": len(failed), "criteria": failed}

    usage_cost = engine_usage_cost(
        telemetry, numbers, len(numbers) if passed else 0)
    integrity.extend(usage_cost.get("usage_integrity_findings") or [])

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
        "report_phase": report_phase,
        "project": project,
        "run": evidence.get("run"),
        "black_box_uat": {"passed": passed, "entrypoint": evidence.get("entrypoint"),
                          "reason": evidence.get("reason")},
        "measurement_integrity": {"passed": not integrity, "findings": sorted(integrity)},
        "kpis": {
            "human_touches": {"count": len(human_touches),
                              "by_classification": dict(sorted(classes.items())),
                              "relay": classes["relay"], "records": human_touches},
            "autonomy": autonomy,
            "worker_attempts_retry_rate": {"attempts": attempts, "stories": len(numbers),
                                           "attempts_per_story": (attempts / len(numbers)
                                                                  if numbers else UNAVAILABLE),
                                           "retries": retries,
                                           "retry_rate": (retries / len(numbers)
                                                          if numbers else UNAVAILABLE)},
            "poison_rate": {"poisoned_stories": poisoned, "stories": len(numbers),
                            "rate": poisoned / len(numbers) if numbers else UNAVAILABLE},
            "escaped_defects": escaped,
            "acceptance_catches": catches,
            "engine_usage_cost": usage_cost,
            "cycle_time": cycle,
        },
        "observation_cutoff": evidence.get("observation_cutoff"),
        "sources": evidence.get("sources") or [],
    }


def render(report):
    k = report["kpis"]
    def value(item, key="count"):
        return item.get(key, UNAVAILABLE) if isinstance(item, dict) else item
    def percent(item):
        return f"{item:.2%}" if isinstance(item, (int, float)) else str(item)
    return "\n".join([
        f"# Phase 5 Rung 1 KPI report — Project #{report['project']}", "",
        f"Black-box UAT: {'PASS' if report['black_box_uat']['passed'] else 'FAIL'}",
        f"Measurement integrity: {'PASS' if report['measurement_integrity']['passed'] else 'FAIL'}",
        "", "| KPI | Result |", "|---|---|",
        f"| Human touches | {k['human_touches']['count']} (relay: {k['human_touches']['relay']}) |",
        f"| Autonomy | {value(k['autonomy'], 'rate')} |",
        f"| Worker attempts / retry rate | {k['worker_attempts_retry_rate']['attempts']} / {percent(k['worker_attempts_retry_rate']['retry_rate'])} |",
        f"| Poison rate | {percent(k['poison_rate']['rate'])} |",
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
