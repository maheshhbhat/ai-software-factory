#!/usr/bin/env python3
"""Standard worker contract — one shape, many execution engines.

Phase 2 Worker Swappability (#84). A delivery worker is an execution engine the
factory points at an authorized Story. Claude and Codex are configuration under
this contract, not code paths anywhere else: the dispatcher decides *what* may
run, this module decides *which engine* runs it, and neither knows the other's
business.

**The factory owns policy; workers own execution.** No adapter may contain
authorization, lifecycle, WIP, retry, or terminal-state logic — a test asserts
that by inspection, because the temptation to "just check one thing" inside an
adapter is exactly how governance leaks into a worker and stops being auditable.

Health and capability are *routing* observations. They may decide which engine
gets a Story; they may never decide whether a Story is authorized, how much WIP
is in use, how much retry budget remains, or whether anything is terminal. Those
live in GitHub (§9.12), and a local probe result must never be able to contradict
them.

## The correctness property this module exists to protect

Failover is the first mechanism in the factory that could produce *two* workers
on one Story. Safety rests on two rules:

  1. **Launch sequentially, stop at the first success.** At most one launch can
     succeed, so at most one worker is ever started.
  2. **A definite failure is not an ambiguous outcome.** `command not found` or
     a non-zero exit proves the worker did not start, so falling back is safe.
     A timeout proves nothing — the worker may be running right now — so it
     fails closed with no fallback, and the bounded §9.4 lease path recovers the
     claim.

Rule 2 is the one worth defending in review. Treating "I don't know" as "it
failed" is precisely how a system ends up with two workers writing to one branch.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import runlog  # noqa: E402 — operational record; never a control surface (#104)

# How long a health probe may take before it is considered unusable. A slow
# probe is a routing signal, not an error: the worker simply is not selected.
HEALTH_TIMEOUT_SECONDS = 20

# How long a launch may take to *hand off* work. This is not how long the worker
# runs — it is how long the factory waits to learn whether the handoff happened.
LAUNCH_TIMEOUT_SECONDS = 60

WORKER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")


class Result:
    """Stable result categories the runtime routes on.

    `FAILED` and `AMBIGUOUS` are deliberately distinct. Collapsing them would
    make fallback unsafe — see the module docstring.
    """

    LAUNCHED = "LAUNCHED"          # the worker definitely accepted the assignment
    FAILED = "FAILED"              # the worker definitely did not start; fallback is safe
    AMBIGUOUS = "AMBIGUOUS"        # unknown — the worker may be running; never fall back
    UNHEALTHY = "UNHEALTHY"        # probe says unusable; not selected, nothing launched
    INCAPABLE = "INCAPABLE"        # cannot do this kind of work
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True)
class WorkerSpec:
    """What every delivery worker must declare.

    Deliberately small. Anything richer — model, cost, latency, quality — is out
    of scope for this increment and would invite the selector to start making
    judgments instead of following configuration.
    """

    name: str                      # stable logical id, matches Agent-ID (§9.7)
    launch: str                    # shell template; {story} {project} {agent} substituted
    health: str = ""               # probe command; empty means "assumed healthy"
    capabilities: frozenset = field(default_factory=frozenset)

    def validate(self) -> str | None:
        if not WORKER_NAME_RE.match(self.name or ""):
            return f"worker name {self.name!r} is not a valid Agent-ID"
        if not (self.launch or "").strip():
            return f"worker {self.name!r} declares no launch command"
        return None


@dataclass
class HealthReport:
    worker: str
    healthy: bool
    reason: str
    detail: str = ""


@dataclass
class LaunchReport:
    """The verdict, plus what was observed producing it.

    `result` and `detail` are the routing contract and are unchanged by #104.
    The fields below carry no routing meaning whatsoever — they exist so that a
    run can be diagnosed afterwards without an interactive session, and nothing
    may branch on them. Keeping them here rather than in a parallel structure is
    deliberate: the evidence and the verdict it produced travel together, so a
    trail entry cannot be read without the observation behind it.
    """

    worker: str
    result: str
    detail: str = ""
    exit_code: int | None = None
    elapsed_ms: int | None = None
    pid: int | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def launched(self) -> bool:
        return self.result == Result.LAUNCHED

    @property
    def may_fall_back(self) -> bool:
        """Only a *definite* failure permits trying another worker."""
        return self.result in (Result.FAILED, Result.UNHEALTHY, Result.INCAPABLE)


# --------------------------------------------------------------------------
# Configuration — adapters are declarations, not code
# --------------------------------------------------------------------------


def _spec_from_env(name: str) -> WorkerSpec | None:
    """Read one worker's declaration from the environment.

    `FACTORY_WORKER_<NAME>_LAUNCH`, `..._HEALTH`, `..._CAPABILITIES`.
    """
    key = name.upper().replace("-", "_")
    launch = os.environ.get(f"FACTORY_WORKER_{key}_LAUNCH", "").strip()
    if not launch:
        return None
    caps = os.environ.get(f"FACTORY_WORKER_{key}_CAPABILITIES", "delivery")
    return WorkerSpec(
        name=name,
        launch=launch,
        health=os.environ.get(f"FACTORY_WORKER_{key}_HEALTH", "").strip(),
        capabilities=frozenset(c.strip() for c in caps.split(",") if c.strip()),
    )


def configured_workers() -> list[WorkerSpec]:
    """Workers in preference order.

    Order comes from `FACTORY_WORKER_ORDER` — configuration, not judgment. The
    selector never reorders, scores, or second-guesses it (#84 item 4).
    """
    order = os.environ.get("FACTORY_WORKER_ORDER", "claude-delivery,codex-delivery")
    specs = []
    for name in (n.strip() for n in order.split(",") if n.strip()):
        spec = _spec_from_env(name)
        if spec is not None and spec.validate() is None:
            specs.append(spec)
    return specs


# --------------------------------------------------------------------------
# Health and capability — routing observations only
# --------------------------------------------------------------------------


def check_health(spec: WorkerSpec, runner=subprocess.run) -> HealthReport:
    """Probe one worker. Never raises; an unusable worker is a routing fact."""
    if not spec.health:
        return HealthReport(spec.name, True, "NO_PROBE_CONFIGURED",
                            "assumed healthy; no probe declared")
    try:
        completed = runner(shlex.split(spec.health), capture_output=True,
                           text=True, timeout=HEALTH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return HealthReport(spec.name, False, "PROBE_TIMEOUT",
                            f"health probe exceeded {HEALTH_TIMEOUT_SECONDS}s")
    except (OSError, ValueError) as exc:
        return HealthReport(spec.name, False, "PROBE_UNRUNNABLE", str(exc))
    if completed.returncode != 0:
        return HealthReport(spec.name, False, "PROBE_FAILED",
                            f"exit {completed.returncode}: "
                            f"{(completed.stderr or '').strip()[:200]}")
    return HealthReport(spec.name, True, "HEALTHY")


def is_capable(spec: WorkerSpec, required: str) -> bool:
    """Capability is declared, not inferred. An empty declaration means the
    worker claims nothing and is never selected for anything."""
    return required in spec.capabilities


# --------------------------------------------------------------------------
# Selection — deterministic, from configuration alone
# --------------------------------------------------------------------------


def select(specs: list[WorkerSpec], required: str = "delivery",
           runner=subprocess.run) -> tuple[list[WorkerSpec], list[HealthReport]]:
    """Eligible workers in preference order, with the reports that explain it.

    Reproducible from configured inputs: same configuration and same probe
    results give the same order, every time, with no scoring or tie-breaking.
    """
    eligible, reports = [], []
    for spec in specs:
        if not is_capable(spec, required):
            reports.append(HealthReport(spec.name, False, Result.INCAPABLE,
                                        f"does not declare {required!r}"))
            continue
        report = check_health(spec, runner=runner)
        reports.append(report)
        if report.healthy:
            eligible.append(spec)
    for report in reports:
        runlog.event("worker.health", worker=report.worker, healthy=report.healthy,
                     reason=report.reason, detail=report.detail)
    runlog.event("worker.selected", required=required,
                 configured=[s.name for s in specs],
                 eligible=[s.name for s in eligible])
    return eligible, reports


# --------------------------------------------------------------------------
# Launch — sequential, stopping at the first success
# --------------------------------------------------------------------------


def launch_command(spec: WorkerSpec, story: int, project: int) -> list[str]:
    """Resolve the launch command for one dispatch.

    The worker receives routing identity only — story, project, agent. No spec,
    no scope, no acceptance criteria: it reads the substrate itself
    (`architecture-v2.1.md` §4).
    """
    return [part.replace("{story}", str(story))
                .replace("{project}", str(project))
                .replace("{agent}", spec.name)
            for part in shlex.split(spec.launch)]


def run_observed(cmd, capture_output=True, text=True, timeout=None):
    """`subprocess.run`, plus the child's PID.

    The default runner for a launch, and the only reason it exists: `run()`
    never exposes the PID, so a hung worker could not be found in `ps`, attached
    to, or killed — the first question anyone asks about a hang was the one
    question the runtime could not answer (#104).

    Semantics match `subprocess.run` exactly, timeout included: the child is
    killed and reaped before `TimeoutExpired` propagates, so the caller's
    ambiguous-outcome handling is unchanged. The PID rides back on the returned
    object rather than via a new parameter, which keeps every injected test
    runner a plain `subprocess.run` stand-in.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text)
    runlog.event("process.started", pid=process.pid, cmd=runlog.command(cmd))
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    completed = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    completed.pid = process.pid
    return completed


def launch(spec: WorkerSpec, story: int, project: int,
           runner=None) -> LaunchReport:
    """Hand one Story to one worker.

    The distinction that matters: a timeout is `AMBIGUOUS`, not `FAILED`. The
    worker may have started and simply not returned in time, so the caller must
    not try anyone else — that is how one Story acquires two workers.

    Every path returns a report carrying what was observed — command, PID, exit
    status, elapsed time, and *both* output streams. Both, always: the verdict
    used to decide which stream survived, so a launched worker's stderr and a
    failed worker's stdout were discarded, and for a hang that was the half with
    the explanation in it.
    """
    cmd = launch_command(spec, story, project)
    runner = runner or run_observed
    started = time.monotonic()
    runlog.event("worker.launch.start", worker=spec.name, story=story, project=project,
                 cmd=runlog.command(cmd), timeout_s=LAUNCH_TIMEOUT_SECONDS)

    def finish(report: LaunchReport) -> LaunchReport:
        runlog.event("worker.launch.end", worker=spec.name, story=story, project=project,
                     result=report.result, exit=report.exit_code, pid=report.pid,
                     elapsed_ms=report.elapsed_ms, detail=report.detail,
                     stdout=report.stdout, stderr=report.stderr)
        return report

    try:
        completed = runner(cmd, capture_output=True, text=True,
                           timeout=LAUNCH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        return finish(LaunchReport(
            spec.name, Result.AMBIGUOUS,
            f"launch exceeded {LAUNCH_TIMEOUT_SECONDS}s; the worker may be "
            f"running. Not falling back — a second worker on one Story is "
            f"worse than a late one. The §9.4 lease will recover the claim "
            f"if nothing was started.",
            elapsed_ms=runlog.elapsed_ms(started),
            stdout=runlog.tail(_stream(exc.output)),
            stderr=runlog.tail(_stream(exc.stderr))))
    except FileNotFoundError as exc:
        return finish(LaunchReport(spec.name, Result.FAILED, f"not launchable: {exc}",
                                   elapsed_ms=runlog.elapsed_ms(started)))
    except (OSError, ValueError) as exc:
        return finish(LaunchReport(spec.name, Result.AMBIGUOUS,
                                   f"launch outcome unknown: {exc}",
                                   elapsed_ms=runlog.elapsed_ms(started)))

    return finish(report_from(spec.name, completed, started))


def report_from(worker: str, completed, started: float) -> LaunchReport:
    """One report from a process that finished: the verdict and its evidence.

    Shared with the poller's legacy single-command adapter, so both paths agree
    on what an exit code means *and* record the same observations. Two places
    deciding that separately is how one adapter quietly stops being diagnosable.
    """
    launched = completed.returncode == 0
    return LaunchReport(
        worker,
        Result.LAUNCHED if launched else Result.FAILED,
        (completed.stdout or "").strip()[:200] if launched
        else f"exit {completed.returncode}: {(completed.stderr or '').strip()[:200]}",
        exit_code=completed.returncode,
        elapsed_ms=runlog.elapsed_ms(started),
        pid=getattr(completed, "pid", None),
        stdout=runlog.tail(completed.stdout),
        stderr=runlog.tail(completed.stderr))


def _stream(value) -> str:
    """Output off a `TimeoutExpired`, which carries `bytes` or `str` depending on
    how the child was opened."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def dispatch_to_worker(specs: list[WorkerSpec], story: int, project: int,
                       required: str = "delivery",
                       runner=None) -> tuple[LaunchReport | None, list]:
    """Give one Story to exactly one worker, or to none.

    Returns (successful report or None, the trail of every attempt). The trail is
    the audit record: a Story that ran nowhere must say why, for each engine.
    """
    eligible, health_reports = select(specs, required,
                                      runner=runner or subprocess.run)
    trail: list = list(health_reports)

    if not eligible:
        runlog.event("worker.outcome", story=story, project=project, worker=None,
                     result="NO_ELIGIBLE_WORKER",
                     detail="no configured worker is both capable and healthy")
        return None, trail

    for index, spec in enumerate(eligible):
        report = launch(spec, story, project, runner=runner)
        trail.append(report)
        remaining = [s.name for s in eligible[index + 1:]]
        if report.launched:
            runlog.event("worker.failover", story=story, project=project,
                         worker=spec.name, decision="NOT_NEEDED",
                         result=report.result, skipped=remaining,
                         reason="the worker accepted the assignment")
            runlog.event("worker.outcome", story=story, project=project,
                         worker=spec.name, result=report.result, detail=report.detail,
                         exit=report.exit_code, elapsed_ms=report.elapsed_ms)
            return report, trail
        if not report.may_fall_back:
            # AMBIGUOUS: stop. The worker may be running, and starting another
            # would put two workers on one Story.
            runlog.event("worker.failover", story=story, project=project,
                         worker=spec.name, decision="SUPPRESSED",
                         result=report.result, skipped=remaining,
                         reason="the outcome is ambiguous — the worker may be running, "
                                "and a second worker on one Story is worse than a late "
                                "one. The §9.4 lease recovers the claim.")
            runlog.event("worker.outcome", story=story, project=project,
                         worker=spec.name, result=report.result, detail=report.detail,
                         exit=report.exit_code, elapsed_ms=report.elapsed_ms)
            return None, trail
        runlog.event("worker.failover", story=story, project=project,
                     worker=spec.name, decision="FELL_BACK" if remaining else "EXHAUSTED",
                     result=report.result, next=remaining[0] if remaining else None,
                     skipped=[], reason="a definite failure proves the worker did not "
                                        "start, so trying another is safe")
    runlog.event("worker.outcome", story=story, project=project, worker=None,
                 result="NO_WORKER_LAUNCHED",
                 detail="every eligible worker failed definitely")
    return None, trail
