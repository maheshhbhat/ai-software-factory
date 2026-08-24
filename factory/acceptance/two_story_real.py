#!/usr/bin/env python3
"""Operator-invoked, never-CI two-Story live E2E scenario.

Creates real GitHub artifacts and spends real engine calls. It is never scheduled
and never a required check.
"""
from __future__ import annotations

import argparse, json, os, pathlib, re, shutil, signal, subprocess, sys, tempfile, time, traceback, urllib.error, urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]

FORBIDDEN = ("FACTORY_DELIVERY_MODEL_CMD", "FACTORY_REVIEW_MODEL_CMD",
             "FACTORY_WORKER_CMD", "FACTORY_REVIEW_CMD")
PREFIXES = ("FACTORY_WORKER_",)
STORY_SECTION = re.compile(r"(### Stories\s*\n).*?(?=\n### )", re.S)
REVIEW_MARKER = re.compile(r"<!-- review-outcome:(\d+):([0-9a-f]{7,64}):(approval|findings) -->")
TRACE_EVENTS = {"story.claimed", "dispatch.received", "delivery.pull-request.written",
                "review.outcome.published", "review_link.transition"}
LIVE_COMPONENTS = {"poller", "delivery-worker", "independent-review"}

class HarnessInterrupted(KeyboardInterrupt):
    def __init__(self, signum: int | None = None):
        self.signum = signum
        super().__init__(f"harness interrupted by signal {signum}" if signum else
                         "harness interrupted")

    @property
    def exit_code(self):
        return 128 + self.signum if self.signum is not None else 130

def termination_handler(run):
    """Stop the child poller before the harness exits on service signals."""
    def handle(signum, _frame):
        run.stop()
        raise HarnessInterrupted(signum)
    return handle

def labels(issue):
    return {item["name"] for item in issue.get("labels", [])}

def lifecycle(issue, prefix):
    found=sorted(name for name in labels(issue) if name.startswith(prefix))
    return found[0] if len(found)==1 else None

def review_observation(records, story, pull, head):
    """Return exact-review stages and elapsed time from the production run log."""
    names=("review.preparing","review.clone.started","review.clone.finished",
           "review.engine.started","review.engine.finished","review.publishing",
           "review.published")
    found={}
    for row in records:
        if row.get("event") not in names or row.get("pull_request") != pull:
            continue
        if row.get("story") not in (None, story): continue
        if row.get("head") not in (None, head): continue
        found.setdefault(row["event"],row.get("timestamp"))
    if set(found) != set(names) or not all(found.values()): return {}
    start=datetime.fromisoformat(found["review.preparing"].replace("Z","+00:00"))
    finish=datetime.fromisoformat(found["review.published"].replace("Z","+00:00"))
    engine_start=datetime.fromisoformat(found["review.engine.started"].replace("Z","+00:00"))
    engine_finish=datetime.fromisoformat(found["review.engine.finished"].replace("Z","+00:00"))
    return {"stages":found,"total_seconds":round((finish-start).total_seconds(),3),
            "engine_seconds":round((engine_finish-engine_start).total_seconds(),3)}

def roadmap_commitment(body):
    match=re.search(r"(?ms)^### Roadmap commitment\s*$\n\s*#([1-9][0-9]*)\s*(?=^### |\Z)",body or "")
    if not match: raise RuntimeError("project has no canonical Roadmap commitment")
    return int(match.group(1))

def forbidden_overrides(env):
    return sorted(k for k, v in env.items() if v and
                  (k in FORBIDDEN or any(k.startswith(p) for p in PREFIXES)))

def durable_replay_state(snapshot):
    """Keep only state whose change means the factory replay wrote something.

    Observability counts, usage rows, and review timings describe additional
    reads and heartbeats. They are expected to grow while the replay is being
    observed and are not lifecycle mutations.
    """
    story_fields=("number","walk","claimed_at","merged_at","pull_count",
                  "pull","head","merged","closed","exact_approval","checks")
    return {
        "project_state":snapshot.get("project_state"),
        "receipts":snapshot.get("receipts",[]),
        "stories":[{key:item.get(key) for key in story_fields}
                   for item in snapshot.get("stories",[])],
    }

