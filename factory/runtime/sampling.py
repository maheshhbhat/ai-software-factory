"""Replay-safe post-merge 1-in-3 sampling and audit decisions."""
from __future__ import annotations
import re
import secrets
from dataclasses import dataclass
import runlog

SAMPLE = re.compile(r"<!-- sampling:(\d+):([0-9a-f]{7,64}):(selected|unselected) -->")
HEADING = re.compile(r"(?m)^## Sampling audit$")
DECISION = re.compile(r"(?mi)^decision:\s*(pass|findings)\s*$")
SECONDS = re.compile(r"(?mi)^seconds-spent:\s*(\d+)\s*$")

class SamplingError(RuntimeError): pass

@dataclass(frozen=True)
class Selection:
    pull_request: int
    head: str
    selected: bool
    replay: bool = False

@dataclass(frozen=True)
class AuditDecision:
    verdict: str
    seconds_spent: int
    actor: str
    findings: tuple[str, ...] = ()

def select(pull: dict, comments: list[dict], draw=None) -> Selection:
    if not (pull.get("merged_at") or pull.get("merged") is True):
        raise SamplingError("only a merged pull request can be sampled")
    number, head = int(pull["number"]), (pull.get("head") or {}).get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{7,64}", head):
        raise SamplingError("merged head SHA is unavailable")
    existing = [(sha, value) for c in comments for pr, sha, value in
                SAMPLE.findall(c.get("body") or "") if int(pr) == number]
    if len(existing) > 1: raise SamplingError("duplicate sampling records fail closed")
    if existing:
        sha, value = existing[0]
        if sha != head: raise SamplingError("sampling record is bound to another head")
        return Selection(number, head, value == "selected", True)
    return Selection(number, head, (draw or secrets.randbelow)(3) == 0)

def selection_comment(value: Selection) -> str:
    result = "selected" if value.selected else "unselected"
    return (f"## Sampling decision\n\nPR: #{value.pull_request}\nhead: `{value.head}`\n"
            f"result: {result}\n\n<!-- sampling:{value.pull_request}:{value.head}:{result} -->")

def classify(comment: dict) -> AuditDecision | None:
    body = (comment.get("body") or "").replace("\r\n", "\n")
    if not HEADING.search(body): return None
    if comment.get("author_association") not in ("OWNER", "MEMBER"):
        raise SamplingError("sampling audit decision is not owner-authored")
    verdicts, seconds = DECISION.findall(body), SECONDS.findall(body)
    if len(verdicts) != 1 or len(seconds) != 1:
        raise SamplingError("malformed sampling audit decision")
    findings = tuple(x[2:].strip() for x in body.splitlines()
                     if x.startswith("- ") and x[2:].strip())
    if verdicts[0] == "findings" and not findings:
        raise SamplingError("findings decision requires actionable findings")
    return AuditDecision(verdicts[0], int(seconds[0]),
                         (comment.get("user") or {}).get("login", ""), findings)

def decision(comments: list[dict]) -> AuditDecision | None:
    found = [x for c in comments if (x := classify(c)) is not None]
    if len(found) > 1: raise SamplingError("multiple audit decisions fail closed")
    return found[0] if found else None

def touch_marker(pull: int, head: str) -> str:
    return f"<!-- sampling-touch:{pull}:{head} -->"

def correction_marker(pull: int, head: str) -> str:
    return f"<!-- sampling-correction:{pull}:{head} -->"

def audit_marker(pull: int, head: str) -> str:
    return f"<!-- sampling-audit:{pull}:{head} -->"

def audit_issue(selection: Selection) -> dict:
    return {"title": f"[Audit] Sample merged PR #{selection.pull_request}",
            "body": (f"Audit merged PR #{selection.pull_request} at `{selection.head}`.\n\n"
                     f"{audit_marker(selection.pull_request, selection.head)}"),
            "labels": ["type:audit"]}

