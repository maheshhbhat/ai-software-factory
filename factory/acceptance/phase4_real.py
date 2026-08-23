#!/usr/bin/env python3
"""Prove Phase 4 with production defaults and nothing substituted.

The Phase 4 acceptance evidence came from `phase4_live.py`, which replaced the
engine commands, supplied its own environment, launched the worker detached,
and drove `poll.sh --once` in its own loop. Every one of those substitutions
later turned out to be hiding a production defect: the 60-second launch cap,
the process-lifetime skip guard, the review-link reclaim, the unauthenticated
engine environment, the missing reviewer credential. This harness exists so
that class of gap cannot recur unnoticed: it runs `./poll.sh` as the
long-lived service it is in production, with no `FACTORY_*_MODEL_CMD`, no
worker override, no environment beyond what the wrapper itself sets — and
watches a fresh fixture Story travel the whole road on its own.

The findings-retry leg is deterministic without mocking anything: the fixture
Story's spec instructs Attempt 1 to include a deliberate planted defect, so
the real engine genuinely writes it and the real reviewer, judging the diff
against acceptance notes it flatly violates, genuinely rejects it. A first-
pass approval in `require` mode fails the run as `findings_leg: not-exercised`
— a reviewer-quality finding in its own right, never a silent pass.

Deliberately operator-invoked:

    python3 factory/acceptance/phase4_real.py --project 325 [--max-minutes 45]
        [--findings-leg require|allow|skip] [--evidence-root runs/phase4-real]

One run creates one real Story and one real pull request, spends roughly
$1-3 of engine tokens, and takes up to the wall bound. It is never CI, never
scheduled, and must never become a required check — an E2E that gates merges
is an E2E nobody dares run. Per §9.3 the harness never closes its own fixture
Story: a leftover fixture is a finding, not litter.

Evidence lands in `runs/phase4-real/<run-id>/`, superseding — never
overwriting — the `runs/phase4/` ledger for the criteria it names.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory" / "dispatcher"))
sys.path.insert(0, str(ROOT / "factory" / "runtime"))

import dispatcher  # noqa: E402
import review_route  # noqa: E402

MARKER = "<!-- phase4-real-verification -->"
POLL_INTERVAL_SECONDS = 20

# Env vars that would substitute a component under test. Presence in the
# operator's shell aborts the run; they are also stripped from the child so a
# forgotten export cannot quietly turn the proof back into a rehearsal.
FORBIDDEN_PREFIXES = ("FACTORY_WORKER_",)
FORBIDDEN_EXACT = ("FACTORY_DELIVERY_MODEL_CMD", "FACTORY_REVIEW_MODEL_CMD",
                   "FACTORY_WORKER_CMD", "FACTORY_REVIEW_CMD")

CRITERIA = ("P4-05", "P4-06", "P4-07", "P4-08", "P4-11", "P4-12")


def forbidden_overrides(environ: dict) -> list[str]:
    """The substitution vars present in this environment, by name."""
    found = [name for name in FORBIDDEN_EXACT if environ.get(name)]
    found += [name for name in environ
              if any(name.startswith(p) for p in FORBIDDEN_PREFIXES)
              and environ.get(name)]
    return sorted(set(found))


def fixture_body(run_id: str, project: int, findings_leg: str,
                 spend_cap: str = "$3 / 15 min") -> str:
    """The fixture Story, per the phase4-live-fixture ADR: fresh per run, the
    planted defect stated in the spec so the real engine writes it for real."""
    planted = ""
    if findings_leg != "skip":
        planted = (
            "\n**Attempt-sensitive requirement.** Read this Story's "
            "`### Attempt` section before writing anything. If it is `1` "
            "(first dispatch), the module docstring must additionally contain "
            "the literal word `defective` — this Story exists to verify that "
            "review catches a planted defect, and Attempt 1 is the plant. If "
            "`### Attempt` is `2` or greater, the file must contain no "
            "occurrence of `defective`: read the review findings on the pull "
            "request and correct the same branch.\n")
    return f"""### Spec

