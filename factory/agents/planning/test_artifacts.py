import copy
import re
import unittest

import artifacts
import contract


class FakeStore:
    def __init__(self, issues=None):
        self.issues = copy.deepcopy(issues or [])
        self.comments = {}
        self.labels = set()
        self.next_number = max([item["number"] for item in self.issues] or [0]) + 1

    def list_issues(self, state="all"):
        return copy.deepcopy(self.issues)

    def get_issue(self, number):
        return copy.deepcopy(next(item for item in self.issues if item["number"] == number))

    def create_issue(self, title, body, labels):
        item = {"number": self.next_number, "title": title, "body": body,
                "labels": list(labels)}
        self.next_number += 1
        self.issues.append(item)
        return copy.deepcopy(item)

    def update_issue(self, number, body, title=None):
        item = next(item for item in self.issues if item["number"] == number)
        item["body"] = body
        if title is not None:
            item["title"] = title
        return copy.deepcopy(item)

    def update_labels(self, number, labels):
        item = next(item for item in self.issues if item["number"] == number)
        item["labels"] = list(labels)
        return copy.deepcopy(item)

    def list_comments(self, number):
        return copy.deepcopy(self.comments.get(number, []))

    def create_comment(self, number, body):
        item = {"id": 1 + sum(len(items) for items in self.comments.values()), "body": body}
        self.comments.setdefault(number, []).append(item)
        return copy.deepcopy(item)

    def update_comment(self, comment_id, body):
        item = next(item for values in self.comments.values() for item in values
                    if item["id"] == comment_id)
        item["body"] = body
        return copy.deepcopy(item)

    def ensure_label(self, name):
        self.labels.add(name)


def project_issue(number=10):
    return {"number": number, "labels": ["type:project", "project:active"],
            "body": ("### Falsifiable acceptance criteria\n\n"
                     "- [ ] Original criterion\n\n"
                     "### Stories\n\n_No response_\n\n"
                     "### Operating envelope\n\n_No response_\n\n"
                     "### Expected bells\n\n2\n\n"
                     "### Risks / notes\n\nNone\n")}


def campaign_output():
    return {"altitude": "campaign", "project": {"title": "Retirement model",
            "goal": "Users can model retirement.",
            "acceptance_criteria": ["A saved scenario reproduces its result"],
            "expected_bells": 2, "risks": "Calculation correctness"},
            "rationale": "Highest user risk first", "risks": ["Incorrect projections"]}


def project_output():
    base = {"phase": "build", "hazard": False, "spend_cap": "$5 / 60 min",
            "scope": ["src/model/**"], "acceptance_criteria": ["Example passes"]}
    return {"altitude": "project",
            "acceptance_criteria": ["A verified projection is returned or refusal is explicit"],
            "operating_envelope": [{"id": "OE-SCALE-1",
                "category": "representative-input",
                "requirement": "A representative retirement input completes",
                "failure_condition": "The representative input does not complete"}],
            "adr": {"title": "Artifact ownership",
            "context": "Product stories belong with code.", "decision": "Write product artifacts here.",
            "alternatives": ["Factory repository"], "consequences": ["Product gate required"]},
            "stories": [{**base, "key": "model", "title": "Model retirement",
                         "operating_envelope_ids": ["OE-SCALE-1"],
                         "spec": "Calculate a projection.", "depends_on": []},
                        {**base, "key": "ui", "title": "Show retirement",
                         "operating_envelope_ids": [],
                         "spec": "Render a projection.", "depends_on": ["model"]}],
            "expected_bells": 2, "digest": """## Plan in plain language

Build the model, then show it.

## How the plan works

```mermaid
flowchart LR
  input --> model --> output
```

Input flows through the model to output.

## Story dependencies

```mermaid
flowchart LR
  model --> ui
```

The UI depends on the model."""}


