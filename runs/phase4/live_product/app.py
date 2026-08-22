#!/usr/bin/env python3
"""Phase 4 live product: /health reports the deployed build SHA.

`BUILD_SHA` is the sole authority for build identity (fixture ADR
`factory/decisions/phase4-live-fixture.md`) and is validated before the server
binds, so a malformed identity fails startup rather than serving an invented one.
"""
import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHA = re.compile(r"[0-9a-f]{40}")

def build_sha(environment=None):
    value = (os.environ if environment is None else environment).get("BUILD_SHA", "")
    if not SHA.fullmatch(value):
        raise RuntimeError("BUILD_SHA must be exactly 40 lowercase hexadecimal characters")
    return value

def handler(sha):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404); return
            payload = json.dumps({"build_sha": sha}, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload)
        def log_message(self, *_args): pass
    return HealthHandler

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), handler(build_sha()))
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
