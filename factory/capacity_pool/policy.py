#!/usr/bin/env python3
"""Checked-in Capacity Pool registry and workload policy.

Model identifiers that have not been verified by an adapter smoke probe remain
disabled data.  They cannot accidentally become executable routes.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    RegistryEntry("anthropic-economy", "anthropic", None, Tier.ECONOMY,
                  frozenset({"exact-answer", "basic-tools"}), frozenset({"low"}),
                  enabled=False),
    RegistryEntry("anthropic-balanced", "anthropic", None, Tier.BALANCED,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"low", "medium", "high"}), enabled=False),
    RegistryEntry("claude-fable-5", "anthropic", "claude-fable-5", Tier.FLAGSHIP,
                  frozenset({"reason", "json", "code", "write", "tests"}),
                  frozenset({"medium", "high", "max"})),
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
