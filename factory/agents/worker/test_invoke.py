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
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invoke
import runlog


class ParsingTests(unittest.TestCase):
    def test_recovery_timestamp_requires_canonical_clock_shape(self):
        self.assertTrue(invoke.valid_utc_timestamp("2026-09-04T00:00:00Z"))
        self.assertTrue(invoke.valid_utc_timestamp(
            "2026-09-04T00:00:00.123456Z"))
        for value in ("2026-09-04T00Z", "20260904T000000Z",
                      "2026-W36-4T00:00:00Z"):
            with self.subTest(value=value):
                self.assertFalse(invoke.valid_utc_timestamp(value))

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

    def verification_body(self, **changes):
        record = {
            "type": "automated", "scope": "tests/check.py",
            "executor": "tests/check.py", "executor_source": "create",
            "action": "python3 tests/check.py", "expected": "exit zero",
            "failure": "exit nonzero",
        }
        record.update(changes)
        return ("### Acceptance notes\n\n- criterion || VERIFY "
                + json.dumps(record) + "\n")

    def test_trusted_wrapper_executes_every_automated_verification(self):
        body = self.verification_body() + (
            "- second || VERIFY " + json.dumps({
                "type": "automated", "scope": "tests/other.test.js",
                "executor": "tests/other.test.js", "executor_source": "existing",
                "action": "node --test tests/other.test.js", "expected": "exit zero",
                "failure": "exit nonzero",
            }) + "\n")
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs.get("env")))
            return subprocess.CompletedProcess(command, 0, "", "")
        with mock.patch.dict(os.environ, {"GH_TOKEN": "secret"}):
            invoke.run_acceptance_verifications(
                body, cwd=pathlib.Path("/product"), timeout=30, trace_id="trace",
                repo="owner/product", story=1, project=2, runner=runner)
        self.assertEqual([
            ["python3", "tests/check.py"],
            ["node", "--test", "tests/other.test.js"],
        ], [command for command, _ in calls])
        self.assertTrue(all("GH_TOKEN" not in environment for _, environment in calls))

    def test_failed_acceptance_verification_stops_delivery(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "criterion failed")
        with self.assertRaisesRegex(invoke.DeliveryError, "criterion failed"):
            invoke.run_acceptance_verifications(
                self.verification_body(), cwd=pathlib.Path("/product"), timeout=30,
                trace_id="trace", repo="owner/product", story=1, project=2,
                runner=runner)

    def test_passive_command_and_non_test_executor_are_rejected(self):
        for changes, message in (
            ({"action": "echo tests/check.py"}, "does not invoke"),
            ({"action": "python3 -c pass tests/check.py"}, "does not invoke"),
            ({"action": "npm --help tests/check.py"}, "does not invoke"),
            ({"scope": "README.md", "executor": "README.md",
              "action": "cat README.md"}, "not a test or workflow"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                    invoke.DeliveryError, message):
                invoke.acceptance_verification_commands(
                    self.verification_body(**changes))

    def test_linked_pr_is_unique(self):
        pulls = [{"number": 1, "body": "Story: #7\n"}]
        self.assertEqual(invoke.linked_prs(7, pulls), pulls)
        with self.assertRaisesRegex(invoke.DeliveryError, "multiple"):
            invoke.linked_prs(7, pulls * 2)

    def test_duplicate_link_in_one_pr_fails(self):
        with self.assertRaisesRegex(invoke.DeliveryError, "duplicates"):
            invoke.linked_prs(7, [{"number": 1, "body": "Story: #7\nStory: #7\n"}])


class BoundaryTests(unittest.TestCase):
    def test_default_capacity_state_is_factory_owned_not_product_owned(self):
        self.assertEqual(invoke.default_state_path(invoke.ROOT, {}),
                         invoke.capacity_state_path({}))
        self.assertEqual(pathlib.Path("/tmp/shared.sqlite"),
                         invoke.capacity_state_path(
                             {"FACTORY_CAPACITY_STATE": "/tmp/shared.sqlite"}))

    def test_cli_refuses_to_bypass_capacity_admission(self):
        buffer = io.StringIO()
        with mock.patch.dict(invoke.os.environ, {"GH_TOKEN": "t"}), \
             mock.patch.object(invoke.sys, "stderr", buffer):
            code = invoke.main(["--repo", "o/r", "--story", "1"])
        self.assertEqual(2, code)
        self.assertIn("admission reservation", buffer.getvalue())

    def test_worker_start_is_durable_and_binds_the_reservation(self):
        class Client:
            def __init__(self): self.comments = []
            def api(self, path, *, method="GET", value=None):
                self.comments.append(value)
                return value
        client = Client()
        invoke.publish_worker_start(
            client, repo="Owner/Repo", story=20, version="claim-1",
            reservation="a" * 32, invocation="delivery:owner/repo:20:1")
        body = client.comments[0]["body"]
        self.assertIn(invoke.START_MARKER, body)
        self.assertIn('"reservation_id": "' + "a" * 32 + '"', body)
        self.assertIn('"state_version": "claim-1"', body)

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

### Attempt

1

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


class CorrectionInputTests(unittest.TestCase):
    HEAD = "9ef7b45c2f7f83ce6e7b0de2be4638229babf132"

    class Client:
        def __init__(self, story_comments=(), pull_comments=()):
            self.story_comments = list(story_comments)
            self.pull_comments = list(pull_comments)

        def pages(self, path):
            if path == "/issues/214/comments":
                return self.story_comments
            if path == "/issues/74/comments":
                return self.pull_comments
            if path == "/issues?state=all":
                return []
            raise AssertionError(path)

    @staticmethod
    def comment(identifier, created, body):
        return {"id": identifier, "created_at": created, "body": body,
                "author_association": "OWNER"}

    def test_fresh_worker_input_has_empty_correction_packet(self):
        value = invoke.build_input(
            self.Client(), {"number": 214, "body": STORY_BODY},
            {"number": 325, "body": "### Goal\n\nx\n"}, repo="owner/repo")
        self.assertFalse(value["correction_context"]["retry"])
        self.assertEqual([], value["correction_context"]["records"])
        self.assertNotIn("review_findings", value)

    def test_retry_reads_story_and_linked_pr_correction_records(self):
        finding = self.comment(
            1, "2026-08-27T02:31:53Z",
            "## Review findings\n\nChrome is skippable.\n\n"
            f"<!-- review-outcome:74:{self.HEAD}:findings -->")
        request = self.comment(
            2, "2026-08-27T02:54:11Z",
            "## Request changes\n\nMahesh requested changes in the active session; "
            "I transcribed his decision here.\n\n" +
            invoke.correction_context.marker(
                kind="request-changes", story=214, pull_request=74, head=self.HEAD))
        value = invoke.build_input(
            self.Client([finding], [request]),
            {"number": 214, "body": STORY_BODY},
            {"number": 325, "body": "### Goal\n\nx\n"},
            repo="owner/repo",
            pull_request={"number": 74, "head": {"sha": self.HEAD}})
        packet = value["correction_context"]
        self.assertTrue(packet["retry"])
        self.assertEqual(["review-findings", "request-changes"],
                         [item["kind"] for item in packet["records"]])
        self.assertEqual(["1", "2"],
                         [item["comment_id"] for item in packet["records"]])


class RecordingRunner:
    """Records the environment handed to every subprocess `execute()` launches."""

    def __init__(self, changed, engine_output=""):
        self.changed, self.calls = changed, []
        self.engine_output = engine_output

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs.get("env")))
        out = ""
        code = 0
        if cmd[:3] == ["git", "diff", "--name-status"]:
            out = "".join(f"M\0{path}\0" for path in self.changed)
        elif cmd[:3] == ["git", "status", "--porcelain"]:
            out = "".join(f" M {path}\0" for path in self.changed)
        elif cmd[:4] == ["git", "diff", "--binary", "--cached"]:
            out = "diff --git a/src/app.py b/src/app.py\n"
        elif cmd[:4] == ["git", "apply", "--numstat", "-z"]:
            out = "".join(f"1\t0\t{path}\0" for path in self.changed)
        elif cmd[:2] == ["git", "rev-parse"]:
            out = "d" * 40 + "\n"
        elif cmd[0] in ("claude", "codex"):
            out = self.engine_output
        return subprocess.CompletedProcess(cmd, code, out, "")

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
                        "owner/ai-software-factory", 214, "token", pathlib.Path("."),
                        runner=runner,
                        client=FakeClient(story_body=body),
                    )
                self.assertFalse(any(
                    command and command[0] in ("codex", "claude")
                    for command, _ in runner.calls
                ))

    def test_product_repository_workflow_is_not_factory_control_scope(self):
        self.assertEqual(
            invoke.protected_control_scope(
                "owner/product", [".github/workflows/tests.yml"]),
            [],
        )

    def test_factory_repository_workflow_remains_control_scope(self):
        self.assertEqual(
            invoke.protected_control_scope(
                "owner/ai-software-factory", [".github/workflows/tests.yml"]),
            [".github/workflows/tests.yml"],
        )