class CampaignTests(unittest.TestCase):
    def test_campaign_accepts_production_shaped_create_issue_labels(self):
        class ProductionShapeStore(FakeStore):
            def create_issue(self, title, body, labels):
                item = super().create_issue(title, body, labels)
                item["labels"] = [{"name": label} for label in item["labels"]]
                return item

        store = ProductionShapeStore([
            {"number": 1, "labels": ["type:roadmap-commitment"], "body": ""}
        ])

        written = artifacts.write(store, store.get_issue(1), "v1", campaign_output())

        self.assertEqual(2, written.project)
        self.assertIn("project:planning", store.get_issue(2)["labels"])

    def test_campaign_writes_proposal_and_project_only(self):
        store = FakeStore([{"number": 1, "labels": ["type:roadmap-commitment"], "body": ""}])
        trigger = store.get_issue(1)
        written = artifacts.write(store, trigger, "v1", campaign_output())
        self.assertEqual(contract.Altitude.CAMPAIGN, written.altitude)
        self.assertEqual(2, written.project)
        self.assertEqual(2, len(store.issues))
        self.assertNotIn("type:story", {label for issue in store.issues for label in issue["labels"]})
        self.assertIn("project:planning", store.get_issue(written.project)["labels"])
        self.assertNotIn("project:awaiting-ready", store.get_issue(written.project)["labels"])
        self.assertEqual(written, artifacts.verify(store, trigger, "v1", written.altitude))

    def test_campaign_replay_creates_no_duplicates(self):
        store = FakeStore([{"number": 1, "labels": ["type:roadmap-commitment"], "body": ""}])
        trigger = store.get_issue(1)
        first = artifacts.write(store, trigger, "v1", campaign_output())
        second = artifacts.write(store, trigger, "v1", campaign_output())
        self.assertEqual(first, second)
        self.assertEqual(2, len(store.issues))
        self.assertEqual(1, len(store.comments[1]))

    def test_campaign_replay_repairs_incomplete_premature_ready_project(self):
        store = FakeStore([{"number": 1, "labels": ["type:roadmap-commitment"], "body": ""}])
        trigger = store.get_issue(1)
        first = artifacts.write(store, trigger, "v1", campaign_output())
        store.update_labels(first.project, ["type:project", "project:awaiting-ready"])

        replayed = artifacts.write(store, trigger, "v1", campaign_output())

        self.assertEqual(first, replayed)
        self.assertIn("project:planning", store.get_issue(first.project)["labels"])
        self.assertNotIn("project:awaiting-ready", store.get_issue(first.project)["labels"])
        self.assertEqual(2, len(store.issues))

    def test_campaign_replay_does_not_move_expanded_project_backwards(self):
        store = FakeStore([{"number": 1, "labels": ["type:roadmap-commitment"], "body": ""}])
        trigger = store.get_issue(1)
        first = artifacts.write(store, trigger, "v1", campaign_output())
        project = store.get_issue(first.project)
        store.update_issue(first.project, project["body"].replace("_No response_", "#99"))
        store.update_labels(first.project, ["type:project", "project:awaiting-ready"])

        replayed = artifacts.write(store, trigger, "v1", campaign_output())

        self.assertEqual(first, replayed)
        self.assertIn("project:awaiting-ready", store.get_issue(first.project)["labels"])
        self.assertNotIn("project:planning", store.get_issue(first.project)["labels"])

    def test_campaign_readback_rejects_ready_project_without_stories_section(self):
        store = FakeStore([{"number": 1, "labels": ["type:roadmap-commitment"], "body": ""}])
        trigger = store.get_issue(1)
        first = artifacts.write(store, trigger, "v1", campaign_output())
        project = store.get_issue(first.project)
        project["body"] = project["body"].replace(
            "### Stories\n\n_No response_\n\n", "")
        store.update_issue(first.project, project["body"])
        store.update_labels(first.project, ["type:project", "project:awaiting-ready"])

        with self.assertRaisesRegex(artifacts.ArtifactError, "labels do not match"):
            artifacts.verify(store, trigger, "v1", contract.Altitude.CAMPAIGN)