def story_body(run, project, ordinal, dependency=None):
    suffix = "json" if ordinal == 1 else "md"
    target = f"runs/two-story-real/{run}/story-{ordinal}/artifact.{suffix}"
    content = (f'a JSON object containing exactly {{"run":"{run}","story":1}}'
               if ordinal == 1 else
               f'a Markdown file containing exactly `run: {run}`')
    return f"""### Spec

Create `{target}` as {content}. Do not change any other file.

### Project

#{project}

### Phase

build

### Depends-on

{f'#{dependency}' if dependency else 'none'}

### Hazard

- [ ] Touches hazard path

### Attempt

0

### Spend cap

$5 / 60 min

### Scope

runs/two-story-real/{run}/story-{ordinal}/**

### Acceptance notes

- The scoped artifact exists and has exactly the requested content.
- No file outside Scope changes.

<!-- two-story-real:{run}:{ordinal} -->"""

def story_list(numbers):
    """The sequencer's Stories dialect is one bare #N per line, never bullets."""
    return "\n".join(f"#{number}" for number in numbers)

def verdict(data):
    if data.get("aborted"): return False, data["aborted"]
    if len(data.get("stories", [])) != 2: return False, "not exactly two stories"
    a, b = data["stories"]
    for n, item in enumerate((a, b), 1):
        if item.get("pull_count") != 1: return False, f"story {n} does not have exactly one PR"
        if not item.get("merged") or not item.get("closed"): return False, f"story {n} did not merge and close"
        if "story:merged" not in item.get("walk", []): return False, f"story {n} lacks merged transition"
        if not item.get("exact_approval"): return False, f"story {n} lacks exact-head approval"
        timing=item.get("review_timing") or {}
        if not timing or timing.get("total_seconds",61)>60: return False, f"story {n} lacks a bounded observable review"
        if set(item.get("checks", [])) != {"merge-gate", "merge-gate-surface"}: return False, f"story {n} lacks checks"
        if not item.get("usage_records"): return False, f"story {n} lacks Codex usage"
    if b.get("claimed_at", "") <= a.get("merged_at", ""): return False, "dependency ordering failed"
    if data.get("project_state") != "project:awaiting-acceptance": return False, "project did not reach acceptance"
    receipts=data.get("receipts",[])
    if sum(r.get("bell_type")=="plan-approval" for r in receipts) != 1: return False, "plan-approval receipt missing or duplicated"
    if data.get("replay_changed"): return False, "replay mutated durable state"
    observation=data.get("observability") or {}
    if not observation.get("valid"): return False, "observability streams are missing or invalid"
    for item in observation.get("stories",[]):
        if set(item.get("events",[])) != TRACE_EVENTS: return False, f"story {item.get('story')} trace events are incomplete"
        if len(item.get("trace_ids",[])) != 1: return False, f"story {item.get('story')} does not have one trace"
        if set(item.get("components",[])) != LIVE_COMPONENTS: return False, f"story {item.get('story')} component activity is incomplete"
        if not item.get("heartbeat"): return False, f"story {item.get('story')} has no live heartbeat"
    return True, "two-story delivery completed"

