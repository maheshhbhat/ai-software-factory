from __future__ import annotations

import pathlib
import tempfile
import unittest

import readiness_receipt as receipt


class ReceiptTests(unittest.TestCase):
    def test_fingerprint_normalizes_poll_sh_defaults(self):
        self.assertEqual(receipt.configuration_fingerprint({}),
                         receipt.configuration_fingerprint(
                             dict(receipt.CONFIG_DEFAULTS)))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = pathlib.Path(temporary.name) / "receipt.json"
        self.env = {"FACTORY_WORKER_ORDER": "capacity-delivery"}
        self.args = dict(repo="Owner/Repo", commitment=45, project=47,
                         target="app.js", revision="a" * 40,
                         checks=[{"name": "probe", "passed": True,
                                  "detail": "answered"}], environ=self.env, now=100)

    def test_matching_fresh_receipt_passes(self):
        receipt.issue(self.path, **self.args)
        value = receipt.validate(self.path, repo="owner/repo", commitment=45,
                                 revision="a" * 40, environ=self.env, now=101)
        self.assertEqual(47, value["project"])

    def test_failed_doctor_cannot_issue_receipt(self):
        args = {**self.args, "checks": [{"name": "probe", "passed": False}]}
        with self.assertRaisesRegex(receipt.ReceiptError, "every check passed"):
            receipt.issue(self.path, **args)

    def test_expired_and_mismatched_receipts_fail(self):
        receipt.issue(self.path, ttl_seconds=10, **self.args)
        base = dict(repo="owner/repo", commitment=45, revision="a" * 40,
                    environ=self.env, now=101)
        cases = (({"now": 111}, "expired"),
                 ({"repo": "other/repo"}, "repo does not match"),
                 ({"commitment": 46}, "commitment does not match"),
                 ({"revision": "b" * 40}, "factory_revision does not match"),
                 ({"environ": {**self.env, "FACTORY_CAPACITY_STATE": "/other/state"}},
                  "configuration_fingerprint does not match"),
                 ({"environ": {"FACTORY_WORKER_ORDER": "other"}},
                  "configuration_fingerprint does not match"))
        for change, message in cases:
            with self.subTest(change=change), self.assertRaisesRegex(
                    receipt.ReceiptError, message):
                receipt.validate(self.path, **{**base, **change})

    def test_tampering_fails_digest(self):
        receipt.issue(self.path, **self.args)
        self.path.write_text(self.path.read_text().replace("app.js", "other.js"))
        with self.assertRaisesRegex(receipt.ReceiptError, "digest"):
            receipt.validate(self.path, repo="owner/repo", commitment=45,
                             revision="a" * 40, environ=self.env, now=101)
