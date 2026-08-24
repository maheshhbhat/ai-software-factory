#!/usr/bin/env python3
"""Operator-invoked live diagnostic for the production review wrapper.

This is never run by CI. It spends a real reviewer invocation and posts a real
review outcome, so use it only with a disposable pull request linked to a
disposable Story in ``story:in-review``.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
REVIEW_RUNNER = ROOT / "factory" / "agents" / "review" / "run.sh"
DEFAULT_TIMEOUT_SECONDS = 60
FORBIDDEN_OVERRIDES = ("FACTORY_REVIEW_CMD", "FACTORY_REVIEW_MODEL_CMD")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def evidence_directory(root: pathlib.Path) -> pathlib.Path:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / run_id
    path.mkdir(parents=True, exist_ok=False)
    return path


def token() -> str:
    value = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if value:
        return value
    result = subprocess.run(["gh", "auth", "token"], capture_output=True,
                            text=True, check=True, timeout=30)
    if not result.stdout.strip():
        raise RuntimeError("GitHub credential unavailable")
    return result.stdout.strip()


def validate_environment(env: dict[str, str]) -> None:
    present = [name for name in FORBIDDEN_OVERRIDES if env.get(name)]
    if present:
        raise RuntimeError("review substitution overrides present: " + ", ".join(present))


def run(repo: str, pull_request: int, evidence_root: pathlib.Path,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, pathlib.Path]:
    validate_environment(os.environ)
    destination = evidence_directory(evidence_root)
    runtime_log = destination / "runtime.jsonl"
    stdout_log = destination / "stdout.log"
    evidence_path = destination / "evidence.json"
    env = os.environ.copy()
    env["GH_TOKEN"] = token()
    env["FACTORY_RUNTIME_LOG"] = str(runtime_log.resolve())
    env["FACTORY_RUNTIME_LOG_STDERR"] = "1"

    started_at = utc_now()
    started = time.monotonic()
    print(f"[reviewer-real] reviewing disposable PR #{pull_request}", flush=True)
    print("[reviewer-real] live reviewer stages follow on stderr", flush=True)
    status = "failed"
    detail = ""
    exit_code = 1
    try:
        result = subprocess.run([str(REVIEW_RUNNER), repo, str(pull_request)],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                text=True, timeout=timeout_seconds)
        stdout_log.write_text(result.stdout or "")
        exit_code = result.returncode
        status = "completed" if exit_code == 0 else "failed"
        detail = (result.stdout or "").strip()
    except subprocess.TimeoutExpired as exc:
        stdout_log.write_text((exc.stdout or "") if isinstance(exc.stdout, str) else "")
        status = "timeout"
        exit_code = 124
        detail = f"review exceeded {timeout_seconds} seconds"

    evidence = {
        "repo": repo,
        "pull_request": pull_request,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "timeout_seconds": timeout_seconds,
        "status": status,
        "exit_code": exit_code,
        "production_entrypoint": str(REVIEW_RUNNER.relative_to(ROOT)),
        "runtime_log": str(runtime_log.relative_to(ROOT)) if runtime_log.is_relative_to(ROOT)
                       else str(runtime_log),
        "detail": detail,
    }
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(f"[reviewer-real] {status.upper()} after {evidence['duration_seconds']} seconds")
    print(f"[reviewer-real] evidence: {evidence_path}")
    return exit_code, evidence_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="maheshhbhat/ai-software-factory")
    parser.add_argument("--pull-request", required=True, type=int)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--evidence-root", type=pathlib.Path,
                        default=pathlib.Path("runs/reviewer-real"))
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0 or args.timeout_seconds > DEFAULT_TIMEOUT_SECONDS:
        parser.error("--timeout-seconds must be between 1 and 60")
    try:
        return run(args.repo, args.pull_request, args.evidence_root,
                   args.timeout_seconds)[0]
    except Exception as exc:  # noqa: BLE001 - command-line diagnostic reports plainly
        print(f"[reviewer-real] FAIL before launch: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
