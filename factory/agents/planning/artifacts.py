"""Durable planning artifacts and independent repository read-back.

The writer accepts a validated planning envelope and writes only GitHub issues,
labels, and comments. Stable markers make each write resumable and idempotent.
The verifier then reconstructs the result from the store; writer return values
are never treated as proof.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

import contract

MARKER = "planning-artifact"
PHASES = frozenset({"build", "ship", "shadow", "cutover", "hardening"})
HAZARD_NAMES = frozenset({
    "package.json", "package-lock.json", "requirements.txt", "pyproject.toml",
    "poetry.lock", "go.mod", "go.sum", "cargo.toml", "cargo.lock",
})


class ArtifactError(ValueError):
    pass


class Store(Protocol):
    def list_issues(self, state: str = "all") -> list[dict]: ...
    def get_issue(self, number: int) -> dict: ...
    def create_issue(self, title: str, body: str, labels: list[str]) -> dict: ...
    def update_issue(self, number: int, body: str, title: str | None = None) -> dict: ...
    def update_labels(self, number: int, labels: list[str]) -> dict: ...
    def list_comments(self, number: int) -> list[dict]: ...
    def create_comment(self, number: int, body: str) -> dict: ...
    def update_comment(self, comment_id: int, body: str) -> dict: ...
    def ensure_label(self, name: str) -> None: ...


@dataclass(frozen=True)
class WrittenPlan:
    altitude: contract.Altitude
    project: int | None
    adr: int | None
    stories: tuple[int, ...]


def marker(key: str, kind: str) -> str:
    return f"<!-- {MARKER}:{key}:{kind} -->"


def _find(items: list[dict], token: str) -> dict | None:
    found = [item for item in items if token in (item.get("body") or "")]
    if len(found) > 1:
        raise ArtifactError(f"duplicate durable artifact for {token}")
    return found[0] if found else None


def _comment_once(store: Store, number: int, key: str, kind: str, body: str) -> dict:
    token = marker(key, kind)
    existing = _find(store.list_comments(number), token)
    return existing or store.create_comment(number, f"{token}\n\n{body}")


def _issue_once(store: Store, key: str, kind: str, title: str,
                body: str, labels: list[str]) -> dict:
    token = marker(key, kind)
    existing = _find(store.list_issues("all"), token)
    if existing:
        return existing
    for label in labels:
        store.ensure_label(label)
    return store.create_issue(title, f"{token}\n\n{body}", labels)


def _prior(items: list[dict], artifact: int, kind: str) -> dict | None:
    pattern = re.compile(
        rf"<!-- {MARKER}:{artifact}:[^\n]*:project:prompt-[^\n]*:{re.escape(kind)} -->")
    found = [item for item in items if pattern.search(item.get("body") or "")]
    if len(found) > 1:
        raise ArtifactError(f"duplicate prior planning artifact for {kind}")
    return found[0] if found else None


def _issue_reconcile(store: Store, artifact: int, key: str, kind: str, title: str,
                     body: str, labels: list[str]) -> dict:
    current = _find(store.list_issues("all"), marker(key, kind))
    if current:
        return current
    prior = _prior(store.list_issues("all"), artifact, kind)
    if not prior:
        return _issue_once(store, key, kind, title, body, labels)
    for label in labels:
        store.ensure_label(label)
    store.update_issue(prior["number"], f"{marker(key, kind)}\n\n{body}", title)
    store.update_labels(prior["number"], labels)
    return store.get_issue(prior["number"])


def _comment_reconcile(store: Store, artifact: int, key: str, kind: str, body: str) -> dict:
    comments = store.list_comments(artifact)
    current = _find(comments, marker(key, kind))
    if current:
        return current
    prior = _prior(comments, artifact, kind)
    rendered = f"{marker(key, kind)}\n\n{body}"
    return (store.update_comment(prior["id"], rendered) if prior else
            store.create_comment(artifact, rendered))


def _project_body(project: dict, commitment: int) -> str:
    criteria = project.get("acceptance_criteria") or []
    if not criteria:
        raise ArtifactError("campaign project requires acceptance criteria")
    return (f"### Goal\n\n{project['goal']}\n\n"
            "### Falsifiable acceptance criteria\n\n"
            + "\n".join(f"- [ ] {item}" for item in criteria)
            + "\n\n### Stories\n\n_No response_\n\n"
            f"### Expected bells\n\n{project['expected_bells']}\n\n"
            f"### Risks / notes\n\n{project.get('risks', 'None identified.')}\n\n"
            f"### Roadmap commitment\n\n#{commitment}\n")


def write_campaign(store: Store, trigger: dict, key: str, output: dict) -> WrittenPlan:
    contract.validate_output(contract.Altitude.CAMPAIGN, output)
    project = output["project"]
    rendered_project = _project_body(project, trigger["number"])
    proposal = ("## Campaign plan proposal\n\n"
                f"### Proposed project\n\n{project['title']}\n\n"
                f"### Rationale\n\n{output['rationale']}\n\n"
                "### Risk order\n\n"
                + "\n".join(f"{index}. {risk}" for index, risk in
                              enumerate(output["risks"], 1)))
    _comment_once(store, trigger["number"], key, "campaign-proposal", proposal)
    created = _issue_once(
        store, key, "project", f"[Project] {project['title']}",
        rendered_project,
        ["type:project", "project:planning"])
    # Repair Projects created by the former campaign contract, which skipped
    # directly to awaiting-ready before any Stories existed.  This is safe to
    # replay: a Project whose Stories section has since been populated is never
    # moved backwards.
    labels = set(created.get("labels") or [])
    if ("project:awaiting-ready" in labels
            and section_lines(created.get("body") or "", "Stories") == ["_No response_"]):
        labels.remove("project:awaiting-ready")
        labels.add("project:planning")
        store.update_labels(created["number"], sorted(labels))
        created = store.get_issue(created["number"])
    return WrittenPlan(contract.Altitude.CAMPAIGN, created["number"], None, ())


def _validate_story_graph(stories: list[dict]) -> list[str]:
    keys = [story.get("key") for story in stories]
    if any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
        raise ArtifactError("story keys must be unique non-empty strings")
    known = set(keys)
    graph = {}
    for story in stories:
        deps = story.get("depends_on")
        if not isinstance(deps, list) or any(dep not in known for dep in deps):
            raise ArtifactError(f"story {story['key']} has an unknown dependency")
        graph[story["key"]] = deps
    visiting, visited, order = set(), set(), []

    def visit(key):
        if key in visiting:
            raise ArtifactError("story dependency graph contains a cycle")
        if key in visited:
            return
        visiting.add(key)
        for dep in graph[key]:
            visit(dep)
        visiting.remove(key)
        visited.add(key)
        order.append(key)

    for key in keys:
        visit(key)
    return order


def _story_body(story: dict, project: int, dependencies: list[int]) -> str:
    required = ("title", "spec", "phase", "hazard", "acceptance_criteria",
                "scope", "spend_cap")
    missing = [field for field in required if field not in story]
    if missing:
        raise ArtifactError(f"story {story.get('key')} missing: {', '.join(missing)}")
    if story["phase"] not in PHASES:
        raise ArtifactError(f"story {story['key']} has unsupported phase {story['phase']!r}")
    depends = "none" if not dependencies else "\n".join(f"#{number}" for number in dependencies)
    hazard = "X" if story["hazard"] else " "
    criteria = story["acceptance_criteria"]
    if not criteria:
        raise ArtifactError(f"story {story['key']} has no acceptance criteria")
    scope = story["scope"]
    if not scope or any(line.startswith("-") for line in scope):
        raise ArtifactError(f"story {story['key']} scope must be bare paths")
    if bool(story["hazard"]) != any(scope_is_hazard(path) for path in scope):
        raise ArtifactError(f"story {story['key']} hazard flag does not match scope")
    return (f"### Spec\n\n{story['spec']}\n\n### Project\n\n#{project}\n\n"
            f"### Phase\n\n{story['phase']}\n\n### Depends-on\n\n{depends}\n\n"
            f"### Hazard\n\n- [{hazard}] Touches hazard path\n\n"
            "### Attempt\n\n0\n\n"
            f"### Spend cap\n\n{story['spend_cap']}\n\n"
            "### Scope\n\n" + "\n".join(scope) + "\n\n"
            "### Acceptance notes\n\n"
            + "\n".join(f"- {item}" for item in criteria) + "\n")


def _adr_body(adr: dict) -> str:
    required = ("title", "context", "decision", "alternatives", "consequences")
    missing = [field for field in required if not adr.get(field)]
    if missing:
        raise ArtifactError(f"ADR missing: {', '.join(missing)}")
    return (f"## Context\n\n{adr['context']}\n\n## Decision\n\n{adr['decision']}\n\n"
            "## Alternatives\n\n" + "\n".join(f"- {item}" for item in adr["alternatives"])
            + "\n\n## Consequences\n\n" + "\n".join(
                f"- {item}" for item in adr["consequences"]) + "\n")


def write_project(store: Store, trigger: dict, key: str, output: dict) -> WrittenPlan:
    contract.validate_output(contract.Altitude.PROJECT, output)
    order = _validate_story_graph(output["stories"])
    by_key = {story["key"]: story for story in output["stories"]}
    if not isinstance(output["expected_bells"], int) or output["expected_bells"] < 2:
        raise ArtifactError("expected_bells must include plan approval and acceptance")
    if not isinstance(output["digest"], str) or not output["digest"].strip():
        raise ArtifactError("planning digest must be non-empty")
    project_criteria = output["acceptance_criteria"]
    if (not isinstance(project_criteria, list) or not project_criteria
            or any(not isinstance(item, str) or not item.strip()
                   for item in project_criteria)):
        raise ArtifactError("project acceptance criteria must be non-empty strings")
    # Validate the complete envelope before the first durable write. Dependency
    # issue numbers are substituted later, but every semantic field is checked now.
    _adr_body(output["adr"])
    for story in output["stories"]:
        _story_body(story, trigger["number"], [1] * len(story["depends_on"]))
    existing_story_keys = {match.group(1) for item in store.list_issues("all")
                           if (match := re.search(
                               rf"<!-- {MARKER}:{trigger['number']}:[^\n]*:project:prompt-[^\n]*:story:([^ ]+) -->",
                               item.get("body") or ""))}
    proposed_story_keys = set(by_key)
    if existing_story_keys and existing_story_keys != proposed_story_keys:
        raise ArtifactError("feedback revision may not silently add or remove story identities")
    adr = _issue_reconcile(store, trigger["number"], key, "adr",
                           f"[ADR] {output['adr']['title']}",
                           _adr_body(output["adr"]), ["type:adr"])
    created = {}
    for story_key in order:
        story = by_key[story_key]
        dep_numbers = [created[dep]["number"] for dep in story["depends_on"]]
        labels = ["type:story", "story:blocked", f"phase:{story['phase']}"]
        if story["hazard"]:
            labels.append("hazard")
        created[story_key] = _issue_reconcile(
            store, trigger["number"], key, f"story:{story_key}", f"[Story] {story['title']}",
            _story_body(story, trigger["number"], dep_numbers), labels)

    live = store.get_issue(trigger["number"])
    body = live.get("body") or ""
    criteria_lines = "\n".join(f"- [ ] {item}" for item in project_criteria)
    body, count = re.subn(
        r"(### Falsifiable acceptance criteria\n\n).*?(\n\n### Stories)",
        rf"\g<1>{criteria_lines}\2", body, count=1, flags=re.S)
    if count != 1:
        raise ArtifactError("project issue has no writable acceptance criteria section")
    story_lines = "\n".join(f"#{created[item]['number']}" for item in order)
    body, count = re.subn(r"(### Stories\n\n).*?(\n\n### Expected bells)",
                          rf"\g<1>{story_lines}\2", body, count=1, flags=re.S)
    if count != 1:
        raise ArtifactError("project issue has no writable Stories section")
    body, count = re.subn(r"(### Expected bells\n\n).*?(\n\n### Risks / notes)",
                          rf"\g<1>{output['expected_bells']}\2", body,
                          count=1, flags=re.S)
    if count != 1:
        raise ArtifactError("project issue has no writable Expected bells section")
    store.update_issue(trigger["number"], body)
    _comment_reconcile(store, trigger["number"], key, "digest",
                       "## Planning digest\n\n" + output["digest"])
    return WrittenPlan(contract.Altitude.PROJECT, trigger["number"], adr["number"],
                       tuple(created[item]["number"] for item in order))


def write(store: Store, trigger: dict, key: str, output: dict) -> WrittenPlan:
    altitude = contract.select_altitude(set(trigger.get("labels") or []))
    if altitude is contract.Altitude.CAMPAIGN:
        return write_campaign(store, trigger, key, output)
    return write_project(store, trigger, key, output)


def verify(store: Store, trigger: dict, key: str,
           altitude: contract.Altitude) -> WrittenPlan:
    """Reconstruct one invocation from durable state and reject partial output."""
    issues = store.list_issues("all")
    if altitude is contract.Altitude.CAMPAIGN:
        project = _find(issues, marker(key, "project"))
        proposal = _find(store.list_comments(trigger["number"]),
                         marker(key, "campaign-proposal"))
        if not project or not proposal:
            raise ArtifactError("campaign read-back missing proposal or project")
        labels = set(project.get("labels") or [])
        stories = section_lines(project.get("body") or "", "Stories")
        planning = {"type:project", "project:planning"} <= labels
        already_expanded = ({"type:project", "project:awaiting-ready"} <= labels
                            and bool(stories) and stories != ["_No response_"])
        if not (planning or already_expanded):
            raise ArtifactError("campaign project labels do not match contract")
        return WrittenPlan(altitude, project["number"], None, ())

    adr = _find(issues, marker(key, "adr"))
    digest = _find(store.list_comments(trigger["number"]), marker(key, "digest"))
    story_prefix = f"<!-- {MARKER}:{key}:story:"
    stories = sorted((item for item in issues
                      if story_prefix in (item.get("body") or "")),
                     key=lambda item: item["number"])
    if not adr or not digest or not stories:
        raise ArtifactError("project read-back missing ADR, stories, or digest")
    if "type:adr" not in set(adr.get("labels") or []):
        raise ArtifactError("ADR label does not conform")
    numbers = {story["number"] for story in stories}
    for story in stories:
        labels = set(story.get("labels") or [])
        phases = [label for label in labels if label.startswith("phase:")]
        deps, error = contract_dependencies(story.get("body") or "")
        if "type:story" not in labels or "story:blocked" not in labels or len(phases) != 1:
            raise ArtifactError(f"story #{story['number']} labels do not conform")
        if error or any(dep not in numbers for dep in deps):
            raise ArtifactError(f"story #{story['number']} dependencies do not conform")
        hazard_checked = "- [X] Touches hazard path" in (story.get("body") or "")
        if hazard_checked != ("hazard" in labels):
            raise ArtifactError(f"story #{story['number']} hazard body/label mismatch")
        scope = section_lines(story.get("body") or "", "Scope")
        if hazard_checked != any(scope_is_hazard(path) for path in scope):
            raise ArtifactError(f"story #{story['number']} hazard flag does not match scope")
        phase = section_lines(story.get("body") or "", "Phase")
        if phase != [phases[0].removeprefix("phase:")] or phase[0] not in PHASES:
            raise ArtifactError(f"story #{story['number']} phase body/label mismatch")
        if section_lines(story.get("body") or "", "Attempt") != ["0"]:
            raise ArtifactError(f"story #{story['number']} attempt does not start at zero")
        if not section_lines(story.get("body") or "", "Acceptance notes"):
            raise ArtifactError(f"story #{story['number']} acceptance criteria missing")
    project = store.get_issue(trigger["number"])
    criteria = section_lines(project.get("body") or "", "Falsifiable acceptance criteria")
    if not criteria or any(not item.startswith("- [ ] ") for item in criteria):
        raise ArtifactError("project acceptance criteria do not conform")
    declared = section_lines(project.get("body") or "", "Stories")
    if set(declared) != {f"#{number}" for number in numbers}:
        raise ArtifactError("project story references do not match durable stories")
    bells = section_lines(project.get("body") or "", "Expected bells")
    if len(bells) != 1 or not bells[0].isdigit() or int(bells[0]) < 2:
        raise ArtifactError("project expected-bells count does not conform")
    return WrittenPlan(altitude, trigger["number"], adr["number"],
                       tuple(story["number"] for story in stories))


def contract_dependencies(body: str) -> tuple[list[int], str | None]:
    raw = re.search(r"(?ms)^### Depends-on\n\n(.*?)(?=\n\n### )", body)
    if not raw:
        return [], "missing"
    lines = [line.strip() for line in raw.group(1).splitlines() if line.strip()]
    if lines == ["none"]:
        return [], None
    if not lines or any(not re.fullmatch(r"#[1-9][0-9]*", line) for line in lines):
        return [], "malformed"
    return [int(line[1:]) for line in lines], None


def section_lines(body: str, name: str) -> list[str]:
    match = re.search(rf"(?ms)^### {re.escape(name)}\n\n(.*?)(?=\n\n### |\Z)", body)
    return [line.strip() for line in match.group(1).splitlines() if line.strip()] if match else []


def scope_is_hazard(path: str) -> bool:
    normalized = path.strip().lower()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    name = normalized.rsplit("/", 1)[-1]
    return (name in HAZARD_NAMES or normalized.startswith(".github/workflows/")
            or normalized.startswith("factory/spec/")
            or normalized.startswith("factory/gates/")
            or "/migrations/" in f"/{normalized}/"
            or any(part in normalized for part in ("secret", "credential", "iam/",
                                                    "branch-protection", "destructive")))


class GitHubStore:
    """Minimal GitHub API adapter. No cache is authoritative."""

    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token
        self.base = f"https://api.github.com/repos/{repo}"

    def _api(self, path: str, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "User-Agent": "factory-planning-agent"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())

    def _pages(self, path):
        out, page = [], 1
        while True:
            separator = "&" if "?" in path else "?"
            batch = self._api(f"{path}{separator}per_page=100&page={page}")
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def list_issues(self, state="all"):
        return [{**item, "labels": [label["name"] for label in item.get("labels", [])]}
                for item in self._pages(f"/issues?state={state}")
                if "pull_request" not in item]

    def get_issue(self, number):
        item = self._api(f"/issues/{number}")
        item["labels"] = [label["name"] for label in item.get("labels", [])]
        return item

    def create_issue(self, title, body, labels):
        return self._api("/issues", "POST", {"title": title, "body": body, "labels": labels})

    def update_issue(self, number, body, title=None):
        payload = {"body": body}
        if title is not None:
            payload["title"] = title
        return self._api(f"/issues/{number}", "PATCH", payload)

    def update_labels(self, number, labels):
        return self._api(f"/issues/{number}", "PATCH", {"labels": labels})

    def list_comments(self, number):
        return self._pages(f"/issues/{number}/comments")

    def create_comment(self, number, body):
        return self._api(f"/issues/{number}/comments", "POST", {"body": body})

    def update_comment(self, comment_id, body):
        return self._api(f"/issues/comments/{comment_id}", "PATCH", {"body": body})

    def ensure_label(self, name):
        encoded = urllib.parse.quote(name, safe="")
        try:
            self._api(f"/labels/{encoded}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            self._api("/labels", "POST", {"name": name, "color": "ededed"})
