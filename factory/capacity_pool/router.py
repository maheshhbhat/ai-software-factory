#!/usr/bin/env python3
"""Deterministic model-capacity routing for factory agents.

This module is control-plane infrastructure. Agents ask for a capability; they do
not select providers or models directly. Routing is pure and deterministic for a
given request + registry snapshot so it can be tested independently before any
Planning/Worker/Review integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable


class Tier(IntEnum):
    ECONOMY = 1
    BALANCED = 2
    FLAGSHIP = 3
    FRONTIER = 4


@dataclass(frozen=True)
class ModelCapacity:
    name: str
    provider: str
    tier: Tier
    capabilities: frozenset[str]
    available: bool = True
    capacity_remaining: float = 1.0  # normalized 0..1
    prepaid_or_expiring: bool = False
    experimental: bool = False
    supports_effort: frozenset[str] = frozenset({"low", "medium", "high"})
    latency_rank: int = 100  # lower is faster
    recent_success: float = 1.0  # normalized 0..1

    def __post_init__(self):
        if not 0 <= self.capacity_remaining <= 1:
            raise ValueError("capacity_remaining must be between 0 and 1")
        if not 0 <= self.recent_success <= 1:
            raise ValueError("recent_success must be between 0 and 1")


@dataclass(frozen=True)
class RouteRequest:
    task_type: str
    required_capabilities: frozenset[str]
    minimum_tier: Tier
    effort: str
    total_timeout_seconds: int
    total_budget_units: float
    allow_experimental: bool = False
    preferred_provider: str | None = None
    explicit_model: str | None = None
    allow_fallback_for_override: bool = False
    avoid_providers: frozenset[str] = frozenset()
    prior_models: tuple[str, ...] = ()

    def __post_init__(self):
        if self.total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be positive")
        if self.total_budget_units <= 0:
            raise ValueError("total_budget_units must be positive")


@dataclass(frozen=True)
class RouteStep:
    model: str
    provider: str
    tier: Tier
    effort: str
    reason: str


@dataclass(frozen=True)
class RoutePlan:
    task_type: str
    steps: tuple[RouteStep, ...]
    total_timeout_seconds: int
    total_budget_units: float
    fallback_on: frozenset[str]
    stop_on: frozenset[str]

    @property
    def primary(self) -> RouteStep:
        if not self.steps:
            raise RuntimeError("route plan has no eligible model")
        return self.steps[0]


FALLBACK_ON = frozenset({
    "missing-executable", "unavailable", "quota", "rate-limit", "timeout", "auth",
})
STOP_ON = frozenset({
    "malformed-output", "schema-invalid", "unsafe-output", "scope-violation",
    "failed-tests", "ambiguous-mutation", "unknown-failure",
})


def _eligible(model: ModelCapacity, request: RouteRequest) -> bool:
    if not model.available or model.capacity_remaining <= 0:
        return False
    if model.provider in request.avoid_providers:
        return False
    if model.experimental and not request.allow_experimental:
        return False
    # A failure may select a peer, never silently raise the requested tier.
    # Escalation is a new request whose policy explicitly names the higher tier.
    if model.tier != request.minimum_tier:
        return False
    if not request.required_capabilities.issubset(model.capabilities):
        return False
    if request.effort not in model.supports_effort:
        return False
    return True


def _score(model: ModelCapacity, request: RouteRequest) -> tuple:
    """Lower tuple wins; stable name/provider tie-breakers make routing reproducible."""
    same_provider_penalty = 0
    if request.preferred_provider and model.provider != request.preferred_provider:
        same_provider_penalty = 1

    # Use the least-capable tier that safely satisfies the request.
    tier_penalty = int(model.tier - request.minimum_tier)

    # Prefer prepaid/expiring capacity when capability is otherwise sufficient.
    expiring_bonus = 0 if model.prepaid_or_expiring else 1

    # Prefer more remaining capacity and better recent reliability.
    capacity_penalty = round(1.0 - model.capacity_remaining, 6)
    reliability_penalty = round(1.0 - model.recent_success, 6)

    # Do not immediately replay a model that already attempted the same logical task.
    replay_penalty = 1 if model.name in request.prior_models else 0

    return (
        tier_penalty,
        replay_penalty,
        expiring_bonus,
        same_provider_penalty,
        capacity_penalty,
        reliability_penalty,
        model.latency_rank,
        model.provider,
        model.name,
    )


def route(request: RouteRequest, registry: Iterable[ModelCapacity], *, max_steps: int = 3) -> RoutePlan:
    if max_steps <= 0:
        raise ValueError("max_steps must be positive")

    models = tuple(registry)
    names = [model.name for model in models]
    if len(set(names)) != len(names):
        raise ValueError("model capacity names must be unique")
    by_name = {model.name: model for model in models}

    if request.explicit_model:
        model = by_name.get(request.explicit_model)
        if model is None or not _eligible(model, request):
            raise LookupError(f"explicit model is not eligible: {request.explicit_model}")
        first = RouteStep(model.name, model.provider, model.tier, request.effort,
                          "explicit operator/model override")
        if not request.allow_fallback_for_override:
            return RoutePlan(request.task_type, (first,), request.total_timeout_seconds,
                             request.total_budget_units, FALLBACK_ON, STOP_ON)
        excluded = {model.name}
        candidates = [m for m in models if m.name not in excluded and _eligible(m, request)]
        candidates.sort(key=lambda item: _score(item, request))
        steps = (first,) + tuple(
            RouteStep(m.name, m.provider, m.tier, request.effort,
                      "fallback authorized for explicit override")
            for m in candidates[: max_steps - 1]
        )
        return RoutePlan(request.task_type, steps, request.total_timeout_seconds,
                         request.total_budget_units, FALLBACK_ON, STOP_ON)

    candidates = [model for model in models if _eligible(model, request)]
    if not candidates:
        raise LookupError("no eligible model capacity")
    candidates.sort(key=lambda item: _score(item, request))

    ordered: list[ModelCapacity] = []
    remaining = list(candidates)
    seen_providers: set[str] = set()
    while remaining and len(ordered) < max_steps:
        index = 0
        if ordered:
            index = next(
                (position for position, candidate in enumerate(remaining)
                 if candidate.provider not in seen_providers),
                0,
            )
        model = remaining.pop(index)
        ordered.append(model)
        seen_providers.add(model.provider)

    steps: list[RouteStep] = []
    for model in ordered:
        reason = "lowest sufficient tier"
        if model.prepaid_or_expiring:
            reason += "; prefers prepaid/expiring capacity"
        if model.name in request.prior_models:
            reason += "; prior-attempt replay only because no better eligible capacity ranked above it"
        steps.append(RouteStep(model.name, model.provider, model.tier, request.effort, reason))

    return RoutePlan(request.task_type, tuple(steps), request.total_timeout_seconds,
                     request.total_budget_units, FALLBACK_ON, STOP_ON)


def remaining_envelope(plan: RoutePlan, *, elapsed_seconds: int,
                       consumed_budget_units: float) -> tuple[int, float]:
    """Return the remainder for the same logical task; fallback never resets bounds."""
    if elapsed_seconds < 0 or consumed_budget_units < 0:
        raise ValueError("consumption cannot be negative")
    timeout = max(0, plan.total_timeout_seconds - elapsed_seconds)
    budget = max(0.0, plan.total_budget_units - consumed_budget_units)
    return timeout, budget
