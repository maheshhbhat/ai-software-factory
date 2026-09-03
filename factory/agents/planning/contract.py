"""Deterministic boundary around the judgment-bearing planning prompt.

The model may propose a plan; this module decides which altitude is authorized
and whether the returned envelope is complete enough to write and verify.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
import shlex

from factory.gates.merge_gate import match_path


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
    "expected_bells", "risks", "digest",
})

ENVELOPE_CATEGORIES = ("representative-input", "responsiveness",
                       "external-provider", "work-bound", "degradation")

BROWSER_ASSURANCE_TERMS = ("chrome", "real-browser", "named-browser")
ESTABLISHED_BROWSER_TOOLS = ("playwright", "selenium", "webdriver", "cypress",
                             "puppeteer")
RAW_BROWSER_LAUNCH_PATTERNS = (
    r"google chrome\.app", r"\bchrome_bin\b", r"--dump-dom",
    r"--remote-debugging", r"child_process", r"\bspawn\s*\(",
    r"\bexecfile\s*\(",
)

VERIFICATION_MARKER = " || VERIFY "
AUTOMATED_VERIFICATION_FIELDS = frozenset({
    "type", "scope", "executor", "executor_source", "action", "expected", "failure",
})
HUMAN_VERIFICATION_FIELDS = frozenset({
    "type", "scope", "action", "expected", "failure", "reason",
})
EXECUTOR_RUNNERS = frozenset({
    "bash", "node", "npx", "npm", "python", "python3", "pytest", "sh",
})
SHELL_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|", "&"})


def proposes_raw_browser_launcher(text: str) -> bool:
    """True only when a plan proposes a raw launcher, not when it forbids one."""
    for sentence in re.split(r"[.\n;]+", text):
        if not any(re.search(pattern, sentence, re.I)
                   for pattern in RAW_BROWSER_LAUNCH_PATTERNS):
            continue
        if re.search(
                r"\b(?:do not|does not|must not|never|without|forbid(?:s|den)?|"
                r"reject(?:s|ed)?|prevent(?:s|ed)?|fail(?:s|ed)? if|block(?:s|ed)?)\b",
                sentence, re.I):
            continue
        return True
    return False

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
                "acceptance_criteria": {"type": "array", "items": {
                    "type": "string", "pattern": r" \|\| VERIFY \{"},
                                        "minItems": 1},
                "operating_envelope_ids": {"type": "array",
                                           "items": {"type": "string"}},
                "operating_envelope_checks": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string", "pattern": "^OE-[A-Z0-9-]+$"},
                        "check": {"type": "string", "minLength": 1}},
                    "required": ["id", "check"]}},
                "scope": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "spend_cap": {"type": "string"}},
            "required": ["key", "title", "spec", "phase", "depends_on", "hazard",
                         "acceptance_criteria", "operating_envelope_ids",
                         "operating_envelope_checks",
                         "scope", "spend_cap"]}},
        "expected_bells": {"type": "integer", "minimum": 2},
        "risks": {"type": "string", "minLength": 1},
        "digest": {"type": "string"},
    }, "required": ["altitude", "acceptance_criteria", "operating_envelope", "adr",
                    "stories", "expected_bells", "risks", "digest"],
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


def validate_output(altitude: Altitude, value: dict,
                    repository: dict | None = None) -> dict:
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
    forbidden = PROJECT_KEYS - {"altitude", "risks"} if altitude is Altitude.CAMPAIGN else {
        "project", "rationale",
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
        envelope_by_id = {item["id"]: item for item in envelope}
        used = set()
        for story in stories:
            obligations = story.get("operating_envelope_ids") if isinstance(story, dict) else None
            if not isinstance(obligations, list) or len(set(obligations)) != len(obligations):
                raise ContractError("Story operating-envelope obligations are malformed")
            if any(item not in known for item in obligations):
                raise ContractError("Story references an unknown operating-envelope ID")
            checks = story.get("operating_envelope_checks")
            check_ids = [item.get("id") for item in checks
                         if isinstance(item, dict)] if isinstance(checks, list) else []
            if (not isinstance(checks, list) or len(check_ids) != len(checks)
                    or len(set(check_ids)) != len(check_ids)
                    or set(check_ids) != set(obligations)
                    or any(set(item) != {"id", "check"}
                           or not isinstance(item["check"], str)
                           or not item["check"].strip() for item in checks)):
                raise ContractError(
                    "every Story operating-envelope obligation needs exactly one "
                    "Story-local executable check")
            story_surface = " ".join([
                str(story.get("title") or ""), str(story.get("spec") or ""),
                *[str(item) for item in (story.get("acceptance_criteria") or [])],
                *[str(item) for item in (story.get("scope") or [])],
            ]).lower()
            if any(term in story_surface for term in BROWSER_ASSURANCE_TERMS):
                if proposes_raw_browser_launcher(story_surface):
                    raise ContractError(
                        f"Story {story.get('key')!r} proposes a raw browser launcher; "
                        "use an established browser-testing tool")
                if not any(tool in story_surface for tool in ESTABLISHED_BROWSER_TOOLS):
                    raise ContractError(
                        f"Story {story.get('key')!r} promises named-browser assurance "
                        "without an established browser-testing tool")
                if "headless" not in story_surface:
                    raise ContractError(
                        f"Story {story.get('key')!r} promises named-browser assurance "
                        "without headless execution")
                if not any(term in story_surface for term in
                           ("github actions", "linux runner", "ci runner",
                            ".github/workflows")):
                    raise ContractError(
                        f"Story {story.get('key')!r} promises named-browser assurance "
                        "without a supported CI or Linux runner")
                if "favicon" not in story_surface:
                    raise ContractError(
                        f"Story {story.get('key')!r} promises named-browser web "
                        "assurance without checking favicon handling")
                if ("console error" in story_surface and
                        not any(term in story_surface for term in
                                ("failed request", "request failure", "network error",
                                 "http error"))):
                    raise ContractError(
                        f"Story {story.get('key')!r} checks console errors but not failed "
                        "page requests such as missing assets")
            surface_terms = {
                "browser": ("browser", "chrome", "dom", "page", "click", "render", "ui"),
                "provider": ("provider", "live", "network", "http", "fetch", "parse"),
            }
            has_term = lambda text, term: bool(re.search(  # noqa: E731 - local predicate
                rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text))
            for check in checks:
                obligation = envelope_by_id[check["id"]]
                demanded = " ".join((obligation["requirement"],
                                      obligation["failure_condition"],
                                      check["check"])).lower()
                for surface, terms in surface_terms.items():
                    if (any(has_term(demanded, term) for term in terms)
                            and not any(has_term(story_surface, term) for term in terms)):
                        raise ContractError(
                            f"Story {story.get('key')!r} cannot verify {surface} "
                            f"obligation {check['id']} within its declared scope")
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
    if repository is not None:
        validate_acceptance_verification(value, repository)
        validate_repository_compatibility(value, repository)
    return value


def validate_acceptance_verification(value: dict, repository: dict) -> dict:
    """Bind every planned Story criterion to a deterministic executor or bell."""
    if value.get("altitude") != Altitude.PROJECT.value:
        return value
    files = {path for path in repository.get("files", []) if isinstance(path, str)}
    human_bells = 0
    for story in value.get("stories", []):
        key = story.get("key")
        scope = story.get("scope") or []
        for criterion in story.get("acceptance_criteria") or []:
            if not isinstance(criterion, str) or criterion.count(VERIFICATION_MARKER) != 1:
                raise ContractError(
                    f"Story {key!r} acceptance criterion lacks one verification record")
            claim, encoded = criterion.split(VERIFICATION_MARKER, 1)
            if not claim.strip():
                raise ContractError(f"Story {key!r} acceptance criterion is empty")
            try:
                record = json.loads(encoded)
            except (TypeError, json.JSONDecodeError) as error:
                raise ContractError(
                    f"Story {key!r} acceptance verification is not valid JSON") from error
            if not isinstance(record, dict):
                raise ContractError(
                    f"Story {key!r} acceptance verification must be an object")
            kind = record.get("type")
            expected_fields = (AUTOMATED_VERIFICATION_FIELDS if kind == "automated"
                               else HUMAN_VERIFICATION_FIELDS if kind == "human-bell"
                               else None)
            if expected_fields is None or set(record) != expected_fields:
                raise ContractError(
                    f"Story {key!r} acceptance verification has an invalid executor type "
                    "or fields")
            if not all(isinstance(record.get(field), str) and record[field].strip()
                       for field in expected_fields):
                raise ContractError(
                    f"Story {key!r} acceptance verification fields must be non-empty strings")
            verification_scope = record["scope"]
            if not _scope_authorizes(verification_scope, scope):
                raise ContractError(
                    f"Story {key!r} acceptance verification scope "
                    f"{verification_scope!r} is not authorized by Story scope")
            if kind == "human-bell":
                human_bells += 1
                continue
            executor = record["executor"]
            if re.search(r"[*?\[\]{}]", executor):
                raise ContractError(
                    f"Story {key!r} acceptance executor must be one concrete path")
            try:
                command = shlex.split(record["action"])
            except ValueError as error:
                raise ContractError(
                    f"Story {key!r} acceptance action is not a valid command") from error
            runner = command[0].rsplit("/", 1)[-1] if command else ""
            if (executor not in command or any(token in SHELL_CONTROL_TOKENS
                                               for token in command)
                    or command[0] != executor and runner not in EXECUTOR_RUNNERS):
                raise ContractError(
                    f"Story {key!r} acceptance action does not invoke its executor")
            source = record["executor_source"]
            if source == "existing":
                if not _repository_path_resolves(executor, files):
                    raise ContractError(
                        f"Story {key!r} acceptance executor does not exist: {executor!r}")
            elif source == "create":
                if executor in files or not _scope_authorizes(executor, scope):
                    raise ContractError(
                        f"Story {key!r} acceptance executor is not created within "
                        f"authorized scope: {executor!r}")
            else:
                raise ContractError(
                    f"Story {key!r} acceptance executor_source must be 'existing' or 'create'")
    expected_bells = value.get("expected_bells")
    if human_bells and (not isinstance(expected_bells, int)
                        or expected_bells < 2 + human_bells):
        raise ContractError(
            "project expected_bells must count every declared human acceptance bell")
    return value


PATH_VALUE = (r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.*?{}\[\]-]+)+|"
              r"[A-Za-z0-9_-]+\.(?:js|mjs|cjs|ts|tsx|jsx|py|json|ya?ml|md|toml)")
EXISTING_PATH_CLAIM = re.compile(
    r"\b(?:(?:reuse\s+(?:the\s+)?)?(?:existing|current)\s+"
    r"(?:(?:implementation|test|manifest|workflow|verification|path|file)\s+){0,3}"
    r"(?:at\s+|in\s+|from\s+)?|"
    r"(?:implementation|manifest|workflow|verification path|test path)\s+"
    r"(?:at\s+|in\s+|from\s+))"
    rf"(?:`|['\"])?({PATH_VALUE})(?:`|['\"])?(?![A-Za-z0-9_./-])", re.I)
DEPENDENCY_PROPOSAL = re.compile(
    r"\b(?:add|install|introduce|require|use)\s+(?:the\s+)?(?:dependency\s+)?"
    r"[`'\"]?([A-Za-z0-9@/_.-]+)", re.I)


def _story_text(story: dict) -> str:
    return "\n".join(str(item) for item in (
        story.get("title") or "", story.get("spec") or "",
        *(story.get("acceptance_criteria") or []),
        *(item.get("check", "") for item in
          (story.get("operating_envelope_checks") or []) if isinstance(item, dict)),
    ))


def _repository_path_resolves(pattern: str, files: set[str]) -> bool:
    normalized = pattern.rstrip("/")
    return normalized in files or any(match_path(normalized, path) for path in files)


def _scope_resolves(pattern: str, files: set[str]) -> bool:
    normalized = pattern.rstrip("/")
    recursive_root = normalized[:-3].rstrip("/") if normalized.endswith("/**") else ""
    parent = normalized.rsplit("/", 1)[0] if "/" in normalized else ""
    new_file_beside_existing = (not re.search(r"[*?\[]", normalized)
                                and (not parent or any(
                                    path.startswith(parent + "/") for path in files)))
    return (_repository_path_resolves(normalized, files) or new_file_beside_existing
            or bool(recursive_root) and any(
                path == recursive_root or path.startswith(recursive_root + "/")
                or path.startswith(recursive_root + ".") for path in files))


def _scope_authorizes(path: str, scope: list[str]) -> bool:
    return any(match_path(pattern, path)
               for pattern in scope if isinstance(pattern, str))


def _owner_facts(repository: dict) -> list[dict]:
    facts = repository.get("production_owners", repository.get("ownership", []))
    if isinstance(facts, dict):
        return [{"behavior": behavior, "path": path}
                for behavior, paths in facts.items()
                for path in ([paths] if isinstance(paths, str) else paths)]
    if not isinstance(facts, list):
        return []
    return [item for item in facts
            if isinstance(item, dict) and isinstance(item.get("path"), str)
            and (isinstance(item.get("behavior"), str)
                 or isinstance(item.get("story_key"), str))]


def _forbidden_dependencies(repository: dict) -> set[str]:
    forbidden = {str(item).lower()
                 for item in (repository.get("forbidden_dependencies") or [])}
    assertions = repository.get("policy_assertions") or repository.get("assertions") or []
    for item in assertions:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").replace("_", "-").lower()
        subject = str(item.get("subject") or "").lower()
        if kind == "forbidden-dependency" or (kind == "forbidden" and subject == "dependency"):
            name = item.get("name", item.get("value"))
            if isinstance(name, str) and name:
                forbidden.add(name.lower())
    return forbidden


def validate_repository_compatibility(value: dict, repository: dict) -> dict:
    """Reject only contradictions mechanically established by repository facts."""
    if value.get("altitude") != Altitude.PROJECT.value:
        return value
    files = {path for path in repository.get("files", [])
             if isinstance(path, str)}
    owners = _owner_facts(repository)
    forbidden = _forbidden_dependencies(repository)
    for story in value.get("stories", []):
        key = story.get("key")
        scope = story.get("scope") or []
        for pattern in scope:
            if not isinstance(pattern, str) or not _scope_resolves(pattern, files):
                raise ContractError(
                    f"Story {key!r} scope path/pattern does not resolve: {pattern!r}")
        text = _story_text(story)
        for claim in EXISTING_PATH_CLAIM.finditer(text):
            path = claim.group(1).rstrip(".,")
            if not _repository_path_resolves(path, files):
                raise ContractError(
                    f"Story {key!r} claims an existing repository path that does "
                    f"not resolve: {path!r}")
        lowered = text.lower()
        for fact in owners:
            path = fact.get("path")
            behavior = fact.get("behavior", fact.get("claim", ""))
            required_key = fact.get("story_key")
            applies = (required_key == key or
                       isinstance(behavior, str) and bool(behavior)
                       and behavior.lower() in lowered)
            if (applies and isinstance(path, str)
                    and _repository_path_resolves(path, files)
                    and not _scope_authorizes(path, scope)):
                raise ContractError(
                    f"Story {key!r} promises behavior owned by {path!r} but omits "
                    "that production owner from scope")
        proposed = set()
        for match in DEPENDENCY_PROPOSAL.finditer(text):
            prefix = text[max(0, match.start() - 24):match.start()]
            if re.search(r"\b(?:do not|must not|avoid|forbid)\s*$", prefix, re.I):
                continue
            proposed.add(_dependency_name(match.group(1)))
        contradicted = sorted(proposed if "*" in forbidden else proposed & forbidden)
        if contradicted:
            raise ContractError(
                f"Story {key!r} proposes forbidden dependency "
                f"{contradicted[0]!r} contrary to repository policy evidence")
    return value


def _dependency_name(value: str) -> str:
    candidate = value.rstrip(".,").lower()
    if candidate.startswith("@"):
        separator = candidate.find("@", 1)
        return candidate if separator < 0 else candidate[:separator]
    return candidate.split("@", 1)[0]
