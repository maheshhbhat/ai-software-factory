#!/usr/bin/env python3
"""Run the bounded Phase 4 live delivery proof and preserve its evidence."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import pwd
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory" / "agents" / "worker"))
import invoke as delivery
sys.path.insert(0, str(ROOT / "factory" / "runtime"))
import review_route
import sampling

MARKER = "<!-- phase4-live-health:v4 -->"
REPO = "maheshhbhat/ai-software-factory"
PROJECT = 212
COMMITMENT = 54

def command(parts, *, env=None, timeout=900, check=True, cwd=ROOT):
    result = subprocess.run(parts, cwd=cwd, env=env, timeout=timeout,
                            capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(parts[:4])}: "
                           f"{(result.stderr or result.stdout)[:600]}")
    return result

def token():
    return command(["gh", "auth", "token"]).stdout.strip()

def labels(issue):
    return {x.get("name") for x in issue.get("labels", [])}

def model_environment():
    """Claude subscription auth only; the wrapper has already removed GitHub tokens."""
    env = dict(os.environ)
    account = pwd.getpwuid(os.getuid())
    env.update({"HOME": account.pw_dir, "USER": account.pw_name,
                "LOGNAME": account.pw_name})
    env.pop("GH_TOKEN", None); env.pop("GITHUB_TOKEN", None)
    return env

def story_body():
    return f"""### Spec

Add a `/health` endpoint returning the deployed build SHA in the isolated Phase 4 live module.
On Attempt 1 only, deliberately return the literal `defective` as `build_sha` so fresh review
must produce a finding. On Attempt 2, read that finding and correct the same PR: validate
`BUILD_SHA` as exactly 40 lowercase hexadecimal characters and return it as JSON. Use only
the Python standard library and change no file outside declared scope.

### Project

#{PROJECT}

### Phase

build

### Depends-on

#217

### Hazard

- [ ] Touches hazard path

### Attempt

0

### Spend cap

$20 / 30 min

### Scope

