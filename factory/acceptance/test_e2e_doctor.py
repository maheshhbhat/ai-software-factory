#!/usr/bin/env python3
"""Tests for the read-only E2E readiness doctor."""

from __future__ import annotations

import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e2e_doctor as doctor


def completed(args, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, code, stdout, stderr)


class RecordingRunner:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        return self.responses.get(tuple(args), completed(args))


class SafetyTests(unittest.TestCase):
    def test_explicit_repo_is_bound_into_receipt_configuration(self):
        value = doctor.Doctor(
            "owner/product", 60, commitment=59, target="", mode="preplanned",
            environ={"FACTORY_REPO": "stale/other"})
        self.assertEqual("owner/product", value.env["FACTORY_REPO"])
        doctor_fingerprint = doctor.readiness_receipt.configuration_fingerprint(
            value.env)
        poller_fingerprint = doctor.readiness_receipt.configuration_fingerprint(
            {"FACTORY_REPO": "owner/product"})
        self.assertEqual(poller_fingerprint, doctor_fingerprint)

    def test_successful_main_writes_scoped_receipt(self):
        checks = [doctor.Check("probe", True, "answered")]
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "ready.json"
            with mock.patch.object(doctor, "Doctor") as doctor_class, \
                 mock.patch.object(doctor.readiness_receipt, "factory_revision",
                                   return_value="a" * 40):
                doctor_class.return_value.run.return_value = checks
                doctor_class.return_value.env = {
                    "FACTORY_WORKER_ORDER": "capacity-delivery"}
                code = doctor.main(["--repo", "owner/repo", "--project", "47",
                                    "--commitment", "45", "--target", "app.js",
                                    "--receipt", str(path)])
            self.assertEqual(0, code)
            self.assertTrue(path.is_file())
            value = doctor.readiness_receipt.validate(
                path, repo="owner/repo", commitment=45, revision="a" * 40,
                environ={"FACTORY_WORKER_ORDER": "capacity-delivery"})
            self.assertEqual(47, value["project"])

    def test_failed_main_does_not_write_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "ready.json"
            with mock.patch.object(doctor, "Doctor") as doctor_class:
                doctor_class.return_value.run.return_value = [
                    doctor.Check("probe", False, "failed")]
                code = doctor.main(["--repo", "owner/repo", "--project", "47",
                                    "--commitment", "45", "--target", "app.js",
                                    "--receipt", str(path)])
            self.assertEqual(1, code)
            self.assertFalse(path.exists())

    def test_mutating_commands_are_refused_before_the_runner(self):
        runner = RecordingRunner()
        value = doctor.Doctor("owner/repo", 400, commitment=384,
                              target="runs/rung1/live_product/project-400/app.py",
                              environ={"PATH":"/bin"}, runner=runner)
        for command in (["gh","api","graphql","-f","query=mutation { x }"],
                        ["git","push","origin","main"],
                        ["sh","poll.sh","--claim"]):
            with self.assertRaisesRegex(RuntimeError,"refused mutating"):
                value.command(command)
        self.assertEqual([], runner.calls)

    def test_dry_run_uses_only_the_normal_entrypoint(self):
        runner = RecordingRunner({("sh","poll.sh","--once","--dry-run"):
                                  completed([],stdout=(
                                      "Dispatcher — 0 issue(s) considered, WIP 0/2\n"
                                      "No eligible work — nothing dispatched."))})
        value = doctor.Doctor("owner/repo",400,commitment=777,
                              target="runs/rung1/live_product/project-400/app.py",
                              environ={"PATH":"/bin"},runner=runner)
        value.token="token"
        value.dry_run()
        args, kwargs = runner.calls[0]
        self.assertEqual(["sh","poll.sh","--once","--dry-run"],args)
        self.assertEqual("777",kwargs["env"]["FACTORY_COMMITMENT"])
        self.assertTrue(value.checks[-1].passed)

    def test_dry_run_blocks_when_global_worker_capacity_is_full(self):
        runner = RecordingRunner({("sh","poll.sh","--once","--dry-run"):
                                  completed([],stdout=(
                                      "Dispatcher — 3 issue(s) considered, WIP 2/2\n"
                                      "No eligible work — nothing dispatched."))})
        value = doctor.Doctor(
            "owner/repo", 400, commitment=777,
            target="runs/rung1/live_product/project-400/app.py",
            environ={"PATH": "/bin"}, runner=runner)
        value.token = "token"
        value.dry_run()
        self.assertFalse(value.checks[-1].passed)
        self.assertIn("capacity exhausted", value.checks[-1].detail)

    def test_source_contains_no_github_mutation_operation(self):
        source=pathlib.Path(doctor.__file__).read_text()
        before, rest = source.split("MUTATING_WORDS =",1)
        _allowlist, after = rest.split("\n\n",1)
        executable = before + after
        for operation in ('method="POST"','method="PATCH"','method="DELETE"',
                          '"createIssue"','"updateIssue"','"addComment"'):
            self.assertNotIn(operation,executable)


