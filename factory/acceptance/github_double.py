#!/usr/bin/env python3
"""One in-memory GitHub, behind the single I/O choke point the factory uses.

Every read and write the factory makes passes through `dispatcher._api`. That is
not an accident of this double — it is a property of the code, and it is what
lets an acceptance suite drive the *real* dispatcher, the *real* poller, the
*real* completion and reconciliation passes against a repository that fits in a
process, with no network and no credential.

## The one behaviour that makes this a test rather than a tautology

The open-issue listing **refuses to return closed issues**, exactly as GitHub's
does, and asserts that the caller asked for `state=open`. A double that served
closed issues from the listing would let a dependency scenario pass against the
dispatcher as it stood before #107 — the defect that shipped precisely because
every test handed the evaluator a pre-built map instead of making it go and
look. A closed issue here is reachable only by number.

The same discipline applies throughout: label writes append `labeled` and
`unlabeled` timeline events, because the factory reads *history* rather than
snapshots for every decision it cares about — the claim instant a lease turns
on is a timeline event, and a double that skipped them would be testing a
lifecycle that does not exist.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

ISSUE_RE = re.compile(r"/repos/[^/]+/[^/]+/issues/(\d+)$")
SUB_RE = re.compile(r"/repos/[^/]+/[^/]+/issues/(\d+)/(timeline|comments)")
PAGE_RE = re.compile(r"[?&]page=(\d+)")

STORY_BODY = """### Spec

{spec}

### Project

#{project}

### Phase

build

### Depends-on

{depends}

### Hazard

- [ ] Touches hazard path

### Attempt

{attempt}

### Spend cap

$5 / 30 min

### Scope

{scope}

### Acceptance notes

x
"""

PROJECT_BODY = """### Goal

g

### Falsifiable acceptance criteria

{criteria}

### Stories

#0

### Expected bells

2

### Risks / notes

x

### Roadmap commitment

