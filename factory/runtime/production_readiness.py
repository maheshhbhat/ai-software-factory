"""Exact-revision production-readiness artifacts and promotion policy.

The artifact is advisory in ``warning`` mode.  ``blocking`` mode is deliberately
fail closed and may advance a Project only when every operating-envelope ID has
one passing result for the currently integrated revision.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone

try:
    from . import operating_envelope
except ImportError:  # direct script-style imports used by runtime tests
    import operating_envelope

MARKER = "<!-- factory-production-readiness:v1 -->"
HEADING = "## Production readiness evaluation"
MODES = frozenset({"warning", "blocking"})


class ReadinessError(ValueError):
    pass


def mode(environ=None) -> str:
    env = os.environ if environ is None else environ
    value = (env.get("FACTORY_PRODUCTION_READINESS_MODE") or "warning").strip()
    if value not in MODES:
        raise ReadinessError(
            "FACTORY_PRODUCTION_READINESS_MODE must be warning or blocking")
    return value


def _canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def artifact_digest(value: dict) -> str:
    unsigned = {key: item for key, item in value.items() if key != "digest"}
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def build(*, repo: str, project: int, revision: str, envelope: list[dict],
          results: list[dict], started_at: str, completed_at: str,
          observations: list[dict] | None = None) -> dict:
    artifact = {
        "schema_version": 1,
        "repo": repo.strip().lower(),
        "project": int(project),
        "revision": revision,
        "envelope_digest": operating_envelope.digest(envelope),
        "started_at": started_at,
        "completed_at": completed_at,
        "results": results,
        "observations": observations or [],
    }
    artifact["overall"] = (
        "ready" if all(item.get("result") == "pass" for item in results)
        else "not-ready")
    artifact["digest"] = artifact_digest(artifact)
    validate(artifact, repo=repo, project=project, revision=revision,
             envelope=envelope)
    return artifact


def _timestamp(value, field: str) -> datetime:
    if not isinstance(value, str):
        raise ReadinessError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReadinessError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReadinessError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate(artifact: dict, *, repo: str, project: int, revision: str,
             envelope: list[dict]) -> dict:
    if not isinstance(artifact, dict) or artifact.get("schema_version") != 1:
        raise ReadinessError("production-readiness schema is invalid")
    if artifact.get("digest") != artifact_digest(artifact):
        raise ReadinessError("production-readiness digest does not match")
    expected = {"repo": repo.strip().lower(), "project": int(project),
                "revision": revision,
                "envelope_digest": operating_envelope.digest(envelope)}
    for field, value in expected.items():
        if artifact.get(field) != value:
            raise ReadinessError(f"production-readiness {field} does not match")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ReadinessError("production-readiness revision must be an exact commit SHA")
    started = _timestamp(artifact.get("started_at"), "started_at")
    completed = _timestamp(artifact.get("completed_at"), "completed_at")
    if completed < started:
        raise ReadinessError("production-readiness timestamps are reversed")
    expected_ids = [item["id"] for item in envelope]
    results = artifact.get("results")
    if not isinstance(results, list) or [item.get("id") for item in results
                                        if isinstance(item, dict)] != expected_ids:
        raise ReadinessError("production-readiness results do not map every envelope ID")
    for item in results:
        if item.get("result") not in ("pass", "fail"):
            raise ReadinessError(f"production-readiness {item.get('id')} result is invalid")
        if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            raise ReadinessError(f"production-readiness {item.get('id')} lacks evidence")
        if item["result"] == "fail" and not str(item.get("detail") or "").strip():
            raise ReadinessError(f"production-readiness {item.get('id')} failure lacks detail")
    overall = "ready" if all(item["result"] == "pass" for item in results) \
        else "not-ready"
    if artifact.get("overall") != overall:
        raise ReadinessError("production-readiness overall result contradicts checks")
    observations = artifact.get("observations")
    if not isinstance(observations, list):
        raise ReadinessError("production-readiness observations must be a list")
    external_ids = [item["id"] for item in envelope
                    if item.get("category") == "external-provider"]
    if ([item.get("id") for item in observations if isinstance(item, dict)]
            != external_ids):
        raise ReadinessError("production-readiness live-provider observation is missing")
    for item in observations:
        bound = item.get("bounded_by_seconds")
        if not isinstance(bound, (int, float)) or isinstance(bound, bool) or bound <= 0:
            raise ReadinessError("production-readiness live-provider bound is invalid")
        started = _timestamp(item.get("started_at"), "observation started_at")
        completed = _timestamp(item.get("completed_at"), "observation completed_at")
        if completed < started or (completed - started).total_seconds() > bound:
            raise ReadinessError("production-readiness live-provider observation exceeded bound")
        if not isinstance(item.get("detail"), str) or not item["detail"].strip():
            raise ReadinessError("production-readiness live-provider observation lacks detail")
    return artifact


def render(artifact: dict) -> str:
    return f"{MARKER}\n\n{HEADING}\n\n```json\n" + json.dumps(
        artifact, sort_keys=True, indent=2) + "\n```"


def parse_comment(body: str) -> dict | None:
    if MARKER not in (body or ""):
        return None
    match = re.search(r"(?ms)^```json\n(.*?)\n```\s*$", body or "")
    if not match:
        raise ReadinessError("production-readiness comment JSON is missing")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ReadinessError("production-readiness comment JSON is malformed") from exc
    if not isinstance(value, dict):
        raise ReadinessError("production-readiness comment must contain an object")
    return value


def latest(comments: list[dict], *, repo: str, project: int, revision: str,
           envelope: list[dict]) -> dict | None:
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if MARKER not in body:
            continue
        try:
            value = parse_comment(body)
            validate(value, repo=repo, project=project, revision=revision,
                     envelope=envelope)
            return value
        except ReadinessError:
            # Never fall back past newer readiness evidence. A stale or
            # malformed latest artifact must cause a fresh evaluation rather
            # than resurrecting an older pass.
            return None
    return None


def permits_completion(artifact: dict | None, enforcement_mode: str) -> bool:
    if enforcement_mode == "warning":
        return True
    if enforcement_mode != "blocking":
        raise ReadinessError("unknown production-readiness enforcement mode")
    return bool(artifact and artifact.get("overall") == "ready")
