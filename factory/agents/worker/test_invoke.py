import json
import os
import pathlib
import subprocess
import sys
import tempfile
import io
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invoke
import runlog


class ParsingTests(unittest.TestCase):
    def test_bounds_are_read_from_story(self):
        value = invoke.parse_bounds("### Spend cap\n\n$40 / 90 min\n")
        self.assertEqual((value.max_usd, value.timeout), (40.0, 5400))

    def test_malformed_bounds_fail(self):
        with self.assertRaises(invoke.DeliveryError):
            invoke.parse_bounds("### Spend cap\n\nunbounded\n")

    def test_latest_claim_is_state_version(self):
        events = [{"event": "labeled", "label": {"name": "story:claimed"}, "id": 1},
                  {"event": "labeled", "label": {"name": "story:claimed"}, "id": 2}]
        self.assertEqual(invoke.state_version(events), "2")

    def test_linked_pr_is_unique(self):
        pulls = [{"number": 1, "body": "Story: #7\n"}]
        self.assertEqual(invoke.linked_prs(7, pulls), pulls)
        with self.assertRaisesRegex(invoke.DeliveryError, "multiple"):
            invoke.linked_prs(7, pulls * 2)

    def test_duplicate_link_in_one_pr_fails(self):
        with self.assertRaisesRegex(invoke.DeliveryError, "duplicates"):
            invoke.linked_prs(7, [{"number": 1, "body": "Story: #7\nStory: #7\n"}])


class BoundaryTests(unittest.TestCase):
    def test_private_repository_401_is_a_loud_access_constraint(self):
        denied = invoke.urllib.error.HTTPError("https://api.github.test", 401,
                                               "Unauthorized", {}, None)
        with mock.patch.object(invoke.urllib.request, "urlopen", side_effect=denied):
            with self.assertRaisesRegex(invoke.DeliveryError, "access constraint"):
                invoke.GitHub("o/private", "bad-token").api("")

    def test_delivery_pull_request_is_immediately_reviewable(self):
        client = invoke.GitHub("o/r", "token")
        with mock.patch.object(client, "api", return_value={"number": 9}) as api:
            client.create_pr("title", "branch", "main", "Story: #7")
        self.assertFalse(api.call_args.kwargs["value"]["draft"])

    def test_model_environment_has_no_repository_credentials(self):
        with mock.patch.dict(invoke.os.environ,
                             {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret2",
                              "PATH": "/bin"}, clear=True):
            self.assertEqual(invoke.clean_environment("claude"), {"PATH": "/bin"})

    def test_model_environment_keeps_model_auth_not_github_auth(self):
        with mock.patch.dict(invoke.os.environ,
                             {"GH_TOKEN": "github", "ANTHROPIC_API_KEY": "model"},
                             clear=True):
            self.assertEqual(invoke.clean_environment("claude"),
                             {"ANTHROPIC_API_KEY": "model"})

    def test_timeout_fails_loudly(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], 1)
        with self.assertRaisesRegex(invoke.DeliveryError, "exhausted"):
            invoke.run(["model"], cwd=pathlib.Path("."), timeout=1, runner=timeout)

    def test_nonzero_model_fails_loudly(self):
        def fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 9, "", "denied")
        with self.assertRaisesRegex(invoke.DeliveryError, "denied"):
            invoke.run(["model"], cwd=pathlib.Path("."), timeout=1, runner=fail)

    def test_default_command_is_headless_and_bounded(self):
        with mock.patch.dict(invoke.os.environ, {}, clear=True), \
             mock.patch.object(invoke.pathlib.Path, "read_text", return_value="prompt"):
            command = invoke.model_command("input.json", invoke.Bounds(3.0, 10))
        self.assertIn("-p", command)
        self.assertIn("--max-budget-usd", command)
        self.assertIn("--no-session-persistence", command)

    def test_default_command_grants_write_permission(self):
        """`dontAsk` denies Edit/Write; the Story asks the engine to edit files."""
        with mock.patch.dict(invoke.os.environ, {}, clear=True), \
             mock.patch.object(invoke.pathlib.Path, "read_text", return_value="prompt"):
            command = invoke.model_command("input.json", invoke.Bounds(3.0, 10))
        self.assertEqual("acceptEdits",
                         command[command.index("--permission-mode") + 1])