class ProjectTests(unittest.TestCase):
    def test_project_writes_adr_stories_dependencies_and_digest(self):
        store = FakeStore([project_issue()])
        trigger = store.get_issue(10)
        written = artifacts.write(store, trigger, "v2", project_output())
        self.assertEqual(11, written.adr)
        self.assertEqual((12, 13), written.stories)
        dependent = store.get_issue(13)
        self.assertIn("### Depends-on\n\n#12", dependent["body"])
        self.assertIn("phase:build", dependent["labels"])
        self.assertIn("digest: ", dependent["body"])
        self.assertIn("OE-SCALE-1", store.get_issue(12)["body"])
        self.assertIn("OE-SCALE-1 | representative-input", store.get_issue(10)["body"])
        self.assertIn(
            "- [ ] A verified projection is returned or refusal is explicit",
            store.get_issue(10)["body"])
        self.assertEqual(written, artifacts.verify(store, trigger, "v2", written.altitude))

    def test_project_revision_updates_owner_signable_criteria_in_place(self):
        store = FakeStore([project_issue()])
        trigger = store.get_issue(10)
        first = artifacts.write(store, trigger, "v2", project_output())
        revised = project_output()
        revised["acceptance_criteria"] = [
            "At most five candidates are attempted",
            "No unverified portfolio is returned",
        ]

        second = artifacts.write(store, trigger, "v3", revised)

        self.assertEqual(first.project, second.project)
        body = store.get_issue(10)["body"]
        self.assertIn("- [ ] At most five candidates are attempted", body)
        self.assertIn("- [ ] No unverified portfolio is returned", body)
        self.assertNotIn("A verified projection is returned or refusal is explicit", body)

    def test_empty_project_criteria_write_nothing(self):
        store = FakeStore([project_issue()])
        output = project_output()
        output["acceptance_criteria"] = []
        with self.assertRaisesRegex(artifacts.ArtifactError, "acceptance criteria"):
            artifacts.write(store, store.get_issue(10), "v2", output)
        self.assertEqual(1, len(store.issues))
        self.assertEqual({}, store.comments)

    def test_missing_project_criteria_section_writes_nothing(self):
        issue = project_issue()
        issue["body"] = re.sub(
            r"### Falsifiable acceptance criteria\n\n.*?\n\n(?=### Stories)",
            "", issue["body"], flags=re.S)
        store = FakeStore([issue])
        with self.assertRaisesRegex(artifacts.ArtifactError, "no writable acceptance"):
            artifacts.write(store, store.get_issue(10), "v2", project_output())
        self.assertEqual(1, len(store.issues))
        self.assertEqual({}, store.comments)

    def test_project_replay_creates_no_duplicates(self):
        store = FakeStore([project_issue()])
        trigger = store.get_issue(10)
        first = artifacts.write(store, trigger, "v2", project_output())
        second = artifacts.write(store, store.get_issue(10), "v2", project_output())
        self.assertEqual(first, second)
        self.assertEqual(4, len(store.issues))
        self.assertEqual(1, len(store.comments[10]))

    def test_unknown_or_cyclic_dependency_writes_nothing(self):
        for mutation in ("unknown", "cycle"):
            output = project_output()
            if mutation == "unknown":
                output["stories"][1]["depends_on"] = ["missing"]
            else:
                output["stories"][0]["depends_on"] = ["ui"]
            store = FakeStore([project_issue()])
            with self.subTest(mutation=mutation), self.assertRaises(artifacts.ArtifactError):
                artifacts.write(store, store.get_issue(10), "v2", output)
            self.assertEqual(1, len(store.issues))

    def test_malformed_story_or_hazard_mismatch_writes_nothing(self):
        for mutation in ("phase", "hazard"):
            output = project_output()
            if mutation == "phase":
                output["stories"][0]["phase"] = "test"
            else:
                output["stories"][0]["scope"] = ["package.json"]
            store = FakeStore([project_issue()])
            with self.subTest(mutation=mutation), self.assertRaises(artifacts.ArtifactError):
                artifacts.write(store, store.get_issue(10), "v2", output)
            self.assertEqual(1, len(store.issues))

    def test_readback_rejects_missing_label_and_hazard_mismatch(self):
        for mutation in ("phase", "hazard"):
            store = FakeStore([project_issue()])
            trigger = store.get_issue(10)
            written = artifacts.write(store, trigger, "v2", project_output())
            story = next(item for item in store.issues if item["number"] == written.stories[0])
            if mutation == "phase":
                story["labels"].remove("phase:build")
            else:
                story["labels"].append("hazard")
            with self.subTest(mutation=mutation), self.assertRaises(artifacts.ArtifactError):
                artifacts.verify(store, trigger, "v2", contract.Altitude.PROJECT)

    def test_readback_rejects_stale_or_unknown_envelope_obligation(self):
        for mutation in ("digest", "unknown"):
            store = FakeStore([project_issue()])
            trigger = store.get_issue(10)
            written = artifacts.write(store, trigger, "v2", project_output())
            story = next(item for item in store.issues
                         if item["number"] == written.stories[0])
            if mutation == "digest":
                story["body"] = story["body"].replace("digest: ", "digest: stale-")
            else:
                story["body"] = story["body"].replace(
                    "OE-SCALE-1\n\n### Scope", "OE-UNKNOWN\n\n### Scope")
            with self.subTest(mutation=mutation), self.assertRaises(
                    artifacts.ArtifactError):
                artifacts.verify(store, trigger, "v2", contract.Altitude.PROJECT)


if __name__ == "__main__":
    unittest.main()
