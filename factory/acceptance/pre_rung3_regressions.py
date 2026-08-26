#!/usr/bin/env python3
"""Validate the four production-shaped regressions required before Rung 3.

This is an evidence gate, not a product simulator. Project-specific commands,
browser UAT, the capacity recovery scenario, and live adapter probes write the
input evidence. This module rejects incomplete or toy-shaped evidence and emits
one deterministic bundle that the repeat can cite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path


class RegressionError(ValueError):
    pass


CLASSES = ("project47_scale", "project30_provider",
           "capacity_recovery", "adapter_contract")
REQUIRED_PROBE_CHECKS = ("authentication", "stream_shape", "permissions",
                         "capacity", "terminal_result")


def _number(value, name, *, minimum=0, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RegressionError(f"{name} must be numeric")
    if value < minimum or (positive and value <= 0):
        raise RegressionError(f"{name} is outside its required bound")
    return value


def _timestamp(value, name):
    if not isinstance(value, str):
        raise RegressionError(f"{name} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegressionError(f"{name} timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise RegressionError(f"{name} timestamp lacks timezone")
    return parsed


def project47_scale(value):
    portfolio = _number(value.get("portfolio_usd"), "portfolio_usd", minimum=500_000)
    if portfolio > 5_000_000:
        raise RegressionError("portfolio_usd exceeds the supported retirement envelope")
    elapsed = _number(value.get("elapsed_ms"), "elapsed_ms")
    bound = _number(value.get("bound_ms"), "bound_ms", positive=True)
    work = _number(value.get("work_units"), "work_units")
    work_bound = _number(value.get("work_bound"), "work_bound", positive=True)
    if elapsed > bound:
        raise RegressionError("representative portfolio exceeded its response bound")
    if work > work_bound:
        raise RegressionError("representative portfolio exceeded its work bound")
    if value.get("work_bound_kind") != "allocation-regimes-and-logarithmic-refinement":
        raise RegressionError("work bound permits portfolio-cent-sized enumeration")
    if value.get("portfolio_cent_iterations") != 0:
        raise RegressionError("portfolio-cent-sized work was observed")
    if value.get("responsive") is not True or value.get("result_rendered") is not True:
        raise RegressionError("representative browser result was not responsive and rendered")
    if not str(value.get("evidence") or "").strip():
        raise RegressionError("representative-scale evidence pointer is missing")


def project30_provider(value):
    offline = value.get("offline")
    if not isinstance(offline, dict) or offline.get("parsed") is not True or \
            offline.get("deterministic") is not True:
        raise RegressionError("offline provider parsing is not reproducibly proven")
    observations = value.get("live_observations")
    if not isinstance(observations, list) or not observations:
        raise RegressionError("fixture-only provider evidence has no live observation")
    providers = set()
    for item in observations:
        provider = str(item.get("provider") or "").lower()
        if provider not in {"vanguard", "fidelity"} or provider in providers:
            raise RegressionError("live provider coverage is duplicated or unsupported")
        providers.add(provider)
        started = _timestamp(item.get("started_at"), f"{provider} started_at")
        completed = _timestamp(item.get("completed_at"), f"{provider} completed_at")
        bound = _number(item.get("bounded_by_seconds"), "bounded_by_seconds",
                        positive=True)
        if completed < started or (completed - started).total_seconds() > bound:
            raise RegressionError(f"{provider} live read exceeded its bound")
        if item.get("read_only") is not True or item.get("compatible") is not True:
            raise RegressionError(f"{provider} live contract is incompatible or mutating")
        if item.get("current") is not True or item.get("stale_fallback_presented") is not False:
            raise RegressionError(f"{provider} evidence is stale or presents stale data as current")
        if not str(item.get("evidence") or "").strip():
            raise RegressionError(f"{provider} live evidence pointer is missing")
    if providers != {"vanguard", "fidelity"}:
        raise RegressionError("both preserved Project #30 provider paths are required")


def capacity_recovery(value):
    expected = {"zero_capacity_mutations": 0, "zero_capacity_attempt_delta": 0,
                "recovered_claims": 1, "worker_starts": 1}
    for name, wanted in expected.items():
        if value.get(name) != wanted:
            raise RegressionError(f"capacity recovery {name} must equal {wanted}")
    if value.get("reservation_reused") is not False:
        raise RegressionError("capacity recovery reused a reservation")
    if not str(value.get("evidence") or "").strip():
        raise RegressionError("capacity recovery evidence pointer is missing")


def adapter_contract(value):
    enabled = value.get("enabled_routes")
    probes = value.get("probes")
    if not isinstance(enabled, list) or not enabled or len(set(enabled)) != len(enabled):
        raise RegressionError("enabled adapter routes are absent or duplicated")
    if not isinstance(probes, list):
        raise RegressionError("adapter probes are missing")
    by_route = {item.get("route"): item for item in probes
                if isinstance(item, dict) and item.get("route")}
    if set(by_route) != set(enabled) or len(by_route) != len(probes):
        raise RegressionError("adapter probes do not map every enabled route exactly once")
    for route in enabled:
        probe = by_route[route]
        if probe.get("live") is not True or probe.get("read_only") is not True:
            raise RegressionError(f"{route} is not a bounded read-only live probe")
        _number(probe.get("bounded_by_seconds"), "bounded_by_seconds", positive=True)
        checks = probe.get("checks")
        if not isinstance(checks, dict) or set(checks) != set(REQUIRED_PROBE_CHECKS):
            raise RegressionError(f"{route} adapter checks are incomplete")
        failed = [name for name in REQUIRED_PROBE_CHECKS if checks.get(name) != "pass"]
        if failed:
            raise RegressionError(f"{route} adapter contract failed: {', '.join(failed)}")
        if not str(probe.get("evidence") or "").strip():
            raise RegressionError(f"{route} probe evidence pointer is missing")


VALIDATORS = {"project47_scale": project47_scale,
              "project30_provider": project30_provider,
              "capacity_recovery": capacity_recovery,
              "adapter_contract": adapter_contract}


def evaluate(evidence: dict) -> dict:
    if not isinstance(evidence, dict) or set(evidence) != set(CLASSES):
        raise RegressionError("regression evidence must contain exactly four classes")
    results = []
    for name in CLASSES:
        try:
            VALIDATORS[name](evidence[name])
            results.append({"class": name, "result": "pass"})
        except RegressionError as exc:
            results.append({"class": name, "result": "fail", "detail": str(exc)})
    artifact = {"schema_version": 1, "results": results,
                "overall": "pass" if all(item["result"] == "pass"
                                           for item in results) else "fail"}
    artifact["evidence_digest"] = hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        artifact = evaluate(evidence)
    except (OSError, json.JSONDecodeError, RegressionError) as exc:
        print(f"pre-Rung-3 regressions invalid: {exc}")
        return 2
    rendered = json.dumps(artifact, sort_keys=True, indent=2) + "\n"
    if args.json:
        args.json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if artifact["overall"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
