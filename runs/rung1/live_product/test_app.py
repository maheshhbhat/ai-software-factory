"""Deterministic tests for the Rung 1 live health endpoint."""

import contextlib
import threading
import unittest
import urllib.error
import urllib.request

import app


BUILD_SHA = "3f2c1ab9e0d4c7b6a5948372615f0e9d8c7b6a51"


@contextlib.contextmanager
def running_server(build_sha=BUILD_SHA):
    server = app.make_server("127.0.0.1", 0, build_sha)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


class HealthEndpointTests(unittest.TestCase):
    def test_health_returns_exact_injected_build_sha(self):
        with running_server() as base_url:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "application/json")
                self.assertEqual(response.read(), b'{"build_sha":"' + BUILD_SHA.encode() + b'"}')

    def test_every_other_path_returns_not_found(self):
        with running_server() as base_url:
            for path in ("/", "/health/", "/health?details=1", "/healthz"):
                with self.subTest(path=path):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(f"{base_url}{path}", timeout=2)
                    self.assertEqual(raised.exception.code, 404)
                    raised.exception.close()

    def test_invalid_build_shas_are_rejected_before_server_is_created(self):
        invalid = (
            None,
            "",
            BUILD_SHA[:39],
            BUILD_SHA + "a",
            BUILD_SHA.upper(),
            "g" * 40,
            f" {BUILD_SHA}",
            0,
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "40 lowercase hexadecimal"):
                    app.make_server("127.0.0.1", 0, value)


if __name__ == "__main__":
    unittest.main()