def evidence_comment(evidence):
    """Render the complete, human-reviewable evidence; fail on missing fields."""
    lines = ["## Two-story E2E evidence", "",
             f"Run `{evidence['run']}`: **{'PASS' if evidence['passed'] else 'FAIL'}** — {evidence['reason']}",
             "", f"Project state: `{evidence['project_state']}`", ""]
    for story in evidence["stories"]:
        required = ("number", "pull", "head", "claimed_at", "merged_at", "walk",
                    "checks", "exact_approval", "usage_records", "review_timing")
        missing = [key for key in required if not story.get(key)]
        if missing:
            raise ValueError(f"story evidence incomplete: {', '.join(missing)}")
        lines.extend([
            f"- Story #{story['number']} / PR #{story['pull']}",
            f"  - merged head: `{story['head']}`",
            f"  - lifecycle: {' → '.join(story['walk'])}",
            f"  - claimed: `{story['claimed_at']}`; merged: `{story['merged_at']}`",
            f"  - checks: {', '.join(story['checks'])}",
            f"  - exact-head independent review: {'approval' if story['exact_approval'] else 'missing'}",
            f"  - review timing: {story['review_timing']['total_seconds']}s total; "
            f"{story['review_timing']['engine_seconds']}s engine; stages: "
            + ", ".join(story['review_timing']['stages']),
            "  - Codex usage: " + "; ".join(
                f"input={(row.get('usage') or {}).get('input_tokens', 'unknown') if isinstance(row.get('usage'), dict) else 'unknown'}, "
                f"output={(row.get('usage') or {}).get('output_tokens', 'unknown') if isinstance(row.get('usage'), dict) else 'unknown'}"
                for row in story["usage_records"]),
        ])
    receipts = evidence.get("receipts") or []
    if not receipts:
        raise ValueError("touch-log receipt evidence missing")
    lines.extend(["", "Touch-log receipts:"])
    for receipt in receipts:
        lines.append(f"- `{receipt.get('bell_type')}` at `{receipt.get('timestamp')}` — {receipt.get('note')}")
    lines.extend(["", f"Replay changed durable state: `{str(evidence['replay_changed']).lower()}`",
                  f"Observability streams valid: `{str(evidence['observability']['valid']).lower()}`",
                  "", f"Evidence bundle: `runs/two-story-real/{evidence['run']}/evidence.json`"]) 
    return "\n".join(lines)

