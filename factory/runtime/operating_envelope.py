"""Parse and verify the Project operating envelope inherited by a Story."""

from __future__ import annotations
import hashlib
import json
import re


class EnvelopeError(ValueError):
    pass


def section(body: str, name: str) -> list[str]:
    match = re.search(rf"(?ms)^### {re.escape(name)}\n\n(.*?)(?=\n\n### |\Z)", body)
    return [line.strip() for line in match.group(1).splitlines()
            if line.strip()] if match else []


def parse_project(body: str) -> list[dict]:
    lines = section(body, "Operating envelope")
    if lines == ["None identified."]:
        return []
    entries = []
    for line in lines:
        match = re.fullmatch(
            r"- (OE-[A-Z0-9-]+) \| ([a-z-]+) \| (.+) \| FAIL WHEN: (.+)", line)
        if not match:
            raise EnvelopeError("Project operating envelope is malformed")
        identifier, category, requirement, failure = match.groups()
        entries.append({"id": identifier, "category": category,
                        "requirement": requirement, "failure_condition": failure})
    identifiers = [item["id"] for item in entries]
    if len(set(identifiers)) != len(identifiers):
        raise EnvelopeError("Project operating envelope contains duplicate IDs")
    return entries


def digest(entries: list[dict]) -> str:
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def obligations(story_body: str, project_body: str) -> list[dict]:
    # Existing approved Projects predate this contract. They remain deliverable,
    # but a partial migration is refused; every newly generated Project carries
    # both sections and planning read-back enforces them.
    project_lines = section(project_body, "Operating envelope")
    story_lines = section(story_body, "Operating-envelope obligations")
    if not project_lines and not story_lines:
        return []
    if not project_lines or not story_lines:
        raise EnvelopeError("operating-envelope inheritance is incomplete")
    entries = parse_project(project_body)
    lines = story_lines
    if not lines or not re.fullmatch(r"digest: [0-9a-f]{64}", lines[0]):
        raise EnvelopeError("Story operating-envelope digest is missing")
    if lines[0].removeprefix("digest: ") != digest(entries):
        raise EnvelopeError("Story operating-envelope digest is stale")
    raw_obligations = [] if lines[1:] == ["none"] else lines[1:]
    parsed = []
    for line in raw_obligations:
        match = re.fullmatch(r"(OE-[A-Z0-9-]+) \| STORY CHECK: (.+)", line)
        if match:
            parsed.append((match.group(1), match.group(2)))
        else:
            # Approved Projects created before Story-local checks remain readable.
            parsed.append((line, None))
    identifiers = [identifier for identifier, _check in parsed]
    if len(set(identifiers)) != len(identifiers):
        raise EnvelopeError("Story operating-envelope IDs are duplicated")
    by_id = {item["id"]: item for item in entries}
    if any(identifier not in by_id for identifier in identifiers):
        raise EnvelopeError("Story references an unknown operating-envelope ID")
    checks = dict(parsed)
    return [{**by_id[identifier], **({"story_check": checks[identifier]}
                                    if checks[identifier] else {})}
            for identifier in identifiers]
