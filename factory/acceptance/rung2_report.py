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
TERMINAL_WORKER_EVENT = "worker.outcome"
REVIEW_OUTCOME_EVENT = "review.outcome.published"


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


def evidence_unavailable(evidence, *, kind, story, identity):
    matches = [row for row in evidence.get("evidence_unavailable", [])
               if isinstance(row, dict) and row.get("kind") == kind
               and row.get("story") == story and row.get("identity") == identity
               and row.get("reason")]
    return matches[0] if len(matches) == 1 else None


def attempt_ledger(evidence, process, numbers):
    claims = unique(
        [row for row in process if row.get("event") == "story.claimed"
         and row.get("story") in numbers],
        lambda row: (row.get("story"), row.get("event_id")))
    claims.sort(key=lambda row: (
        row.get("story"), str(row.get("trace_id") or ""),
        str(row.get("event_id") or "")))
    rows, findings = [], []
    claimed_stories = {row.get("story") for row in claims}
    for story in sorted(numbers - claimed_stories):
        findings.append(f"Story {story} has no durable attempt claim evidence")
    for claim in claims:
        story, claim_id = claim.get("story"), claim.get("event_id")
        trace = claim.get("trace_id")
        identity = trace or claim_id
        outcomes = unique(
            [row for row in process
             if row.get("event") == TERMINAL_WORKER_EVENT
             and row.get("story") == story
             and ((trace and row.get("trace_id") == trace) or
                  (not trace and row.get("claim_event_id") == claim_id))],
            lambda row: (row.get("trace_id"), row.get("event_id")))
        unavailable = evidence_unavailable(
            evidence, kind="attempt-terminal-outcome", story=story,
            identity=identity)
        if len(outcomes) + bool(unavailable) != 1:
            findings.append(
                f"claim {claim_id!r} for Story {story} needs exactly one "
                "terminal worker outcome or evidence-unavailable record")
        outcome = outcomes[0] if len(outcomes) == 1 else None
        if outcome and not outcome.get("event_id"):
            findings.append(
                f"terminal worker outcome for claim {claim_id!r} needs a durable event ID")
        failed = bool(outcome and (outcome.get("exit") not in (None, 0) or
                                  outcome.get("result") != "LAUNCHED"))
        diagnostic = None
        diagnostic_unavailable = None
        launch_starts = unique(
            [row for row in process
             if row.get("event") == "worker.launch.start"
             and row.get("story") == story and trace
             and row.get("trace_id") == trace],
            lambda row: (row.get("trace_id"), row.get("event_id")))
        launch_starts.sort(key=lambda row: (
            str(row.get("trace_id") or ""), str(row.get("span_id") or ""),
            str(row.get("worker") or ""), str(row.get("event_id") or "")))
        launch_ends = unique(
            [row for row in process
             if row.get("event") == "worker.launch.end"
             and row.get("story") == story
             and trace and row.get("trace_id") == trace
             and row.get("event_id")
             and (row.get("exit") is not None or row.get("result"))],
            lambda row: (row.get("trace_id"), row.get("event_id")))
        launch_ends.sort(key=lambda row: (
            str(row.get("trace_id") or ""), str(row.get("span_id") or ""),
            str(row.get("worker") or ""), str(row.get("event_id") or "")))
        launch_ledger = []
        for start in launch_starts:
            start_id, span = start.get("event_id"), start.get("span_id")
            worker = start.get("worker")
            if not start_id:
                findings.append(
                    f"worker launch start for claim {claim_id!r} needs a durable event ID")
            matching_ends = [
                row for row in launch_ends
                if span and row.get("span_id") == span
                and row.get("worker") == worker]
            unavailable_launch = evidence_unavailable(
                evidence, kind="attempt-launch-diagnostics", story=story,
                identity=f"{identity}:{start_id}")
            launch_ledger.append({
                "start": start,
                "terminal_diagnostic": (matching_ends[0]
                                        if len(matching_ends) == 1 else None),
                "evidence_unavailable": unavailable_launch})
            if len(matching_ends) + bool(unavailable_launch) != 1:
                findings.append(
                    f"worker launch {start_id!r} for claim {claim_id!r} needs "
                    "exactly one terminal diagnostic or evidence-unavailable record")
        if failed:
            diagnostic_events = [
                row for row in launch_ends
                if (row.get("exit") not in (None, 0) or
                    row.get("result") not in (None, "LAUNCHED"))
                and (row.get("stderr") or row.get("stdout") or row.get("detail"))]
            diagnostic = ((outcome.get("diagnostic_ref") or
                           outcome.get("recovery_ref")) or
                          diagnostic_events or None)
            diagnostic_unavailable = evidence_unavailable(
                evidence, kind="attempt-diagnostics", story=story,
                identity=identity)
            if not diagnostic and not diagnostic_unavailable:
                findings.append(
                    f"failed claim {claim_id!r} for Story {story} needs durable "
                    "diagnostics/recovery evidence or an evidence-unavailable record")
        rows.append({"story": story, "claim_event_id": claim_id,
                     "trace_id": trace, "terminal_outcome": outcome,
                     "evidence_unavailable": unavailable,
                     "diagnostic": diagnostic,
                     "diagnostic_evidence_unavailable": diagnostic_unavailable,
                     "launches": launch_starts,
                     "launch_ledger": launch_ledger})
    if any(row.get("event_id") is None for row in claims):
        findings.append("every claim needs a durable event ID")
    return rows, findings


