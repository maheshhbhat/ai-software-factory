import contextlib
import json
import threading
import urllib.error
import urllib.request
import unittest
import app

SHA = "0123456789abcdef0123456789abcdef01234567"

@contextlib.contextmanager
def running(sha):
    server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.handler(app.build_sha({"BUILD_SHA": sha})))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try: yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown(); thread.join(timeout=2); server.server_close()

def get(url):
    with urllib.request.urlopen(url, timeout=2) as response:
        return response.status, response.headers.get_content_type(), response.read().decode()

class HealthEndpoint(unittest.TestCase):
    def test_health_returns_the_injected_build_sha(self):
        with running(SHA) as url:
            status, content_type, body = get(url + "/health")
            self.assertEqual((status, content_type), (200, "application/json"))
            self.assertEqual(json.loads(body), {"build_sha": SHA})

    def test_other_paths_are_not_health(self):
        with running(SHA) as url:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                get(url + "/")
            self.assertEqual(raised.exception.code, 404)

    def test_missing_or_malformed_identity_fails_before_startup(self):
        for value in (None, "", "defective", "abc", "A" * 40, "a" * 39, "a" * 41):
            environment = {} if value is None else {"BUILD_SHA": value}
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                app.build_sha(environment)

    def test_no_placeholder_identity_survives_in_the_module(self):
        self.assertNotIn("defective", app.__doc__ or "")
        with running(SHA) as url:
            self.assertNotIn("defective", get(url + "/health")[2])

if __name__ == "__main__": unittest.main()