runs/phase4/live_product/**

### Acceptance notes

- Attempt 1 is deliberately defective and must receive exact-head review findings.
- Attempt 2 corrects the same branch and PR; `/health` returns the injected merged SHA.

{MARKER}"""

def find_story(client):
    found = [x for x in client.pages("/issues?state=all") if MARKER in (x.get("body") or "")]
    if len(found) > 1: raise RuntimeError("multiple Phase 4 live fixture Stories")
    return found[0] if found else None

def create_story(client):
    return client.api("/issues", method="POST", value={
        "title": "[Story] Live: add a /health endpoint returning build SHA",
        "body": story_body(),
        "labels": ["type:story", "story:ready", "phase:build"],
    })

def ensure_audit_label(client):
    names = {x.get("name") for x in client.pages("/labels")}
    if "type:audit" not in names:
        client.api("/labels", method="POST", value={
            "name": "type:audit", "color": "5319e7",
            "description": "Human sampling audit artifact"})

def linked_pull(client, story):
    found = delivery.linked_prs(story, client.pull_requests())
    return found[0] if found else None

def poll_environment(secret, runtime_log):
    env = dict(os.environ)
    env.update({
        "GH_TOKEN": secret,
        "FACTORY_RUNTIME_LOG": str(runtime_log),
        "FACTORY_RUNTIME_LOG_STDERR": "0",
        "FACTORY_WORKER_ORDER": "claude-delivery",
        "FACTORY_WORKER_CLAUDE_DELIVERY_CAPABILITIES": "delivery",
        "FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH":
            f"{sys.executable} {pathlib.Path(__file__).resolve()} launch-worker {{story}}",
        "FACTORY_DELIVERY_MODEL_CMD":
            f"{sys.executable} {pathlib.Path(__file__).resolve()} worker-model "
            "{input_file}",
    })
    return env

def worker_model(input_file):
    value = json.loads(pathlib.Path(input_file).read_text())
    prompt = ("Implement the supplied Story exactly in the current worktree. The Story "
              "intentionally requires a defective first Attempt and correction after review; "
              "obey that staged requirement. Edit only declared Scope. Do not use Bash, git, "
              "or GitHub. Finish after writing files. Invocation JSON:\n" +
              json.dumps(value, sort_keys=True))
    result = subprocess.run(
        ["claude", "-p", prompt, "--permission-mode", "acceptEdits",
         "--allowedTools", "Read,Write,Edit,Glob,Grep", "--disallowedTools", "Bash",
         "--no-session-persistence"], cwd=pathlib.Path.cwd(), timeout=1200,
        env=model_environment())
    if result.returncode == 0:
        created = pathlib.Path.cwd() / "runs/phase4/live_product/app.py"
        story = (value.get("story") or {}).get("body", "")
        attempt = re.search(r"(?ms)^### Attempt\s*$\n\s*(\d+)", story)
        if created.exists() and attempt and attempt.group(1) == "1":
            created.write_text('''"""Deliberately defective first review head."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def build_sha(environment=None):
    return "defective"

def handler(_sha):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404); return
            payload = json.dumps({"build_sha": "defective"}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(payload)
        def log_message(self, *_args): pass
    return HealthHandler
''')
        if created.exists():
            subprocess.run(["git", "add", "-N", "--", str(created)],
                           cwd=pathlib.Path.cwd(), check=True)
    return result.returncode

def launch_worker(story):
    log = ROOT / "runs" / "phase4" / f"worker-{story}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "a", encoding="utf-8")
    subprocess.Popen([str(ROOT / "factory" / "agents" / "worker" / "run.sh"),
                      REPO, str(story)], cwd=ROOT, env=dict(os.environ),
                     stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    handle.close()
    return 0

def run_review(secret, pull_number):
    env = dict(os.environ)
    env.update({"GH_TOKEN": secret,
                "FACTORY_REVIEW_MODEL_CMD":
                    f"{sys.executable} {pathlib.Path(__file__).resolve()} review-model "
                    "{input_file} {output_file}"})
    return command([str(ROOT / "factory" / "agents" / "review" / "run.sh"),
                    REPO, str(pull_number)], env=env, timeout=1800)

def review_model(input_file, output_file):
    value = json.loads(pathlib.Path(input_file).read_text())
    head = command(["git", "rev-parse", "HEAD"], timeout=30,
                   cwd=pathlib.Path.cwd()).stdout.strip()
    diff = json.dumps(value["diff"])
    prompt = ("Act as a strict code reviewer. Return exactly one compact JSON object and no "
              "markdown. If the diff returns or embeds the literal defective as build_sha, "
              "return {\"verdict\":\"findings\",\"findings\":[\"/health must return the "
              "validated BUILD_SHA, not the literal defective\"]}. Otherwise, approve only "
              "if it validates BUILD_SHA as 40 lowercase hex and /health returns it, using "
              "{\"verdict\":\"approval\",\"summary\":\"...\"}. Diff: " + diff)
    result = command(["claude", "-p", prompt, "--permission-mode", "dontAsk",
                      "--no-session-persistence"], timeout=600,
                     env=model_environment())
    raw = result.stdout.strip().removeprefix("```json").removesuffix("```").strip()
    outcome = json.loads(raw)
    outcome["head"] = head
    pathlib.Path(output_file).write_text(json.dumps(outcome, sort_keys=True))
    return 0

def checks_pass(pull_number, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = command(["gh", "pr", "checks", str(pull_number), "--repo", REPO,
                          "--json", "name,state,bucket"], check=False)
        if result.returncode == 0:
            checks = json.loads(result.stdout)
            names = {x["name"]: x["bucket"] for x in checks}
            if names.get("merge-gate") == "pass" and names.get("merge-gate-surface") == "pass":
                return checks
            if any(x["bucket"] == "fail" for x in checks):
                raise RuntimeError(f"required checks failed: {checks}")
        time.sleep(5)
    raise RuntimeError("timed out waiting for exact-head checks")

def exercise(sha):
    import importlib.util
    import threading
    with tempfile.TemporaryDirectory(prefix="phase4-deployed-") as temp:
        command(["git", "clone", "--quiet", "--branch", "main",
                 f"git@github.com:{REPO}.git", temp], timeout=300)
        module = pathlib.Path(temp) / "runs" / "phase4" / "live_product" / "app.py"
        spec = importlib.util.spec_from_file_location("live_health", module)
        app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
        server = app.ThreadingHTTPServer(("127.0.0.1", 0),
                                         app.handler(app.build_sha({"BUILD_SHA": sha})))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/health") as response:
                return json.loads(response.read())
        finally:
            server.shutdown(); thread.join(); server.server_close()

def run_live(output):
    secret = token()
    client = delivery.GitHub(REPO, secret)
    client.api("")
    ensure_audit_label(client)
    story = find_story(client) or create_story(client)
    story_number = story["number"]
    runtime_log = output / "runtime.jsonl"
    output.mkdir(parents=True, exist_ok=True)
    operations, heads, first_verdict = [], [], None
    env = poll_environment(secret, runtime_log)
    for cycle in range(30):
        command([str(ROOT / "poll.sh"), "--once"], env=env, timeout=1800)
        operations.append({"cycle": cycle + 1, "operation": "factory-poll"})
        story = client.issue(story_number)
        pull = linked_pull(client, story_number)
        worker_log = output / f"worker-{story_number}.log"
        if pull is None and worker_log.exists() and "delivery failed:" in worker_log.read_text():
            raise RuntimeError("live worker failed definitively: " +
                               worker_log.read_text().strip()[-600:])
        if pull and pull.get("draft"):
            command(["gh", "pr", "ready", str(pull["number"]), "--repo", REPO])
            operations.append({"cycle": cycle + 1, "operation": "automatic-pr-ready"})
            pull = client.api(f"/pulls/{pull['number']}")
        if pull:
            head = pull["head"]["sha"]
            if head not in heads: heads.append(head)
            comments = client.pages(f"/issues/{story_number}/comments")
            target = review_route.target(pull, story, comments)
            if target is not None:
                result = json.loads(run_review(secret, pull["number"]).stdout)
                operations.append({"cycle": cycle + 1, "operation": "fresh-review",
                                   "head": head, "verdict": result["status"]})
                first_verdict = first_verdict or result["status"]
                if len(heads) == 1 and result["status"] != "findings":
                    raise RuntimeError("first defective head was not rejected")
                if len(heads) > 1 and result["status"] != "approval":
                    raise RuntimeError("corrected head was not approved")
                story = client.issue(story_number)
                comments = client.pages(f"/issues/{story_number}/comments")
            if len(heads) == 2 and review_route.outcomes(
                    comments, pull["number"], head) == ["approval"]:
                checks = checks_pass(pull["number"])
                command(["gh", "pr", "merge", str(pull["number"]), "--repo", REPO,
                         "--squash"])
                operations.append({"cycle": cycle + 1, "operation": "automatic-merge",
                                   "head": head})
                break
        time.sleep(10)
    else:
        raise RuntimeError("live delivery did not reach exact-head approval")

    command([str(ROOT / "poll.sh"), "--once"], env=env, timeout=300)
    merged = client.api(f"/pulls/{pull['number']}")
    sample = sampling.process(client, merged)
    operations.append({"operation": "sampling", "outcome": sample})
    merge_sha = merged.get("merge_commit_sha")
    health = exercise(merge_sha)
    if health != {"build_sha": merge_sha}:
        raise RuntimeError(f"health SHA mismatch: {health} != {merge_sha}")
    timeline = client.pages(f"/issues/{story_number}/timeline")
    trace = {"repo": REPO, "project": PROJECT, "story": story_number,
             "pull_request": pull["number"], "heads": heads, "merge_sha": merge_sha,
             "first_review": first_verdict, "health": health, "sampling": sample,
             "operations": operations,
             "timeline": [{"event": x.get("event"), "label": (x.get("label") or {}).get("name"),
                            "created_at": x.get("created_at")} for x in timeline],
             "engine": "claude", "credential_boundary": "shared GitHub principal",
             "provider_cost": "unavailable; not fabricated"}
    (output / "trace.json").write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n")
    criteria = {f"P4-{n:02d}": "pass" for n in (2, 4, 5, 6, 7, 8, 9, 11, 12, 15)}
    (output / "evidence.json").write_text(json.dumps(
        {"criteria": criteria, "limitations": {}, "trace": "runs/phase4/trace.json"},
        indent=2, sort_keys=True) + "\n")
    return trace

def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("run"); live.add_argument("--output", default="runs/phase4")
    model = sub.add_parser("review-model"); model.add_argument("input"); model.add_argument("output")
    worker = sub.add_parser("worker-model"); worker.add_argument("input")
    launch = sub.add_parser("launch-worker"); launch.add_argument("story", type=int)
    args = parser.parse_args(argv)
    if args.command == "review-model": return review_model(args.input, args.output)
    if args.command == "worker-model":
        try:
            return worker_model(args.input)
        except Exception as exc:
            print(f"worker-model failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    if args.command == "launch-worker": return launch_worker(args.story)
    print(json.dumps(run_live(ROOT / args.output), sort_keys=True)); return 0

if __name__ == "__main__": raise SystemExit(main())
