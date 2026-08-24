#!/usr/bin/env python3
"""What the operator's own wrapper must carry before it is allowed to poll.

`poll.sh` is the wrapper the factory runs day to day. Everything here is about
one class of defect: the wrapper starting a poller that can claim work but has
no engine to service it, or has one that was never going to deliver anything.

* Stories #299 and #300 were poisoned on 2026-08-22 — five and six claims each,
  no branch, no pull request, no worker process ever started — because the
  worker declarations lived only in `live-e2e.sh`, so `configured_workers()`
  returned an empty list under `poll.sh` and the claim simply expired each time.
* Stories #299, #300, #303, #318 and #324 reached `story:completed` with no
  deliverable because the launch target was `factory/runtime/bridge.py`, the
  Phase 2 acknowledgement stub, whose prompt forbids branches and commits.
* A launch string defaulted with `${VAR:-… --story {story} …}` loses its closing
  brace to the parameter expansion and resolves to `--story {story`, on which
  the engine exits 2.

The tests drive the real script with a stubbed `python3` on `PATH`, so what is
asserted is the environment the poller would actually have been handed — not a
re-implementation of the wrapper's logic in Python.

## What is deliberately *not* asserted here, and why

Three defects in the same failure story live in `factory/runtime/`, outside what
any wrapper can reach. They are owned by #345 (the launch cap) and #342 (the two
findings-retry defects), which depend on this Story:

* `workers.LAUNCH_TIMEOUT_SECONDS = 60` bounds every launch. Sixty seconds was
  the right bound for the Phase 2 acknowledgement bridge, whose task was posting
  one comment; a delivery takes minutes (#316 took seven), so the poller kills
  the worker and records `AMBIGUOUS`. The verdict is correct and must stay — a
  timeout does not prove the worker did nothing, so failover stays suppressed —
  but the cap belongs to the claimed Story's `### Spend cap`.
* `poller.py` builds its `seen` guard once outside the polling loop, so a Story
  woken again after review findings is skipped for the life of the process.
* `review_link.reconcile()` moves a claimed Story to `story:in-review` on the
  sole basis of an open pull request carrying its `Story: #N` link, without
  distinguishing "delivered, awaiting review" from "redispatched, correction in
  progress" — it reclaimed #328 sixty-two seconds after the claim.

None has an environment hook, and exporting one from `poll.sh` would put the
bound in two places — and would collide with the Story that owns it. What this
file *can* pin is that the wrapper is not their source, so none of the three
lands at this layer by mistake — that is what the "bounds the wrapper must not
be the source of" tests below do.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
POLL_SH = ROOT / "poll.sh"
WORKER_INVOKE = ROOT / "factory" / "agents" / "worker" / "invoke.py"

sys.path.insert(0, str(ROOT / "factory" / "runtime"))
import workers  # noqa: E402 — the resolver the wrapper must agree with

ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

PYTHON3_STUB = """#!/bin/sh
env > "$POLL_TEST_ENV"
: > "$POLL_TEST_ARGV"
for argument in "$@"; do printf '%s\\n' "$argument" >> "$POLL_TEST_ARGV"; done
exit 0
"""

GH_STUB = """#!/bin/sh
printf 'stub-token\\n'
"""


class PollRun:
    """One invocation of `poll.sh`, plus what the poller would have received."""

    def __init__(self, completed, environment, argv):
        self.completed = completed
        self.environment = environment
        self.argv = argv

    @property
    def polled(self) -> bool:
        """True when the wrapper actually reached `poller.py`."""
        return self.argv is not None

    def launch(self, worker: str = "claude-delivery") -> str:
        key = "FACTORY_WORKER_" + worker.upper().replace("-", "_") + "_LAUNCH"
        return self.environment.get(key, "")


def poll_sh_code() -> str:
    """`poll.sh` with its commentary removed.

    The comments explain what the wrapper deliberately does *not* do, and name
    the things it must never route to. Asserting over the whole file would make
    the explanation indistinguishable from the defect it warns about.
    """
    return "\n".join(line for line in POLL_SH.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))


class OperatorWrapper(unittest.TestCase):
    """Behaviour of `poll.sh` itself, observed by running it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="poll-sh-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stubs = pathlib.Path(self.tmp, "bin")
        self.stubs.mkdir()
        for name, body in (("python3", PYTHON3_STUB), ("gh", GH_STUB)):
            path = self.stubs / name
            path.write_text(body)
            path.chmod(0o755)

    def poll(self, *arguments: str, **overrides: str) -> PollRun:
        env_file = pathlib.Path(self.tmp, "captured-env")
        argv_file = pathlib.Path(self.tmp, "captured-argv")
        for stale in (env_file, argv_file):
            if stale.exists():
                stale.unlink()
        environment = {
            "PATH": f"{self.stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": self.tmp,
            "GH_TOKEN": "test-token",
            "POLL_TEST_ENV": str(env_file),
            "POLL_TEST_ARGV": str(argv_file),
            "FACTORY_COMMITMENT": "99",
        }
        environment.update(overrides)
        completed = subprocess.run(
            ["sh", str(POLL_SH), *arguments], cwd=str(ROOT), env=environment,
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        captured, argv = {}, None
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                match = ENV_LINE.match(line)
                if match:
                    captured[match.group(1)] = match.group(2)
        if argv_file.exists():
            argv = argv_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return PollRun(completed, captured, argv)

    # -- the wrapper carries its own configuration (WE-08) ------------------

    def test_polls_with_a_delivery_worker_declared(self):
        """The everyday wrapper needs nothing from the operator's shell."""
        run = self.poll("--once")
        self.assertEqual(0, run.completed.returncode, run.completed.stderr)
        self.assertTrue(run.polled, "poll.sh never reached poller.py")
        self.assertIn("factory/runtime/poller.py", run.argv[0])
        self.assertIn("--once", run.argv)
        self.assertNotEqual("", run.launch(),
                            "poll.sh polled without declaring a delivery worker")

    def test_refuses_without_an_explicit_commitment(self):
        run = self.poll("--once", FACTORY_COMMITMENT="")
        self.assertEqual(2, run.completed.returncode)
        self.assertFalse(run.polled, "poller started without an authorization root")
        self.assertIn("FACTORY_COMMITMENT is required", run.completed.stderr)
        self.assertIn("must never select its own implementation work", run.completed.stderr)

    def test_declared_worker_is_the_one_configured_workers_resolves(self):
        """The wrapper's guard and the dispatcher must read the same thing."""
        run = self.poll("--once")
        specs = self._configured_workers(run.environment)
        self.assertNotEqual([], specs, "configured_workers() saw an empty list")
        self.assertIn("delivery", set().union(*(s.capabilities for s in specs)))

    def test_only_codex_resolves_by_default(self):
        run = self.poll("--once")
        specs = self._configured_workers(run.environment)
        self.assertEqual(["codex-delivery"], [spec.name for spec in specs])

    def test_each_default_worker_selects_the_engine_it_names(self):
        run = self.poll("--once")
        self.assertIn("--engine claude", run.launch("claude-delivery"))
        self.assertIn("--engine codex", run.launch("codex-delivery"))
        self.assertNotEqual(run.launch("claude-delivery"),
                            run.launch("codex-delivery"))

    def test_plain_poll_dispatches_to_codex(self):
        run = self.poll("--once")
        spec = self._configured_workers(run.environment)[0]
        command = workers.launch_command(spec, story=367, project=314)
        self.assertEqual("codex-delivery", spec.name)
        self.assertEqual("codex", command[command.index("--engine") + 1])

    def test_refuses_to_poll_when_no_worker_resolves(self):
        """The defect that poisoned #299 and #300: claim work, launch nothing."""
        run = self.poll("--once", FACTORY_WORKER_ORDER="ghost-delivery")
        self.assertNotEqual(0, run.completed.returncode,
                            "poll.sh polled with no worker configured")
        self.assertFalse(run.polled, "poller.py was started with no worker")
        self.assertIn("no delivery worker resolves", run.completed.stderr)
        self.assertIn("ghost-delivery", run.completed.stderr)

    def test_refusal_agrees_with_configured_workers(self):
        """The refusal is not a guess — the resolver returns nothing too."""
        self.assertEqual([], self._configured_workers(
            {"FACTORY_WORKER_ORDER": "ghost-delivery"}))

    def test_proceeds_when_the_caller_declares_the_worker(self):
        """Refusal is about resolution, not about a hard-coded name."""
        run = self.poll(
            "--once",
            FACTORY_WORKER_ORDER="ghost-delivery",
            FACTORY_WORKER_GHOST_DELIVERY_LAUNCH=(
                "python3 factory/agents/worker/invoke.py --repo o/n --story {story}"))
        self.assertEqual(0, run.completed.returncode, run.completed.stderr)
        self.assertTrue(run.polled)

    def test_every_declaration_stays_overridable(self):
        """Defaults, not decisions: the caller's environment still wins."""
        mine = "python3 factory/agents/worker/invoke.py --repo me/mine --story {story}"
        run = self.poll("--once", FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH=mine,
                        FACTORY_REPO="me/mine", FACTORY_COMMITMENT="99")
        self.assertEqual(mine, run.launch())
        self.assertEqual(["factory/runtime/poller.py", "--repo", "me/mine",
                          "--commitment", "99", "--once"], run.argv)

    # -- the launch target is the Phase 4 worker (WE-05) --------------------

    def test_launch_target_is_the_phase4_delivery_worker(self):
        run = self.poll("--once")
        self.assertIn("factory/agents/worker/invoke.py", run.launch())

    def test_no_delivery_is_routed_to_the_acknowledgement_bridge(self):
        """The bridge is told not to create branches, commits or pull requests,
        so a Story routed to it completes having produced nothing."""
        run = self.poll("--once")
        self.assertNotIn("bridge.py", run.launch())
        self.assertNotIn("bridge.py", poll_sh_code())

    # -- the parameter-expansion regression ---------------------------------

    def test_resolved_launch_keeps_the_literal_story_placeholder(self):
        """`${VAR:-… --story {story} …}` ends at the `}` inside `{story}`, so the
        declaration silently becomes `--story {story` and the engine exits 2."""
        launch = self.poll("--once").launch()
        self.assertIn("{story}", launch)
        self.assertNotIn("{story ", launch)
        self.assertEqual(launch.count("{story}"), launch.count("{story"))

    def test_no_declaration_leaks_a_truncated_placeholder(self):
        run = self.poll("--once")
        for key, value in run.environment.items():
            if not key.startswith("FACTORY_WORKER_") or not key.endswith("_LAUNCH"):
                continue
            for placeholder in ("{story", "{project", "{agent"):
                self.assertEqual(
                    value.count(placeholder), value.count(placeholder + "}"),
                    f"{key} carries an unclosed placeholder: {value!r}")

    def test_placeholder_substitution_yields_the_story_number(self):
        """End to end through the resolver the poller actually uses."""
        run = self.poll("--once")
        spec = self._configured_workers(run.environment)[0]
        command = workers.launch_command(spec, story=329, project=325)
        self.assertIn("--story", command)
        self.assertEqual("329", command[command.index("--story") + 1])
        self.assertFalse([part for part in command if "{" in part],
                         f"a placeholder survived substitution: {command}")

    def test_the_declared_launch_is_a_command_the_worker_accepts(self):
        """A flag `invoke.py` does not take is exit 2 on every dispatch, which
        looks exactly like a definite failure and burns the attempt budget."""
        run = self.poll("--once")
        spec = self._configured_workers(run.environment)[0]
        command = workers.launch_command(spec, story=329, project=325)
        probe = subprocess.run(
            command, cwd=str(ROOT), capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp})
        self.assertNotIn("unrecognized arguments", probe.stderr,
                         f"poll.sh declares a flag invoke.py rejects: {command}")
        self.assertIn("no GH_TOKEN", probe.stderr,
                      f"invoke.py did not reach its own body: {probe.stderr[:400]}")

    # -- review runs from the wrapper, not from memory ----------------------

    def test_review_invocation_is_enabled_by_the_wrapper(self):
        run = self.poll("--once")
        self.assertEqual("1", run.environment.get("FACTORY_PHASE4_REVIEWS"),
                         "the poller would skip the Phase 4 review pass")

    def test_review_enablement_stays_overridable(self):
        run = self.poll("--once", FACTORY_PHASE4_REVIEWS="0")
        self.assertEqual("0", run.environment.get("FACTORY_PHASE4_REVIEWS"))

    # -- the wrapper holds no policy ---------------------------------------

    def test_the_wrapper_makes_no_durable_write_of_its_own(self):
        """Its own header: environment and arguments, nothing else. A lifecycle
        edit here would be a second source for a rule GitHub already owns."""
        source = poll_sh_code()
        for forbidden in ("gh issue", "gh pr", "gh api", "--add-label",
                          "--remove-label", "git push", "git commit"):
            self.assertNotIn(forbidden, source,
                             f"poll.sh performs a durable write: {forbidden!r}")

    def test_the_wrapper_reads_no_lifecycle_state(self):
        """Eligibility is the dispatcher's question. The only thing this wrapper
        asks GitHub for is a token."""
        calls = re.findall(r"\bgh\s+[a-z-]+", poll_sh_code())
        self.assertEqual(["gh auth"], sorted(set(calls)))

    # -- bounds the wrapper must not be the source of ------------------------
    #
    # The launch cap and the redispatch guard are runtime decisions. These pin
    # that `poll.sh` does not quietly become a second source for either — which
    # is the shape the fix would take if it were attempted at this layer.

    def test_the_wrapper_sets_no_timeout_of_its_own(self):
        """How long a worker may run is bounded by the claimed Story's `### Spend
        cap`, read by the runtime. A cap exported here would silently outrank it,
        and an operator debugging a killed delivery would have two places to look."""
        source = poll_sh_code()
        self.assertNotIn("TIMEOUT", source,
                         "poll.sh exports a timeout; the bound belongs to the Story")
        run = self.poll("--once")
        leaked = sorted(k for k in run.environment
                        if "TIMEOUT" in k and k.startswith("FACTORY_"))
        self.assertEqual([], leaked, f"poll.sh handed the poller a cap: {leaked}")

    def test_the_wrapper_keeps_no_memory_between_cycles(self):
        """Whether a woken Story is dispatched again is `poller.py`'s `seen`
        guard. A state file here would be a second, unauditable copy of it."""
        source = poll_sh_code()
        for forbidden in (">>", "mktemp", "/tmp", ".state", ".seen"):
            self.assertNotIn(forbidden, source,
                             f"poll.sh keeps state across cycles: {forbidden!r}")

    def test_the_wrapper_starts_the_long_lived_service_by_default(self):
        """With no arguments this is the service, not a one-shot — which is the
        only configuration in which a per-process guard can outlive a cycle."""
        run = self.poll()
        self.assertTrue(run.polled, "poll.sh never reached poller.py")
        self.assertNotIn("--once", run.argv)
        self.assertNotIn("--dry-run", run.argv)

    def test_arguments_reach_the_poller_untouched(self):
        """The wrapper forwards; it does not interpret. `--dry-run` deciding
        anything here would be policy in a file whose header forbids it."""
        run = self.poll("--once", "--dry-run")
        self.assertEqual(["factory/runtime/poller.py", "--repo", "maheshhbhat/ai-software-factory",
                      "--commitment", "99", "--once", "--dry-run"], run.argv)

    def _configured_workers(self, environment: dict) -> list:
        keep = {k: v for k, v in environment.items() if k.startswith("FACTORY_WORKER")}
        saved = {k: v for k, v in os.environ.items() if k.startswith("FACTORY_WORKER")}
        for key in saved:
            del os.environ[key]
        os.environ.update(keep)
        try:
            return workers.configured_workers()
        finally:
            for key in keep:
                os.environ.pop(key, None)
            os.environ.update(saved)