class GitHubChecks(unittest.TestCase):
    @staticmethod
    def resumed_payload():
        project_body = ("### Stories\n\n#69\n#70\n#71\n\n"
                        "### Roadmap commitment\n\n#66\n")
        return {"data":{"repository":{"isPrivate":False,"viewerPermission":"ADMIN",
          "autoMergeAllowed":True,
          "project":{"number":67,"state":"OPEN","body":project_body,
                     "labels":{"nodes":[{"name":"type:project"},{"name":"project:active"}]}},
          "commitment":{"number":66,"state":"OPEN","body":"real product outcome",
                        "labels":{"nodes":[{"name":"type:roadmap-commitment"}]}},
          "issues":{"nodes":[
              {"number":67,"state":"OPEN","body":"### Roadmap commitment\n\n#66\n",
               "labels":{"nodes":[{"name":"type:project"}]}},
              {"number":69,"state":"CLOSED",
               "body":"### Project\n\n#67\n\n### Depends-on\n\nnone\n",
               "labels":{"nodes":[{"name":"type:story"},{"name":"story:merged"}]}},
              {"number":70,"state":"OPEN",
               "body":"### Project\n\n#67\n\n### Depends-on\n\n#69\n",
               "labels":{"nodes":[{"name":"type:story"},{"name":"story:ready"}]}},
              {"number":71,"state":"OPEN",
               "body":"### Project\n\n#67\n\n### Depends-on\n\n#70\n",
               "labels":{"nodes":[{"name":"type:story"},{"name":"story:blocked"}]}}],
              "pageInfo":{"hasNextPage":False}},
          "defaultBranchRef":{"branchProtectionRule":{
              "requiredStatusCheckContexts":["merge-gate"]}}},
          "rateLimit":{"remaining":4999,"resetAt":"later"}}}

    @staticmethod
    def github_runner(payload):
        def respond(args, **_):
            if "issues?state=open" in args[-1]:
                return completed(args, stdout="[]")
            if args[-1].endswith("/rulesets"):
                return completed(args, stdout="[]")
            return completed(args, stdout=json.dumps(payload))
        return respond

    def test_resume_mode_accepts_exact_merged_ready_blocked_topology(self):
        value = doctor.Doctor(
            "owner/repo", 67, commitment=66, target="", mode="resume",
            environ={"PATH":"/bin"}, runner=self.github_runner(self.resumed_payload()))
        value.token = "token"; value.github()
        self.assertTrue(all(check.passed for check in value.checks), value.checks)
        check = next(row for row in value.checks
                     if row.name == "isolated resumed Project")
        self.assertIn("stories=[69, 70, 71]", check.detail)

    def test_resume_mode_accepts_no_deliverable_terminal_success(self):
        payload = self.resumed_payload()
        payload["data"]["repository"]["issues"]["nodes"][1]["labels"]["nodes"][1]["name"] = \
            "story:completed"
        value = doctor.Doctor(
            "owner/repo", 67, commitment=66, target="", mode="resume",
            environ={"PATH":"/bin"}, runner=self.github_runner(payload))
        value.token = "token"; value.github()
        check = next(row for row in value.checks
                     if row.name == "isolated resumed Project")
        self.assertTrue(check.passed, value.checks)

    def test_preplanned_mode_still_rejects_started_resume_topology(self):
        value = doctor.Doctor(
            "owner/repo", 67, commitment=66, target="", mode="preplanned",
            environ={"PATH":"/bin"}, runner=self.github_runner(self.resumed_payload()))
        value.token = "token"; value.github()
        check = next(row for row in value.checks
                     if row.name == "isolated preplanned Project")
        self.assertFalse(check.passed)

    def test_resume_mode_fails_closed_for_incomplete_or_invalid_topology(self):
        variants = {}
        missing = self.resumed_payload()
        missing["data"]["repository"]["issues"]["nodes"] = [
            item for item in missing["data"]["repository"]["issues"]["nodes"]
            if item["number"] != 69]
        variants["missing completed Story"] = missing
        two_ready = self.resumed_payload()
        two_ready["data"]["repository"]["issues"]["nodes"][3]["labels"]["nodes"][1]["name"] = "story:ready"
        variants["two ready Stories"] = two_ready
        open_completed = self.resumed_payload()
        open_completed["data"]["repository"]["issues"]["nodes"][1]["state"] = "OPEN"
        variants["terminal-success Story left open"] = open_completed
        closed_blocked = self.resumed_payload()
        closed_blocked["data"]["repository"]["issues"]["nodes"][3]["state"] = "CLOSED"
        variants["nonterminal Story closed"] = closed_blocked
        unmet = self.resumed_payload()
        unmet["data"]["repository"]["issues"]["nodes"][2]["body"] = \
            "### Project\n\n#67\n\n### Depends-on\n\n#71\n"
        variants["ready dependency is not completed"] = unmet
        cyclic = copy.deepcopy(unmet)
        cyclic["data"]["repository"]["issues"]["nodes"][3]["body"] = \
            "### Project\n\n#67\n\n### Depends-on\n\n#70\n"
        variants["cyclic dependencies"] = cyclic
        for name, payload in variants.items():
            with self.subTest(name=name):
                value = doctor.Doctor(
                    "owner/repo", 67, commitment=66, target="", mode="resume",
                    environ={"PATH":"/bin"}, runner=self.github_runner(payload))
                value.token = "token"; value.github()
                check = next(row for row in value.checks
                             if row.name == "isolated resumed Project")
                self.assertFalse(check.passed)

    def test_preplanned_mode_requires_exact_declared_unstarted_stories(self):
        project_body = """### Stories

#402
#403

### Roadmap commitment

#384
"""
        payload={"data":{"repository":{"isPrivate":False,"viewerPermission":"ADMIN",
          "autoMergeAllowed":True,
          "project":{"number":400,"state":"OPEN","body":project_body,
                     "labels":{"nodes":[{"name":"type:project"},{"name":"project:active"}]}},
          "commitment":{"number":384,"state":"OPEN","body":"real product outcome",
                        "labels":{"nodes":[{"name":"type:roadmap-commitment"}]}},
          "issues":{"nodes":[
              {"number":400,"body":"### Roadmap commitment\n\n#384\n",
               "labels":{"nodes":[{"name":"type:project"}]}},
              {"number":402,"body":"### Project\n\n#400\n",
               "labels":{"nodes":[{"name":"type:story"},{"name":"story:ready"}]}},
              {"number":403,"body":"### Project\n\n#400\n",
               "labels":{"nodes":[{"name":"type:story"},{"name":"story:blocked"}]}}],
              "pageInfo":{"hasNextPage":False}},
          "defaultBranchRef":{"branchProtectionRule":{
              "requiredStatusCheckContexts":["merge-gate"]}}},
          "rateLimit":{"remaining":4999,"resetAt":"later"}}}
        def respond(args, **_):
            if "issues?state=open" in args[-1]:
                return completed(args, stdout="[]")
            if args[-1].endswith("/rulesets"):
                return completed(args, stdout="[]")
            return completed(args, stdout=json.dumps(payload))
        value=doctor.Doctor("owner/repo",400,commitment=384,target="",
                            mode="preplanned",environ={"PATH":"/bin"},runner=respond)
        value.token="token"; value.github()
        self.assertTrue(all(check.passed for check in value.checks), value.checks)
        self.assertNotIn("test-only commitment", [row.name for row in value.checks])

    def test_preplanned_mode_blocks_undeclared_or_started_story(self):
        project_body = "### Stories\n\n#402\n\n### Roadmap commitment\n\n#384\n"
        payload={"data":{"repository":{"isPrivate":False,"viewerPermission":"ADMIN",
          "autoMergeAllowed":True,
          "project":{"number":400,"state":"OPEN","body":project_body,
                     "labels":{"nodes":[{"name":"type:project"},{"name":"project:active"}]}},
          "commitment":{"number":384,"state":"OPEN","body":"real product outcome",
                        "labels":{"nodes":[{"name":"type:roadmap-commitment"}]}},
          "issues":{"nodes":[
              {"number":400,"body":"### Roadmap commitment\n\n#384\n",
               "labels":{"nodes":[{"name":"type:project"}]}},
              {"number":402,"body":"### Project\n\n#400\n",
               "labels":{"nodes":[{"name":"type:story"},{"name":"story:claimed"}]}},
              {"number":403,"body":"### Project\n\n#400\n",
               "labels":{"nodes":[{"name":"type:story"},{"name":"story:blocked"}]}}],
              "pageInfo":{"hasNextPage":False}},
          "defaultBranchRef":{"branchProtectionRule":{
              "requiredStatusCheckContexts":["merge-gate"]}}},
          "rateLimit":{"remaining":4999,"resetAt":"later"}}}
        def respond(args, **_):
            if "issues?state=open" in args[-1]: return completed(args, stdout="[]")
            if args[-1].endswith("/rulesets"): return completed(args, stdout="[]")
            return completed(args, stdout=json.dumps(payload))
        value=doctor.Doctor("owner/repo",400,commitment=384,target="",
                            mode="preplanned",environ={"PATH":"/bin"},runner=respond)
        value.token="token"; value.github()
        check=next(row for row in value.checks
                   if row.name=="isolated preplanned Project")
        self.assertFalse(check.passed)
        self.assertIn("declared=[402]", check.detail)

    def test_preplanned_mode_requires_human_approved_project(self):
        payload={"data":{"repository":{"isPrivate":False,"viewerPermission":"ADMIN",
          "autoMergeAllowed":True,
          "project":{"number":400,"state":"OPEN",
                     "body":"### Stories\n\n#402\n\n### Roadmap commitment\n\n#384\n",
                     "labels":{"nodes":[{"name":"type:project"},
                                          {"name":"project:awaiting-ready"}]}},
          "commitment":{"number":384,"state":"OPEN","body":"real product outcome",
                        "labels":{"nodes":[{"name":"type:roadmap-commitment"}]}},
          "issues":{"nodes":[
              {"number":400,"body":"### Roadmap commitment\n\n#384\n",
               "labels":{"nodes":[{"name":"type:project"}]}},
              {"number":402,"body":"### Project\n\n#400\n",
               "labels":{"nodes":[{"name":"type:story"},
                                    {"name":"story:blocked"}]}}],
              "pageInfo":{"hasNextPage":False}},
          "defaultBranchRef":{"branchProtectionRule":{
              "requiredStatusCheckContexts":["merge-gate"]}}},
          "rateLimit":{"remaining":4999,"resetAt":"later"}}}
        def respond(args, **_):
            if "issues?state=open" in args[-1]: return completed(args, stdout="[]")
            if args[-1].endswith("/rulesets"): return completed(args, stdout="[]")
            return completed(args, stdout=json.dumps(payload))
        value=doctor.Doctor("owner/repo",400,commitment=384,target="",
                            mode="preplanned",environ={"PATH":"/bin"},runner=respond)
        value.token="token"; value.github()
        check=next(row for row in value.checks if row.name=="Project authorization")
        self.assertFalse(check.passed)

    def test_disabled_repository_auto_merge_blocks_readiness(self):
        payload={"data":{"repository":{"isPrivate":False,"viewerPermission":"ADMIN",
          "autoMergeAllowed":False,
          "project":{"number":400,"state":"OPEN","body":"### Roadmap commitment\n\n#384\n",
                     "labels":{"nodes":[{"name":"type:project"},{"name":"project:active"}]}},
          "commitment":{"number":384,"state":"OPEN",
                        "body":"No product or factory implementation work",
                        "labels":{"nodes":[{"name":"type:roadmap-commitment"}]}},
          "issues":{"nodes":[
              {"number":400,"body":"### Roadmap commitment\n\n#384\n",
               "labels":{"nodes":[{"name":"type:project"}]}}],
              "pageInfo":{"hasNextPage":False}},
          "defaultBranchRef":{"branchProtectionRule":{
              "requiredStatusCheckContexts":["merge-gate"]}}},
          "rateLimit":{"remaining":4999,"resetAt":"later"}}}
        def respond(args, **_):
            if args[-1].endswith("/rulesets"):
                return completed(args,stdout="[]")
            return completed(args,stdout=json.dumps(payload))
        value=doctor.Doctor("owner/repo",400,commitment=384,
                            target="runs/rung1/live_product/project-400/app.py",
                            environ={"PATH":"/bin"},runner=respond)
        value.token="token"; value.github()
        check=next(row for row in value.checks if row.name=="repository auto-merge")
        self.assertFalse(check.passed)
        self.assertEqual("disabled", check.detail)

    def test_authorization_branch_protection_and_capacity_are_checked(self):
        payload={"data":{"repository":{"isPrivate":True,"viewerPermission":"ADMIN",
          "autoMergeAllowed":True,
          "project":{"number":400,"state":"OPEN","body":"### Roadmap commitment\n\n#384\n",
                     "labels":{"nodes":[{"name":"type:project"},{"name":"project:awaiting-ready"}]}},
          "commitment":{"number":384,"state":"OPEN",
                        "body":"No product or factory implementation work may descend from this commitment.",
                        "labels":{"nodes":[{"name":"type:roadmap-commitment"}]}},
          "issues":{"nodes":[
              {"number":400,"body":"### Roadmap commitment\n\n#384\n",
               "labels":{"nodes":[{"name":"type:project"}]}}],
              "pageInfo":{"hasNextPage":False}},
          "defaultBranchRef":{"branchProtectionRule":{"requiresStatusChecks":True,
                                "requiredStatusCheckContexts":["merge-gate"]}}},
          "rateLimit":{"remaining":4999,"resetAt":"later"}}}
        rulesets=[{"id":7,"enforcement":"active"}]
        ruleset={"rules":[{"type":"required_status_checks","parameters":{
                 "required_status_checks":[{"context":"merge-gate"}]}}]}
        runner=RecordingRunner()
        def respond(args,**kwargs):
            runner.calls.append((list(args),kwargs))
            if args[-1]=="repos/owner/repo/rulesets": return completed(args,stdout=json.dumps(rulesets))
            if args[-1]=="repos/owner/repo/rulesets/7": return completed(args,stdout=json.dumps(ruleset))
            return completed(args,stdout=json.dumps(payload))
        value=doctor.Doctor("owner/repo",400,commitment=384,
                            target="runs/rung1/live_product/project-400/app.py",
                            environ={"PATH":"/bin"},runner=respond)
        value.token="token"; value.github()
        self.assertTrue(all(check.passed for check in value.checks),value.checks)
        self.assertTrue(all("mutation" not in " ".join(call[0]) for call in runner.calls))

    def test_real_rest_failure_blocks_even_when_graphql_capacity_is_healthy(self):
        payload={"data":{"repository":{"isPrivate":False,"viewerPermission":"ADMIN",
          "autoMergeAllowed":True,
          "project":{"number":400,"state":"OPEN","body":"### Roadmap commitment\n\n#384\n",
                     "labels":{"nodes":[{"name":"type:project"},{"name":"project:active"}]}},
          "commitment":{"number":384,"state":"OPEN",
                        "body":"No product or factory implementation work",
                        "labels":{"nodes":[{"name":"type:roadmap-commitment"}]}},
          "issues":{"nodes":[
              {"number":400,"body":"### Roadmap commitment\n\n#384\n",
               "labels":{"nodes":[{"name":"type:project"}]}}],
              "pageInfo":{"hasNextPage":False}},
          "defaultBranchRef":{"branchProtectionRule":{"requiredStatusCheckContexts":["merge-gate"]}}},
          "rateLimit":{"remaining":4999,"resetAt":"later"}}}
        def respond(args,**_):
            if "issues?state=open" in args[-1]:
                return completed(args,code=1,stderr="API rate limit exceeded")
            if args[-1].endswith("/rulesets"):
                return completed(args,stdout="[]")
            return completed(args,stdout=json.dumps(payload))
        value=doctor.Doctor("owner/repo",400,commitment=384,
                            target="runs/rung1/live_product/project-400/app.py",
                            environ={"PATH":"/bin"},runner=respond)
        value.token="token"; value.github()
        check=next(row for row in value.checks if row.name=="production REST read path")
        self.assertFalse(check.passed)
        self.assertIn("rate limit",check.detail)

    def test_existing_project_or_story_blocks_commitment_isolation(self):
        payload={"data":{"repository":{"isPrivate":False,"viewerPermission":"ADMIN",
          "autoMergeAllowed":True,
          "project":{"number":400,"state":"OPEN","body":"### Roadmap commitment\n\n#384\n",
                     "labels":{"nodes":[{"name":"type:project"},{"name":"project:active"}]}},
          "commitment":{"number":384,"state":"OPEN",
                        "body":"No product or factory implementation work",
                        "labels":{"nodes":[{"name":"type:roadmap-commitment"}]}},
          "issues":{"nodes":[
              {"number":400,"body":"### Roadmap commitment\n\n#384\n",
               "labels":{"nodes":[{"name":"type:project"}]}},
              {"number":401,"body":"### Roadmap commitment\n\n#384\n",
               "labels":{"nodes":[{"name":"type:project"}]}},
              {"number":402,"body":"### Project\n\n#400\n",
               "labels":{"nodes":[{"name":"type:story"}]}}],
              "pageInfo":{"hasNextPage":False}},
          "defaultBranchRef":{"branchProtectionRule":{"requiredStatusCheckContexts":["merge-gate"]}}},
          "rateLimit":{"remaining":4999,"resetAt":"later"}}}
        def respond(args, **_):
            if "issues?state=open" in args[-1]:
                return completed(args, stdout="[]")
            if args[-1].endswith("/rulesets"):
                return completed(args, stdout="[]")
            return completed(args, stdout=json.dumps(payload))
        value=doctor.Doctor("owner/repo",400,commitment=384,
                            target="runs/rung1/live_product/project-400/app.py",
                            environ={"PATH":"/bin"},runner=respond)
        value.token="token"; value.github()
        check=next(row for row in value.checks if row.name=="isolated test commitment")
        self.assertFalse(check.passed)
        self.assertIn("projects=[400, 401]", check.detail)
        self.assertIn("stories=[402]", check.detail)