class FailedWorkCheckpointTests(unittest.TestCase):
    BASE = "a" * 40
    WORKER = {"task": "delivery:owner/repo:214:5", "provider": "openai",
              "model": "gpt-test", "invocation_id": "invoke-1"}

    def test_patch_path_reader_includes_both_sides_of_a_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            patch = pathlib.Path(directory, "rename.patch")
            patch.write_text(
                "diff --git a/old.txt b/new.txt\n"
                "similarity index 100%\n"
                "rename from old.txt\n"
                "rename to new.txt\n")
            self.assertEqual(
                ["new.txt", "old.txt"], invoke.recovery_patch_paths(patch))

    def test_patch_path_reader_decodes_utf8_octal_rename_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            patch = pathlib.Path(directory, "rename.patch")
            patch.write_text(
                'diff --git "a/\\303\\251.txt" "b/\\303\\251-new.txt"\n'
                "similarity index 100%\n"
                'rename from "\\303\\251.txt"\n'
                'rename to "\\303\\251-new.txt"\n')
            self.assertEqual(
                ["é-new.txt", "é.txt"], invoke.recovery_patch_paths(patch))

    def test_in_scope_changes_are_committed_to_a_stable_local_recovery_ref(self):
        runner = RecordingRunner(["src/app.py"])
        with tempfile.TemporaryDirectory() as recovery:
            with mock.patch.dict(invoke.os.environ,
                                 {"FACTORY_RECOVERY_DIR": recovery}):
                ref = invoke.checkpoint_failed_work(
                    pathlib.Path("/worktree"), pathlib.Path("/checkout"),
                    repo="owner/repo", story_number=214, default="main",
                    base_ref="origin/main", base_commit=self.BASE,
                    scope=["src/app.py"], mutation_state="post-mutation",
                    terminal_outcome="started-mid-work-failed",
                    originating_worker=self.WORKER, runner=runner)
                manifest = json.loads(
                    invoke.recovery_manifest(pathlib.Path(ref)).read_text())
                self.assertEqual(["src/app.py"], manifest["recovered_paths"])
                self.assertEqual("started-mid-work-failed",
                                 manifest["previous_terminal_outcome"])
                self.assertEqual(invoke.RECOVERY_TRUST, manifest["trust"])
                self.assertEqual(self.BASE, manifest["base_commit"])
                self.assertEqual(self.WORKER, manifest["originating_worker"])
                self.assertTrue(manifest["recovered_at"].endswith("Z"))
        self.assertTrue(ref.endswith("owner--repo/story-214.patch"))
        commands = [command for command, _ in runner.calls]
        self.assertIn(["git", "add", "--", "src/app.py"], commands)

    def test_out_of_scope_changes_are_not_checkpointed(self):
        runner = RecordingRunner(["secrets.txt"])
        ref = invoke.checkpoint_failed_work(
            pathlib.Path("/worktree"), pathlib.Path("/checkout"), repo="owner/repo",
            story_number=214, default="main", scope=["src/app.py"],
            base_ref="origin/main", base_commit=self.BASE,
            mutation_state="post-mutation",
            terminal_outcome="started-mid-work-failed",
            originating_worker=self.WORKER, runner=runner)
        self.assertEqual("", ref)
        self.assertFalse(any(command[:2] == ["git", "add"]
                             for command, _ in runner.calls))

    def test_restore_returns_explicit_untrusted_worker_context(self):
        runner = RecordingRunner(["src/app.py"])
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            ref = invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner)
            value = invoke.restore_failed_work(
                pathlib.Path(ref), pathlib.Path("/new"), repo="owner/repo",
                story_number=214, base_commit=self.BASE,
                scope=["src/app.py"], runner=runner)
        self.assertTrue(value.pop("recovered_at").endswith("Z"))
        self.assertEqual({
            "present": True,
            "trust": "untrusted-partial-work-from-failed-worker",
            "recovered_paths": ["src/app.py"],
            "previous_mutation_state": "post-mutation",
            "previous_terminal_outcome": "started-mid-work-failed",
            "base_commit": self.BASE,
            "originating_worker": self.WORKER,
        }, value)

    def test_restore_rejects_a_patch_that_does_not_match_its_manifest(self):
        runner = RecordingRunner(["src/app.py"])
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            ref = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            ref.write_text("altered patch")
            with self.assertRaisesRegex(invoke.DeliveryError, "digest"):
                invoke.restore_failed_work(
                    ref, pathlib.Path("/new"), repo="owner/repo",
                    story_number=214, base_commit=self.BASE,
                    scope=["src/app.py"], runner=runner)
        self.assertFalse(any(command[:3] == ["git", "apply", "--index"]
                             for command, _ in runner.calls))

    def test_wrong_story_base_or_scope_blocks_patch_application(self):
        for override, message in (
            ({"story_number": 999}, "identity"),
            ({"scope": ["tests/**"]}, "scope"),
        ):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as recovery, \
                    mock.patch.dict(invoke.os.environ,
                                    {"FACTORY_RECOVERY_DIR": recovery}):
                runner = RecordingRunner(["src/app.py"])
                ref = pathlib.Path(invoke.checkpoint_failed_work(
                    pathlib.Path("/old"), pathlib.Path("/checkout"),
                    repo="owner/repo", story_number=214, default="main",
                    base_ref="origin/main", base_commit=self.BASE,
                    scope=["src/app.py"], mutation_state="post-mutation",
                    terminal_outcome="started-mid-work-failed",
                    originating_worker=self.WORKER, runner=runner))
                arguments = {"repo": "owner/repo", "story_number": 214,
                             "base_commit": self.BASE, "scope": ["src/app.py"]}
                arguments.update(override)
                runner.calls.clear()
                with self.assertRaisesRegex(invoke.DeliveryError, message):
                    invoke.restore_failed_work(
                        ref, pathlib.Path("/new"), runner=runner, **arguments)
                self.assertFalse(any(command[:3] == ["git", "apply", "--index"]
                                     for command, _ in runner.calls))

    def test_default_branch_advance_restores_with_three_way_application(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            ref = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            runner.calls.clear()
            value = invoke.restore_failed_work(
                ref, pathlib.Path("/new"), repo="owner/repo",
                story_number=214, base_commit="b" * 40,
                scope=["src/app.py"], runner=runner)
            commands = [command for command, _ in runner.calls]
            self.assertIn(
                ["git", "merge-base", "--is-ancestor", self.BASE, "b" * 40],
                commands)
            self.assertIn(
                ["git", "apply", "--3way", "--index", str(ref)], commands)
            self.assertEqual(self.BASE, value["base_commit"])

    def test_manifest_paths_must_match_the_applied_patch(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            ref = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            runner.changed = ["src/other.py"]
            with self.assertRaisesRegex(invoke.DeliveryError, "do not match"):
                invoke.restore_failed_work(
                    ref, pathlib.Path("/new"), repo="owner/repo",
                    story_number=214, base_commit=self.BASE,
                    scope=["src/**"], runner=runner)

    def test_rejected_restore_does_not_rewrite_invalid_checkpoint(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            base = "d" * 40
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=base,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            manifest = invoke.recovery_manifest(patch)
            original = manifest.read_bytes()
            runner.changed = ["src/other.py"]
            with self.assertRaisesRegex(invoke.RecoveryError, "do not match"):
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=FakeClient())
            self.assertEqual(original, manifest.read_bytes())
            self.assertFalse(any(command and command[0] in ("codex", "claude")
                                 for command, _ in runner.calls))

    def test_success_cleanup_removes_patch_and_manifest_together(self):
        with tempfile.TemporaryDirectory() as directory:
            patch = pathlib.Path(directory, "story-214.patch")
            manifest = invoke.recovery_manifest(patch)
            patch.write_text("patch")
            manifest.write_text("{}")
            invoke.remove_recovery(patch)
            self.assertFalse(patch.exists())
            self.assertFalse(manifest.exists())

    def test_interrupted_cleanup_is_reconciled_without_an_orphan(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": directory}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_pushed(patch, "b" * 40)
            manifest = invoke.recovery_manifest(patch)
            patch_tombstone = patch.with_name(patch.name + ".deleting")
            patch.replace(patch_tombstone)
            self.assertFalse(invoke.recovery_available(patch, runner=runner))
            self.assertFalse(patch.exists())
            self.assertFalse(manifest.exists())
            self.assertFalse(patch_tombstone.exists())

    def test_cleanup_resumes_after_patch_tombstone_was_deleted(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": directory}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_pushed(patch, "b" * 40)
            manifest = invoke.recovery_manifest(patch)
            patch_tombstone = patch.with_name(patch.name + ".deleting")
            manifest_tombstone = manifest.with_name(manifest.name + ".deleting")
            patch.replace(patch_tombstone)
            manifest.replace(manifest_tombstone)
            patch_tombstone.unlink()
            self.assertFalse(invoke.recovery_available(patch, runner=runner))
            self.assertFalse(manifest_tombstone.exists())

    def test_interrupted_cleanup_preserves_invalid_worker_provenance(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": directory}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_pushed(patch, "b" * 40)
            manifest = invoke.recovery_manifest(patch)
            patch_tombstone = patch.with_name(patch.name + ".deleting")
            patch.replace(patch_tombstone)
            value = json.loads(manifest.read_text())
            value["originating_worker"]["model"] = ["not", "a", "model"]
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "worker identity"):
                invoke.recovery_available(
                    patch, repo="owner/repo", story_number=214,
                    scope=["src/app.py"], runner=runner)
            self.assertTrue(patch_tombstone.exists())
            self.assertTrue(manifest.exists())

    def test_recovered_gitlink_directory_uses_staged_state(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = pathlib.Path(directory)
            (worktree / "vendor/lib").mkdir(parents=True)
            runner = RecordingRunner(["vendor/lib"])
            state = invoke.recovered_work_state(
                worktree, ["vendor/lib"], base_commit=self.BASE, runner=runner)
            self.assertEqual(64, len(state))
            self.assertTrue(any(command[:4] ==
                                ["git", "diff", "--binary", "--cached"]
                                for command, _ in runner.calls))

    def test_restore_checks_out_the_staged_gitlink_before_validation(self):
        class GitlinkRunner(RecordingRunner):
            def __call__(self, cmd, **kwargs):
                result = super().__call__(cmd, **kwargs)
                if cmd[:3] == ["git", "ls-files", "--stage"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, "160000 " + "c" * 40 + " 0\tvendor/lib\n", "")
                return result

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = GitlinkRunner(["vendor/lib"])
            ref = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["vendor/lib"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.restore_failed_work(
                ref, pathlib.Path("/new"), repo="owner/repo", story_number=214,
                base_commit=self.BASE, scope=["vendor/lib"], runner=runner)
        self.assertIn(
            ["git", "submodule", "update", "--init", "--checkout", "--",
             "vendor/lib"],
            [command for command, _ in runner.calls])

    def test_restore_removes_a_gitlink_deleted_from_the_index(self):
        class DeletedGitlinkRunner(RecordingRunner):
            def __call__(self, cmd, **kwargs):
                result = super().__call__(cmd, **kwargs)
                if cmd[:2] == ["git", "ls-tree"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, "160000 commit " + "c" * 40 +
                        "\tvendor/lib\n", "")
                if cmd[:3] == ["git", "ls-files", "--stage"]:
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return result

        with tempfile.TemporaryDirectory() as recovery, \
             tempfile.TemporaryDirectory() as target, \
             mock.patch.dict(invoke.os.environ,
                             {"FACTORY_RECOVERY_DIR": recovery}):
            runner = DeletedGitlinkRunner(["vendor/lib"])
            ref = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["vendor/lib"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            worktree = pathlib.Path(target)
            (worktree / "vendor/lib").mkdir(parents=True)
            (worktree / "vendor/lib/old").write_text("old checkout")
            invoke.restore_failed_work(
                ref, worktree, repo="owner/repo", story_number=214,
                base_commit=self.BASE, scope=["vendor/lib"], runner=runner)
            self.assertFalse((worktree / "vendor/lib").exists())

    def test_gitlink_replacement_files_are_preserved_and_fail_closed(self):
        class ReplacementRunner(RecordingRunner):
            def __call__(self, cmd, **kwargs):
                result = super().__call__(cmd, **kwargs)
                if cmd[:2] == ["git", "ls-tree"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, "160000 commit " + "c" * 40 +
                        "\tvendor/lib\n", "")
                if cmd[:3] == ["git", "ls-files", "--stage"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, "100644 " + "e" * 40 +
                        " 0\tvendor/lib/new.py\n", "")
                return result

        with tempfile.TemporaryDirectory() as target:
            worktree = pathlib.Path(target)
            replacement = worktree / "vendor/lib/new.py"
            replacement.parent.mkdir(parents=True)
            replacement.write_text("replacement")
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "explicit cleanup"):
                invoke.checkout_recovered_gitlinks(
                    worktree, ["vendor/lib"], base_commit=self.BASE,
                    runner=ReplacementRunner(["vendor/lib"]))
            self.assertEqual("replacement", replacement.read_text())

    def test_post_apply_gitlink_failure_preserves_original_manifest(self):
        class GitlinkFailureRunner(RecordingRunner):
            def __call__(self, cmd, **kwargs):
                result = super().__call__(cmd, **kwargs)
                if cmd[:3] == ["git", "ls-files", "--stage"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, "160000 " + "c" * 40 + " 0\tvendor/lib\n", "")
                if cmd[:3] == ["git", "submodule", "update"]:
                    return subprocess.CompletedProcess(
                        cmd, 1, "", "submodule unavailable")
                return result

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = GitlinkFailureRunner(["vendor/lib"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit="d" * 40,
                scope=["vendor/lib"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            manifest = invoke.recovery_manifest(patch)
            original = manifest.read_bytes()
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "application failed") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=FakeClient(
                        story_body=STORY_BODY.replace("src/app.py", "vendor/lib")))
            self.assertEqual(original, manifest.read_bytes())
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertFalse(any(command and command[0] in ("codex", "claude")
                                 for command, _ in runner.calls))

    def test_invalid_recovery_blocks_execute_before_engine_launch(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            patch = invoke.recovery_patch("owner/repo", 214)
            patch.parent.mkdir(parents=True)
            patch.write_text("orphan")
            runner = RecordingRunner([])
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "incomplete") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=FakeClient())
            self.assertEqual("post-mutation", caught.exception.mutation_state)
            self.assertEqual("recovery-invalid",
                             caught.exception.terminal_outcome)
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertFalse(any(command and command[0] in ("codex", "claude")
                                 for command, _ in runner.calls))

    def test_input_preflight_failure_preserves_recovery_accounting(self):
        class InputFailureClient(FakeClient):
            def pages(self, path):
                if path.endswith("/timeline"):
                    return super().pages(path)
                if path == "/issues/214/comments":
                    raise OSError("GitHub input read failed")
                return super().pages(path)

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            with self.assertRaisesRegex(OSError, "input read failed") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=InputFailureClient())
            self.assertEqual("post-mutation", caught.exception.mutation_state)
            self.assertEqual("recovery-input-preflight-failed",
                             caught.exception.terminal_outcome)
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertTrue(patch.exists())
            self.assertFalse(any(command and command[0] in ("codex", "claude")
                                 for command, _ in runner.calls))

    def test_non_object_manifest_reports_prior_mutation_and_reference(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            patch = invoke.recovery_patch("owner/repo", 214)
            patch.parent.mkdir(parents=True)
            patch.write_text("patch")
            invoke.recovery_manifest(patch).write_text("[]")
            runner = RecordingRunner([])
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "not an object") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=FakeClient())
            self.assertEqual("post-mutation", caught.exception.mutation_state)
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertFalse(any(command and command[0] in ("codex", "claude")
                                 for command, _ in runner.calls))

    def test_non_string_delivered_head_is_a_recovery_error(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit="d" * 40,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            manifest = invoke.recovery_manifest(patch)
            value = json.loads(manifest.read_text())
            value["delivered_head"] = ["not", "a", "commit"]
            value["delivery_verified_at"] = "2026-09-04T00:00:00Z"
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "provenance") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=FakeClient())
            self.assertEqual("post-mutation", caught.exception.mutation_state)
            self.assertEqual(str(patch), caught.exception.recovery_ref)

    def test_non_string_pending_head_fails_before_engine_relaunch(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit="d" * 40,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            manifest = invoke.recovery_manifest(patch)
            value = json.loads(manifest.read_text())
            value["pending_head"] = ["not", "a", "commit"]
            value["push_prepared_at"] = "2026-09-04T00:00:00Z"
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "pending") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=FakeClient())
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertFalse(any(command and command[0] in ("codex", "claude")
                                 for command, _ in runner.calls))

    def test_date_only_pending_timestamp_fails_before_engine_relaunch(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit="d" * 40,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            manifest = invoke.recovery_manifest(patch)
            value = json.loads(manifest.read_text())
            value["pending_head"] = "e" * 40
            value["push_prepared_at"] = "2026-09-04Z"
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "pending") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=FakeClient())
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertFalse(any(command and command[0] in ("codex", "claude")
                                 for command, _ in runner.calls))

    def test_date_only_recovered_timestamp_fails_before_engine_relaunch(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit="d" * 40,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            manifest = invoke.recovery_manifest(patch)
            value = json.loads(manifest.read_text())
            value["recovered_at"] = "2026-09-04Z"
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "timestamp") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=FakeClient())
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertFalse(any(command and command[0] in ("codex", "claude")
                                 for command, _ in runner.calls))

    def test_date_only_delivery_timestamp_cannot_finalize_recovery(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit="d" * 40,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_pushed(patch, "e" * 40)
            manifest = invoke.recovery_manifest(patch)
            value = json.loads(manifest.read_text())
            value["delivery_verified_at"] = "2026-09-04Z"
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "delivery_verified_at") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=FakeClient())
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertTrue(patch.exists())

    def test_pushed_head_drift_reports_recovery_reference(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_pushed(patch, "b" * 40)
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "does not match") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=FakeClient())
            self.assertEqual(str(patch), caught.exception.recovery_ref)

    def test_durable_pr_replay_rejects_recovery_head_drift(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_pushed(patch, "b" * 40)
            client = FakeClient()
            client.created = {
                "number": 9,
                "body": "Story: #214\n\n" + invoke.marker(214, "5") + "\n",
                "head": {"ref": "story/214-delivery", "sha": "c" * 40},
            }
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "does not match") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=client)
            self.assertEqual("post-mutation", caught.exception.mutation_state)
            self.assertEqual(str(patch), caught.exception.recovery_ref)

    def test_durable_pr_replay_revalidates_recovery_scope_before_cleanup(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["other.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["other.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_pushed(patch, "c" * 40)
            client = FakeClient()
            client.created = {
                "number": 9,
                "body": "Story: #214\n\n" + invoke.marker(214, "5") + "\n",
                "head": {"ref": "story/214-delivery", "sha": "c" * 40},
            }
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "provenance") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=client)
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertTrue(patch.exists())

    def test_durable_pr_replay_preserves_invalid_worker_provenance(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_pushed(patch, "c" * 40)
            manifest = invoke.recovery_manifest(patch)
            value = json.loads(manifest.read_text())
            value["originating_worker"]["model"] = ["not", "a", "model"]
            manifest.write_text(json.dumps(value))
            client = FakeClient()
            client.created = {
                "number": 9,
                "body": "Story: #214\n\n" + invoke.marker(214, "5") + "\n",
                "head": {"ref": "story/214-delivery", "sha": "c" * 40},
            }
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "worker identity") as caught:
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=client)
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertTrue(patch.exists())

    def test_durable_pr_replay_binds_worker_task_to_current_story(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_pushed(patch, "c" * 40)
            manifest = invoke.recovery_manifest(patch)
            value = json.loads(manifest.read_text())
            value["originating_worker"]["task"] = "delivery:owner/repo:999:1"
            manifest.write_text(json.dumps(value))
            client = FakeClient()
            client.created = {
                "number": 9,
                "body": "Story: #214\n\n" + invoke.marker(214, "5") + "\n",
                "head": {"ref": "story/214-delivery", "sha": "c" * 40},
            }
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "worker identity"):
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=client)
            self.assertTrue(patch.exists())

    def test_durable_pr_replay_compares_manifest_paths_with_patch(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/**"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_pushed(patch, "c" * 40)
            manifest = invoke.recovery_manifest(patch)
            value = json.loads(manifest.read_text())
            value["recovered_paths"] = ["src/other.py"]
            manifest.write_text(json.dumps(value))
            client = FakeClient(story_body=STORY_BODY.replace(
                "src/app.py", "src/**"))
            client.created = {
                "number": 9,
                "body": "Story: #214\n\n" + invoke.marker(214, "5") + "\n",
                "head": {"ref": "story/214-delivery", "sha": "c" * 40},
            }
            with self.assertRaisesRegex(invoke.RecoveryError,
                                        "provenance"):
                invoke.execute(
                    "owner/repo", 214, "token", pathlib.Path("/checkout"),
                    runner=runner, client=client)
            self.assertTrue(patch.exists())

    def test_pending_push_is_reconciled_without_relaunch(self):
        class RemoteRunner(RecordingRunner):
            def __call__(self, cmd, **kwargs):
                result = super().__call__(cmd, **kwargs)
                if cmd[:3] == ["git", "ls-remote", "--heads"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, "d" * 40 + "\trefs/heads/story/214-delivery\n", "")
                return result

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RemoteRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_push_pending(patch, "d" * 40)
            result = invoke.execute(
                "owner/repo", 214, "token", pathlib.Path("/checkout"),
                runner=runner, client=FakeClient())
            self.assertEqual("d" * 40, result.head)
            self.assertFalse(patch.exists())
            self.assertFalse(any(command and command[0] in ("codex", "claude")
                                 for command, _ in runner.calls))

    def test_pending_promotion_write_failure_keeps_recovery_accounting(self):
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            invoke.mark_recovery_push_pending(patch, "d" * 40)
            with mock.patch.object(
                    invoke, "mark_recovery_pushed",
                    side_effect=OSError("manifest replace failed")):
                with self.assertRaisesRegex(
                        invoke.RecoveryError,
                        "promotion could not be recorded") as caught:
                    invoke.execute(
                        "owner/repo", 214, "token", pathlib.Path("/checkout"),
                        runner=runner, client=FakeClient())
            self.assertEqual("post-mutation", caught.exception.mutation_state)
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertTrue(patch.exists())
            self.assertFalse(any(command and command[0] in ("codex", "claude")
                                 for command, _ in runner.calls))

    def test_discarded_recovery_still_reports_original_reference(self):
        class CommitFailureRunner(RecordingRunner):
            def __call__(self, cmd, **kwargs):
                result = super().__call__(cmd, **kwargs)
                if cmd[:2] == ["git", "commit"]:
                    return subprocess.CompletedProcess(cmd, 1, "", "nothing to commit")
                return result

        class State:
            health = None
            @staticmethod
            def models_for_task_prefix(*_args, **_kwargs): return ()

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = CommitFailureRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit="d" * 40,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            result = SimpleNamespace(
                outcome="success", output="done", terminal_outcome="completed",
                attempts=({"model": "gpt-test", "provider": "openai",
                           "invocation_id": "next", "reservation_id": None,
                           "mutation_state": "post-mutation"},))
            executor = mock.Mock()
            executor.execute.return_value = result
            with mock.patch.object(invoke, "CapacityExecutor",
                                   return_value=executor), \
                 mock.patch.object(invoke, "cli_adapter", return_value=mock.Mock()), \
                 mock.patch.object(invoke, "repository_test_command",
                                   return_value=["python3", "-m", "unittest"]), \
                 mock.patch.object(invoke, "recovered_work_state",
                                   side_effect=["restored", "discarded"]), \
                 mock.patch.object(invoke, "checkpoint_failed_work",
                                   return_value=""):
                with self.assertRaisesRegex(invoke.DeliveryError,
                                            "nothing to commit") as caught:
                    invoke.execute(
                        "owner/repo", 214, "token", pathlib.Path("/checkout"),
                        runner=runner, client=FakeClient(), state=State(),
                        registry=[SimpleNamespace(provider="openai")])
            self.assertEqual("post-mutation", caught.exception.mutation_state)
            self.assertEqual(str(patch), caught.exception.recovery_ref)

    def test_rejected_retry_push_preserves_revised_checkpoint(self):
        class PushFailureRunner(RecordingRunner):
            def __init__(self, changed):
                super().__init__(changed)
                self.patch_writes = 0

            def __call__(self, cmd, **kwargs):
                result = super().__call__(cmd, **kwargs)
                if cmd[:4] == ["git", "diff", "--binary", "--cached"]:
                    self.patch_writes += 1
                    return subprocess.CompletedProcess(
                        cmd, 0,
                        result.stdout + f"# checkpoint {self.patch_writes}\n", "")
                if cmd[:2] == ["git", "push"]:
                    return subprocess.CompletedProcess(
                        cmd, 1, "", "remote rejected push")
                return result

        class State:
            health = None
            @staticmethod
            def models_for_task_prefix(*_args, **_kwargs): return ()

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = PushFailureRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit="d" * 40,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            original_patch = patch.read_text()
            result = SimpleNamespace(
                outcome="success", output="done", terminal_outcome="completed",
                attempts=({"model": "gpt-test", "provider": "openai",
                           "invocation_id": "next", "reservation_id": None,
                           "mutation_state": "post-mutation"},))
            executor = mock.Mock()
            executor.execute.return_value = result
            with mock.patch.object(invoke, "CapacityExecutor",
                                   return_value=executor), \
                 mock.patch.object(invoke, "cli_adapter", return_value=mock.Mock()), \
                 mock.patch.object(invoke, "repository_test_command",
                                   return_value=["python3", "-m", "unittest"]):
                with self.assertRaisesRegex(invoke.DeliveryError,
                                            "remote rejected") as caught:
                    invoke.execute(
                        "owner/repo", 214, "token", pathlib.Path("/checkout"),
                        runner=runner, client=FakeClient(), state=State(),
                        registry=[SimpleNamespace(provider="openai")])
            manifest = json.loads(invoke.recovery_manifest(patch).read_text())
            self.assertEqual("d" * 40, manifest["pending_head"])
            self.assertNotIn("delivered_head", manifest)
            self.assertNotEqual(original_patch, patch.read_text())
            self.assertIn("# checkpoint 3", patch.read_text())
            self.assertEqual(str(patch), caught.exception.recovery_ref)

    def test_first_pass_ambiguous_push_has_a_pending_recovery_marker(self):
        class PushFailureRunner(RecordingRunner):
            def __call__(self, cmd, **kwargs):
                result = super().__call__(cmd, **kwargs)
                if cmd[:2] == ["git", "push"]:
                    return subprocess.CompletedProcess(
                        cmd, 1, "", "remote accepted; response lost")
                return result

        class State:
            health = None
            @staticmethod
            def models_for_task_prefix(*_args, **_kwargs): return ()

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = PushFailureRunner(["src/app.py"])
            result = SimpleNamespace(
                outcome="success", output="done", terminal_outcome="completed",
                attempts=({"model": "gpt-test", "provider": "openai",
                           "invocation_id": "first", "reservation_id": None,
                           "mutation_state": "post-mutation"},))
            executor = mock.Mock()
            executor.execute.return_value = result
            with mock.patch.object(invoke, "CapacityExecutor",
                                   return_value=executor), \
                 mock.patch.object(invoke, "cli_adapter", return_value=mock.Mock()), \
                 mock.patch.object(invoke, "repository_test_command",
                                   return_value=["python3", "-m", "unittest"]):
                with self.assertRaisesRegex(invoke.DeliveryError,
                                            "response lost") as caught:
                    invoke.execute(
                        "owner/repo", 214, "token", pathlib.Path("/checkout"),
                        runner=runner, client=FakeClient(), state=State(),
                        registry=[SimpleNamespace(provider="openai")])
            patch = invoke.recovery_patch("owner/repo", 214)
            manifest = json.loads(invoke.recovery_manifest(patch).read_text())
            self.assertEqual("d" * 40, manifest["pending_head"])
            self.assertEqual("ambiguous", caught.exception.mutation_state)
            self.assertEqual(str(patch), caught.exception.recovery_ref)

    def test_failed_push_marker_promotion_preserves_pending_recovery(self):
        class State:
            health = None
            @staticmethod
            def models_for_task_prefix(*_args, **_kwargs): return ()

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit="d" * 40,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            result = SimpleNamespace(
                outcome="success", output="done", terminal_outcome="completed",
                attempts=({"model": "gpt-test", "provider": "openai",
                           "invocation_id": "next", "reservation_id": None,
                           "mutation_state": "post-mutation"},))
            executor = mock.Mock()
            executor.execute.return_value = result
            with mock.patch.object(invoke, "CapacityExecutor",
                                   return_value=executor), \
                 mock.patch.object(invoke, "cli_adapter", return_value=mock.Mock()), \
                 mock.patch.object(invoke, "repository_test_command",
                                   return_value=["python3", "-m", "unittest"]), \
                 mock.patch.object(invoke, "mark_recovery_pushed",
                                   side_effect=OSError("manifest replace failed")):
                with self.assertRaisesRegex(OSError,
                                            "manifest replace failed") as caught:
                    invoke.execute(
                        "owner/repo", 214, "token", pathlib.Path("/checkout"),
                        runner=runner, client=FakeClient(), state=State(),
                        registry=[SimpleNamespace(provider="openai")])
            manifest = json.loads(invoke.recovery_manifest(patch).read_text())
            self.assertEqual("d" * 40, manifest["pending_head"])
            self.assertNotIn("delivered_head", manifest)
            self.assertEqual("ambiguous", caught.exception.mutation_state)
            self.assertEqual("push-outcome-ambiguous",
                             caught.exception.terminal_outcome)
            self.assertEqual(str(patch), caught.exception.recovery_ref)

    def test_successful_delivery_removes_recovery_pair_after_durable_pr(self):
        self._assert_execute_recovery_cleanup(create_pr_fails=False)

    def test_durable_delivery_cleanup_failure_preserves_accounting(self):
        class State:
            health = None
            @staticmethod
            def models_for_task_prefix(*_args, **_kwargs): return ()

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit="d" * 40,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            result = SimpleNamespace(
                outcome="success", output="done", terminal_outcome="completed",
                attempts=({"model": "gpt-test", "provider": "openai",
                           "invocation_id": "next", "reservation_id": None,
                           "mutation_state": "post-mutation"},))
            executor = mock.Mock()
            executor.execute.return_value = result
            with mock.patch.object(invoke, "CapacityExecutor",
                                   return_value=executor), \
                 mock.patch.object(invoke, "cli_adapter", return_value=mock.Mock()), \
                 mock.patch.object(invoke, "repository_test_command",
                                   return_value=["python3", "-m", "unittest"]), \
                 mock.patch.object(invoke, "remove_recovery",
                                   side_effect=OSError("cleanup unavailable")):
                with self.assertRaisesRegex(OSError,
                                            "cleanup unavailable") as caught:
                    invoke.execute(
                        "owner/repo", 214, "token", pathlib.Path("/checkout"),
                        runner=runner, client=FakeClient(), state=State(),
                        registry=[SimpleNamespace(provider="openai")])
            self.assertEqual("post-mutation", caught.exception.mutation_state)
            self.assertEqual("recovery-cleanup-failed",
                             caught.exception.terminal_outcome)
            self.assertEqual(str(patch), caught.exception.recovery_ref)
            self.assertTrue(patch.exists())

    def test_failed_durable_pr_write_preserves_recovery_pair(self):
        self._assert_execute_recovery_cleanup(create_pr_fails=True)

    def test_no_attempt_retry_preserves_original_worker_provenance(self):
        class State:
            health = None
            @staticmethod
            def models_for_task_prefix(*_args, **_kwargs): return ()

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            base = "d" * 40
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=base,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            manifest = invoke.recovery_manifest(patch)
            original = manifest.read_bytes()
            result = SimpleNamespace(
                outcome="no-eligible-capacity", output="", attempts=(),
                terminal_outcome="not-admitted")
            executor = mock.Mock()
            executor.execute.return_value = result
            with mock.patch.object(invoke, "CapacityExecutor",
                                   return_value=executor), \
                 mock.patch.object(invoke, "cli_adapter", return_value=mock.Mock()):
                with self.assertRaisesRegex(invoke.DeliveryError,
                                            "no-eligible-capacity"):
                    invoke.execute(
                        "owner/repo", 214, "token", pathlib.Path("/checkout"),
                        runner=runner, client=FakeClient(), state=State(),
                        registry=[SimpleNamespace(provider="openai")])
            self.assertEqual(original, manifest.read_bytes())
            self.assertEqual(self.WORKER,
                             json.loads(original)["originating_worker"])

    def _assert_execute_recovery_cleanup(self, *, create_pr_fails):
        class State:
            health = None
            @staticmethod
            def models_for_task_prefix(*_args, **_kwargs): return ()

        class Client(FakeClient):
            def create_pr(client_self, title, head, base, body):
                if create_pr_fails:
                    raise invoke.DeliveryError("durable PR write failed")
                return super().create_pr(title, head, base, body)

        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            runner = RecordingRunner(["src/app.py"])
            base = "d" * 40
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=base,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            result = SimpleNamespace(
                outcome="success", output="done", terminal_outcome="completed",
                attempts=({"model": "gpt-test", "provider": "openai",
                           "invocation_id": "next", "reservation_id": None,
                           "mutation_state": "post-mutation"},))
            executor = mock.Mock()
            executor.execute.return_value = result
            patches = (
                mock.patch.object(invoke, "CapacityExecutor", return_value=executor),
                mock.patch.object(invoke, "cli_adapter", return_value=mock.Mock()),
                mock.patch.object(invoke, "repository_test_command",
                                  return_value=["python3", "-m", "unittest"]),
            )
            with patches[0], patches[1], patches[2]:
                if create_pr_fails:
                    with self.assertRaisesRegex(
                            invoke.DeliveryError,
                            "durable PR write failed") as caught:
                        invoke.execute(
                            "owner/repo", 214, "token", pathlib.Path("/checkout"),
                            runner=runner, client=Client(), state=State(),
                            registry=[SimpleNamespace(provider="openai")])
                    self.assertEqual("post-mutation",
                                     caught.exception.mutation_state)
                    self.assertEqual("delivery-finalization-failed",
                                     caught.exception.terminal_outcome)
                    self.assertEqual(str(patch), caught.exception.recovery_ref)
                    self.assertTrue(patch.exists())
                    self.assertTrue(invoke.recovery_manifest(patch).exists())
                    self.assertEqual(
                        base, json.loads(invoke.recovery_manifest(
                            patch).read_text())["delivered_head"])
                    with self.assertRaisesRegex(
                            invoke.DeliveryError,
                            "durable PR write failed") as resumed:
                        invoke.execute(
                            "owner/repo", 214, "token", pathlib.Path("/checkout"),
                            runner=runner, client=Client(), state=State(),
                            registry=[SimpleNamespace(provider="openai")])
                    self.assertEqual("post-mutation",
                                     resumed.exception.mutation_state)
                    self.assertEqual(str(patch), resumed.exception.recovery_ref)
                    self.assertEqual(1, executor.execute.call_count)
                    invoke.execute(
                        "owner/repo", 214, "token", pathlib.Path("/checkout"),
                        runner=runner, client=FakeClient(), state=State(),
                        registry=[SimpleNamespace(provider="openai")])
                    self.assertFalse(patch.exists())
                    self.assertFalse(invoke.recovery_manifest(patch).exists())
                else:
                    invoke.execute(
                        "owner/repo", 214, "token", pathlib.Path("/checkout"),
                        runner=runner, client=Client(), state=State(),
                        registry=[SimpleNamespace(provider="openai")])
                    self.assertFalse(patch.exists())
                    self.assertFalse(invoke.recovery_manifest(patch).exists())

    def test_orphaned_patch_or_manifest_fails_closed_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            patch = pathlib.Path(directory, "story-214.patch")
            for orphan in (patch, invoke.recovery_manifest(patch)):
                with self.subTest(orphan=orphan.name):
                    patch.unlink(missing_ok=True)
                    invoke.recovery_manifest(patch).unlink(missing_ok=True)
                    orphan.write_text("orphan")
                    with self.assertRaisesRegex(invoke.DeliveryError, "incomplete"):
                        invoke.recovery_available(patch)
                    self.assertTrue(orphan.exists())

    def test_half_written_checkpoint_pair_is_discarded(self):
        runner = RecordingRunner(["src/app.py"])
        with tempfile.TemporaryDirectory() as recovery, mock.patch.dict(
                invoke.os.environ, {"FACTORY_RECOVERY_DIR": recovery}):
            patch = pathlib.Path(invoke.checkpoint_failed_work(
                pathlib.Path("/old"), pathlib.Path("/checkout"),
                repo="owner/repo", story_number=214, default="main",
                base_ref="origin/main", base_commit=self.BASE,
                scope=["src/app.py"], mutation_state="post-mutation",
                terminal_outcome="started-mid-work-failed",
                originating_worker=self.WORKER, runner=runner))
            patch.write_text("replacement interrupted before manifest update")
            with self.assertRaisesRegex(invoke.DeliveryError, "digest"):
                invoke.recovery_available(patch)
            self.assertTrue(patch.exists())
            self.assertTrue(invoke.recovery_manifest(patch).exists())

    def test_worker_prompt_discloses_recovered_paths_and_outcome(self):
        value = {"recovery_context": {
            "present": True,
            "trust": invoke.RECOVERY_TRUST,
            "recovered_paths": ["src/app.py"],
            "previous_mutation_state": "post-mutation",
            "previous_terminal_outcome": "started-mid-work-failed",
        }}
        prompt = invoke.worker_prompt(value)
        self.assertIn("`untrusted partial changes`", prompt)
        self.assertIn('"src/app.py"', prompt)
        self.assertIn('"started-mid-work-failed"', prompt)


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

    def test_failed_test_output_keeps_early_error_and_late_summary(self):
        error = "browserType.launch: Google Chrome crashed"
        summary = "tests 189; pass 186; fail 0; skipped 3"
        stderr = error + "\n" + ("routine output\n" * 400) + summary
        with self.assertRaises(invoke.DeliveryError) as caught:
            invoke.run(["npm", "test"], cwd=pathlib.Path("."), timeout=5,
                       runner=self.failing(stderr=stderr))
        message = str(caught.exception)
        self.assertIn(error, message)
        self.assertIn(summary, message)

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
            code = invoke.main(["--repo", "o/r", "--story", "1",
                                "--reservation", "a" * 32])
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
                                    "--checkout", directory,
                                    "--reservation", "a" * 32])
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

    def test_pending_rename_lists_source_and_destination_paths(self):
        root = self.repo()
        source = root / "old.txt"
        source.write_text("tracked\n")
        subprocess.run(["git", "add", "old.txt"], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "tracked"], cwd=root, check=True)
        source.rename(root / "new.txt")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        self.assertEqual(
            ["new.txt", "old.txt"], invoke.changed_paths(root, "HEAD"))

    def test_committed_rename_lists_source_and_destination_paths(self):
        root = self.repo()
        source = root / "old.txt"
        source.write_text("tracked\n" * 20)
        subprocess.run(["git", "add", "old.txt"], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "tracked"], cwd=root, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True).stdout.strip()
        source.rename(root / "new.txt")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "rename"], cwd=root, check=True)
        self.assertEqual(
            ["new.txt", "old.txt"], invoke.changed_paths(root, base))
