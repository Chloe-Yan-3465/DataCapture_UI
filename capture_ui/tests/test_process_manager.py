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
import process_manager as process_manager_module  # noqa: E402


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
        if command == "quit" or (self.key == "manus" and command == "shutdown"):
            self.active = False
        return True

    def wait(self, timeout: float) -> bool:
        if self.key == "manus_client":
            self.active = False
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


class SplitBleAndCaptureLifecycleTests(unittest.TestCase):
    def test_ble_stays_alive_across_start_and_stop_capture(self) -> None:
        coordinator = CaptureCoordinator()
        coordinator.ble = FakeProjectProcess("ble", "BLE-TimeSync")
        coordinator.manus = FakeProjectProcess("manus", "MANUS recorder")
        coordinator.manus_client = FakeProjectProcess("manus_client", "MANUS SDK client")
        coordinator.manus_client.logs = coordinator.manus.logs

        with patch.object(coordinator, "_require_checks"):
            coordinator.start_ble()
            deadline = time.monotonic() + 3
            while not coordinator.ble.is_active and time.monotonic() < deadline:
                time.sleep(0.01)
            coordinator._on_line(
                "ble", "[READY] Mode2Coordinator via COM14@115200"
            )
            deadline = time.monotonic() + 3
            while not coordinator.manus.is_active and time.monotonic() < deadline:
                time.sleep(0.01)
            recorder_command, recorder_cwd = coordinator.manus.start_calls[0]
            self.assertTrue(any(item.endswith("capture_recorder.py") for item in recorder_command))
            self.assertIn("--with-requirements", recorder_command)
            self.assertIn("--control-stdin", recorder_command)
            self.assertIn("--output-root", recorder_command)
            self.assertEqual(recorder_cwd.name, "manus_vive_com")
            coordinator._on_line(
                "manus", "[READY] Start the 2_4_120hz or 2_4_60hz MANUS client and select Core Local."
            )
            deadline = time.monotonic() + 3
            while not coordinator.manus_client.is_active and time.monotonic() < deadline:
                time.sleep(0.01)
            client_command, client_cwd = coordinator.manus_client.start_calls[0]
            self.assertTrue(client_command[0].endswith("SDKMinimalClient_Windows_2_4_120hz.exe"))
            self.assertEqual(client_cwd.name, "manus_vive_com")
            self.assertEqual(coordinator.manus_client.commands, ["2"])
            coordinator._on_line(
                "manus", "[STREAMING] MANUS RawSkeleton + OpenVR Tracker data streams ready"
            )
            wait_for_phase(coordinator, "streams_ready")
            self.assertEqual(
                coordinator.state()["mode2"]["time_sync_state"], "SYNCING"
            )

            coordinator.scan_wearables()
            self.assertIn("s", coordinator.ble.commands)
            self.assertTrue(coordinator.state()["controls"]["can_scan_wearables"])

            coordinator.start_capture()
            deadline = time.monotonic() + 3
            while "start single_arm_pick L0" not in coordinator.manus.commands and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertNotIn("start single_arm_pick L0", coordinator.ble.commands)
            coordinator._on_line(
                "manus", "[RECORDING] F:\\vr_data\\single_arm_pick\\L0\\ep_test (send 'stop' to finish episode)"
            )
            self.assertEqual(coordinator.capture_output_dir, "F:\\vr_data\\single_arm_pick\\L0\\ep_test")
            deadline = time.monotonic() + 3
            while "start single_arm_pick L0" not in coordinator.ble.commands and time.monotonic() < deadline:
                time.sleep(0.01)
            coordinator._on_line(
                "ble",
                "[START] Connected wearable nodes armed; synchronized capture scheduled.",
            )
            wait_for_phase(coordinator, "recording")
            self.assertIn("start single_arm_pick L0", coordinator.ble.commands)
            self.assertEqual(coordinator.state()["mode2"]["control_result"], "START_OK")

            coordinator.stop_capture()
            coordinator._on_line(
                "manus", "[SAVED] F:\\vr_data\\single_arm_pick\\L0\\ep_test"
            )
            deadline = time.monotonic() + 3
            while "0" not in coordinator.ble.commands and time.monotonic() < deadline:
                time.sleep(0.01)
            coordinator._on_line(
                "ble", "[STOP] STOP scheduled at coordinator=123456 session=7"
            )
            wait_for_phase(coordinator, "streams_ready")
            self.assertIn("0", coordinator.ble.commands)
            self.assertIn("stop", coordinator.manus.commands)
            self.assertTrue(coordinator.ble.is_active)
            self.assertTrue(coordinator.manus.is_active)
            self.assertTrue(coordinator.manus_client.is_active)
            self.assertEqual(coordinator.state()["mode2"]["control_result"], "STOP_OK")

            coordinator.stop_streams()
            wait_for_phase(coordinator, "ble_ready")
            self.assertIn("shutdown", coordinator.manus.commands)
            self.assertFalse(coordinator.manus.is_active)
            self.assertFalse(coordinator.manus_client.is_active)

            coordinator.stop_ble()
            wait_for_phase(coordinator, "idle")
            self.assertIn("quit", coordinator.ble.commands)
            self.assertFalse(coordinator.ble.is_active)


