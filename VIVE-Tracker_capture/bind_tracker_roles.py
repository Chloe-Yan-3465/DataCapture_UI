# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#   "pyopenxr==1.1.5301",
#   "openvr>=2.12",
# ]
# ///

"""Export SteamVR OpenXR Tracker role assignments to tracker_roles.json."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from openxr_tracker_capture import CaptureConfig, OpenXRTrackerCapture


def enumerate_openvr_roles() -> tuple[dict[str, str], dict]:
    """Read every explicit Tracker role currently assigned by SteamVR."""

    import openvr

    role_to_serial: dict[str, str] = {}
    devices: dict[str, dict] = {}
    system = openvr.init(openvr.VRApplication_Background)
    try:
        accepted_classes = {
            openvr.TrackedDeviceClass_GenericTracker,
            openvr.TrackedDeviceClass_Controller,
        }
        for device_index in range(openvr.k_unMaxTrackedDeviceCount):
            device_class = system.getTrackedDeviceClass(device_index)
            if device_class not in accepted_classes:
                continue
            try:
                serial = system.getStringTrackedDeviceProperty(
                    device_index, openvr.Prop_SerialNumber_String
                )
                controller_type = system.getStringTrackedDeviceProperty(
                    device_index, openvr.Prop_ControllerType_String
                )
                model = system.getStringTrackedDeviceProperty(
                    device_index, openvr.Prop_ModelNumber_String
                )
            except Exception:
                continue
            prefix = "vive_tracker_"
            role = (
                controller_type[len(prefix) :]
                if controller_type.startswith(prefix)
                else ""
            )
            if not role:
                continue
            if role in role_to_serial:
                raise RuntimeError(
                    f"SteamVR中有多台Tracker使用同一角色 {role}："
                    f"{role_to_serial[role]}、{serial}"
                )
            role_to_serial[role] = serial
            devices[serial] = {
                "device_index_openvr": device_index,
                "device_class_openvr": int(device_class),
                "controller_type": controller_type,
                "model": model,
            }
    finally:
        openvr.shutdown()
    return role_to_serial, devices


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tracker_roles.json"))
    args = parser.parse_args()
    capture = OpenXRTrackerCapture(
        CaptureConfig(
            output_dir=Path("captures"),
            duration_s=0.1,
            role_map_path=None,
            write_output_files=False,
        )
    )
    trackers = []
    openxr_error = None
    try:
        capture.initialize()
        trackers = capture.trackers
    except Exception as exc:
        openxr_error = exc
    finally:
        capture.close()

    openxr_role_to_serial = {
        tracker.tracker_role: tracker.serial_number for tracker in trackers
    }
    openxr_devices = {
        tracker.serial_number: {
            "device_id": tracker.device_id,
            "role_path": tracker.role_path,
            "interaction_profile": tracker.interaction_profile,
        }
        for tracker in trackers
    }

    try:
        openvr_role_to_serial, openvr_devices = enumerate_openvr_roles()
        openvr_error = None
    except Exception as exc:
        openvr_role_to_serial = {}
        openvr_devices = {}
        openvr_error = exc

    # OpenVR exposes SteamVR's explicit role configuration directly, so prefer it
    # whenever it found assigned Trackers. OpenXR remains a fallback for runtimes
    # where OpenVR role enumeration is unavailable.
    if openvr_role_to_serial:
        role_to_serial = openvr_role_to_serial
        devices = openvr_devices
        source = "OpenVR Prop_ControllerType_String"
        if openxr_role_to_serial and openxr_role_to_serial != role_to_serial:
            print(
                "OpenXR与SteamVR/OpenVR枚举结果不同；"
                "将按SteamVR当前显式分配的全部角色导出。"
            )
    elif openxr_role_to_serial:
        role_to_serial = openxr_role_to_serial
        devices = openxr_devices
        source = "XR_HTCX_vive_tracker_interaction persistent_path + role_path"
        if openvr_error is not None:
            print(f"OpenVR角色读取失败，使用OpenXR结果：{openvr_error}")
    else:
        diagnostics = []
        if openxr_error is not None:
            diagnostics.append(f"OpenXR：{openxr_error}")
        if openvr_error is not None:
            diagnostics.append(f"OpenVR：{openvr_error}")
        detail = "；".join(diagnostics)
        if detail:
            detail = f"（{detail}）"
        raise SystemExit(
            "未发现已在SteamVR中分配角色的Tracker。"
            "请确认Tracker已连接、可定位并已设置角色。"
            f"{detail}"
        )

    payload = {
        "format_version": 2,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "role_to_serial": role_to_serial,
        "serial_to_role": {
            serial: role for role, serial in role_to_serial.items()
        },
        "devices": devices,
    }
    output = args.output.resolve()
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出Tracker角色（来源：{source}）：{output}")
    for role, serial in sorted(role_to_serial.items()):
        print(f"  {role}: {serial}")


if __name__ == "__main__":
    main()
