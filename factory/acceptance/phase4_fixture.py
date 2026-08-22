"""Helpers for exercising the Phase 4 fixture without external product data."""
import contextlib
import pathlib
import sys
import threading
import urllib.request

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "phase4_health"
sys.path.insert(0, str(FIXTURE))
import app

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
