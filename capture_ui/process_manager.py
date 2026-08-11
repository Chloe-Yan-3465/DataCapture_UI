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
VIVE_DIR = ROOT_DIR / "VIVE-Tracker_capture"

MODE2_NODE_PATTERN = re.compile(
    r"node=(?P<node_id>\d+)\s+connected=(?P<connected>[01])\s+"
    r"state=(?P<state>[A-Z0-9_]+)(?P<details>.*)",
    re.IGNORECASE,
)
MODE2_READY_PATTERN = re.compile(
    r"\[READY\]\s+(?P<name>.+?)\s+via\s+(?P<serial>\S+)", re.IGNORECASE
)
MODE2_STOP_FRAME_PATTERN = re.compile(
    r"\bSTOP_FRAME\s+session=(?P<session>\d+)\s+"
    r"node=(?P<node_id>\d+)\s+frames=(?P<frames>-?\d+)\b",
    re.IGNORECASE,
)

BLE_CONTROL_TOKENS = (
    "[CONTROL]",
    "[SCAN]",
    "[START]",
    "[START FAILED]",
    "[STOP]",
    "START REJECTED",
    "START ABORTED",
    "START IGNORED",
    "START DID NOT RECEIVE",
    "STOP DID NOT RECEIVE",
    "START SCHEDULED",
    "STOP SCHEDULED",
    "STOP_FRAME",
    " ARMED ",
    "NO ACTIVE SESSION",
    "SCAN ",
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
        self._ble_ready = threading.Event()
        self._vive_ready = threading.Event()
        self._ble_start_result = threading.Event()
        self._ble_stop_result = threading.Event()
        self._ble_stop_requested = threading.Event()
        self._capture_stop_requested = threading.Event()
        self._binding_cancel_requested = threading.Event()
        self._shutting_down = False
        self.phase = "idle"
        self.message = "请先启动 BLE 授时"
        self.error: str | None = None
        self.capture_started_at: str | None = None
        self.last_stopped_at: str | None = None
        self.vive_output_dir: str | None = None
        self.ble_timesync_logs = LogStore()
        self.ble_control_logs = LogStore()
        self._ble_start_ok: bool | None = None
        self._ble_stop_ok: bool | None = None
        self._ble_control_state = "OFFLINE"
        self._ble_control_result = "OFFLINE"
        self._ble_time_sync_state = "STOPPED"
        self._coordinator_name = "Mode2Coordinator"
        self._coordinator_serial = "未连接"
        self._utc_map_state = "UNKNOWN"
        self._node_states: dict[str, dict[str, object]] = {}
        self._frame_report_status = "idle"
        self._frame_report_session: str | None = None
        self._frame_report_expected_nodes: set[str] = set()
        self._frame_report_nodes: dict[str, dict[str, object]] = {}
        self.ble = ProjectProcess("ble", "BLE-TimeSync", self._on_line, self._on_exit)
        self.vive = ProjectProcess(
            "vive", "VIVE Tracker", self._on_line, self._on_exit
        )

    def preflight(self) -> dict:
        uv_path = shutil.which("uv")
        checks = [
            {
                "name": "Windows 平台",
                "ok": sys.platform == "win32",
                "detail": sys.platform,
            },
            {
                "name": "授时 Python 运行环境",
                "ok": (BLE_DIR / ".venv" / "Scripts" / "python.exe").is_file(),
                "detail": str(BLE_DIR / ".venv" / "Scripts" / "python.exe"),
            },
            {
                "name": "Mode2 授时入口",
                "ok": (BLE_DIR / "app" / "main.py").is_file(),
                "detail": str(BLE_DIR / "app" / "main.py"),
            },
            {
                "name": "uv",
                "ok": uv_path is not None,
                "detail": uv_path or "未找到",
            },
            {
                "name": "VIVE 采集入口",
                "ok": (
                    VIVE_DIR / "collect_openxr_tracker_poses_and_triggers.py"
                ).is_file(),
                "detail": str(
                    VIVE_DIR / "collect_openxr_tracker_poses_and_triggers.py"
                ),
            },
            {
                "name": "Tracker 角色映射",
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

    def _start_frame_report(self) -> None:
        with self._lock:
            self._frame_report_status = "recording"
            self._frame_report_session = None
            self._frame_report_expected_nodes = {
                node_id
                for node_id, item in self._node_states.items()
                if item["connected"]
            }
            self._frame_report_nodes.clear()

    def _wait_for_frame_report(self) -> None:
        with self._lock:
            self._frame_report_status = "waiting"
            self._frame_report_session = None
            self._frame_report_expected_nodes = {
                node_id
                for node_id, item in self._node_states.items()
                if item["connected"]
            }
            self._frame_report_nodes.clear()

    def _require_checks(self, names: set[str]) -> None:
        checks = self.preflight()["checks"]
        failed = [item["name"] for item in checks if item["name"] in names and not item["ok"]]
        if failed:
            raise RuntimeError("启动检查未通过：" + "、".join(failed))

    # BLE service lifecycle -------------------------------------------------
    def start_ble(self) -> None:
        self._require_checks({"Windows 平台", "授时 Python 运行环境", "Mode2 授时入口"})
        with self._lock:
            if self.phase not in {"idle", "error"}:
                raise RuntimeError("当前状态不能启动 BLE 授时")
            if self.ble.is_active or self.vive.is_active:
                raise RuntimeError("仍有项目进程没有退出")
            self._ble_ready.clear()
            self._ble_stop_requested.clear()
            self._ble_start_result.clear()
            self._ble_stop_result.clear()
            self._ble_start_ok = None
            self._ble_stop_ok = None
            self._ble_control_state = "CONNECTING"
            self._ble_control_result = "STANDBY"
            self._ble_time_sync_state = "SYNCING"
            self._coordinator_serial = "连接中"
            self._utc_map_state = "UNKNOWN"
            self._node_states.clear()
            self.error = None
            self.phase = "starting_ble"
            self.message = "正在连接 Mode2 中控并等待首次授时成功…"
        self.ble_timesync_logs.append(
            "[UI] ===== Start persistent Mode2 time sync =====", "system"
        )
        self.ble.logs.append("[UI] ===== Start BLE time sync service =====", "system")
        threading.Thread(
            target=self._start_ble_worker,
            name="ble-start-sequence",
            daemon=True,
        ).start()

    def _start_ble_worker(self) -> None:
        try:
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
            if not self._wait_until_ready(
                self._ble_ready, self.ble, self._ble_stop_requested
            ):
                return
            self._set_phase("ble_ready", "Mode2 已就绪并持续授时，可以开始录制")
        except Exception as exc:
            if not self._ble_stop_requested.is_set():
                self._set_phase("error", f"BLE 启动失败：{exc}", str(exc))

    def scan_wearables(self) -> None:
        with self._lock:
            if (
                self.phase != "ble_ready"
                or not self.ble.is_active
                or not self._ble_ready.is_set()
            ):
                raise RuntimeError("请先启动 BLE 授时并等待中控串口就绪")
        if not self.ble.send_line("s"):
            raise RuntimeError("无法向 BLE 授时进程发送扫描指令")
        self.ble_control_logs.append(
            "[UI] Requested wearable scan (keyboard s)", "system"
        )

    def stop_ble(self) -> None:
        with self._lock:
            if self.phase in {"starting_tracker", "recording", "stopping_capture"}:
                raise RuntimeError("请先停止并保存当前采集，再停止 BLE 授时")
            if self.phase in {"binding_trackers", "stopping_binding"}:
                raise RuntimeError("Tracker 角色绑定期间不能停止 BLE")
            if self.phase == "stopping_ble":
                return
            if not self.ble.is_active:
                self._ble_ready.clear()
                self._ble_control_state = "OFFLINE"
                self._ble_control_result = "OFFLINE"
                self._ble_time_sync_state = "STOPPED"
                self.phase = "idle"
                self.message = "Mode2 授时未运行"
                return
            self._ble_stop_requested.set()
            self.phase = "stopping_ble"
            self.message = "正在停止 Mode2 常驻授时进程…"
            self.error = None
        threading.Thread(
            target=self._stop_ble_worker,
            name="ble-stop-sequence",
            daemon=True,
        ).start()

    def _stop_ble_worker(self) -> None:
        try:
            self.ble.send_line("quit")
            if not self.ble.wait(10):
                self.ble.force_stop()
        finally:
            if self.ble.is_active:
                self.ble.force_stop()
            self._ble_ready.clear()
            with self._lock:
                self._node_states.clear()
                self._ble_control_state = "OFFLINE"
                self._ble_control_result = "OFFLINE"
                self._ble_time_sync_state = "STOPPED"
                self._coordinator_serial = "未连接"
                self._utc_map_state = "UNKNOWN"
            self._set_phase("idle", "Mode2 常驻授时已停止")

    # Tracker role binding -------------------------------------------------
    def bind_tracker_roles(self) -> None:
        bind_script = VIVE_DIR / "01_绑定Tracker角色.ps1"
        self._require_checks({"Windows 平台", "uv"})
        if not bind_script.is_file():
            raise RuntimeError(f"角色绑定脚本不存在：{bind_script}")
        with self._lock:
            if self.phase not in {"idle", "error"}:
                raise RuntimeError("请在 BLE 和采集均未运行时绑定 Tracker 角色")
            if self.ble.is_active or self.vive.is_active:
                raise RuntimeError("仍有项目进程没有退出")
            self._binding_cancel_requested.clear()
            self.phase = "binding_trackers"
            self.message = "正在从 SteamVR 读取 Tracker 角色…"
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
                    str(VIVE_DIR / "01_绑定Tracker角色.ps1"),
                    "-NonInteractive",
                ],
                VIVE_DIR,
            )
        except Exception as exc:
            if not self._binding_cancel_requested.is_set():
                self._set_phase("error", f"Tracker 角色绑定失败：{exc}", str(exc))

    def cancel_tracker_binding(self) -> None:
        with self._lock:
            if self.phase != "binding_trackers":
                return
            self._binding_cancel_requested.set()
            self.phase = "stopping_binding"
            self.message = "正在取消 Tracker 角色绑定…"
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
        self._set_phase("idle", "已取消 Tracker 角色绑定")

    # Capture session lifecycle --------------------------------------------
    def start_capture(self, tracker_rate: float = 120.0) -> None:
        if tracker_rate <= 0 or tracker_rate > 1000:
            raise ValueError("Tracker 采样率必须大于 0 且不超过 1000 Hz")
        self._require_checks({"Windows 平台", "uv", "VIVE 采集入口", "Tracker 角色映射"})
        with self._lock:
            if self.phase != "ble_ready" or not self.ble.is_active or not self._ble_ready.is_set():
                raise RuntimeError("请先启动 BLE 授时并等待其就绪")
            if self.vive.is_active:
                raise RuntimeError("VIVE Tracker 进程已经在运行")
            self._capture_stop_requested.clear()
            self._vive_ready.clear()
            self._ble_start_result.clear()
            self._ble_start_ok = None
            self.error = None
            self.capture_started_at = None
            self.vive_output_dir = None
            self.phase = "starting_tracker"
            self.message = "正在初始化 VIVE Tracker；Mode2 继续授时…"
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
            with self._lock:
                self._ble_control_state = "STARTING"
                self._ble_control_result = "STARTING"
            self.ble_control_logs.append(
                "[UI -> Mode2] 1 / START（等待全部 wearable ARMED）", "system"
            )
            if not self.ble.is_active or not self.ble.send_line("1"):
                with self._lock:
                    self._ble_control_state = "ERROR"
                    self._ble_control_result = "ERROR"
                raise RuntimeError("无法向 Mode2 控制器发送键盘事件 1")
            if not self._ble_start_result.wait(45):
                with self._lock:
                    self._ble_control_state = "ERROR"
                    self._ble_control_result = "ERROR"
                raise RuntimeError("等待 Mode2 START 结果超时")
            if not self._ble_start_ok:
                raise RuntimeError("Mode2 START 未成功，详情见录制控制日志")
            if self._capture_stop_requested.is_set():
                return
            self.capture_started_at = _now()
            self._set_phase(
                "recording", "录制中：Mode2 START 成功，Tracker 正在记录"
            )
        except Exception as exc:
            if self._capture_stop_requested.is_set():
                return
            if self.vive.is_active:
                self.vive.send_line("stop")
                if not self.vive.wait(10):
                    self.vive.force_stop()
            if self.ble.is_active and self._ble_ready.is_set():
                self._set_phase(
                    "ble_ready",
                    f"录制启动失败：{exc}；Mode2 仍在持续授时",
                    str(exc),
                )
            else:
                self._set_phase("error", f"采集启动失败：{exc}", str(exc))

    def stop_capture(self) -> None:
        with self._lock:
            if self.phase not in {"starting_tracker", "recording"}:
                raise RuntimeError("当前没有正在启动或运行的采集")
            self._capture_stop_requested.set()
            self.phase = "stopping_capture"
            self.message = "正在发送 Mode2 0 / STOP，并停止保存 Tracker 数据…"
            self.error = None
        self._wait_for_frame_report()
        threading.Thread(
            target=self._stop_capture_worker,
            name="capture-stop-sequence",
            daemon=True,
        ).start()

    def _stop_capture_worker(self) -> None:
        self._ble_stop_result.clear()
        self._ble_stop_ok = None
        stop_sent = False
        if self.vive.is_active:
            self.vive.send_line("stop")
        if self.ble.is_active and self._ble_ready.is_set():
            with self._lock:
                self._ble_control_state = "STOPPING"
                self._ble_control_result = "STOPPING"
            self.ble_control_logs.append(
                "[UI -> Mode2] 0 / STOP（等待中控确认）", "system"
            )
            stop_sent = self.ble.send_line("0")
        if self.vive.is_active and not self.vive.wait(20):
            self.vive.force_stop()
        stop_confirmed = True
        if stop_sent and self.ble.is_active and self._ble_ready.is_set():
            stop_confirmed = self._ble_stop_result.wait(55) and bool(self._ble_stop_ok)
        self.last_stopped_at = _now()
        if self.ble.is_active and self._ble_ready.is_set():
            if stop_confirmed:
                self._set_phase(
                    "ble_ready", "本轮录制已停止并保存；Mode2 继续持续授时"
                )
            else:
                with self._lock:
                    self._ble_control_state = "ERROR"
                    self._ble_control_result = "ERROR"
                self._set_phase(
                    "ble_ready",
                    "Tracker 已保存，但未确认 Mode2 STOP 成功；请检查录制控制日志",
                    "Mode2 STOP was not confirmed",
                )
        else:
            self._set_phase(
                "error", "录制已停止，但 Mode2 授时进程已退出", "BLE exited"
            )

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
                raise RuntimeError(f"{process.title} 在就绪前退出")
        return not cancel_event.is_set()

    # Process callbacks and state ------------------------------------------
    def _on_line(self, key: str, line: str) -> None:
        if key == "ble":
            self._record_ble_line(line)
            self._update_mode2_state_from_line(line)
        elif key == "vive" and "Recording poses to:" in line:
            self.vive_output_dir = line.split("Recording poses to:", 1)[1].strip()
            self._vive_ready.set()

    def _record_ble_line(self, line: str) -> None:
        upper = line.upper()
        if MODE2_NODE_PATTERN.search(line) or any(
            token in upper for token in BLE_CONTROL_TOKENS
        ):
            self.ble_control_logs.append(line)
        else:
            self.ble_timesync_logs.append(line)

    def _update_mode2_state_from_line(self, line: str) -> None:
        upper = line.upper()
        now = _now()

        ready_match = MODE2_READY_PATTERN.search(line)
        if ready_match:
            with self._lock:
                self._coordinator_name = ready_match.group("name").strip()
                self._coordinator_serial = ready_match.group("serial").strip()
                self._ble_control_state = "IDLE"
                self._ble_control_result = "STANDBY"
                self._ble_time_sync_state = "SYNCING"
            self._ble_ready.set()

        node_match = MODE2_NODE_PATTERN.search(line)
        if node_match:
            node_id = node_match.group("node_id")
            with self._lock:
                self._node_states[node_id] = {
                    "connected": node_match.group("connected") == "1",
                    "state": node_match.group("state").upper(),
                    "details": node_match.group("details").strip(),
                    "updated_at": now,
                }

        utc_match = re.search(r"\butc_map=(LOCKED|UNLOCKED)\b", line, re.IGNORECASE)
        if utc_match:
            with self._lock:
                self._utc_map_state = utc_match.group(1).upper()

        if "TIME SYNC ACCEPTED:" in upper:
            with self._lock:
                self._ble_time_sync_state = "SYNCED"
        elif (
            "INITIAL TIME SYNC WAS NOT ACCEPTED" in upper
            or "PERIODIC COORDINATOR TIME SYNC FAILED" in upper
            or "TIME_ERROR" in upper
        ):
            with self._lock:
                self._ble_time_sync_state = "RETRYING"

        if "[START]" in upper and "WEARABLE" in upper and "ARMED" in upper:
            with self._lock:
                self._ble_control_state = "RUNNING"
                self._ble_control_result = "START_OK"
                self._ble_start_ok = True
            self._start_frame_report()
            self._ble_start_result.set()
        elif any(
            marker in upper
            for marker in (
                "[START FAILED]",
                "START REJECTED BECAUSE",
                "START DID NOT RECEIVE",
                "START IGNORED",
            )
        ):
            with self._lock:
                self._ble_control_state = "ERROR"
                self._ble_control_result = "ERROR"
                self._ble_start_ok = False
            self._ble_start_result.set()

        if "[STOP]" in upper:
            with self._lock:
                self._ble_control_state = "IDLE"
                self._ble_control_result = "STOP_OK"
                self._ble_stop_ok = True
            self._ble_stop_result.set()
        elif "STOP DID NOT RECEIVE" in upper:
            with self._lock:
                self._ble_control_state = "ERROR"
                self._ble_control_result = "ERROR"
                self._ble_stop_ok = False
            self._ble_stop_result.set()

        frame_match = MODE2_STOP_FRAME_PATTERN.search(line)
        if frame_match:
            session = frame_match.group("session")
            node_id = frame_match.group("node_id")
            frames = int(frame_match.group("frames"))
            with self._lock:
                if self._frame_report_session not in {None, session}:
                    self._frame_report_nodes.clear()
                self._frame_report_session = session
                self._frame_report_nodes[node_id] = {
                    "frames": frames,
                    "updated_at": now,
                }
                received_nodes = set(self._frame_report_nodes)
                expected_nodes = self._frame_report_expected_nodes
                self._frame_report_status = (
                    "complete"
                    if not expected_nodes or expected_nodes <= received_nodes
                    else "receiving"
                )

    def _on_exit(self, key: str, exit_code: int) -> None:
        if self._shutting_down:
            return
        with self._lock:
            phase = self.phase
        if key == "ble":
            self._ble_ready.clear()
            with self._lock:
                self._node_states.clear()
                self._ble_control_state = "OFFLINE"
                self._ble_control_result = "OFFLINE"
                self._ble_time_sync_state = "STOPPED"
                self._ble_start_ok = False
                self._ble_stop_ok = False
            self._ble_start_result.set()
            self._ble_stop_result.set()
            if phase == "stopping_ble":
                return
            if phase in {"starting_tracker", "recording", "stopping_capture"}:
                self._capture_stop_requested.set()
                self._set_phase(
                    "error",
                    f"Mode2 授时意外退出（代码 {exit_code}），正在停止 Tracker 以保存数据",
                    f"BLE exited with code {exit_code}",
                )
                threading.Thread(
                    target=self._stop_vive_after_ble_failure,
                    name="vive-stop-after-ble-failure",
                    daemon=True,
                ).start()
            elif phase != "idle":
                self._set_phase(
                    "error",
                    f"Mode2 授时进程意外退出（代码 {exit_code}）",
                    f"BLE exited with code {exit_code}",
                )
            return

        if phase == "binding_trackers":
            if exit_code == 0:
                self._set_phase("idle", "Tracker 角色绑定完成，角色映射已更新")
            else:
                self._set_phase(
                    "error",
                    f"Tracker 角色绑定失败（退出代码 {exit_code}）",
                    f"Role binding exited with code {exit_code}",
                )
        elif phase in {"stopping_binding", "stopping_capture"}:
            return
        elif phase == "starting_tracker":
            if self.ble.is_active and self._ble_ready.is_set():
                self._set_phase(
                    "ble_ready",
                    f"Tracker 在开始录制前退出（代码 {exit_code}）；Mode2 继续授时",
                    f"VIVE exited with code {exit_code}",
                )
            else:
                self._set_phase("error", "Tracker 与 Mode2 均未正常运行")
        elif phase == "recording":
            self._capture_stop_requested.set()
            self._set_phase(
                "stopping_capture",
                f"Tracker 意外退出（代码 {exit_code}），正在向 Mode2 发送 0 / STOP",
                f"VIVE exited with code {exit_code}",
            )
            threading.Thread(
                target=self._finish_after_unexpected_vive_exit,
                name="ble-stop-after-vive-failure",
                daemon=True,
            ).start()

    def _stop_vive_after_ble_failure(self) -> None:
        if self.vive.is_active:
            self.vive.send_line("stop")
            if not self.vive.wait(15):
                self.vive.force_stop()

    def _finish_after_unexpected_vive_exit(self) -> None:
        if self.ble.is_active and self._ble_ready.is_set():
            self._wait_for_frame_report()
            self._ble_stop_result.clear()
            self._ble_stop_ok = None
            with self._lock:
                self._ble_control_state = "STOPPING"
                self._ble_control_result = "STOPPING"
            self.ble_control_logs.append(
                "[UI -> Mode2] Tracker 异常退出，发送 0 / STOP", "system"
            )
            self.ble.send_line("0")
            confirmed = self._ble_stop_result.wait(20) and bool(self._ble_stop_ok)
            if confirmed:
                self._set_phase(
                    "ble_ready",
                    "Tracker 异常退出；Mode2 STOP 已确认，常驻授时继续",
                    "VIVE exited unexpectedly",
                )
            else:
                with self._lock:
                    self._ble_control_state = "ERROR"
                    self._ble_control_result = "ERROR"
                self._set_phase(
                    "ble_ready",
                    "Tracker 异常退出；未确认 Mode2 STOP，请检查录制控制日志",
                    "VIVE exited and Mode2 STOP was not confirmed",
                )
        else:
            self._set_phase("error", "Tracker 与 Mode2 均未正常运行")

    def clear_logs(self) -> None:
        self.ble.logs.clear()
        self.ble_timesync_logs.clear()
        self.ble_control_logs.clear()
        self.vive.logs.clear()

    def state(
        self,
        after_ble_timesync: int = 0,
        after_ble_control: int = 0,
        after_vive: int = 0,
    ) -> dict:
        timesync_lines, timesync_sequence = self.ble_timesync_logs.since(
            after_ble_timesync
        )
        control_lines, control_sequence = self.ble_control_logs.since(
            after_ble_control
        )
        vive_lines, vive_sequence = self.vive.logs.since(after_vive)
        with self._lock:
            ble_active = self.ble.is_active
            vive_active = self.vive.is_active
            controls = {
                "can_start_ble": self.phase in {"idle", "error"} and not ble_active and not vive_active,
                "can_stop_ble": ble_active and not vive_active and self.phase in {"starting_ble", "ble_ready", "error"},
                "can_start_capture": self.phase == "ble_ready" and ble_active and self._ble_ready.is_set() and not vive_active,
                "can_scan_wearables": self.phase == "ble_ready" and ble_active and self._ble_ready.is_set() and not vive_active,
                "can_stop_capture": self.phase in {"starting_tracker", "recording"},
                "can_bind_trackers": self.phase in {"idle", "error"} and not ble_active and not vive_active,
                "can_cancel_binding": self.phase == "binding_trackers",
            }
            return {
                "phase": self.phase,
                "message": self.message,
                "error": self.error,
                "ble_ready": self._ble_ready.is_set() and ble_active,
                "capture_started_at": self.capture_started_at,
                "last_stopped_at": self.last_stopped_at,
                "vive_output_dir": self.vive_output_dir,
                "mode2": {
                    "name": self._coordinator_name,
                    "serial": self._coordinator_serial,
                    "time_sync_state": self._ble_time_sync_state,
                    "control_state": self._ble_control_state,
                    "control_result": self._ble_control_result,
                    "utc_map_state": self._utc_map_state,
                    "nodes": [
                        {
                            "node_id": node_id,
                            "connected": item["connected"],
                            "state": item["state"],
                            "details": item["details"],
                            "updated_at": item["updated_at"],
                        }
                        for node_id, item in sorted(
                            self._node_states.items(), key=lambda pair: int(pair[0])
                        )
                    ],
                    "frame_report": {
                        "status": self._frame_report_status,
                        "session_id": self._frame_report_session,
                        "expected_nodes": sorted(
                            self._frame_report_expected_nodes, key=int
                        ),
                        "nodes": [
                            {
                                "node_id": node_id,
                                "frames": item["frames"],
                                "updated_at": item["updated_at"],
                            }
                            for node_id, item in sorted(
                                self._frame_report_nodes.items(),
                                key=lambda pair: int(pair[0]),
                            )
                        ],
                    },
                },
                "controls": controls,
                "processes": {
                    "ble": self.ble.snapshot(),
                    "vive": self.vive.snapshot(),
                },
                "logs": {
                    "ble_timesync": {
                        "items": timesync_lines,
                        "last_seq": timesync_sequence,
                    },
                    "ble_control": {
                        "items": control_lines,
                        "last_seq": control_sequence,
                    },
                    "vive": {"items": vive_lines, "last_seq": vive_sequence},
                },
            }

    def shutdown(self) -> None:
        self._shutting_down = True
        self._ble_stop_requested.set()
        self._capture_stop_requested.set()
        self._binding_cancel_requested.set()
        if self.vive.is_active:
            self.vive.send_line("stop")
            if not self.vive.wait(15):
                self.vive.force_stop()
        if self.ble.is_active:
            if self._ble_ready.is_set() and self.phase in {"recording", "stopping_capture"}:
                self.ble.send_line("0")
                time.sleep(0.5)
            self.ble.send_line("quit")
            if not self.ble.wait(10):
                self.ble.force_stop()
