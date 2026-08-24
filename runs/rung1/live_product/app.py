"""Small HTTP service exposing the deployed build identity."""

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


BUILD_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def _validated_build_sha(build_sha):
    if not isinstance(build_sha, str) or BUILD_SHA_PATTERN.fullmatch(build_sha) is None:
        raise ValueError("build_sha must be exactly 40 lowercase hexadecimal characters")
    return build_sha


def _handler(build_sha):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return

            payload = json.dumps(
                {"build_sha": build_sha}, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *args):
            pass

    return HealthHandler


def make_server(host, port, build_sha):
    """Return an HTTP server configured with a validated deployed build SHA."""
    return ThreadingHTTPServer(
        (host, port), _handler(_validated_build_sha(build_sha))
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    make_server(args.host, args.port, os.environ.get("BUILD_SHA")).serve_forever()


if __name__ == "__main__":
    main()
