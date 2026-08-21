#!/usr/bin/env python3
"""Wiring — the real factory, pointed at an in-memory repository.

Nothing here decides anything. It exists so a scenario can say "poll once" and
get *the actual poll*: the real dispatcher deciding authorization and WIP, the
real reconciliation and continuation passes, the real completion path, with only
two things replaced —

* `dispatcher._api`, the single choke point every read and write passes through,
  becomes the in-memory repository;
* `workers.run_observed`, the process boundary, becomes a callable standing in
  for a CLI engine, because launching a real engine is Phase 4's business and
  not something an acceptance suite can assert on.

`poller.run_dispatcher` is redirected to call `dispatcher.main` **in-process**
rather than as a subprocess. That is the one substitution worth justifying: the
subprocess boundary is real, and a suite that skipped it would miss a dispatcher
that cannot start. It is covered instead by the dispatcher's own CLI tests and
by the live runs recorded in the acceptance evidence; what this buys is that the
dispatcher's decisions and the poller's reactions meet over one repository, in
one process, deterministically.
"""

from __future__ import annotations

import io
import os
import subprocess
from contextlib import contextmanager, redirect_stdout
from unittest import mock

import continuation
import dispatcher
import poller
import workers

REPO = "o/r"
COMMITMENT = 54


def engine(hub, *, posts=None, exit_code=0, stdout="engine finished", stderr="",
           on_call=None):
    """A stand-in for a CLI worker engine, at the `run_observed` boundary.

    `posts` is the acknowledgement heading the worker writes to its Story, which
    is the durable evidence §9.16 requires. `on_call` is anything else the
    worker does to the repository — opening a pull request, typically.
    """
    calls = []

    def run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        hub.advance(minutes=1)
        if posts:
            hub.post_comment(posts["story"], posts["body"])
        if on_call:
            on_call(hub, cmd)
        return subprocess.CompletedProcess(cmd, exit_code, stdout, stderr)

    run.calls = calls
    return run


@contextmanager
def factory(hub, worker=None, env=None):
    """The real components, over one in-memory repository.

    The ambient `FACTORY_WORKER*` configuration is cleared first: a worker
    declared in the developer's shell would route a scenario through a path it
    was not written for, and the failure would look like a factory bug. `env`
    is the configuration the scenario itself declares, applied after the clear.
    """
    previous = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("FACTORY_WORKER"):
            del os.environ[key]
    os.environ["GITHUB_TOKEN"] = "acceptance-token"
    os.environ.update(env or {})
    if not any(key.startswith("FACTORY_WORKER_ORDER") for key in os.environ):
        os.environ.setdefault("FACTORY_WORKER_CMD", "/usr/bin/true")

    def in_process_dispatcher(*args, **kwargs):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            dispatcher.main(["--repo", REPO, "--commitment", str(COMMITMENT), "--claim"])
        return buffer.getvalue()

    # `continuation.py` keeps its own HTTP client rather than going through
    # `dispatcher._api`, so the choke point does not reach it. Its three I/O
    # functions are routed to the same repository here — a shim, not a stub:
    # the decision logic under test is still continuation's own.
    def projects(repo, token):
        return {number: dict(issue) for number, issue in hub.issues.items()
                if any(label["name"] == "type:project" for label in issue["labels"])}

    def comments(repo, number, token):
        return list(hub.comments.get(number, []))

    def apply_outcome(repo, project_issue, outcome, token):
        labels = sorted((
            {label["name"] for label in project_issue["labels"]}
            - {continuation.lifecycle_of(project_issue)}) | {outcome.action})
        payload = {"labels": labels}
        if outcome.action == continuation.ACCEPTED:
            payload.update({"state": "closed", "state_reason": "completed"})
        hub(f"https://api.github.com/repos/{repo}/issues/{project_issue['number']}",
            token, method="PATCH", payload=payload)
        return True, f"{continuation.lifecycle_of(project_issue)} -> {outcome.action}"

    patches = [
        mock.patch.object(dispatcher, "_api", hub),
        mock.patch.object(continuation, "fetch_projects", projects),
        mock.patch.object(continuation, "fetch_comments", comments),
        mock.patch.object(continuation, "apply_outcome", apply_outcome),
        mock.patch.object(poller, "run_dispatcher", side_effect=in_process_dispatcher),
    ]
    if worker is not None:
        patches.append(mock.patch.object(workers, "run_observed", worker))
    for patch in patches:
        patch.start()
    try:
        yield
    finally:
        for patch in reversed(patches):
            patch.stop()
        os.environ.clear()
        os.environ.update(previous)


def poll(hub, worker=None, seen=None, env=None) -> tuple[list, str]:
    """One real poll cycle. Returns (dispatches, stdout)."""
    buffer = io.StringIO()
    with factory(hub, worker, env), redirect_stdout(buffer):
        woken = poller.poll_once(REPO, COMMITMENT, seen if seen is not None else set())
    return woken, buffer.getvalue()


def dispatch_only(hub, claim=True) -> str:
    """The dispatcher alone, over the same repository. Returns its stdout."""
    buffer = io.StringIO()
    argv = ["--repo", REPO, "--commitment", str(COMMITMENT)] + (["--claim"] if claim else [])
    with factory(hub), redirect_stdout(buffer):
        dispatcher.main(argv)
    return buffer.getvalue()