def delivered_identity(outcome):
    try:
        detail = json.loads(outcome.get("detail") or "")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(detail, dict):
        return None
    pr, head = detail.get("pull_request"), detail.get("head")
    return (pr, head) if pr and head else None


def review_ledger(evidence, process, numbers, attempts):
    findings = []
    grouped = {}
    for row in process:
        if (row.get("event") == "delivery.pull-request.written" and
                row.get("story") in numbers):
            key = (row.get("trace_id"), row.get("pull_request"), row.get("head"))
            grouped.setdefault(key, []).append(row)
    produced = []
    for key in sorted(grouped, key=lambda value: tuple(str(v) for v in value)):
        candidates = grouped[key]
        durable = unique([row for row in candidates if row.get("event_id")],
                         lambda row: row.get("event_id"))
        if len(durable) > 1:
            findings.append(
                f"PR/head production evidence {key!r} has conflicting durable event IDs")
        produced.append(sorted(durable, key=lambda row: row["event_id"])[0]
                        if durable else candidates[0])
    observed_pairs = {(row.get("pull_request"), row.get("head")) for row in produced}
    expected = []
    for attempt in attempts:
        outcome = attempt.get("terminal_outcome") or {}
        if outcome.get("result") == "LAUNCHED":
            matching = [row for row in produced
                        if row.get("trace_id") == attempt.get("trace_id")]
            identity = ((matching[0].get("pull_request"), matching[0].get("head"))
                        if len(matching) == 1 else delivered_identity(outcome))
            if len(matching) != 1:
                pr, head = identity or (None, None)
                produced.append({"story": attempt.get("story"),
                                 "pull_request": pr, "head": head,
                                 "production_evidence_missing": True})
    for story in evidence.get("stories") or []:
        pr, head = story.get("pull_request"), story.get("head")
        if head:
            expected.append((story.get("number"), (pr, head)))
        elif pr and not any(row.get("pull_request") == pr for row in produced):
            expected.append((story.get("number"), (pr, None)))
    for story, identity in expected:
        pr, head = identity or (None, None)
        if (pr, head) not in observed_pairs:
            produced.append({"story": story, "pull_request": pr, "head": head})
            observed_pairs.add((pr, head))
    rows = []
    for item in produced:
        pr, head, story = (item.get("pull_request"), item.get("head"),
                           item.get("story"))
        review_candidates = [
            row for row in process
            if row.get("event") == REVIEW_OUTCOME_EVENT
            and row.get("pull_request") == pr and row.get("head") == head
            and row.get("verdict") in ("approval", "findings")]
        review_groups = {}
        for row in review_candidates:
            review_groups.setdefault(
                (row.get("pull_request"), row.get("head"), row.get("verdict")),
                []).append(row)
        outcomes = []
        for key in sorted(review_groups, key=lambda value: tuple(str(v) for v in value)):
            candidates = review_groups[key]
            durable = unique([row for row in candidates if row.get("event_id")],
                             lambda row: row.get("event_id"))
            if len(durable) > 1:
                findings.append(
                    f"exact-head review evidence {key!r} has conflicting durable event IDs")
            outcomes.append(sorted(durable, key=lambda row: row["event_id"])[0]
                            if durable else candidates[0])
        identity = f"{pr}:{head}"
        if not item.get("production_evidence_missing") and not item.get("event_id"):
            findings.append(
                f"PR/head production event for {identity} needs a durable event ID")
        unavailable = evidence_unavailable(
            evidence, kind="exact-head-review", story=story, identity=identity)
        if item.get("production_evidence_missing"):
            findings.append(
                f"successful attempt for Story {story} lacks a matching durable "
                f"PR/head production event for {identity}")
        if (not pr or not head or len(outcomes) + bool(unavailable) != 1):
            findings.append(
                f"produced PR {pr!r} head {head!r} needs exactly one exact-head "
                "independent review outcome or evidence-unavailable record")
        if len(outcomes) == 1 and not outcomes[0].get("event_id"):
            findings.append(
                f"exact-head review outcome for PR {pr!r} head {head!r} "
                "needs a durable event ID")
        rows.append({"story": story, "pull_request": pr, "head": head,
                     "review_outcome": outcomes[0] if len(outcomes) == 1 else None,
                     "evidence_unavailable": unavailable})
    return rows, findings


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

    ledger, ledger_findings = attempt_ledger(evidence, process, numbers)
    reviews, review_findings = review_ledger(evidence, process, numbers, ledger)
    integrity.extend(ledger_findings)
    integrity.extend(review_findings)
    claims = [row for row in process if row.get("event") == "story.claimed"
              and row.get("story") in numbers]
    claims = unique(claims, lambda row: (row.get("story"), row.get("event_id")))
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
                and row.get("result") == "approved" and not row.get("superseded")]
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
        "measurement_integrity": not integrity,
    }
    evidence_complete = not ledger_findings and not review_findings
    verdict = ("INCONCLUSIVE" if not evidence_complete else
               "PASS" if all(threshold_results.values()) else "FAIL")
    if not evidence_complete:
        unavailable_kpi = unavailable("attempt ledger is incomplete")
        autonomy = unavailable_kpi
        attempts_kpi = unavailable_kpi
    else:
        attempts_kpi = {
            "attempts": attempts, "stories": len(numbers),
            "attempts_by_story": attempts_by_story,
            "attempts_per_story": attempts / len(numbers) if numbers else UNAVAILABLE,
            "retries": retries,
            "retry_share_of_attempts": retries / attempts if attempts else UNAVAILABLE,
        }

    return {
        "schema_version": 1,
        "rung": 2,
        "report_phase": "final",
        "repository": evidence.get("repository"),
        "commitment": evidence.get("commitment"),
        "project": evidence.get("project"),
        "product_outcome": evidence.get("product_outcome"),
        "rung_verdict": verdict,
        "attempt_ledger": ledger,
        "review_ledger": reviews,
        "qualification_series": {
            "eligible": verdict != "INCONCLUSIVE",
            "pass_samples": 1 if verdict == "PASS" else 0,
            "fail_samples": 1 if verdict == "FAIL" else 0},
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
            "worker_attempts_retry_rate": attempts_kpi,
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
    autonomy = k["autonomy"]
    attempts = k["worker_attempts_retry_rate"]
    autonomy_text = (f"{autonomy['autonomous_stories']}/{autonomy['stories']} = "
                     f"{percent(autonomy['rate'])}"
                     if "autonomous_stories" in autonomy else UNAVAILABLE)
    attempts_text = (f"{attempts['attempts']} "
                     f"({attempts['attempts_per_story']} per Story)"
                     if "attempts" in attempts else UNAVAILABLE)
    return "\n".join([
        "# Phase 5 Rung 2 final report", "",
        f"**Verdict: {report['rung_verdict']}.** Product outcome: {report['product_outcome']}.", "",
        "| KPI | Result |", "|---|---|",
        f"| Human touches | {k['human_touches']['canonical_bells']} canonical bells; relay {k['human_touches']['relay']} |",
        f"| Autonomy | {autonomy_text} |",
        f"| Attempts | {attempts_text} |",
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
