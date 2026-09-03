#!/usr/bin/env python3
"""Transactional health, reservation, and lease state for Capacity Pool."""

from __future__ import annotations

import hashlib
import os
import pathlib
import sqlite3
import time
import uuid
from dataclasses import dataclass


HEALTH_STATES = frozenset({
    "unknown", "healthy", "degraded", "rate-limited", "quota-exhausted",
    "unavailable", "cooldown", "probe",
})


@dataclass(frozen=True)
class Lease:
    lease_id: str
    task_key: str
    provider: str
    model: str
    reserved_budget: float
    expires_at: float


class CapacityStateError(RuntimeError):
    pass


class DuplicateTask(CapacityStateError):
    pass


class CapacityUnavailable(CapacityStateError):
    pass


def default_state_path(root, environ=None) -> pathlib.Path:
    """Return one Capacity Pool database shared by all Git worktrees.

    Linked worktrees each have their own checkout directory, but their Git
    administrative directories point to one common directory.  Store the
    operational database there so a clean-worktree change cannot erase model
    health or same-Story attempt history.  Explicit operator configuration
    remains authoritative, and non-Git installations retain the old fallback.
    """
    env = os.environ if environ is None else environ
    configured = env.get("FACTORY_CAPACITY_STATE", "").strip()
    if configured:
        return pathlib.Path(configured)
    root = pathlib.Path(root)
    dotgit = root / ".git"
    try:
        if dotgit.is_dir():
            git_dir = dotgit.resolve()
        elif dotgit.is_file():
            marker = dotgit.read_text().strip()
            if not marker.startswith("gitdir:"):
                raise ValueError("invalid Git worktree marker")
            git_dir = pathlib.Path(marker.removeprefix("gitdir:").strip())
            if not git_dir.is_absolute():
                git_dir = dotgit.parent / git_dir
            git_dir = git_dir.resolve()
        else:
            raise FileNotFoundError("Git metadata unavailable")
        common_marker = git_dir / "commondir"
        if common_marker.is_file():
            common_dir = pathlib.Path(common_marker.read_text().strip())
            if not common_dir.is_absolute():
                common_dir = git_dir / common_dir
            common_dir = common_dir.resolve()
        else:
            common_dir = git_dir
        return common_dir / "factory" / "capacity-pool.sqlite"
    except (OSError, ValueError):
        return root / "runs" / "capacity-pool.sqlite"


