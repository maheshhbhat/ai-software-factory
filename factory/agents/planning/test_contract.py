import json
import pathlib
import re
import unittest

import contract


class AltitudeTests(unittest.TestCase):
    def test_codex_structured_output_discriminators_have_explicit_types(self):
        for altitude in contract.Altitude:
            pending = [contract.json_schema(altitude)]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if "const" in value or "enum" in value:
                        self.assertIn("type", value)
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)

    def test_codex_structured_output_objects_are_closed_and_fully_required(self):
        """The Codex structured-output boundary rejects dynamic object keys."""
        for altitude in contract.Altitude:
            pending = [contract.json_schema(altitude)]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if value.get("type") == "object":
                        self.assertIs(value.get("additionalProperties"), False)
                        self.assertEqual(set(value.get("properties", {})),
                                         set(value.get("required", [])))
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)

    def test_trigger_type_selects_altitude(self):
        self.assertEqual(contract.Altitude.CAMPAIGN,
                         contract.select_altitude({"type:roadmap-commitment"}))
        self.assertEqual(contract.Altitude.PROJECT,
                         contract.select_altitude({"type:project"}))

    def test_missing_unsupported_or_conflicting_type_fails(self):
        for labels in (set(), {"type:story"},
                       {"type:project", "type:roadmap-commitment"}):
            with self.subTest(labels=labels), self.assertRaises(contract.ContractError):
                contract.select_altitude(labels)


class InputTests(unittest.TestCase):
    def valid(self):
        return {"trigger": {"labels": ["type:project"]},
                "product": "# Product", "adrs": [],
                "repository": {"files": ["product.md"]}, "review_comments": [],
                "existing_plan": {}}

    def test_all_grounding_inputs_are_required(self):
        for key in contract.REQUIRED_INPUTS:
            value = self.valid()
            del value[key]
            with self.subTest(key=key), self.assertRaises(contract.ContractError):
                contract.validate_input(value)

    def test_empty_product_or_repository_fails(self):
        for key, value in (("product", ""), ("repository", {})):
            data = self.valid()
            data[key] = value
            with self.subTest(key=key), self.assertRaises(contract.ContractError):
                contract.validate_input(data)