Create `runs/phase4-real/{run_id}/product/app.py`: a copy of the canonical
`factory/fixtures/phase4_health/` health module (standard library only) whose
module docstring records the verification run id `{run_id}`. `GET /health`
returns JSON `{{"build_sha": "<value>"}}` where the value is `BUILD_SHA`
validated as exactly 40 lowercase hexadecimal characters; a missing or
malformed value fails startup rather than inventing health data.
{planted}
### Project

#{project}

### Phase

build

### Depends-on

none

### Hazard

- [ ] Touches hazard path

### Attempt

0

### Spend cap

{spend_cap}

### Scope

runs/phase4-real/{run_id}/product/**

### Acceptance notes

- `/health` returns the validated `BUILD_SHA`, never a literal placeholder.
- The merged deliverable contains no occurrence of the word `defective`.
- The module docstring records run id `{run_id}`.

{MARKER}"""


def verdict(ledger: dict, findings_leg: str) -> tuple[bool, dict]:
    """Judge one run from its durable observations. Pure — testable offline.

    `ledger` carries: `transitions` (story lifecycle labels in timeline
    order), `outcomes` ([{head, verdict}] in comment order), `pr_merged`,
    `merged_head`, `story_closed`, `aborted` (str | None).
    """
    detail: dict = {"findings_leg": "skipped"}
    if ledger.get("aborted"):
        return False, {**detail, "reason": ledger["aborted"]}

    walk = ledger.get("transitions", [])
    outcomes = ledger.get("outcomes", [])
    heads = []
    for outcome in outcomes:
        if outcome["head"] not in heads:
            heads.append(outcome["head"])

    if not ledger.get("pr_merged"):
        return False, {**detail, "reason": "the pull request did not merge"}
    if not ledger.get("story_closed") or "story:merged" not in walk:
        return False, {**detail, "reason": "the fixture story did not reach "
                                           "story:merged and close"}
    approved = [o for o in outcomes if o["verdict"] == "approval"
                and o["head"] == ledger.get("merged_head")]
    if not approved:
        return False, {**detail, "reason": "no exact-head approval marker on "
                                           "the merged head"}

    findings = [o for o in outcomes if o["verdict"] == "findings"]
    retried = ["story:in-review", "story:ready", "story:claimed"]
    walked = any(walk[i:i + 3] == retried for i in range(len(walk)))
    exercised = bool(findings) and len(heads) >= 2 and walked

    if findings_leg == "skip":
        return True, detail
    if exercised:
        return True, {**detail, "findings_leg": "exercised"}
    if findings_leg == "require":
        return False, {**detail, "findings_leg": "not-exercised",
                       "reason": "first review approved the planted defect or "
                                 "the worker declined to plant it — the retry "
                                 "path went unproven, which this mode treats "
                                 "as a failure worth a human's eyes"}
    return True, {**detail, "findings_leg": "not-exercised"}


def runtime_workspace(run_id: str) -> pathlib.Path:
    """Where a run writes while it is alive: outside the repository, always.

    The worker runs in the repository working tree and its scope check
    compares that tree before and after the engine. Run 20260823T130637Z
    failed exactly there: the harness's own poller.log, growing inside
    `runs/phase4-real/`, read as an out-of-scope change and the delivery was
    correctly refused. The evidence bundle is copied into the repository only
    after the poller is dead and nothing is being written.
    """
    return pathlib.Path(tempfile.mkdtemp(prefix=f"phase4-real-{run_id}-"))


class Run:
    def __init__(self, args):
        self.args = args
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.directory = runtime_workspace(self.run_id)
        self.final_directory = pathlib.Path(args.evidence_root) / self.run_id
        self.token = ""
        self.story = 0
        self.pull = 0
        self.aborted: str | None = None
        self.poller: subprocess.Popen | None = None

    # -- preflight ---------------------------------------------------------

    def preflight(self) -> bool:
        overrides = forbidden_overrides(dict(os.environ))
        if overrides:
            self.aborted = ("substitution overrides present; production "
                            "defaults are the thing under test: "
                            + ", ".join(overrides))
            return False

        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and \
                not (pathlib.Path.home() / ".factory-reviewer-token").is_file():
            self.aborted = ("no reviewer credential: CLAUDE_CODE_OAUTH_TOKEN "
                            "unset and ~/.factory-reviewer-token absent — the "
                            "run would stall at story:in-review through no "
                            "fault of the loop under test")
            return False

        try:
            self.token = subprocess.run(["gh", "auth", "token"],
                                        capture_output=True, text=True,
                                        timeout=30, check=True).stdout.strip()
            issues = dispatcher.fetch_issues(self.args.repo, self.token)
        except Exception as exc:  # noqa: BLE001 — unreachable repo, clean abort
            self.aborted = f"cannot read {self.args.repo}: {exc}"
            return False

        parent = issues.get(self.args.project)
        if parent is None or "type:project" not in dispatcher.labels_of(parent):
            self.aborted = f"#{self.args.project} is not an open project"
            return False
        state = dispatcher.lifecycle_of(parent, dispatcher.PROJECT_LIFECYCLE)
        if state != dispatcher.PROJECT_ACTIVE:
            self.aborted = f"project #{self.args.project} is {state}, not active"
            return False

        busy = {n: dispatcher.lifecycle_of(i, dispatcher.STORY_LIFECYCLE)
                for n, i in issues.items()
                if "type:story" in dispatcher.labels_of(i)}
        offenders = {n: s for n, s in busy.items()
                     if s in (dispatcher.READY, dispatcher.CLAIMED)}
        if offenders:
            self.aborted = ("the field is not clear — the run must dispatch "
                            "only its own fixture. Hold or deliver first: "
                            + ", ".join(f"#{n} ({s})"
                                        for n, s in sorted(offenders.items())))
            return False
        return True

    # -- execution ---------------------------------------------------------

    def api(self, path: str, **kwargs):
        return dispatcher._api(
            f"https://api.github.com/repos/{self.args.repo}{path}",
            self.token, **kwargs)

    def create_fixture(self) -> None:
        issue = self.api("/issues", method="POST", payload={
            "title": f"[Story] Phase 4 real-delivery verification {self.run_id}",
            "body": fixture_body(self.run_id, self.args.project,
                                 self.args.findings_leg),
            "labels": ["type:story", "story:ready", "phase:build"],
        })
        self.story = issue["number"]

    def spawn_poller(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if k not in FORBIDDEN_EXACT
               and not any(k.startswith(p) for p in FORBIDDEN_PREFIXES)}
        log = open(self.directory / "poller.log", "w")
        self.poller = subprocess.Popen(
            ["sh", str(ROOT / "poll.sh")], cwd=str(ROOT), env=env,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def observe(self) -> dict:
        """One durable snapshot: lifecycle walk, review outcomes, PR state."""
        timeline = dispatcher.fetch_timeline(self.args.repo, self.story,
                                             self.token)
        walk = [(x.get("label") or {}).get("name", "") for x in timeline
                if x.get("event") == "labeled"
                and (x.get("label") or {}).get("name", "").startswith("story:")]
        comments = self.api(f"/issues/{self.story}/comments?per_page=100") or []
        outcomes = []
        for comment in comments:
            for pr, sha, kind in review_route.MARKER.findall(
                    comment.get("body") or ""):
                outcomes.append({"pull": int(pr), "head": sha, "verdict": kind})
        pulls = [p for p in (self.api("/pulls?state=all&per_page=100") or [])
                 if f"Story: #{self.story}" in (p.get("body") or "")]
        pull = pulls[0] if pulls else {}
        self.pull = pull.get("number", 0)
        issue = self.api(f"/issues/{self.story}")
        return {
            "transitions": walk,
            "outcomes": [o for o in outcomes if o["pull"] == self.pull],
            "pr_merged": bool(pull.get("merged_at")),
            "pr_closed_unmerged": (pull.get("state") == "closed"
                                   and not pull.get("merged_at")),
            "merged_head": (pull.get("head") or {}).get("sha", ""),
            "story_closed": issue.get("state") == "closed",
            "story_labels": sorted(dispatcher.labels_of(issue)),
            "attempt": (issue.get("body") or ""),
            "pull_count": len(pulls),
        }

    def watch(self) -> dict:
        deadline = time.monotonic() + self.args.max_minutes * 60
        ledger: dict = {}
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            if self.poller and self.poller.poll() is not None:
                ledger["aborted"] = (f"poller exited "
                                     f"{self.poller.returncode} mid-run; "
                                     f"see poller.log")
                break
            try:
                ledger = self.observe()
            except Exception as exc:  # noqa: BLE001 — transient API failure
                print(f"[phase4-real] observe failed, retrying: {exc}",
                      flush=True)
                continue
            state = (ledger["transitions"] or ["story:ready"])[-1]
            print(f"[phase4-real] story #{self.story}: {state}; "
                  f"PR #{self.pull or '—'}; "
                  f"outcomes {[o['verdict'] for o in ledger['outcomes']]}",
                  flush=True)
            if ledger.get("pull_count", 0) > 1:
                ledger["aborted"] = ("more than one pull request carries the "
                                    "fixture's Story link")
                break
            if "story:blocked:poison" in ledger.get("story_labels", []):
                ledger["aborted"] = "the fixture story poisoned"
                break
            if ledger.get("pr_closed_unmerged"):
                ledger["aborted"] = "the fixture PR was closed without merging"
                break
            if ledger.get("pr_merged") and ledger.get("story_closed"):
                break
        else:
            ledger["aborted"] = (f"wall bound of {self.args.max_minutes} "
                                 f"minutes reached")
        return ledger

    def teardown(self) -> None:
        if not self.poller:
            return
        try:
            os.killpg(self.poller.pid, signal.SIGTERM)
            try:
                self.poller.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.poller.pid, signal.SIGKILL)
                self.poller.wait(timeout=5)
        except (ProcessLookupError, PermissionError):
            pass

    def write_evidence(self, ledger: dict, passed: bool, detail: dict) -> None:
        evidence = {
            "run_id": self.run_id,
            "finished": datetime.now(timezone.utc).isoformat(),
            "repo": self.args.repo,
            "fixture_story": self.story,
            "pull_request": self.pull,
            "nothing_substituted": {name: None for name in FORBIDDEN_EXACT},
            "transitions": ledger.get("transitions", []),
            "review_outcomes": ledger.get("outcomes", []),
            "aborted": ledger.get("aborted"),
            "passed": passed,
            **detail,
            "criteria": {key: "pass" if passed else "unproven"
                         for key in CRITERIA},
            "poller_log": "poller.log",
        }
        (self.directory / "evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="maheshhbhat/ai-software-factory")
    parser.add_argument("--commitment", type=int, default=54)
    parser.add_argument("--project", type=int, required=True)
    parser.add_argument("--max-minutes", type=int, default=45)
    parser.add_argument("--findings-leg", default="require",
                        choices=("require", "allow", "skip"))
    parser.add_argument("--evidence-root", default="runs/phase4-real")
    args = parser.parse_args(argv)

    run = Run(args)
    if not run.preflight():
        print(f"[phase4-real] ABORT before any write: {run.aborted}",
              file=sys.stderr)
        return 2

    run.create_fixture()
    print(f"[phase4-real] fixture story #{run.story}; run {run.run_id}",
          flush=True)
    try:
        run.spawn_poller()
        ledger = run.watch()
    finally:
        run.teardown()

    passed, detail = verdict(ledger, args.findings_leg)
    run.write_evidence(ledger, passed, detail)
    # Only now, with the poller dead and nothing writing, may the evidence
    # enter the repository tree.
    run.final_directory.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(run.directory, run.final_directory)
    print(f"[phase4-real] {'PASS' if passed else 'FAIL'}: "
          f"{json.dumps(detail)}", flush=True)
    print(f"[phase4-real] evidence: {run.final_directory}/evidence.json",
          flush=True)
    # §9.3 — the fixture story is never closed by this harness. If the run
    # failed mid-lifecycle the leftover fixture *is* the finding.
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
