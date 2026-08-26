"""Opaque, bounded admission reservations for delivery work."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .policy import POLICIES
from .router import ModelCapacity, RouteRequest, route
from .state import CapacityState, CapacityUnavailable, DuplicateTask


@dataclass(frozen=True)
class Admission:
    reservation_id: str


def delivery_request(story: dict) -> RouteRequest:
    body = (story.get("body") or "").replace("\r\n", "\n")
    section = re.search(r"(?ms)^### Spend cap\n\n(.*?)(?=\n### |\Z)", body)
    money = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", section.group(1)) if section else None
    minutes = re.search(r"([0-9]+)\s*min", section.group(1), re.I) if section else None
    if not money or not minutes:
        raise ValueError("Story Spend cap must contain `$N / N min`")
    labels = {item.get("name", "") if isinstance(item, dict) else str(item)
              for item in story.get("labels", [])}
    triggers = frozenset(name for name, candidates in {
        "hazard": {"hazard", "risk:hazard"},
        "high-complexity": {"high-complexity", "complexity:high"},
    }.items() if labels & candidates)
    return POLICIES["delivery"].request(
        triggers=triggers, total_timeout_seconds=int(minutes.group(1)) * 60,
        total_budget_units=float(money.group(1)))


def delivery_task_key(repo: str, story: dict, *, next_attempt: bool = True) -> str:
    body = (story.get("body") or "").replace("\r\n", "\n")
    match = re.search(r"(?m)^### Attempt\n\n(\d+)\s*$", body)
    if not match:
        raise ValueError("Story Attempt must be an integer")
    attempt = int(match.group(1)) + (1 if next_attempt else 0)
    return f"delivery:{repo.lower()}:{int(story['number'])}:{attempt}"


def reserve(*, task_key: str, request: RouteRequest,
            registry: tuple[ModelCapacity, ...], state: CapacityState) -> Admission | None:
    """Reserve one eligible route without exposing its identity to dispatch."""
    try:
        plan = route(request, registry)
    except LookupError:
        return None
    attempts = len(plan.steps)
    ttl = max(1, plan.total_timeout_seconds // attempts)
    budget = plan.total_budget_units / attempts
    for step in plan.steps:
        try:
            lease = state.reserve(task_key, step.provider, step.model, budget,
                                  ttl_seconds=ttl)
            return Admission(lease.lease_id)
        except DuplicateTask:
            existing = state.active_for_task(task_key)
            return Admission(existing.lease_id) if existing else None
        except CapacityUnavailable:
            continue
    return None
