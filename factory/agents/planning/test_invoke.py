import base64
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

import invoke
import contract
from factory.capacity_pool.router import ModelCapacity, Tier
from factory.capacity_pool.state import CapacityState
from test_artifacts import FakeStore, campaign_output, project_issue, project_output


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Client(FakeStore):
    def __init__(self, product_paths=None, product_text="# Product"):
        super().__init__([{"number": 1, "labels": ["type:roadmap-commitment"],
                           "body": "Retirement direction"}])
        self.repo, self.token = "o/r", "token"
        self.product_paths = ["product.md"] if product_paths is None else product_paths
        self.product_text = product_text

    def _api(self, path, method="GET", payload=None):
        if path == "":
            return {"default_branch": "main"}
        if path.startswith("/git/trees/"):
            return {"tree": ([{"path": item, "type": "blob"} for item in self.product_paths]
                             + [{"path": "docs/decisions/0001.md", "type": "blob"}])}
        if path.startswith("/contents/"):
            text = self.product_text if path.lower().endswith("product.md") else "# ADR"
            return {"content": base64.b64encode(text.encode()).decode()}
        raise AssertionError(path)

    def _pages(self, path):
        return []


class RepositoryBytesClient(Client):
    """Controls the full file tree and per-path content, so the
    repository-grounding byte limit can be tested precisely against real
    accumulated byte counts rather than a mocked-out check."""

    def __init__(self, files, contents):
        super().__init__(product_paths=files)
        self.contents = contents

    def _api(self, path, method="GET", payload=None):
        if path.startswith("/contents/"):
            target = path[len("/contents/"):].split("?", 1)[0]
            text = self.contents.get(target, "# ADR")
            return {"content": base64.b64encode(text.encode()).decode()}
        return super()._api(path, method=method, payload=payload)


class CloneFakeClient(RepositoryBytesClient):
    """Adds the exact-commit ref lookup `clone_and_ground_repository` needs,
    on top of `RepositoryBytesClient`'s per-path content control."""

    def __init__(self, files, contents, commit_sha="c" * 40):
        super().__init__(files, contents)
        self.commit_sha = commit_sha

    def _api(self, path, method="GET", payload=None):
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": self.commit_sha, "type": "commit"}}
        return super()._api(path, method=method, payload=payload)


class CloneSizedClient(CloneFakeClient):
    """Adds declared per-path blob sizes to the recursive tree response,
    as GitHub's real API does, so the checkout-size preflight can be
    tested against a declared size distinct from any fake local content."""

    def __init__(self, files, contents, sizes, commit_sha="c" * 40):
        super().__init__(files, contents, commit_sha=commit_sha)
        self.sizes = sizes

    def _api(self, path, method="GET", payload=None):
        if path.startswith("/git/trees/"):
            result = super()._api(path, method=method, payload=payload)
            for item in result["tree"]:
                item["size"] = self.sizes.get(item["path"], 0)
            return result
        return super()._api(path, method=method, payload=payload)


def fake_clone_runner(contents):
    """Simulates `git clone` + `git checkout` by writing the given files to
    disk for real, so downstream local-file reads (repository_evidence,
    the model's own tool access) see genuine content — not a mocked
    subprocess result with nothing behind it."""
    def runner(command, **kwargs):
        if command[:2] == ["git", "clone"]:
            (pathlib.Path(kwargs["cwd"]) / "repo").mkdir(parents=True, exist_ok=True)
        elif command[:2] == ["git", "checkout"]:
            repo_dir = pathlib.Path(kwargs["cwd"])
            for path, text in contents.items():
                target = repo_dir / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        return Result(0, "", "")
    return runner


class CloneProjectClient(CloneFakeClient):
    """A Project trigger (matching ProjectClient's shape, so project_output()
    validates cleanly) combined with CloneFakeClient's controllable oversized
    file content, for exercising the clone fallback through full execute()."""

    def __init__(self, contents, commit_sha="c" * 40):
        super().__init__(files=["product.md", "src/model/core.js"],
                         contents=contents, commit_sha=commit_sha)
        FakeStore.__init__(self, [project_issue()])
        self.issues[0]["labels"] = ["type:project", "project:planning"]
        self.repo, self.token = "o/r", "token"
        self.contents = contents
        self.commit_sha = commit_sha

    def _pages(self, path):
        if path.endswith("/timeline"):
            return [{"id": 99, "event": "labeled",
                     "label": {"name": "project:planning"}}]
        return []


