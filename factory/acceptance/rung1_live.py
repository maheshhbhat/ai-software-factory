#!/usr/bin/env python3
"""Operator-invoked, never-CI Phase 5 Rung 1 black-box UAT.

The delivery phase enters only through poll.sh and stops at the acceptance bell.
After the owner records a canonical decision, ``finalize`` invokes the same
entrypoint once, freezes the decision evidence, and generates the KPI report.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from factory.acceptance import rung1_report
from factory.acceptance import two_story_real as base
from factory.runtime import continuation

MARKER = "<!-- rung1-live-health:PROJECT -->"
FORBIDDEN = base.FORBIDDEN
PREFIXES = base.PREFIXES


def story_body(project):
    return f"""### Spec

Create a standard-library HTTP `/health` endpoint under
`runs/rung1/live_product/app.py`. It must return JSON containing the deployed
build SHA from `BUILD_SHA`, validated as exactly 40 lowercase hexadecimal
characters. Add deterministic tests. Change no file outside Scope.

The module must export `make_server(host, port, build_sha)` returning an HTTP
server whose `/health` response is exactly `{{"build_sha":"<sha>"}}` apart from
JSON whitespace. Every other path returns 404.

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

$5 / 60 min

### Scope

runs/rung1/live_product/**

### Acceptance notes

- `/health` returns the injected exact merged build SHA.
- Invalid build SHAs are rejected.
- No file outside Scope changes.

{MARKER.replace('PROJECT', str(project))}"""


def preflight_environment(env):
    bad = base.forbidden_overrides(env)
    if bad:
        raise RuntimeError("substitution overrides present: " + ", ".join(bad))
    return True


def doctor_result(completed, external_process_guard=False):
    """Accept an owner-run process guard only for sandbox inspection absence.

    Every other doctor failure remains fatal. The attestation is evidence of a
    real external check, not a claim that the sandbox inspected the host.
    """
    failures = [line for line in completed.stdout.splitlines() if line.startswith("FAIL  ")]
    process_unavailable = [
        "FAIL  no competing poller — process inspection unavailable"]
    if completed.returncode == 0:
        return completed.stdout.strip()
    if external_process_guard and failures == process_unavailable:
        return (completed.stdout.strip() +
                "\nPASS  external competing-poller guard — owner reported no pgrep matches")
    raise RuntimeError("real-dependency preflight failed: " +
                       (completed.stderr or completed.stdout)[-800:])


def decision_rows(comments):
    rows = []
    for comment in comments:
        body = comment.get("body") or ""
        bell = None
        if continuation.APPROVAL_HEADING.search(body): bell = "plan-approval"
        if continuation.ACCEPTANCE_HEADING.search(body): bell = "acceptance"
        if bell and continuation.is_owner(comment):
            rows.append({"bell_type": bell,
                         "timestamp": comment.get("created_at") or comment.get("createdAt"),
                         "url": comment.get("html_url") or comment.get("url")})
    return rows


def acceptance_record(comments):
    found = [row for row in comments if continuation.is_owner(row)
             and continuation.ACCEPTANCE_HEADING.search(row.get("body") or "")]
    if len(found) != 1:
        raise RuntimeError("exactly one canonical acceptance decision is required")
    verdict, error = continuation.classify_acceptance(found[0])
    if error: raise RuntimeError(f"malformed acceptance decision: {error}")
    _fingerprint, payload = continuation.acceptance_identity(found[0])
    parsed = json.loads(payload)
    criteria = [{"criterion": key, "result": value}
                for key, value in parsed.get("criteria", [])]
    if not criteria:
        raise RuntimeError("acceptance decision has no canonical per-criterion results")
    return {"result": verdict, "criteria": criteria,
            "source": found[0].get("html_url") or found[0].get("url")}


def exercise(repo, token, merge_sha):
    env = dict(os.environ)
    # Reuse the authenticated HTTPS boundary proven by the existing harness.
    import base64
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"})
    with tempfile.TemporaryDirectory(prefix="rung1-deployed-") as temp:
        subprocess.run(["git", "clone", "--quiet", "--branch", "main",
                        f"https://github.com/{repo}.git", temp], check=True,
                       timeout=300, env=env)
        module = Path(temp) / "runs/rung1/live_product/app.py"
        spec = importlib.util.spec_from_file_location("rung1_health", module)
        app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
        server = app.make_server("127.0.0.1", 0, merge_sha)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/health", timeout=10) as response:
                value = json.loads(response.read())
        finally:
            server.shutdown(); thread.join(); server.server_close()
    if value != {"build_sha": merge_sha}:
        raise RuntimeError(f"/health returned {value!r}, expected merged SHA {merge_sha}")
    return value


class Run(base.Run):
    def create(self, project):
        story = self.api("/issues", "POST", {
            "title": "[Story] Rung 1: add a /health endpoint returning build SHA",
            "body": story_body(self.args.project),
            "labels": ["type:story", "story:ready", "phase:build"]})
        self.story = [story["number"]]
        self.persist({"run": self.run, "project": self.args.project,
                      "status": "FIXTURES_CREATED", "stories": self.story})
        body = base.STORY_SECTION.sub(
            lambda match: match.group(1) + "\n" + base.story_list(self.story) + "\n",
            project["body"], count=1)
        self.api(f"/issues/{self.args.project}", "PATCH", {"body": body})

    def execute(self):
        preflight_environment(os.environ)
        doctor = subprocess.run(
            [sys.executable, str(ROOT / "factory/acceptance/e2e_doctor.py"),
             "--repo", self.args.repo, "--project", str(self.args.project)],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        preflight = doctor_result(doctor, self.args.external_process_guard_no_matches)
        project = self.preflight()
        self.create(project); self.spawn()
        deadline = time.monotonic() + self.args.max_minutes * 60
        data = {}
        while time.monotonic() < deadline:
            time.sleep(20)
            data = self.snapshot()
            self.persist({"run": self.run, "project": self.args.project,
                          "status": "RUNNING", **data})
            if data["project_state"] == "project:awaiting-acceptance": break
            if self.poller.poll() is not None:
                data["aborted"] = "poller exited"; break
        else:
            data["aborted"] = "wall timeout"
        before = base.durable_replay_state(data)
        time.sleep(self.args.heartbeat * 2)
        after = self.snapshot(); data["replay_changed"] = before != base.durable_replay_state(after)
        self.stop()
        story = data.get("stories", [{}])[0]
        ok = (not data.get("aborted") and len(data.get("stories", [])) == 1
              and story.get("merged") and story.get("closed") and story.get("exact_approval")
              and set(story.get("checks", [])) == {"merge-gate", "merge-gate-surface"}
              and data.get("project_state") == "project:awaiting-acceptance"
              and not data.get("replay_changed") and data.get("observability", {}).get("valid"))
        reason = "delivery reached the acceptance bell" if ok else "delivery evidence incomplete"
        if ok:
            pull = self.api(f"/pulls/{story['pull']}")
            merge_sha = pull.get("merge_commit_sha")
            health = exercise(self.args.repo, self.token, merge_sha)
            story.update({"merge_sha": merge_sha, "health": health})
        evidence = {"run": self.run, "project": self.args.project,
                    "passed": ok, "reason": reason,
                    "commitment": self.commitment,
                    "entrypoint": f"sh poll.sh --interval {self.args.heartbeat}",
                    "production_substitutions": [],
                    "preflight": preflight, **data}
        (self.tmp / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True)+"\n")
        self.persist(evidence)
        self.api(f"/issues/{self.args.project}/comments", "POST", {"body":
            f"## Rung 1 delivery evidence\n\nRun `{self.run}`: **{'PASS' if ok else 'FAIL'}** — {reason}.\n\n"
            f"Story #{story.get('number')} / PR #{story.get('pull')}; merged SHA "
            f"`{story.get('merge_sha')}`; `/health` response `{json.dumps(story.get('health'))}`.\n\n"
            f"Evidence: `runs/rung1/{self.run}/evidence.json`\n\nNo acceptance decision is recorded by this comment."})
        return 0 if ok else 1


def finalize(args):
    preflight_environment(os.environ)
    run_dir = Path(args.evidence_root) / args.run
    evidence_path = run_dir / "evidence.json"
    evidence = json.loads(evidence_path.read_text())
    if evidence.get("project") != args.project or not evidence.get("passed"):
        raise RuntimeError("delivery evidence does not identify a passing run for this Project")
    env = dict(os.environ)
    env["FACTORY_RUN_DIR"] = str(run_dir / "observability")
    env["FACTORY_COMMITMENT"] = str(evidence["commitment"])
    subprocess.run(["sh", str(ROOT / "poll.sh"), "--once"], cwd=ROOT, env=env,
                   check=True, timeout=600)
    # Reuse only the production harness's HTTP method without constructing a
    # second run directory during finalization.
    helper = object.__new__(base.Run)
    helper.args = args
    helper.token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or
        subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                       check=True).stdout.strip())
    project = helper.api(f"/issues/{args.project}")
    if base.lifecycle(project, "project:") != "project:accepted":
        raise RuntimeError("normal poll did not consume a passing acceptance decision")
    comments = helper.api(f"/issues/{args.project}/comments?per_page=100") or []
    evidence["decisions"] = decision_rows(comments)
    evidence["acceptance"] = acceptance_record(comments)
    evidence["observation_cutoff"] = max(row["timestamp"] for row in evidence["decisions"])
    evidence["quality_observations"] = []
    evidence["human_code_interventions"] = "unavailable"
    evidence["sources"] = [row.get("url") for row in evidence["decisions"] if row.get("url")]
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    result = rung1_report.build(
        evidence,
        rung1_report.read_jsonl(run_dir / "observability/process-events.jsonl"),
        rung1_report.read_jsonl(run_dir / "observability/telemetry.jsonl"),
        rung1_report.read_jsonl(ROOT / "factory/touchlog/touchlog.jsonl"))
    (run_dir / "report.json").write_text(json.dumps(result, indent=2, sort_keys=True)+"\n")
    (run_dir / "report.md").write_text(rung1_report.render(result))
    return 0 if result["measurement_integrity"]["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--repo", default="maheshhbhat/ai-software-factory")
    start.add_argument("--project", type=int, required=True)
    start.add_argument("--max-minutes", type=int, default=45)
    start.add_argument("--heartbeat", type=int, default=15)
    start.add_argument("--evidence-root", default="runs/rung1")
    start.add_argument("--external-process-guard-no-matches", action="store_true",
                       help="owner ran the documented pgrep guard and observed no matches")
    finish = sub.add_parser("finalize")
    finish.add_argument("--repo", default="maheshhbhat/ai-software-factory")
    finish.add_argument("--project", type=int, required=True)
    finish.add_argument("--run", required=True)
    finish.add_argument("--evidence-root", default="runs/rung1")
    args = parser.parse_args(argv)
    if args.command == "finalize": return finalize(args)
    run = Run(args)
    try: return run.execute()
    except Exception as exc:
        run.fail(exc); print(f"[rung1] FAIL: {type(exc).__name__}: {exc}", file=sys.stderr); return 2
    finally: run.stop()


if __name__ == "__main__": raise SystemExit(main())
