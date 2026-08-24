#!/usr/bin/env python3
"""Dependency-free structured events, diagnostics, telemetry, and live activity.

The three streams are deliberately separate.  A lifecycle fact is not a log
message, a stack trace is not a metric, and a heartbeat is not durable factory
state.  Writers in this module make those category errors difficult to express.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import pathlib
import re
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

SCHEMA_VERSION = 1
HEARTBEAT_SECONDS = 5
STUCK_SECONDS = 15
NO_PROGRESS_SECONDS = 30
LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
FILES = {
    "process": "process-events.jsonl",
    "operation": "operations.jsonl",
    "telemetry": "telemetry.jsonl",
}
DEFAULT_RUN_DIR = pathlib.Path(__file__).resolve().parent / "logs" / "current"
MAX_STREAM_BYTES = 5 * 1024 * 1024
_WRITE_LOCK = threading.Lock()
_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)(authorization:\s*(?:bearer|token)\s+)\S+"),
)

_context = contextvars.ContextVar("factory_observability_context", default={})


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run_directory() -> pathlib.Path:
    configured = os.environ.get("FACTORY_RUN_DIR", "").strip()
    return pathlib.Path(configured) if configured else DEFAULT_RUN_DIR


def stream_path(kind: str) -> pathlib.Path:
    if kind not in FILES:
        raise ValueError(f"unknown observability stream: {kind}")
    return run_directory() / FILES[kind]


def redact(value):
    if not isinstance(value, str):
        return value
    text = value
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        secret = os.environ.get(name, "")
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: ((match.group(1) + "[redacted]")
                                          if match.groups() else "[redacted]"), text)
    return text


def _clean(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def _base(kind: str, **fields) -> dict:
    context = _context.get()
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": kind,
        "timestamp": now(),
        **context,
    }
    record.update({key: value for key, value in fields.items() if value is not None})
    return _clean(record)


def _validate(record: dict) -> None:
    kind = record.get("record_type")
    if kind == "process":
        if "event" not in record:
            raise ValueError("process event requires event")
        if "level" in record or "stack_trace" in record or "metric" in record:
            raise ValueError("process event contains diagnostic or telemetry fields")
    elif kind == "operation":
        if record.get("level") not in LEVELS or not record.get("message"):
            raise ValueError("operational log requires valid level and message")
        if "event" in record or "metric" in record:
            raise ValueError("operational log contains process or telemetry fields")
    elif kind == "telemetry":
        if not record.get("metric"):
            raise ValueError("telemetry requires metric")
        if "level" in record or "stack_trace" in record or "event" in record:
            raise ValueError("telemetry contains process or diagnostic fields")
    else:
        raise ValueError(f"invalid record type: {kind!r}")


def _append(kind: str, record: dict, *, sync: bool = False) -> dict:
    _validate(record)
    destination = stream_path(kind)
    destination.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), sort_keys=True, default=str)
    with _WRITE_LOCK:
        try:
            if destination.stat().st_size >= MAX_STREAM_BYTES:
                destination.replace(destination.with_suffix(destination.suffix + ".1"))
        except FileNotFoundError:
            pass
        with destination.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
    return record


def process_event(event: str, *, evidence: dict | None = None, **fields) -> dict:
    """Append one lifecycle fact.  It has no severity and no free-form message."""
    event_id = hashlib.sha256(json.dumps(
        {"event": event, "evidence": evidence, **fields}, sort_keys=True,
        default=str).encode()).hexdigest()[:32]
    return _append("process", _base("process", event=event, event_id=event_id,
                                     evidence=evidence, **fields), sync=True)


def operational_log(level: str, message: str, *, exc: BaseException | None = None,
                    stack_trace: str | None = None, **fields) -> dict:
    level = level.upper()
    if exc is not None:
        fields.update(exception_type=type(exc).__name__, exception_message=str(exc))
        stack_trace = stack_trace or "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__))
    record = _base("operation", level=level, message=message,
                   stack_trace=stack_trace, **fields)
    try:
        return _append("operation", record, sync=level in {"ERROR", "CRITICAL"})
    finally:
        threshold = os.environ.get("FACTORY_LOG_LEVEL", "INFO").upper()
        order = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        if level in order and threshold in order and order.index(level) >= order.index(threshold):
            print(json.dumps(record, separators=(",", ":"), sort_keys=True, default=str),
                  file=sys.stderr, flush=True)


def telemetry(metric: str, *, value=None, unit: str | None = None, **fields) -> dict:
    return _append("telemetry", _base("telemetry", metric=metric, value=value,
                                       unit=unit, **fields))


def trace_id(repo: str, story: int, claimed_at: str) -> str:
    """Stable W3C-sized trace identifier for one durable delivery attempt."""
    return hashlib.sha256(f"{repo}\n{story}\n{claimed_at}".encode()).hexdigest()[:32]


def story_trace_id(repo: str, story: int, timeline: list[dict]) -> str:
    """Derive one trace from the latest durable ``story:claimed`` transition."""
    claimed = [item.get("created_at", "") for item in timeline
               if item.get("event") == "labeled"
               and (item.get("label") or {}).get("name") == "story:claimed"]
    if not claimed:
        raise ValueError(f"Story #{story} has no durable story:claimed transition")
    return trace_id(repo, story, max(claimed))


def span_id() -> str:
    return secrets.token_hex(8)


@contextlib.contextmanager
def bound_context(**fields):
    merged = {**_context.get(), **{key: value for key, value in fields.items()
                                   if value is not None}}
    token = _context.set(merged)
    try:
        yield merged
    finally:
        _context.reset(token)


class Activity:
    """Live operation record with a supervisor-owned five-second heartbeat."""

    def __init__(self, component: str, operation: str, stage: str, **work):
        self.component, self.operation, self.stage = component, operation, stage
        self.work = work
        self.started = self.progress_at = time.monotonic()
        self.started_at = now()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.span = work.pop("span_id", None) or span_id()
        self.parent_span = work.pop("parent_span_id", None)
        self.context_manager = None

    def _fields(self) -> dict:
        current = time.monotonic()
        return {"component": self.component, "operation": self.operation,
                "stage": self.stage, "span_id": self.span,
                "parent_span_id": self.parent_span,
                "elapsed_seconds": round(current - self.started, 3),
                "last_progress_seconds": round(current - self.progress_at, 3),
                "pid": os.getpid(), **self.work}

    # Thread scheduling is intentionally absent from deterministic line coverage:
    # the behaviour has an explicit clock-bounded test, but whether the OS enters
    # this loop before a fast caller exits cannot be made a reproducible line hit.
    def _heartbeat(self):  # pragma: no cover - timing behaviour is tested directly
        while not self.stop_event.wait(HEARTBEAT_SECONDS):
            self._emit(telemetry, "activity.heartbeat", status=self.status(),
                       **self._fields())

    @staticmethod
    def _emit(writer, *args, **kwargs):
        """Observability must never replace the exception being observed."""
        try:
            return writer(*args, **kwargs)
        except Exception as log_error:  # noqa: BLE001 - last-resort diagnostic path
            print(f"[observability] {type(log_error).__name__}: {log_error}",
                  file=sys.stderr, flush=True)
            return None

    def status(self, current: float | None = None) -> str:
        age = (current if current is not None else time.monotonic()) - self.progress_at
        return "ALIVE_NO_PROGRESS" if age >= NO_PROGRESS_SECONDS else "RUNNING"

    def __enter__(self):
        context = {key: value for key, value in self.work.items()
                   if key in {"trace_id", "repo", "story", "project", "commitment",
                              "pull_request", "artifact"}}
        context.update(component=self.component, span_id=self.span,
                       parent_span_id=self.parent_span)
        self.context_manager = bound_context(**context)
        self.context_manager.__enter__()
        self._emit(operational_log, "INFO", "activity started", **self._fields())
        self._emit(telemetry, "activity.started", status="RUNNING", **self._fields())
        if os.environ.get("FACTORY_HEARTBEATS", "1").strip().lower() not in {"0", "false", "no"}:
            self.thread = threading.Thread(target=self._heartbeat, daemon=True,
                                           name=f"factory-heartbeat-{self.component}")
            self.thread.start()
        return self

    def progress(self, stage: str, message: str = "activity progressed", **fields):
        self.stage, self.progress_at = stage, time.monotonic()
        values = {**self._fields(), **fields}
        self._emit(operational_log, "INFO", message, **values)
        self._emit(telemetry, "activity.progress", status="RUNNING", **values)

    def __exit__(self, exc_type, exc, tb):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=HEARTBEAT_SECONDS)
        fields = self._fields()
        if exc is None:
            self._emit(telemetry, "activity.completed", status="COMPLETED", **fields)
            self._emit(operational_log, "INFO", "activity completed", **fields)
        else:
            self._emit(telemetry, "activity.failed", status="FAILED", **fields)
            self._emit(operational_log, "ERROR", "activity failed", exc=exc,
                       stack_trace="".join(traceback.format_exception(exc_type, exc, tb)),
                       **fields)
        if self.context_manager:
            self.context_manager.__exit__(exc_type, exc, tb)
        return False


def read_records(kind: str, directory: pathlib.Path | None = None) -> list[dict]:
    path = (directory or run_directory()) / FILES[kind]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def activity_status(records: list[dict], current_time: datetime | None = None) -> list[dict]:
    """Materialize latest component/span status from telemetry records."""
    current_time = current_time or datetime.now(timezone.utc)
    latest: dict[tuple[str, str], dict] = {}
    for record in records:
        if not str(record.get("metric", "")).startswith("activity."):
            continue
        key = (str(record.get("component", "")), str(record.get("span_id", "")))
        latest[key] = record
    rows = []
    for record in latest.values():
        metric = record["metric"]
        if metric == "activity.completed":
            status = "COMPLETED"
        elif metric == "activity.failed":
            status = "FAILED"
        else:
            timestamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            age = (current_time - timestamp).total_seconds()
            status = "STUCK" if age >= STUCK_SECONDS else record.get("status", "RUNNING")
        rows.append({**record, "status": status})
    return sorted(rows, key=lambda row: (str(row.get("component")), str(row.get("span_id"))))
