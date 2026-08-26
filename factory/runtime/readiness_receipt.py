"""Short-lived readiness receipts shared by doctor and the mutable poller."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import time

SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 15 * 60
CONFIG_NAMES = (
    "FACTORY_WORKER_ORDER", "FACTORY_PHASE4_REVIEWS",
    "FACTORY_CAPACITY_STATE",
    "FACTORY_CAPACITY_OPENAI_MODEL", "FACTORY_CAPACITY_OPENAI_SPARK_MODEL",
    "FACTORY_CAPACITY_ANTHROPIC_BALANCED_MODEL",
    "FACTORY_CAPACITY_ANTHROPIC_ECONOMY_MODEL",
    "FACTORY_CAPACITY_META_EXPERIMENTAL_MODEL",
    "FACTORY_PRODUCTION_READINESS_MODE",
    "FACTORY_PRODUCTION_READINESS_LAUNCH",
    "FACTORY_WORKER_CAPACITY_DELIVERY_LAUNCH",
    "FACTORY_WORKER_CAPACITY_DELIVERY_CAPABILITIES",
)
CONFIG_DEFAULTS = {
    "FACTORY_WORKER_ORDER": "capacity-delivery",
    "FACTORY_PHASE4_REVIEWS": "1",
    "FACTORY_CAPACITY_OPENAI_SPARK_MODEL": "gpt-5.3-codex-spark",
    "FACTORY_CAPACITY_ANTHROPIC_BALANCED_MODEL": "claude-sonnet-5",
    "FACTORY_CAPACITY_ANTHROPIC_ECONOMY_MODEL": "claude-haiku-4-5-20251001",
    "FACTORY_PRODUCTION_READINESS_MODE": "warning",
    "FACTORY_PRODUCTION_READINESS_LAUNCH": (
        "python3 factory/agents/readiness/invoke.py --repo {repo} --project {project}"),
    "FACTORY_WORKER_CAPACITY_DELIVERY_CAPABILITIES": "delivery",
}


class ReceiptError(ValueError):
    pass


def canonical_repo(repo: str) -> str:
    value = repo.strip().lower()
    if value.endswith(".git"):
        value = value[:-4]
    if value.count("/") != 1 or any(not part for part in value.split("/")):
        raise ReceiptError("repository must be canonical owner/name")
    return value


def configuration_fingerprint(environ=None) -> str:
    env = os.environ if environ is None else environ
    # poll.sh supplies these defaults before launching the poller. Normalize
    # them here too so doctor and poller bind the same effective configuration.
    defaults = dict(CONFIG_DEFAULTS)
    defaults["FACTORY_WORKER_CAPACITY_DELIVERY_LAUNCH"] = (
        "python3 factory/agents/worker/invoke.py --repo "
        f"{env.get('FACTORY_REPO', '')} --story {{story}} "
        "--reservation {reservation}")
    selected = {name: env.get(name) or defaults.get(name, "")
                for name in CONFIG_NAMES}
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def factory_revision(root: pathlib.Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, timeout=10)
    revision = result.stdout.strip()
    if result.returncode or len(revision) != 40:
        raise ReceiptError("factory revision could not be resolved")
    return revision


def default_path(repo: str, commitment: int, directory=None) -> pathlib.Path:
    key = hashlib.sha256(
        f"{canonical_repo(repo)}#{int(commitment)}".encode()).hexdigest()[:20]
    root = pathlib.Path(directory or os.environ.get(
        "FACTORY_READINESS_DIR", tempfile.gettempdir()))
    return root / f"factory-readiness-{key}.json"


def _digest(payload: dict) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "digest"}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def issue(path: pathlib.Path, *, repo: str, commitment: int, project: int,
          target: str, revision: str, checks: list[dict], environ=None,
          now: int | None = None,
          ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict:
    if ttl_seconds <= 0 or ttl_seconds > DEFAULT_TTL_SECONDS:
        raise ReceiptError("receipt lifetime must be positive and at most 15 minutes")
    if not checks or any(not row.get("passed") for row in checks):
        raise ReceiptError("doctor may issue a receipt only when every check passed")
    issued = int(time.time() if now is None else now)
    payload = {
        "schema_version": SCHEMA_VERSION, "repo": canonical_repo(repo),
        "commitment": int(commitment), "project": int(project), "target": target,
        "factory_revision": revision,
        "configuration_fingerprint": configuration_fingerprint(environ),
        "issued_at": issued, "expires_at": issued + ttl_seconds, "checks": checks,
    }
    payload["digest"] = _digest(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload


def validate(path: pathlib.Path, *, repo: str, commitment: int, revision: str,
             environ=None, now: int | None = None) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"readiness receipt unavailable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ReceiptError("readiness receipt schema is invalid")
    if payload.get("digest") != _digest(payload):
        raise ReceiptError("readiness receipt digest does not match")
    expected = {"repo": canonical_repo(repo), "commitment": int(commitment),
                "factory_revision": revision,
                "configuration_fingerprint": configuration_fingerprint(environ)}
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ReceiptError(f"readiness receipt {field} does not match")
    current = int(time.time() if now is None else now)
    if not isinstance(payload.get("issued_at"), int) or payload["issued_at"] > current + 5:
        raise ReceiptError("readiness receipt issue time is invalid")
    if not isinstance(payload.get("expires_at"), int) or payload["expires_at"] < current:
        raise ReceiptError("readiness receipt expired")
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks or any(
            not isinstance(row, dict) or row.get("passed") is not True for row in checks):
        raise ReceiptError("readiness receipt contains a failed or malformed check")
    return payload
