"""Deterministic boundary around the judgment-bearing planning prompt.

The model may propose a plan; this module decides which altitude is authorized
and whether the returned envelope is complete enough to write and verify.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ContractError(ValueError):
    pass


class Altitude(str, Enum):
    CAMPAIGN = "campaign"
    PROJECT = "project"


TRIGGER_LABELS = {
    "type:roadmap-commitment": Altitude.CAMPAIGN,
    "type:project": Altitude.PROJECT,
}

REQUIRED_INPUTS = ("trigger", "product", "adrs", "repository")

CAMPAIGN_KEYS = frozenset({"altitude", "project", "rationale", "risks"})
PROJECT_KEYS = frozenset({
    "altitude", "adr", "stories", "expected_bells", "digest",
})


@dataclass(frozen=True)
class PlanningInput:
    trigger: dict
    product: str
    adrs: tuple[dict, ...]
    repository: dict


def select_altitude(labels: list[str] | set[str]) -> Altitude:
    selected = {altitude for label, altitude in TRIGGER_LABELS.items() if label in labels}
    if len(selected) != 1:
        raise ContractError(
            "trigger must carry exactly one supported type label: "
            "type:roadmap-commitment or type:project")
    return selected.pop()


def validate_input(value: dict) -> PlanningInput:
    missing = [key for key in REQUIRED_INPUTS if key not in value]
    if missing:
        raise ContractError(f"planning input missing: {', '.join(missing)}")
    trigger = value["trigger"]
    if not isinstance(trigger, dict):
        raise ContractError("trigger must be an issue object")
    select_altitude(set(trigger.get("labels") or []))
    product = value["product"]
    if not isinstance(product, str) or not product.strip():
        raise ContractError("product.md must be readable and non-empty")
    adrs = value["adrs"]
    if not isinstance(adrs, (list, tuple)):
        raise ContractError("existing ADRs must be a list")
    repository = value["repository"]
    if not isinstance(repository, dict) or not repository.get("files"):
        raise ContractError("repository read access must provide a non-empty file index")
    return PlanningInput(trigger, product, tuple(adrs), repository)


def validate_output(altitude: Altitude, value: dict) -> dict:
    if not isinstance(value, dict):
        raise ContractError("planning output must be an object")
    required = CAMPAIGN_KEYS if altitude is Altitude.CAMPAIGN else PROJECT_KEYS
    missing = sorted(required - set(value))
    if missing:
        raise ContractError(
            f"{altitude.value} output missing: {', '.join(missing)}")
    if value.get("altitude") != altitude.value:
        raise ContractError(
            f"output altitude {value.get('altitude')!r} does not match trigger "
            f"altitude {altitude.value!r}")
    forbidden = PROJECT_KEYS - {"altitude"} if altitude is Altitude.CAMPAIGN else {
        "project", "rationale", "risks",
    }
    leaked = sorted(forbidden & set(value))
    if leaked:
        raise ContractError(
            f"{altitude.value} output contains other-altitude artifacts: "
            f"{', '.join(leaked)}")
    return value