class ProjectClient(Client):
    def __init__(self):
        FakeStore.__init__(self, [project_issue()])
        self.issues[0]["labels"] = ["type:project", "project:planning"]
        self.repo, self.token = "o/r", "token"
        self.product_paths = ["product.md", "src/model/core.js"]
        self.product_text = "# Product"

    def _pages(self, path):
        if path.endswith("/timeline"):
            return [{"id": 99, "event": "labeled",
                     "label": {"name": "project:planning"}}]
        return []


def capacity():
    state = CapacityState()
    model = ModelCapacity("gpt-5.6-terra", "openai", Tier.BALANCED,
                          frozenset({"reason", "json"}))
    state.mark_healthy(model.provider, model.name, "test-probe")
    return state, (model,)


class InvocationTests(unittest.TestCase):
    def test_repository_evidence_extracts_explicit_owner_and_dependency_policy(self):
        evidence = invoke.repository_evidence(
            ["index.html", "app.js", "test/policy.test.js", "package.json"],
            {"index.html": '<script type="module" src="/app.js"></script>',
             "app.js": "export const render = () => {};",
             "test/policy.test.js": (
                 "assert.equal(packageJson.devDependencies.playwright, undefined);"),
             "package.json": json.dumps({
                 "factoryPlanning": {"productionOwners": [{
                     "behavior": "winning allocation disclosure",
                     "path": "app.js"}]},
                 "factoryPolicy": {"forbiddenDependencies": ["puppeteer"]}})})
        self.assertEqual("app.js", evidence["production_owners"][0]["path"])
        self.assertEqual(["playwright", "puppeteer"],
                         evidence["forbidden_dependencies"])

    def test_html_script_relationship_does_not_infer_production_ownership(self):
        evidence = invoke.repository_evidence(
            ["index.html", "app.js"],
            {"index.html": '<script type="module" src="/app.js"></script>',
             "app.js": "export const render = () => {};"})
        self.assertEqual([], evidence["production_owners"])

    def test_positive_dependency_presence_assertion_is_not_a_ban(self):
        lines = [
            "expect(packageJson.dependencies.react).not.toBeNull();",
            "expect(packageJson.dependencies).not.toEqual({});",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(set(), invoke.forbidden_dependency_assertions(line))
        line = lines[0]
        evidence = invoke.repository_evidence(
            ["test/policy.test.js"], {"test/policy.test.js": line})
        self.assertEqual([], evidence["forbidden_dependencies"])

    def test_explicit_dependency_absence_assertion_is_a_ban(self):
        line = "assert.equal(packageJson.devDependencies.playwright, undefined);"
        self.assertEqual(
            {"playwright"}, invoke.forbidden_dependency_assertions(line))

    def test_assertion_shaped_text_outside_executable_tests_is_not_policy(self):
        assertion = "assert.equal(packageJson.devDependencies.playwright, undefined);"
        for path in ("README.md", "src/example.js"):
            with self.subTest(path=path):
                evidence = invoke.repository_evidence([path], {path: assertion})
                self.assertEqual([], evidence["forbidden_dependencies"])

    def test_product_preflight_requires_exactly_one_nonempty_product(self):
        for client, error in ((Client(product_paths=[]), invoke.InvocationError),
                              (Client(product_paths=["product.md", "Product.md"]),
                               invoke.InvocationError),
                              (Client(product_text=""), contract.ContractError)):
            with self.assertRaises(error):
                product, adrs, repository = invoke.read_repository(client)
                contract.validate_input({"trigger": client.get_issue(1), "product": product,
                                         "adrs": adrs, "repository": repository,
                                         "review_comments": [], "existing_plan": {}})
        product, adrs, repository = invoke.read_repository(
            Client(product_paths=["PRODUCT.md"], product_text="# Human product"))
        self.assertEqual("# Human product", product)
        self.assertIn("PRODUCT.md", repository["files"])

    def test_default_repository_byte_limit_constant_is_unchanged(self):
        self.assertEqual(500_000, invoke.DEFAULT_MAX_REPOSITORY_BYTES)

    def test_default_read_fails_over_500kb_without_override(self):
        client = RepositoryBytesClient(
            files=["product.md", "a.py"], contents={"a.py": "x" * 600_000})
        with self.assertRaisesRegex(invoke.InvocationError,
                                    "exceeds configured limit of 500000 bytes"):
            invoke.read_repository(client)

    def test_explicit_wider_override_permits_more_content(self):
        client = RepositoryBytesClient(
            files=["product.md", "a.py"], contents={"a.py": "x" * 600_000})
        _, _, repository = invoke.read_repository(client, max_repository_bytes=700_000)
        self.assertIn("a.py", repository["sources"])

    def test_explicit_narrower_override_rejects_content_that_passes_at_default(self):
        """Proves this is a real override in both directions, not just a
        higher ceiling: content well under the 500KB default still fails
        against an explicitly smaller configured limit."""
        client = RepositoryBytesClient(
            files=["product.md", "a.py"], contents={"a.py": "x" * 100_000})
        invoke.read_repository(client)  # succeeds at the unchanged default
        with self.assertRaisesRegex(invoke.InvocationError,
                                    "exceeds configured limit of 50000 bytes"):
            invoke.read_repository(client, max_repository_bytes=50_000)

    def test_content_exceeding_explicit_override_still_fails_closed(self):
        client = RepositoryBytesClient(
            files=["product.md", "a.py"], contents={"a.py": "x" * 800_000})
        with self.assertRaisesRegex(invoke.InvocationError,
                                    "exceeds configured limit of 700000 bytes"):
            invoke.read_repository(client, max_repository_bytes=700_000)

    def test_non_positive_override_rejected(self):
        client = RepositoryBytesClient(files=["product.md"], contents={})
        with self.assertRaisesRegex(invoke.InvocationError, "must be positive"):
            invoke.read_repository(client, max_repository_bytes=0)

    def test_effective_limit_is_logged_even_when_the_read_fails(self):
        """PR #673 review finding: the observability call originally sat
        after the read loop, so the exact failure it exists to explain
        (exceeding the configured limit) skipped it entirely."""
        client = RepositoryBytesClient(
            files=["product.md", "a.py"], contents={"a.py": "x" * 600_000})
        with mock.patch.object(invoke.obs, "process_event") as process_event:
            with self.assertRaises(invoke.InvocationError):
                invoke.read_repository(client, max_repository_bytes=500_000)
        process_event.assert_any_call(
            "planning.repository.max_bytes_configured",
            max_repository_bytes=500_000, repo=None, artifact=None)

    def test_effective_limit_is_logged_even_when_product_preflight_fails(self):
        """Second-round finding on the same PR: moving the log before the
        byte-accumulation loop wasn't enough — product.md/ADR reads happen
        even earlier and can fail first. The log must be the first thing
        this function does past its own input validation."""
        client = Client(product_paths=[])
        with mock.patch.object(invoke.obs, "process_event") as process_event:
            with self.assertRaises(invoke.InvocationError):
                invoke.read_repository(client, max_repository_bytes=123)
        process_event.assert_any_call(
            "planning.repository.max_bytes_configured",
            max_repository_bytes=123, repo=None, artifact=None)

    def test_logged_limit_carries_invocation_identity_when_available(self):
        """Third-round finding on the same PR: the log carried no repo/
        artifact identity at all, so two invocations with the same limit
        were indistinguishable in the log — and would hash to the same
        event_id. Proves both fields reach the log when the caller supplies
        them (as execute() now always does)."""
        client = RepositoryBytesClient(
            files=["product.md", "a.py"], contents={"a.py": "small"})
        with mock.patch.object(invoke.obs, "process_event") as process_event:
            invoke.read_repository(client, max_repository_bytes=500_000,
                                   repo="o/r", artifact=42)
        process_event.assert_any_call(
            "planning.repository.max_bytes_configured",
            max_repository_bytes=500_000, repo="o/r", artifact=42)

    def test_oversized_repository_falls_back_to_an_exact_commit_clone(self):
        """Story #680: read_repository's byte cap now raises a distinct
        RepositoryTooLargeError, and clone_and_ground_repository grounds
        against an exact, recorded commit instead — stronger provenance
        than the non-fallback path, which records no commit at all."""
        client = CloneFakeClient(
            files=["product.md", "a.py"], contents={"a.py": "x" * 600_000},
            commit_sha="d34db33f" * 5)
        with self.assertRaises(invoke.RepositoryTooLargeError):
            invoke.read_repository(client, max_repository_bytes=500_000)
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run",
                                  side_effect=fake_clone_runner({"a.py": "x" * 600_000})):
            product, adrs, repository, workspace = invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=42)
        self.assertEqual("d34db33f" * 5, repository["commit_sha"])
        self.assertEqual({}, repository["sources"])
        self.assertIn("a.py", repository["files"])
        # workspace is the neutral parent, not the checkout itself — see
        # the review finding this was fixed for: an engine launched with
        # its working directory *inside* a real checkout auto-loads that
        # repository's own CLAUDE.md/AGENTS.md as its own instructions.
        self.assertEqual(pathlib.Path(workspace_root), workspace)
        self.assertFalse(str(workspace).endswith("repo"))

    def test_clone_fallback_evidence_matches_the_api_based_path(self):
        """repository_evidence() itself is unchanged; only its input source
        changes. The same manifest content must produce identical facts
        whether read via the GitHub API or a local clone."""
        manifest = json.dumps({"factoryPolicy": {"forbiddenDependencies": ["puppeteer"]}})
        client = CloneFakeClient(
            files=["product.md", "app.py", "policy.json"],
            contents={"app.py": "def handle(): pass", "policy.json": manifest})
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run",
                                  side_effect=fake_clone_runner(
                                      {"app.py": "def handle(): pass",
                                       "policy.json": manifest})):
            _, _, repository, _ = invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)
        api_evidence = invoke.repository_evidence(
            repository["files"], {"policy.json": manifest})
        self.assertEqual(api_evidence["forbidden_dependencies"],
                         repository["forbidden_dependencies"])
        self.assertEqual(["puppeteer"], repository["forbidden_dependencies"])

    def test_execute_falls_back_and_cleans_up_the_clone_on_success(self):
        big_content = {"src/model/core.js": "x" * 600_000}
        client = CloneProjectClient(contents=big_content)
        runner = mock.Mock(return_value=Result(stdout=json.dumps(project_output())))
        clone_dirs = []

        def capturing_clone_runner(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                (pathlib.Path(kwargs["cwd"]) / "repo").mkdir(parents=True, exist_ok=True)
                clone_dirs.append(pathlib.Path(kwargs["cwd"]))
            elif command[:2] == ["git", "checkout"]:
                for path, text in big_content.items():
                    target = pathlib.Path(kwargs["cwd"]) / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
            return Result(0, "", "")

        state, registry = capacity()
        try:
            with mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client), \
                    mock.patch.object(invoke.subprocess, "run",
                                      side_effect=capturing_clone_runner):
                result = invoke.execute("o/r", 10, "token", 30, 2.5,
                                        runner=runner, state=state, registry=registry,
                                        max_repository_bytes=500_000)
        finally:
            state.close()
        self.assertIn("project:awaiting-ready", client.get_issue(10)["labels"])
        self.assertEqual(1, len(clone_dirs))
        self.assertFalse(clone_dirs[0].exists(),
                         "the ephemeral clone workspace must not survive execute()")

    def test_execute_cleans_up_the_clone_even_when_the_model_call_fails(self):
        big_content = {"src/model/core.js": "x" * 600_000}
        client = CloneProjectClient(contents=big_content)
        runner = mock.Mock(return_value=Result(1, "", "failed"))
        clone_dirs = []

        def capturing_clone_runner(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                (pathlib.Path(kwargs["cwd"]) / "repo").mkdir(parents=True, exist_ok=True)
                clone_dirs.append(pathlib.Path(kwargs["cwd"]))
            elif command[:2] == ["git", "checkout"]:
                for path, text in big_content.items():
                    target = pathlib.Path(kwargs["cwd"]) / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
            return Result(0, "", "")

        state, registry = capacity()
        try:
            with mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client), \
                    mock.patch.object(invoke.subprocess, "run",
                                      side_effect=capturing_clone_runner), \
                    self.assertRaises(invoke.InvocationError):
                invoke.execute("o/r", 10, "token", 30, 2.5,
                               runner=runner, state=state, registry=registry,
                               max_repository_bytes=500_000)
        finally:
            state.close()
        self.assertEqual(1, len(clone_dirs))
        self.assertFalse(clone_dirs[0].exists(),
                         "the ephemeral clone workspace must be removed even on failure")

    def test_fallback_keeps_the_read_only_sandbox(self):
        """run_model's InvocationPayload access mode must stay read-only in
        the fallback path — Planning must never be able to write into the
        product repository it is planning, only into its own output file."""
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            return Result(0, json.dumps(project_output()), "")

        state, registry = capacity()
        try:
            invoke.run_model(
                {"trigger": {**project_issue(), "labels": ["type:project", "project:planning"]},
                 "product": "# Product", "adrs": [],
                 "repository": {"files": ["product.md", "src/model/core.js"]},
                 "review_comments": [], "existing_plan": {}},
                30, 2.5, runner=runner, state=state, registry=registry,
                workspace=pathlib.Path("/tmp"))
        finally:
            state.close()
        self.assertIn("--sandbox", captured["command"])
        self.assertEqual("read-only",
                         captured["command"][captured["command"].index("--sandbox") + 1])

    def test_fallback_skips_git_repo_check_since_cwd_is_neutral(self):
        """The engine's working directory is the neutral parent, not the
        checkout — which is itself not a git repository, so the adapter
        must be told not to require one."""
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            return Result(0, json.dumps(project_output()), "")

        state, registry = capacity()
        try:
            invoke.run_model(
                {"trigger": {**project_issue(), "labels": ["type:project", "project:planning"]},
                 "product": "# Product", "adrs": [],
                 "repository": {"files": ["product.md", "src/model/core.js"]},
                 "review_comments": [], "existing_plan": {}},
                30, 2.5, runner=runner, state=state, registry=registry,
                workspace=pathlib.Path("/tmp"))
        finally:
            state.close()
        self.assertIn("--skip-git-repo-check", captured["command"])

    def test_clone_grounding_pins_every_read_to_the_exact_commit(self):
        """Review finding: the branch can advance between the commit
        lookup and later reads. Every grounding read must name the
        resolved commit explicitly, never the branch, so a checkout and
        its file index/product.md/ADRs can never disagree."""
        client = CloneFakeClient(
            files=["product.md", "docs/decisions/0001.md"],
            contents={}, commit_sha="f" * 40)
        seen_paths = []
        original_api = client._api

        def recording_api(path, method="GET", payload=None):
            seen_paths.append(path)
            return original_api(path, method=method, payload=payload)

        client._api = recording_api
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run", side_effect=fake_clone_runner({})):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)
        tree_calls = [p for p in seen_paths if p.startswith("/git/trees/")]
        content_calls = [p for p in seen_paths if p.startswith("/contents/")]
        self.assertTrue(tree_calls and all("f" * 40 in p for p in tree_calls),
                        f"tree read must use the exact commit, not the branch: {tree_calls}")
        self.assertTrue(content_calls and all(f"ref={'f' * 40}" in p for p in content_calls),
                        f"content reads must pin ?ref= to the exact commit: {content_calls}")

    def test_clone_evidence_fails_closed_when_oversized(self):
        """Review finding: the fallback's evidence scan had no size bound
        at all, removing exactly the protection the non-fallback path has
        — for the repositories most likely to trip it."""
        huge_manifest = json.dumps({"factoryPolicy": {
            "forbiddenDependencies": ["x" * 600_000]}})
        client = CloneFakeClient(
            files=["product.md", "policy.json"], contents={})
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run",
                                  side_effect=fake_clone_runner(
                                      {"policy.json": huge_manifest})), \
                self.assertRaisesRegex(invoke.InvocationError, "evidence"):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)

    def test_clone_evidence_checks_size_before_reading_file_content(self):
        """Review finding: a single oversized file was fully read into
        memory (read_text) before its size was ever checked against the
        limit — the size check must gate the read, not follow it."""
        huge_manifest = "x" * 600_000
        client = CloneFakeClient(files=["product.md", "policy.json"], contents={})
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run",
                                  side_effect=fake_clone_runner(
                                      {"policy.json": huge_manifest})), \
                mock.patch.object(
                    pathlib.Path, "read_text",
                    side_effect=AssertionError(
                        "read_text must not run once the on-disk size alone "
                        "exceeds the limit")), \
                self.assertRaisesRegex(invoke.InvocationError, "evidence"):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)

    def test_clone_evidence_honors_a_narrower_configured_limit(self):
        """Review finding: the fallback's evidence check compared against
        the hardcoded DEFAULT_MAX_REPOSITORY_BYTES regardless of what the
        caller configured, so an operator-set narrower limit was silently
        not enforced during the fallback."""
        client = CloneFakeClient(files=["product.md", "policy.json"], contents={})
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run",
                                  side_effect=fake_clone_runner(
                                      {"policy.json": "x" * 100_000})), \
                self.assertRaisesRegex(invoke.InvocationError,
                                      "exceeds 50000 bytes"):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1,
                max_repository_bytes=50_000)

    def test_clone_evidence_honors_a_wider_configured_limit(self):
        """Review finding, other direction: the fallback's evidence check
        compared against the hardcoded default even when the caller
        configured a wider limit, so a file within the configured
        allowance but over the hardcoded default was wrongly rejected."""
        client = CloneFakeClient(files=["product.md", "policy.json"], contents={})
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run",
                                  side_effect=fake_clone_runner(
                                      {"policy.json": "x" * 600_000})):
            # Must not raise: 600,000 bytes is over the 500,000-byte
            # default but under this call's explicit 700,000-byte limit.
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1,
                max_repository_bytes=700_000)

    def test_clone_disables_lfs_smudge(self):
        """Review finding: without GIT_LFS_SKIP_SMUDGE, checkout runs the
        LFS smudge filter and downloads every tracked object the commit
        references — unbounded, repository-controlled outbound traffic
        and disk use Planning never needs (it only reads text)."""
        captured = {}

        def runner(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                captured["env"] = kwargs["env"]
            return fake_clone_runner({})(command, **kwargs)

        client = CloneFakeClient(files=["product.md"], contents={})
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run", side_effect=runner):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)
        self.assertEqual("1", captured["env"]["GIT_LFS_SKIP_SMUDGE"])

    def test_checkout_carries_the_same_credential_and_lfs_env_as_the_clone(self):
        """Review finding: with --no-checkout on the clone, the checkout
        step is the one that actually fetches the target commit's blobs
        over the network and runs the LFS smudge filter — but it ran with
        a PATH-only environment, silently dropping both the scoped
        credential header (breaking private-repository checkout) and
        GIT_LFS_SKIP_SMUDGE (reopening the unbounded-LFS-download finding
        this same PR already claimed to fix)."""
        captured = {}

        def runner(command, **kwargs):
            if command[:2] == ["git", "checkout"]:
                captured["env"] = kwargs["env"]
            return fake_clone_runner({})(command, **kwargs)

        client = CloneFakeClient(files=["product.md"], contents={})
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run", side_effect=runner):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)
        self.assertEqual("1", captured["env"]["GIT_LFS_SKIP_SMUDGE"])
        self.assertEqual("http.https://github.com/.extraHeader",
                         captured["env"]["GIT_CONFIG_KEY_0"])

    def test_clone_refuses_before_checkout_when_blobs_exceed_the_limit(self):
        """Review finding: --filter=blob:none only defers ordinary blob
        transfer, it does not bound it — checkout still fetches and
        writes every blob the target commit's tree references, with
        nothing limiting total bytes materialized. The tree API already
        reports each blob's size, so that must gate checkout the same
        way the evidence scan is gated."""
        client = CloneSizedClient(
            files=["product.md", "big.bin"], contents={},
            sizes={"big.bin": 4_000_000})

        def runner(command, **kwargs):
            raise AssertionError(
                "no clone/checkout subprocess may run once declared blob "
                "sizes already exceed the configured limit")

        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run", side_effect=runner), \
                self.assertRaisesRegex(invoke.InvocationError, "blob content"):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)

    def test_clone_removes_every_symlink_in_the_checkout_not_just_evidence(self):
        """Security finding: only symlinks selected by the evidence scan
        were rejected; every other symlink in the checkout remained live
        and readable, and the prompt tells the model to read repository
        files directly by path — a tracked symlink to an absolute host
        path (a worker credential file, a procfs entry) would be exposed
        to the model like any other repository file."""
        client = CloneFakeClient(files=["product.md", "src/config.py"], contents={})

        def runner(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                (pathlib.Path(kwargs["cwd"]) / "repo").mkdir(parents=True, exist_ok=True)
            elif command[:2] == ["git", "checkout"]:
                repo_dir = pathlib.Path(kwargs["cwd"])
                outside = repo_dir.parent / "outside_target.txt"
                outside.write_text("host content", encoding="utf-8")
                link_path = repo_dir / "src" / "config.py"
                link_path.parent.mkdir(parents=True, exist_ok=True)
                link_path.symlink_to(outside)
            return Result(0, "", "")

        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run", side_effect=runner):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)
            self.assertFalse(
                (pathlib.Path(workspace_root) / "repo" / "src" / "config.py").exists(),
                "the symlink must be removed, not merely skipped by the evidence scan")

    def test_clone_fetches_a_filtered_no_checkout_tree_not_full_history(self):
        """Review finding: an unrestricted clone downloads every reachable
        object even though the fallback only ever uses one commit's tree
        — a repository with a long history or large historical binaries
        can time out or exhaust worker disk before planning begins."""
        captured = {}

        def runner(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                captured["command"] = command
            return fake_clone_runner({})(command, **kwargs)

        client = CloneFakeClient(files=["product.md"], contents={})
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run", side_effect=runner):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)
        self.assertIn("--filter=blob:none", captured["command"])
        self.assertIn("--no-checkout", captured["command"])

    def test_clone_evidence_rejects_symlinked_paths(self):
        """Security finding: a symlink whose target can report a stat()
        size that does not reflect what read() actually returns (as with
        a procfs pseudo-file) would defeat the size check entirely. A
        symlinked evidence path must be rejected outright, never stat'd
        or read through, regardless of what its target is."""
        client = CloneFakeClient(files=["product.md", "tests/exhaust.py"], contents={})

        def runner(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                (pathlib.Path(kwargs["cwd"]) / "repo").mkdir(parents=True, exist_ok=True)
            elif command[:2] == ["git", "checkout"]:
                repo_dir = pathlib.Path(kwargs["cwd"])
                outside = repo_dir.parent / "outside_target.txt"
                outside.write_text("innocuous", encoding="utf-8")
                link_path = repo_dir / "tests" / "exhaust.py"
                link_path.parent.mkdir(parents=True, exist_ok=True)
                link_path.symlink_to(outside)
            return Result(0, "", "")

        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run", side_effect=runner), \
                mock.patch.object(
                    pathlib.Path, "read_text",
                    side_effect=AssertionError(
                        "a symlinked evidence path must never be read")):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)

    def test_clone_credential_header_is_scoped_to_github_not_global(self):
        """Review finding: an unscoped http.extraHeader is inherited by
        every HTTP request git makes for this process, including a Git
        LFS smudge filter's request to a repository-controlled lfs.url —
        leaking this factory token to that server. The header must be
        scoped to the github.com URL prefix, which git-lfs also honors
        since it reads the same git config."""
        captured = {}

        def runner(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                captured["env"] = kwargs["env"]
            return fake_clone_runner({})(command, **kwargs)

        client = CloneFakeClient(files=["product.md"], contents={})
        with tempfile.TemporaryDirectory() as workspace_root, \
                mock.patch.object(invoke.subprocess, "run", side_effect=runner):
            invoke.clone_and_ground_repository(
                client, "o/r", "token", pathlib.Path(workspace_root), artifact=1)
        self.assertEqual("http.https://github.com/.extraHeader",
                         captured["env"]["GIT_CONFIG_KEY_0"])

    def test_campaign_executes_through_capacity_pool_then_reads_back(self):
        client, (state, registry) = Client(), capacity()
        runner = mock.Mock(return_value=Result(stdout=json.dumps(campaign_output())))
        try:
            with mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client):
                result = invoke.execute("o/r", 1, "token", 30, 2.5, runner=runner,
                                        state=state, registry=registry)
        finally:
            state.close()
        self.assertEqual("campaign", result.altitude.value)
        self.assertEqual("codex", runner.call_args.args[0][0])
        self.assertEqual(2, len(client.issues))

    def test_project_label_moves_only_after_verified_readback(self):
        client, (state, registry) = ProjectClient(), capacity()
        try:
            with mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client):
                result = invoke.execute(
                    "o/r", 10, "token", 30, 2.5,
                    runner=mock.Mock(return_value=Result(stdout=json.dumps(project_output()))),
                    state=state, registry=registry)
        finally:
            state.close()
        self.assertEqual((12, 13), result.stories)
        self.assertIn("project:awaiting-ready", client.get_issue(10)["labels"])

    def test_invalid_output_writes_nothing_and_keeps_project_planning(self):
        client, (state, registry) = ProjectClient(), capacity()
        try:
            with mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client), \
                    self.assertRaisesRegex(invoke.InvocationError, "schema-invalid"):
                invoke.execute("o/r", 10, "token", 30, 2.5,
                               runner=lambda *a, **k: Result(stdout="{}"),
                               state=state, registry=registry)
        finally:
            state.close()
        self.assertIn("project:planning", client.get_issue(10)["labels"])
        self.assertEqual({}, client.comments)

    def test_repository_contradiction_fails_before_artifact_write(self):
        client, (state, registry) = ProjectClient(), capacity()
        client.product_paths.extend(["app.js", "test/app.test.js"])
        output = project_output()
        output["stories"][0]["scope"] = ["test/app.test.js"]
        output["stories"][0]["spec"] = "Change winning allocation disclosure."
        repository_facts = [{"behavior": "winning allocation disclosure",
                             "path": "app.js"}]
        original_read = invoke.read_repository

        def read_with_facts(store, **kwargs):
            product, adrs, repository = original_read(store, **kwargs)
            repository["production_owners"] = repository_facts
            return product, adrs, repository

        try:
            with mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client), \
                    mock.patch.object(invoke, "read_repository",
                                      side_effect=read_with_facts), \
                    mock.patch.object(invoke.artifacts, "write") as write, \
                    self.assertRaisesRegex(invoke.InvocationError, "schema-invalid"):
                invoke.execute(
                    "o/r", 10, "token", 30, 2.5,
                    runner=mock.Mock(return_value=Result(stdout=json.dumps(output))),
                    state=state, registry=registry)
        finally:
            state.close()
        write.assert_not_called()
        self.assertIn("project:planning", client.get_issue(10)["labels"])
        self.assertEqual({}, client.comments)

    def test_403_and_404_fail_before_any_write(self):
        for code in (403, 404):
            client = Client()
            with mock.patch.object(client, "get_issue", side_effect=urllib.error.HTTPError(
                    "url", code, "denied", {}, None)), \
                 mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client), \
                 self.assertRaisesRegex(invoke.InvocationError, "no planning artifacts were written"):
                invoke.execute("o/r", 1, "token", 30, 2.5)
            self.assertEqual({}, client.comments)

    def test_output_parser_accepts_direct_and_structured_stream_json(self):
        expected = campaign_output()
        self.assertEqual(expected, invoke._parse_output(json.dumps(expected)))
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "StructuredOutput", "input": expected}]}},
            {"type": "result", "result": "done"}]
        self.assertEqual(expected, invoke._parse_output(
            "\n".join(json.dumps(event) for event in events)))

    def test_output_parser_rejects_malformed_and_non_object(self):
        with self.assertRaisesRegex(invoke.InvocationError, "malformed JSON"):
            invoke._parse_output("not-json")
        with self.assertRaisesRegex(invoke.InvocationError, "non-object"):
            invoke._parse_output("[]")

    def test_normal_planning_does_not_escalate_but_named_trigger_does(self):
        self.assertEqual(frozenset(), invoke._planning_triggers(
            {"trigger": {"labels": ["type:project"]}}))
        self.assertEqual(frozenset({"architecture"}), invoke._planning_triggers(
            {"trigger": {"labels": ["type:project", "architecture"]}}))

    def test_prompt_version_changes_with_prompt(self):
        with mock.patch.object(invoke.pathlib.Path, "read_bytes", return_value=b"one"):
            first = invoke.prompt_version()
        with mock.patch.object(invoke.pathlib.Path, "read_bytes", return_value=b"two"):
            second = invoke.prompt_version()
        self.assertNotEqual(first, second)

    def test_readback_retry_is_bounded(self):
        expected = object()
        with mock.patch.object(invoke.artifacts, "verify", side_effect=[
                invoke.artifacts.ArtifactError("missing"), expected]) as verify:
            sleeps = []
            actual = invoke.verify_with_retry(None, {}, "key", contract.Altitude.CAMPAIGN,
                                               sleeper=sleeps.append)
        self.assertIs(expected, actual)
        self.assertEqual([1], sleeps)
        self.assertEqual(2, verify.call_count)


if __name__ == "__main__":
    unittest.main()
