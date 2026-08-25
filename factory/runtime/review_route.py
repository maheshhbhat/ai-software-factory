"""Pure exact-head review routing decisions for the Phase 4 reviewer."""

from __future__ import annotations

import re
from dataclasses import dataclass

LINK = re.compile(r"(?m)^Story: #(\d+)$")
MARKER = re.compile(r"<!-- review-outcome:(\d+):([0-9a-f]{7,64}):(approval|findings) -->")


class RouteError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewTarget:
    story: int
    pull_request: int
    head: str


def story_number(pull: dict) -> int:
    matches = LINK.findall((pull.get("body") or "").replace("\r\n", "\n"))
    if len(matches) != 1:
        raise RouteError("pull request must contain exactly one canonical Story link")
    return int(matches[0])


def outcomes(comments: list[dict], pull_number: int, head: str) -> list[str]:
    found = []
    for comment in comments:
        for pr, sha, verdict in MARKER.findall(comment.get("body") or ""):
            if int(pr) == pull_number and sha == head:
                found.append(verdict)
    return found


def target(pull: dict, story: dict, comments: list[dict]) -> ReviewTarget | None:
    """Return a review target once per open PR head; reject ambiguous history."""
    if pull.get("state") != "open" or pull.get("draft"):
        return None
    number = story_number(pull)
    if number != story.get("number"):
        raise RouteError("pull request Story link does not match supplied Story")
    labels = {x.get("name", "") for x in story.get("labels", [])}
    if "story:in-review" not in labels:
        return None
    head = (pull.get("head") or {}).get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{7,64}", head):
        raise RouteError("pull request head SHA is missing or malformed")
    existing = outcomes(comments, pull["number"], head)
    if len(existing) > 1:
        raise RouteError("duplicate outcomes exist for the current head")
    if existing:
        return None
    return ReviewTarget(number, pull["number"], head)


def behind_targets(pulls: list[dict], issues: dict[int, dict]) -> list[int]:
    """Return factory review PRs whose branch GitHub reports behind its base."""
    found = []
    for pull in sorted(pulls, key=lambda value: value.get("number", 0)):
        if (pull.get("state") != "open" or pull.get("draft") or
                pull.get("mergeable_state") != "behind"):
            continue
        if not LINK.findall((pull.get("body") or "").replace("\r\n", "\n")):
            continue
        number = story_number(pull)
        story = issues.get(number) or {}
        labels = {item.get("name", "") for item in story.get("labels", [])}
        if "story:in-review" in labels:
            found.append(pull["number"])
    return found


def marker(pull_number: int, head: str, verdict: str) -> str:
    if verdict not in ("approval", "findings"):
        raise RouteError("review verdict must be approval or findings")
    return f"<!-- review-outcome:{pull_number}:{head}:{verdict} -->"
