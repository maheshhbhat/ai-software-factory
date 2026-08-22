"""Tests for the Phase 4 live product /health endpoint.

The endpoint's whole purpose is to name the deployed commit, so every test here
drives the injected environment rather than a constant handed to the handler:
Attempt 1 returned a literal and passed tests written the other way round.
"""
import contextlib
import json
import threading
import unittest
import urllib.error
import urllib.request

import app

MERGED = "3f2c1ab9e0d4c7b6a5948372615f0e9d8c7b6a51"

@contextlib.contextmanager
def running(environment):
    server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.handler(app.build_sha(environment)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try: yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown(); thread.join(timeout=2); server.server_close()

class BuildShaTest(unittest.TestCase):
    def test_exact_injected_sha_is_returned(self):
        self.assertEqual(app.build_sha({"BUILD_SHA": MERGED}), MERGED)

    def test_malformed_identity_fails_loudly(self):
        for name, value in (("missing", None),
                            ("empty", ""),
                            ("abbreviated", MERGED[:7]),
                            ("uppercase", MERGED.upper()),
                            ("over-long", MERGED + "a"),
                            ("non-hexadecimal", "z" * 40),
                            ("padded", f" {MERGED} "),
                            ("literal", "defective")):
            with self.subTest(name):
                environment = {} if value is None else {"BUILD_SHA": value}
                with self.assertRaises(RuntimeError):
                    app.build_sha(environment)

class HealthEndpointTest(unittest.TestCase):
    def test_health_returns_the_injected_sha_as_json(self):
        with running({"BUILD_SHA": MERGED}) as base:
            with urllib.request.urlopen(f"{base}/health", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "application/json")
                self.assertEqual(json.loads(response.read()), {"build_sha": MERGED})

    def test_other_paths_are_not_found(self):
        with running({"BUILD_SHA": MERGED}) as base:
            for path in ("/", "/health/", "/healthz"):
                with self.subTest(path):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(f"{base}{path}", timeout=2)
                    self.assertEqual(raised.exception.code, 404)

    def test_server_does_not_start_without_a_valid_identity(self):
        with self.assertRaises(RuntimeError):
            with running({"BUILD_SHA": "defective"}):
                self.fail("the server started on an invented build identity")

if __name__ == "__main__":
    unittest.main()
