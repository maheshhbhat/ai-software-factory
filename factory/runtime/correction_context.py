"""Deterministic, bounded correction evidence for a delivery retry.

Comments are public data, not authorization.  This module recognizes only the
two factory-owned marker dialects, binds every record to the current Story, PR,
and exact head, and returns a canonical packet shared by dispatcher and worker.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

SCHEMA_VERSION = 1
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "COLLABORATOR", "MEMBER"})
HUMAN_KINDS = frozenset({"human-review", "request-changes", "retry-authorization"})
RECORD_MAX = 4096
PACKET_MAX = 12288

REVIEW_MARKER = re.compile(
    r"<!--\s*review-outcome:(?P<pr>[1-9][0-9]*):"
    r"(?P<head>[0-9a-f]{40}):(?P<outcome>approved|findings)\s*-->")
CORRECTION_MARKER = re.compile(
    r"<!--\s*correction-context:v1:(?P<kind>human-review|request-changes|"
    r"retry-authorization):story:(?P<story>[1-9][0-9]*):"
    r"pr:(?P<pr>[1-9][0-9]*):head:(?P<head>[0-9a-f]{40})\s*-->")
PROVENANCE = re.compile(r"(?is)Mahesh.*(?:in|active).*session.*transcrib")
BANNED = (
    "<!-- factory-worker-start:v1 -->",
    "## Factory failure recovery",
    "## Operator recovery",
    "engine output tail:",
)
CREDENTIAL = re.compile(
    r"(?i)(?:GH_TOKEN|GITHUB_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
    r"Authorization\s*:\s*Bearer)\s*(?:=|:|\s)")


class ContextError(ValueError):
    """The retry context is absent, ambiguous, or unsafe."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class Target:
    story: int
    pull_request: int
    head: str
    attempt: int


def marker(*, kind: str, story: int, pull_request: int, head: str) -> str:
    """Canonical marker human-decision transcription code can append."""
    if kind not in HUMAN_KINDS or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("invalid correction-context marker fields")
    return (f"<!-- correction-context:v1:{kind}:story:{story}:"
            f"pr:{pull_request}:head:{head} -->")


def _trusted(comment: dict) -> bool:
    return (comment.get("author_association") or "").upper() in TRUSTED_ASSOCIATIONS


def _identity(comment: dict) -> str:
    value = comment.get("id")
    if not isinstance(value, (int, str)) or str(value).strip() == "":
        raise ContextError("CORRECTION_COMMENT_ID_INVALID", repr(value))
    return str(value)


def _created(comment: dict) -> str:
    value = comment.get("created_at") or comment.get("createdAt")
    if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[^\s]+Z", value):
        raise ContextError("CORRECTION_COMMENT_TIME_INVALID", repr(value))
    return value


def _record(kind: str, source: str, comment: dict) -> dict:
    body = (comment.get("body") or "").replace("\r\n", "\n")
    if len(body.encode("utf-8")) > RECORD_MAX:
        raise ContextError("CORRECTION_RECORD_OVERSIZED",
                           f"comment {_identity(comment)} exceeds {RECORD_MAX} bytes")
    if any(value in body for value in BANNED):
        raise ContextError("CORRECTION_TRANSCRIPT_FORBIDDEN",
                           f"comment {_identity(comment)} contains operational transcript")
    if CREDENTIAL.search(body):
        raise ContextError("CORRECTION_CREDENTIAL_FORBIDDEN",
                           f"comment {_identity(comment)} contains credential-shaped text")
    return {"kind": kind, "source": source, "comment_id": _identity(comment),
            "created_at": _created(comment), "body": body}