class ReviewerCredential(OperatorWrapper):
    """#349 — the reviewer builds a fresh identity and so needs a credential of
    its own. On macOS the CLI keeps credentials in the login keychain, which
    the reviewer's replaced HOME cannot reach, so with no explicit token every
    review refused `reviewer credential unavailable`. The wrapper reads a token
    minted once by `claude setup-token` from a file outside the repository."""

    TOKEN_FILE = ".factory-reviewer-token"

    def mint(self, value: str = "file-token") -> None:
        token = pathlib.Path(self.tmp, self.TOKEN_FILE)
        token.write_text(value)
        token.chmod(0o600)

    def test_the_reviewer_token_never_reaches_the_shared_poller_environment(self):
        self.mint()
        run = self.poll("--once")
        self.assertTrue(run.polled)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", run.environment)

    def test_an_explicit_shared_token_is_removed_before_worker_launch(self):
        self.mint("file-token")
        run = self.poll("--once", CLAUDE_CODE_OAUTH_TOKEN="caller-token")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", run.environment)

    def test_no_file_and_no_token_still_polls(self):
        """Reviews fail with their own named error; the wrapper adds none."""
        run = self.poll("--once")
        self.assertTrue(run.polled)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", run.environment)

    def test_the_token_never_appears_in_the_wrapper_output(self):
        self.mint("secret-value-1234")
        run = self.poll("--once")
        for stream in (run.completed.stdout, run.completed.stderr):
            self.assertNotIn("secret-value-1234", stream)



if __name__ == "__main__":
    unittest.main(verbosity=2)