def corrective_story(project: int, story: int, selection: Selection,
                     audit: AuditDecision, scope: str = "_No response_") -> dict:
    bullets = "\n".join(f"- {x}" for x in audit.findings)
    body = f"""### Spec

Correct sampled findings from PR #{selection.pull_request}.

{bullets}

### Project

#{project}

### Phase

build

### Depends-on

#{story}

### Hazard

- [ ] Touches hazard path

### Attempt

0

### Spend cap

$20 / 45 min

### Scope

{scope}

### Acceptance notes

- Correct every sampling finding with a regression test.

{correction_marker(selection.pull_request, selection.head)}"""
    return {"title": f"[Story] Correct sampled findings from PR #{selection.pull_request}",
            "body": body, "labels": ["type:story", "story:blocked", "phase:build"]}

def process(client, pull: dict, *, draw=None) -> str:
    """Apply one replay-safe lifecycle pass through a small GitHub-like client."""
    pr = pull["number"]
    pr_comments = client.pages(f"/issues/{pr}/comments")
    chosen = select(pull, pr_comments, draw)
    if not chosen.replay:
        client.api(f"/issues/{pr}/comments", method="POST",
                   value={"body": selection_comment(chosen)})
    if not chosen.selected:
        return "unselected"
    issues = client.pages("/issues?state=all")
    audits = [x for x in issues if audit_marker(pr, chosen.head) in (x.get("body") or "")]
    if len(audits) > 1: raise SamplingError("duplicate audit artifacts fail closed")
    if not audits:
        audit = client.api("/issues", method="POST", value=audit_issue(chosen))
        return f"awaiting:{audit['number']}"
    audit = audits[0]
    comments = client.pages(f"/issues/{audit['number']}/comments")
    verdict = decision(comments)
    if verdict is None: return f"awaiting:{audit['number']}"
    touch = touch_marker(pr, chosen.head)
    touches = [x for x in comments if touch in (x.get("body") or "")]
    if len(touches) > 1: raise SamplingError("duplicate audit touches fail closed")
    if touches: return verdict.verdict
    if verdict.verdict == "findings":
        corrections = [x for x in issues
                       if correction_marker(pr, chosen.head) in (x.get("body") or "")]
        if len(corrections) > 1: raise SamplingError("duplicate corrective Stories fail closed")
        if not corrections:
            story_match = re.search(r"(?m)^Story: #(\d+)$", pull.get("body") or "")
            if not story_match: raise SamplingError("canonical Story link unavailable")
            story = client.api(f"/issues/{story_match.group(1)}")
            project_match = re.search(r"(?m)^#(\d+)$", story.get("body") or "")
            scope_match = re.search(r"(?ms)^### Scope\s*$\n(.*?)(?=^### |\Z)", story.get("body") or "")
            if not project_match or not scope_match: raise SamplingError("corrective bounds unavailable")
            payload = corrective_story(int(project_match.group(1)), int(story_match.group(1)),
                                       chosen, verdict, scope_match.group(1).strip())
            payload["labels"] = ["type:story", "story:ready", "phase:build"]
            client.api("/issues", method="POST", value=payload)
        else:
            correction = corrections[0]
            names = {x.get("name") for x in correction.get("labels", [])}
            blocked = {x for x in names if x.startswith("story:blocked")}
            if blocked:
                names -= blocked
                names.add("story:ready")
                client.api(f"/issues/{correction['number']}", method="PATCH",
                           value={"labels": sorted(names), "state": "open"})
    client.api(f"/issues/{audit['number']}/comments", method="POST",
               value={"body": (f"Touch: sampling / audit / {verdict.seconds_spent}s / "
                               f"@{verdict.actor}\n\n{touch}")})
    runlog.event("sampling.touch", project=None, story=None,
                 pull_request=pr, head=chosen.head, bell_type="sampling",
                 classification="audit", seconds_spent=verdict.seconds_spent,
                 actor=verdict.actor)
    client.api(f"/issues/{audit['number']}", method="PATCH",
               value={"state": "closed", "state_reason": "completed"})
    return verdict.verdict
