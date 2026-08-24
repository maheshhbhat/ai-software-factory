#!/usr/bin/env python3
"""Read-only monitor for factory two-Story black-box UAT runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

STUCK_AFTER_SECONDS = 45


def read_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def read_jsonl(path: pathlib.Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def instant(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def choose_run(root: pathlib.Path, selected: str | None) -> pathlib.Path:
    if selected:
        path = pathlib.Path(selected).expanduser()
        return path if path.is_absolute() else root / path
    candidates = [path for path in (root / "runs" / "two-story-real").glob("*")
                  if path.is_dir()]
    if not candidates:
        raise FileNotFoundError("no runs/two-story-real run exists")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def active_operations(run: pathlib.Path) -> list[dict]:
    operations = read_jsonl(run / "observability" / "operations.jsonl")
    telemetry = read_jsonl(run / "observability" / "telemetry.jsonl")
    latest_operation: dict[str, dict] = {}
    latest_signal: dict[str, dict] = {}
    for row in operations:
        if row.get("span_id"):
            latest_operation[row["span_id"]] = row
    for row in telemetry:
        if row.get("span_id"):
            latest_signal[row["span_id"]] = row
    now = datetime.now(timezone.utc)
    active = []
    for span, row in latest_operation.items():
        if row.get("message") != "activity started":
            continue
        signal = latest_signal.get(span, row)
        seen = instant(signal.get("timestamp"))
        age = (now - seen).total_seconds() if seen else float("inf")
        active.append({**row, "age":age,
                       "health":"STUCK" if age >= STUCK_AFTER_SECONDS else "ACTIVE"})
    return sorted(active, key=lambda row: (row.get("story") or 0,
                                           row.get("component") or ""))


def completed_durations(run: pathlib.Path) -> list[dict]:
    rows = read_jsonl(run / "observability" / "operations.jsonl")
    return [row for row in rows
            if row.get("message") == "activity completed"
            and isinstance(row.get("elapsed_seconds"), (int, float))]


def failed_operations(run: pathlib.Path) -> list[dict]:
    return [row for row in read_jsonl(run / "observability" / "operations.jsonl")
            if row.get("message") == "activity failed"]


def engine_progress(run: pathlib.Path) -> list[dict]:
    """Newest bounded streamed line for each active engine identity."""
    rows = read_jsonl(run / "observability" / "operations.jsonl")
    latest = {}
    for row in rows:
        if row.get("operation") != "engine-stream" or row.get("message") != "engine output":
            continue
        identity = (row.get("component"), row.get("story"),
                    row.get("pull_request"), row.get("artifact"))
        latest[identity] = row
    return list(latest.values())


def progress_summary(value: str) -> str:
    """Turn one engine JSON event into useful bounded operator text."""
    try:
        event = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        text = str(value or "")
        if '"type":"result"' in text or '"type": "result"' in text:
            return "result (event exceeded display bound)"
        return text[-200:]
    kind = event.get("type", "event") if isinstance(event, dict) else "event"
    subtype = event.get("subtype") if isinstance(event, dict) else None
    if kind == "system" and subtype == "thinking_tokens":
        return f"thinking; estimated tokens={event.get('estimated_tokens', '?')}"
    if kind == "assistant":
        content = ((event.get("message") or {}).get("content") or [])
        texts = [item.get("text", "") for item in content
                 if isinstance(item, dict) and item.get("type") == "text"]
        return ("assistant: " + " ".join(texts))[-200:]
    if kind == "result":
        cost = event.get("total_cost_usd")
        return "result" + (f"; reported cost=${cost}" if cost is not None else "")
    return f"{kind}" + (f"/{subtype}" if subtype else "")


def snapshot(run: pathlib.Path) -> tuple[str, bool | None]:
    evidence = read_json(run / "evidence.json")
    state = evidence or read_json(run / "run-state.json")
    if not state:
        has_observability = any((run / "observability" / name).exists()
                                for name in ("operations.jsonl", "telemetry.jsonl"))
        if not has_observability:
            return f"run {run.name}: waiting for state", None
        state = {}
        lines = [f"run {run.name} — production observability"]
    else:
        lines = [f"run {state.get('run', run.name)} — Project #{state.get('project', '?')} — "
                 f"{state.get('project_state') or state.get('status', 'starting')}"]
    stories = state.get("stories", [])
    if stories and isinstance(stories[0], int):
        lines.append("fixtures: " + ", ".join(f"Story #{number}" for number in stories))
    else:
        for story in stories:
            walk = story.get("walk") or []
            current = walk[-1] if walk else "created"
            suffix = []
            if story.get("pull"):
                suffix.append(f"PR #{story['pull']}")
            if story.get("checks"):
                suffix.append("checks=" + ",".join(story["checks"]))
            if story.get("exact_approval"):
                suffix.append("exact-head-approved")
            timing = story.get("review_timing") or {}
            if timing.get("total_seconds") is not None:
                suffix.append(f"review={timing['total_seconds']}s")
            claimed = instant(story.get("claimed_at"))
            merged = instant(story.get("merged_at"))
            if claimed and merged:
                suffix.append(f"claim-to-merged={(merged-claimed).total_seconds():.1f}s")
            lines.append(f"Story #{story.get('number')}: {current}" +
                         (" — " + "; ".join(suffix) if suffix else ""))
    for row in active_operations(run):
        identity = (f"Story #{row['story']}" if row.get("story") else
                    f"Project #{row['project']}" if row.get("project") else "run")
        lines.append(f"{row['health']}: {identity} — {row.get('component')}/"
                     f"{row.get('operation')}:{row.get('stage')} — "
                     f"last signal {row['age']:.1f}s ago")
    for row in engine_progress(run):
        identity = (f"Story #{row['story']}" if row.get("story") else
                    f"PR #{row['pull_request']}" if row.get("pull_request") else
                    f"artifact #{row['artifact']}" if row.get("artifact") else "run")
        lines.append(f"progress: {identity} — {row.get('component')}: "
                     f"{progress_summary(row.get('engine_output_tail', ''))}")
    for row in failed_operations(run)[-5:]:
        identity = (f"Story #{row['story']}" if row.get("story") else
                    f"PR #{row['pull_request']}" if row.get("pull_request") else "run")
        detail = str(row.get("exception_message") or "unknown failure")[-200:]
        lines.append(f"FAILED: {identity} — {row.get('component')}/"
                     f"{row.get('operation')} — {detail}")
    durations = completed_durations(run)
    story_durations = [row for row in durations if row.get("story")]
    run_durations = [row for row in durations if not row.get("story")]
    for row in story_durations:
        identity = f"Story #{row['story']}" if row.get("story") else "run"
        lines.append(f"completed: {identity} — {row.get('component')}/"
                     f"{row.get('operation')} — {row['elapsed_seconds']:.1f}s")
    for row in run_durations[-5:]:
        lines.append(f"completed: run — {row.get('component')}/"
                     f"{row.get('operation')} — {row['elapsed_seconds']:.1f}s")
    verdict = evidence.get("passed") if evidence else None
    if evidence:
        lines.append(f"FINAL: {'PASS' if verdict else 'FAIL'} — {evidence.get('reason', '')}")
        lines.append("poller: stopped by harness before final evidence was written")
    return "\n".join(lines), verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--run", help="specific run directory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="print one snapshot")
    mode.add_argument("--follow", action="store_true", help="watch until final evidence")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=2700.0)
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root).resolve()
    try:
        run = choose_run(root, args.run)
    except FileNotFoundError as error:
        print(f"factory-monitor: {error}", file=sys.stderr)
        return 2
    follow = args.follow and not args.once
    deadline = time.monotonic() + args.timeout
    previous = ""
    while True:
        rendered, verdict = snapshot(run)
        digest = hashlib.sha256(rendered.encode()).hexdigest()
        if digest != previous:
            print(rendered, flush=True)
            previous = digest
        if verdict is not None:
            return 0 if verdict else 1
        if not follow:
            return 0
        if time.monotonic() >= deadline:
            print("factory-monitor: timeout before final evidence", file=sys.stderr)
            return 3
        time.sleep(max(args.interval, 0.25))


if __name__ == "__main__":
    raise SystemExit(main())
