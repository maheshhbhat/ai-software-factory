#!/usr/bin/env python3
"""Show live component work from one factory run directory."""

from __future__ import annotations

import argparse
import pathlib
import signal

import observability


def render(rows: list[dict]) -> str:
    headings = ("COMPONENT", "WORK", "STAGE", "ELAPSED", "STATUS")
    values = []
    for row in rows:
        work = (f"Story #{row['story']}" if row.get("story") else
                f"Project #{row['project']}" if row.get("project") else
                f"Commitment #{row['commitment']}" if row.get("commitment") else
                f"Artifact #{row['artifact']}" if row.get("artifact") else "factory")
        values.append((str(row.get("component", "")), work,
                       str(row.get("stage", "")),
                       f"{float(row.get('elapsed_seconds', 0)):.1f}s",
                       str(row.get("status", ""))))
    widths = [max(len(headings[index]), *(len(row[index]) for row in values))
              if values else len(headings[index]) for index in range(len(headings))]
    lines = ["  ".join(value.ljust(widths[index])
                       for index, value in enumerate(headings))]
    lines.extend("  ".join(value.ljust(widths[index])
                           for index, value in enumerate(row)) for row in values)
    return "\n".join(lines)


def current_components(rows: list[dict]) -> list[dict]:
    """One most-recent activity per component, rather than historical spans."""
    latest = {}
    for row in rows:
        component = str(row.get("component", ""))
        if component not in latest or row.get("timestamp", "") > latest[component].get("timestamp", ""):
            latest[component] = row
    return sorted(latest.values(), key=lambda row: str(row.get("component", "")))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    rows = current_components(observability.activity_status(
        observability.read_records("telemetry", args.run_dir)))
    print(render(rows))
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main())
