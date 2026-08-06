"""OpenXR Runtime-time pose, trigger, and ESP32 synchronization capture."""

from __future__ import annotations

import csv
import ctypes
import json
import math
import time
from ctypes import POINTER, byref, cast
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import xr

from .esp32_sync import Esp32SyncMessage, Esp32UdpReceiver, query_performance_counter_ticks
from .schema import ESP32_SYNC_FIELDS, POSE_FIELDS, TRIGGER_FIELDS


REQUIRED_EXTENSIONS = [
    "XR_MND_headless",
    "XR_KHR_win32_convert_performance_counter_time",
    "XR_HTCX_vive_tracker_interaction",
]
ULTIMATE_EXTENSION = "XR_HTC_vive_xr_tracker_interaction"
TRACKER_INTERACTION_PROFILE = "/interaction_profiles/htc/vive_tracker_htcx"
ULTIMATE_INTERACTION_PROFILE = "/interaction_profiles/htc/vive_xr_tracker"
POSITION_VALID = int(xr.SpaceLocationFlags.POSITION_VALID_BIT)
ORIENTATION_VALID = int(xr.SpaceLocationFlags.ORIENTATION_VALID_BIT)


@dataclass
class CaptureConfig:
    output_dir: Path
    duration_s: float | None = 10.0
    rate_hz: float = 120.0
    console_pose_interval_s: float = 1.0
    esp32_bind_host: str = "0.0.0.0"
    esp32_udp_port: int | None = None
    sync_match_window_ms: float = 30.0
    role_map_path: Path | None = Path("tracker_roles.json")
    write_output_files: bool = True
    pose_frame_callback: Callable[[list[dict]], None] | None = None
    trigger_event_callback: Callable[[dict], None] | None = None
    triggered_pose_frame_callback: Callable[[list[dict]], None] | None = None
    external_stop_event: object | None = None


@dataclass
class TrackerDescriptor:
    device_id: str
    serial_number: str
    tracker_role: str
    role_path: str
    persistent_path: str
    interaction_profile: str
    role_xr_path: object
    discovery_source: str = "XR_HTCX_vive_tracker_interaction"
    action_space: object | None = None


@dataclass
class UltimateSlot:
    slot_index: int
    user_path: str
    xr_path: object
    action_space: object | None = None
    mapped_serial_number: str = ""


@dataclass
class LocatedPose:
    location_flags: int
    velocity_flags: int
    position_x: float
    position_y: float
    position_z: float
    rotation_x: float
    rotation_y: float
    rotation_z: float
    rotation_w: float
    linear_x: float | None
    linear_y: float | None
    linear_z: float | None
    angular_x: float | None
    angular_y: float | None
    angular_z: float | None

    @property
    def tracking_valid(self) -> bool:
        needed = POSITION_VALID | ORIENTATION_VALID
        return (self.location_flags & needed) == needed


