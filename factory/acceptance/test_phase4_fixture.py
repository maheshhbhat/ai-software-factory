import json
import pathlib
import urllib.error
import unittest
import phase4_fixture as fixture

SHA = "0123456789abcdef0123456789abcdef01234567"

class FixtureAcceptance(unittest.TestCase):
    def test_live_health_interface_returns_authoritative_sha(self):
        with fixture.running(SHA) as url:
            status, content_type, body = fixture.get(url + "/health")
            self.assertEqual((status, content_type), (200, "application/json"))
            self.assertEqual(json.loads(body), {"build_sha": SHA})

    def test_other_paths_are_not_health(self):
        with fixture.running(SHA) as url:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                fixture.get(url + "/")
            self.assertEqual(raised.exception.code, 404)

    def test_missing_or_malformed_identity_fails_before_startup(self):
        for value in (None, "", "abc", "A" * 40, "a" * 39, "a" * 41):
            environment = {} if value is None else {"BUILD_SHA": value}
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                fixture.app.build_sha(environment)

    def test_adr_pins_location_owner_reset_interface_and_sha_source(self):
        adr = (pathlib.Path(__file__).resolve().parents[1] /
               "decisions" / "phase4-live-fixture.md").read_text()
        for fact in ("factory/fixtures/phase4_health/", "owned", "GET /health",
                     "BUILD_SHA", "force-pushes", "retirement-modeling"):
            self.assertIn(fact, adr)

if __name__ == "__main__": unittest.main()
