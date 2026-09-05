#!/usr/bin/env python3
"""Generate a deterministic Phase 5 Rung 2 KPI report from frozen evidence."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

from factory.acceptance import rung1_report as shared

UNAVAILABLE = shared.UNAVAILABLE
STORY_BELLS = ("plan-approval", "poison-rescue", "acceptance")
AUTONOMY_BREAKING_ACTIONS = {"recovery", "code-intervention"}
TERMINAL_WORKER_EVENT = "worker.outcome"
REVIEW_OUTCOME_EVENT = "review.outcome.published"
TERMINAL_WORKER_RESULTS = {
    "LAUNCHED", "FAILED", "TERMINAL_FAILURE", "AMBIGUOUS",
    "NO_ELIGIBLE_WORKER", "NO_WORKER_LAUNCHED",
}
FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REPOSITORY_SLUG = re.compile(r"[^/\s]+/[^/\s]+")


def unavailable(reason):
    return shared.unavailable(reason)


def valid_commit_id(value):
    return bool(isinstance(value, str) and FULL_COMMIT_ID.fullmatch(value)
                and set(value) != {"0"})


def valid_pull_request(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def valid_repository(value):
    return isinstance(value, str) and bool(REPOSITORY_SLUG.fullmatch(value))


def valid_event_id(value):
    return isinstance(value, str) and bool(value.strip())


def valid_terminal_worker_outcome(row, *, allow_missing_executable=False):
    result = row.get("result")
    if not isinstance(result, str) or result not in TERMINAL_WORKER_RESULTS:
        return False
    exit_code = row.get("exit")
    valid_worker = (isinstance(row.get("worker"), str) and
                    bool(row["worker"].strip()))
    valid_exit = (exit_code is None or
                  (isinstance(exit_code, int) and
                   not isinstance(exit_code, bool)))
    if not valid_exit:
        return False
    if result == "LAUNCHED":
        return exit_code == 0 and valid_worker
    if result in {"NO_ELIGIBLE_WORKER", "NO_WORKER_LAUNCHED"}:
        return row.get("worker") is None and exit_code is None
    if result == "AMBIGUOUS":
        return valid_worker and exit_code is None
    if result == "TERMINAL_FAILURE":
        return valid_worker and exit_code is not None and exit_code != 0
    if valid_worker and exit_code is not None and exit_code != 0:
        return True
    return bool(
        allow_missing_executable and result == "FAILED" and valid_worker and
        exit_code is None and isinstance(row.get("detail"), str) and
        re.fullmatch(
            r"not launchable: \[Errno 2\] No such file or directory: .+",
            row["detail"]))


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


def deterministic_copies(rows, key, findings, description):
    groups = {}
    for row in rows:
        marker = key(row)
        encoded = json.dumps(marker, sort_keys=True, default=str)
        groups.setdefault(encoded, {"marker": marker, "rows": []})["rows"].append(row)
    result = []
    for encoded in sorted(groups):
        marker = groups[encoded]["marker"]
        candidates = groups[encoded]["rows"]
        encodings = {
            json.dumps(row, sort_keys=True, default=str)
            for row in candidates}
        if len(encodings) > 1:
            findings.append(
                f"{description} {marker!r} has conflicting durable copies")
        result.append(sorted(
            candidates,
            key=lambda row: json.dumps(row, sort_keys=True, default=str))[0])
    return result


def evidence_unavailable(evidence, *, kind, story, identity):
    matches = [row for row in evidence.get("evidence_unavailable", [])
               if isinstance(row, dict) and row.get("kind") == kind
               and row.get("story") == story and row.get("identity") == identity
               and row.get("reason")]
    return matches[0] if len(matches) == 1 else None


def attempt_ledger(evidence, process, numbers):
    repository = evidence.get("repository")
    rows, findings = [], []
    legacy_claim_ids = {
        (row.get("story"), row.get("event_id"))
        for row in process
        if (row.get("event") == "story.claimed" and
            row.get("repo") == repository and row.get("story") in numbers and
            "trace_id" not in row and valid_event_id(row.get("event_id")))}
    attempt_events = {
        TERMINAL_WORKER_EVENT, "worker.launch.start", "worker.launch.end",
        "worker.failover", "delivery.pull-request.written",
    }
    for row in process:
        if (row.get("event") in attempt_events and
                row.get("repo") == repository and row.get("story") in numbers and
                not valid_event_id(row.get("trace_id")) and
                not (row.get("event") == TERMINAL_WORKER_EVENT and
                     (row.get("story"), row.get("claim_event_id")) in
                     legacy_claim_ids)):
            findings.append(
                f"{row.get('event')} event for Story {row.get('story')} needs a "
                "nonempty string attempt trace ID")
    terminal_event_ids = {}
    unhashed_context = {
        "schema_version", "record_type", "timestamp", "event_id", "trace_id",
        "repo", "commitment", "component", "span_id", "parent_span_id",
    }
    for row in process:
        if (row.get("event") == TERMINAL_WORKER_EVENT and
                row.get("repo") == repository and row.get("story") in numbers and
                valid_event_id(row.get("event_id"))):
            producer_payload = {
                key: value for key, value in row.items()
                if key not in unhashed_context}
            encoded = json.dumps(
                producer_payload, sort_keys=True, default=str)
            terminal_event_ids.setdefault(row["event_id"], set()).add(encoded)
    for event_id, payloads in sorted(terminal_event_ids.items()):
        if len(payloads) > 1:
            findings.append(
                f"terminal worker outcome event ID {event_id!r} identifies "
                "conflicting producer payloads")
    valid_failover = {
        "NOT_NEEDED": {"LAUNCHED"},
        "SUPPRESSED": {"AMBIGUOUS", "TERMINAL_FAILURE"},
        "FELL_BACK": {"FAILED"},
        "EXHAUSTED": {"FAILED"},
    }
    scoped_failovers = []
    for row in process:
        if (row.get("event") == "worker.failover" and
                row.get("repo") == repository and row.get("story") in numbers):
            scoped_failovers.append(row)
            if not valid_event_id(row.get("event_id")):
                findings.append(
                    f"worker failover for Story {row.get('story')} needs a "
                    "durable nonempty string event ID")
            if not valid_event_id(row.get("worker")):
                findings.append(
                    f"worker failover for Story {row.get('story')} needs a "
                    "nonempty string worker identity")
            if row.get("result") not in valid_failover.get(
                    row.get("decision"), set()):
                findings.append(
                    f"worker failover for Story {row.get('story')} has an "
                    "inconsistent decision and result")
    failover_event_ids = {}
    for row in scoped_failovers:
        if valid_event_id(row.get("event_id")):
            producer_payload = {
                key: value for key, value in row.items()
                if key not in unhashed_context}
            encoded = json.dumps(
                producer_payload, sort_keys=True, default=str)
            failover_event_ids.setdefault(row["event_id"], set()).add(encoded)
    for event_id, payloads in sorted(failover_event_ids.items()):
        if len(payloads) > 1:
            findings.append(
                f"worker failover event ID {event_id!r} identifies conflicting "
                "producer payloads")
    claim_groups = {}
    for row in process:
        if (row.get("event") == "story.claimed" and
                row.get("story") in numbers and
                row.get("repo") == repository):
            if not valid_event_id(row.get("event_id")):
                findings.append(
                    f"claim {row.get('event_id')!r} for Story "
                    f"{row.get('story')} needs a durable nonempty string event ID")
                continue
            if ("trace_id" in row and
                    not valid_event_id(row.get("trace_id"))):
                findings.append(
                    f"claim {row.get('event_id')!r} for Story "
                    f"{row.get('story')} needs a nonempty string attempt trace ID")
            claim_groups.setdefault(
                (row.get("story"), row.get("event_id")), []).append(row)
    selected_claims = []
    for key in sorted(claim_groups, key=lambda value: tuple(str(v) for v in value)):
        candidates = claim_groups[key]
        encodings = {
            json.dumps(row, sort_keys=True, default=str) for row in candidates}
        if len(encodings) > 1:
            findings.append(
                f"claim {key[1]!r} for Story {key[0]} has conflicting "
                "durable copies")
        trace_identities = {
            json.dumps(row.get("trace_id"), sort_keys=True, default=str)
            for row in candidates}
        if len(trace_identities) > 1:
            findings.append(
                f"claim {key[1]!r} for Story {key[0]} has conflicting trace identities")
        selected_claims.append(sorted(
            candidates,
            key=lambda row: json.dumps(row, sort_keys=True, default=str))[0])
    for row in selected_claims:
        if ("trace_id" in row and
                not valid_event_id(row.get("trace_id"))):
            findings.append(
                f"claim {row.get('event_id')!r} for Story {row.get('story')} "
                "needs a nonempty string attempt trace ID")
    trace_groups = {}
    for row in selected_claims:
        trace_id = (row.get("trace_id")
                    if valid_event_id(row.get("trace_id")) else None)
        trace_key = ((row.get("story"), "trace", trace_id)
                     if trace_id else
                     (row.get("story"), "event", row.get("event_id")))
        trace_groups.setdefault(trace_key, []).append(row)
    claims = []
    for key in sorted(trace_groups, key=lambda value: tuple(str(v) for v in value)):
        candidates = trace_groups[key]
        if key[1] == "trace" and len(candidates) > 1:
            findings.append(
                f"attempt trace {key[2]!r} for Story {key[0]} has multiple claim IDs")
        claims.append(sorted(
            candidates,
            key=lambda row: json.dumps(row, sort_keys=True, default=str))[0])
    claims.sort(key=lambda row: (
        row.get("story"), str(row.get("trace_id") or ""),
        str(row.get("event_id") or "")))
    claimed_traces = {
        (row.get("story"), row.get("trace_id"))
        for row in claims if valid_event_id(row.get("trace_id"))}
    worker_traces = {
        (row.get("story"), row.get("trace_id"))
        for row in process
        if row.get("event") in
        (TERMINAL_WORKER_EVENT, "worker.launch.start", "worker.launch.end",
         "worker.failover", "delivery.pull-request.written")
        and row.get("repo") == repository and row.get("story") in numbers
        and valid_event_id(row.get("trace_id"))}
    for story, trace in sorted(worker_traces - claimed_traces):
        findings.append(
            f"worker attempt trace {trace!r} for Story {story} has no matching "
            "durable claim")
    claimed_stories = {row.get("story") for row in claims}
    for story in sorted(numbers - claimed_stories):
        findings.append(f"Story {story} has no durable attempt claim evidence")
    for claim in claims:
        story, claim_id = claim.get("story"), claim.get("event_id")
        trace = (claim.get("trace_id")
                 if valid_event_id(claim.get("trace_id")) else None)
        identity = trace or claim_id
        outcome_groups = {}
        for row in process:
            if (row.get("event") == TERMINAL_WORKER_EVENT and
                    row.get("repo") == repository and
                    row.get("story") == story and
                    ((trace and row.get("trace_id") == trace) or
                     (not trace and row.get("claim_event_id") == claim_id))):
                if not valid_event_id(row.get("event_id")):
                    findings.append(
                        f"terminal worker outcome for claim {claim_id!r} needs "
                        "a durable nonempty string event ID")
                    continue
                outcome_groups.setdefault(
                    (json.dumps(row.get("trace_id"), sort_keys=True, default=str),
                     row.get("event_id")), []).append(row)
        outcomes = []
        for key in sorted(
                outcome_groups,
                key=lambda value: tuple(str(v) for v in value)):
            candidates = outcome_groups[key]
            encodings = {
                json.dumps(row, sort_keys=True, default=str)
                for row in candidates}
            if len(encodings) > 1:
                findings.append(
                    f"terminal outcome {key[1]!r} for claim {claim_id!r} "
                    "has conflicting durable copies")
            outcomes.append(sorted(
                candidates,
                key=lambda row: json.dumps(row, sort_keys=True, default=str))[0])
        unavailable = evidence_unavailable(
            evidence, kind="attempt-terminal-outcome", story=story,
            identity=identity)
        if len(outcomes) + bool(unavailable) != 1:
            findings.append(
                f"claim {claim_id!r} for Story {story} needs exactly one "
                "terminal worker outcome or evidence-unavailable record")
        outcome = outcomes[0] if len(outcomes) == 1 else None
        if outcome and not valid_event_id(outcome.get("event_id")):
            findings.append(
                f"terminal worker outcome for claim {claim_id!r} needs a durable "
                "nonempty string event ID")
        if outcome and not valid_terminal_worker_outcome(outcome):
            findings.append(
                f"terminal worker outcome for claim {claim_id!r} needs a recognized "
                "result with a consistent worker and exit status")
        failed = bool(outcome and (outcome.get("exit") not in (None, 0) or
                                  outcome.get("result") != "LAUNCHED"))
        diagnostic = None
        diagnostic_unavailable = None
        launch_starts = deterministic_copies(
            [row for row in process
             if row.get("event") == "worker.launch.start"
             and row.get("repo") == repository
             and row.get("story") == story and trace
             and row.get("trace_id") == trace],
            lambda row: (row.get("trace_id"), row.get("event_id")),
            findings, "worker launch start")
        launch_starts.sort(key=lambda row: (
            str(row.get("trace_id") or ""), str(row.get("span_id") or ""),
            str(row.get("worker") or ""), str(row.get("event_id") or "")))
        failovers = deterministic_copies(
            [row for row in scoped_failovers
             if trace and row.get("story") == story
             and row.get("trace_id") == trace],
            lambda row: (row.get("trace_id"), row.get("event_id")),
            findings, "worker failover")
        failovers.sort(key=lambda row: (
            str(row.get("trace_id") or ""), str(row.get("worker") or ""),
            str(row.get("event_id") or "")))
        for failover in failovers:
            matching_starts = [
                row for row in launch_starts
                if valid_event_id(failover.get("worker"))
                and row.get("worker") == failover.get("worker")]
            if len(matching_starts) != 1:
                findings.append(
                    f"worker failover {failover.get('event_id')!r} for claim "
                    f"{claim_id!r} needs exactly one matching worker launch")
            if failover.get("decision") in {"NOT_NEEDED", "SUPPRESSED"} and (
                    not unavailable and
                    (not outcome or
                     failover.get("worker") != outcome.get("worker") or
                     failover.get("result") != outcome.get("result"))):
                findings.append(
                    f"terminal worker failover {failover.get('event_id')!r} for "
                    f"claim {claim_id!r} must match its terminal worker outcome")
            if failover.get("decision") == "EXHAUSTED" and (
                    not unavailable and
                    (not outcome or
                     outcome.get("result") != "NO_WORKER_LAUNCHED" or
                     outcome.get("worker") is not None)):
                findings.append(
                    f"exhausted worker failover {failover.get('event_id')!r} for "
                    f"claim {claim_id!r} must end in NO_WORKER_LAUNCHED")
            if failover.get("decision") == "FELL_BACK":
                next_worker = failover.get("next")
                next_launches = [
                    row for row in launch_starts
                    if valid_event_id(next_worker)
                    and row.get("worker") == next_worker
                    and next_worker != failover.get("worker")]
                if len(next_launches) != 1:
                    findings.append(
                        f"fallback decision {failover.get('event_id')!r} for "
                        f"claim {claim_id!r} needs exactly one launch of its "
                        "nonempty next-worker identity")
        launch_ends = deterministic_copies(
            [row for row in process
             if row.get("event") == "worker.launch.end"
             and row.get("repo") == repository
             and row.get("story") == story
             and trace and row.get("trace_id") == trace],
            lambda row: (row.get("trace_id"), row.get("event_id")),
            findings, "worker launch end")
        launch_ends.sort(key=lambda row: (
            str(row.get("trace_id") or ""), str(row.get("span_id") or ""),
            str(row.get("worker") or ""), str(row.get("event_id") or "")))
        reconciled_ends = []
        for end in launch_ends:
            if not valid_event_id(end.get("event_id")):
                findings.append(
                    f"worker launch end for claim {claim_id!r} needs a durable "
                    "nonempty string event ID")
            if not valid_event_id(end.get("span_id")):
                findings.append(
                    f"worker launch end {end.get('event_id')!r} for claim "
                    f"{claim_id!r} needs a nonempty string span ID")
            if not valid_terminal_worker_outcome(
                    end, allow_missing_executable=True):
                findings.append(
                    f"worker launch end {end.get('event_id')!r} for claim "
                    f"{claim_id!r} needs a recognized result with a consistent "
                    "worker and exit status")
            matching_starts = [
                row for row in launch_starts
                if valid_event_id(row.get("event_id"))
                and valid_event_id(end.get("span_id"))
                and valid_event_id(row.get("span_id"))
                and row.get("span_id") == end.get("span_id")
                and end.get("worker")
                and row.get("worker") == end.get("worker")]
            if len(matching_starts) != 1:
                findings.append(
                    f"worker launch end {end.get('event_id')!r} for claim "
                    f"{claim_id!r} needs exactly one matching durable launch start")
            else:
                reconciled_ends.append(end)
        for failover in failovers:
            matching_ends = [
                row for row in reconciled_ends
                if valid_event_id(failover.get("worker"))
                and row.get("worker") == failover.get("worker")]
            if (len(matching_ends) != 1 or
                    matching_ends[0].get("result") != failover.get("result")):
                findings.append(
                    f"worker failover {failover.get('event_id')!r} for claim "
                    f"{claim_id!r} must match exactly one terminal launch result")
        launch_ledger = []
        launch_evidence_unavailable = evidence_unavailable(
            evidence, kind="attempt-launches", story=story, identity=identity)
        for start in launch_starts:
            start_id, span = start.get("event_id"), start.get("span_id")
            worker = start.get("worker")
            if not valid_event_id(start_id):
                findings.append(
                    f"worker launch start for claim {claim_id!r} needs a durable "
                    "string event ID")
            if not valid_event_id(span):
                findings.append(
                    f"worker launch {start_id!r} for claim {claim_id!r} needs "
                    "a nonempty string span ID")
            if not valid_event_id(worker):
                findings.append(
                    f"worker launch {start_id!r} for claim {claim_id!r} needs "
                    "a worker identity as a nonempty string")
            matching_ends = [
                row for row in reconciled_ends
                if valid_event_id(span) and row.get("span_id") == span
                and valid_event_id(worker) and row.get("worker") == worker]
            usable_ends = [
                row for row in matching_ends
                if valid_terminal_worker_outcome(
                    row, allow_missing_executable=True)
                and ((row.get("result") == "LAUNCHED" and row.get("exit") == 0) or
                     row.get("stderr") or row.get("stdout") or row.get("detail"))]
            unavailable_launch = evidence_unavailable(
                evidence, kind="attempt-launch-diagnostics", story=story,
                identity=f"{identity}:{start_id}")
            matching_failovers = [
                row for row in failovers
                if valid_event_id(worker) and row.get("worker") == worker]
            unavailable_failover = evidence_unavailable(
                evidence, kind="attempt-failover", story=story,
                identity=f"{identity}:{start_id}")
            launch_ledger.append({
                "start": start,
                "terminal_diagnostic": (usable_ends[0]
                                        if len(usable_ends) == 1 else None),
                "evidence_unavailable": unavailable_launch,
                "failover": (matching_failovers[0]
                             if len(matching_failovers) == 1 else None),
                "failover_evidence_unavailable": unavailable_failover})
            if len(usable_ends) + bool(unavailable_launch) != 1:
                findings.append(
                    f"worker launch {start_id!r} for claim {claim_id!r} needs "
                    "exactly one terminal diagnostic or evidence-unavailable record")
            if len(matching_failovers) + bool(unavailable_failover) != 1:
                findings.append(
                    f"worker launch {start_id!r} for claim {claim_id!r} needs "
                    "exactly one failover decision or evidence-unavailable record")
        all_successful_launches = [
            row for row in launch_ledger
            if row.get("terminal_diagnostic")
            and row["terminal_diagnostic"].get("result") == "LAUNCHED"
            and row["terminal_diagnostic"].get("exit") == 0]
        if outcome and outcome.get("result") == "LAUNCHED":
            matching_successful_launches = [
                row for row in all_successful_launches
                if row["start"].get("worker") == outcome.get("worker")]
            if (len(all_successful_launches) +
                    bool(launch_evidence_unavailable) != 1):
                findings.append(
                    f"successful claim {claim_id!r} for Story {story} needs "
                    "exactly one successful worker-specific launch overall or "
                    "evidence-unavailable record")
            elif (all_successful_launches and
                  len(matching_successful_launches) != 1):
                findings.append(
                    f"successful claim {claim_id!r} for Story {story} needs its "
                    "single successful launch to match the terminal worker")
        elif outcome and all_successful_launches:
            findings.append(
                f"non-successful claim {claim_id!r} for Story {story} cannot "
                "contain a successful worker launch")
        elif (outcome and
              (outcome.get("worker") or
               outcome.get("result") == "NO_WORKER_LAUNCHED") and
              not launch_starts and
              not launch_evidence_unavailable):
            findings.append(
                f"worker outcome for claim {claim_id!r} in Story {story} needs "
                "durable launch evidence or an evidence-unavailable record")
        if failed:
            diagnostic_events = [
                row for row in reconciled_ends
                if (row.get("exit") not in (None, 0) or
                    row.get("result") not in (None, "LAUNCHED"))
                and (row.get("stderr") or row.get("stdout") or row.get("detail"))]
            diagnostic = ((outcome.get("diagnostic_ref") or
                           outcome.get("recovery_ref") or
                           (outcome.get("detail") if outcome.get("result") ==
                            "NO_ELIGIBLE_WORKER" else None)) or
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
                     "launch_evidence_unavailable": launch_evidence_unavailable,
                     "launch_ledger": launch_ledger})
    if any(not valid_event_id(row.get("event_id")) for row in claims):
        findings.append("every claim needs a durable nonempty string event ID")
    return rows, findings


def delivered_identity(outcome, findings=None, story=None):
    try:
        detail = json.loads(outcome.get("detail") or "")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(detail, dict):
        return None
    pr, head = detail.get("pull_request"), detail.get("head")
    if "pull_request" not in detail and "head" not in detail:
        return None
    if not valid_pull_request(pr) or not valid_commit_id(head):
        if findings is not None:
            findings.append(
                f"successful attempt for Story {story} has malformed terminal "
                "PR/head identity")
        return None
    return pr, head


def review_ledger(evidence, process, numbers, attempts):
    findings = []
    repository = evidence.get("repository")
    delivery_capable_attempts = {
        (attempt.get("story"), attempt.get("trace_id"))
        for attempt in attempts
        if valid_terminal_worker_outcome(attempt.get("terminal_outcome") or {})
        and attempt["terminal_outcome"].get("result") == "LAUNCHED"}
    scoped_reviews = [
        row for row in process
        if row.get("event") == REVIEW_OUTCOME_EVENT
        and row.get("repo") == repository and row.get("story") in numbers]
    durable_reviews = deterministic_copies(
        [row for row in scoped_reviews if valid_event_id(row.get("event_id"))],
        lambda row: (row.get("event_id"),), findings,
        "scoped review event")
    legacy_reviews = sorted(
        [row for row in scoped_reviews if not valid_event_id(row.get("event_id"))],
        key=lambda row: json.dumps(row, sort_keys=True, default=str))
    scoped_reviews = durable_reviews + legacy_reviews
    grouped = {}
    production_ids = {}
    for row in process:
        if (row.get("event") == "delivery.pull-request.written" and
                row.get("story") in numbers and row.get("repo") == repository):
            production_invalid = False
            identity = f"{row.get('pull_request')}:{row.get('head')}"
            if not valid_pull_request(row.get("pull_request")):
                findings.append(
                    f"PR/head production event for {identity} needs a positive "
                    "integer PR number")
                production_invalid = True
            if not valid_commit_id(row.get("head")):
                findings.append(
                    f"PR/head production event for {identity} needs a full commit SHA")
                production_invalid = True
            if not valid_event_id(row.get("trace_id")):
                findings.append(
                    f"Story {row.get('story')} PR/head production event needs a "
                    "nonempty string attempt trace ID")
                production_invalid = True
            elif ((row.get("story"), row.get("trace_id")) not in
                  delivery_capable_attempts):
                findings.append(
                    f"Story {row.get('story')} PR/head production event for "
                    f"{identity} needs a matching successful attempt outcome")
                production_invalid = True
            if production_invalid:
                continue
            key = (row.get("story"), row.get("trace_id"),
                   row.get("pull_request"), row.get("head"))
            grouped.setdefault(key, []).append(row)
            if valid_event_id(row.get("event_id")):
                production_ids.setdefault(row["event_id"], set()).add(key)
    for event_id, identities in sorted(production_ids.items()):
        if len(identities) > 1:
            findings.append(
                f"PR/head production event ID {event_id!r} is reused across "
                "multiple attempt or delivery identities")
    produced = []
    for key in sorted(grouped, key=lambda value: tuple(str(v) for v in value)):
        candidates = grouped[key]
        durable = deterministic_copies(
            [row for row in candidates if valid_event_id(row.get("event_id"))],
            lambda row: (row.get("event_id"),), findings,
            "PR/head production event")
        if len(durable) > 1:
            findings.append(
                f"PR/head production evidence {key!r} has conflicting durable event IDs")
        produced.append(sorted(durable, key=lambda row: row["event_id"])[0]
                        if durable else sorted(
                            candidates,
                            key=lambda row: json.dumps(
                                row, sort_keys=True, default=str))[0])
    observed_identities = {
        (row.get("story"), row.get("pull_request"), row.get("head"))
        for row in produced}
    expected = []
    for attempt in attempts:
        outcome = attempt.get("terminal_outcome") or {}
        if outcome.get("result") == "LAUNCHED":
            matching = [row for row in produced
                        if row.get("story") == attempt.get("story")
                        and row.get("trace_id") == attempt.get("trace_id")]
            outcome_identity = delivered_identity(
                outcome, findings, attempt.get("story"))
            identity = ((matching[0].get("pull_request"), matching[0].get("head"))
                        if len(matching) == 1 else outcome_identity)
            if (len(matching) == 1 and outcome_identity and
                    identity != outcome_identity):
                findings.append(
                    f"successful attempt for Story {attempt.get('story')} has "
                    "conflicting terminal and production PR/head identities")
            if len(matching) != 1:
                pr, head = identity or (None, None)
                produced.append({"story": attempt.get("story"),
                                 "pull_request": pr, "head": head,
                                 "production_evidence_missing": True})
                observed_identities.add((attempt.get("story"), pr, head))
    for story in evidence.get("stories") or []:
        pr, head = story.get("pull_request"), story.get("head")
        valid_declared_pr = ("pull_request" not in story or
                             valid_pull_request(pr))
        valid_declared_head = ("head" not in story or valid_commit_id(head))
        if "pull_request" in story and not valid_pull_request(pr):
            findings.append(
                f"Story {story.get('number')} declares an invalid pull-request number")
        if "head" in story and not valid_commit_id(head):
            findings.append(
                f"Story {story.get('number')} declares an invalid commit head")
        if not valid_declared_pr or not valid_declared_head:
            continue
        if head:
            expected.append((story.get("number"), (pr, head)))
        elif pr and not any(row.get("story") == story.get("number") and
                            row.get("pull_request") == pr for row in produced):
            expected.append((story.get("number"), (pr, None)))
    for story, identity in expected:
        pr, head = identity or (None, None)
        if (story, pr, head) not in observed_identities:
            produced.append({"story": story, "pull_request": pr, "head": head})
            observed_identities.add((story, pr, head))
    for story in sorted(numbers):
        if not any(row.get("story") == story for row in produced):
            produced.append({"story": story, "pull_request": None, "head": None,
                             "story_delivery_missing": True})
    rows = []
    for item in produced:
        pr, head, story = (item.get("pull_request"), item.get("head"),
                           item.get("story"))
        matching_review_rows = [
            row for row in scoped_reviews
            if row.get("story") == story
            and row.get("pull_request") == pr and row.get("head") == head]
        if any(row.get("verdict") not in ("approval", "findings")
               for row in matching_review_rows):
            findings.append(
                f"exact-head review evidence for PR {pr!r} head {head!r} "
                "contains an unrecognized verdict")
        review_candidates = [
            row for row in matching_review_rows
            if row.get("verdict") in ("approval", "findings")]
        durable_reviews = deterministic_copies(
            [row for row in review_candidates
             if valid_event_id(row.get("event_id"))],
            lambda row: (row.get("event_id"),), findings,
            "exact-head review event")
        legacy_reviews = sorted(
            [row for row in review_candidates
             if not valid_event_id(row.get("event_id"))],
            key=lambda row: json.dumps(row, sort_keys=True, default=str))
        review_candidates = durable_reviews + legacy_reviews
        review_groups = {}
        for row in review_candidates:
            review_groups.setdefault(
                (row.get("story"), row.get("pull_request"), row.get("head"),
                 row.get("verdict")),
                []).append(row)
        outcomes = []
        for key in sorted(review_groups, key=lambda value: tuple(str(v) for v in value)):
            candidates = review_groups[key]
            durable = unique([row for row in candidates
                              if valid_event_id(row.get("event_id"))],
                             lambda row: row.get("event_id"))
            if len(durable) > 1:
                findings.append(
                    f"exact-head review evidence {key!r} has conflicting durable event IDs")
            outcomes.append(sorted(durable, key=lambda row: row["event_id"])[0]
                            if durable else candidates[0])
        identity = f"{pr}:{head}"
        if pr is not None and not valid_pull_request(pr):
            findings.append(
                f"PR/head production event for {identity} needs a positive integer PR number")
        if head and not valid_commit_id(head):
            findings.append(
                f"PR/head production event for {identity} needs a full commit SHA")
        if (not item.get("production_evidence_missing") and
                not item.get("story_delivery_missing") and
                not valid_event_id(item.get("event_id"))):
            findings.append(
                f"PR/head production event for {identity} needs a durable "
                "nonempty string event ID")
        unavailable = evidence_unavailable(
            evidence, kind="exact-head-review", story=story, identity=identity)
        if item.get("production_evidence_missing"):
            findings.append(
                f"successful attempt for Story {story} lacks a matching durable "
                f"PR/head production event for {identity}")
        if item.get("story_delivery_missing"):
            findings.append(
                f"merged Story {story} lacks a durable PR/head production event")
        if (not pr or not head or len(outcomes) + bool(unavailable) != 1):
            findings.append(
                f"produced PR {pr!r} head {head!r} needs exactly one exact-head "
                "independent review outcome or evidence-unavailable record")
        if (len(outcomes) == 1 and
                not valid_event_id(outcomes[0].get("event_id"))):
            findings.append(
                f"exact-head review outcome for PR {pr!r} head {head!r} "
                "needs a durable nonempty string event ID")
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

    if valid_repository(evidence.get("repository")):
        ledger, ledger_findings = attempt_ledger(evidence, process, numbers)
        reviews, review_findings = review_ledger(
            evidence, process, numbers, ledger)
    else:
        ledger, reviews = [], []
        ledger_findings = [
            "qualification evidence needs a valid measured owner/name repository"]
        review_findings = []
    integrity.extend(ledger_findings)
    integrity.extend(review_findings)
    attempts_by_story = {
        number: sum(row.get("story") == number for row in ledger)
        for number in sorted(numbers)
    }
    attempts = len(ledger)
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