def assemble(*, repository: str, project: int, story: dict, pull_request: dict | None,
             story_comments: list[dict], pull_comments: list[dict]) -> dict:
    """Build a canonical packet or raise a named fail-closed refusal.

    Fresh delivery has no linked PR and therefore no correction records.  A
    linked PR is a retry and requires one trusted current-head findings marker.
    Human records are optional, but any current-target marker is validated
    strictly rather than silently dropped.
    """
    number = story.get("number")
    attempt_raw = re.search(
        r"(?ms)^### Attempt\s*$\n(.*?)(?=^### |\Z)", story.get("body") or "")
    attempt_text = attempt_raw.group(1).strip() if attempt_raw else ""
    if (not isinstance(number, int) or not isinstance(project, int) or
            not re.fullmatch(r"[^/\s]+/[^/\s]+", repository)):
        raise ContextError("CORRECTION_TARGET_INVALID",
                           f"repository={repository!r} project={project!r} "
                           f"story={number!r} Attempt={attempt_text!r}")

    if pull_request is None:
        base = {"schema_version": SCHEMA_VERSION, "retry": False,
                "repository": repository, "project": project,
                "story": number, "pull_request": None, "head": None,
                "attempt": int(attempt_text) if attempt_text.isdigit() else None,
                "records": []}
        base["digest"] = digest(base)
        return base

    if not attempt_text.isdigit():
        raise ContextError("CORRECTION_TARGET_INVALID",
                           f"story={number!r} Attempt={attempt_text!r}")

    pr = pull_request.get("number")
    head = ((pull_request.get("head") or {}).get("sha") or "").lower()
    if not isinstance(pr, int) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ContextError("CORRECTION_TARGET_INVALID", f"PR={pr!r} head={head!r}")
    target = Target(number, pr, head, int(attempt_text))

    candidates: list[dict] = []
    for source, comments in (("story", story_comments), ("pull_request", pull_comments)):
        for comment in comments:
            if not _trusted(comment):
                continue
            body = (comment.get("body") or "").replace("\r\n", "\n")
            project_refs = {int(value) for value in re.findall(
                r"(?i)\bProject\s*#([1-9][0-9]*)", body)}
            if project_refs - {project}:
                continue
            for match in REVIEW_MARKER.finditer(body):
                if (int(match.group("pr")), match.group("head")) == (pr, head) and \
                        match.group("outcome") == "findings":
                    candidates.append(_record("review-findings", source, comment))
            for match in CORRECTION_MARKER.finditer(body):
                marker_target = (int(match.group("story")), int(match.group("pr")),
                                 match.group("head"))
                current = (number, pr, head)
                if marker_target != current:
                    continue  # stale or unrelated structured evidence is not current input.
                if not PROVENANCE.search(body):
                    raise ContextError("CORRECTION_PROVENANCE_MISSING",
                                       f"comment {_identity(comment)} kind={match.group('kind')}")
                candidates.append(_record(match.group("kind"), source, comment))

    findings = [item for item in candidates if item["kind"] == "review-findings"]
    if len(findings) != 1:
        raise ContextError("CURRENT_REVIEW_FINDING_REQUIRED",
                           f"expected 1 for PR #{pr} head {head}; found {len(findings)}")
    finding_time = findings[0]["created_at"]
    records = [item for item in candidates
               if item["kind"] == "review-findings" or item["created_at"] > finding_time]
    records.sort(key=lambda item: (item["created_at"], item["comment_id"], item["kind"]))

    for kind in HUMAN_KINDS:
        count = sum(item["kind"] == kind for item in records)
        if count > 1:
            raise ContextError("CORRECTION_KIND_AMBIGUOUS", f"{kind} count={count}")
    if records[0]["kind"] != "review-findings":
        raise ContextError("CORRECTION_CHRONOLOGY_INVALID", "finding is not first")

    base = {"schema_version": SCHEMA_VERSION, "retry": True,
            "repository": repository, "project": project,
            "story": target.story, "pull_request": target.pull_request,
            "head": target.head, "attempt": target.attempt, "records": records}
    encoded = json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > PACKET_MAX:
        raise ContextError("CORRECTION_PACKET_OVERSIZED",
                           f"{len(encoded)} bytes exceeds {PACKET_MAX}")
    base["digest"] = digest(base)
    return base


def digest(packet: dict) -> str:
    value = {key: packet[key] for key in packet if key != "digest"}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