class Run:
    def __init__(self, args):
        self.args=args; self.run=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.token=""; self.story=[]; self.poller=None
        self.tmp=pathlib.Path(tempfile.mkdtemp(prefix=f"two-story-{self.run}-"))
        self.final=pathlib.Path(args.evidence_root)/self.run
        self.run_dir=self.tmp/"observability"
        self.persist({"run":self.run,"project":args.project,"status":"STARTING",
                      "stories":[],"timestamp":datetime.now(timezone.utc).isoformat()})
    def persist(self, state=None):
        if state is not None:
            (self.tmp/"run-state.json").write_text(json.dumps(state,indent=2,sort_keys=True)+"\n")
        self.final.parent.mkdir(parents=True,exist_ok=True)
        shutil.copytree(self.tmp,self.final,dirs_exist_ok=True)
    def api(self, path, method="GET", payload=None):
        data=None if payload is None else json.dumps(payload).encode()
        request=urllib.request.Request(f"https://api.github.com/repos/{self.args.repo}{path}",data=data,method=method,headers={"Authorization":f"Bearer {self.token}","Accept":"application/vnd.github+json","Content-Type":"application/json","User-Agent":"factory-two-story-e2e"})
        try:
            with urllib.request.urlopen(request,timeout=30) as response: return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"GitHub {method} {path} returned {exc.code}; authentication or repository access failed") from exc
    def preflight(self):
        bad=forbidden_overrides(os.environ)
        if bad: raise RuntimeError("substitution overrides present: " + ", ".join(bad))
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and not (pathlib.Path.home()/".factory-reviewer-token").is_file(): raise RuntimeError("reviewer credential missing")
        self.token=(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or
                    subprocess.run(["gh","auth","token"],capture_output=True,text=True,
                                   check=True,timeout=30).stdout.strip())
        project=self.api(f"/issues/{self.args.project}")
        if lifecycle(project,"project:")!="project:active": raise RuntimeError("project is not active")
        self.commitment=roadmap_commitment(project.get("body") or "")
        return project
    def create(self, project):
        first=self.api("/issues","POST",{"title":f"[Story] Two-story E2E {self.run} — JSON", "body":story_body(self.run,self.args.project,1),"labels":["type:story","story:ready","phase:build"]})
        second=self.api("/issues","POST",{"title":f"[Story] Two-story E2E {self.run} — receipt", "body":story_body(self.run,self.args.project,2,first["number"]),"labels":["type:story","story:blocked","phase:build"]})
        self.story=[first["number"],second["number"]]
        self.persist({"run":self.run,"project":self.args.project,"status":"FIXTURES_CREATED",
                      "stories":self.story,"timestamp":datetime.now(timezone.utc).isoformat()})
        body=STORY_SECTION.sub(lambda m: m.group(1)+"\n"+story_list(self.story)+"\n",
                               project["body"],count=1)
        self.api(f"/issues/{self.args.project}","PATCH",{"body":body})
    def spawn(self):
        env={k:v for k,v in os.environ.items() if not (k in FORBIDDEN or any(k.startswith(p) for p in PREFIXES))}
        env["FACTORY_COMMITMENT"]=str(self.commitment)
        env["FACTORY_RUN_DIR"]=str(self.run_dir)
        log=open(self.tmp/"poller.log","w")
        self.poller=subprocess.Popen(["sh",str(ROOT/"poll.sh"),"--interval",str(self.args.heartbeat)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    def one(self,n):
        issue=self.api(f"/issues/{n}"); timeline=self.api(f"/issues/{n}/timeline?per_page=100") or []
        walk=[]; times={}
        for e in timeline:
            label=(e.get("label") or {}).get("name","")
            if e.get("event")=="labeled" and label.startswith("story:"): walk.append(label); times[label]=e.get("created_at","")
        pulls=[p for p in (self.api("/pulls?state=all&per_page=100") or []) if f"Story: #{n}" in (p.get("body") or "")]
        pull=pulls[0] if len(pulls)==1 else {}; head=(pull.get("head") or {}).get("sha",""); outcomes=[]
        for c in self.api(f"/issues/{n}/comments?per_page=100") or []: outcomes += REVIEW_MARKER.findall(c.get("body") or "")
        checks=[]
        if head:
            checks=[x["name"] for x in (self.api(f"/commits/{head}/check-runs") or {}).get("check_runs",[]) if x.get("conclusion")=="success" and x.get("name") in ("merge-gate","merge-gate-surface")]
        usage_records=[]; review_timing={}
        process_log=self.run_dir/"process-events.jsonl"
        telemetry_log=self.run_dir/"telemetry.jsonl"
        process_records=([json.loads(x) for x in process_log.read_text().splitlines() if x.strip()]
                         if process_log.exists() else [])
        telemetry_records=([json.loads(x) for x in telemetry_log.read_text().splitlines() if x.strip()]
                           if telemetry_log.exists() else [])
        usage_records=[row for row in telemetry_records if row.get("metric")=="engine.usage" and row.get("story")==n and row.get("engine")=="codex" and row.get("launch")=="completed"]
        review_timing=review_observation(process_records,n,pull.get("number"),head)
        return {"number":n,"walk":walk,"claimed_at":times.get("story:claimed",""),"merged_at":times.get("story:merged",""),"pull_count":len(pulls),"pull":pull.get("number"),"head":head,"merged":bool(pull.get("merged_at")),"closed":issue.get("state")=="closed","exact_approval":any(h==head and v=="approval" for _p,h,v in outcomes),"checks":sorted(set(checks)),"usage_records":usage_records,"review_timing":review_timing}
    def snapshot(self):
        project=self.api(f"/issues/{self.args.project}")
        touch=ROOT/"factory/touchlog/touchlog.jsonl"; receipts=[]
        if touch.exists(): receipts=[json.loads(x) for x in touch.read_text().splitlines() if x.strip() and json.loads(x).get("project")==f"#{self.args.project}"]
        return {"stories":[self.one(n) for n in self.story],"project_state":lifecycle(project,"project:"),"receipts":receipts,
                "observability":self.observability()}
    def observability(self):
        records={}
        for kind,name in (("process","process-events.jsonl"),("operation","operations.jsonl"),("telemetry","telemetry.jsonl")):
            path=self.run_dir/name
            records[kind]=([json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                           if path.exists() else [])
        invalid=[]
        for kind,rows in records.items():
            for row in rows:
                if row.get("record_type")!=kind: invalid.append(f"{kind}:record_type")
                if kind=="process" and any(key in row for key in ("level","stack_trace","metric")): invalid.append("process:cross-field")
                if kind=="operation" and any(key in row for key in ("event","metric")): invalid.append("operation:cross-field")
                if kind=="telemetry" and any(key in row for key in ("event","level","stack_trace")): invalid.append("telemetry:cross-field")
        stories=[]
        for number in self.story:
            process=[row for row in records["process"] if row.get("story")==number and row.get("event") in TRACE_EVENTS]
            activity=[row for row in records["telemetry"] if row.get("story")==number and str(row.get("metric","")).startswith("activity.")]
            stories.append({"story":number,"events":sorted({row["event"] for row in process}),
                            "trace_ids":sorted({row.get("trace_id") for row in process+activity if row.get("trace_id")}),
                            "components":sorted({row.get("component") for row in activity if row.get("component") in LIVE_COMPONENTS}),
                            "heartbeat":any(row.get("metric")=="activity.heartbeat" for row in activity)})
        return {"valid":all(records.values()) and not invalid,"invalid":invalid,
                "counts":{kind:len(rows) for kind,rows in records.items()},"stories":stories}
    def stop(self):
        if self.poller and self.poller.poll() is None:
            os.killpg(self.poller.pid,signal.SIGTERM); self.poller.wait(timeout=15)
    def execute(self):
        project=self.preflight(); self.create(project); self.spawn(); deadline=time.monotonic()+self.args.max_minutes*60; data={}
        while time.monotonic()<deadline:
            time.sleep(20); data=self.snapshot(); self.persist({"run":self.run,"project":self.args.project,"status":"RUNNING",**data}); print(f"[two-story-real] {[(x['number'],x['walk'][-1] if x['walk'] else 'new') for x in data['stories']]}",flush=True)
            if data["project_state"]=="project:awaiting-acceptance": break
            if self.poller.poll() is not None: data["aborted"]="poller exited"; break
        else: data["aborted"]="wall timeout"
        before=durable_replay_state(data); time.sleep(self.args.heartbeat*2); after=self.snapshot()
        data["replay_changed"]=before!=durable_replay_state(after)
        self.stop(); ok,reason=verdict(data); evidence={"run":self.run,"project":self.args.project,"passed":ok,"reason":reason,**data}
        (self.tmp/"evidence.json").write_text(json.dumps(evidence,indent=2,sort_keys=True)+"\n"); self.persist(evidence)
        body=evidence_comment(evidence)
        self.api(f"/issues/{self.args.project}/comments","POST",{"body":body})
        return 0 if ok else 1
    def fail(self, error):
        self.stop()
        stack="".join(traceback.format_exception(type(error),error,error.__traceback__))
        evidence={"run":self.run,"project":self.args.project,"passed":False,
                  "reason":f"{type(error).__name__}: {error}","exception":stack,
                  "stories_created":self.story}
        # Preserve the primary failure before doing any more I/O.  Snapshotting
        # calls GitHub and can itself fail or be interrupted; that must never
        # erase the only useful stack trace from the run.
        (self.tmp/"evidence.json").write_text(json.dumps(evidence,indent=2,sort_keys=True)+"\n")
        (self.tmp/"exception.txt").write_text(stack)
        self.persist(evidence)
        if self.story and self.token:
            try: evidence.update(self.snapshot())
            except BaseException as snapshot_error:
                evidence["snapshot_error"]=(
                    f"{type(snapshot_error).__name__}: {snapshot_error}")
        (self.tmp/"evidence.json").write_text(json.dumps(evidence,indent=2,sort_keys=True)+"\n")
        self.persist(evidence)
        if self.token:
            try:
                self.api(f"/issues/{self.args.project}/comments","POST",{"body":
                    f"## Two-story E2E diagnostic\n\nRun `{self.run}` failed. No acceptance verdict was recorded.\n\n"
                    f"Failure: `{type(error).__name__}: {error}`\n\n"
                    f"Evidence: `runs/two-story-real/{self.run}/evidence.json`"})
            except Exception:
                pass

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--repo",default="maheshhbhat/ai-software-factory"); p.add_argument("--project",type=int,required=True); p.add_argument("--max-minutes",type=int,default=45); p.add_argument("--heartbeat",type=int,default=15); p.add_argument("--evidence-root",default="runs/two-story-real"); a=p.parse_args(argv)
    run=Run(a)
    stop_on_signal=termination_handler(run)
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name): signal.signal(getattr(signal, name), stop_on_signal)
    try: return run.execute()
    except KeyboardInterrupt as error:
        run.fail(error)
        print("[two-story-real] interrupted; child poller stopped",file=sys.stderr)
        return getattr(error, "exit_code", 130)
    except Exception as e:
        run.fail(e)
        print(f"[two-story-real] FAIL: {type(e).__name__}: {e}",file=sys.stderr); return 2
    finally:
        run.stop()
if __name__=="__main__": raise SystemExit(main())
