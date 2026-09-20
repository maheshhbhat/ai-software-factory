import base64
import json
import pathlib
import tempfile
import time
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


class GroundingScopeClient(Client):
    """Controls the full file tree, per-path content, and the Project body,
    so `### Grounding scope` can be exercised against real path matching and
    the real 500KB accumulator — not a mocked-out version of either."""

    def __init__(self, files, contents, project_body):
        super().__init__(product_paths=files)
        self.contents = contents
        self.issues[0]["labels"] = ["type:project", "project:planning"]
        self.issues[0]["body"] = project_body

    def _api(self, path, method="GET", payload=None):
        if path.startswith("/contents/"):
            target = path[len("/contents/"):]
            text = self.contents.get(target, "# ADR")
            return {"content": base64.b64encode(text.encode()).decode()}
        return super()._api(path, method=method, payload=payload)


class CampaignWithScopeLikeSectionClient(Client):
    """A campaign (`type:roadmap-commitment`) trigger whose body happens to
    contain a `### Grounding scope`-shaped section pointing at a pattern that
    matches nothing real — reproducing the PR #670 finding that this must
    never be applied outside a confirmed Project trigger."""

    def __init__(self):
        FakeStore.__init__(self, [{"number": 1, "labels": ["type:roadmap-commitment"],
                                   "body": ("Retirement direction\n\n"
                                            "### Grounding scope\nnonexistent/**\n")}])
        self.repo, self.token = "o/r", "token"
        self.product_paths = ["product.md"]
        self.product_text = "# Product"


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

    def test_absent_grounding_scope_reads_every_matching_file_unchanged(self):
        """Story #669: no `### Grounding scope` section must be byte-for-byte
        the pre-existing behavior — every matching file is read."""
        client = GroundingScopeClient(
            files=["product.md", "a.py", "b.py"],
            contents={"a.py": "a = 1", "b.py": "b = 2"},
            project_body="### Objective\nno scope declared here\n")
        _, _, repository = invoke.read_repository(client, client.issues[0]["body"])
        # `GroundingScopeClient`/`Client` always add an ADR fixture path (a
        # `.md` file, also a generic grounded source) alongside the declared
        # files — real pre-existing behavior, unaffected by this change.
        self.assertEqual({"a.py", "b.py", "docs/decisions/0001.md"},
                         set(repository["grounded_files"]))
        self.assertEqual(repository["grounded_files"], invoke.read_repository(client)[2]
                         ["grounded_files"])

    def test_grounding_scope_narrows_to_declared_patterns_only(self):
        client = GroundingScopeClient(
            files=["product.md", "a.py", "b.py"],
            contents={"a.py": "a = 1", "b.py": "b = 2"},
            project_body="### Grounding scope\na.py\n")
        _, _, repository = invoke.read_repository(client, client.issues[0]["body"])
        self.assertEqual(["a.py"], repository["grounded_files"])
        self.assertNotIn("b.py", repository["sources"])

    def test_grounding_scope_matching_no_files_fails_closed(self):
        client = GroundingScopeClient(
            files=["product.md", "a.py"], contents={"a.py": "a = 1"},
            project_body="### Grounding scope\nnonexistent/**\n")
        with self.assertRaisesRegex(invoke.InvocationError, "grounding scope matched no files"):
            invoke.read_repository(client, client.issues[0]["body"])

    def test_malformed_grounding_scope_fails_closed(self):
        client = GroundingScopeClient(
            files=["product.md", "a.py"], contents={"a.py": "a = 1"},
            project_body="### Grounding scope\n- a.py\n")
        with self.assertRaisesRegex(invoke.InvocationError, "grounding scope"):
            invoke.read_repository(client, client.issues[0]["body"])

    def test_scoped_grounding_still_enforces_500kb_cap(self):
        client = GroundingScopeClient(
            files=["product.md", "a.py"], contents={"a.py": "x" * 600_000},
            project_body="### Grounding scope\na.py\n")
        with self.assertRaisesRegex(invoke.InvocationError, "exceeds 500KB"):
            invoke.read_repository(client, client.issues[0]["body"])

    def test_grounding_scope_rejects_internal_blank_line(self):
        """PR #670 review finding: merge_gate.parse_scope silently drops an
        internal blank line rather than rejecting it. Story #669's own
        fail-closed contract requires rejection; enforce it here without
        touching that shared, unmodified parser."""
        client = GroundingScopeClient(
            files=["product.md", "a.py", "b.py"],
            contents={"a.py": "a = 1", "b.py": "b = 2"},
            project_body="### Grounding scope\na.py\n\nb.py\n")
        with self.assertRaisesRegex(invoke.InvocationError, "blank line"):
            invoke.read_repository(client, client.issues[0]["body"])

    def test_repository_evidence_survives_narrow_grounding_scope(self):
        """PR #670 review finding (P1): a Grounding scope that excludes the
        repository's only policy-bearing file must not cause
        repository_evidence() to report empty facts. Evidence is computed
        from every JSON/executable-test-path file regardless of scope."""
        client = GroundingScopeClient(
            files=["product.md", "app.py", "policy.json"],
            contents={
                "app.py": "def handle(): pass",
                "policy.json": json.dumps({"factoryPolicy": {
                    "forbiddenDependencies": ["puppeteer"]}}),
            },
            # Scope deliberately excludes policy.json — only app.py is
            # declared as relevant grounding for the model.
            project_body="### Grounding scope\napp.py\n")
        _, _, repository = invoke.read_repository(client, client.issues[0]["body"])
        self.assertEqual(["app.py"], repository["grounded_files"])
        self.assertNotIn("policy.json", repository["sources"])
        self.assertEqual(["puppeteer"], repository["forbidden_dependencies"])

    def test_grounding_scope_rejects_over_complex_pattern_before_matching(self):
        """PR #670 review finding (security, P2): merge_gate's `**` matcher
        has exponential worst-case cost against a deep mismatching path —
        confirmed directly against the real matcher at ~10s for 12 `**`
        segments. This must be rejected before any match is attempted, not
        merely 'eventually' — so this test bounds wall-clock time, not just
        the raised error, using the same pathological shape (many `**`
        segments against a path deep enough to actually trigger the
        exponential branching, not a trivially short one)."""
        pattern = "/".join(["**"] * 12 + ["x.py"])
        deep_path = "/".join(["seg"] * 12 + ["z.py"])
        client = GroundingScopeClient(
            files=["product.md", deep_path], contents={deep_path: "z = 1"},
            project_body=f"### Grounding scope\n{pattern}\n")
        started = time.monotonic()
        with self.assertRaisesRegex(invoke.InvocationError, "too many \\*\\* segments"):
            invoke.read_repository(client, client.issues[0]["body"])
        self.assertLess(time.monotonic() - started, 1.0,
                        "rejection must be immediate, not after attempting a match")

    def test_campaign_trigger_ignores_grounding_scope_shaped_section(self):
        """PR #670 review finding: a `### Grounding scope`-shaped section in
        a campaign (`type:roadmap-commitment`) issue's body must never narrow
        or fail campaign planning, which surveys the whole repository to
        propose a Project in the first place. Before the fix, this raised
        InvocationError('grounding scope matched no files') instead of
        completing normally."""
        client, (state, registry) = CampaignWithScopeLikeSectionClient(), capacity()
        runner = mock.Mock(return_value=Result(stdout=json.dumps(campaign_output())))
        try:
            with mock.patch.object(invoke.artifacts, "GitHubStore", return_value=client):
                result = invoke.execute("o/r", 1, "token", 30, 2.5, runner=runner,
                                        state=state, registry=registry)
        finally:
            state.close()
        self.assertEqual("campaign", result.altitude.value)

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

        def read_with_facts(store, project_body=None):
            product, adrs, repository = original_read(store, project_body)
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
