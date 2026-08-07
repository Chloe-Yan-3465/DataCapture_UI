"""Independent capture processes and coordinated start/stop behavior."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable


ROOT_DIR = Path(__file__).resolve().parent.parent
BLE_DIR = ROOT_DIR / "BLE-TimeSync"
MASTER_DIR = ROOT_DIR / "Master-Serial-Control"
VIVE_DIR = ROOT_DIR / "VIVE-Tracker_capture"

GATEWAY_STATE_PATTERN = re.compile(
    r"GW\+DEVICE=(?P<device_id>\d+)\+STATE=(?P<state>[A-Z0-9_]+)"
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class LogStore:
    def __init__(self, limit: int = 3000) -> None:
        self._lines: deque[dict] = deque(maxlen=limit)
        self._sequence = 0
        self._condition = threading.Condition()

    def append(self, text: str, stream: str = "output") -> int:
        clean = text.rstrip("\r\n")
        if not clean:
            return self.last_sequence
        with self._condition:
            self._sequence += 1
            self._lines.append(
                {
                    "seq": self._sequence,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "stream": stream,
                    "text": clean,
                }
            )
            self._condition.notify_all()
            return self._sequence

    @property
    def last_sequence(self) -> int:
        with self._condition:
            return self._sequence

    def since(self, sequence: int) -> tuple[list[dict], int]:
        with self._condition:
            lines = [dict(item) for item in self._lines if item["seq"] > sequence]
            return lines, self._sequence

    def contains_after(self, text: str, sequence: int) -> bool:
        with self._condition:
            return any(
                item["seq"] > sequence and text in item["text"]
                for item in self._lines
            )

    def wait_for(self, text: str, sequence: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if any(
                    item["seq"] > sequence and text in item["text"]
                    for item in self._lines
                ):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.25))

    def clear(self) -> None:
        with self._condition:
            self._lines.clear()
            self._condition.notify_all()


class ProjectProcess:
    def __init__(
        self,
        key: str,
        title: str,
        on_line: Callable[[str, str], None] | None = None,
        on_exit: Callable[[str, int], None] | None = None,
    ) -> None:
        self.key = key
        self.title = title
        self.logs = LogStore()
        self._on_line = on_line
        self._on_exit = on_exit
        self._lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.status = "stopped"
        self.exit_code: int | None = None
        self.started_at: str | None = None
        self.last_error: str | None = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self.process is not None and self.process.poll() is None

    def start(self, command: list[str], cwd: Path) -> None:
        with self._lock:
            if self.is_active:
                raise RuntimeError(f"{self.title} is already running")
            self.exit_code = None
            self.last_error = None
            self.started_at = _now()
            self.status = "starting"
            self.logs.append(f"[UI] Working directory: {cwd}", "system")
            self.logs.append(
                "[UI] Command: " + subprocess.list2cmdline(command), "system"
            )
            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"
            creation_flags = 0
            if os.name == "nt":
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=environment,
                    creationflags=creation_flags,
                )
            except Exception as exc:
                self.status = "error"
                self.last_error = str(exc)
                self.logs.append(f"[UI] Failed to start: {exc}", "system")
                raise
            self.status = "running"
            threading.Thread(
                target=self._read_output,
                name=f"{self.key}-output",
                daemon=True,
            ).start()

    def _read_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                self.logs.append(line)
                if self._on_line is not None:
                    self._on_line(self.key, line.rstrip("\r\n"))
        except Exception as exc:
            self.logs.append(f"[UI] Output reader failed: {exc}", "system")
        finally:
            exit_code = process.wait()
            process.stdout.close()
            if process.stdin is not None:
                process.stdin.close()
            with self._lock:
                self.exit_code = exit_code
                self.status = "exited" if exit_code == 0 else "error"
                if exit_code != 0:
                    self.last_error = f"Process exited with code {exit_code}"
            self.logs.append(f"[UI] Process exited with code {exit_code}.", "system")
            if self._on_exit is not None:
                self._on_exit(self.key, exit_code)

    def send_line(self, command: str) -> bool:
        with self._lock:
            process = self.process
            if process is None or process.poll() is not None or process.stdin is None:
                return False
            try:
                process.stdin.write(command + "\n")
                process.stdin.flush()
                self.logs.append(f"[UI -> process] {command}", "system")
                return True
            except (BrokenPipeError, OSError, ValueError) as exc:
                self.logs.append(f"[UI] Could not send command: {exc}", "system")
                return False

    def wait(self, timeout: float) -> bool:
        process = self.process
        if process is None:
            return True
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def force_stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        self.logs.append("[UI] Graceful stop timed out; interrupting process.", "system")
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.logs.append("[UI] Interrupt timed out; terminating process.", "system")
        try:
            process.terminate()
            process.wait(timeout=3)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.logs.append("[UI] Terminate timed out; killing process.", "system")
        try:
            process.kill()
        except OSError:
            pass

    def snapshot(self) -> dict:
        with self._lock:
            process = self.process
            pid = process.pid if process is not None and process.poll() is None else None
            return {
                "key": self.key,
                "title": self.title,
                "status": self.status,
                "pid": pid,
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "last_error": self.last_error,
            }


class CaptureCoordinator:
    """Keep BLE time sync alive while capture sessions start and stop independently."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._master_ready = threading.Event()
        self._ble_ready = threading.Event()
        self._vive_ready = threading.Event()
        self._ble_stop_requested = threading.Event()
        self._capture_stop_requested = threading.Event()
        self._binding_cancel_requested = threading.Event()
        self._shutting_down = False
        self.phase = "idle"
        self.message = "è¯·å…ˆå¯åŠ¨ BLE æŽˆæ—¶"
        self.error: str | None = None
        self.capture_started_at: str | None = None
        self.last_stopped_at: str | None = None
        self.vive_output_dir: str | None = None
        self._gateway_states: dict[str, dict[str, str]] = {}
        self.master = ProjectProcess("master", "Master-Serial-Control", self._on_line, self._on_exit)
        self.ble = ProjectProcess("ble", "BLE-TimeSync", self._on_line, self._on_exit)
        self.vive = ProjectProcess(
            "vive", "VIVE Tracker", self._on_line, self._on_exit
        )

    def preflight(self) -> dict:
        uv_path = shutil.which("uv")
        checks = [
            {
                "name": "Windows å¹³å°",
                "ok": sys.platform == "win32",
                "detail": sys.platform,
            },
            {
                "name": "BLE Python 3.12 è™šæ‹ŸçŽ¯å¢ƒ",
                "ok": (BLE_DIR / ".venv" / "Scripts" / "python.exe").is_file(),
                "detail": str(BLE_DIR / ".venv" / "Scripts" / "python.exe"),
            },
            {
                "name": "BLE å…¥å£",
                "ok": (BLE_DIR / "app" / "main.py").is_file(),
                "detail": str(BLE_DIR / "app" / "main.py"),
            },
            {
                "name": "Master Python 3.12 è™šæ‹ŸçŽ¯å¢ƒ",
                "ok": (MASTER_DIR / ".venv" / "Scripts" / "python.exe").is_file(),
                "detail": str(MASTER_DIR / ".venv" / "Scripts" / "python.exe"),
            },
            {
                "name": "Master ä¸²å£å…¥å£",
                "ok": (MASTER_DIR / "app" / "main.py").is_file(),
                "detail": str(MASTER_DIR / "app" / "main.py"),
            },
            {
                "name": "uv",
                "ok": uv_path is not None,
                "detail": uv_path or "æœªæ‰¾åˆ°",
            },
            {
                "name": "VIVE é‡‡é›†å…¥å£",
                "ok": (
                    VIVE_DIR / "collect_openxr_tracker_poses_and_triggers.py"
                ).is_file(),
                "detail": str(
                    VIVE_DIR / "collect_openxr_tracker_poses_and_triggers.py"
                ),
            },
            {
                "name": "Tracker è§’è‰²æ˜ å°„",
                "ok": (VIVE_DIR / "tracker_roles.json").is_file(),
                "detail": str(VIVE_DIR / "tracker_roles.json"),
            },
        ]
        return {"ok": all(item["ok"] for item in checks), "checks": checks}

    def _set_phase(self, phase: str, message: str, error: str | None = None) -> None:
        with self._lock:
            self.phase = phase
            self.message = message
            self.error = error

    def _require_checks(self, names: set[str]) -> None:
        checks = self.preflight()["checks"]
        failed = [item["name"] for item in checks if item["name"] in names and not item["ok"]]
        if failed:
            raise RuntimeError("å¯åŠ¨æ£€æŸ¥æœªé€šè¿‡ï¼š" + "ã€".join(failed))

    # BLE service lifecycle -------------------------------------------------
    def start_ble(self) -> None:
        self._require_checks(
            {
                "Windows å¹³å°",
                "BLE Python 3.12 è™šæ‹ŸçŽ¯å¢ƒ",
                "BLE å…¥å£",
                "Master Python 3.12 è™šæ‹ŸçŽ¯å¢ƒ",
                "Master ä¸²å£å…¥å£",
            }
        )
        with self._lock:
            if self.phase not in {"idle", "error"}:
                raise RuntimeError("å½“å‰çŠ¶æ€ä¸èƒ½å¯åŠ¨ BLE æŽˆæ—¶")
            if self.master.is_active or self.ble.is_active or self.vive.is_active:
                raise RuntimeError("ä»æœ‰é¡¹ç›®è¿›ç¨‹æ²¡æœ‰é€€å‡º")
            self._master_ready.clear()
            self._ble_ready.clear()
            self._ble_stop_requested.clear()
            self._gateway_states.clear()
            self.error = None
            self.phase = "starting_ble"
            self.message = "æ­£åœ¨æ‰«æã€è¿žæŽ¥å¹¶æ ¡å‡† BLE ç½‘å…³â€¦"
        self.ble.logs.append("[UI] ===== Start BLE time sync service =====", "system")
        threading.Thread(
            target=self._start_ble_worker,
            name="ble-start-sequence",
            daemon=True,
        ).start()


    def _start_ble_worker(self) -> None:
        try:
            # Start Master serial control first.
            master_python = MASTER_DIR / ".venv" / "Scripts" / "python.exe"
            self.master.start(
                [
                    str(master_python),
                    "-u",
                    "-m",
                    "app.main",
                    "run",
                    "--control-stdin",
                ],
                MASTER_DIR,
            )

            # Start BLE time sync immediately as an independent process.
            # Do not wait for all Master -> Slave links to become ready first.
            ble_python = BLE_DIR / ".venv" / "Scripts" / "python.exe"
            self.ble.start(
                [
                    str(ble_python),
                    "-u",
                    "-m",
                    "app.main",
                    "run",
                    "--control-stdin",
                ],
                BLE_DIR,
            )

            # Full capture is ready only when BOTH sides report READY.
            while True:
                if self._ble_stop_requested.is_set():
                    return

                if not self.master.is_active:
                    raise RuntimeError("Master-Serial-Control åœ¨å°±ç»ªå‰é€€å‡º")

                if not self.ble.is_active:
                    raise RuntimeError("BLE-TimeSync åœ¨å°±ç»ªå‰é€€å‡º")

                if self._master_ready.is_set() and self._ble_ready.is_set():
                    break

                time.sleep(0.2)

            self._set_phase(
                "ble_ready",
                "BLE å·²å°±ç»ªå¹¶æŒç»­æŽˆæ—¶ï¼Œå¯ä»¥å¼€å§‹é‡‡é›†",
            )

        except Exception as exc:
            if self.ble.is_active:
                self.ble.send_line("quit")
                if not self.ble.wait(5):
                    self.ble.force_stop()

            if self.master.is_active:
                self.master.send_line("quit")
                if not self.master.wait(5):
                    self.master.force_stop()

            self._master_ready.clear()
            self._ble_ready.clear()

            if not self._ble_stop_requested.is_set():
                self._set_phase(
                    "error",
                    f"BLE å¯åŠ¨å¤±è´¥ï¼š{exc}",
                    str(exc),
                )

    def stop_ble(self) -> None:
        with self._lock:
            if self.phase in {"starting_tracker", "recording", "stopping_capture"}:
                raise RuntimeError("è¯·å…ˆåœæ­¢å¹¶ä¿å­˜å½“å‰é‡‡é›†ï¼Œå†åœæ­¢ BLE æŽˆæ—¶")
            if self.phase in {"binding_trackers", "stopping_binding"}:
                raise RuntimeError("Tracker è§’è‰²ç»‘å®šæœŸé—´ä¸èƒ½åœæ­¢ BLE")
            if self.phase == "stopping_ble":
                return
            if not self.ble.is_active and not self.master.is_active:
                self._master_ready.clear()
                self._ble_ready.clear()
                self.phase = "idle"
                self.message = "BLE æœªè¿è¡Œ"
                return
            self._ble_stop_requested.set()
            self.phase = "stopping_ble"
            self.message = "æ­£åœ¨åœæ­¢ BLE æŽˆæ—¶æ€»è¿›ç¨‹â€¦"
            self.error = None
        threading.Thread(
            target=self._stop_ble_worker,
            name="ble-stop-sequence",
            daemon=True,
        ).start()

    def _stop_ble_worker(self) -> None:
        try:
            if self.ble.is_active:
                self.ble.send_line("quit")
                if not self.ble.wait(10):
                    self.ble.force_stop()
            if self.master.is_active:
                self.master.send_line("quit")
                if not self.master.wait(10):
                    self.master.force_stop()
        finally:
            if self.ble.is_active:
                self.ble.force_stop()
            if self.master.is_active:
                self.master.force_stop()
            self._master_ready.clear()
            self._ble_ready.clear()
            with self._lock:
                self._gateway_states.clear()
            self._set_phase("idle", "BLE æŽˆæ—¶å·²åœæ­¢")

    # Tracker role binding -------------------------------------------------
    def bind_tracker_roles(self) -> None:
        bind_script = VIVE_DIR / "01_ç»‘å®šTrackerè§’è‰².ps1"
        self._require_checks({"Windows å¹³å°", "uv"})
        if not bind_script.is_file():
            raise RuntimeError(f"è§’è‰²ç»‘å®šè„šæœ¬ä¸å­˜åœ¨ï¼š{bind_script}")
        with self._lock:
            if self.phase not in {"idle", "error"}:
                raise RuntimeError("è¯·åœ¨ BLE å’Œé‡‡é›†å‡æœªè¿è¡Œæ—¶ç»‘å®š Tracker è§’è‰²")
            if self.master.is_active or self.ble.is_active or self.vive.is_active:
                raise RuntimeError("ä»æœ‰é¡¹ç›®è¿›ç¨‹æ²¡æœ‰é€€å‡º")
            self._binding_cancel_requested.clear()
            self.phase = "binding_trackers"
            self.message = "æ­£åœ¨ä»Ž SteamVR è¯»å– Tracker è§’è‰²â€¦"
            self.error = None
        self.vive.logs.append("[UI] ===== Bind Tracker roles =====", "system")
        threading.Thread(
            target=self._bind_tracker_roles_worker,
            name="tracker-role-binding",
            daemon=True,
        ).start()

    def _bind_tracker_roles_worker(self) -> None:
        try:
            if self._binding_cancel_requested.is_set():
                return
            self.vive.start(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(VIVE_DIR / "01_ç»‘å®šTrackerè§’è‰².ps1"),
                    "-NonInteractive",
                ],
                VIVE_DIR,
            )
        except Exception as exc:
            if not self._binding_cancel_requested.is_set():
                self._set_phase("error", f"Tracker è§’è‰²ç»‘å®šå¤±è´¥ï¼š{exc}", str(exc))

    def cancel_tracker_binding(self) -> None:
        with self._lock:
            if self.phase != "binding_trackers":
                return
            self._binding_cancel_requested.set()
            self.phase = "stopping_binding"
            self.message = "æ­£åœ¨å–æ¶ˆ Tracker è§’è‰²ç»‘å®šâ€¦"
        threading.Thread(
            target=self._cancel_binding_worker,
            name="tracker-binding-cancel",
            daemon=True,
        ).start()

    def _cancel_binding_worker(self) -> None:
        deadline = time.monotonic() + 1
        while not self.vive.is_active and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.vive.is_active:
            self.vive.force_stop()
        self._set_phase("idle", "å·²å–æ¶ˆ Tracker è§’è‰²ç»‘å®š")

    # Capture session lifecycle --------------------------------------------
    def start_capture(self, tracker_rate: float = 120.0) -> None:
        if tracker_rate <= 0 or tracker_rate > 1000:
            raise ValueError("Tracker é‡‡æ ·çŽ‡å¿…é¡»å¤§äºŽ 0 ä¸”ä¸è¶…è¿‡ 1000 Hz")
        self._require_checks({"Windows å¹³å°", "uv", "VIVE é‡‡é›†å…¥å£", "Tracker è§’è‰²æ˜ å°„"})
        with self._lock:
            if (
                self.phase != "ble_ready"
                or not self.master.is_active
                or not self._master_ready.is_set()
                or not self.ble.is_active
                or not self._ble_ready.is_set()
            ):
                raise RuntimeError("è¯·å…ˆå¯åŠ¨ BLE æŽˆæ—¶å¹¶ç­‰å¾…å…¶å°±ç»ª")
            if self.vive.is_active:
                raise RuntimeError("VIVE Tracker è¿›ç¨‹å·²ç»åœ¨è¿è¡Œ")
            self._capture_stop_requested.clear()
            self._vive_ready.clear()
            self.error = None
            self.capture_started_at = None
            self.vive_output_dir = None
            self.phase = "starting_tracker"
            self.message = "æ­£åœ¨åˆå§‹åŒ– VIVE Trackerï¼›BLE ç»§ç»­æŽˆæ—¶â€¦"
        self.vive.logs.append("[UI] ===== New Tracker capture =====", "system")
        threading.Thread(
            target=self._start_capture_worker,
            args=(tracker_rate,),
            name="capture-start-sequence",
            daemon=True,
        ).start()

    def _start_capture_worker(self, tracker_rate: float) -> None:
        try:
            uv_path = shutil.which("uv")
            if uv_path is None:
                raise RuntimeError("uv disappeared after preflight")
            vive_script = VIVE_DIR / "collect_openxr_tracker_poses_and_triggers.py"
            self.vive.start(
                [
                    uv_path,
                    "run",
                    "--script",
                    str(vive_script),
                    "--role-map",
                    str(VIVE_DIR / "tracker_roles.json"),
                    "--output-root",
                    str(VIVE_DIR / "vr_captures"),
                    "--tracker-rate",
                    f"{tracker_rate:g}",
                    "--control-stdin",
                ],
                VIVE_DIR,
            )
            if not self._wait_until_ready(
                self._vive_ready, self.vive, self._capture_stop_requested
            ):
                return
            self._set_connected_gateway_state("WAIT_START_ACK")
            master_start_seq = self.master.logs.last_sequence
            if not self.master.is_active or not self.master.send_line("1"):
                self._set_connected_gateway_state("ERROR")
                raise RuntimeError("æ— æ³•å‘ Master ä¸²å£æŽ§åˆ¶å™¨å‘é€åŽŸé”®ç›˜äº‹ä»¶ 1")
            if not self.master.logs.wait_for("[START_OK]", master_start_seq, 12):
                self._set_connected_gateway_state("ERROR")
                raise RuntimeError("ç­‰å¾… Master START ç¡®è®¤è¶…æ—¶")
            self._set_connected_gateway_state("RUNNING")
            self.capture_started_at = _now()
            self._set_phase("recording", "é‡‡é›†ä¸­ï¼šæŽ§åˆ¶ä¸»æœºå·²å¯åŠ¨é‡‡é›†ï¼ŒTracker æ­£åœ¨è®°å½•")
        except Exception as exc:
            if self._capture_stop_requested.is_set():
                return
            if self.vive.is_active:
                self.vive.send_line("stop")
                if not self.vive.wait(10):
                    self.vive.force_stop()
            if (
                self.master.is_active
                and self._master_ready.is_set()
                and self.ble.is_active
                and self._ble_ready.is_set()
            ):
                self._set_phase(
                    "ble_ready",
                    f"Tracker å¯åŠ¨å¤±è´¥ï¼š{exc}ï¼›BLE ä»åœ¨æŒç»­æŽˆæ—¶",
                    str(exc),
                )
            else:
                self._set_phase("error", f"é‡‡é›†å¯åŠ¨å¤±è´¥ï¼š{exc}", str(exc))

    def stop_capture(self) -> None:
        with self._lock:
            if self.phase not in {"starting_tracker", "recording"}:
                raise RuntimeError("å½“å‰æ²¡æœ‰æ­£åœ¨å¯åŠ¨æˆ–è¿è¡Œçš„é‡‡é›†")
            was_recording = self.phase == "recording"
            self._capture_stop_requested.set()
            self.phase = "stopping_capture"
            self.message = "æ­£åœ¨å‘é€åœæ­¢æŒ‡ä»¤ï¼Œå¹¶åœæ­¢ä¿å­˜ Tracker æ•°æ®â€¦"
            self.error = None
        threading.Thread(
            target=self._stop_capture_worker,
            args=(was_recording,),
            name="capture-stop-sequence",
            daemon=True,
        ).start()

    def _stop_capture_worker(self, was_recording: bool) -> None:
        master_stop_seq = self.master.logs.last_sequence
        if self.vive.is_active:
            self.vive.send_line("stop")
        if self.master.is_active and self._master_ready.is_set():
            self._set_connected_gateway_state("WAIT_STOP_ACK")
            self.master.send_line("0")
        if self.vive.is_active and not self.vive.wait(20):
            self.vive.force_stop()
        if was_recording and self.master.is_active:
            if self.master.logs.wait_for("[STOP_OK]", master_stop_seq, 12):
                self._set_connected_gateway_state("IDLE")
            else:
                self._set_connected_gateway_state("ERROR")
        self.last_stopped_at = _now()
        if (
            self.master.is_active
            and self._master_ready.is_set()
            and self.ble.is_active
            and self._ble_ready.is_set()
        ):
            self._set_phase("ble_ready", "æœ¬è½®é‡‡é›†å·²åœæ­¢å¹¶ä¿å­˜ï¼›BLE ç»§ç»­æŒç»­æŽˆæ—¶")
        else:
            self._set_phase("error", "é‡‡é›†å·²åœæ­¢ï¼Œä½†é€šä¿¡è¿›ç¨‹å·²é€€å‡º", "Communication exited")

    def _wait_until_ready(
        self,
        ready_event: threading.Event,
        process: ProjectProcess,
        cancel_event: threading.Event,
    ) -> bool:
        while not ready_event.wait(0.2):
            if cancel_event.is_set():
                return False
            if not process.is_active:
                raise RuntimeError(f"{process.title} åœ¨å°±ç»ªå‰é€€å‡º")
        return not cancel_event.is_set()

    # Process callbacks and state ------------------------------------------
    def _on_line(self, key: str, line: str) -> None:
        if key == "master":
            self.ble.logs.append(f"[MASTER] {line}")
            self._update_master_states_from_line(line)
            if "[READY] Master serial connected" in line:
                self._master_ready.set()
            if "[NOT_READY]" in line:
                self._master_ready.clear()
            if "[AUTO_STOP]" in line:
                self._set_connected_gateway_state("IDLE")
                with self._lock:
                    recording = self.phase in {"starting_tracker", "recording"}
                if recording:
                    self._capture_stop_requested.set()
                    self._set_phase(
                        "stopping_capture",
                        "æŽ§åˆ¶ä¸»æœºæ£€æµ‹åˆ° Slave æ–­çº¿ï¼Œæ­£åœ¨åœæ­¢å¹¶ä¿å­˜ Tracker æ•°æ®",
                        "Master auto stopped after Slave disconnect",
                    )
                    threading.Thread(
                        target=self._stop_vive_after_comm_failure,
                        name="vive-stop-after-master-auto-stop",
                        daemon=True,
                    ).start()
        elif key == "ble":
            self._update_gateway_states_from_line(line)
            if "[READY]" in line:
                self._ble_ready.set()
        elif key == "vive" and "Recording poses to:" in line:
            self.vive_output_dir = line.split("Recording poses to:", 1)[1].strip()
            self._vive_ready.set()

    def _update_master_states_from_line(self, line: str) -> None:
        status_match = re.search(r"STATUS\+[^\r\n]*\+END", line)
        if status_match:
            status = status_match.group(0)
            for device_id in ("68", "69", "70"):
                state_match = re.search(
                    rf"(?:^|\+)S{device_id}=(CONNECTED|DISCONNECTED)(?:\+|$)",
                    status,
                )
                if state_match:
                    if state_match.group(1) == "CONNECTED":
                        self._set_gateway_state(device_id, "IDLE")
                    else:
                        self._set_gateway_state(device_id, "ERROR")

        event_match = re.search(
            r"EVENT\+SLAVE\+DEVICE=(68|69|70)\+(FOUND|CONNECTED|DISCONNECTED)(?:\+[^\r\n]*?)?\+END",
            line,
        )
        if event_match:
            device_id, event = event_match.groups()
            if event == "DISCONNECTED":
                self._set_gateway_state(device_id, "ERROR")
            elif event == "CONNECTED":
                with self._lock:
                    self._gateway_states.setdefault(
                        device_id,
                        {"state": "IDLE", "updated_at": _now()},
                    )

    def _update_gateway_states_from_line(self, line: str) -> None:
        state_match = GATEWAY_STATE_PATTERN.search(line)
        if state_match:
            self._set_gateway_state(
                state_match.group("device_id"), state_match.group("state")
            )

        ready_match = re.search(r"\[READY\]\s+Online gateways:\s*(.*)", line)
        if ready_match:
            online = set(re.findall(r"\b\d+\b", ready_match.group(1)))
            with self._lock:
                for device_id in list(self._gateway_states):
                    if device_id not in online:
                        self._gateway_states.pop(device_id, None)
                for device_id in online:
                    self._gateway_states.setdefault(
                        device_id,
                        {"state": "IDLE", "updated_at": _now()},
                    )

        offline_match = re.search(r"\[OFFLINE\]\s+Gateways:\s*(.*)", line)
        if offline_match and offline_match.group(1).strip().casefold() != "none":
            with self._lock:
                for device_id in re.findall(r"\b\d+\b", offline_match.group(1)):
                    self._gateway_states.pop(device_id, None)

        online_match = re.search(r"\[ONLINE\]\s+(?:Gateway|Slave)\s+(\d+)", line)
        if online_match:
            with self._lock:
                self._gateway_states.setdefault(
                    online_match.group(1),
                    {"state": "IDLE", "updated_at": _now()},
                )

        reconnect_match = re.search(r"\[(\d+)\]\s+reconnecting\b", line, re.IGNORECASE)
        if reconnect_match:
            with self._lock:
                self._gateway_states.pop(reconnect_match.group(1), None)

        ack_match = re.search(r"\[(\d+)\]\s+ACK\s+(START|STOP)\s+OK", line)
        if ack_match:
            self._set_gateway_state(
                ack_match.group(1),
                "RUNNING" if ack_match.group(2) == "START" else "IDLE",
            )

        failed_match = re.search(r"\[(\d+)\]\s+FAILED:", line)
        if failed_match:
            self._set_gateway_state(failed_match.group(1), "ERROR")

        gateway_error_match = re.search(r"\[(\d+)\]\s+Gateway status:\s+GW\+ERROR", line)
        if gateway_error_match:
            self._set_gateway_state(gateway_error_match.group(1), "ERROR")

        if "Overall state: ERROR" in line:
            with self._lock:
                for item in self._gateway_states.values():
                    if item["state"] in {"WAIT_START_ACK", "WAIT_STOP_ACK"}:
                        item["state"] = "ERROR"
                        item["updated_at"] = _now()

    def _set_gateway_state(self, device_id: str, state: str) -> None:
        with self._lock:
            self._gateway_states[device_id] = {
                "state": state,
                "updated_at": _now(),
            }

    def _set_connected_gateway_state(self, state: str) -> None:
        with self._lock:
            for item in self._gateway_states.values():
                item["state"] = state
                item["updated_at"] = _now()

    def _on_exit(self, key: str, exit_code: int) -> None:
        if self._shutting_down:
            return
        with self._lock:
            phase = self.phase

        if key == "master":
            self._master_ready.clear()
            if phase == "stopping_ble":
                return
            if phase in {"starting_tracker", "recording", "stopping_capture"}:
                self._capture_stop_requested.set()
                self._set_phase(
                    "error",
                    f"æŽ§åˆ¶ä¸»æœºæ„å¤–é€€å‡ºï¼ˆä»£ç  {exit_code}ï¼‰ï¼Œæ­£åœ¨åœæ­¢ Tracker ä»¥ä¿å­˜æ•°æ®",
                    f"Master exited with code {exit_code}",
                )
                threading.Thread(
                    target=self._stop_vive_after_comm_failure,
                    name="vive-stop-after-master-failure",
                    daemon=True,
                ).start()
            elif phase != "idle":
                self._set_phase(
                    "error",
                    f"æŽ§åˆ¶ä¸»æœºè¿›ç¨‹æ„å¤–é€€å‡ºï¼ˆä»£ç  {exit_code}ï¼‰",
                    f"Master exited with code {exit_code}",
                )
                if self.ble.is_active:
                    threading.Thread(
                        target=self._stop_ble_after_master_failure,
                        name="ble-stop-after-master-failure",
                        daemon=True,
                    ).start()
            return

        if key == "ble":
            self._ble_ready.clear()
            with self._lock:
                self._gateway_states.clear()
            if phase == "stopping_ble":
                return
            if phase in {"starting_tracker", "recording", "stopping_capture"}:
                self._capture_stop_requested.set()
                self._set_phase(
                    "error",
                    f"BLE æ„å¤–é€€å‡ºï¼ˆä»£ç  {exit_code}ï¼‰ï¼Œæ­£åœ¨åœæ­¢ Tracker ä»¥ä¿å­˜æ•°æ®",
                    f"BLE exited with code {exit_code}",
                )
                threading.Thread(
                    target=self._stop_vive_after_comm_failure,
                    name="vive-stop-after-ble-failure",
                    daemon=True,
                ).start()
            elif phase != "idle":
                self._set_phase(
                    "error",
                    f"BLE æŽˆæ—¶è¿›ç¨‹æ„å¤–é€€å‡ºï¼ˆä»£ç  {exit_code}ï¼‰",
                    f"BLE exited with code {exit_code}",
                )
                if self.master.is_active:
                    threading.Thread(
                        target=self._stop_master_after_ble_failure,
                        name="master-stop-after-ble-failure",
                        daemon=True,
                    ).start()
            return

        if phase == "binding_trackers":
            if exit_code == 0:
                self._set_phase("idle", "Tracker è§’è‰²ç»‘å®šå®Œæˆï¼Œè§’è‰²æ˜ å°„å·²æ›´æ–°")
            else:
                self._set_phase(
                    "error",
                    f"Tracker è§’è‰²ç»‘å®šå¤±è´¥ï¼ˆé€€å‡ºä»£ç  {exit_code}ï¼‰",
                    f"Role binding exited with code {exit_code}",
                )
        elif phase in {"stopping_binding", "stopping_capture"}:
            return
        elif phase == "starting_tracker":
            if (
                self.master.is_active
                and self._master_ready.is_set()
                and self.ble.is_active
                and self._ble_ready.is_set()
            ):
                self._set_phase(
                    "ble_ready",
                    f"Tracker åœ¨å¼€å§‹é‡‡é›†å‰é€€å‡ºï¼ˆä»£ç  {exit_code}ï¼‰ï¼›BLE ç»§ç»­æŽˆæ—¶",
                    f"VIVE exited with code {exit_code}",
                )
            else:
                self._set_phase("error", "Tracker ä¸Žé€šä¿¡è¿›ç¨‹å‡æœªæ­£å¸¸è¿è¡Œ")
        elif phase == "recording":
            self._capture_stop_requested.set()
            self._set_phase(
                "stopping_capture",
                f"Tracker æ„å¤–é€€å‡ºï¼ˆä»£ç  {exit_code}ï¼‰ï¼Œæ­£åœ¨å‘é€åœæ­¢æŒ‡ä»¤",
                f"VIVE exited with code {exit_code}",
            )
            threading.Thread(
                target=self._finish_after_unexpected_vive_exit,
                name="master-stop-after-vive-failure",
                daemon=True,
            ).start()

    def _stop_vive_after_comm_failure(self) -> None:
        if self.master.is_active and self._master_ready.is_set():
            self.master.send_line("0")
        if self.vive.is_active:
            self.vive.send_line("stop")
            if not self.vive.wait(15):
                self.vive.force_stop()

    def _stop_ble_after_master_failure(self) -> None:
        if self.ble.is_active:
            self.ble.send_line("quit")
            if not self.ble.wait(10):
                self.ble.force_stop()

    def _stop_master_after_ble_failure(self) -> None:
        if self.master.is_active:
            self.master.send_line("quit")
            if not self.master.wait(10):
                self.master.force_stop()

    def _finish_after_unexpected_vive_exit(self) -> None:
        if self.master.is_active and self._master_ready.is_set():
            master_stop_seq = self.master.logs.last_sequence
            self._set_connected_gateway_state("WAIT_STOP_ACK")
            self.master.send_line("0")
            if self.master.logs.wait_for("[STOP_OK]", master_stop_seq, 12):
                self._set_connected_gateway_state("IDLE")
            else:
                self._set_connected_gateway_state("ERROR")
        if (
            self.master.is_active
            and self._master_ready.is_set()
            and self.ble.is_active
            and self._ble_ready.is_set()
        ):
            self._set_phase(
                "ble_ready",
                "Tracker å¼‚å¸¸é€€å‡ºï¼›å·²å‘é€åœæ­¢æŒ‡ä»¤ï¼ŒBLE ç»§ç»­æŽˆæ—¶",
                "VIVE exited unexpectedly",
            )
        else:
            self._set_phase("error", "Tracker ä¸Žé€šä¿¡è¿›ç¨‹å‡æœªæ­£å¸¸è¿è¡Œ")

    def clear_logs(self) -> None:
        self.master.logs.clear()
        self.ble.logs.clear()
        self.vive.logs.clear()

    def state(self, after_ble: int = 0, after_vive: int = 0) -> dict:
        ble_lines, ble_sequence = self.ble.logs.since(after_ble)
        vive_lines, vive_sequence = self.vive.logs.since(after_vive)
        with self._lock:
            master_active = self.master.is_active
            ble_active = self.ble.is_active
            vive_active = self.vive.is_active
            communication_ready = (
                master_active
                and self._master_ready.is_set()
                and ble_active
                and self._ble_ready.is_set()
            )
            controls = {
                "can_start_ble": self.phase in {"idle", "error"} and not master_active and not ble_active and not vive_active,
                "can_stop_ble": (master_active or ble_active) and not vive_active and self.phase in {"starting_ble", "ble_ready", "error"},
                "can_start_capture": self.phase == "ble_ready" and communication_ready and not vive_active,
                "can_stop_capture": self.phase in {"starting_tracker", "recording"},
                "can_bind_trackers": self.phase in {"idle", "error"} and not master_active and not ble_active and not vive_active,
                "can_cancel_binding": self.phase == "binding_trackers",
            }
            return {
                "phase": self.phase,
                "message": self.message,
                "error": self.error,
                "ble_ready": communication_ready,
                "capture_started_at": self.capture_started_at,
                "last_stopped_at": self.last_stopped_at,
                "vive_output_dir": self.vive_output_dir,
                "gateways": [
                    {
                        "device_id": device_id,
                        "state": item["state"],
                        "updated_at": item["updated_at"],
                    }
                    for device_id, item in sorted(
                        self._gateway_states.items(),
                        key=lambda pair: int(pair[0]),
                    )
                ],
                "controls": controls,
                "processes": {
                    "ble": self.ble.snapshot(),
                    "vive": self.vive.snapshot(),
                },
                "logs": {
                    "ble": {"items": ble_lines, "last_seq": ble_sequence},
                    "vive": {"items": vive_lines, "last_seq": vive_sequence},
                },
            }

    def shutdown(self) -> None:
        self._shutting_down = True
        self._ble_stop_requested.set()
        self._capture_stop_requested.set()
        self._binding_cancel_requested.set()
        if self.master.is_active and self.phase in {"recording", "stopping_capture"}:
            self.master.send_line("0")
            time.sleep(0.5)
        if self.vive.is_active:
            self.vive.send_line("stop")
            if not self.vive.wait(15):
                self.vive.force_stop()
        if self.ble.is_active:
            self.ble.send_line("quit")
            if not self.ble.wait(10):
                self.ble.force_stop()
        if self.master.is_active:
            self.master.send_line("quit")
            if not self.master.wait(10):
                self.master.force_stop()
