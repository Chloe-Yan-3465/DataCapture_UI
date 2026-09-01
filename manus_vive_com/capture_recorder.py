"""Persistent MANUS RawSkeleton + OpenVR tracker stream and episode recorder.

Run this process first, then start either SDKMinimalClient_Windows_2_4_120hz.exe
or SDKMinimalClient_Windows_2_4_60hz.exe and select the local MANUS Core
connection. RawSkeleton callbacks are reduced to the latest sample in each
configured output interval by the C++ client. Stream connections stay alive
between episode start/stop commands; only active episodes are written to disk.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import platform
import queue
import re
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover - depends on workstation setup
    raise SystemExit("Missing PyYAML. Run: py -m pip install -r requirements_capture.txt") from exc


MANUS_COLUMNS = [
    "sequence", "callback_sequence", "callbacks_coalesced_into_frame", "output_rate_hz",
    "skeleton_index", "glove_id", "role", "node_id",
    "source_timestamp_kind", "manus_publish_time_raw",
    "manus_publish_time_unix_ns", "manus_publish_time_iso8601_utc",
    "callback_system_time_unix_ns", "callback_system_time_iso8601_utc",
    "callback_steady_time_ns", "serialize_system_time_unix_ns",
    "receiver_system_time_unix_ns", "receiver_system_time_iso8601_utc",
    "receiver_steady_time_ns", "transport_latency_estimate_ns",
    "position_x_m", "position_y_m", "position_z_m",
    "quaternion_x", "quaternion_y", "quaternion_z", "quaternion_w",
]

OPENVR_TRACKER_COLUMNS = [
    "serial", "role", "role_source",
    "host_query_monotonic_ns", "host_query_system_time_unix_ns", "query_span_ns",
    "frame_index", "device_id", "controller_type",
    "device_connected", "tracking_valid", "tracking_result",
    "position_x_m", "position_y_m", "position_z_m",
    "rotation_quaternion_x", "rotation_quaternion_y",
    "rotation_quaternion_z", "rotation_quaternion_w",
    "linear_velocity_x_m_s", "linear_velocity_y_m_s", "linear_velocity_z_m_s",
    "angular_velocity_x_rad_s", "angular_velocity_y_rad_s", "angular_velocity_z_rad_s",
]

CAPTURE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def utc_iso(unix_ns: int) -> str:
    return datetime.fromtimestamp(unix_ns / 1_000_000_000, tz=timezone.utc).isoformat(timespec="microseconds")


def clock_anchor() -> Dict[str, Any]:
    before = time.perf_counter_ns()
    system_ns = time.time_ns()
    after = time.perf_counter_ns()
    return {
        "system_time_unix_ns": system_ns,
        "system_time_iso8601_utc": utc_iso(system_ns),
        "steady_time_before_ns": before,
        "steady_time_after_ns": after,
        "steady_time_midpoint_ns": (before + after) // 2,
        "measurement_span_ns": after - before,
    }


def parse_device_id(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 0)
    except ValueError:
        return int(text, 16)


class CsvSink:
    def __init__(self, path: Path, columns: Sequence[str], flush_every_rows: int) -> None:
        self.file = path.open("w", newline="", encoding="utf-8-sig")
        self.writer = csv.DictWriter(self.file, fieldnames=columns, extrasaction="ignore")
        self.writer.writeheader()
        self.flush_every_rows = max(1, flush_every_rows)
        self.rows = 0

    def writerow(self, row: Dict[str, Any]) -> None:
        self.writer.writerow(row)
        self.rows += 1
        if self.rows % self.flush_every_rows == 0:
            self.file.flush()

    def close(self) -> None:
        self.file.flush()
        self.file.close()


class JsonlSink:
    def __init__(self, path: Path, enabled: bool) -> None:
        self.file = path.open("w", encoding="utf-8") if enabled else None
        self.rows = 0

    def write(self, value: Dict[str, Any]) -> None:
        if self.file is None:
            return
        self.file.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.rows += 1
        if self.rows % 120 == 0:
            self.file.flush()

    def close(self) -> None:
        if self.file is not None:
            self.file.flush()
            self.file.close()


class ManusReceiver(threading.Thread):
    def __init__(self, config: Dict[str, Any], stop_event: threading.Event) -> None:
        super().__init__(name="manus-receiver", daemon=True)
        self.config = config
        self.stop_event = stop_event
        self.connected_event = threading.Event()
        self.first_frame_event = threading.Event()
        self.error: Optional[str] = None
        self.server: Optional[socket.socket] = None
        self.client: Optional[socket.socket] = None
        self.stop_request_sent = False
        self._recording_lock = threading.RLock()
        self.csv: Optional[CsvSink] = None
        self.jsonl: Optional[JsonlSink] = None
        self._reset_episode_counters()

        mapping = config.get("gloves", {})
        self.glove_roles = {
            device_id: role
            for role, value in mapping.items()
            if (device_id := parse_device_id(value)) is not None
        }

    def _reset_episode_counters(self) -> None:
        self.frames = 0
        self.nodes = 0
        self.parse_errors = 0
        self.sequence_gaps = 0
        self.last_sequence: Optional[int] = None
        self.last_callback_sequence: Optional[int] = None
        self.first_callback_sequence: Optional[int] = None
        self.callbacks_coalesced = 0
        self.callback_sequence_gaps_unexplained = 0
        self.output_rate_target_hz: Optional[int] = None
        self.first_callback_steady_ns: Optional[int] = None
        self.last_callback_steady_ns: Optional[int] = None
        self.observed_glove_ids: set[int] = set()

    def start_recording(self, session_dir: Path) -> None:
        with self._recording_lock:
            if self.csv is not None:
                raise RuntimeError("MANUS episode is already recording")
            self._reset_episode_counters()
            flush_rows = int(self.config.get("flush_every_rows", 240))
            self.csv = CsvSink(
                session_dir / "manus_raw_skeleton.csv", MANUS_COLUMNS, flush_rows
            )
            self.jsonl = JsonlSink(
                session_dir / "manus_raw_skeleton.jsonl",
                bool(self.config.get("save_jsonl", True)),
            )

    def stop_recording(self) -> None:
        with self._recording_lock:
            csv_sink, jsonl_sink = self.csv, self.jsonl
            self.csv = None
            self.jsonl = None
            if csv_sink is not None:
                csv_sink.close()
            if jsonl_sink is not None:
                jsonl_sink.close()

    def run(self) -> None:
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((str(self.config.get("host", "127.0.0.1")), int(self.config.get("port", 8888))))
            self.server.listen(1)
            self.server.settimeout(1.0)
            print(f"[MANUS] waiting on {self.config.get('host', '127.0.0.1')}:{self.config.get('port', 8888)}")
            while not self.stop_event.is_set():
                try:
                    self.client, address = self.server.accept()
                    break
                except socket.timeout:
                    continue
            if self.client is None:
                return
            print(f"[MANUS] connected: {address}")
            self.connected_event.set()
            self.client.settimeout(1.0)
            buffer = b""
            # After Ctrl+C, keep receiving until the C++ client disables SDK
            # callbacks, drains both sender queues, and closes this socket.
            while True:
                try:
                    chunk = self.client.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                if not chunk:
                    if self.stop_event.is_set():
                        print("[STOP] MANUS queues drained; finalizing files")
                    else:
                        print("[MANUS] client closed the stream; stopping the session cleanly")
                        self.stop_event.set()
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    arrival_system_ns = time.time_ns()
                    arrival_steady_ns = time.perf_counter_ns()
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                        if message.get("stream") == "manus_raw_skeleton":
                            self._record(message, arrival_system_ns, arrival_steady_ns)
                    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                        self.parse_errors += 1
                        print(f"[MANUS] discarded malformed frame: {exc}")
        except Exception as exc:  # keep the main thread informed
            self.error = f"MANUS receiver failed: {exc}"
            self.stop_event.set()
        finally:
            self._close_sockets()
            self.stop_recording()

    def _record(self, message: Dict[str, Any], arrival_system_ns: int, arrival_steady_ns: int) -> None:
        if message.get("stream") != "manus_raw_skeleton":
            raise ValueError(f"unexpected stream {message.get('stream')!r}")
        self.first_frame_event.set()
        with self._recording_lock:
            if self.csv is None or self.jsonl is None:
                return
            self._record_active_frame(message, arrival_system_ns, arrival_steady_ns)

    def _record_active_frame(
        self, message: Dict[str, Any], arrival_system_ns: int, arrival_steady_ns: int
    ) -> None:
        sequence = int(message["sequence"])
        if self.last_sequence is not None and sequence != self.last_sequence + 1:
            self.sequence_gaps += max(0, sequence - self.last_sequence - 1)
        self.last_sequence = sequence
        callback_sequence = int(message.get("callback_sequence", sequence))
        coalesced = int(message.get("callbacks_coalesced_into_frame", 0))
        if self.last_callback_sequence is None:
            self.first_callback_sequence = callback_sequence
            self.callback_sequence_gaps_unexplained += max(0, callback_sequence - coalesced)
        else:
            expected_callback_sequence = self.last_callback_sequence + coalesced + 1
            self.callback_sequence_gaps_unexplained += max(
                0, callback_sequence - expected_callback_sequence
            )
        self.last_callback_sequence = callback_sequence
        self.callbacks_coalesced += coalesced
        if message.get("output_rate_hz") is not None:
            self.output_rate_target_hz = int(message["output_rate_hz"])
        callback_system_ns = int(message["callback_system_time_unix_ns"])
        callback_steady_ns = int(message["callback_steady_time_ns"])
        if self.first_callback_steady_ns is None:
            self.first_callback_steady_ns = callback_steady_ns
        self.last_callback_steady_ns = callback_steady_ns
        capture_info = {
            "receiver_system_time_unix_ns": arrival_system_ns,
            "receiver_steady_time_ns": arrival_steady_ns,
            "transport_latency_estimate_ns": arrival_system_ns - callback_system_ns,
        }
        raw_message = copy.deepcopy(message)
        raw_message["recorder_receive"] = capture_info
        self.jsonl.write(raw_message)

        base = {
            "sequence": sequence,
            "callback_sequence": callback_sequence,
            "callbacks_coalesced_into_frame": coalesced,
            "output_rate_hz": message.get("output_rate_hz", ""),
            "source_timestamp_kind": "manus_core_publish_time",
            "manus_publish_time_raw": message.get("manus_publish_time_raw", ""),
            "manus_publish_time_unix_ns": message.get("manus_publish_time_unix_ns", ""),
            "manus_publish_time_iso8601_utc": (
                utc_iso(int(message["manus_publish_time_unix_ns"]))
                if message.get("manus_publish_time_unix_ns") is not None else ""
            ),
            "callback_system_time_unix_ns": callback_system_ns,
            "callback_system_time_iso8601_utc": utc_iso(callback_system_ns),
            "callback_steady_time_ns": message.get("callback_steady_time_ns", ""),
            "serialize_system_time_unix_ns": message.get("serialize_system_time_unix_ns", ""),
            "receiver_system_time_unix_ns": arrival_system_ns,
            "receiver_system_time_iso8601_utc": utc_iso(arrival_system_ns),
            "receiver_steady_time_ns": arrival_steady_ns,
            "transport_latency_estimate_ns": arrival_system_ns - callback_system_ns,
        }
        for skeleton_index, skeleton in enumerate(message.get("skeletons", [])):
            glove_id = int(skeleton["glove_id"])
            self.observed_glove_ids.add(glove_id)
            role = self.glove_roles.get(glove_id, f"unmapped:{glove_id}")
            for node in skeleton.get("nodes", []):
                position = node["position"]
                quaternion = node["quaternion_xyzw"]
                self.csv.writerow({
                    **base,
                    "skeleton_index": skeleton_index,
                    "glove_id": glove_id,
                    "role": role,
                    "node_id": node["node_id"],
                    "position_x_m": position[0], "position_y_m": position[1], "position_z_m": position[2],
                    "quaternion_x": quaternion[0], "quaternion_y": quaternion[1],
                    "quaternion_z": quaternion[2], "quaternion_w": quaternion[3],
                })
                self.nodes += 1
        self.frames += 1

    def _close_sockets(self) -> None:
        for sock in (self.client, self.server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def abort_drain(self) -> None:
        """Unblock recv when the MANUS client does not close after STOP."""
        if self.client is not None:
            try:
                self.client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._close_sockets()

    def request_shutdown(self) -> None:
        """Stop SDK acquisition, then keep recv alive until C++ drains and closes."""
        self.stop_event.set()
        if self.client is not None and not self.stop_request_sent:
            try:
                self.client.sendall(b'{"command":"stop"}\n')
                self.stop_request_sent = True
                print("[STREAM] MANUS shutdown requested; draining sender queue...")
            except OSError:
                pass

    def summary(self) -> Dict[str, Any]:
        configured = set(self.glove_roles)
        callback_duration_seconds = 0.0
        if (
            self.first_callback_steady_ns is not None
            and self.last_callback_steady_ns is not None
            and self.last_callback_steady_ns > self.first_callback_steady_ns
        ):
            callback_duration_seconds = (
                self.last_callback_steady_ns - self.first_callback_steady_ns
            ) / 1_000_000_000
        output_rate_hz = (
            (self.frames - 1) / callback_duration_seconds
            if callback_duration_seconds > 0 and self.frames > 1 else None
        )
        source_callback_rate_hz = (
            (self.last_callback_sequence - self.first_callback_sequence) / callback_duration_seconds
            if callback_duration_seconds > 0
            and self.last_callback_sequence is not None
            and self.first_callback_sequence is not None else None
        )
        return {
            "frames": self.frames,
            "node_rows": self.nodes,
            "parse_errors": self.parse_errors,
            "sequence_gaps": self.sequence_gaps,
            "output_rate_target_hz": self.output_rate_target_hz,
            "output_rate_measured_hz": output_rate_hz,
            "source_callback_rate_estimate_hz": source_callback_rate_hz,
            "source_callback_frames": (
                self.last_callback_sequence + 1
                if self.last_callback_sequence is not None else 0
            ),
            "callbacks_coalesced": self.callbacks_coalesced,
            "callback_sequence_gaps_unexplained": self.callback_sequence_gaps_unexplained,
            "observed_glove_ids": sorted(self.observed_glove_ids),
            "configured_glove_ids": sorted(configured),
            "missing_configured_glove_ids": sorted(configured - self.observed_glove_ids),
            "error": self.error,
        }


def matrix34_values(matrix: Any) -> List[List[float]]:
    rows = getattr(matrix, "m", matrix)
    return [[float(rows[r][c]) for c in range(4)] for r in range(3)]


def vector3_values(vector: Any) -> Tuple[float, float, float]:
    values = getattr(vector, "v", vector)
    return float(values[0]), float(values[1]), float(values[2])


def rotation_matrix_to_xyzw(m: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]:
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        return ((m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, 0.25 * s)
    if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        return (0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s, (m[2][1] - m[1][2]) / s)
    if m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        return ((m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s, (m[0][2] - m[2][0]) / s)
    s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
    return ((m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s, (m[1][0] - m[0][1]) / s)


class OpenVrRecorder:
    def __init__(self, config: Dict[str, Any]) -> None:
        try:
            import openvr
        except ImportError as exc:  # pragma: no cover - depends on workstation setup
            raise RuntimeError("Missing openvr. Run: py -m pip install -r requirements_capture.txt") from exc
        self.openvr = openvr
        self.system = openvr.init(openvr.VRApplication_Background)
        self.config = config
        self.poll_hz = float(config.get("poll_hz", 120.0))
        self.prediction_seconds = float(config.get("predicted_seconds_from_now", 0.0))
        universe_name = str(config.get("tracking_universe", "standing")).lower()
        universes = {
            "standing": openvr.TrackingUniverseStanding,
            "seated": openvr.TrackingUniverseSeated,
            "raw": openvr.TrackingUniverseRawAndUncalibrated,
        }
        if universe_name not in universes:
            raise ValueError(f"Unsupported OpenVR tracking_universe: {universe_name}")
        self.universe_name = universe_name
        self.universe = universes[universe_name]
        self.serial_to_role = {
            str(serial).strip().upper(): role
            for role, serial in config.get("trackers", {}).items()
            if serial
        }
        self.openvr_serial_to_role: Dict[str, str] = {}
        self.device_controller_types: Dict[int, str] = {}
        self._last_tracker_status: Dict[str, Tuple[str, bool, bool]] = {}
        self.device_serials = self._enumerate_trackers()
        missing = sorted(set(self.serial_to_role) - {serial.upper() for serial in self.device_serials.values()})
        if missing and bool(config.get("strict_device_mapping", True)):
            raise RuntimeError(f"Configured Vive trackers not found: {missing}; discovered: {self.device_serials}")
        self._recording_lock = threading.RLock()
        self.csv: Optional[CsvSink] = None
        self.jsonl: Optional[JsonlSink] = None
        self.stream_polls = 0
        self._reset_episode_counters()

        self.interval_ns = max(1, int(1_000_000_000 / self.poll_hz))
        self.next_poll_ns = time.perf_counter_ns()

    def _reset_episode_counters(self) -> None:
        self.polls = 0
        self.rows = 0
        self.invalid_poses = 0
        self.poll_overruns = 0

    def start_recording(self, session_dir: Path) -> None:
        with self._recording_lock:
            if self.csv is not None:
                raise RuntimeError("OpenVR episode is already recording")
            self._reset_episode_counters()
            flush_rows = int(self.config.get("flush_every_rows", 120))
            self.csv = CsvSink(
                session_dir / "tracker_openvr.csv", OPENVR_TRACKER_COLUMNS, flush_rows
            )
            self.jsonl = JsonlSink(
                session_dir / "tracker_openvr.jsonl",
                bool(self.config.get("save_jsonl", True)),
            )

    def stop_recording(self) -> None:
        with self._recording_lock:
            csv_sink, jsonl_sink = self.csv, self.jsonl
            self.csv = None
            self.jsonl = None
            if csv_sink is not None:
                csv_sink.close()
            if jsonl_sink is not None:
                jsonl_sink.close()

    def emit_initial_status(self) -> None:
        discovered = {serial.strip().upper() for serial in self.device_serials.values()}
        for serial, role in self.serial_to_role.items():
            if serial not in discovered:
                self._emit_tracker_status(serial, role, False, False)
        for index, serial in self.device_serials.items():
            role, _ = self._resolve_role(serial)
            self._emit_tracker_status(
                serial,
                role,
                bool(self.system.isTrackedDeviceConnected(index)),
                False,
            )

    def _emit_tracker_status(
        self, serial: str, role: str, connected: bool, tracking: bool
    ) -> None:
        normalized = serial.strip().upper()
        state = (role, connected, tracking)
        if self._last_tracker_status.get(normalized) == state:
            return
        self._last_tracker_status[normalized] = state
        safe_role = "".join(
            ch if ch.isalnum() or ch in "_-" else "_" for ch in role
        ) or "unknown"
        print(
            f"[TRACKER_STATUS] role={safe_role} serial={normalized} "
            f"connected={int(connected)} tracking={int(tracking)}",
            flush=True,
        )

    def _enumerate_trackers(self) -> Dict[int, str]:
        result: Dict[int, str] = {}
        accepted_classes = {
            self.openvr.TrackedDeviceClass_GenericTracker,
            self.openvr.TrackedDeviceClass_Controller,
        }
        for index in range(self.openvr.k_unMaxTrackedDeviceCount):
            device_class = self.system.getTrackedDeviceClass(index)
            if device_class not in accepted_classes:
                continue
            try:
                serial = self.system.getStringTrackedDeviceProperty(index, self.openvr.Prop_SerialNumber_String)
                controller_type = self.system.getStringTrackedDeviceProperty(index, self.openvr.Prop_ControllerType_String)
            except Exception:
                continue
            prefix = "vive_tracker_"
            role = controller_type[len(prefix):] if controller_type.startswith(prefix) else ""
            if device_class == self.openvr.TrackedDeviceClass_Controller and not role:
                continue
            normalized_serial = serial.strip().upper()
            if role:
                existing = self.openvr_serial_to_role.get(normalized_serial)
                if existing is not None and existing != role:
                    raise RuntimeError(f"SteamVR Tracker {serial} has conflicting roles: {existing}, {role}")
                if role in self.openvr_serial_to_role.values() and normalized_serial not in self.openvr_serial_to_role:
                    raise RuntimeError(f"SteamVR has multiple Trackers assigned to role {role}")
                self.openvr_serial_to_role[normalized_serial] = role
            self.device_controller_types[index] = controller_type
            result[index] = serial
        print(f"[OpenVR] generic trackers: {result}")
        if self.openvr_serial_to_role:
            print(f"[OpenVR] tracker roles: {self.openvr_serial_to_role}")
        return result

    def _resolve_role(self, serial: str) -> Tuple[str, str]:
        normalized = serial.strip().upper()
        openvr_role = self.openvr_serial_to_role.get(normalized)
        if openvr_role:
            return openvr_role, "openvr_controller_type"
        configured_role = self.serial_to_role.get(normalized)
        if configured_role:
            return configured_role, "capture_config"
        return f"unmapped:{serial}", "unmapped"

    def poll_once(self) -> None:
        now_ns = time.perf_counter_ns()
        if now_ns < self.next_poll_ns:
            time.sleep((self.next_poll_ns - now_ns) / 1_000_000_000)
        before_steady = time.perf_counter_ns()
        before_system = time.time_ns()
        poses = self.system.getDeviceToAbsoluteTrackingPose(
            self.universe,
            self.prediction_seconds,
            self.openvr.k_unMaxTrackedDeviceCount,
        )
        after_system = time.time_ns()
        after_steady = time.perf_counter_ns()
        self._record_poll(poses, before_system, after_system, before_steady, after_steady)
        self.stream_polls += 1
        self.next_poll_ns += self.interval_ns
        if after_steady > self.next_poll_ns:
            skipped = (after_steady - self.next_poll_ns) // self.interval_ns + 1
            with self._recording_lock:
                if self.csv is not None:
                    self.poll_overruns += int(skipped)
            self.next_poll_ns += int(skipped) * self.interval_ns

    def _record_poll(self, poses: Iterable[Any], system_before: int, system_after: int, steady_before: int, steady_after: int) -> None:
        midpoint_system = (system_before + system_after) // 2
        midpoint_steady = (steady_before + steady_after) // 2
        records: List[Dict[str, Any]] = []
        for index, serial in self.device_serials.items():
            pose = poses[index]
            valid = bool(pose.bPoseIsValid)
            connected = bool(pose.bDeviceIsConnected)
            blank = ""
            position: Sequence[Any] = [blank] * 3
            quaternion: Sequence[Any] = [blank] * 4
            velocity: Sequence[Any] = [blank] * 3
            angular_velocity: Sequence[Any] = [blank] * 3
            if valid:
                matrix = matrix34_values(pose.mDeviceToAbsoluteTracking)
                position = [matrix[0][3], matrix[1][3], matrix[2][3]]
                quaternion = rotation_matrix_to_xyzw(matrix)
                velocity = vector3_values(pose.vVelocity)
                angular_velocity = vector3_values(pose.vAngularVelocity)
            role, role_source = self._resolve_role(serial)
            self._emit_tracker_status(serial, role, connected, connected and valid)
            with self._recording_lock:
                recording = self.csv is not None and self.jsonl is not None
            if not recording:
                continue
            if not valid:
                self.invalid_poses += 1
            controller_type = self.device_controller_types.get(index, "")
            row = {
                "serial": serial,
                "role": role,
                "role_source": role_source,
                "host_query_monotonic_ns": midpoint_steady,
                "host_query_system_time_unix_ns": midpoint_system,
                "query_span_ns": steady_after - steady_before,
                "frame_index": self.polls,
                "device_id": index,
                "controller_type": controller_type,
                "device_connected": int(connected),
                "tracking_valid": int(connected and valid),
                "tracking_result": int(pose.eTrackingResult),
                "position_x_m": position[0], "position_y_m": position[1], "position_z_m": position[2],
                "rotation_quaternion_x": quaternion[0],
                "rotation_quaternion_y": quaternion[1],
                "rotation_quaternion_z": quaternion[2],
                "rotation_quaternion_w": quaternion[3],
                "linear_velocity_x_m_s": velocity[0],
                "linear_velocity_y_m_s": velocity[1],
                "linear_velocity_z_m_s": velocity[2],
                "angular_velocity_x_rad_s": angular_velocity[0],
                "angular_velocity_y_rad_s": angular_velocity[1],
                "angular_velocity_z_rad_s": angular_velocity[2],
            }
            assert self.csv is not None
            self.csv.writerow(row)
            records.append(row)
            self.rows += 1
        with self._recording_lock:
            if self.csv is None or self.jsonl is None:
                return
            self.jsonl.write({
            "schema_version": 1,
            "stream": "tracker_openvr",
            "poll_sequence": self.polls,
            "poll_system_time_before_unix_ns": system_before,
            "poll_system_time_after_unix_ns": system_after,
            "poll_system_time_midpoint_unix_ns": midpoint_system,
            "poll_steady_time_before_ns": steady_before,
            "poll_steady_time_after_ns": steady_after,
            "trackers": records,
            })
            self.polls += 1

    def close(self) -> None:
        self.stop_recording()
        self.openvr.shutdown()

    def summary(self) -> Dict[str, Any]:
        discovered_serials = set(self.device_serials.values())
        normalized_discovered_serials = {serial.strip().upper() for serial in discovered_serials}
        role_bindings = {}
        for serial in discovered_serials:
            role, source = self._resolve_role(serial)
            role_bindings[serial] = {"role": role, "source": source}
        return {
            "polls": self.polls,
            "tracker_rows": self.rows,
            "invalid_pose_rows": self.invalid_poses,
            "poll_overruns": self.poll_overruns,
            "discovered_trackers": self.device_serials,
            "configured_tracker_serials": sorted(self.serial_to_role),
            "missing_configured_tracker_serials": sorted(set(self.serial_to_role) - normalized_discovered_serials),
            "role_bindings": role_bindings,
        }


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError("configuration root must be a mapping")
    return config


def validate_config(config: Dict[str, Any], manus_only: bool = False) -> None:
    required_glove_roles = {"left_glove", "right_glove"}
    manus = config.get("manus", {})
    openvr_config = config.get("openvr", {})
    if bool(manus.get("strict_device_mapping", True)):
        gloves = manus.get("gloves", {})
        missing = sorted(role for role in required_glove_roles if not gloves.get(role))
        if missing:
            raise ValueError(f"Fill MANUS glove IDs in YAML before capture; missing roles: {missing}")
        ids = [parse_device_id(gloves[role]) for role in required_glove_roles]
        if len(set(ids)) != len(ids):
            raise ValueError("MANUS left/right glove IDs must be different")
    if not manus_only and bool(openvr_config.get("strict_device_mapping", True)):
        trackers = openvr_config.get("trackers", {})
        serials = [str(serial) for serial in trackers.values() if serial]
        if len(set(serials)) != len(serials):
            raise ValueError("Configured Vive tracker serials must be different")


def wait_for_event_or_stop(
    event: threading.Event, stop_event: threading.Event, timeout_seconds: float
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while not event.wait(0.25):
        if stop_event.is_set() or time.monotonic() >= deadline:
            return False
    return True


def validate_capture_segment(value: str, name: str, maximum: int) -> str:
    clean = value.strip()
    if (
        not clean
        or len(clean) > maximum
        or CAPTURE_SEGMENT_PATTERN.fullmatch(clean) is None
    ):
        raise ValueError(
            f"{name} must contain only letters, digits, underscores, or hyphens"
        )
    return clean


def watch_control_stdin(
    commands: "queue.Queue[str]", shutdown_event: threading.Event
) -> None:
    """Forward UI commands without coupling episode STOP to stream shutdown."""
    for line in sys.stdin:
        command = line.strip()
        if not command:
            continue
        commands.put(command)
        if command.lower() in {"shutdown", "q", "quit"}:
            return
    shutdown_event.set()


def write_metadata(path: Path, metadata: Dict[str, Any]) -> None:
    path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def new_episode_metadata(
    config: Dict[str, Any],
    task_name: str,
    complex_level: str,
    session_id: str,
    manus_only: bool,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "schema_version": 2,
        "session_id": session_id,
        "task_name": task_name,
        "complex_level": complex_level,
        "capture_mode": (
            "manus_raw_skeleton_only"
            if manus_only else "manus_raw_skeleton_and_openvr_tracker"
        ),
        "sources": (
            ["manus_raw_skeleton"]
            if manus_only else ["manus_raw_skeleton", "openvr_tracker"]
        ),
        "status": "recording",
        "created": clock_anchor(),
        "recording_started": clock_anchor(),
        "clock_notes": {
            "system_time": "UTC Unix time from the Windows/Python system clock; can jump if the OS clock is adjusted.",
            "steady_time": "Monotonic host clock for interval/alignment calculations; epoch is intentionally unspecified.",
            "manus_publish_time": "MANUS Core RawSkeleton publishTime decoded to UTC while retaining the raw value for audit.",
            "openvr_time": "OpenVR returns no per-device timestamp here; host_query_* is the midpoint of the host query interval.",
        },
        "coordinate_notes": {
            "manus": "Configured by the C++ client as right-handed, +Z up, X-from-viewer, meters; HandMotion_Auto.",
        },
        "software": {
            "python": sys.version,
            "platform": platform.platform(),
        },
        "config": config,
    }
    if not manus_only:
        metadata["coordinate_notes"].update(
            {
                "openvr": f"OpenVR {config.get('openvr', {}).get('tracking_universe', 'standing')} universe, meters.",
                "warning": "MANUS and OpenVR coordinates are not calibrated or transformed by this recorder.",
            }
        )
    return metadata


def finalize_episode(
    metadata_path: Path,
    metadata: Dict[str, Any],
    manus: ManusReceiver,
    openvr_recorder: Optional[OpenVrRecorder],
    config: Dict[str, Any],
) -> None:
    if openvr_recorder is not None:
        openvr_recorder.stop_recording()
        metadata["openvr"] = openvr_recorder.summary()
    manus.stop_recording()
    manus_summary = manus.summary()
    metadata["manus"] = manus_summary
    metadata["ended"] = clock_anchor()
    integrity_errors = []
    if manus.error:
        integrity_errors.append(manus.error)
    if (
        bool(config.get("manus", {}).get("strict_device_mapping", True))
        and manus_summary["missing_configured_glove_ids"]
    ):
        integrity_errors.append(
            f"Configured gloves not observed: {manus_summary['missing_configured_glove_ids']}"
        )
    if (
        bool(config.get("manus", {}).get("fail_on_sequence_gap", True))
        and manus_summary["sequence_gaps"]
    ):
        integrity_errors.append(f"MANUS sequence gaps: {manus_summary['sequence_gaps']}")
    if (
        bool(config.get("manus", {}).get("fail_on_sequence_gap", True))
        and manus_summary["callback_sequence_gaps_unexplained"]
    ):
        integrity_errors.append(
            "MANUS unexplained callback gaps: "
            f"{manus_summary['callback_sequence_gaps_unexplained']}"
        )
    if manus_summary["parse_errors"]:
        integrity_errors.append(f"MANUS parse errors: {manus_summary['parse_errors']}")
    metadata["status"] = "failed" if integrity_errors else "complete"
    if integrity_errors:
        metadata["error"] = "; ".join(integrity_errors)
    write_metadata(metadata_path, metadata)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("capture_config.yaml"))
    parser.add_argument(
        "--manus-only",
        action="store_true",
        help="record MANUS RawSkeleton only; do not initialize SteamVR/OpenVR",
    )
    parser.add_argument(
        "--control-stdin",
        action="store_true",
        help="accept start/stop/shutdown commands while data streams stay alive",
    )
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--task-name", default="test")
    parser.add_argument("--complex-level", default="L_test")
    args = parser.parse_args()
    config = load_config(args.config)
    validate_config(config, manus_only=args.manus_only)
    session_config = config.get("session", {})
    output_root = (args.output_root or Path(session_config.get("output_root", "vr_data"))).resolve()
    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    commands: "queue.Queue[str]" = queue.Queue()
    if args.control_stdin:
        threading.Thread(
            target=watch_control_stdin,
            args=(commands, stop_event),
            name="control-stdin",
            daemon=True,
        ).start()
    else:
        commands.put(f"start {args.task_name} {args.complex_level}")

    manus = ManusReceiver(config.get("manus", {}), stop_event)
    openvr_recorder: Optional[OpenVrRecorder] = None
    active_episode: Optional[Dict[str, Any]] = None
    active_metadata_path: Optional[Path] = None
    episode_deadline: Optional[float] = None
    exit_code = 0
    try:
        manus.start()
        if not args.manus_only:
            openvr_recorder = OpenVrRecorder(config.get("openvr", {}))
            openvr_recorder.emit_initial_status()
        print("[READY] Start the 2_4_120hz or 2_4_60hz MANUS client and select Core Local.")
        wait_seconds = float(session_config.get("manus_first_frame_timeout_seconds", 180))
        if not wait_for_event_or_stop(manus.first_frame_event, stop_event, wait_seconds):
            if not stop_event.is_set():
                raise RuntimeError(
                    f"No MANUS RawSkeleton frame received within {wait_seconds:g} seconds"
                )
            print("[STREAM] stopped before the first MANUS frame")
        else:
            print("[STREAMING] MANUS RawSkeleton + OpenVR Tracker data streams ready")

        while not stop_event.is_set():
            while True:
                try:
                    command = commands.get_nowait()
                except queue.Empty:
                    break
                parts = command.split()
                action = parts[0].lower() if parts else ""
                if action == "start":
                    if len(parts) != 3:
                        print("[CONTROL_ERROR] usage: start <task_name> <complex_level>")
                        continue
                    if active_episode is not None:
                        print("[CONTROL_ERROR] an episode is already recording")
                        continue
                    try:
                        task_name = validate_capture_segment(parts[1], "task_name", 31)
                        complex_level = validate_capture_segment(parts[2], "complex_level", 15)
                        now = datetime.now()
                        session_id = f"{time.time_ns() % 1_000_000_000:09d}"
                        episode_name = f"ep_{now.strftime('%Y%m%d_%H%M%S')}_{session_id}"
                        session_dir = output_root / task_name / complex_level / episode_name
                        session_dir.mkdir(parents=True, exist_ok=False)
                        active_episode = new_episode_metadata(
                            config, task_name, complex_level, session_id, args.manus_only
                        )
                        active_metadata_path = session_dir / "session_metadata.json"
                        write_metadata(active_metadata_path, active_episode)
                        manus.start_recording(session_dir)
                        if openvr_recorder is not None:
                            openvr_recorder.start_recording(session_dir)
                        duration_seconds = float(session_config.get("duration_seconds", 0))
                        episode_deadline = (
                            time.monotonic() + duration_seconds
                            if duration_seconds > 0 else None
                        )
                        print(f"[RECORDING] {session_dir} (send 'stop' to finish episode)")
                    except Exception as exc:
                        if active_metadata_path is not None and active_episode is not None:
                            active_episode["status"] = "failed"
                            active_episode["error"] = str(exc)
                            write_metadata(active_metadata_path, active_episode)
                        manus.stop_recording()
                        if openvr_recorder is not None:
                            openvr_recorder.stop_recording()
                        active_episode = None
                        active_metadata_path = None
                        episode_deadline = None
                        print(f"[CONTROL_ERROR] could not start episode: {exc}")
                elif action == "stop":
                    if active_episode is None or active_metadata_path is None:
                        print("[IDLE] no episode is recording; data streams remain active")
                        continue
                    saved_dir = active_metadata_path.parent
                    finalize_episode(
                        active_metadata_path,
                        active_episode,
                        manus,
                        openvr_recorder,
                        config,
                    )
                    print(f"[SAVED] {saved_dir}")
                    print("[IDLE] data streams remain active")
                    active_episode = None
                    active_metadata_path = None
                    episode_deadline = None
                elif action in {"shutdown", "q", "quit"}:
                    print("[CONTROL] tracker and MANUS stream shutdown requested")
                    stop_event.set()
                    break
                else:
                    print(f"[CONTROL_ERROR] unknown command: {command}")

            if (
                active_episode is not None
                and episode_deadline is not None
                and time.monotonic() >= episode_deadline
            ):
                commands.put("stop")
            if openvr_recorder is not None:
                openvr_recorder.poll_once()
            else:
                stop_event.wait(0.02)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        stop_event.set()
        exit_code = 1
    finally:
        if active_episode is not None and active_metadata_path is not None:
            finalize_episode(
                active_metadata_path,
                active_episode,
                manus,
                openvr_recorder,
                config,
            )
            print(f"[SAVED] {active_metadata_path.parent}")
        manus.request_shutdown()
        manus.join(timeout=15.0)
        if manus.is_alive():
            print("[STOP] MANUS client did not close after 15 seconds; closing the receiver socket")
            manus.abort_drain()
            manus.join()
        if openvr_recorder is not None:
            openvr_recorder.close()
        print("[DONE] MANUS/OpenVR data streams stopped")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
