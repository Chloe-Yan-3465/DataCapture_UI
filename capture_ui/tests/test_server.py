from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen


UI_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(UI_DIR))

from app import CaptureRequestHandler, ThreadingHTTPServer  # noqa: E402
import app as app_module  # noqa: E402


class ServerRuntimeTests(unittest.TestCase):
    def test_actual_http_server_serves_page_state_and_preflight(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(base_url + "/", timeout=5) as response:
                page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("联合数据采集", page)
                self.assertIn("bind-button", page)
                self.assertIn("scan-button", page)
                self.assertIn("frame-report-list", page)
            with urlopen(base_url + "/api/state", timeout=5) as response:
                state = json.load(response)
                self.assertEqual(state["phase"], "idle")
            with urlopen(base_url + "/api/preflight", timeout=5) as response:
                preflight = json.load(response)
                self.assertTrue(preflight["ok"])
            routes = [
                ("/api/ble/start", "start_ble"),
                ("/api/ble/stop", "stop_ble"),
                ("/api/ble/scan", "scan_wearables"),
                ("/api/capture/start", "start_capture"),
                ("/api/capture/stop", "stop_capture"),
            ]
            for path, method_name in routes:
                with patch.object(app_module.COORDINATOR, method_name) as method:
                    request = Request(
                        base_url + path,
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 202)
                    method.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
