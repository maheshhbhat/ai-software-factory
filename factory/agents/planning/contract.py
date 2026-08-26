"""Deterministic boundary around the judgment-bearing planning prompt.

The model may propose a plan; this module decides which altitude is authorized
and whether the returned envelope is complete enough to write and verify.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class ContractError(ValueError):
    pass


class Altitude(str, Enum):
    CAMPAIGN = "campaign"
    PROJECT = "project"


TRIGGER_LABELS = {
    "type:roadmap-commitment": Altitude.CAMPAIGN,
    "type:project": Altitude.PROJECT,
}

REQUIRED_INPUTS = ("trigger", "product", "adrs", "repository", "review_comments",
                   "existing_plan")

CAMPAIGN_KEYS = frozenset({"altitude", "project", "rationale", "risks"})
PROJECT_KEYS = frozenset({
    "altitude", "acceptance_criteria", "operating_envelope", "adr", "stories",
    "expected_bells", "digest",
})

ENVELOPE_CATEGORIES = ("representative-input", "responsiveness",
                       "external-provider", "work-bound", "degradation")

CAMPAIGN_JSON_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "altitude": {"type": "string", "const": "campaign"},
        "project": {"type": "object", "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"}, "goal": {"type": "string"},
                        "acceptance_criteria": {"type": "array", "items": {"type": "string"},
                                                "minItems": 1},
                        "expected_bells": {"type": "integer", "minimum": 2},
                        "risks": {"type": "string"}},
                    "required": ["title", "goal", "acceptance_criteria",
                                 "expected_bells", "risks"]},
        "rationale": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    }, "required": ["altitude", "project", "rationale", "risks"],
}

PROJECT_JSON_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "altitude": {"type": "string", "const": "project"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"},
                                "minItems": 1},
        "operating_envelope": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "pattern": "^OE-[A-Z0-9-]+$"},
                "category": {"type": "string", "enum": list(ENVELOPE_CATEGORIES)},
                "requirement": {"type": "string", "minLength": 1},
                "failure_condition": {"type": "string", "minLength": 1}},
            "required": ["id", "category", "requirement", "failure_condition"]}},
        "adr": {"type": "object", "additionalProperties": False,
                "properties": {key: ({"type": "string"} if key in
                                      {"title", "context", "decision"} else
                                      {"type": "array", "items": {"type": "string"},
                                       "minItems": 1})
                               for key in ("title", "context", "decision",
                                           "alternatives", "consequences")},
                "required": ["title", "context", "decision", "alternatives", "consequences"]},
        "stories": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "key": {"type": "string"}, "title": {"type": "string"},
                "spec": {"type": "string"},
                "phase": {"type": "string",
                          "enum": ["build", "ship", "shadow", "cutover", "hardening"]},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "hazard": {"type": "boolean"},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"},
                                        "minItems": 1},
                "operating_envelope_ids": {"type": "array",
                                           "items": {"type": "string"}},
                "scope": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "spend_cap": {"type": "string"}},
            "required": ["key", "title", "spec", "phase", "depends_on", "hazard",
                         "acceptance_criteria", "operating_envelope_ids",
                         "scope", "spend_cap"]}},
        "expected_bells": {"type": "integer", "minimum": 2},
        "digest": {"type": "string"},
    }, "required": ["altitude", "acceptance_criteria", "operating_envelope", "adr",
                    "stories", "expected_bells", "digest"],
}


def json_schema(altitude: Altitude) -> dict:
    return CAMPAIGN_JSON_SCHEMA if altitude is Altitude.CAMPAIGN else PROJECT_JSON_SCHEMA


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
    if not isinstance(value["review_comments"], (list, tuple)):
        raise ContractError("review comments must be a list")
    if not isinstance(value["existing_plan"], dict):
        raise ContractError("existing plan must be an object")
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
    if altitude is Altitude.PROJECT:
        envelope = value.get("operating_envelope")
        stories = value.get("stories")
        if not isinstance(envelope, list) or not isinstance(stories, list):
            raise ContractError("project operating envelope and stories must be arrays")
        identifiers = [item.get("id") for item in envelope if isinstance(item, dict)]
        if len(identifiers) != len(envelope) or len(set(identifiers)) != len(identifiers):
            raise ContractError("operating envelope IDs must be unique objects")
        for item in envelope:
            if (not re.fullmatch(r"OE-[A-Z0-9-]+", item.get("id") or "") or
                    item.get("category") not in ENVELOPE_CATEGORIES or
                    not all(isinstance(item.get(field), str) and item[field].strip()
                            for field in ("requirement", "failure_condition"))):
                raise ContractError("operating envelope entry is malformed")
        known = set(identifiers)
        used = set()
        for story in stories:
            obligations = story.get("operating_envelope_ids") if isinstance(story, dict) else None
            if not isinstance(obligations, list) or len(set(obligations)) != len(obligations):
                raise ContractError("Story operating-envelope obligations are malformed")
            if any(item not in known for item in obligations):
                raise ContractError("Story references an unknown operating-envelope ID")
            used.update(obligations)
        if used != known:
            raise ContractError("every operating-envelope ID must belong to a Story")
        digest = value.get("digest")
        if not isinstance(digest, str):
            raise ContractError("project digest must be a string")
        required_sections = ("Plan in plain language", "How the plan works",
                             "Story dependencies")
        sections = {}
        for heading in required_sections:
            match = re.search(
                rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", digest)
            if not match or not match.group(1).strip():
                raise ContractError(f"project digest section {heading!r} is missing or empty")
            sections[heading] = match.group(1)
        for heading in ("How the plan works", "Story dependencies"):
            section = sections[heading]
            diagram = re.search(r"(?ms)```mermaid\s*\n.*?^```\s*$", section)
            if not diagram:
                raise ContractError(f"project digest section {heading!r} lacks a Mermaid diagram")
            fallback = section[diagram.end():]
            prose = re.sub(r"(?ms)```.*?^```\s*$", "", fallback).strip()
            if not prose:
                raise ContractError(
                    f"project digest section {heading!r} lacks a textual fallback")
        if len(re.findall(r"```mermaid\s*\n", digest)) < 2:
            raise ContractError(
                "project digest must contain a plain-language explanation and two Mermaid "
                "diagrams (system flow and story dependencies)")
    return value
