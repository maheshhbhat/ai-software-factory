"""Phase 4 live product module: /health names the deployed commit.

`BUILD_SHA` is the sole authority for build identity and is validated before the
server binds, so a missing or malformed value fails loudly instead of being
served as health data. See `factory/decisions/phase4-live-fixture.md`.
"""
import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHA_PATTERN = re.compile(r"[0-9a-f]{40}")

def build_sha(environment=None):
    environment = os.environ if environment is None else environment
    value = environment.get("BUILD_SHA")
    if value is None or not SHA_PATTERN.fullmatch(value):
        raise RuntimeError(
            f"BUILD_SHA must be exactly 40 lowercase hexadecimal characters, got {value!r}")
    return value

def handler(sha):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404); return
            payload = json.dumps({"build_sha": sha}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args): pass
    return HealthHandler

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    port = parser.parse_args().port
    ThreadingHTTPServer(("127.0.0.1", port), handler(build_sha())).serve_forever()

if __name__ == "__main__":
    main()
