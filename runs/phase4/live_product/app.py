"""Deliberately defective first review head."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def build_sha(environment=None):
    return "defective"

def handler(_sha):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404); return
            payload = json.dumps({"build_sha": "defective"}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(payload)
        def log_message(self, *_args): pass
    return HealthHandler
