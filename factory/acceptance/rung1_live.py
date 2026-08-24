#!/usr/bin/env python3
"""Operator-invoked, never-CI Phase 5 Rung 1 black-box UAT.

The delivery phase enters only through poll.sh and stops at the acceptance bell.
After the owner records a canonical decision, ``finalize`` invokes the same
entrypoint once, freezes the decision evidence, and generates the KPI report.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from factory.acceptance import rung1_report
from factory.acceptance import two_story_real as base
from factory.runtime import continuation

MARKER = "<!-- rung1-live-health:PROJECT -->"
FORBIDDEN = base.FORBIDDEN
PREFIXES = base.PREFIXES


def product_path(project):
    return f"runs/rung1/live_product/project-{project}/app.py"


def story_body(project):
    target = product_path(project)
    return f"""### Spec

Create a standard-library HTTP `/health` endpoint under
`{target}`. It must return JSON containing the deployed
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

runs/rung1/live_product/project-{project}/**

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


def terminal_worker_failure(run_dir, stories):
    """Return the factory's durable terminal launch failure, if observed."""
    path = Path(run_dir) / "process-events.jsonl"
    if not path.exists():
        return None
    numbers = set(stories)
    rows = rung1_report.read_jsonl(path)
    failed = [row for row in rows
              if row.get("event") == "worker.outcome"
              and row.get("story") in numbers
              and row.get("result") == "NO_WORKER_LAUNCHED"]
    if not failed:
        return None
    row = failed[-1]
    return (f"Story #{row.get('story')} reached worker.outcome="
            f"{row.get('result')}: {row.get('detail') or 'every eligible worker failed'}")


def foreign_dispatch(run_dir, project, stories):
    """Return any poller dispatch outside this UAT's Project and Stories."""
    path = Path(run_dir) / "process-events.jsonl"
    if not path.exists():
        return None
    intended = set(stories)
    rows = rung1_report.read_jsonl(path)
    foreign = [row for row in rows if row.get("event") == "dispatch.received"
               and (row.get("project") != project or
                    row.get("story") not in intended)]
    if not foreign:
        return None
    row = foreign[-1]
    return (f"foreign dispatch observed: Project #{row.get('project')}, "
            f"Story #{row.get('story')}; expected Project #{project}, "
            f"Stories {sorted(intended)}")


def progress_summary(data):
    stories = data.get("stories") or []
    states = []
    for story in stories:
        walk = story.get("walk") or []
        states.append(f"Story #{story.get('number')}: "
                      f"{walk[-1] if walk else 'created'}")
    return (f"Project: {data.get('project_state') or 'unknown'}; "
            + (", ".join(states) if states else "Story: not created"))


def fixture_selection(output, project, story):
    """Require the production dry-run to select this exact fixture."""
    if "Capacity exhausted" in output:
        return False, "worker capacity exhausted after fixture creation"
    selected = re.search(r"(?m)^Selected \(would claim, in order\):\s*(.+)$",
                         output or "")
    if not selected:
        return False, "normal dry-run did not select any Story"
    numbers = {int(value) for value in re.findall(r"#(\d+)", selected.group(1))}
    if numbers != {story}:
        return False, (f"normal dry-run selected Stories {sorted(numbers)}, "
                       f"expected only Story #{story} for Project #{project}")
    return True, f"normal dry-run selected only Story #{story}"


def write_report(run, evidence):
    """Write all eight KPIs for a terminal run, including a failed UAT."""
    process = run.run_dir / "process-events.jsonl"
    telemetry = run.run_dir / "telemetry.jsonl"
    touchlog = ROOT / "factory/touchlog/touchlog.jsonl"
    read = lambda path: rung1_report.read_jsonl(path) if path.exists() else []
    result = rung1_report.build(evidence, read(process), read(telemetry), read(touchlog))
    (run.tmp / "report.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n")
    (run.tmp / "report.md").write_text(rung1_report.render(result))
    run.persist(evidence)
    return result


def cleanup_failed_run(run, evidence):
    """Retire this harness's disposable artifacts after evidence is frozen.

    Closing is teardown, not rescue: labels and history remain unchanged, and
    the failed verdict cannot become green. Open worker PRs are closed first so
    later global review scans cannot deliver stale disposable work.
    """
    if evidence.get("passed"):
        return []
    retired = []
    pulls = run.api("/pulls?state=open&per_page=100") or []
    for number in run.story:
        issue = run.api(f"/issues/{number}")
        body = issue.get("body") or ""
        if MARKER.replace("PROJECT", str(run.args.project)) not in body:
            raise RuntimeError(
                f"refused to retire Story #{number}: Rung 1 marker missing")
        for pull in pulls:
            if f"Story: #{number}" in (pull.get("body") or ""):
                run.api(f"/pulls/{pull['number']}", "PATCH", {"state": "closed"})
                retired.append(f"Pull request #{pull['number']}")
        if (issue.get("state") or "open").lower() == "open":
            run.api(f"/issues/{number}", "PATCH",
                    {"state": "closed", "state_reason": "not_planned"})
            retired.append(f"Story #{number}")
    project = run.api(f"/issues/{run.args.project}")
    project_labels = base.labels(project)
    project_link = base.roadmap_commitment(project.get("body") or "")
    if ("type:project" not in project_labels
            or not (project.get("title") or "").startswith(
                "[Project] Phase 5 Rung 1")
            or project_link != run.commitment):
        raise RuntimeError(
            f"refused to retire Project #{run.args.project}: "
            "not a matching disposable Rung 1 Project")
    if (project.get("state") or "open").lower() == "open":
        run.api(f"/issues/{run.args.project}", "PATCH",
                {"state": "closed", "state_reason": "not_planned"})
        retired.append(f"Project #{run.args.project}")
    commitment = run.api(f"/issues/{run.commitment}")
    commitment_labels = base.labels(commitment)
    if ("type:roadmap-commitment" not in commitment_labels
            or not (commitment.get("title") or "").startswith(
                "[Commitment] Isolate Phase 5 Rung 1")):
        raise RuntimeError(
            f"refused to retire Commitment #{run.commitment}: "
            "not a disposable Rung 1 commitment")
    if (commitment.get("state") or "open").lower() == "open":
        run.api(f"/issues/{run.commitment}", "PATCH",
                {"state": "closed", "state_reason": "not_planned"})
        retired.append(f"Commitment #{run.commitment}")
    evidence["failed_artifact_retirement"] = {
        "status": "complete", "artifacts": retired,
        "meaning": "closed after frozen FAIL; labels and verdict unchanged",
    }
    (run.tmp / "evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    run.persist(evidence)
    return retired


def cleanup_accepted_run(run, evidence):
    """Retire the one-run commitment after normal acceptance completed."""
    if not evidence.get("passed") or (evidence.get("acceptance") or {}).get(
            "result") != "pass":
        return []
    commitment = run.api(f"/issues/{evidence['commitment']}")
    if ("type:roadmap-commitment" not in base.labels(commitment)
            or not (commitment.get("title") or "").startswith(
                "[Commitment] Isolate Phase 5 Rung 1")):
        raise RuntimeError("refused to retire accepted run commitment: "
                           "not a disposable Rung 1 commitment")
    retired = []
    if (commitment.get("state") or "open").lower() == "open":
        run.api(f"/issues/{evidence['commitment']}", "PATCH",
                {"state": "closed", "state_reason": "completed"})
        retired.append(f"Commitment #{evidence['commitment']}")
    evidence["accepted_artifact_retirement"] = {
        "status": "complete", "artifacts": retired,
        "meaning": "one-run commitment closed after normal acceptance",
    }
    return retired


def exercise(repo, token, merge_sha, project):
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
        module = Path(temp) / product_path(project)
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
    def __init__(self, args):
        super().__init__(args)
        self.started_at = datetime.strptime(
            self.run, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()

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
        print(f"[rung1] run {self.run} — Project #{self.args.project} — "
              "checking real dependencies", flush=True)
        project = self.preflight()
        doctor = subprocess.run(
            [sys.executable, str(ROOT / "factory/acceptance/e2e_doctor.py"),
             "--repo", self.args.repo, "--project", str(self.args.project),
             "--commitment", str(self.commitment),
             "--target", product_path(self.args.project)],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        preflight = doctor_result(doctor, self.args.external_process_guard_no_matches)
        print(f"[rung1] preflight READY — Roadmap Commitment "
              f"#{self.commitment} is isolated", flush=True)
        self.create(project)
        print(f"[rung1] created Story #{self.story[0]}; starting normal poll.sh path",
              flush=True)
        check_env = dict(os.environ)
        check_env["FACTORY_COMMITMENT"] = str(self.commitment)
        check_env["FACTORY_REPO"] = self.args.repo
        check = subprocess.run(
            ["sh", str(ROOT / "poll.sh"), "--once", "--dry-run"],
            cwd=ROOT, env=check_env, capture_output=True, text=True, timeout=180)
        selected, selection_detail = fixture_selection(
            (check.stdout or "") + "\n" + (check.stderr or ""),
            self.args.project, self.story[0])
        if check.returncode != 0 or not selected:
            raise RuntimeError("fixture dispatch preflight failed: "
                               + selection_detail)
        print(f"[rung1] dispatch preflight READY — {selection_detail}",
              flush=True)
        self.spawn()
        deadline = time.monotonic() + self.args.max_minutes * 60
        data = {}
        while time.monotonic() < deadline:
            time.sleep(20)
            data = self.snapshot()
            self.persist({"run": self.run, "project": self.args.project,
                          "status": "RUNNING", **data})
            print(f"[rung1] {progress_summary(data)}", flush=True)
            if data["project_state"] == "project:awaiting-acceptance": break
            foreign = foreign_dispatch(self.run_dir, self.args.project, self.story)
            if foreign:
                data["aborted"] = foreign
                break
            terminal = terminal_worker_failure(self.run_dir, self.story)
            if terminal:
                data["aborted"] = terminal
                break
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
        reason = ("delivery reached the acceptance bell" if ok else
                  data.get("aborted") or "delivery evidence incomplete")
        if ok:
            pull = self.api(f"/pulls/{story['pull']}")
            merge_sha = pull.get("merge_commit_sha")
            health = exercise(self.args.repo, self.token, merge_sha,
                              self.args.project)
            story.update({"merge_sha": merge_sha, "health": health})
        comments = self.api(f"/issues/{self.args.project}/comments?per_page=100") or []
        decisions = decision_rows(comments)
        evidence = {"run": self.run, "project": self.args.project,
                    "passed": ok, "reason": reason,
                    "report_phase": "pre-acceptance" if ok else "final",
                    "operator_actions": [{
                        "action": "fixture-launch",
                        "actor": "operator",
                        "classification": "operation",
                        "timestamp": self.started_at,
                        "source": f"runs/rung1/{self.run}/run-state.json",
                    }],
                    "commitment": self.commitment,
                    "entrypoint": f"sh poll.sh --interval {self.args.heartbeat}",
                    "production_substitutions": [],
                    "preflight": preflight, "decisions": decisions,
                    "observation_cutoff": (max(row["timestamp"] for row in decisions)
                                           if decisions else None),
                    "sources": [row.get("url") for row in decisions if row.get("url")],
                    **data}
        (self.tmp / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True)+"\n")
        self.persist(evidence)
        write_report(self, evidence)
        self.api(f"/issues/{self.args.project}/comments", "POST", {"body":
            f"## Rung 1 delivery evidence\n\nRun `{self.run}`: **{'PASS' if ok else 'FAIL'}** — {reason}.\n\n"
            f"Story #{story.get('number')} / PR #{story.get('pull')}; merged SHA "
            f"`{story.get('merge_sha')}`; `/health` response `{json.dumps(story.get('health'))}`.\n\n"
            f"Evidence: `runs/rung1/{self.run}/evidence.json`\n"
            f"Pre-acceptance KPI report: `runs/rung1/{self.run}/report.md`\n\n"
            "All eight KPIs are present. Acceptance-dependent values remain explicitly "
            "unavailable until the owner decides. No acceptance decision is recorded by "
            "this comment."})
        if not ok:
            cleanup_failed_run(self, evidence)
        print(f"[rung1] {'PASS' if ok else 'FAIL'} — {reason}", flush=True)
        print(f"[rung1] evidence: runs/rung1/{self.run}/evidence.json", flush=True)
        return 0 if ok else 1

    def fail(self, error):
        """Preserve Rung 1-specific evidence even on an interrupt or signal."""
        self.stop()
        stack = "".join(traceback.format_exception(type(error), error,
                                                    error.__traceback__))
        evidence = {"run": self.run, "project": self.args.project,
                    "passed": False,
                    "reason": f"{type(error).__name__}: {error}",
                    "exception": stack, "stories_created": self.story,
                    "entrypoint": f"sh poll.sh --interval {self.args.heartbeat}",
                    "production_substitutions": []}
        (self.tmp / "evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        (self.tmp / "exception.txt").write_text(stack)
        self.persist(evidence)
        if self.story and self.token:
            try:
                evidence.update(self.snapshot())
                comments = self.api(
                    f"/issues/{self.args.project}/comments?per_page=100") or []
                evidence["decisions"] = decision_rows(comments)
                evidence["sources"] = [row.get("url") for row in evidence["decisions"]
                                       if row.get("url")]
            except BaseException as snapshot_error:
                evidence["snapshot_error"] = (
                    f"{type(snapshot_error).__name__}: {snapshot_error}")
        try:
            write_report(self, evidence)
        except BaseException as report_error:
            evidence["report_error"] = f"{type(report_error).__name__}: {report_error}"
            (self.tmp / "evidence.json").write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n")
            self.persist(evidence)
        try:
            cleanup_failed_run(self, evidence)
        except BaseException as cleanup_error:
            evidence["failed_artifact_retirement"] = {
                "status": "failed",
                "error": f"{type(cleanup_error).__name__}: {cleanup_error}",
            }
            (self.tmp / "evidence.json").write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n")
            self.persist(evidence)
        if self.token:
            try:
                self.api(f"/issues/{self.args.project}/comments", "POST", {"body":
                    f"## Rung 1 UAT diagnostic\n\nRun `{self.run}` failed. No acceptance "
                    f"verdict was recorded.\n\nFailure: `{type(error).__name__}: {error}`\n\n"
                    f"Evidence: `runs/rung1/{self.run}/evidence.json`"})
            except Exception:
                pass


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
    evidence["report_phase"] = "final"
    evidence["sources"] = [row.get("url") for row in evidence["decisions"] if row.get("url")]
    cleanup_accepted_run(helper, evidence)
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
    stop_on_signal = base.termination_handler(run)
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name): signal.signal(getattr(signal, name), stop_on_signal)
    try: return run.execute()
    except KeyboardInterrupt as exc:
        run.fail(exc)
        print("[rung1] interrupted; child poller stopped", file=sys.stderr)
        return getattr(exc, "exit_code", 130)
    except Exception as exc:
        run.fail(exc); print(f"[rung1] FAIL: {type(exc).__name__}: {exc}", file=sys.stderr); return 2
    finally: run.stop()


if __name__ == "__main__": raise SystemExit(main())