#{commitment}
"""


def labels(*names) -> list[dict]:
    return [{"name": name} for name in names]


def story(number: int, *, lifecycle="story:ready", project=901, depends="none",
          attempt="0", scope="src/**", assoc="OWNER", state="open",
          extra_labels=(), spec="do the thing", body=None) -> dict:
    """A `type:story` issue as the issue form renders it (§3)."""
    return {
        "number": number,
        "state": state,
        "state_reason": None,
        "author_association": assoc,
        "title": f"[Story] {spec}",
        "html_url": f"https://github.com/o/r/issues/{number}",
        "labels": labels("type:story", "phase:build",
                         *([lifecycle] if lifecycle else ()), *extra_labels),
        "body": body if body is not None else STORY_BODY.format(
            spec=spec, project=project, depends=depends, attempt=attempt, scope=scope),
    }


def project(number: int = 901, *, lifecycle="project:active", commitment=54,
            assoc="OWNER", typed=True, state="open",
            criteria="- [ ] it works") -> dict:
    return {
        "number": number,
        "state": state,
        "state_reason": None,
        "author_association": assoc,
        "title": "[Project] p",
        "html_url": f"https://github.com/o/r/issues/{number}",
        "labels": labels(*(["type:project"] if typed else []),
                         *([lifecycle] if lifecycle else ())),
        "body": PROJECT_BODY.format(criteria=criteria, commitment=commitment),
    }


def pull(number: int, story_number: int, *, state="open", merged_at=None,
         body=None) -> dict:
    return {"number": number, "state": state, "merged_at": merged_at,
            "html_url": f"https://github.com/o/r/pull/{number}",
            "body": body if body is not None
            else f"Story: #{story_number}\nAgent-ID: claude-delivery\n"}


class FakeGitHub:
    """The repository, in memory. Call it wherever `dispatcher._api` is used."""

    def __init__(self, issues=(), pulls=()):
        self.now = datetime.now(timezone.utc)
        self.issues: dict[int, dict] = {}
        self.timelines: dict[int, list] = {}
        self.comments: dict[int, list] = {}
        self.requests: list[tuple[str, str]] = []
        self.writes: list[tuple[int, dict]] = []
        for issue in issues:
            self.add(issue)
        self.pulls: list[dict] = list(pulls)

    # -- construction and inspection ---------------------------------------

    def add(self, issue: dict) -> dict:
        number = issue["number"]
        self.issues[number] = dict(issue)
        self.timelines.setdefault(number, [])
        self.comments.setdefault(number, [])
        for label in self.labels(number):
            self.timelines[number].append(
                {"event": "labeled", "created_at": self.stamp(),
                 "label": {"name": label}})
        return self.issues[number]

    def labels(self, number: int) -> set[str]:
        return {label["name"] for label in self.issues[number]["labels"]}

    def lifecycle(self, number: int, prefix="story:") -> str | None:
        found = sorted(x for x in self.labels(number) if x.startswith(prefix))
        return found[0] if len(found) == 1 else None

    def state(self, number: int) -> str:
        return self.issues[number]["state"]

    def section(self, number: int, name: str) -> str:
        import merge_gate
        return (merge_gate.parse_section(self.issues[number]["body"], name) or "").strip()

    def applied(self, number: int) -> list[str]:
        """Lifecycle labels applied to this issue, in order. The audit trail."""
        return [event["label"]["name"] for event in self.timelines[number]
                if event["event"] == "labeled"
                and (event["label"]["name"].startswith("story:")
                     or event["label"]["name"].startswith("project:"))]

    def post_comment(self, number: int, body: str) -> dict:
        comment = {"id": len(self.comments[number]) + 1, "body": body,
                   "created_at": self.stamp(), "author_association": "OWNER",
                   "user": {"login": "owner"},
                   "html_url": f"https://github.com/o/r/issues/{number}#c{len(self.comments[number])}"}
        self.comments[number].append(comment)
        return comment

    # -- time ---------------------------------------------------------------

    def stamp(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)

    def age(self, **delta) -> None:
        """Push recorded history into the past.

        A lease is measured against `datetime.now`, which no fixture can move,
        so time passes here by the history getting older instead.
        """
        shift = timedelta(**delta)

        def older(stamp: str) -> str:
            return (datetime.fromisoformat(stamp.replace("Z", "+00:00")) - shift
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")

        for events in self.timelines.values():
            for event in events:
                event["created_at"] = older(event["created_at"])
        for comments in self.comments.values():
            for comment in comments:
                comment["created_at"] = older(comment["created_at"])

    # -- the API surface ----------------------------------------------------

    def __call__(self, url, token, method="GET", payload=None):
        self.requests.append((method, url))

        if method == "PATCH":
            return self._patch(url, payload)
        if method == "POST":
            number = int(re.search(r"/issues/(\d+)/comments", url).group(1))
            self.post_comment(number, payload["body"])
            return {}

        page = int(PAGE_RE.search(url).group(1)) if "page=" in url else 1
        if page > 1:
            return []

        if "/pulls" in url:
            return [dict(pr) for pr in self.pulls]

        sub = SUB_RE.search(url)
        if sub:
            number, kind = int(sub.group(1)), sub.group(2)
            return list(self.timelines.get(number, []) if kind == "timeline"
                        else self.comments.get(number, []))

        single = ISSUE_RE.search(url.split("?")[0])
        if single:
            number = int(single.group(1))
            if number not in self.issues:
                import urllib.error, io
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
            return dict(self.issues[number])

        # The listing. `state=open` is asserted rather than assumed: a double
        # that served closed issues here would pass a dependency scenario
        # against the dispatcher as it stood before #107.
        assert "state=open" in url, f"the queue is open issues (§9.3): {url}"
        return [dict(issue) for issue in self.issues.values()
                if issue["state"] == "open"]

    def _patch(self, url, payload):
        number = int(ISSUE_RE.search(url.split("?")[0]).group(1))
        issue = self.issues[number]
        self.writes.append((number, dict(payload)))
        if "labels" in payload:
            before, after = self.labels(number), set(payload["labels"])
            for name in sorted(before - after):
                self.timelines[number].append(
                    {"event": "unlabeled", "created_at": self.stamp(),
                     "label": {"name": name}})
            for name in sorted(after - before):
                self.timelines[number].append(
                    {"event": "labeled", "created_at": self.stamp(),
                     "label": {"name": name}})
            issue["labels"] = labels(*sorted(after))
        if "body" in payload:
            issue["body"] = payload["body"]
        if "state" in payload:
            issue["state"] = payload["state"]
        if "state_reason" in payload:
            issue["state_reason"] = payload["state_reason"]
        return dict(issue)