class CapacityState:
    def __init__(self, path="file:capacity-pool?mode=memory&cache=shared", *, uri=True,
                 clock=time.time, telemetry=lambda **fields: None):
        self.path, self.uri, self.clock, self.emit = str(path), uri, clock, telemetry
        self.connection = sqlite3.connect(self.path, uri=uri, isolation_level=None,
                                          timeout=5)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self):
        self.connection.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS health (
          provider TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL,
          reason TEXT NOT NULL, observed_at REAL NOT NULL, cooldown_until REAL,
          probe_failures INTEGER NOT NULL DEFAULT 0,
          quality_failures INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(provider, model)
        );
        CREATE TABLE IF NOT EXISTS leases (
          lease_id TEXT PRIMARY KEY, task_key TEXT NOT NULL,
          provider TEXT NOT NULL, model TEXT NOT NULL,
          reserved_budget REAL NOT NULL, expires_at REAL NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transitions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
          model TEXT NOT NULL, previous_state TEXT, new_state TEXT NOT NULL,
          reason TEXT NOT NULL, observed_at REAL NOT NULL
        );
        """)

    def close(self):
        self.connection.close()

    def models_for_task_prefix(self, task_prefix: str, *,
                               exclude_task_key: str | None = None) -> tuple[str, ...]:
        """Return models already leased for one logical task family.

        Delivery attempt numbers are the final task-key component.  Keeping the
        lookup in state makes retry diversity survive process restarts without
        adding another source of routing history.
        """
        if not task_prefix:
            raise ValueError("task prefix must not be empty")
        rows = self.connection.execute(
            "SELECT task_key, model FROM leases "
            "WHERE substr(task_key, 1, ?) = ? "
            "AND status IN ('consumed', 'complete') ORDER BY rowid",
            (len(task_prefix), task_prefix)).fetchall()
        models = []
        for row in rows:
            if row["task_key"] == exclude_task_key or row["model"] in models:
                continue
            models.append(row["model"])
        return tuple(models)

    def health(self, provider: str, model: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM health WHERE provider=? AND model=?", (provider, model)).fetchone()
        return dict(row) if row else {"provider": provider, "model": model,
                                      "state": "unknown", "reason": "no-observation",
                                      "observed_at": 0.0,
                                      "cooldown_until": None, "probe_failures": 0,
                                      "quality_failures": 0}

    def _transition(self, provider, model, new_state, reason, *, cooldown_until=None,
                    probe_failures=None, quality_failures=None):
        if new_state not in HEALTH_STATES:
            raise ValueError(f"invalid health state: {new_state}")
        now, previous = self.clock(), self.health(provider, model)
        failures = (previous["probe_failures"] if probe_failures is None
                    else probe_failures)
        quality = (previous["quality_failures"] if quality_failures is None
                   else quality_failures)
        with self.connection:
            self.connection.execute(
                "INSERT INTO health(provider,model,state,reason,observed_at,cooldown_until,probe_failures,quality_failures) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(provider,model) DO UPDATE SET "
                "state=excluded.state,reason=excluded.reason,observed_at=excluded.observed_at,"
                "cooldown_until=excluded.cooldown_until,probe_failures=excluded.probe_failures,"
                "quality_failures=excluded.quality_failures",
                (provider, model, new_state, reason, now, cooldown_until, failures, quality))
            self.connection.execute(
                "INSERT INTO transitions(provider,model,previous_state,new_state,reason,observed_at) "
                "VALUES(?,?,?,?,?,?)",
                (provider, model, previous["state"], new_state, reason, now))
        self.emit(metric="capacity.health.transition", provider=provider, model=model,
                  previous_state=previous["state"], new_state=new_state,
                  reason=reason, observed_at=now, cooldown_until=cooldown_until)
        return self.health(provider, model)

    def mark_healthy(self, provider, model, reason="validated-observation"):
        return self._transition(provider, model, "healthy", reason, probe_failures=0,
                                quality_failures=0)

    def mark_quality_failure(self, provider, model, reason, *, threshold=2):
        if threshold <= 0:
            raise ValueError("quality threshold must be positive")
        current = self.health(provider, model)
        failures = current["quality_failures"] + 1
        state = "degraded" if failures >= threshold else current["state"]
        return self._transition(provider, model, state, reason,
                                quality_failures=failures)

    def mark_failure(self, provider, model, reason, *, retry_after=None,
                     base_cooldown=30, maximum_cooldown=900):
        failure_state = (reason if reason in {"rate-limited", "quota-exhausted"}
                         else "unavailable")
        self._transition(provider, model, failure_state, reason)
        delay = retry_after if retry_after is not None else base_cooldown
        delay = min(maximum_cooldown, max(1, delay))
        return self._transition(provider, model, "cooldown", reason,
                                cooldown_until=self.clock() + delay)

    def begin_probe(self, provider, model):
        current = self.health(provider, model)
        if current["state"] != "cooldown" or self.clock() < (current["cooldown_until"] or 0):
            raise RuntimeError("capacity is not eligible for probe")
        return self._transition(provider, model, "probe", "cooldown-expired")

    def finish_probe(self, provider, model, success: bool, *, base_cooldown=30,
                     maximum_cooldown=900):
        current = self.health(provider, model)
        if current["state"] != "probe":
            raise RuntimeError("capacity has no active probe")
        if success:
            return self._transition(provider, model, "healthy", "probe-success",
                                    probe_failures=0, quality_failures=0)
        failures = current["probe_failures"] + 1
        raw = base_cooldown * (2 ** min(failures - 1, 8))
        digest = int(hashlib.sha256(f"{provider}:{model}".encode()).hexdigest()[:4], 16)
        jitter = digest % max(1, base_cooldown // 4 + 1)
        return self._transition(provider, model, "cooldown", "probe-failed",
                                cooldown_until=self.clock() + min(maximum_cooldown, raw + jitter),
                                probe_failures=failures)

    def reserve(self, task_key, provider, model, budget_units, *, ttl_seconds,
                capacity_limit=None, max_health_age_seconds=None):
        if budget_units <= 0 or ttl_seconds <= 0:
            raise ValueError("lease bounds must be positive")
        now, lease_id = self.clock(), uuid.uuid4().hex
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "UPDATE leases SET status='expired' WHERE status='active' AND expires_at<=?", (now,))
            existing = self.connection.execute(
                "SELECT 1 FROM leases WHERE task_key=? AND status='active'", (task_key,)).fetchone()
            if existing:
                raise DuplicateTask("duplicate active logical task")
            current = self.health(provider, model)
            if current["state"] != "healthy":
                raise CapacityUnavailable(f"capacity is not healthy: {current['state']}")
            provider_health = self.health(provider, "*")
            if provider_health["state"] not in {"unknown", "healthy"}:
                raise CapacityUnavailable(
                    f"provider capacity is not healthy: {provider_health['state']}")
            if (max_health_age_seconds is not None and
                    now - current["observed_at"] > max_health_age_seconds):
                raise CapacityUnavailable("capacity health observation is stale")
            if capacity_limit is not None:
                reserved = self.connection.execute(
                    "SELECT COALESCE(SUM(reserved_budget),0) FROM leases "
                    "WHERE provider=? AND model=? AND status='active'",
                    (provider, model)).fetchone()[0]
                if float(reserved) + budget_units > capacity_limit:
                    raise CapacityUnavailable("capacity reservation would oversubscribe model")
            self.connection.execute(
                "INSERT INTO leases VALUES(?,?,?,?,?,?,?)",
                (lease_id, task_key, provider, model, budget_units,
                 now + ttl_seconds, "active"))
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return Lease(lease_id, task_key, provider, model, budget_units,
                     now + ttl_seconds)

    def consume(self, lease_id: str, *, task_key: str) -> Lease:
        """Atomically turn one unexpired admission reservation into execution.

        The caller supplies the logical task identity so an opaque reservation
        copied from another Story cannot be used to launch work.
        """
        now = self.clock()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if not row or row["status"] != "active":
                raise CapacityUnavailable("capacity reservation is not active")
            if row["expires_at"] <= now:
                self.connection.execute(
                    "UPDATE leases SET status='expired' WHERE lease_id=?", (lease_id,))
                self.connection.execute("COMMIT")
                raise CapacityUnavailable("capacity reservation expired")
            if row["task_key"] != task_key:
                raise CapacityUnavailable("capacity reservation task does not match")
            current = self.health(row["provider"], row["model"])
            provider_health = self.health(row["provider"], "*")
            if current["state"] != "healthy" or provider_health["state"] not in {
                    "unknown", "healthy"}:
                raise CapacityUnavailable(
                    "reserved capacity became unavailable before start")
            self.connection.execute(
                "UPDATE leases SET status='consumed' WHERE lease_id=?", (lease_id,))
            self.connection.execute("COMMIT")
        except CapacityUnavailable:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        return Lease(row["lease_id"], row["task_key"], row["provider"],
                     row["model"], float(row["reserved_budget"]),
                     float(row["expires_at"]))

    def reservation(self, lease_id: str, *, task_key: str) -> Lease:
        """Read an active opaque reservation for Capacity Pool internals."""
        row = self.connection.execute(
            "SELECT * FROM leases WHERE lease_id=? AND status='active'", (lease_id,)).fetchone()
        if not row:
            raise CapacityUnavailable("capacity reservation is not active")
        if row["task_key"] != task_key:
            raise CapacityUnavailable("capacity reservation task does not match")
        if row["expires_at"] <= self.clock():
            self.connection.execute(
                "UPDATE leases SET status='expired' WHERE lease_id=?", (lease_id,))
            raise CapacityUnavailable("capacity reservation expired")
        return Lease(row["lease_id"], row["task_key"], row["provider"],
                     row["model"], float(row["reserved_budget"]),
                     float(row["expires_at"]))

    def active_for_task(self, task_key: str) -> Lease | None:
        row = self.connection.execute(
            "SELECT * FROM leases WHERE task_key=? AND status='active' "
            "ORDER BY expires_at DESC LIMIT 1", (task_key,)).fetchone()
        if not row or row["expires_at"] <= self.clock():
            return None
        return Lease(row["lease_id"], row["task_key"], row["provider"],
                     row["model"], float(row["reserved_budget"]),
                     float(row["expires_at"]))

    def abort_start(self, lease_id: str) -> bool:
        """Undo acquisition only while model start evidence is still absent."""
        changed = self.connection.execute(
            "UPDATE leases SET status='released' "
            "WHERE lease_id=? AND status='consumed'", (lease_id,)).rowcount
        return changed == 1

    def release(self, lease_id: str) -> bool:
        """Release only a reservation that has not started execution."""
        changed = self.connection.execute(
            "UPDATE leases SET status='released' "
            "WHERE lease_id=? AND status IN ('active','expired')", (lease_id,)).rowcount
        return changed == 1

    def lease_status(self, lease_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT status FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        return row["status"] if row else None

    def reconcile(self, lease_id, *, consumed_budget_units):
        if consumed_budget_units < 0:
            raise ValueError("consumption cannot be negative")
        row = self.connection.execute(
            "SELECT * FROM leases WHERE lease_id=? AND status IN ('active','consumed')",
            (lease_id,)).fetchone()
        if not row:
            raise RuntimeError("lease is not active")
        self.connection.execute("UPDATE leases SET status='complete' WHERE lease_id=?", (lease_id,))
        return min(float(row["reserved_budget"]), consumed_budget_units)

    def transitions(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM transitions ORDER BY id")]
