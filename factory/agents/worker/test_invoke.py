import contextlib
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
            self.assertEqual(invoke.provider_environment("anthropic"), {"PATH": "/bin"})

    def test_model_environment_keeps_model_auth_not_github_auth(self):
        with mock.patch.dict(invoke.os.environ,
                             {"GH_TOKEN": "github", "ANTHROPIC_API_KEY": "model"},
                             clear=True):
            self.assertEqual(invoke.provider_environment("anthropic"),
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

STORY_BODY = """### Project

#325

### Spend cap

$5 / 60 min

### Scope

src/app.py
"""


class FakeClient:
    """Just enough GitHub for one delivery, with no network."""

    def __init__(self, story=214, story_body=STORY_BODY):
        self.story, self.story_body, self.created = story, story_body, None

    def api(self, path, *, method="GET", value=None):
        assert path == "", path
        return {"default_branch": "main"}

    def issue(self, number):
        return {"number": number,
                "body": self.story_body if number == self.story else "### Goal\n\nx\n"}

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
        elif cmd[0] in ("claude", "codex"):
            out = self.engine_output
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def environment_for(self, predicate):
        found = [env for cmd, env in self.calls if predicate(cmd)]
        assert len(found) == 1, found
        return found[0]


class FactorySelfModificationTests(unittest.TestCase):
    def test_worker_refuses_protected_scope_before_engine_launch(self):
        scopes = (
            "factory/agents/worker/invoke.py", "f*/**", "**", "poll.sh",
            "approve-plan.sh", "live-e2e.sh", ".github/**", ".claude/**",
            "AGENTS.md", "CLAUDE.md",
        )
        for scope in scopes:
            with self.subTest(scope=scope):
                body = STORY_BODY.replace("src/app.py", scope)
                runner = RecordingRunner([])
                with self.assertRaisesRegex(
                        invoke.DeliveryError,
                        "FACTORY_SELF_MODIFICATION_FORBIDDEN"):
                    invoke.execute(
                        "owner/repo", 214, "token", pathlib.Path("."),
                        runner=runner,
                        client=FakeClient(story_body=body),
                    )
                self.assertFalse(any(
                    command and command[0] in ("codex", "claude")
                    for command, _ in runner.calls
                ))


class ProductCheckoutTests(unittest.TestCase):
    def test_matching_checkout_is_reused_without_clone(self):
        calls = []
        def runner(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, "git@github.com:owner/product.git\n", "")
        with invoke.checkout_for_repo(
                "owner/product", pathlib.Path("/configured"), runner=runner) as checkout:
            self.assertEqual(pathlib.Path("/configured"), checkout)
        self.assertFalse(any(call[:3] == ["gh", "repo", "clone"] for call in calls))

    def test_mismatched_checkout_clones_requested_product_without_secret_argument(self):
        calls = []
        def runner(cmd, **kwargs):
            calls.append(cmd)
            stdout = "git@github.com:owner/factory.git\n" if cmd[:3] == [
                "git", "remote", "get-url"] else ""
            return subprocess.CompletedProcess(cmd, 0, stdout, "")
        with mock.patch.dict(invoke.os.environ, {"GH_TOKEN": "secret-token"}), \
             invoke.checkout_for_repo(
                 "owner/product", pathlib.Path("/factory"), runner=runner) as checkout:
            self.assertEqual("repository", checkout.name)
        clone = next(call for call in calls if call[:3] == ["gh", "repo", "clone"])
        self.assertEqual("owner/product", clone[3])
        self.assertNotIn("secret-token", " ".join(clone))

    def test_clone_failure_is_named(self):
        def runner(cmd, **kwargs):
            if cmd[:3] == ["git", "remote", "get-url"]:
                return subprocess.CompletedProcess(cmd, 0, "git@github.com:o/factory.git\n", "")
            return subprocess.CompletedProcess(cmd, 1, "", "clone denied")
        with self.assertRaisesRegex(invoke.DeliveryError, "clone denied"):
            with invoke.checkout_for_repo(
                    "o/product", pathlib.Path("/factory"), runner=runner):
                pass


class RepositoryTestCommandTests(unittest.TestCase):
    def test_javascript_product_uses_its_declared_test_script(self):
        with tempfile.TemporaryDirectory() as directory:
            pathlib.Path(directory, "package.json").write_text(
                json.dumps({"scripts": {"test": "node --test test/*.test.js"}}))
            with mock.patch.dict(invoke.os.environ, {}, clear=True):
                self.assertEqual(["npm", "test"],
                                 invoke.repository_test_command(pathlib.Path(directory)))

    def test_factory_checkout_uses_its_own_test_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            script = pathlib.Path(directory, "factory/agents/worker/test_repo.sh")
            script.parent.mkdir(parents=True)
            script.write_text("#!/bin/sh\n")
            with mock.patch.dict(invoke.os.environ, {}, clear=True):
                self.assertEqual([str(script)],
                                 invoke.repository_test_command(pathlib.Path(directory)))

    def test_explicit_product_command_wins(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                invoke.os.environ, {"FACTORY_DELIVERY_TEST_CMD": "make check"},
                clear=True):
            self.assertEqual(["make", "check"],
                             invoke.repository_test_command(pathlib.Path(directory)))

    def test_unknown_product_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                invoke.os.environ, {}, clear=True):
            with self.assertRaisesRegex(invoke.DeliveryError, "no supported test"):
                invoke.repository_test_command(pathlib.Path(directory))


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
             mock.patch.object(
                 invoke, "checkout_for_repo",
                 return_value=contextlib.nullcontext(pathlib.Path("."))), \
             mock.patch.dict(invoke.os.environ, {"GH_TOKEN": "t"}), \
             mock.patch.object(invoke.sys, "stderr", buffer):
            code = invoke.main(["--repo", "o/r", "--story", "1"])
        self.assertEqual(1, code)
        self.assertIn("the engine said: cannot write", buffer.getvalue())

    def test_failure_log_has_non_sensitive_platform_diagnostics(self):
        error = invoke.DeliveryError("cannot create worktree")
        with tempfile.TemporaryDirectory() as directory:
            checkout = pathlib.Path(directory)
            (checkout / ".git" / "worktrees").mkdir(parents=True)
            with mock.patch.object(invoke, "execute", side_effect=error), \
                 mock.patch.dict(invoke.os.environ, {"GH_TOKEN": "secret"}), \
                 mock.patch.object(invoke.obs, "operational_log") as log:
                code = invoke.main(["--repo", "o/r", "--story", "1",
                                    "--checkout", directory])
        self.assertEqual(1, code)
        diagnostics = log.call_args.kwargs["platform_diagnostics"]
        self.assertTrue(diagnostics["git_dir"]["writable_by_access_check"])
        self.assertEqual("0o755", diagnostics["git_worktrees_dir"]["mode"])
        self.assertNotIn("secret", json.dumps(diagnostics))

    def test_diagnostics_cannot_hide_the_primary_failure(self):
        with mock.patch.object(pathlib.Path, "read_text",
                               side_effect=PermissionError("sandbox denied read")), \
             mock.patch.object(pathlib.Path, "is_file", return_value=True):
            diagnostics = invoke.platform_diagnostics(pathlib.Path("."))
        self.assertIn("PermissionError", diagnostics["git_dir_resolution_error"])
        self.assertIn("git_dir", diagnostics)

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