class LocalChecks(unittest.TestCase):
    def setUp(self):
        self.capacity_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.capacity_directory.cleanup)
        self.capacity_path = str(
            pathlib.Path(self.capacity_directory.name) / "capacity.sqlite")

    def environment(self):
        return {"PATH": "/bin", "FACTORY_CAPACITY_STATE": self.capacity_path}

    def test_every_configured_capacity_is_probed_independently(self):
        calls = []
        def respond(args, **_):
            calls.append(args)
            return completed(args, stdout="CAPACITY_OK")
        value = doctor.Doctor(
            "owner/repo", 400, commitment=384, target="product.js",
            environ=self.environment(),
            runner=respond)
        value.worker_engine_start()
        # One probe per registry entry that is enabled without environment
        # configuration — count it from the registry itself so adding a model
        # does not silently rot this test.
        enabled = [item for item in doctor.resolved_registry(self.environment())
                   if item.available]
        probes = [row for row in value.checks if row.name.startswith("capacity probe")]
        self.assertEqual(len(enabled), len(probes))
        self.assertTrue(all(row.passed for row in probes))
        self.assertEqual(len(enabled), len(calls))
        persisted = doctor.CapacityState(self.capacity_path, uri=False)
        try:
            self.assertTrue(all(
                persisted.health(item.provider, item.name)["state"] == "healthy"
                for item in enabled))
            worker_view = doctor.resolved_registry(
                self.environment(), health=persisted.health)
            self.assertEqual(
                {item.name for item in enabled},
                {item.name for item in worker_view if item.available})
        finally:
            persisted.close()

    def test_one_provider_success_does_not_hide_another_failure(self):
        count = 0
        def respond(args, **_):
            nonlocal count
            count += 1
            return completed(args, stdout="CAPACITY_OK" if count == 1 else "wrong")
        runner = RecordingRunner()
        value = doctor.Doctor(
            "owner/repo", 400, commitment=384, target="product.js",
            environ=self.environment(), runner=respond)
        value.worker_engine_start()
        probes = [row for row in value.checks if row.name.startswith("capacity probe")]
        self.assertTrue(probes[0].passed)
        self.assertTrue(any(not row.passed for row in probes[1:]))

    def test_echoing_probe_prompt_is_not_success(self):
        prompt_echo = completed([], stdout="Reply exactly CAPACITY_OK")
        value = doctor.Doctor(
            "owner/repo", 400, commitment=384, target="product.js",
            environ=self.environment(), runner=lambda args, **kwargs: prompt_echo)
        value.worker_engine_start()
        self.assertFalse(value.checks[-1].passed)

    def test_worktree_creation_failure_blocks_readiness(self):
        def respond(args, **_):
            if args[:4] == ["git", "worktree", "add", "--detach"]:
                return completed(args, code=128,
                                 stderr="fatal: cannot create .git/worktrees: Operation not permitted")
            self.fail(f"unexpected command: {args}")

        value = doctor.Doctor("owner/repo", 400, commitment=384,
                              target="runs/rung1/live_product/project-400/app.py",
                              environ={"PATH": "/bin"},
                              runner=respond)
        value.worktree()
        self.assertFalse(value.checks[-1].passed)
        self.assertIn("Operation not permitted", value.checks[-1].detail)

    def test_worktree_probe_removes_successful_probe(self):
        runner = RecordingRunner()
        value = doctor.Doctor("owner/repo", 400, commitment=384,
                              target="runs/rung1/live_product/project-400/app.py",
                              environ={"PATH": "/bin"},
                              runner=runner)
        value.worktree()
        self.assertTrue(value.checks[-1].passed)
        commands = [call[0] for call in runner.calls]
        self.assertEqual(["git", "worktree", "add", "--detach"], commands[0][:4])
        self.assertEqual(["git", "worktree", "remove", "--force"], commands[1][:4])
        self.assertEqual(commands[0][4], commands[1][4])

    def test_substitution_overrides_block_readiness(self):
        value=doctor.Doctor("owner/repo",400,commitment=384,
                            target="runs/rung1/live_product/project-400/app.py",
                            environ={"FACTORY_DELIVERY_MODEL_CMD":"fake"})
        value.substitutions()
        self.assertFalse(value.checks[0].passed)
        self.assertIn("FACTORY_DELIVERY_MODEL_CMD",value.checks[0].detail)

    def test_preplanned_mode_requires_clean_exact_origin_main(self):
        responses = {
            ("git", "status", "--porcelain"): completed([], stdout=""),
            ("git", "rev-parse", "HEAD"): completed([], stdout="a" * 40 + "\n"),
            ("git", "rev-parse", "origin/main"): completed([], stdout="a" * 40 + "\n"),
            ("pgrep", "-f", "[f]actory/runtime/poller.py"):
                completed([], code=1),
        }
        value=doctor.Doctor("owner/repo",400,commitment=384,target="",
                            mode="preplanned",environ={"PATH":"/bin"},
                            runner=RecordingRunner(responses))
        with mock.patch.object(doctor.shutil, "which", return_value="/bin/tool"):
            value.local()
        check=next(row for row in value.checks
                   if row.name=="clean merged factory revision")
        self.assertTrue(check.passed, check)

    def test_preplanned_mode_blocks_dirty_or_unmerged_revision(self):
        responses = {
            ("git", "status", "--porcelain"):
                completed([], stdout=" M factory/acceptance/e2e_doctor.py\n"),
            ("git", "rev-parse", "HEAD"): completed([], stdout="a" * 40 + "\n"),
            ("git", "rev-parse", "origin/main"): completed([], stdout="b" * 40 + "\n"),
            ("pgrep", "-f", "[f]actory/runtime/poller.py"):
                completed([], code=1),
        }
        value=doctor.Doctor("owner/repo",400,commitment=384,target="",
                            mode="preplanned",environ={"PATH":"/bin"},
                            runner=RecordingRunner(responses))
        with mock.patch.object(doctor.shutil, "which", return_value="/bin/tool"):
            value.local()
        check=next(row for row in value.checks
                   if row.name=="clean merged factory revision")
        self.assertFalse(check.passed)

    def test_observability_smoke_writes_all_streams_and_a_heartbeat(self):
        value=doctor.Doctor("owner/repo",400,commitment=384,
                            target="runs/rung1/live_product/project-400/app.py",
                            environ=dict(os.environ))
        value.observability()
        self.assertTrue(value.checks[-1].passed,value.checks[-1])

    def test_existing_product_target_blocks_readiness(self):
        target = "runs/rung1/live_product/project-400/app.py"
        runner = RecordingRunner({
            ("gh", "api", f"repos/owner/repo/contents/{target}?ref=main"):
                completed([], stdout='{"type":"file"}'),
        })
        value = doctor.Doctor("owner/repo", 400, commitment=384,
                              target=target, environ={"PATH": "/bin"},
                              runner=runner)
        value.token = "token"
        value.target_freshness()
        self.assertFalse(value.checks[-1].passed)
        self.assertIn("already exists", value.checks[-1].detail)

    def test_absent_product_target_passes_readiness(self):
        target = "runs/rung1/live_product/project-400/app.py"
        runner = RecordingRunner({
            ("gh", "api", f"repos/owner/repo/contents/{target}?ref=main"):
                completed([], code=1, stderr="HTTP 404: Not Found"),
        })
        value = doctor.Doctor("owner/repo", 400, commitment=384,
                              target=target, environ={"PATH": "/bin"},
                              runner=runner)
        value.token = "token"
        value.target_freshness()
        self.assertTrue(value.checks[-1].passed)

    def test_render_is_a_clear_blocking_verdict(self):
        text=doctor.render([doctor.Check("one",True,"ok"),
                            doctor.Check("two",False,"broken")])
        self.assertIn("PASS  one",text)
        self.assertIn("FAIL  two",text)
        self.assertIn("BLOCKED — fix 1 failure",text)

    def test_preplanned_cli_does_not_require_target_and_binds_project_identity(self):
        checks = [doctor.Check("probe", True, "answered")]
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "ready.json"
            with mock.patch.object(doctor, "Doctor") as doctor_class, \
                 mock.patch.object(doctor.readiness_receipt, "factory_revision",
                                   return_value="a" * 40):
                doctor_class.return_value.run.return_value = checks
                doctor_class.return_value.env = {}
                code = doctor.main([
                    "--repo", "owner/repo", "--project", "60",
                    "--commitment", "59", "--mode", "preplanned",
                    "--receipt", str(path)])
            self.assertEqual(0, code)
            self.assertEqual("project:60", json.loads(path.read_text())["target"])
            self.assertEqual("preplanned", doctor_class.call_args.kwargs["mode"])

    def test_rehearsal_cli_still_requires_target(self):
        with self.assertRaises(SystemExit):
            doctor.main(["--project", "400", "--commitment", "384"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
