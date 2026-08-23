import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invoke


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

    def test_model_environment_has_no_credentials(self):
        with mock.patch.dict(invoke.os.environ,
                             {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret2",
                              "PATH": "/bin"}, clear=True):
            self.assertEqual(invoke.clean_environment(), {"PATH": "/bin"})

    def test_model_environment_keeps_model_auth_not_github_auth(self):
        with mock.patch.dict(invoke.os.environ,
                             {"GH_TOKEN": "github", "ANTHROPIC_API_KEY": "model"},
                             clear=True):
            self.assertEqual(invoke.clean_environment(),
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
        self.assertIsNone(invoke.engine_name(["./scripted-model.sh"]))
        self.assertIsNone(invoke.engine_name([]))

    def test_the_default_command_gets_an_environment_it_can_log_in_with(self):
        with mock.patch.dict(invoke.os.environ, {}, clear=True), \
             mock.patch.object(invoke.pathlib.Path, "read_text", return_value="prompt"):
            command = invoke.model_command("input.json", invoke.Bounds(3.0, 10))
        engine = invoke.engine_name(command)
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                self.assertEqual([], invoke.unreachable_credentials(
                    engine, self.environment(engine, platform), platform))


if __name__ == "__main__":
    unittest.main()