# A fully populated operator shell, written out rather than derived from the
# declarations, so a new credential must be justified against a real session.
OPERATOR_SESSION = {
    "PATH": "/usr/bin:/bin",
    "LANG": "en_US.UTF-8",
    "LC_ALL": "en_US.UTF-8",
    "TMPDIR": "/tmp/",
    "SHELL": "/bin/zsh",
    "HOME": "/home/operator",
    "USER": "operator",
    "LOGNAME": "operator",
    "ANTHROPIC_API_KEY": "model-key",
    "CLAUDE_CODE_OAUTH_TOKEN": "model-token",
    "GITHUB_TOKEN": "repository-write",
    "GH_TOKEN": "repository-write",
    "FACTORY_WORKER_ORDER": "claude-delivery,codex-delivery",
    "FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH": "python3 bridge.py --engine claude",
}
PLATFORMS = ("darwin", "linux")


class EngineAuthenticationTests(unittest.TestCase):
    """Ask whether each engine can log in, not whether a given name is present."""

    def environment(self, engine, platform):
        return invoke.clean_environment(engine, platform, OPERATOR_SESSION)

    def test_every_engine_can_authenticate_on_every_platform(self):
        for engine in invoke.ENGINE_CREDENTIALS:
            for platform in PLATFORMS:
                with self.subTest(engine=engine, platform=platform):
                    cut_off = invoke.unreachable_credentials(
                        engine, self.environment(engine, platform), platform)
                    self.assertEqual(
                        [], [source.source for source in cut_off],
                        f"{engine} cannot log in on {platform}")

    def test_an_unrecognised_engine_still_gets_every_declared_login(self):
        """A custom FACTORY_DELIVERY_MODEL_CMD could be any of them."""
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                self.assertEqual([], invoke.unreachable_credentials(
                    "some-other-engine", self.environment("some-other-engine",
                                                          platform), platform))

    def test_the_credential_each_engine_needs_is_platform_specific(self):
        """One shared list of names would pass one platform and fail the other."""
        macos = self.environment("claude", "darwin")
        linux = self.environment("claude", "linux")
        self.assertIn("USER", macos)      # keychain lookup, macOS only
        self.assertNotIn("HOME", macos)
        self.assertIn("HOME", linux)      # credential file, Linux only
        self.assertNotIn("USER", linux)

    def test_stripping_a_declared_login_is_reported_as_a_lost_capability(self):
        """The regression this guards: USER absent, so the keychain is unreachable."""
        stripped = {key: value for key, value in
                    self.environment("claude", "darwin").items() if key != "USER"}
        cut_off = invoke.unreachable_credentials("claude", stripped, "darwin")
        self.assertIn("USER", {name for source in cut_off
                               for name in source.variables})

    def test_environment_stays_an_allowlist_for_every_engine(self):
        """A leaked FACTORY_WORKER_* or GITHUB_TOKEN must not reach the engine."""
        for engine in (*invoke.ENGINE_CREDENTIALS, None):
            for platform in PLATFORMS:
                with self.subTest(engine=engine, platform=platform):
                    env = self.environment(engine, platform)
                    self.assertNotIn("GITHUB_TOKEN", env)
                    self.assertNotIn("GH_TOKEN", env)
                    self.assertEqual([], [key for key in env
                                          if key.startswith("FACTORY_")])
                    self.assertNotIn("LOGNAME", env)

    def test_engine_is_read_from_the_command_the_worker_will_run(self):
        self.assertEqual("claude", invoke.engine_name(["claude", "-p", "..."]))
        self.assertEqual("codex", invoke.engine_name(["/opt/bin/codex", "exec"]))
        # Unrecognised, but still an engine that has to log in.
        self.assertEqual("scripted-model.sh",
                         invoke.engine_name(["./scripted-model.sh"]))
        self.assertIsNone(invoke.engine_name([]))

    def test_a_subprocess_that_is_not_an_engine_gets_no_credential(self):
        """The delivered change's own test command has nothing to log in to.

        The regression this guards: it shared `clean_environment()` with the
        model launch, so it silently inherited the operator's `USER` (macOS) or
        `HOME` (Linux) — and would inherit every credential declared later.
        """
        env = invoke.tool_environment(OPERATOR_SESSION)
        self.assertEqual(set(invoke.BASE_ENVIRONMENT), set(env))
        declared = {name for group in invoke.ENGINE_CREDENTIALS.values()
                    for source in group for name in source.variables}
        self.assertEqual(set(), declared & set(env))
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                self.assertEqual(env, invoke.clean_environment(
                    None, platform, OPERATOR_SESSION))

    def test_the_default_command_gets_an_environment_it_can_log_in_with(self):
        with mock.patch.dict(invoke.os.environ, {}, clear=True), \
             mock.patch.object(invoke.pathlib.Path, "read_text", return_value="prompt"):
            command = invoke.model_command("input.json", invoke.Bounds(3.0, 10))
        engine = invoke.engine_name(command)
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                self.assertEqual([], invoke.unreachable_credentials(
                    engine, self.environment(engine, platform), platform))


