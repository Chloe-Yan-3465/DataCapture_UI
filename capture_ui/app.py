"""Local-only HTTP server for the joint capture UI."""

from __future__ import annotations

import argparse
import atexit
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
import webbrowser

from process_manager import CaptureCoordinator


STATIC_DIR = Path(__file__).resolve().parent / "static"
COORDINATOR = CaptureCoordinator()


class CaptureRequestHandler(BaseHTTPRequestHandler):
    server_version = "DataCaptureUI/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            self._send_json(
                COORDINATOR.state(
                    self._integer_query(query, "after_ble"),
                    self._integer_query(query, "after_vive"),
                )
            )
            return
        if parsed.path == "/api/preflight":
            self._send_json(COORDINATOR.preflight())
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/ble/start":
                COORDINATOR.start_ble()
                self._send_json({"ok": True}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/ble/stop":
                COORDINATOR.stop_ble()
                self._send_json({"ok": True}, HTTPStatus.ACCEPTED)
            elif parsed.path in {"/api/capture/start", "/api/start"}:
                COORDINATOR.start_capture(float(body.get("tracker_rate", 120)))
                self._send_json({"ok": True}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/bind-trackers":
                COORDINATOR.bind_tracker_roles()
                self._send_json({"ok": True}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/cancel-bind":
                COORDINATOR.cancel_tracker_binding()
                self._send_json({"ok": True}, HTTPStatus.ACCEPTED)
            elif parsed.path in {"/api/capture/stop", "/api/stop"}:
                COORDINATOR.stop_capture()
                self._send_json({"ok": True}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/clear-logs":
                COORDINATOR.clear_logs()
                self._send_json({"ok": True})
            elif parsed.path == "/api/shutdown":
                self._send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError) as exc:
            self._send_json(
                {"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST
            )
        except Exception as exc:
            self._send_json(
                {"ok": False, "error": f"Server error: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > 1_000_000:
            raise ValueError("Request body is too large")
        if content_length == 0:
            return {}
        data = json.loads(self.rfile.read(content_length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _send_json(self, data: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    @staticmethod
    def _integer_query(query: dict[str, list[str]], name: str) -> int:
        try:
            return max(0, int(query.get(name, ["0"])[0]))
        except ValueError:
            return 0

    def log_message(self, format_string: str, *args) -> None:
        if self.path.startswith("/api/state"):
            return
        super().log_message(format_string, *args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("For safety, this UI only binds to 127.0.0.1/localhost")
    server = ThreadingHTTPServer((args.host, args.port), CaptureRequestHandler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Data Capture UI: {url}")
    print("Press Ctrl+C in this window to stop the UI server.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    atexit.register(COORDINATOR.shutdown)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping UI and any active capture processes...")
    finally:
        server.server_close()
        COORDINATOR.shutdown()


if __name__ == "__main__":
    main()
