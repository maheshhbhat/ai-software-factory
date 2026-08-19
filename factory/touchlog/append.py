#!/usr/bin/env python3
"""
Append a single JSON line to factory/touchlog/touchlog.jsonl.

Usage:
  python factory/touchlog/append.py \
    --project "#12" --bell-type plan-approval --classification decision \
    --seconds-spent 180 [--story "#15"] [--note "approved criteria"] [--actor "@you"] \
    [--file factory/touchlog/touchlog.jsonl] [--timestamp 2026-08-19T14:03:00Z]

Constraints:
  - File stays valid JSONL: one JSON object per line, UTF-8, no header/comment.
  - Exactly decision|audit|rescue|relay for classification.
  - No dependency on shell flock(1). Uses Python stdlib only; advisory lock via fcntl/msvcrt where available, else atomic append.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

VALID_BELL_TYPES = {
    "plan-approval",
    "hazard-ack",
    "poison-rescue",
    "cutover-approval",
    "acceptance",
    "sampling",
}

VALID_CLASSIFICATIONS = {"decision", "audit", "rescue", "relay"}

DEFAULT_FILE = Path(__file__).with_name("touchlog.jsonl")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Append a touch to touchlog.jsonl")
    p.add_argument("--project", required=True, help='Project issue ref, e.g. "#12"')
    p.add_argument("--story", default=None, help='Story issue ref, e.g. "#15" or omit/null')
    p.add_argument("--bell-type", required=True, dest="bell_type", choices=sorted(VALID_BELL_TYPES))
    p.add_argument("--classification", required=True, choices=sorted(VALID_CLASSIFICATIONS))
    p.add_argument("--seconds-spent", required=True, dest="seconds_spent", type=int, help="Non-negative integer seconds")
    p.add_argument("--note", default="", help="Human-readable note")
    p.add_argument("--actor", default="", help='Actor handle, e.g. "@alice"')
    p.add_argument("--timestamp", default=None, help="ISO-8601 UTC timestamp; defaults to now")
    p.add_argument("--file", dest="file", default=str(DEFAULT_FILE), help="Path to touchlog.jsonl")
    return p.parse_args(argv)


def _validate(project: str, story: str | None, seconds_spent: int) -> str | None:
    if not project or not project.strip():
        return "project is required"
    # project/story are free-form refs but must be non-empty if provided; allow "#N" or "owner/repo#N"
    if seconds_spent < 0:
        return "seconds-spent must be >= 0"
    if story is not None and story.strip() == "":
        return "story must be null/omitted or a non-empty ref"
    return None


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _lock_exclusive(f) -> None:
    """Advisory exclusive lock using stdlib only. No-op if unavailable."""
    try:
        import fcntl  # Unix

        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return
    except ImportError:
        pass
    except Exception:
        # fcntl present but flock failed — fall through to try msvcrt or no-op
        pass
    try:
        import msvcrt  # Windows

        # _locking with LK_LOCK is advisory; lock 1 byte at current position
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        return
    except ImportError:
        pass
    except Exception:
        pass
    # Last resort: no lock available — append is still atomic for small writes on POSIX
    # with O_APPEND, which Python's 'a' mode uses.


def _unlock(f) -> None:
    try:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return
    except ImportError:
        pass
    except Exception:
        pass
    try:
        import msvcrt

        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        return
    except ImportError:
        pass
    except Exception:
        pass


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    # Normalize story: treat empty string as None (null in JSON)
    story_val: str | None = args.story
    if story_val is not None and story_val.strip() == "":
        story_val = None

    timestamp = args.timestamp if args.timestamp is not None else _iso_now()

    err = _validate(args.project, story_val, args.seconds_spent)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2

    # Validate timestamp is roughly ISO-8601 (allow any non-empty; strict check is not required for append)
    if not timestamp or not timestamp.strip():
        print("error: timestamp must be non-empty", file=sys.stderr)
        return 2

    record = {
        "timestamp": timestamp,
        "project": args.project,
        "story": story_val,
        "bell_type": args.bell_type,
        "classification": args.classification,
        "seconds_spent": args.seconds_spent,
        "note": args.note,
        "actor": args.actor,
    }

    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))

    # Reject embedded newlines in the serialized line (should not happen via json.dumps)
    if "\n" in line or "\r" in line:
        print("error: record contains newline", file=sys.stderr)
        return 2

    path = Path(args.file)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure existing file is valid JSONL before appending (fail fast on corruption)
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open("r", encoding="utf-8") as rf:
                for i, raw in enumerate(rf, start=1):
                    if raw.strip() == "":
                        print(f"error: existing file has empty line at {i}", file=sys.stderr)
                        return 1
                    json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"error: existing file is not valid JSONL: {e}", file=sys.stderr)
            return 1

    # Atomic append: open in 'a' (O_APPEND) so each write appends at EOF even under concurrency.
    # Lock where possible, but O_APPEND guarantees the write itself is not interleaved for small lines.
    try:
        with path.open("a", encoding="utf-8", newline="\n") as f:
            _lock_exclusive(f)
            try:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                _unlock(f)
    except OSError as e:
        print(f"error: failed to append: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
