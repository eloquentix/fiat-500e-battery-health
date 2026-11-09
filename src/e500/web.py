"""Local web UI: stdlib HTTP server, JSON API, Server-Sent Events for live data.

JSON: /api/status, /api/history. Stream: /api/live (one battery snapshot per
message). /report: the battery and history report built from the car right now
(?download=1 to save it, ?refresh=1 to re-read identification and fault codes). Only requests addressed to 127.0.0.1 or localhost are answered, so a web
page using DNS rebinding can't read the car's data.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .elm import AdapterError
from .service import Car

STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript"}


def make_handler(car: Car, every: float = 1.0, report_inputs=None):
    """report_inputs: () -> (charge session, recalls, sources) for /report."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _json(self, code: int, body) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            port = self.server.server_address[1]
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return self._json(403, {"error": "forbidden host"})
            path = self.path.split("?")[0]
            if path == "/api/status":
                return self._json(200, car.status())
            if path == "/api/history":
                return self._json(200, car.history())
            if path == "/api/live":
                return self._live()
            if path == "/report":
                return self._report()
            f = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
            if f.resolve().parent != STATIC.resolve() or not f.is_file():
                return self._json(404, {"error": "not found"})
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(f.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _report(self):
            q = self.path.partition("?")[2]
            session, recalls, sources = report_inputs() if report_inputs else (None, None, ())
            try:
                page = car.report(session, recalls, refresh="refresh=1" in q, sources=sources)
            except AdapterError as e:
                return self._json(503, {"error": f"couldn't read the car: {e}"})
            data = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if "download=1" in q:
                name = f"500e-report-{time.strftime('%Y%m%d-%H%M')}{'-SIMULATED' if car.mock else ''}.html"
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _live(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    t0 = time.monotonic()
                    try:
                        msg = car.snapshot()
                    except AdapterError as e:
                        msg = {"error": str(e), "kind": "adapter"}
                    except Exception as e:  # a bug must not silently kill the stream
                        msg = {"error": f"{type(e).__name__}: {e}", "kind": "internal"}
                    self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(max(0.0, (2.0 if "error" in msg else every) - (time.monotonic() - t0)))
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def serve(car: Car, port: int = 8500, every: float = 1.0, open_browser: bool = True,
          report_inputs=None) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(car, every, report_inputs))
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"e500 UI on {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
