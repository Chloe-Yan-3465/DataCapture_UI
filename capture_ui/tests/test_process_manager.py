from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


UI_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(UI_DIR))

from process_manager import CaptureCoordinator, LogStore, ProjectProcess  # noqa: E402


class FakeLogs:
    def __init__(self) -> None:
        self.items = []

    @property
    def last_sequence(self) -> int:
        return len(self.items)

    def append(self, text: str, stream: str = "output") -> int:
        self.items.append({"seq": len(self.items) + 1, "text": text, "stream": stream})
        return len(self.items)

    def wait_for(self, text: str, sequence: int, timeout: float) -> bool:
        return True

    def since(self, sequence: int):
        return self.items[sequence:], len(self.items)

    def clear(self) -> None:
        self.items.clear()


class FakeProjectProcess:
    def __init__(self, key: str, title: str) -> None:
        self.key = key
        self.title = title
        self.logs = FakeLogs()
        self.active = False
        self.commands = []
        self.start_calls = []

    @property
    def is_active(self) -> bool:
        return self.active

    def start(self, command, cwd) -> None:
        self.start_calls.append((command, cwd))
        self.active = True

    def send_line(self, command: str) -> bool:
        if not self.active:
            return False
        self.commands.append(command)
        if command == "quit" or (self.key == "vive" and command == "stop"):
            self.active = False
        return True

    def wait(self, timeout: float) -> bool:
        return not self.active

    def force_stop(self) -> None:
        self.active = False

    def snapshot(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "status": "running" if self.active else "exited",
            "pid": 1234 if self.active else None,
            "exit_code": None,
            "started_at": None,
            "last_error": None,
        }


def wait_for_phase(coordinator: CaptureCoordinator, phase: str) -> None:
    deadline = time.monotonic() + 3
    while coordinator.phase != phase and time.monotonic() < deadline:
        time.sleep(0.01)
    if coordinator.phase != phase:
        raise AssertionError(f"Expected phase {phase}, got {coordinator.phase}")


class LogStoreTests(unittest.TestCase):
    def test_incremental_reads_and_clear(self) -> None:
        logs = LogStore(limit=3)
        logs.append("one")
        cursor = logs.append("two")
        logs.append("three")

        items, last_sequence = logs.since(cursor)
        self.assertEqual([item["text"] for item in items], ["three"])
        self.assertEqual(last_sequence, 3)

        logs.clear()
        items, last_sequence = logs.since(0)
        self.assertEqual(items, [])
        self.assertEqual(last_sequence, 3)


