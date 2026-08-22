"""Tests for the Phase 4 live product health endpoint.

Attempt 1's suite asserted only that a `build_sha` key was present, which is why
a literal `defective` passed it. These tests pin the served value to the injected
identity and cover each malformed-identity class named by the fixture ADR.
"""
import contextlib
import json
import threading
import unittest
import urllib.error
import urllib.request

import app

SHA = "0123456789abcdef0123456789abcdef01234567"


@contextlib.contextmanager
def running(sha):
    server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.handler(sha))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=2) as response:
        return response.status, response.headers.get_content_type(), response.read().decode()


class BuildIdentity(unittest.TestCase):
    def test_injected_identity_is_the_reported_identity(self):
        self.assertEqual(app.build_sha({"BUILD_SHA": SHA}), SHA)

    def test_malformed_identity_fails_before_startup(self):
        for value in (None, "", "defective", "abc", SHA.upper(), "a" * 39, "a" * 41,
                      " " + SHA, SHA + "\n", "g" * 40):
            environment = {} if value is None else {"BUILD_SHA": value}
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                app.build_sha(environment)


class HealthEndpoint(unittest.TestCase):
    def test_health_returns_the_injected_sha_as_json(self):
        with running(app.build_sha({"BUILD_SHA": SHA})) as url:
            status, content_type, body = get(url + "/health")
            self.assertEqual((status, content_type), (200, "application/json"))
            self.assertEqual(json.loads(body), {"build_sha": SHA})

    def test_health_never_reports_a_placeholder(self):
        with running(app.build_sha({"BUILD_SHA": SHA})) as url:
            _, _, body = get(url + "/health")
            self.assertNotIn("defective", body)

    def test_other_paths_are_not_health(self):
        with running(app.build_sha({"BUILD_SHA": SHA})) as url:
            for path in ("/", "/healthz", "/health/"):
                with self.subTest(path=path):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        get(url + path)
                    self.assertEqual(raised.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
