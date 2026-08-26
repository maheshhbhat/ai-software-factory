#!/usr/bin/env python3
"""Checked-in Capacity Pool registry and workload policy.

Model identifiers that have not been verified by an adapter smoke probe remain
disabled data.  They cannot accidentally become executable routes.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

from .router import ModelCapacity, RouteRequest, Tier


@dataclass(frozen=True)
class RegistryEntry:
    name: str
    provider: str
    model_id: str | None
    tier: Tier
    capabilities: frozenset[str]
    efforts: frozenset[str]
    enabled: bool = True
    prepaid_or_expiring: bool = False
    experimental: bool = False

    def capacity(self, *, available: bool = True,
                 remaining: float = 1.0) -> ModelCapacity:
        return ModelCapacity(
            self.name, self.provider, self.tier, self.capabilities,
            available=self.enabled and bool(self.model_id) and available,
            capacity_remaining=remaining,
            prepaid_or_expiring=self.prepaid_or_expiring,
            experimental=self.experimental,
            supports_effort=self.efforts,
        )


@dataclass(frozen=True)
class WorkloadPolicy:
    name: str
    capabilities: frozenset[str]
    normal_tier: Tier
    effort: str
    timeout_seconds: int
    budget_units: float
    escalation_triggers: frozenset[str] = frozenset()
    escalation_tier: Tier | None = None

    def request(self, *, triggers=frozenset(), **overrides) -> RouteRequest:
        triggers = frozenset(triggers)
        unknown = triggers - self.escalation_triggers
        if unknown:
            raise ValueError(f"unsupported escalation trigger(s): {sorted(unknown)}")
        tier = self.escalation_tier if triggers else self.normal_tier
        return RouteRequest(
            self.name, self.capabilities, tier or self.normal_tier, self.effort,
            overrides.pop("total_timeout_seconds", self.timeout_seconds),
            overrides.pop("total_budget_units", self.budget_units),
            **overrides,
        )


# Spark's installed CLI slug must be filled only after its adapter smoke check.
# Keeping the capacity class checked in but disabled avoids inventing a slug.
REGISTRY = (
    RegistryEntry("codex-spark", "openai", None, Tier.BALANCED,
                  frozenset({"code", "write", "tests"}),
                  frozenset({"low", "medium"}), enabled=False,
                  prepaid_or_expiring=True),
    RegistryEntry("gpt-5.6-terra", "openai", "gpt-5.6-terra", Tier.BALANCED,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"low", "medium", "high"})),
    RegistryEntry("gpt-5.6-luna", "openai", "gpt-5.6-luna", Tier.ECONOMY,
                  frozenset({"exact-answer", "basic-tools"}),
                  frozenset({"low", "medium"})),
    RegistryEntry("gpt-5.6-sol", "openai", "gpt-5.6-sol", Tier.FLAGSHIP,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"medium", "high", "max"})),
    # The four entries below were added from the Codex CLI's own model catalog
    # (~/.codex/models_cache.json) and the Claude CLI, each verified by a live
    # adapter probe answering CAPACITY_OK on 2026-08-25. Hidden internal
    # catalog entries (gpt-reserve, codex-auto-review) are deliberately absent.
    RegistryEntry("gpt-5.5", "openai", "gpt-5.5", Tier.FLAGSHIP,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"medium", "high", "max"})),
    RegistryEntry("gpt-5.4", "openai", "gpt-5.4", Tier.BALANCED,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"low", "medium", "high"})),
    RegistryEntry("gpt-5.4-mini", "openai", "gpt-5.4-mini", Tier.ECONOMY,
                  frozenset({"exact-answer", "basic-tools"}),
                  frozenset({"low", "medium"})),
    RegistryEntry("anthropic-economy", "anthropic", None, Tier.ECONOMY,
                  frozenset({"exact-answer", "basic-tools"}), frozenset({"low"}),
                  enabled=False),
    RegistryEntry("anthropic-balanced", "anthropic", None, Tier.BALANCED,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"low", "medium", "high"}), enabled=False),
    RegistryEntry("claude-fable-5", "anthropic", "claude-fable-5", Tier.FLAGSHIP,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"medium", "high", "max"})),
    RegistryEntry("claude-opus-5", "anthropic", "claude-opus-5", Tier.FLAGSHIP,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"low", "medium", "high", "max"})),
    RegistryEntry("claude-opus-4-8", "anthropic", "claude-opus-4-8", Tier.FLAGSHIP,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"low", "medium", "high", "max"})),
    # Muse is the ADR's named experimental entry, finally real: Meta's `muse`
    # CLI, verified by a live probe answering CAPACITY_OK on 2026-08-26.
    # Experimental means no workload routes to it unless it explicitly opts
    # in (allow_experimental); promotion to normal rotation is by recorded
    # evidence, per the ADR.
    RegistryEntry("muse-spark-1.2-contributor", "meta", "muse-spark-1.2-contributor",
                  Tier.BALANCED,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"low", "medium", "high"}), experimental=True),
)

POLICIES = {
    "planning": WorkloadPolicy(
        "planning", frozenset({"reason", "json"}), Tier.BALANCED, "medium", 900, 5,
        frozenset({"architecture", "high-complexity"}), Tier.FLAGSHIP),
    "delivery": WorkloadPolicy(
        "delivery", frozenset({"code", "write", "tests"}), Tier.BALANCED,
        "medium", 3600, 10, frozenset({"hazard", "high-complexity"}), Tier.FLAGSHIP),
    "review": WorkloadPolicy(
        "review", frozenset({"code", "reason", "json"}), Tier.BALANCED,
        "medium", 180, 2, frozenset({"high-risk", "architecture", "security"}),
        Tier.FLAGSHIP),
    "bridge": WorkloadPolicy(
        "bridge", frozenset({"basic-tools"}), Tier.ECONOMY, "low", 300, 1),
    "readiness": WorkloadPolicy(
        "readiness", frozenset({"exact-answer"}), Tier.ECONOMY, "low", 90, 1),
}


def active_registry(**availability) -> tuple[ModelCapacity, ...]:
    return tuple(entry.capacity(available=availability.get(entry.name, True))
                 for entry in REGISTRY)


MODEL_ID_ENV = {
    "codex-spark": "FACTORY_CAPACITY_OPENAI_SPARK_MODEL",
    "anthropic-economy": "FACTORY_CAPACITY_ANTHROPIC_ECONOMY_MODEL",
    "anthropic-balanced": "FACTORY_CAPACITY_ANTHROPIC_BALANCED_MODEL",
}


def resolved_registry(environ=None, *, health=None) -> tuple[ModelCapacity, ...]:
    """Resolve reviewed placeholders without inventing provider identifiers.

    `health(provider, model)` returns a persisted state row.  Unknown or stale
    capacity is excluded until the doctor records a successful adapter probe.
    """
    environ = os.environ if environ is None else environ
    values = []
    for entry in REGISTRY:
        model_id = entry.model_id or environ.get(MODEL_ID_ENV.get(entry.name, ""), "").strip()
        enabled = entry.enabled or bool(model_id)
        healthy = True
        if health is not None and model_id:
            healthy = health(entry.provider, model_id).get("state") == "healthy"
        values.append(ModelCapacity(
            model_id or entry.name, entry.provider, entry.tier, entry.capabilities,
            available=enabled and bool(model_id) and healthy,
            prepaid_or_expiring=entry.prepaid_or_expiring,
            experimental=entry.experimental,
            supports_effort=entry.efforts,
        ))
    return tuple(values)