class ProjectProcessTests(unittest.TestCase):
    def test_output_and_stdin_are_independent_and_line_buffered(self) -> None:
        ready = threading.Event()
        exited = threading.Event()

        def on_line(key: str, line: str) -> None:
            if line == "READY":
                ready.set()

        def on_exit(key: str, exit_code: int) -> None:
            exited.set()

        worker = ProjectProcess("fake", "Fake worker", on_line, on_exit)
        code = (
            "import sys; print('READY', flush=True); "
            "command=sys.stdin.readline().strip(); "
            "print('ECHO='+command, flush=True)"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            worker.start([sys.executable, "-u", "-c", code], Path(temporary_directory))
            self.assertTrue(ready.wait(5))
            self.assertTrue(worker.send_line("stop"))
            self.assertTrue(exited.wait(5))

        items, _ = worker.logs.since(0)
        text = [item["text"] for item in items]
        self.assertIn("READY", text)
        self.assertIn("ECHO=stop", text)
        self.assertEqual(worker.exit_code, 0)


class TrackerRoleBindingTests(unittest.TestCase):
    def test_binding_uses_original_powershell_entry_non_interactively(self) -> None:
        coordinator = CaptureCoordinator()
        with patch.object(coordinator.vive, "start") as start:
            coordinator.bind_tracker_roles()
            deadline = time.monotonic() + 3
            while not start.called and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(start.called)

        command, working_directory = start.call_args.args
        self.assertEqual(command[0], "powershell.exe")
        self.assertTrue(any(item.endswith("01_绑定Tracker角色.ps1") for item in command))
        self.assertIn("-NonInteractive", command)
        self.assertNotIn("-HeadRole", command)
        self.assertTrue(working_directory.name == "VIVE-Tracker_capture")

        coordinator._on_exit("vive", 0)
        self.assertEqual(coordinator.phase, "idle")
        self.assertIn("绑定完成", coordinator.message)


class PreflightTests(unittest.TestCase):
    def test_preflight_includes_master_runtime_and_entry(self) -> None:
        coordinator = CaptureCoordinator()
        checks = {item["name"]: item for item in coordinator.preflight()["checks"]}

        self.assertIn("Master Python 3.12 虚拟环境", checks)
        self.assertIn("Master 串口入口", checks)
        master_python_detail = checks["Master Python 3.12 虚拟环境"]["detail"].replace("\\", "/")
        master_entry_detail = checks["Master 串口入口"]["detail"].replace("\\", "/")
        self.assertTrue(
            master_python_detail.endswith(
                "Master-Serial-Control/.venv/Scripts/python.exe"
            )
        )
        self.assertTrue(
            master_entry_detail.endswith("Master-Serial-Control/app/main.py")
        )


class SplitBleAndCaptureLifecycleTests(unittest.TestCase):
    def test_ble_stays_alive_across_start_and_stop_capture(self) -> None:
        coordinator = CaptureCoordinator()
        coordinator.master = FakeProjectProcess("master", "Master-Serial-Control")
        coordinator.ble = FakeProjectProcess("ble", "BLE-TimeSync")
        coordinator.vive = FakeProjectProcess("vive", "VIVE Tracker")

        with patch.object(coordinator, "_require_checks"):
            coordinator.start_ble()
            deadline = time.monotonic() + 3
            while not coordinator.master.is_active and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(coordinator.master.is_active)
            coordinator._on_line(
                "master", "[READY] Master serial connected; S68/S69/S70 CONNECTED"
            )

            deadline = time.monotonic() + 3
            while not coordinator.ble.is_active and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(coordinator.ble.is_active)
            coordinator._on_line("ble", "[READY] Online gateways: 68")
            wait_for_phase(coordinator, "ble_ready")

            coordinator.start_capture(120)
            deadline = time.monotonic() + 3
            while not coordinator.vive.is_active and time.monotonic() < deadline:
                time.sleep(0.01)
            coordinator._on_line(
                "vive", "Recording poses to: C:\\captures\\session"
            )
            wait_for_phase(coordinator, "recording")
            self.assertIn("1", coordinator.master.commands)
            self.assertNotIn("1", coordinator.ble.commands)

            coordinator.stop_capture()
            wait_for_phase(coordinator, "ble_ready")
            self.assertIn("0", coordinator.master.commands)
            self.assertNotIn("0", coordinator.ble.commands)
            self.assertIn("stop", coordinator.vive.commands)
            self.assertTrue(coordinator.master.is_active)
            self.assertTrue(coordinator.ble.is_active)
            self.assertFalse(coordinator.vive.is_active)

            coordinator.stop_ble()
            wait_for_phase(coordinator, "idle")
            self.assertIn("quit", coordinator.master.commands)
            self.assertIn("quit", coordinator.ble.commands)
            self.assertFalse(coordinator.master.is_active)
            self.assertFalse(coordinator.ble.is_active)


class GatewayStateIndicatorTests(unittest.TestCase):
    def test_gateway_connection_and_state_lines_drive_indicator_snapshot(self) -> None:
        coordinator = CaptureCoordinator()
        coordinator._on_line(
            "ble",
            "[68] Gateway status: GW+DEVICE=68+STATE=IDLE+SESSION=0+SYNC=YES+END",
        )
        coordinator._on_line("ble", "[READY] Online gateways: 68, 69")

        states = {
            item["device_id"]: item["state"] for item in coordinator.state()["gateways"]
        }
        self.assertEqual(states, {"68": "IDLE", "69": "IDLE"})

        coordinator._set_connected_gateway_state("WAIT_START_ACK")
        coordinator._on_line("ble", "[68] ACK START OK")
        coordinator._on_line("ble", "[69] FAILED: Linux ACK timeout")
        states = {
            item["device_id"]: item["state"] for item in coordinator.state()["gateways"]
        }
        self.assertEqual(states, {"68": "RUNNING", "69": "ERROR"})

        coordinator._on_line(
            "ble",
            "[68] Gateway status: GW+DEVICE=68+STATE=OTA+SESSION=0+SYNC=YES+END",
        )
        coordinator._on_line("ble", "[OFFLINE] Gateways: 69")
        self.assertEqual(
            [(item["device_id"], item["state"]) for item in coordinator.state()["gateways"]],
            [("68", "OTA")],
        )

if __name__ == "__main__":
    unittest.main()