STORY_BODY = """### Project

#325

### Spend cap

$5 / 60 min

### Scope

factory/agents/worker/invoke.py
"""


class FakeClient:
    """Just enough GitHub for one delivery, with no network."""

    def __init__(self, story=214):
        self.story, self.created = story, None

    def api(self, path, *, method="GET", value=None):
        assert path == "", path
        return {"default_branch": "main"}

    def issue(self, number):
        return {"number": number,
                "body": STORY_BODY if number == self.story else "### Goal\n\nx\n"}

    def pages(self, path):
        if path.endswith("/timeline"):
            return [{"event": "labeled", "label": {"name": "story:claimed"}, "id": 5}]
        return []

    def pull_requests(self):
        return [] if self.created is None else [self.created]

    def create_pr(self, title, head, base, body):
        self.created = {"number": 9, "body": body,
                        "head": {"ref": head, "sha": "deadbeef"}}
        return self.created


class RecordingRunner:
    """Records the environment handed to every subprocess `execute()` launches."""

    def __init__(self, changed, engine_output=""):
        self.changed, self.calls = changed, []
        self.engine_output = engine_output

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs.get("env")))
        out = ""
        if cmd[:3] == ["git", "diff", "--name-only"]:
            out = "\n".join(self.changed)
        elif cmd[:2] == ["git", "rev-parse"]:
            out = "deadbeef\n"
        elif cmd[0] == "claude":
            out = self.engine_output
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def environment_for(self, predicate):
        found = [env for cmd, env in self.calls if predicate(cmd)]
        assert len(found) == 1, found
        return found[0]