class OutputTests(unittest.TestCase):
    DIGEST = """## Plan in plain language

Plain explanation.

## How the plan works

```mermaid
flowchart LR
  a --> b
```

Text fallback.

## Story dependencies

```mermaid
flowchart LR
  story_a --> story_b
```

Text fallback."""

    def test_campaign_schema_excludes_project_artifacts(self):
        value = {"altitude": "campaign", "project": {}, "rationale": "why",
                 "risks": []}
        self.assertIs(value, contract.validate_output(contract.Altitude.CAMPAIGN, value))
        value["stories"] = []
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.CAMPAIGN, value)

    def test_project_schema_excludes_campaign_proposal(self):
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [], "adr": {}, "stories": [],
                 "expected_bells": 2, "risks": "risk", "digest": self.DIGEST}
        self.assertIs(value, contract.validate_output(contract.Altitude.PROJECT, value))
        value["project"] = {}
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.PROJECT, value)

    def test_every_envelope_assignment_requires_exact_story_local_check(self):
        story = {"key": "core", "title": "Core", "spec": "bounded",
                 "phase": "build", "depends_on": [], "hazard": False,
                 "acceptance_criteria": ["check"],
                 "operating_envelope_ids": ["OE-BROWSER-1"],
                 "operating_envelope_checks": [], "scope": ["src/core.js"],
                 "spend_cap": "$5 / 60 min"}
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [{"id": "OE-BROWSER-1",
                     "category": "responsiveness", "requirement": "render promptly",
                     "failure_condition": "browser render exceeds bound"}],
                 "adr": {}, "stories": [story], "expected_bells": 2,
                 "risks": "risk", "digest": self.DIGEST}
        with self.assertRaisesRegex(contract.ContractError, "Story-local executable"):
            contract.validate_output(contract.Altitude.PROJECT, value)
        story["title"] = "Browser core"
        story["operating_envelope_checks"] = [{
            "id": "OE-BROWSER-1", "check": "browser render exceeds bound"}]
        self.assertIs(value, contract.validate_output(contract.Altitude.PROJECT, value))
        story["operating_envelope_checks"].append({
            "id": "OE-EXTRA", "check": "unassigned check"})
        with self.assertRaisesRegex(contract.ContractError, "Story-local executable"):
            contract.validate_output(contract.Altitude.PROJECT, value)

    def test_project60_core_story_cannot_own_browser_wide_obligations(self):
        story = {"key": "scenario_engine", "title": "Scenario engine",
                 "spec": "Build a pure deterministic calculation API.",
                 "phase": "build", "depends_on": [], "hazard": False,
                 "acceptance_criteria": ["pure calculation tests pass"],
                 "operating_envelope_ids": ["OE-RESP-1", "OE-DEGRADE-1"],
                 "operating_envelope_checks": [
                     {"id": "OE-RESP-1", "check": "Chrome render exceeds one second"},
                     {"id": "OE-DEGRADE-1", "check": "invalid page leaves stale UI visible"}],
                 "scope": ["src/scenario-engine.js"], "spend_cap": "$5 / 60 min"}
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [
                     {"id": "OE-RESP-1", "category": "responsiveness",
                      "requirement": "browser renders within one second",
                      "failure_condition": "Chrome render exceeds one second"},
                     {"id": "OE-DEGRADE-1", "category": "degradation",
                      "requirement": "invalid browser input clears stale output",
                      "failure_condition": "page leaves stale output visible"}],
                 "adr": {}, "stories": [story], "expected_bells": 2,
                 "risks": "risk", "digest": self.DIGEST}
        with self.assertRaisesRegex(contract.ContractError, "cannot verify browser"):
            contract.validate_output(contract.Altitude.PROJECT, value)

        story["title"] = "Browser scenario assurance"
        story["spec"] = ("Use Playwright headless branded Chrome in GitHub Actions. "
                         "Fail on HTTP request failures, including favicon handling.")
        story["scope"] = ["src/browser-app.js", ".github/workflows/tests.yml"]
        self.assertIs(value, contract.validate_output(contract.Altitude.PROJECT, value))

    def test_named_browser_plan_blocks_raw_launchers_and_incomplete_assurance(self):
        story = {
            "key": "browser", "title": "Chrome browser assurance",
            "spec": ("Use Playwright headless Chrome in GitHub Actions; capture failed "
                     "requests and favicon handling."),
            "phase": "hardening", "depends_on": [], "hazard": True,
            "acceptance_criteria": ["Chrome page renders with zero console errors"],
            "operating_envelope_ids": [], "operating_envelope_checks": [],
            "scope": ["test/browser.test.js", ".github/workflows/tests.yml"],
            "spend_cap": "$5 / 60 min",
        }
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [], "adr": {}, "stories": [story],
                 "expected_bells": 2, "risks": "risk", "digest": self.DIGEST}
        self.assertIs(value, contract.validate_output(contract.Altitude.PROJECT, value))

        original = story["acceptance_criteria"]
        story["acceptance_criteria"] = [
            "Chrome page renders with zero console errors and must not use child_process"]
        self.assertIs(value, contract.validate_output(contract.Altitude.PROJECT, value))
        story["acceptance_criteria"] = original

        for fragment, message in (
            (" Launch Google Chrome.app with child_process spawn.", "raw browser launcher"),
            ("", None),
        ):
            if message:
                story["spec"] += fragment
                with self.assertRaisesRegex(contract.ContractError, message):
                    contract.validate_output(contract.Altitude.PROJECT, value)
                story["spec"] = story["spec"].removesuffix(fragment)

        for missing, message in (
            ("Playwright", "established browser-testing tool"),
            ("headless", "headless execution"),
            ("GitHub Actions", "supported CI or Linux runner"),
            ("favicon", "favicon handling"),
            ("failed requests", "failed page requests"),
        ):
            original = story["spec"]
            original_scope = story["scope"]
            story["spec"] = original.replace(missing, "")
            if missing == "GitHub Actions":
                story["scope"] = ["test/browser.test.js"]
            with self.subTest(missing=missing), self.assertRaisesRegex(
                    contract.ContractError, message):
                contract.validate_output(contract.Altitude.PROJECT, value)
            story["spec"] = original
            story["scope"] = original_scope

    def test_missing_or_mismatched_output_fails(self):
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.CAMPAIGN, {"altitude": "campaign"})
        project = {"altitude": "campaign", "operating_envelope": [],
                   "adr": {}, "stories": [],
                   "expected_bells": 2, "digest": self.DIGEST}
        with self.assertRaises(contract.ContractError):
            contract.validate_output(contract.Altitude.PROJECT, project)

    def test_project_digest_requires_plain_language_and_two_diagrams(self):
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [], "adr": {}, "stories": [],
                 "expected_bells": 2, "risks": "risk", "digest": "plan"}
        with self.assertRaisesRegex(contract.ContractError, "Plan in plain language"):
            contract.validate_output(contract.Altitude.PROJECT, value)

    def test_project_digest_requires_text_after_each_diagram(self):
        for missing, digest in (
            ("How the plan works", self.DIGEST.replace(
                "\n\nText fallback.\n\n## Story dependencies",
                "\n\n## Story dependencies")),
            ("Story dependencies", self.DIGEST.rsplit("\n\nText fallback.", 1)[0]),
        ):
            value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                     "operating_envelope": [], "adr": {}, "stories": [],
                     "expected_bells": 2, "risks": "risk", "digest": digest}
            with self.subTest(section=missing), self.assertRaisesRegex(
                    contract.ContractError, f"{missing!r} lacks a textual fallback"):
                contract.validate_output(contract.Altitude.PROJECT, value)

    def test_code_block_or_following_section_text_is_not_a_fallback(self):
        digest = self.DIGEST.replace(
            "Text fallback.\n\n## Story dependencies",
            "```text\nnot prose\n```\n\n## Story dependencies")
        value = {"altitude": "project", "acceptance_criteria": ["criterion"],
                 "operating_envelope": [], "adr": {}, "stories": [],
                 "expected_bells": 2, "risks": "risk", "digest": digest}
        with self.assertRaisesRegex(contract.ContractError,
                                    "How the plan works.*textual fallback"):
            contract.validate_output(contract.Altitude.PROJECT, value)


