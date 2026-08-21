import copy
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

    def update_issue(self, number, body):
        item = next(item for item in self.issues if item["number"] == number)
        item["body"] = body
        return copy.deepcopy(item)

    def update_labels(self, number, labels):
        item = next(item for item in self.issues if item["number"] == number)
        item["labels"] = list(labels)
        return copy.deepcopy(item)

    def list_comments(self, number):
        return copy.deepcopy(self.comments.get(number, []))

    def create_comment(self, number, body):
        item = {"id": len(self.comments.get(number, [])) + 1, "body": body}
        self.comments.setdefault(number, []).append(item)
        return copy.deepcopy(item)

    def ensure_label(self, name):
        self.labels.add(name)


def project_issue(number=10):
    return {"number": number, "labels": ["type:project", "project:active"],
            "body": "### Stories\n\n_No response_\n\n### Expected bells\n\n2\n\n### Risks / notes\n\nNone\n"}


def campaign_output():
    return {"altitude": "campaign", "project": {"title": "Retirement model",
            "goal": "Users can model retirement.",
            "acceptance_criteria": ["A saved scenario reproduces its result"],
            "expected_bells": 2, "risks": "Calculation correctness"},
            "rationale": "Highest user risk first", "risks": ["Incorrect projections"]}


def project_output():
    base = {"phase": "build", "hazard": False, "spend_cap": "$20 / 60 min",
            "scope": ["src/model/**"], "acceptance_criteria": ["Example passes"]}
    return {"altitude": "project", "adr": {"title": "Artifact ownership",
            "context": "Product stories belong with code.", "decision": "Write product artifacts here.",
            "alternatives": ["Factory repository"], "consequences": ["Product gate required"]},
            "stories": [{**base, "key": "model", "title": "Model retirement",
                         "spec": "Calculate a projection.", "depends_on": []},
                        {**base, "key": "ui", "title": "Show retirement",
                         "spec": "Render a projection.", "depends_on": ["model"]}],
            "expected_bells": 2, "digest": "ADR, model, then UI; no hazards."}


class CampaignTests(unittest.TestCase):
    def test_campaign_writes_proposal_and_project_only(self):
        store = FakeStore([{"number": 1, "labels": ["type:roadmap-commitment"], "body": ""}])
        trigger = store.get_issue(1)
        written = artifacts.write(store, trigger, "v1", campaign_output())
        self.assertEqual(contract.Altitude.CAMPAIGN, written.altitude)
        self.assertEqual(2, written.project)
        self.assertEqual(2, len(store.issues))
        self.assertNotIn("type:story", {label for issue in store.issues for label in issue["labels"]})
        self.assertEqual(written, artifacts.verify(store, trigger, "v1", written.altitude))

    def test_campaign_replay_creates_no_duplicates(self):
        store = FakeStore([{"number": 1, "labels": ["type:roadmap-commitment"], "body": ""}])
        trigger = store.get_issue(1)
        first = artifacts.write(store, trigger, "v1", campaign_output())
        second = artifacts.write(store, trigger, "v1", campaign_output())
        self.assertEqual(first, second)
        self.assertEqual(2, len(store.issues))
        self.assertEqual(1, len(store.comments[1]))


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
        self.assertEqual(written, artifacts.verify(store, trigger, "v2", written.altitude))

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


if __name__ == "__main__":
    unittest.main()