class DeliveryHarness(unittest.TestCase):
    """One delivery through `execute()`, with a disposable runtime log.

    The log path is part of the session on purpose: the runtime log must never
    be written to the operator's real one by a test run, and `FACTORY_*` names
    must not reach the engine either — which the allowlist tests below check
    against exactly this session.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = os.path.join(self.dir.name, "runtime.jsonl")
        self.session = dict(OPERATOR_SESSION,
                            FACTORY_RUNTIME_LOG=self.log,
                            FACTORY_RUNTIME_LOG_STDERR="0")

    def deliver(self, engine_output=""):
        runner = RecordingRunner(["factory/agents/worker/invoke.py"],
                                 engine_output=engine_output)
        with mock.patch.dict(invoke.os.environ, self.session, clear=True), \
             mock.patch.object(invoke.pathlib.Path, "read_text",
                               return_value="prompt"):
            invoke.execute("owner/repo", 214, "token", pathlib.Path("."),
                           runner=runner, client=FakeClient())
        return runner

    def records(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class EngineUsageTests(DeliveryHarness):
    """Project #322 criterion 5: what the engine spent has to be captured while
    the story runs, because it cannot be recovered afterwards."""

    def test_the_delivery_engine_usage_is_recorded_against_the_story(self):
        self.deliver(engine_output=json.dumps(
            {"type": "result", "total_cost_usd": 0.37,
             "usage": {"input_tokens": 900, "output_tokens": 120}}))
        usage = [r for r in self.records() if r["event"] == runlog.USAGE_EVENT]
        self.assertEqual(1, len(usage))
        self.assertEqual(214, usage[0]["story"])
        self.assertEqual(325, usage[0]["project"])
        self.assertEqual("claude", usage[0]["engine"])
        self.assertEqual("worker", usage[0]["phase"])
        self.assertTrue(usage[0]["usage_reported"])
        self.assertEqual({"input_tokens": 900, "output_tokens": 120,
                          "total_cost_usd": 0.37}, usage[0]["usage"])

    def test_a_silent_engine_is_recorded_as_having_reported_nothing(self):
        self.deliver(engine_output="delivered.")
        usage = [r for r in self.records() if r["event"] == runlog.USAGE_EVENT]
        self.assertEqual(1, len(usage))
        self.assertFalse(usage[0]["usage_reported"])
        self.assertEqual(runlog.USAGE_UNAVAILABLE, usage[0]["usage"])

    def test_the_default_command_asks_the_engine_to_report_its_usage(self):
        """Without this the engine prints prose and every record is blank."""
        with mock.patch.dict(invoke.os.environ, {}, clear=True), \
             mock.patch.object(invoke.pathlib.Path, "read_text", return_value="prompt"):
            command = invoke.model_command("input.json", invoke.Bounds(3.0, 10))
        self.assertEqual("json", command[command.index("--output-format") + 1])

    def test_a_failed_launch_still_carries_what_it_spent(self):
        """Tokens spent by an engine that crashed were still spent."""
        def fail(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 9, json.dumps({"usage": {"output_tokens": 7}}), "denied")
        with mock.patch.dict(invoke.os.environ, self.session, clear=True):
            with self.assertRaisesRegex(invoke.DeliveryError, "denied"):
                invoke.launch_engine(["claude", "-p", "x"], cwd=pathlib.Path("."),
                                     timeout=1, env={}, story=214, runner=fail)
        usage = [r for r in self.records() if r["event"] == runlog.USAGE_EVENT]
        self.assertEqual([("failed", True, {"output_tokens": 7})],
                         [(r["launch"], r["usage_reported"], r["usage"])
                          for r in usage])

    def test_a_timed_out_launch_is_recorded_too(self):
        def timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd, 1, output=json.dumps({"usage": {"output_tokens": 3}}).encode())
        with mock.patch.dict(invoke.os.environ, self.session, clear=True):
            with self.assertRaisesRegex(invoke.DeliveryError, "exhausted"):
                invoke.launch_engine(["claude", "-p", "x"], cwd=pathlib.Path("."),
                                     timeout=1, env={}, story=214, runner=timeout)
        usage = [r for r in self.records() if r["event"] == runlog.USAGE_EVENT]
        self.assertEqual([{"output_tokens": 3}], [r["usage"] for r in usage])


class LaunchEnvironmentTests(DeliveryHarness):
    """What a real delivery hands each subprocess, at the call sites themselves."""

    def test_the_engine_is_launched_with_a_login_and_no_repository_token(self):
        env = self.deliver().environment_for(lambda cmd: cmd[0] == "claude")
        self.assertEqual([], invoke.unreachable_credentials("claude", env))
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual([], [key for key in env if key.startswith("FACTORY_")])

    def test_the_repo_test_command_is_launched_with_no_credential_at_all(self):
        """The regression: it shared the engine's environment and so its login."""
        env = self.deliver().environment_for(
            lambda cmd: cmd[0].endswith("test_repo.sh"))
        self.assertEqual(invoke.tool_environment(OPERATOR_SESSION), env)
        declared = {name for group in invoke.ENGINE_CREDENTIALS.values()
                    for source in group for name in source.variables}
        self.assertEqual(set(), declared & set(env))


