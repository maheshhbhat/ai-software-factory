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

        def read_with_facts(store):
            product, adrs, repository = original_read(store)
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