class OpenXRTrackerCapture:
    """Headless OpenXR capture using the SteamVR VIVE Tracker interaction profile."""

    def __init__(self, config: CaptureConfig):
        self.config = config
        self.instance = None
        self.session = None
        self.system_id = None
        self.action_set = None
        self.pose_action = None
        self.trigger_action = None
        self.ultimate_pose_action = None
        self.ultimate_trigger_action = None
        self.base_space = None
        self.trackers: list[TrackerDescriptor] = []
        self.ultimate_slots: list[UltimateSlot] = []
        self.session_state = xr.SessionState.IDLE
        self.session_running = False
        self.locate_space_fn = None
        self.esp32_receiver: Esp32UdpReceiver | None = None
        self.esp32_messages: list[Esp32SyncMessage] = []
        self.trigger_rows: list[dict] = []
        self.runtime_name = ""
        self.runtime_version = ""
        self.frame_count = 0
        self.pose_row_count = 0
        self.trigger_active_samples: dict[str, int] = {}
        self.trigger_total_samples: dict[str, int] = {}
        self.trigger_true_samples: dict[str, int] = {}
        self.initialized_trigger_keys: set[str] = set()
        self.trigger_last_state: dict[str, bool] = {}
        self.trigger_last_change_time: dict[str, int] = {}
        self.triggered_pose_event_count = 0

    def _emit_all_tracker_poses_at_trigger(
        self, event_time: int, trigger_row: dict
    ) -> None:
        """Locate every Tracker on the same XrTime as one trigger edge."""

        self.triggered_pose_event_count += 1
        event_index = self.triggered_pose_event_count
        rows: list[dict] = []
        for tracker in self.trackers:
            pose_state = xr.get_action_state_pose(
                self.session,
                xr.ActionStateGetInfo(
                    action=self.pose_action, subaction_path=tracker.role_xr_path
                ),
            )
            pose = self._locate(tracker.action_space, event_time)
            host_receive_ns = time.perf_counter_ns()
            host_receive_system_ns = time.time_ns()
            rows.append(
                {
                    "trigger_event_index": event_index,
                    "trigger_timestamp_xr_ns": event_time,
                    "trigger_source_device_id": trigger_row["device_id"],
                    "trigger_source_serial_number": trigger_row["serial_number"],
                    "trigger_source_tracker_role": trigger_row["tracker_role"],
                    "trigger_state": int(trigger_row["trigger_state"]),
                    "pose_timestamp_xr_ns": event_time,
                    "host_receive_monotonic_ns": host_receive_ns,
                    "host_receive_system_time_ns": host_receive_system_ns,
                    "latency_ns": self.xr_now() - event_time,
                    "device_id": tracker.device_id,
                    "serial_number": tracker.serial_number,
                    "tracker_role": tracker.tracker_role,
                    "interaction_profile": tracker.interaction_profile,
                    "pose_action_active": int(bool(pose_state.is_active)),
                    "tracking_valid": int(
                        bool(pose_state.is_active) and pose.tracking_valid
                    ),
                    "location_flags": pose.location_flags,
                    "velocity_flags": pose.velocity_flags,
                    "position_x": pose.position_x,
                    "position_y": pose.position_y,
                    "position_z": pose.position_z,
                    "rotation_quaternion_x": pose.rotation_x,
                    "rotation_quaternion_y": pose.rotation_y,
                    "rotation_quaternion_z": pose.rotation_z,
                    "rotation_quaternion_w": pose.rotation_w,
                    "linear_velocity_x": pose.linear_x,
                    "linear_velocity_y": pose.linear_y,
                    "linear_velocity_z": pose.linear_z,
                    "angular_velocity_x": pose.angular_x,
                    "angular_velocity_y": pose.angular_y,
                    "angular_velocity_z": pose.angular_z,
                }
            )
        if self.config.triggered_pose_frame_callback is not None:
            self.config.triggered_pose_frame_callback(rows)

    def initialize(self) -> None:
        available = {
            _decode_c_string(item.extension_name)
            for item in xr.enumerate_instance_extension_properties()
        }
        missing = [name for name in REQUIRED_EXTENSIONS if name not in available]
        if missing:
            raise RuntimeError(f"OpenXR Runtime missing required extensions: {missing}")

        enabled_extensions = list(REQUIRED_EXTENSIONS)
        if ULTIMATE_EXTENSION in available:
            enabled_extensions.append(ULTIMATE_EXTENSION)
        create_info = xr.InstanceCreateInfo(
            application_info=xr.ApplicationInfo(
                application_name="vive_openxr_tracker_capture",
                application_version=1,
            ),
            enabled_extension_names=enabled_extensions,
        )
        self.instance = xr.create_instance(create_info)
        properties = xr.get_instance_properties(self.instance)
        self.runtime_name = _decode_c_string(properties.runtime_name)
        self.runtime_version = str(properties.runtime_version)
        self.system_id = xr.get_system(self.instance)
        self.session = xr.create_session(
            self.instance, xr.SessionCreateInfo(system_id=self.system_id)
        )

        self.trackers = self._enumerate_trackers()
        if not self.trackers:
            self.trackers = self._trackers_from_role_map()
        if not self.trackers:
            raise RuntimeError(
                "OpenXR returned no VIVE Tracker paths and no usable role map was found. "
                "Connect/locate a Tracker or provide tracker_roles.json."
            )
        if ULTIMATE_EXTENSION in available:
            self.ultimate_slots = [
                UltimateSlot(
                    slot_index=index,
                    user_path=f"/user/xr_tracker_htc/vive_ultimate_tracker_{index}",
                    xr_path=xr.string_to_path(
                        self.instance,
                        f"/user/xr_tracker_htc/vive_ultimate_tracker_{index}",
                    ),
                )
                for index in range(5)
            ]
        self._create_actions_and_spaces()
        self.locate_space_fn = cast(
            xr.get_instance_proc_addr(self.instance, "xrLocateSpace"),
            xr.PFN_xrLocateSpace,
        )

        if self.config.esp32_udp_port is not None:
            self.esp32_receiver = Esp32UdpReceiver(
                self.config.esp32_bind_host, self.config.esp32_udp_port
            )
            self.esp32_receiver.start()

    def _enumerate_trackers(self) -> list[TrackerDescriptor]:
        result = []
        for paths in xr.enumerate_vive_tracker_paths_htcx(self.instance):
            persistent = xr.path_to_string(self.instance, paths.persistent_path)
            role_path = (
                xr.path_to_string(self.instance, paths.role_path)
                if paths.role_path
                else ""
            )
            if not role_path:
                continue
            role = role_path.rsplit("/", 1)[-1]
            serial = _serial_from_persistent_path(persistent)
            result.append(
                TrackerDescriptor(
                    device_id=persistent,
                    serial_number=serial,
                    tracker_role=role,
                    role_path=role_path,
                    persistent_path=persistent,
                    interaction_profile=TRACKER_INTERACTION_PROFILE,
                    role_xr_path=paths.role_path,
                )
            )
        return result

    def _trackers_from_role_map(self) -> list[TrackerDescriptor]:
        path = self.config.role_map_path
        if path is None or not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            role_to_serial = data["role_to_serial"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid Tracker role map {path}: {exc}") from exc

        result = []
        for role, serial_value in role_to_serial.items():
            serial = str(serial_value).upper()
            role_path = f"/user/vive_tracker_htcx/role/{role}"
            result.append(
                TrackerDescriptor(
                    device_id=f"/devices/htc/vive_tracker{serial.lower()}",
                    serial_number=serial,
                    tracker_role=str(role),
                    role_path=role_path,
                    persistent_path=f"/devices/htc/vive_tracker{serial.lower()}",
                    interaction_profile=TRACKER_INTERACTION_PROFILE,
                    role_xr_path=xr.string_to_path(self.instance, role_path),
                    discovery_source=f"role_map:{path.resolve()}",
                )
            )
        print(
            "OpenXR 当前未枚举到可跟踪设备；使用角色文件创建等待中的 action："
            f"{path.resolve()}"
        )
        return result

    def _create_actions_and_spaces(self) -> None:
        role_paths = [tracker.role_xr_path for tracker in self.trackers]
        self.action_set = xr.create_action_set(
            self.instance,
            xr.ActionSetCreateInfo(
                action_set_name="capture",
                localized_action_set_name="VIVE Tracker Capture",
            ),
        )
        self.pose_action = xr.create_action(
            self.action_set,
            xr.ActionCreateInfo(
                action_name="tracker_pose",
                localized_action_name="Tracker Pose",
                action_type=xr.ActionType.POSE_INPUT,
                subaction_paths=role_paths,
            ),
        )
        self.trigger_action = xr.create_action(
            self.action_set,
            xr.ActionCreateInfo(
                action_name="trigger_press",
                localized_action_name="Pogo Trigger",
                action_type=xr.ActionType.BOOLEAN_INPUT,
                subaction_paths=role_paths,
            ),
        )
        if self.ultimate_slots:
            ultimate_paths = [slot.xr_path for slot in self.ultimate_slots]
            self.ultimate_pose_action = xr.create_action(
                self.action_set,
                xr.ActionCreateInfo(
                    action_name="ultimate_pose",
                    localized_action_name="Ultimate Tracker Pose",
                    action_type=xr.ActionType.POSE_INPUT,
                    subaction_paths=ultimate_paths,
                ),
            )
            self.ultimate_trigger_action = xr.create_action(
                self.action_set,
                xr.ActionCreateInfo(
                    action_name="ultimate_trigger",
                    localized_action_name="Ultimate Pogo Trigger",
                    action_type=xr.ActionType.BOOLEAN_INPUT,
                    subaction_paths=ultimate_paths,
                ),
            )

        bindings = []
        for tracker in self.trackers:
            bindings.append(
                xr.ActionSuggestedBinding(
                    self.pose_action,
                    xr.string_to_path(
                        self.instance, tracker.role_path + "/input/grip/pose"
                    ),
                )
            )
            bindings.append(
                xr.ActionSuggestedBinding(
                    self.trigger_action,
                    xr.string_to_path(
                        self.instance, tracker.role_path + "/input/trigger/click"
                    ),
                )
            )
        xr.suggest_interaction_profile_bindings(
            self.instance,
            xr.InteractionProfileSuggestedBinding(
                interaction_profile=xr.string_to_path(
                    self.instance, TRACKER_INTERACTION_PROFILE
                ),
                suggested_bindings=bindings,
            ),
        )
        if self.ultimate_slots:
            ultimate_bindings = []
            for slot in self.ultimate_slots:
                ultimate_bindings.extend(
                    [
                        xr.ActionSuggestedBinding(
                            self.ultimate_pose_action,
                            xr.string_to_path(
                                self.instance, slot.user_path + "/input/entity_htc/pose"
                            ),
                        ),
                        xr.ActionSuggestedBinding(
                            self.ultimate_trigger_action,
                            xr.string_to_path(
                                self.instance, slot.user_path + "/input/trigger/click"
                            ),
                        ),
                    ]
                )
            xr.suggest_interaction_profile_bindings(
                self.instance,
                xr.InteractionProfileSuggestedBinding(
                    interaction_profile=xr.string_to_path(
                        self.instance, ULTIMATE_INTERACTION_PROFILE
                    ),
                    suggested_bindings=ultimate_bindings,
                ),
            )
        xr.attach_session_action_sets(
            self.session,
            xr.SessionActionSetsAttachInfo(action_sets=[self.action_set]),
        )
        self.base_space = xr.create_reference_space(
            self.session,
            xr.ReferenceSpaceCreateInfo(reference_space_type=xr.ReferenceSpaceType.STAGE),
        )
        for tracker in self.trackers:
            tracker.action_space = xr.create_action_space(
                self.session,
                xr.ActionSpaceCreateInfo(
                    action=self.pose_action,
                    subaction_path=tracker.role_xr_path,
                ),
            )
        for slot in self.ultimate_slots:
            slot.action_space = xr.create_action_space(
                self.session,
                xr.ActionSpaceCreateInfo(
                    action=self.ultimate_pose_action,
                    subaction_path=slot.xr_path,
                ),
            )

    def qpc_ticks_to_xr_time(self, qpc_ticks: int) -> int:
        counter = ctypes.c_longlong(qpc_ticks)
        return xr.convert_win32_performance_counter_to_time_khr(
            self.instance, byref(counter)
        ).value

    def xr_now(self) -> int:
        return self.qpc_ticks_to_xr_time(query_performance_counter_ticks())

    def _poll_events(self) -> None:
        while True:
            try:
                buffer = xr.poll_event(self.instance)
            except xr.EventUnavailable:
                return
            if buffer.type != xr.StructureType.EVENT_DATA_SESSION_STATE_CHANGED:
                continue
            event = cast(
                byref(buffer), POINTER(xr.EventDataSessionStateChanged)
            ).contents
            self.session_state = xr.SessionState(event.state)
            print(f"OpenXR session state: {self.session_state.name}")
            if self.session_state == xr.SessionState.READY:
                xr.begin_session(
                    self.session,
                    xr.SessionBeginInfo(xr.ViewConfigurationType.PRIMARY_STEREO),
                )
                self.session_running = True
            elif self.session_state == xr.SessionState.STOPPING:
                self.session_running = False
                xr.end_session(self.session)
            elif self.session_state in (
                xr.SessionState.EXITING,
                xr.SessionState.LOSS_PENDING,
            ):
                raise RuntimeError(f"OpenXR session ended: {self.session_state.name}")

    def _locate(self, space, timestamp_xr_ns: int) -> LocatedPose:
        velocity = xr.SpaceVelocity()
        location = xr.SpaceLocation(next=velocity)
        result_code = self.locate_space_fn(
            space,
            self.base_space,
            timestamp_xr_ns,
            byref(location),
        )
        try:
            checked = xr.check_result(xr.Result(result_code))
        except ValueError as exc:
            raise RuntimeError(f"xrLocateSpace returned unknown result {result_code}") from exc
        if checked.is_exception():
            raise checked

        linear_valid = bool(
            int(velocity.velocity_flags) & int(xr.SpaceVelocityFlags.LINEAR_VALID_BIT)
        )
        angular_valid = bool(
            int(velocity.velocity_flags) & int(xr.SpaceVelocityFlags.ANGULAR_VALID_BIT)
        )
        p = location.pose.position
        q = location.pose.orientation
        lv = velocity.linear_velocity
        av = velocity.angular_velocity
        return LocatedPose(
            location_flags=int(location.location_flags),
            velocity_flags=int(velocity.velocity_flags),
            position_x=p.x,
            position_y=p.y,
            position_z=p.z,
            rotation_x=q.x,
            rotation_y=q.y,
            rotation_z=q.z,
            rotation_w=q.w,
            linear_x=lv.x if linear_valid else None,
            linear_y=lv.y if linear_valid else None,
            linear_z=lv.z if linear_valid else None,
            angular_x=av.x if angular_valid else None,
            angular_y=av.y if angular_valid else None,
            angular_z=av.z if angular_valid else None,
        )

    def _drain_esp32(self) -> None:
        if self.esp32_receiver is None:
            return
        for message in self.esp32_receiver.drain():
            message.xr_receive_timestamp_ns = self.qpc_ticks_to_xr_time(
                message.pc_receive_qpc_ticks
            )
            self.esp32_messages.append(message)

    def _refresh_interaction_profiles(self) -> None:
        for tracker in self.trackers:
            try:
                state = xr.get_current_interaction_profile(
                    self.session, tracker.role_xr_path
                )
                if state.interaction_profile:
                    tracker.interaction_profile = xr.path_to_string(
                        self.instance, state.interaction_profile
                    )
            except Exception:
                pass

    def _capture_sample(self, writer, frame_index: int, timestamp_xr_ns: int) -> None:
        xr.sync_actions(
            self.session,
            xr.ActionsSyncInfo(
                active_action_sets=[xr.ActiveActionSet(self.action_set, xr.NULL_PATH)]
            ),
        )
        self._drain_esp32()
        self._refresh_interaction_profiles()

        valid_standard_poses: dict[str, tuple[TrackerDescriptor, LocatedPose]] = {}
        standard_trigger_edges: list[tuple[TrackerDescriptor, bool, int]] = []
        frame_rows: list[dict] = []
        for tracker in self.trackers:
            pose_state = xr.get_action_state_pose(
                self.session,
                xr.ActionStateGetInfo(
                    action=self.pose_action, subaction_path=tracker.role_xr_path
                ),
            )
            pose = self._locate(tracker.action_space, timestamp_xr_ns)
            host_receive_ns = time.perf_counter_ns()
            host_receive_system_ns = time.time_ns()
            latency_ns = self.xr_now() - timestamp_xr_ns
            row = {
                    "timestamp_xr_ns": timestamp_xr_ns,
                    "host_receive_monotonic_ns": host_receive_ns,
                    "host_receive_system_time_ns": host_receive_system_ns,
                    "latency_ns": latency_ns,
                    "frame_index": frame_index,
                    "device_id": tracker.device_id,
                    "serial_number": tracker.serial_number,
                    "tracker_role": tracker.tracker_role,
                    "interaction_profile": tracker.interaction_profile,
                    "pose_action_active": int(bool(pose_state.is_active)),
                    "tracking_valid": int(
                        bool(pose_state.is_active) and pose.tracking_valid
                    ),
                    "location_flags": pose.location_flags,
                    "velocity_flags": pose.velocity_flags,
                    "position_x": pose.position_x,
                    "position_y": pose.position_y,
                    "position_z": pose.position_z,
                    "rotation_quaternion_x": pose.rotation_x,
                    "rotation_quaternion_y": pose.rotation_y,
                    "rotation_quaternion_z": pose.rotation_z,
                    "rotation_quaternion_w": pose.rotation_w,
                    "linear_velocity_x": pose.linear_x,
                    "linear_velocity_y": pose.linear_y,
                    "linear_velocity_z": pose.linear_z,
                    "angular_velocity_x": pose.angular_x,
                    "angular_velocity_y": pose.angular_y,
                    "angular_velocity_z": pose.angular_z,
                }
            frame_rows.append(row)
            if writer is not None:
                writer.writerow(row)
            self.pose_row_count += 1
            if bool(pose_state.is_active) and pose.tracking_valid:
                valid_standard_poses[tracker.serial_number] = (tracker, pose)

            trigger = xr.get_action_state_boolean(
                self.session,
                xr.ActionStateGetInfo(
                    action=self.trigger_action,
                    subaction_path=tracker.role_xr_path,
                ),
            )
            key = tracker.serial_number
            self.trigger_total_samples[key] = self.trigger_total_samples.get(key, 0) + 1
            if trigger.is_active:
                self.trigger_active_samples[key] = self.trigger_active_samples.get(key, 0) + 1
            if trigger.is_active and trigger.current_state:
                self.trigger_true_samples[key] = self.trigger_true_samples.get(key, 0) + 1
            first_active_sample = trigger.is_active and key not in self.initialized_trigger_keys
            if first_active_sample:
                self.initialized_trigger_keys.add(key)
                self.trigger_last_state[key] = bool(trigger.current_state)
                self.trigger_last_change_time[key] = int(trigger.last_change_time)
            # Some HTC layers update lastChangeTime while an inactive boolean remains
            # false. A real digital event therefore requires a currentState edge.
            observed_change = (
                trigger.is_active
                and not first_active_sample
                and bool(trigger.current_state) != self.trigger_last_state.get(key)
            )
            if trigger.is_active:
                self.trigger_last_state[key] = bool(trigger.current_state)
                self.trigger_last_change_time[key] = int(trigger.last_change_time)
            if not observed_change or trigger.last_change_time <= 0:
                continue
            event_time = int(trigger.last_change_time)
            standard_trigger_edges.append(
                (tracker, bool(trigger.current_state), event_time)
            )
            # The HTC Ultimate profile is the canonical Pogo event source. The
            # standard profile edge is retained only to identify slot -> serial.
            if self.ultimate_slots:
                continue
            event_pose = self._locate(tracker.action_space, event_time)
            event_host_ns = time.perf_counter_ns()
            event_host_system_ns = time.time_ns()
            event_latency_ns = self.xr_now() - event_time
            row = {
                "trigger_timestamp_xr_ns": event_time,
                "pose_timestamp_xr_ns": event_time,
                "host_receive_monotonic_ns": event_host_ns,
                "host_receive_system_time_ns": event_host_system_ns,
                "latency_ns": event_latency_ns,
                "device_id": tracker.device_id,
                "serial_number": tracker.serial_number,
                "tracker_role": tracker.tracker_role,
                "interaction_profile": tracker.interaction_profile,
                "trigger_state": int(bool(trigger.current_state)),
                "trigger_changed": int(bool(trigger.changed_since_last_sync)),
                "trigger_action_active": int(bool(trigger.is_active)),
                "sync_id": "",
                "sync_match_delta_ns": "",
                "tracking_valid": int(event_pose.tracking_valid),
                "location_flags": event_pose.location_flags,
                "position_x": event_pose.position_x,
                "position_y": event_pose.position_y,
                "position_z": event_pose.position_z,
                "rotation_quaternion_x": event_pose.rotation_x,
                "rotation_quaternion_y": event_pose.rotation_y,
                "rotation_quaternion_z": event_pose.rotation_z,
                "rotation_quaternion_w": event_pose.rotation_w,
            }
            self.trigger_rows.append(row)
            if self.config.trigger_event_callback is not None:
                self.config.trigger_event_callback(dict(row))
            self._emit_all_tracker_poses_at_trigger(event_time, row)
            event_name = "pressed" if trigger.current_state else "released"
            print(
                f"Trigger {event_name}: role={tracker.tracker_role} "
                f"serial={tracker.serial_number} lastChangeTime={event_time} "
                f"pose=({event_pose.position_x:+.4f}, {event_pose.position_y:+.4f}, "
                f"{event_pose.position_z:+.4f}) valid={event_pose.tracking_valid}"
            )

        if self.config.pose_frame_callback is not None:
            self.config.pose_frame_callback([dict(row) for row in frame_rows])
        self._capture_ultimate_triggers(
            timestamp_xr_ns, valid_standard_poses, standard_trigger_edges
        )

    def _capture_ultimate_triggers(
        self,
        timestamp_xr_ns: int,
        valid_standard_poses: dict[str, tuple[TrackerDescriptor, LocatedPose]],
        standard_trigger_edges: list[tuple[TrackerDescriptor, bool, int]],
    ) -> None:
        if not self.ultimate_slots:
            return
        for slot in self.ultimate_slots:
            pose_state = xr.get_action_state_pose(
                self.session,
                xr.ActionStateGetInfo(self.ultimate_pose_action, slot.xr_path),
            )
            slot_pose = self._locate(slot.action_space, timestamp_xr_ns)
            if pose_state.is_active and slot_pose.tracking_valid and valid_standard_poses:
                nearest = min(
                    valid_standard_poses.values(),
                    key=lambda item: _position_distance_squared(slot_pose, item[1]),
                )
                distance_squared = _position_distance_squared(slot_pose, nearest[1])
                # Profiles for the same physical Tracker should be nearly coincident.
                if distance_squared <= 0.01:
                    slot.mapped_serial_number = nearest[0].serial_number

            trigger = xr.get_action_state_boolean(
                self.session,
                xr.ActionStateGetInfo(self.ultimate_trigger_action, slot.xr_path),
            )
            key = f"ultimate_slot_{slot.slot_index}"
            self.trigger_total_samples[key] = self.trigger_total_samples.get(key, 0) + 1
            if trigger.is_active:
                self.trigger_active_samples[key] = self.trigger_active_samples.get(key, 0) + 1
            if trigger.is_active and trigger.current_state:
                self.trigger_true_samples[key] = self.trigger_true_samples.get(key, 0) + 1
            first_active_sample = trigger.is_active and key not in self.initialized_trigger_keys
            if first_active_sample:
                self.initialized_trigger_keys.add(key)
                self.trigger_last_state[key] = bool(trigger.current_state)
                self.trigger_last_change_time[key] = int(trigger.last_change_time)
            observed_change = (
                trigger.is_active
                and not first_active_sample
                and bool(trigger.current_state) != self.trigger_last_state.get(key)
            )
            if trigger.is_active:
                self.trigger_last_state[key] = bool(trigger.current_state)
                self.trigger_last_change_time[key] = int(trigger.last_change_time)
            if not observed_change or trigger.last_change_time <= 0:
                continue

            event_time = int(trigger.last_change_time)
            matching_edges = [
                edge
                for edge in standard_trigger_edges
                if edge[1] == bool(trigger.current_state)
                and abs(edge[2] - event_time) <= 30_000_000
            ]
            if matching_edges:
                closest = min(matching_edges, key=lambda edge: abs(edge[2] - event_time))
                slot.mapped_serial_number = closest[0].serial_number
            tracker = next(
                (
                    item
                    for item in self.trackers
                    if item.serial_number == slot.mapped_serial_number
                ),
                None,
            )
            event_pose = self._locate(slot.action_space, event_time)
            event_host_ns = time.perf_counter_ns()
            event_host_system_ns = time.time_ns()
            row = {
                "trigger_timestamp_xr_ns": event_time,
                "pose_timestamp_xr_ns": event_time,
                "host_receive_monotonic_ns": event_host_ns,
                "host_receive_system_time_ns": event_host_system_ns,
                "latency_ns": self.xr_now() - event_time,
                "device_id": tracker.device_id if tracker else slot.user_path,
                "serial_number": tracker.serial_number if tracker else "",
                "tracker_role": tracker.tracker_role if tracker else f"ultimate_tracker_{slot.slot_index}",
                "interaction_profile": ULTIMATE_INTERACTION_PROFILE,
                "trigger_state": int(bool(trigger.current_state)),
                "trigger_changed": int(bool(trigger.changed_since_last_sync)),
                "trigger_action_active": 1,
                "sync_id": "",
                "sync_match_delta_ns": "",
                "tracking_valid": int(event_pose.tracking_valid),
                "location_flags": event_pose.location_flags,
                "position_x": event_pose.position_x,
                "position_y": event_pose.position_y,
                "position_z": event_pose.position_z,
                "rotation_quaternion_x": event_pose.rotation_x,
                "rotation_quaternion_y": event_pose.rotation_y,
                "rotation_quaternion_z": event_pose.rotation_z,
                "rotation_quaternion_w": event_pose.rotation_w,
            }
            self.trigger_rows.append(row)
            if self.config.trigger_event_callback is not None:
                self.config.trigger_event_callback(dict(row))
            self._emit_all_tracker_poses_at_trigger(event_time, row)
            event_name = "pressed" if trigger.current_state else "released"
            print(
                f"Trigger {event_name}: ultimate_slot={slot.slot_index} "
                f"serial={row['serial_number'] or 'unmapped'} "
                f"lastChangeTime={event_time} pose=({event_pose.position_x:+.4f}, "
                f"{event_pose.position_y:+.4f}, {event_pose.position_z:+.4f}) "
                f"valid={event_pose.tracking_valid}"
            )

    def run(self) -> dict:
        if self.config.write_output_files:
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
        pose_path = self.config.output_dir / "tracker_poses.csv"
        trigger_path = self.config.output_dir / "trigger_events.csv"
        sync_path = self.config.output_dir / "esp32_sync_events.csv"
        metadata_path = self.config.output_dir / "metadata.json"

        print(f"OpenXR Runtime: {self.runtime_name} {self.runtime_version}")
        print("Tracker paths:")
        for tracker in self.trackers:
            print(
                f"  serial={tracker.serial_number} role={tracker.tracker_role} "
                f"device_id={tracker.device_id} profile={tracker.interaction_profile}"
            )
        if self.esp32_receiver:
            print(
                f"ESP32 UDP: {self.config.esp32_bind_host}:"
                f"{self.config.esp32_udp_port}"
            )
        print(
            "Ultimate Tracker Pogo profile: "
            + ("enabled (5 slots)" if self.ultimate_slots else "extension unavailable")
        )

        period_xr_ns = round(1_000_000_000 / self.config.rate_hz)
        deadline_host = (
            None
            if self.config.duration_s is None
            else time.perf_counter() + self.config.duration_s
        )
        next_sample_xr = None
        next_console_host = 0.0

        pose_handle = None
        writer = None
        try:
            if self.config.write_output_files:
                pose_handle = pose_path.open("w", newline="", encoding="utf-8")
                writer = csv.DictWriter(pose_handle, fieldnames=POSE_FIELDS)
                writer.writeheader()
            while (
                (deadline_host is None or time.perf_counter() < deadline_host)
                and not self._external_stop_requested()
            ):
                self._poll_events()
                if not self.session_running:
                    time.sleep(0.01)
                    continue

                runtime_now = self.xr_now()
                if next_sample_xr is None:
                    next_sample_xr = runtime_now
                if runtime_now < next_sample_xr:
                    time.sleep(min((next_sample_xr - runtime_now) / 1e9, 0.002))
                    continue

                frame_state = xr.wait_frame(self.session)
                xr.begin_frame(self.session)
                try:
                    self._capture_sample(writer, self.frame_count, next_sample_xr)
                finally:
                    xr.end_frame(
                        self.session,
                        xr.FrameEndInfo(
                            display_time=frame_state.predicted_display_time,
                            environment_blend_mode=xr.EnvironmentBlendMode.OPAQUE,
                            layers=[],
                        ),
                    )
                self.frame_count += 1
                next_sample_xr += period_xr_ns

                # Do not request history older than the guaranteed 50 ms window.
                lag = self.xr_now() - next_sample_xr
                if lag > 40_000_000:
                    skipped = math.ceil((lag - 40_000_000) / period_xr_ns)
                    next_sample_xr += skipped * period_xr_ns

                if time.perf_counter() >= next_console_host:
                    next_console_host = (
                        time.perf_counter() + self.config.console_pose_interval_s
                    )
                    active = sum(
                        bool(
                            xr.get_action_state_pose(
                                self.session,
                                xr.ActionStateGetInfo(
                                    self.pose_action, tracker.role_xr_path
                                ),
                            ).is_active
                        )
                        for tracker in self.trackers
                    )
                    print(
                        f"frame={self.frame_count} timestamp_xr_ns={next_sample_xr - period_xr_ns} "
                        f"active_trackers={active}/{len(self.trackers)}"
                    )
        finally:
            if pose_handle is not None:
                pose_handle.flush()
                pose_handle.close()

        self._drain_esp32()
        self._match_sync_events()
        sync_rows = [
                {
                    "sync_id": item.sync_id,
                    "esp32_timestamp_us": item.esp32_timestamp_us,
                    "esp32_source_timestamp_us": item.esp32_source_timestamp_us,
                    "esp32_gpio_timestamp_us": item.esp32_gpio_timestamp_us,
                    "pc_receive_qpc_ticks": item.pc_receive_qpc_ticks,
                    "pc_receive_monotonic_ns": item.pc_receive_monotonic_ns,
                    "xr_receive_timestamp_ns": item.xr_receive_timestamp_ns,
                    "matched_trigger_timestamp_xr_ns": item.matched_trigger_timestamp_xr_ns,
                    "match_delta_ns": item.match_delta_ns,
                    "sender_address": item.sender_address,
                    "raw_message": item.raw_message,
                }
                for item in self.esp32_messages
            ]
        if self.config.write_output_files:
            _write_rows(trigger_path, TRIGGER_FIELDS, self.trigger_rows)
            _write_rows(sync_path, ESP32_SYNC_FIELDS, sync_rows)
        metadata = {
            "format_version": 1,
            "runtime_name": self.runtime_name,
            "runtime_version": self.runtime_version,
            "time_semantics": {
                "timestamp_xr_ns": (
                    "XrTime requested from xrLocateSpace; Runtime-level location time, "
                    "not Tracker hardware sample time"
                ),
                "trigger_timestamp_xr_ns": "XrActionStateBoolean.lastChangeTime",
                "host_receive_monotonic_ns": (
                    "Python time.perf_counter_ns after receiving the API result"
                ),
                "host_receive_system_time_ns": (
                    "Windows wall-clock time.time_ns after receiving the API result; "
                    "secondary label only, not the Tracker event/pose timestamp"
                ),
                "latency_ns": (
                    "XrTime converted from post-call QPC minus requested/event XrTime"
                ),
            },
            "interaction_profile": TRACKER_INTERACTION_PROFILE,
            "interaction_profiles": {
                "pose_and_identity": TRACKER_INTERACTION_PROFILE,
                "canonical_pogo_trigger": (
                    ULTIMATE_INTERACTION_PROFILE if self.ultimate_slots else TRACKER_INTERACTION_PROFILE
                ),
            },
            "trackers": [
                {
                    "device_id": tracker.device_id,
                    "serial_number": tracker.serial_number,
                    "tracker_role": tracker.tracker_role,
                    "role_path": tracker.role_path,
                    "persistent_path": tracker.persistent_path,
                    "interaction_profile": tracker.interaction_profile,
                    "discovery_source": tracker.discovery_source,
                }
                for tracker in self.trackers
            ],
            "frame_count": self.frame_count,
            "pose_row_count": self.pose_row_count,
            "trigger_event_count": len(self.trigger_rows),
            "esp32_sync_event_count": len(self.esp32_messages),
            "esp32_invalid_message_count": (
                self.esp32_receiver.invalid_messages if self.esp32_receiver else 0
            ),
            "trigger_interface_diagnostics": {
                key: {
                    "active_samples": self.trigger_active_samples.get(key, 0),
                    "total_samples": total,
                    "state_true_samples": self.trigger_true_samples.get(key, 0),
                    "last_change_time_xr_ns": self.trigger_last_change_time.get(key),
                }
                for key, total in self.trigger_total_samples.items()
            },
        }
        if self.config.write_output_files:
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"Saved OpenXR capture to {self.config.output_dir}")
        print("Trigger Action diagnostics:")
        for key, total in sorted(self.trigger_total_samples.items()):
            print(
                f"  {key}: active_samples={self.trigger_active_samples.get(key, 0)}/"
                f"{total} state_true_samples={self.trigger_true_samples.get(key, 0)} "
                f"lastChangeTime={self.trigger_last_change_time.get(key)}"
            )
        return metadata

    def _external_stop_requested(self) -> bool:
        event = self.config.external_stop_event
        return bool(event is not None and event.is_set())

    def _match_sync_events(self) -> None:
        window_ns = round(self.config.sync_match_window_ms * 1_000_000)
        unmatched = set(range(len(self.esp32_messages)))
        for trigger in self.trigger_rows:
            if not unmatched:
                continue
            event_time = int(trigger["trigger_timestamp_xr_ns"])
            candidates = [
                (abs(self.esp32_messages[index].xr_receive_timestamp_ns - event_time), index)
                for index in unmatched
                if self.esp32_messages[index].xr_receive_timestamp_ns is not None
            ]
            if not candidates:
                continue
            distance, index = min(candidates)
            if distance > window_ns:
                continue
            message = self.esp32_messages[index]
            delta = message.xr_receive_timestamp_ns - event_time
            trigger["sync_id"] = message.sync_id
            trigger["sync_match_delta_ns"] = delta
            message.matched_trigger_timestamp_xr_ns = event_time
            message.match_delta_ns = delta
            unmatched.remove(index)

    def close(self) -> None:
        if self.esp32_receiver is not None:
            self.esp32_receiver.close()
            self.esp32_receiver = None
        for tracker in self.trackers:
            if tracker.action_space is not None:
                try:
                    xr.destroy_space(tracker.action_space)
                except Exception:
                    pass
                tracker.action_space = None
        for slot in self.ultimate_slots:
            if slot.action_space is not None:
                try:
                    xr.destroy_space(slot.action_space)
                except Exception:
                    pass
                slot.action_space = None
        if self.base_space is not None:
            try:
                xr.destroy_space(self.base_space)
            except Exception:
                pass
            self.base_space = None
        if self.action_set is not None:
            try:
                xr.destroy_action_set(self.action_set)
            except Exception:
                pass
            self.action_set = None
        if self.session is not None:
            try:
                xr.destroy_session(self.session)
            except Exception:
                pass
            self.session = None
        if self.instance is not None:
            try:
                xr.destroy_instance(self.instance)
            except Exception:
                pass
            self.instance = None


def run_capture(config: CaptureConfig) -> dict:
    capture = OpenXRTrackerCapture(config)
    try:
        capture.initialize()
        return capture.run()
    finally:
        capture.close()


def _decode_c_string(value) -> str:
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    try:
        return bytes(value).split(b"\0", 1)[0].decode("utf-8", errors="replace")
    except TypeError:
        return str(value)


def _serial_from_persistent_path(path: str) -> str:
    marker = "vive_tracker"
    position = path.lower().rfind(marker)
    if position < 0:
        return path
    return path[position + len(marker) :].upper()


def _write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _position_distance_squared(left: LocatedPose, right: LocatedPose) -> float:
    return (
        (left.position_x - right.position_x) ** 2
        + (left.position_y - right.position_y) ** 2
        + (left.position_z - right.position_z) ** 2
    )