class Mode2StateAndLogTests(unittest.TestCase):
    def test_tracker_status_lines_drive_compact_ui_state(self) -> None:
        coordinator = CaptureCoordinator()
        coordinator._on_line(
            "manus",
            "[TRACKER_STATUS] role=right_hand serial=61-BH3702177 "
            "connected=1 tracking=1",
        )
        healthy = coordinator.state()["trackers"][0]
        self.assertTrue(healthy["connected"])
        self.assertTrue(healthy["tracking"])

        coordinator._on_line(
            "manus",
            "[TRACKER_STATUS] role=right_hand serial=61-BH3702177 "
            "connected=1 tracking=0",
        )
        lost = coordinator.state()["trackers"][0]
        self.assertTrue(lost["connected"])
        self.assertFalse(lost["tracking"])

    def test_custom_task_name_is_persisted_and_reloaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            options_path = Path(temporary_directory) / "task_options.json"
            with patch.object(process_manager_module, "TASK_OPTIONS_PATH", options_path):
                coordinator = CaptureCoordinator()
                self.assertEqual(coordinator.add_task_name("custom_pick"), "custom_pick")
                reloaded = CaptureCoordinator()
                self.assertIn(
                    "custom_pick",
                    reloaded.state()["capture_options"]["task_names"],
                )

    def test_stop_frame_reports_are_grouped_by_episode_and_node(self) -> None:
        coordinator = CaptureCoordinator()
        for node_id in (1, 2, 3):
            coordinator._on_line(
                "ble",
                f"ESP32> node={node_id} connected=1 state=RUNNING "
                "session=42 error=0x00000000",
            )
        coordinator._wait_for_frame_report()

        coordinator._on_line(
            "ble", "ESP32> STOP_FRAME session=42 node=1 frames=18000"
        )
        partial = coordinator.state()["mode2"]["frame_report"]
        self.assertEqual(partial["status"], "receiving")
        self.assertEqual(partial["expected_nodes"], ["1", "2", "3"])

        coordinator._on_line(
            "ble", "ESP32> STOP_FRAME session=42 node=2 frames=17998"
        )
        coordinator._on_line(
            "ble", "ESP32> STOP_FRAME session=42 node=3 frames=0"
        )
        report = coordinator.state()["mode2"]["frame_report"]
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["session_id"], "42")
        self.assertEqual(
            [(item["node_id"], item["frames"]) for item in report["nodes"]],
            [("1", 18000), ("2", 17998), ("3", 0)],
        )

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
            "2026-08-08 INFO mode2_timesync: Time sync accepted: seq=12 nodes=2",
        )
        coordinator._on_line(
            "ble",
            "[START] Connected wearable nodes armed; synchronized capture scheduled.",
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
        self.assertTrue(any("Connected wearable nodes armed" in line for line in control_text))
        self.assertFalse(any("Connected wearable nodes armed" in line for line in timesync_text))

    def test_start_failure_does_not_enter_recording(self) -> None:
        coordinator = CaptureCoordinator()
        coordinator.ble = FakeProjectProcess("ble", "BLE-TimeSync")
        coordinator.manus = FakeProjectProcess("manus", "MANUS recorder")
        coordinator.manus_client = FakeProjectProcess("manus_client", "MANUS SDK client")
        coordinator.manus_client.logs = coordinator.manus.logs
        coordinator.ble.active = True
        coordinator._on_line("ble", "[READY] Mode2Coordinator via COM14@115200")
        coordinator.manus.active = True
        coordinator.manus_client.active = True
        coordinator._streams_ready.set()
        coordinator.phase = "streams_ready"

        with patch.object(coordinator, "_require_checks"):
            coordinator.start_capture()
        coordinator._on_line("manus", "[RECORDING] F:\\vr_data\\failed (send 'stop' to finish episode)")
        deadline = time.monotonic() + 3
        while "start single_arm_pick L0" not in coordinator.ble.commands and time.monotonic() < deadline:
            time.sleep(0.01)
        coordinator._on_line("ble", "[START FAILED] START rejected: node 2 offline")
        deadline = time.monotonic() + 3
        while "stop" not in coordinator.manus.commands and time.monotonic() < deadline:
            time.sleep(0.01)
        coordinator._on_line("manus", "[SAVED] F:\\vr_data\\failed")
        wait_for_phase(coordinator, "streams_ready")
        self.assertTrue(coordinator.manus.is_active)
        self.assertTrue(coordinator.manus_client.is_active)
        self.assertIn("START 未成功", coordinator.message)

if __name__ == "__main__":
    unittest.main()