class AcceptanceVerificationTests(unittest.TestCase):
    DIGEST = OutputTests.DIGEST

    def record(self, **changes):
        executor = changes.get("executor", "test/app.test.js")
        record = {
            "type": "automated", "scope": "test/app.test.js",
            "executor": "test/app.test.js", "executor_source": "existing",
            "action": f"python3 -m unittest {executor}",
            "expected": "the named assertion passes",
            "failure": "the command exits nonzero",
        }
        record.update(changes)
        return "The disclosure is visible || VERIFY " + json.dumps(
            record, separators=(",", ":"), sort_keys=True)

    def plan(self, criterion=None, *, scope=None, expected_bells=2,
             envelope=None, checks=None):
        story = {
            "key": "disclosure", "title": "Allocation disclosure",
            "spec": "Change the disclosure.", "phase": "hardening",
            "depends_on": [], "hazard": False,
            "acceptance_criteria": [criterion or self.record()],
            "operating_envelope_ids": ["OE-1"] if envelope else [],
            "operating_envelope_checks": checks or [],
            "scope": scope or ["app.js", "test/app.test.js"],
            "spend_cap": "$5 / 60 min",
        }
        return {
            "altitude": "project", "acceptance_criteria": ["criterion"],
            "operating_envelope": envelope or [], "adr": {}, "stories": [story],
            "expected_bells": expected_bells, "risks": "risk", "digest": self.DIGEST,
        }

    def repository(self):
        return {"files": ["app.js", "test/app.test.js"]}

    def test_criterion_without_executor_record_is_rejected(self):
        with self.assertRaisesRegex(contract.ContractError, "lacks one verification"):
            contract.validate_output(
                contract.Altitude.PROJECT, self.plan("The disclosure is visible"),
                self.repository())

    def test_structured_output_requires_verification_on_new_story_criteria(self):
        story_schema = contract.PROJECT_JSON_SCHEMA["properties"]["stories"]["items"]
        pattern = story_schema["properties"]["acceptance_criteria"]["items"]["pattern"]
        self.assertIsNone(re.search(pattern, "The disclosure is visible"))
        self.assertIsNotNone(re.search(pattern, self.record()))

    def test_nonexistent_existing_executor_is_rejected(self):
        criterion = self.record(scope="test/missing.test.js",
                                executor="test/missing.test.js")
        with self.assertRaisesRegex(contract.ContractError, "executor does not exist"):
            contract.validate_output(
                contract.Altitude.PROJECT, self.plan(
                    criterion, scope=["app.js", "test/*.test.js"]), self.repository())

    def test_executor_created_within_authorized_scope_is_accepted(self):
        criterion = self.record(scope="test/new.test.js", executor="test/new.test.js",
                                executor_source="create")
        value = self.plan(criterion, scope=["app.js", "test/*.test.js"])
        self.assertIs(value, contract.validate_output(
            contract.Altitude.PROJECT, value, self.repository()))

    def test_automated_record_requires_action_result_and_failure_detection(self):
        for field in ("action", "expected", "failure"):
            with self.subTest(field=field):
                criterion = self.record(**{field: ""})
                with self.assertRaisesRegex(contract.ContractError, "non-empty strings"):
                    contract.validate_output(
                        contract.Altitude.PROJECT, self.plan(criterion), self.repository())

    def test_automated_action_must_invoke_one_concrete_executor(self):
        for changes, message in (
            ({"action": "echo test/app.test.js"}, "does not invoke"),
            ({"action": "python3 -c pass test/app.test.js"}, "does not invoke"),
            ({"action": "npm --help test/app.test.js"}, "does not invoke"),
            ({"scope": "test/*.test.js", "executor": "test/*.test.js",
              "action": "node --test test/*.test.js"}, "one concrete path"),
            ({"scope": "README.md", "executor": "README.md",
              "action": "cat README.md"}, "not a test or workflow"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                    contract.ContractError, message):
                contract.validate_output(
                    contract.Altitude.PROJECT,
                    self.plan(self.record(**changes), scope=(
                        ["README.md"] if changes.get("executor") == "README.md"
                        else ["app.js", "test/*.test.js"])),
                    self.repository())

    def test_explicit_human_bell_is_accepted_and_counted(self):
        record = {
            "type": "human-bell", "scope": "app.js",
            "action": "owner judges whether the legal wording is acceptable",
            "expected": "owner explicitly accepts or rejects the wording",
            "failure": "no explicit owner decision is recorded",
            "reason": "legal acceptability has no deterministic repository oracle",
        }
        criterion = "The legal wording is acceptable || VERIFY " + json.dumps(record)
        value = self.plan(criterion, expected_bells=3)
        self.assertIs(value, contract.validate_output(
            contract.Altitude.PROJECT, value, self.repository()))
        with self.assertRaisesRegex(contract.ContractError, "count every declared human"):
            contract.validate_output(
                contract.Altitude.PROJECT,
                self.plan(criterion, expected_bells=2), self.repository())

    def test_human_bell_without_automation_reason_is_rejected(self):
        record = {
            "type": "human-bell", "scope": "app.js", "action": "owner reviews",
            "expected": "owner decides", "failure": "no decision", "reason": "",
        }
        criterion = "The wording is acceptable || VERIFY " + json.dumps(record)
        with self.assertRaisesRegex(contract.ContractError, "non-empty strings"):
            contract.validate_output(
                contract.Altitude.PROJECT, self.plan(criterion, expected_bells=3),
                self.repository())

    def test_existing_operating_envelope_verification_remains_compatible(self):
        envelope = [{
            "id": "OE-1", "category": "work-bound",
            "requirement": "processing remains bounded",
            "failure_condition": "the bounded test exceeds one second",
        }]
        checks = [{"id": "OE-1", "check": "bounded test exceeds one second"}]
        value = self.plan(envelope=envelope, checks=checks)
        self.assertIs(value, contract.validate_output(
            contract.Altitude.PROJECT, value, self.repository()))


class RepositoryCompatibilityTests(unittest.TestCase):
    def criterion(self, *, executor="test/app.test.js", source="existing",
                  action="python3 -m unittest test/app.test.js",
                  expected="the disclosure assertion passes",
                  failure="the command exits nonzero"):
        record = {
            "type": "automated", "scope": executor, "executor": executor,
            "executor_source": source, "action": action, "expected": expected,
            "failure": failure,
        }
        return "Winning allocation disclosure is visible || VERIFY " + json.dumps(
            record, separators=(",", ":"), sort_keys=True)

    def plan(self, *, spec="Change allocation disclosure.", scope=None):
        story = {
            "key": "disclosure", "title": "Allocation disclosure", "spec": spec,
            "phase": "hardening", "depends_on": [], "hazard": False,
            "acceptance_criteria": [self.criterion()],
            "operating_envelope_ids": [], "operating_envelope_checks": [],
            "scope": scope or ["app.js", "test/app.test.js"],
            "spend_cap": "$5 / 60 min",
        }
        return {"altitude": "project", "stories": [story]}

    def repository(self):
        return {"files": ["product.md", "app.js", "test/app.test.js", "package.json"],
                "production_owners": [{
                    "behavior": "winning allocation disclosure", "path": "app.js",
                    "evidence": "repository ownership index"}],
                "policy_assertions": [{
                    "kind": "forbidden-dependency", "name": "playwright",
                    "evidence": "deterministic dependency policy test"}]}

    def test_historical_missing_app_owner_is_rejected(self):
        value = self.plan(scope=["test/app.test.js"])
        with self.assertRaisesRegex(contract.ContractError,
                                    "owned by 'app.js'.*omits"):
            contract.validate_repository_compatibility(value, self.repository())

    def test_unresolved_scope_pattern_is_rejected(self):
        value = self.plan(scope=["app.js", "test/missing-*.js"])
        with self.assertRaisesRegex(contract.ContractError, "does not resolve"):
            contract.validate_repository_compatibility(value, self.repository())

    def test_scope_star_does_not_cross_path_segments(self):
        value = self.plan(scope=["src/*.py"])
        repository = {"files": ["product.md", "src/nested/app.py"]}
        with self.assertRaisesRegex(contract.ContractError, "does not resolve"):
            contract.validate_repository_compatibility(value, repository)

    def test_scope_keeps_merge_gate_repository_relative_syntax(self):
        value = self.plan(scope=["./app.js"])
        with self.assertRaisesRegex(contract.ContractError, "does not resolve"):
            contract.validate_repository_compatibility(value, self.repository())

    def test_claimed_existing_verification_path_must_resolve(self):
        value = self.plan(
            spec="Reuse the existing verification path `test/missing.js`.")
        with self.assertRaisesRegex(contract.ContractError,
                                    "existing repository path.*does not resolve"):
            contract.validate_repository_compatibility(value, self.repository())

    def test_new_path_is_not_misclassified_by_later_existing_path_claim(self):
        value = self.plan(
            spec="Create src/new.py. Reuse existing src/base.py.",
            scope=["src/new.py", "src/base.py"])
        repository = self.repository()
        repository["files"].append("src/base.py")
        repository["production_owners"] = []
        self.assertIs(value, contract.validate_repository_compatibility(
            value, repository))

    def test_explicitly_forbidden_dependency_is_rejected(self):
        value = self.plan(spec=(
            "Change winning allocation disclosure in app.js and add dependency playwright."))
        with self.assertRaisesRegex(contract.ContractError,
                                    "forbidden dependency 'playwright'"):
            contract.validate_repository_compatibility(value, self.repository())

    def test_later_prohibition_does_not_hide_forbidden_dependency_proposal(self):
        value = self.plan(spec=(
            "Use dependency playwright. Do not add a raw browser launcher."))
        with self.assertRaisesRegex(contract.ContractError,
                                    "forbidden dependency 'playwright'"):
            contract.validate_repository_compatibility(value, self.repository())

    def test_versioned_forbidden_dependency_is_rejected(self):
        value = self.plan(spec="Add dependency playwright@1.40.0.")
        with self.assertRaisesRegex(contract.ContractError,
                                    "forbidden dependency 'playwright'"):
            contract.validate_repository_compatibility(value, self.repository())

    def test_repository_compatible_plan_passes(self):
        value = self.plan(spec=(
            "Change winning allocation disclosure in the existing `app.js`; "
            "reuse the existing verification path `test/app.test.js`."))
        self.assertIs(value, contract.validate_repository_compatibility(
            value, self.repository()))

    def test_semantic_inference_is_not_invented(self):
        value = self.plan(spec="Improve the result explanation.", scope=["app.js"])
        repository = self.repository()
        repository["sources"] = {
            "test/app.test.js": "assert(result.text.includes('explanation'))"}
        repository["production_owners"] = []
        self.assertIs(value, contract.validate_repository_compatibility(
            value, repository))


class CanonicalPathTests(unittest.TestCase):
    def test_prompt_has_required_contract_language(self):
        prompt = pathlib.Path(__file__).with_name("prompt.md").read_text()
        for phrase in ("product.md", "existing ADR", "repository",
                       "type:roadmap-commitment", "type:project",
                       "risk-ordered", "expected bells", "Depends-on",
                       "hazard", "falsifiable", "read-back"):
            self.assertIn(phrase, prompt)

    def test_prompt_limits_retired_story_replacement_to_one_authorized_story(self):
        prompt = pathlib.Path(__file__).with_name("prompt.md").read_text()
        normalized = " ".join(prompt.split())
        for phrase in (
            "remove exactly its old key",
            "exactly one new key",
            "after its third poisoning",
            "all three delivery Attempts are spent",
            "at least one poisoning",
            "owner-cancelled-poison",
            "structured human `## Story replacement` authorization",
            "repoint every downstream dependency",
            "Preserve every other Story identity",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, normalized)
        for phrase in ("Plan in plain language", "How the plan works",
                       "Story dependencies", "```mermaid"):
            self.assertIn(phrase, prompt)

    def test_prompt_gates_exact_and_optimal_claims_on_a_complete_proof_obligation(self):
        prompt = pathlib.Path(__file__).with_name("prompt.md").read_text()
        section = prompt.split("### Proof obligations for exact or complete claims", 1)[1]
        section = section.split("Return this exact JSON shape", 1)[0]
        normalized = " ".join(section.split())

        for claim in ("maximum", "minimum", "highest", "lowest", "optimal",
                      "exact", "exhaustive", "all", "every", "guaranteed"):
            with self.subTest(claim=claim):
                self.assertIn(f"`{claim}`", section)

        for proof_part in ("**Claim**", "**Domain**",
                           "**Invariant / monotonicity / structural property**",
                           "**Skipped-value justification**", "**Bound**",
                           "**Falsification strategy**"):
            with self.subTest(proof_part=proof_part):
                self.assertIn(proof_part, section)

        self.assertIn(
            "Testing selected candidates and an adjacent candidate is not, by itself, "
            "proof of a global maximum.", normalized)
        self.assertIn(
            "proves that every skipped value cannot be a better feasible result", normalized)
        self.assertIn(
            "narrow the product claim to what the method can establish or fail planning",
            normalized)
        self.assertIn(
            "Do not emit the Project or Story for Delivery with the unsupported claim.",
            normalized)

    def test_prompt_requires_scope_authority_for_every_promised_product_change(self):
        prompt = pathlib.Path(__file__).with_name("prompt.md").read_text()
        section = prompt.split("### Scope must authorize the promised behavior", 1)[1]
        section = section.split("Return this exact JSON shape", 1)[0]
        normalized = " ".join(section.split())

        for requirement in (
            "`scope` as its complete implementation authority",
            "Cross-check every Story spec, acceptance criterion, and "
            "operating-envelope check against the repository file index",
            "production implementation surface that can create or change that behavior",
            "a scope containing only test or documentation paths is invalid",
            "acceptance-to-scope mapping",
            "fail planning and name the missing ownership decision",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, normalized)

        self.assertIn(
            "new or changed browser-visible text, state, interaction, or rendering must "
            "include the relevant application/UI implementation path", normalized)
        self.assertIn(
            "A final assurance Story may remain test-only only when it promises "
            "verification rather than a product change", normalized)

    def test_real_browser_assurance_requires_a_feasible_planned_mechanism(self):
        prompt = pathlib.Path(__file__).with_name("prompt.md").read_text()
        section = prompt.split(
            "### Browser assurance must have a feasible mechanism", 1)[1]
        section = section.split("Return this exact JSON shape", 1)[0]
        normalized = " ".join(section.split())

        for requirement in (
            "real-browser or named-browser assurance",
            "Reuse an existing browser-testing mechanism",
            "Authorize an established browser-testing dependency",
            "every manifest, implementation, test, and configuration path",
            "narrow the browser-assurance promise",
            "fail planning before writing any Project artifacts",
            "For a plan that proceeds under the first or second route",
            "state the chosen mechanism and its repository evidence",
            "raw browser-process",
            "debug-protocol",
            "`--dump-dom`",
            "homemade driver is not an acceptable substitute",
            "repository evidence proves it reliable for every promised check",
            "Do not make Delivery discover missing browser tooling through retries",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, normalized)

    def test_no_competing_root_agents_tree(self):
        root = pathlib.Path(__file__).resolve().parents[3]
        self.assertFalse((root / "agents" / "planning").exists())


if __name__ == "__main__":
    unittest.main()
