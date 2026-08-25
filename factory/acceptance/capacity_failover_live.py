#!/usr/bin/env python3
"""Controlled real Capacity Pool failover and recovery proof."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from factory.capacity_pool.executor import CapacityExecutor
from factory.capacity_pool.policy import POLICIES
from factory.capacity_pool.providers import (
    AttemptResult, InvocationPayload, ProviderAdapter, cli_adapter,
    provider_environment,
)
from factory.capacity_pool.router import ModelCapacity, Tier
from factory.capacity_pool.state import CapacityState

SENTINEL = "CAPACITY_FAILOVER_OK"
PROBE_TIMEOUT_SECONDS = 30
CAPABILITIES = frozenset({"code", "reason", "json"})
PRIMARY = ModelCapacity(
    "claude-fable-5", "anthropic", Tier.FLAGSHIP, CAPABILITIES,
    supports_effort=frozenset({"medium", "high", "max"}),
    prepaid_or_expiring=True)
FALLBACK = ModelCapacity(
    "gpt-5.6-sol", "openai", Tier.FLAGSHIP, CAPABILITIES,
    supports_effort=frozenset({"medium", "high", "max"}))
SCHEMA = {
    "type": "object",
    "properties": {"sentinel": {"type": "string", "const": SENTINEL}},
    "required": ["sentinel"],
    "additionalProperties": False,
}


def controlled_primary() -> ProviderAdapter:
    return ProviderAdapter(
        "anthropic", lambda **_kwargs: AttemptResult(
            "unavailable", consumed_budget_units=0,
            diagnostic="deliberate pre-inference provider outage",
            failure_scope="provider"),
        probe=lambda **_kwargs: True)


def validate_output(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("fallback returned malformed JSON") from exc
    if value != {"sentinel": SENTINEL}:
        raise ValueError("fallback returned the wrong sentinel")
    return value


def exercise(output: pathlib.Path, *, adapters=None, registry=None,
             clock=None) -> dict:
    now = [time.time()] if clock is None else clock
    state = CapacityState(clock=lambda: now[0])
    telemetry = []
    models = tuple(registry or (PRIMARY, FALLBACK))
    for model in models:
        state.mark_healthy(model.provider, model.name, "controlled-preflight")
    try:
        with tempfile.TemporaryDirectory(prefix="capacity-failover-") as temp:
            root = pathlib.Path(temp)
            schema_path = root / "schema.json"
            result_path = root / "result.json"
            schema_path.write_text(json.dumps(SCHEMA, sort_keys=True))
            active_adapters = adapters or {
                "anthropic": controlled_primary(),
                "openai": cli_adapter(
                    "openai", cwd=root,
                    environment=provider_environment("openai")),
            }
            parsed = {}

            def validate(raw):
                parsed.update(validate_output(raw))

            result = CapacityExecutor(
                active_adapters, state,
                telemetry=lambda **fields: telemetry.append(fields)).execute(
                    task_key="controlled-review-provider-failover",
                    request=POLICIES["review"].request(
                        triggers={"security"}, total_timeout_seconds=90,
                        total_budget_units=1),
                    registry=models,
                    payload=InvocationPayload(
                        "Return exactly one JSON object with sentinel value "
                        f"{SENTINEL}. Do not use tools or change any state.",
                        output_schema=SCHEMA, schema_path=schema_path,
                        output_path=result_path, access="read-only",
                        skip_git_repo_check=True),
                    validate=validate)
        if result.outcome != "success" or parsed != {"sentinel": SENTINEL}:
            raise RuntimeError(f"controlled fallback failed: {result.outcome}")
        if [item["model"] for item in result.attempts] != [PRIMARY.name, FALLBACK.name]:
            raise RuntimeError("controlled fallback did not use the approved model sequence")
        if any(item["tier"] != "flagship" or item["effort"] != "medium"
               for item in result.attempts):
            raise RuntimeError("controlled fallback changed tier or effort")
        recovery_model = "*"
        failed_health = state.health(PRIMARY.provider, recovery_model)
        if failed_health["state"] != "cooldown":
            raise RuntimeError("failed primary did not enter cooldown")
        now[0] = failed_health["cooldown_until"]
        state.begin_probe(PRIMARY.provider, recovery_model)
        probe_success = active_adapters["anthropic"].health_probe(
            model=PRIMARY.name, timeout_seconds=PROBE_TIMEOUT_SECONDS,
            effort="medium")
        state.finish_probe(PRIMARY.provider, recovery_model, probe_success)
        recovered = state.health(PRIMARY.provider, recovery_model)
        if recovered["state"] != "healthy":
            raise RuntimeError("primary did not recover through a successful probe")
        transitions = [dict(row) for row in state.connection.execute(
            "SELECT provider,model,previous_state,new_state,reason,observed_at "
            "FROM transitions WHERE provider=? AND model=? ORDER BY id",
            (PRIMARY.provider, recovery_model))]
        evidence = {
            "schema_version": 1,
            "outcome": result.outcome,
            "sentinel": parsed["sentinel"],
            "combined_envelope": {
                "timeout_seconds": 90,
                "budget_units": 1,
                "consumed_budget_units": result.consumed_budget_units,
            },
            "attempts": list(result.attempts),
            "recovery": {
                "state_after_failure": failed_health["state"],
                "final_state": recovered["state"],
                "probe": {"success": probe_success,
                          "timeout_seconds": PROBE_TIMEOUT_SECONDS,
                          "effort": "medium", "kind": "controlled-adapter"},
                "transitions": transitions,
            },
            "telemetry": telemetry,
            "limitations": [
                "Anthropic unavailability and recovery are controlled adapter "
                "signals; the GPT-5.6 Sol fallback inference is real."],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        return evidence
    finally:
        state.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    try:
        evidence = exercise(args.output)
    except Exception as exc:
        print(f"controlled failover failed: {type(exc).__name__}: {exc}")
        return 1
    print(json.dumps({"outcome": evidence["outcome"],
                      "evidence": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
