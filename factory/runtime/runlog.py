#!/usr/bin/env python3
"""Structured operational logging for worker dispatch and execution.

Phase 2, story #104 item 2. Until now a factory-launched worker was observable
only through whatever `[worker]` line survived to stdout, and `workers.launch`
keeps a *launched* worker's stdout and a *failed* worker's stderr — so for any
run that hung, the one channel carrying the explanation was the one thrown away.
Diagnosing a hang meant re-running the engine by hand in an interactive session,
which is exactly the state the factory exists to leave behind.

This module is the record. It answers, after the fact and with no live session:

    which worker ran · what it was told to run · what PID · what it printed ·
    what it exited · how long it took · what the acknowledgement check found ·
    why failover did or did not happen · what the runtime concluded

**It is a log, not a control surface.** Nothing here decides anything, nothing
raises, and no caller may branch on its return value. A logging failure that
stopped a dispatch would be a logging system that costs more than it earns, so
every write is best-effort and every error is swallowed. That is the one place
in this repository where swallowing an exception is correct.

## What is deliberately not recorded

**Secrets.** Tokens are redacted by pattern *and* by value: any occurrence of
the live `GITHUB_TOKEN` / `GH_TOKEN` is replaced before a line is written, so a
worker that echoes its environment cannot spill it into the log.

**Hidden model reasoning.** Only externally observable worker output is kept —
what the process wrote to stdout/stderr, capped. Nothing asks an engine for its
chain of thought and nothing here would store it if offered.

## Per-invocation engine usage

`engine_usage()` appends one record per engine launch naming the story, the
engine, and whatever that launch reported about itself. It exists because a
cost-per-story figure has to be *computed* from what the engines said, and
until now nothing in this log carried a usage field at all.

Nothing here prices anything. There is no rate table, no conversion, and no
ceiling: the monetary cost-per-story ceiling is the owner's to set, and an
engine's own reported numbers are recorded exactly as the engine gave them.

Output goes two places, both best-effort: a JSONL file (`FACTORY_RUNTIME_LOG`,
default `factory/runtime/logs/runtime.jsonl`) for the durable record, and stderr
for a human watching the poller. **Never stdout** — under this repository's
monitor one stdout line is one notification, so a chatty log there would page a
human per event.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_LOG_PATH = os.path.join(HERE, "logs", "runtime.jsonl")

# A single line is a diagnosis, not a transcript. Worker output is tailed rather
# than truncated from the front: when a process dies, what it said last is what
# explains it.
MAX_FIELD_CHARS = 2000

# The log is written by a long-running loop, which makes it a loop without a
# bound unless something caps it (`architecture-v2.1.md` §4). One rotation, kept
# deliberately dumb: the current file and one predecessor.
MAX_LOG_BYTES = 5 * 1024 * 1024

# Token shapes GitHub issues, plus a generic bearer header. Patterns catch what
# value-redaction cannot: a token belonging to some *other* identity, echoed by
# a worker into its own output.
_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)(authorization:\s*(?:bearer|token)\s+)\S+"),
)

REDACTED = "[redacted]"

# One runtime process, one id, so events from a poll and from the bridge
# subprocess it launched can be stitched back together after the fact.
RUN_ID = f"{os.getpid()}"


def log_path() -> str:
    """Where the durable record goes. Configuration, read at call time so a test
    can point it somewhere disposable."""
    return os.environ.get("FACTORY_RUNTIME_LOG", "").strip() or DEFAULT_LOG_PATH


def stderr_enabled() -> bool:
    return os.environ.get("FACTORY_RUNTIME_LOG_STDERR", "1").strip() not in ("0", "false", "no")


def redact(value):
    """Remove secrets from anything about to be written.

    Value-redaction runs first and is the one that matters: the factory's own
    token is known exactly, so it can be removed exactly, whatever shape it has.
    Pattern-redaction is the backstop for tokens this process never held.
    """
    if not isinstance(value, str):
        return value
    text = value
    # The engine credentials joined this list with #330: engine stderr now
    # rides into errors, launch records, and poison reports, and an engine
    # refused mid-authentication is exactly the case that echoes its token.
    for name in ("GITHUB_TOKEN", "GH_TOKEN",
                 "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        secret = os.environ.get(name, "")
        if secret and len(secret) >= 8:
            text = text.replace(secret, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + REDACTED) if m.groups() else REDACTED, text)
    return text


def tail(value: str | None, limit: int = MAX_FIELD_CHARS) -> str:
    """The last `limit` characters, redacted. Empty stays empty — a field that
    says `""` is evidence the process printed nothing, which is a real finding."""
    text = redact((value or "").strip())
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _rotate(path: str) -> None:
    try:
        if os.path.getsize(path) < MAX_LOG_BYTES:
            return
        os.replace(path, path + ".1")
    except OSError:
        pass


def _write(record: dict) -> None:
    line = json.dumps(record, separators=(",", ":"), sort_keys=True, default=str)
    if stderr_enabled():
        try:
            print(line, file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass
    path = log_path()
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        _rotate(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        # A log that cannot be written must not stop work from being done.
        pass


def event(name: str, **fields) -> dict:
    """Record one operational event. Returns the record, for tests only.

    Callers must not branch on the return value: this function's contract is
    that it always succeeds and always means nothing.
    """
    record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
              "run": RUN_ID,
              "event": name}
    for key, value in fields.items():
        if value is None:
            continue
        record[key] = redact(value) if isinstance(value, str) else value
    try:
        _write(record)
    except Exception:  # noqa: BLE001 — see the module docstring; logging never raises
        pass
    return record


USAGE_EVENT = "engine.usage"

# What an engine may say about one invocation of itself. An allowlist, and
# numbers only: everything else an engine prints is free text it controls, and
# free text does not belong in a field a report will later add up.
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens",
                "total_cost_usd", "duration_ms", "num_turns")

# Said nothing. Deliberately a sentence and not a zero — see `parse_usage()`.
USAGE_UNAVAILABLE = "engine reported no usage"


def _objects(output: str):
    """Every JSON object in an engine's output, whole document last.

    Engines print their totals differently: one JSON document for a single
    result, one object per line for a stream. Both are read, and neither is
    required — an engine printing plain prose simply yields nothing.
    """
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value
    text = output.strip()
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except ValueError:
            return
        if isinstance(value, dict):
            yield value


def _reported(value: dict) -> dict:
    """The declared usage numbers in one object, nested block preferred.

    An engine's `usage` block holds the token counts; the object around it holds
    the run totals. A field that is not a number is not a count, so it is
    dropped rather than stored as whatever the engine typed.
    """
    nested = value.get("usage")
    found: dict = {}
    for source in (nested if isinstance(nested, dict) else {}, value):
        for key, number in source.items():
            if (key in USAGE_FIELDS and isinstance(number, (int, float))
                    and not isinstance(number, bool)):
                found.setdefault(key, number)
    return found


def parse_usage(output: str | None) -> dict | None:
    """What an engine said about its own usage, or `None` if it said nothing.

    `None` and `{"output_tokens": 0}` are different findings and must stay
    different all the way into the log: one is a measurement of zero, the other
    is the absence of a measurement. Collapsing them would let a report sum an
    invocation that never reported anything as if it had cost nothing.

    The last reporting object wins, which is what a streaming engine means by
    printing running totals.
    """
    reported = None
    for value in _objects(output if isinstance(output, str) else ""):
        found = _reported(value)
        if found:
            reported = found
    return reported


def engine_usage(*, story, engine, phase, output: str | None = None,
                 usage: dict | None = None, **fields) -> dict:
    """Record one engine invocation's own account of what it used.

    The record carries the story and the engine, so usage sums per story with
    nothing but this log open. Pass `output` to read the engine's report out of
    what it printed, or `usage` when a caller already has the numbers.

    An engine that reported nothing is recorded saying so, in words. No value
    is defaulted, guessed, or carried over from another invocation.
    """
    reported = parse_usage(output) if usage is None else usage
    if reported is None:
        return event(USAGE_EVENT, story=story, engine=engine, phase=phase,
                     usage_reported=False, usage=USAGE_UNAVAILABLE, **fields)
    return event(USAGE_EVENT, story=story, engine=engine, phase=phase,
                 usage_reported=True, usage=dict(reported), **fields)


def command(parts, elide: str | None = None) -> str:
    """A command line fit for a log: redacted, and with a prompt elided.

    A prompt is not a secret, but it is long, multi-line, and identical on every
    launch. Eliding it keeps one event on one line while the *shape* of the
    invocation — engine, flags, permission grant — stays reviewable, which is the
    part that has actually been wrong before (#96's `acceptEdits`).
    """
    rendered = [("<prompt>" if elide is not None and part == elide else str(part))
                for part in parts]
    return tail(" ".join(rendered), MAX_FIELD_CHARS)


def elapsed_ms(started: float) -> int:
    """Milliseconds since a `time.monotonic()` mark. Monotonic on purpose — a
    clock adjustment mid-run must not produce a negative duration."""
    return int((time.monotonic() - started) * 1000)
