#!/usr/bin/env python3
"""Readiness doctor for the real two-Story factory UAT.

This command may read GitHub, inspect local processes, and run ``poll.sh`` with
``--dry-run``.  It also creates and removes one temporary detached Git worktree
to prove that the production worker can initialize its checkout.  It never
creates, edits, labels, comments on, or closes a GitHub artifact, never pushes,
and never invokes a model for delivery or review.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "factory" / "runtime"
sys.path.insert(0, str(RUNTIME))
import observability as obs  # noqa: E402
import status as live_status  # noqa: E402

DEFAULT_REPO = "maheshhbhat/ai-software-factory"
TEST_COMMITMENT = 384
FORBIDDEN_EXACT = {
    "FACTORY_DELIVERY_MODEL_CMD", "FACTORY_REVIEW_MODEL_CMD",
    "FACTORY_WORKER_CMD", "FACTORY_REVIEW_CMD",
}
FORBIDDEN_PREFIXES = ("FACTORY_WORKER_",)
MUTATING_WORDS = ("mutation", "createIssue", "updateIssue", "addComment",
                  "--claim", "issue edit", "issue comment", "pr merge", "git push")


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


class Doctor:
    def __init__(self, repo: str, project: int, *, environ=None, runner=None):
        self.repo, self.project = repo, project
        self.env = dict(os.environ if environ is None else environ)
        self.runner = runner or subprocess.run
        self.checks: list[Check] = []
        self.token = ""

    def record(self, name: str, passed: bool, detail: str) -> bool:
        self.checks.append(Check(name, passed, detail))
        return passed

    def command(self, args: list[str], *, timeout=30, env=None):
        rendered = " ".join(args)
        if any(word in rendered for word in MUTATING_WORDS):
            raise RuntimeError(f"doctor refused mutating command: {rendered}")
        return self.runner(args, cwd=ROOT, env=env or self.env,
                           capture_output=True, text=True, timeout=timeout)

    def local(self):
        missing = [name for name in ("python3", "git", "gh", "codex", "claude")
                   if shutil.which(name, path=self.env.get("PATH")) is None]
        self.record("required binaries", not missing,
                    "available" if not missing else "missing: " + ", ".join(missing))
        status = self.command(["git", "status", "--porcelain"])
        self.record("local candidate", status.returncode == 0 and bool(status.stdout.strip()),
                    "local uncommitted candidate present" if status.stdout.strip()
                    else "no local candidate changes found")
        processes = self.command(["pgrep", "-f", "[f]actory/runtime/poller.py"])
        self.record("no competing poller", processes.returncode == 1,
                    "none running" if processes.returncode == 1
                    else ("poller process already running" if processes.returncode == 0
                          else "process inspection unavailable"))
        free = shutil.disk_usage(ROOT).free
        self.record("free disk", free >= 1024 ** 3,
                    f"{free // (1024 ** 2)} MiB available")

    def worktree(self):
        """Exercise the production worker's first mutating Git operation."""
        with tempfile.TemporaryDirectory(prefix="factory-doctor-worktree-") as directory:
            add = self.command(
                ["git", "worktree", "add", "--detach", directory, "HEAD"],
                timeout=60)
            if add.returncode != 0:
                detail = (add.stderr or add.stdout).strip()[-400:] or f"exit {add.returncode}"
                self.record("worker worktree creation", False, detail)
                return
            remove = self.command(
                ["git", "worktree", "remove", "--force", directory], timeout=60)
            if remove.returncode != 0:
                detail = (remove.stderr or remove.stdout).strip()[-400:] or f"exit {remove.returncode}"
                self.record("worker worktree creation", False,
                            "created, but cleanup failed: " + detail)
                return
            self.record("worker worktree creation", True,
                        "temporary detached worktree created and removed")

    def substitutions(self):
        present = sorted(name for name, value in self.env.items() if value and
                         (name in FORBIDDEN_EXACT or
                          any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)))
        self.record("no substitution overrides", not present,
                    "none" if not present else ", ".join(present))

    def credentials(self):
        token = self.command(["gh", "auth", "token"])
        self.token = token.stdout.strip() if token.returncode == 0 else ""
        self.record("GitHub credential", bool(self.token),
                    "available" if self.token else "gh auth token failed")
        codex = self.command(["codex", "login", "status"])
        self.record("Codex authentication", codex.returncode == 0,
                    (codex.stdout or codex.stderr).strip()[:200] or
                    f"exit {codex.returncode}")
        reviewer_file = pathlib.Path(self.env.get("HOME", "")) / ".factory-reviewer-token"
        reviewer = self.env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        if not reviewer and reviewer_file.is_file():
            reviewer = reviewer_file.read_text().strip()
        self.record("reviewer credential", bool(reviewer),
                    "dedicated credential available" if reviewer else
                    "CLAUDE_CODE_OAUTH_TOKEN and ~/.factory-reviewer-token are absent")
        claude = self.command(["claude", "--version"])
        self.record("reviewer binary", claude.returncode == 0,
                    (claude.stdout or claude.stderr).strip()[:200] or
                    f"exit {claude.returncode}")
        if reviewer:
            with tempfile.TemporaryDirectory(prefix="factory-review-auth-") as home:
                review_env = {key: self.env[key] for key in
                              ("PATH", "LANG", "LC_ALL", "SHELL", "TMPDIR")
                              if self.env.get(key)}
                review_env.update({"HOME": home, "USER": "factory-reviewer",
                                   "LOGNAME": "factory-reviewer",
                                   "CLAUDE_CODE_OAUTH_TOKEN": reviewer})
                auth = self.command(["claude", "auth", "status"], env=review_env)
            self.record("reviewer authentication", auth.returncode == 0,
                        "dedicated reviewer identity authenticated" if auth.returncode == 0
                        else (auth.stderr or auth.stdout).strip()[:200])
        else:
            self.record("reviewer authentication", False, "credential unavailable")

    def github(self):
        if not self.token:
            self.record("GitHub substrate", False, "credential unavailable")
            return
        owner, name = self.repo.split("/", 1)
        query = """query($owner:String!,$name:String!,$project:Int!,$commitment:Int!){
          repository(owner:$owner,name:$name){isPrivate viewerPermission
            project:issue(number:$project){number state body labels(first:20){nodes{name}}}
            commitment:issue(number:$commitment){number state body labels(first:20){nodes{name}}}
            defaultBranchRef{branchProtectionRule{requiresStatusChecks requiredStatusCheckContexts}}
          }
          rateLimit{remaining resetAt}
        }"""
        result = self.command(
            ["gh", "api", "graphql", "-f", f"query={query}",
             "-F", f"owner={owner}", "-F", f"name={name}",
             "-F", f"project={self.project}", "-F", f"commitment={TEST_COMMITMENT}"],
            env={**self.env, "GH_TOKEN": self.token})
        try:
            value = json.loads(result.stdout)["data"]
            repo = value["repository"]
        except (KeyError, TypeError, json.JSONDecodeError):
            self.record("GitHub substrate", False,
                        (result.stderr or result.stdout).strip()[:300] or "unreadable response")
            return
        self.record("repository access",
                    repo.get("viewerPermission") in {"WRITE", "MAINTAIN", "ADMIN"},
                    f"visibility={'private' if repo.get('isPrivate') else 'public'}; "
                    f"permission={repo.get('viewerPermission')}")
        remaining = int((value.get("rateLimit") or {}).get("remaining", 0))
        self.record("GitHub API capacity", remaining >= 200,
                    f"{remaining} GraphQL points remaining")
        # `/rate_limit` and GraphQL can report healthy capacity while the REST
        # issue endpoint used by production is already returning 403. Probe the
        # real read path before creating any disposable artifact.
        rest = self.command(
            ["gh", "api", f"repos/{self.repo}/issues?state=open&per_page=1"],
            env={**self.env, "GH_TOKEN": self.token})
        self.record("production REST read path", rest.returncode == 0,
                    ("issue listing succeeds" if rest.returncode == 0 else
                     (rest.stderr or rest.stdout).strip()[:300]))
        project = repo.get("project") or {}
        labels = {item["name"] for item in (project.get("labels") or {}).get("nodes", [])}
        project_ok = (project.get("state") == "OPEN" and "type:project" in labels and
                      len({x for x in labels if x.startswith("project:")}) == 1 and
                      labels & {"project:awaiting-ready", "project:active"} and
                      re.search(r"(?m)^### Roadmap commitment\s*$\n\s*#384\s*$", project.get("body") or ""))
        self.record("Project authorization", bool(project_ok),
                    f"Project #{self.project}; labels={sorted(labels)}")
        commitment = repo.get("commitment") or {}
        commitment_labels = {item["name"] for item in
                             (commitment.get("labels") or {}).get("nodes", [])}
        self.record("test-only commitment",
                    commitment.get("state") == "OPEN" and
                    "type:roadmap-commitment" in commitment_labels and
                    "No product or factory implementation work" in (commitment.get("body") or ""),
                    f"Commitment #{TEST_COMMITMENT} remains open and test-only")
        protection = ((repo.get("defaultBranchRef") or {}).get("branchProtectionRule") or {})
        contexts = set(protection.get("requiredStatusCheckContexts") or [])
        rulesets = self.command(["gh", "api", f"repos/{self.repo}/rulesets"],
                                env={**self.env, "GH_TOKEN": self.token})
        try:
            summaries = json.loads(rulesets.stdout)
        except json.JSONDecodeError:
            summaries = []
        for summary in summaries if isinstance(summaries, list) else []:
            if summary.get("enforcement") != "active":
                continue
            detail = self.command(
                ["gh", "api", f"repos/{self.repo}/rulesets/{summary['id']}"],
                env={**self.env, "GH_TOKEN": self.token})
            try:
                rules = json.loads(detail.stdout).get("rules", [])
            except json.JSONDecodeError:
                rules = []
            for rule in rules:
                if rule.get("type") == "required_status_checks":
                    contexts.update(item.get("context") for item in
                                    (rule.get("parameters") or {}).get("required_status_checks", []))
        self.record("required merge gate",
                    "merge-gate" in contexts,
                    "required checks: " + (", ".join(sorted(contexts)) or "none"))

    def configuration(self):
        poll = (ROOT / "poll.sh").read_text()
        self.record("Codex-only worker default",
                    'FACTORY_WORKER_ORDER="codex-delivery"' in poll,
                    "poll.sh defaults to codex-delivery")
        harness = (ROOT / "factory" / "acceptance" / "two_story_real.py").read_text()
        self.record("Story spend default", "$5 / 60 min" in harness,
                    "$5 and 60 minutes per disposable Story")
        self.record("black-box harness boundary",
                    'str(ROOT/"poll.sh")' in harness and
                    "import dispatcher" not in harness and "import review_route" not in harness,
                    "harness enters through poll.sh only")

    def observability(self):
        previous = os.environ.copy()
        try:
            with tempfile.TemporaryDirectory(prefix="factory-doctor-") as directory:
                os.environ["FACTORY_RUN_DIR"] = directory
                os.environ["FACTORY_LOG_LEVEL"] = "CRITICAL"
                os.environ["FACTORY_HEARTBEATS"] = "1"
                old_interval = obs.HEARTBEAT_SECONDS
                obs.HEARTBEAT_SECONDS = 0.01
                try:
                    with obs.Activity("doctor", "smoke", "running", project=self.project):
                        time.sleep(0.025)
                    obs.process_event("doctor.smoke.completed", project=self.project)
                finally:
                    obs.HEARTBEAT_SECONDS = old_interval
                streams = {kind: obs.read_records(kind) for kind in
                           ("process", "operation", "telemetry")}
                rows = live_status.current_components(
                    obs.activity_status(streams["telemetry"]))
                good = bool(all(streams.values()) and rows and
                            any(row.get("metric") == "activity.heartbeat"
                                for row in streams["telemetry"]))
                self.record("observability smoke", good,
                            "separate files, heartbeat, and status are readable" if good
                            else "heartbeat or status evidence missing")
        finally:
            os.environ.clear(); os.environ.update(previous)

    def dry_run(self):
        if not self.token:
            self.record("poll.sh dry run", False, "GitHub credential unavailable")
            return
        env = {key: value for key, value in self.env.items()
               if key not in FORBIDDEN_EXACT and
               not any(key.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)}
        env.update({"GH_TOKEN": self.token, "FACTORY_COMMITMENT": str(TEST_COMMITMENT),
                    "FACTORY_REPO": self.repo})
        result = self.command(["sh", "poll.sh", "--once", "--dry-run"],
                              timeout=120, env=env)
        self.record("poll.sh dry run", result.returncode == 0,
                    "normal entrypoint completed without writes" if result.returncode == 0
                    else (result.stderr or result.stdout).strip()[-400:])

    def run(self) -> list[Check]:
        self.local(); self.worktree(); self.substitutions(); self.credentials(); self.github()
        self.configuration(); self.observability(); self.dry_run()
        return self.checks


def render(checks: list[Check]) -> str:
    lines = [f"{'PASS' if item.passed else 'FAIL'}  {item.name} — {item.detail}"
             for item in checks]
    failures = sum(not item.passed for item in checks)
    lines.append("READY — no E2E artifacts were created" if failures == 0 else
                 f"BLOCKED — fix {failures} failure(s) before E2E")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--project", required=True, type=int)
    args = parser.parse_args(argv)
    if args.project <= 0:
        parser.error("--project must be positive")
    checks = Doctor(args.repo, args.project).run()
    print(render(checks))
    return 0 if all(item.passed for item in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
