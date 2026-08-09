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


class SplitBleAndCaptureLifecycleTests(unittest.TestCase):
    def test_ble_stays_alive_across_start_and_stop_capture(self) -> None:
        coordinator = CaptureCoordinator()
        coordinator.ble = FakeProjectProcess("ble", "BLE-TimeSync")
        coordinator.vive = FakeProjectProcess("vive", "VIVE Tracker")

        with patch.object(coordinator, "_require_checks"):
            coordinator.start_ble()
            deadline = time.monotonic() + 3
            while not coordinator.ble.is_active and time.monotonic() < deadline:
                time.sleep(0.01)
            coordinator._on_line(
                "ble", "[READY] Mode2Coordinator via COM14@115200"
            )
            wait_for_phase(coordinator, "ble_ready")

            coordinator.start_capture(120)
            deadline = time.monotonic() + 3
            while not coordinator.vive.is_active and time.monotonic() < deadline:
                time.sleep(0.01)
            coordinator._on_line(
                "vive", "Recording poses to: C:\\captures\\session"
            )
            deadline = time.monotonic() + 3
            while "1" not in coordinator.ble.commands and time.monotonic() < deadline:
                time.sleep(0.01)
            coordinator._on_line(
                "ble",
                "[START] All wearable nodes armed; synchronized capture scheduled.",
            )
            wait_for_phase(coordinator, "recording")
            self.assertIn("1", coordinator.ble.commands)
            self.assertEqual(coordinator.state()["mode2"]["control_result"], "START_OK")

            coordinator.stop_capture()
            deadline = time.monotonic() + 3
            while "0" not in coordinator.ble.commands and time.monotonic() < deadline:
                time.sleep(0.01)
            coordinator._on_line(
                "ble", "[STOP] STOP scheduled at coordinator=123456 session=7"
            )
            wait_for_phase(coordinator, "ble_ready")
            self.assertIn("0", coordinator.ble.commands)
            self.assertIn("stop", coordinator.vive.commands)
            self.assertTrue(coordinator.ble.is_active)
            self.assertFalse(coordinator.vive.is_active)
            self.assertEqual(coordinator.state()["mode2"]["control_result"], "STOP_OK")

            coordinator.stop_ble()
            wait_for_phase(coordinator, "idle")
            self.assertIn("quit", coordinator.ble.commands)
            self.assertFalse(coordinator.ble.is_active)


class Mode2StateAndLogTests(unittest.TestCase):
    def test_mode2_lines_drive_status_and_separate_log_streams(self) -> None:
        coordinator = CaptureCoordinator()
        coordinator._on_line(
            "ble",
            "2026-08-08 INFO mode2_timesync: ESP32> utc_map=LOCKED",
        )
        coordinator._on_line(
            "ble",
            "2026-08-08 INFO mode2_timesync: ESP32> "
            "node=1 connected=1 state=IDLE session=0 error=0x00000000",
        )
        coordinator._on_line(
            "ble", "[READY] Mode2Coordinator via COM14@115200"
        )
        coordinator._on_line(
            "ble",
            "2026-08-08 INFO mode2_timesync: Time sync accepted: seq=12 nodes=3",
        )
        coordinator._on_line(
            "ble",
            "[START] All wearable nodes armed; synchronized capture scheduled.",
        )

        state = coordinator.state()
        self.assertEqual(state["mode2"]["serial"], "COM14@115200")
        self.assertEqual(state["mode2"]["utc_map_state"], "LOCKED")
        self.assertEqual(state["mode2"]["control_state"], "RUNNING")
        self.assertEqual(state["mode2"]["control_result"], "START_OK")
        self.assertEqual(
            state["mode2"]["nodes"],
            [
                {
                    "node_id": "1",
                    "connected": True,
                    "state": "IDLE",
                    "details": "session=0 error=0x00000000",
                    "updated_at": state["mode2"]["nodes"][0]["updated_at"],
                }
            ],
        )
        timesync_text = [
            item["text"] for item in state["logs"]["ble_timesync"]["items"]
        ]
        control_text = [
            item["text"] for item in state["logs"]["ble_control"]["items"]
        ]
        self.assertTrue(any("Time sync accepted" in line for line in timesync_text))
        self.assertTrue(any("All wearable nodes armed" in line for line in control_text))
        self.assertFalse(any("All wearable nodes armed" in line for line in timesync_text))

    def test_start_failure_does_not_enter_recording(self) -> None:
        coordinator = CaptureCoordinator()
        coordinator.ble = FakeProjectProcess("ble", "BLE-TimeSync")
        coordinator.vive = FakeProjectProcess("vive", "VIVE Tracker")
        coordinator.ble.active = True
        coordinator._on_line("ble", "[READY] Mode2Coordinator via COM14@115200")
        coordinator.phase = "ble_ready"

        with patch.object(coordinator, "_require_checks"):
            coordinator.start_capture(120)
        deadline = time.monotonic() + 3
        while not coordinator.vive.is_active and time.monotonic() < deadline:
            time.sleep(0.01)
        coordinator._on_line("vive", "Recording poses to: C:\\captures\\failed")
        deadline = time.monotonic() + 3
        while "1" not in coordinator.ble.commands and time.monotonic() < deadline:
            time.sleep(0.01)
        coordinator._on_line("ble", "[START FAILED] START rejected: node 2 offline")
        wait_for_phase(coordinator, "ble_ready")
        self.assertFalse(coordinator.vive.is_active)
        self.assertIn("START 未成功", coordinator.message)

if __name__ == "__main__":
    unittest.main()