class EngineExplanationTests(unittest.TestCase):
    """#330 — the engine's own account of a failure must survive into the
    error. Twice it did not: the 2026-08-22 authentication refusal was printed
    to stdout and discarded by a 300-character stderr slice, and #332's
    2026-08-23 failures recorded `command failed (1):` with nothing after the
    colon."""

    def failing(self, code=1, stdout="", stderr=""):
        def runner(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, code, stdout, stderr)
        return runner

    def test_a_stdout_only_explanation_reaches_the_error(self):
        """The exact 2026-08-22 case: the refusal was on stdout."""
        message = "Not logged in \u00b7 Please run /login"
        with self.assertRaises(invoke.DeliveryError) as caught:
            invoke.run(["engine"], cwd=pathlib.Path("."), timeout=5,
                       runner=self.failing(stdout=message))
        self.assertIn("Please run /login", str(caught.exception))

    def test_stderr_is_no_longer_cut_to_300_characters(self):
        long_explanation = "x" * 400 + " THE ACTUAL REASON"
        with self.assertRaises(invoke.DeliveryError) as caught:
            invoke.run(["engine"], cwd=pathlib.Path("."), timeout=5,
                       runner=self.failing(stderr=long_explanation))
        self.assertIn("THE ACTUAL REASON", str(caught.exception))

    def test_a_timeout_keeps_the_stderr_the_engine_managed(self):
        """A timeout's stderr was dropped entirely, and for an engine killed
        mid-explanation that was the half worth reading."""
        def timing_out(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5, output=b"partial out",
                                            stderr=b"engine was saying this")
        with self.assertRaises(invoke.DeliveryError) as caught:
            invoke.run(["engine"], cwd=pathlib.Path("."), timeout=5,
                       runner=timing_out)
        self.assertIn("engine was saying this", str(caught.exception))

    def test_main_surfaces_the_engine_output_on_stderr(self):
        """The launcher records the worker's stderr on worker.launch.end; the
        engine's account must be printed there, not swallowed."""
        error = invoke.DeliveryError("boom", "the engine said: cannot write")
        buffer = io.StringIO()
        with mock.patch.object(invoke, "execute", side_effect=error), \
             mock.patch.dict(invoke.os.environ, {"GH_TOKEN": "t"}), \
             mock.patch.object(invoke.sys, "stderr", buffer):
            code = invoke.main(["--repo", "o/r", "--story", "1"])
        self.assertEqual(1, code)
        self.assertIn("the engine said: cannot write", buffer.getvalue())

    def test_credentials_are_redacted_from_the_explanation(self):
        """runlog.tail owns redaction; the error must go through it."""
        secret = "sk-ant-oat01-" + "a" * 40
        with mock.patch.dict(invoke.os.environ,
                             {"CLAUDE_CODE_OAUTH_TOKEN": secret}):
            with self.assertRaises(invoke.DeliveryError) as caught:
                invoke.run(["engine"], cwd=pathlib.Path("."), timeout=5,
                           runner=self.failing(stderr=f"token {secret} leaked"))
        self.assertNotIn(secret, str(caught.exception))



if __name__ == "__main__":
    unittest.main()


class ScopeSeesFilesTests(unittest.TestCase):
    """#358 — `git status --porcelain` collapses a new untracked directory to
    one `dir/` line, which no `dir/sub/**` scope can match; the worker then
    refuses its own in-scope work. Found live by verification fixture #357."""

    def repo(self):
        base = tempfile.mkdtemp(prefix="scope-test-")
        self.addCleanup(__import__("shutil").rmtree, base, True)
        root = pathlib.Path(base)
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty",
                                      "-m", "root"]):
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            *args], cwd=root, check=True, capture_output=True)
        return root

    def test_a_file_in_a_brand_new_nested_directory_is_listed_by_full_path(self):
        root = self.repo()
        target = root / "runs" / "verify" / "product"
        target.mkdir(parents=True)
        (target / "app.py").write_text("x")
        paths = invoke.changed_paths(root, "HEAD")
        self.assertIn("runs/verify/product/app.py", paths)
        self.assertNotIn("runs/", paths)
        self.assertNotIn("runs/verify/", paths)

    def test_out_of_scope_work_in_a_new_directory_is_still_refused_by_file(self):
        """The fix must not blunt the guard: the offending FILE is reported."""
        root = self.repo()
        (root / "forbidden").mkdir()
        (root / "forbidden" / "escape.py").write_text("x")
        paths = invoke.changed_paths(root, "HEAD")
        self.assertIn("forbidden/escape.py", paths)
