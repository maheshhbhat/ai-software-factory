"""Deterministic tests for the Rung 1 live product."""

import contextlib
import json
import threading
import unittest
import urllib.error
import urllib.request

import app


BUILD_SHA = "0123456789abcdef0123456789abcdef01234567"


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


class MakeServerTest(unittest.TestCase):
    def test_rejects_invalid_build_shas_before_binding(self):
        invalid_values = (
            None,
            "",
            BUILD_SHA[:-1],
            BUILD_SHA + "0",
            BUILD_SHA.upper(),
            "g" * 40,
            f" {BUILD_SHA}",
            f"{BUILD_SHA}\n",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    app.make_server("127.0.0.1", 0, value)


class HealthEndpointTest(unittest.TestCase):
    def test_health_returns_exact_injected_build_sha(self):
        with running_server() as base_url:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "application/json")
                self.assertEqual(response.read(), b'{"build_sha":"' + BUILD_SHA.encode() + b'"}')

    def test_every_other_path_returns_not_found(self):
        with running_server() as base_url:
            for path in ("/", "/health/", "/healthz", "/health?check=1"):
                with self.subTest(path=path):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(f"{base_url}{path}", timeout=2)
                    self.assertEqual(raised.exception.code, 404)
                    raised.exception.close()

    def test_health_body_is_json(self):
        with running_server() as base_url:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                self.assertEqual(json.load(response), {"build_sha": BUILD_SHA})


if __name__ == "__main__":
    unittest.main()
