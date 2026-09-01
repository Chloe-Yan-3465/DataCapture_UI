我现在可以将manus core读取到的manus手套的数据通过官方的SDK和client传递给Python，后续计划在manus手套腕部加入vive tracker，获取手部整体平移或旋转的位姿信息。我应该怎么预留接口？我应该怎么使用vive tracker？之前是在Linux上用一个现成的无头显的代码，但是我不确定现在全部转在Windows上会出现什么问题。

我来帮你梳理这个方案。让我先了解一下你的具体情况：我没有收到你的选择——可能是UI小问题。没关系，我直接根据你描述的情况给你一个完整的方案。

---

## 整体架构设计

你的数据来源有两路：**Manus Core SDK（手指关节数据）** + **Vive Tracker（腕部6DoF位姿）**，最终汇聚到Python pipeline。

### 推荐的数据结构设计

```python
# hand_data.py - 核心数据结构
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import time

@dataclass
class Pose6DoF:
    """腕部全局位姿，来自 Vive Tracker"""
    position: np.ndarray        # [x, y, z] 单位: 米
    rotation: np.ndarray        # quaternion [x, y, z, w]
    timestamp: float = field(default_factory=time.time)
    is_valid: bool = False      # tracker 是否在追踪

@dataclass
class FingerJointData:
    """Manus Core 提供的手指关节数据"""
    joint_angles: np.ndarray    # 根据你SDK返回的格式定义
    timestamp: float = field(default_factory=time.time)
    is_valid: bool = False

@dataclass
class HandFrame:
    """一帧完整的手部数据，两路数据合并后的输出"""
    hand_id: str                            # "left" / "right"
    finger_data: Optional[FingerJointData] = None
    wrist_pose: Optional[Pose6DoF] = None   # 预留接口，可为 None
    fused_timestamp: float = field(default_factory=time.time)

    @property
    def has_full_data(self) -> bool:
        """是否同时具有手套+Tracker数据"""
        return (self.finger_data is not None and self.finger_data.is_valid and
                self.wrist_pose is not None and self.wrist_pose.is_valid)
```

---

### Vive Tracker 接入层（接口预留）

```python
# tracker_provider.py
from abc import ABC, abstractmethod
from hand_data import Pose6DoF
import numpy as np

class TrackerProvider(ABC):
    """抽象基类，方便后续替换 Tracker 品牌/驱动方案"""
    
    @abstractmethod
    def start(self): ...
    
    @abstractmethod
    def stop(self): ...
    
    @abstractmethod
    def get_pose(self, tracker_serial: str) -> Pose6DoF: ...
    
    @abstractmethod
    def list_trackers(self) -> list[str]: ...


class OpenVRTrackerProvider(TrackerProvider):
    """
    通过 openvr (Python bindings) 读取 Vive Tracker。
    Windows 和 Linux 均支持，但行为有差异（见下方注意事项）。
    """
    
    def __init__(self):
        self._vr = None
        self._tracker_map: dict[str, int] = {}  # serial -> device_index
    
    def start(self):
        import openvr
        self._vr = openvr.init(openvr.VRApplication_Other)
        # VRApplication_Other = 不需要头显，是 headless 的关键参数
        self._refresh_tracker_map()
    
    def stop(self):
        import openvr
        if self._vr:
            openvr.shutdown()
    
    def _refresh_tracker_map(self):
        import openvr
        for i in range(openvr.k_unMaxTrackedDeviceCount):
            device_class = self._vr.getTrackedDeviceClass(i)
            if device_class == openvr.TrackedDeviceClass_GenericTracker:
                serial = self._vr.getStringTrackedDeviceProperty(
                    i, openvr.Prop_SerialNumber_String)
                self._tracker_map[serial] = i
    
    def list_trackers(self) -> list[str]:
        self._refresh_tracker_map()
        return list(self._tracker_map.keys())
    
    def get_pose(self, tracker_serial: str) -> Pose6DoF:
        import openvr
        idx = self._tracker_map.get(tracker_serial)
        if idx is None:
            return Pose6DoF(np.zeros(3), np.array([0,0,0,1]), is_valid=False)
        
        poses, _ = self._vr.getDeviceToAbsoluteTrackingPose(
            openvr.TrackingUniverseStanding, 0,
            openvr.k_unMaxTrackedDeviceCount)
        
        pose = poses[idx]
        if not pose.bPoseIsValid:
            return Pose6DoF(np.zeros(3), np.array([0,0,0,1]), is_valid=False)
        
        m = pose.mDeviceToAbsoluteTracking  # 3x4 矩阵
        position = np.array([m[0][3], m[1][3], m[2][3]])
        rotation = self._matrix_to_quaternion(m)
        return Pose6DoF(position, rotation, is_valid=True)
    
    def _matrix_to_quaternion(self, m) -> np.ndarray:
        """OpenVR 3x4 旋转矩阵 -> quaternion [x,y,z,w]"""
        r = np.array([[m[0][0], m[0][1], m[0][2]],
                      [m[1][0], m[1][1], m[1][2]],
                      [m[2][0], m[2][1], m[2][2]]])
        trace = r[0,0] + r[1,1] + r[2,2]
        if trace > 0:
            s = 0.5 / np.sqrt(trace + 1.0)
            w = 0.25 / s
            x = (r[2,1] - r[1,2]) * s
            y = (r[0,2] - r[2,0]) * s
            z = (r[1,0] - r[0,1]) * s
        else:
            # 其余分支省略，实际建议用 scipy.spatial.transform.Rotation
            x, y, z, w = 0, 0, 0, 1
        return np.array([x, y, z, w])


class DummyTrackerProvider(TrackerProvider):
    """
    占位实现：Tracker 还没到货时，pipeline 照样能跑。
    返回固定位姿，is_valid=False。
    """
    def start(self): pass
    def stop(self): pass
    def list_trackers(self): return []
    def get_pose(self, serial): 
        import numpy as np
        return Pose6DoF(np.zeros(3), np.array([0,0,0,1]), is_valid=False)
```

---

### 数据融合主循环

```python
# pipeline.py
import threading
import time
from hand_data import HandFrame, FingerJointData, Pose6DoF
from tracker_provider import OpenVRTrackerProvider, DummyTrackerProvider

class HandDataPipeline:
    
    def __init__(self, manus_client, tracker_serial: str = None,
                 use_tracker: bool = True):
        self.manus_client = manus_client
        self.tracker_serial = tracker_serial
        
        # 根据是否要用 Tracker 选择 Provider
        if use_tracker:
            self.tracker = OpenVRTrackerProvider()
        else:
            self.tracker = DummyTrackerProvider()
        
        self._running = False
        self._callbacks = []     # 注册下游消费者
    
    def register_callback(self, fn):
        """下游模块（机器人控制/录制/识别）注册回调"""
        self._callbacks.append(fn)
    
    def start(self):
        self.tracker.start()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        self._running = False
        self.tracker.stop()
    
    def _loop(self):
        while self._running:
            # --- 1. 读 Manus ---
            raw = self.manus_client.get_latest_frame()  # 你现有的调用
            finger_data = FingerJointData(
                joint_angles=raw.joint_angles,
                timestamp=raw.timestamp,
                is_valid=raw.is_valid
            )
            
            # --- 2. 读 Tracker（如果有）---
            wrist_pose = None
            if self.tracker_serial:
                wrist_pose = self.tracker.get_pose(self.tracker_serial)
            
            # --- 3. 组装 HandFrame ---
            frame = HandFrame(
                hand_id="right",
                finger_data=finger_data,
                wrist_pose=wrist_pose,
            )
            
            # --- 4. 分发 ---
            for cb in self._callbacks:
                cb(frame)
            
            time.sleep(0.005)  # ~200Hz，按需调整
```

---

## Windows vs Linux 的关键差异

这是最容易踩坑的部分，逐条列出：

| 问题 | Linux 现状 | Windows 注意事项 |
|---|---|---|
| **SteamVR 启动** | 无头显需要 `vr_headless` 参数 + `--vrsystem` | 同样需要 headless 模式，但 SteamVR 在 Windows 下默认会弹 UI，需在 SteamVR 设置里关闭"显示桌面游戏剧院" |
| **openvr Python 包** | `pip install openvr` | **同一个包，但需要 `openvr.dll` 在 PATH 里**，通常装完 SteamVR 后自动有 |
| **无头显 headless 启动** | `VRApplication_Other` 模式 | Windows 下 `VRApplication_Other` **同样有效**，但 SteamVR 有时会要求手动点一次 "OK" |
| **Tracker 识别** | `/dev/hidraw*` 权限问题 | Windows 下 USB 驱动由 SteamVR 管理，无需额外配置 |
| **时间戳同步** | `clock_gettime` 精度高 | Windows 用 `time.perf_counter()`，不要用 `time.time()` |
| **线程优先级** | `nice` / `chrt` | Windows 用 `SetThreadPriority`，Python 里用 `os.nice()` 无效，需要 `ctypes` |

### Headless 模式在 Windows 的正确启动方式

```python
# 在调用 openvr.init 之前设置环境变量
import os
os.environ["VR_OVERRIDE_STEAMVR_RUNTIME"] = "1"  # 某些版本需要

import openvr
# 关键：VRApplication_Other 跳过头显检测
vr = openvr.init(openvr.VRApplication_Other)
```

如果 SteamVR 仍然报错找不到头显，在 SteamVR 的 `steamvr.vrsettings` 文件里加：
```json
"steamvr" : {
   "requireHmd" : false,
   "forcedDriver" : "null"
}
```

---

## 推荐的依赖安装

```bash
pip install openvr numpy scipy

# 时间戳对齐建议用 scipy rotation
# from scipy.spatial.transform import Rotation as R
# r = R.from_matrix(rot_matrix)
# quat = r.as_quat()  # [x,y,z,w]
```

---

## 总结建议

1. **现在就用 `DummyTrackerProvider`** 把接口占好，Pipeline 和下游代码完全不用等 Tracker 到货
2. **`TrackerProvider` 抽象层** 让你以后换 Tracker 品牌（比如 Rokoko、OptiTrack）只改一个类
3. **Windows headless 最大坑**：SteamVR 弹窗拦截，用 `VRApplication_Other` + `steamvr.vrsettings` 里 `requireHmd: false` 基本可解决
4. **时间戳同步**：Manus 和 Tracker 各有自己的时钟，如果下游对时序敏感（比如机器人控制），需要做简单的时间戳对齐，`perf_counter` 是 Windows 上最高精度的选择

有具体某一块想深入的话告诉我，比如时间戳融合算法、Tracker 坐标系标定，或者 SteamVR 的具体配置文件位置。



Q: 你目前的开发环境是？
A: 果然是UI问题，这个界面出来的很晚。我大概率会在win上，因为如果同时用win和Linux，可能涉及到跨电脑通讯，调试起来可能有问题。

Q: Vive Tracker的使用方式？
A: 只用vive tracker追踪腕部信息，我不清楚manus手套自己可不可以获取腕部位姿

Q: 你的Python数据pipeline最终目标是？ (Select all that apply)
A: 动作捕捉和录制，进行后续数据分析，用于人因工程和认知实验数据



好的，信息很清晰了。先回答你的一个关键问题：

**Manus 手套本身能不能提供腕部位姿？**
不能。Manus Core SDK 提供的是手指关节角度（相对角度），腕部的全局6DoF位姿需要外部追踪设备。所以加 Vive Tracker 的方向是正确的。

---

## 针对你场景的完整方案

### 你的数据特点重新梳理

- **目标**：动作捕捉+录制，用于人因工程/认知实验
- **意味着**：对**时间戳精度**要求高，需要**事后数据分析**友好的格式，要有**实验元数据**管理

---

### 推荐目录结构

```
hand_capture/
├── core/
│   ├── data_types.py        # 数据结构定义
│   ├── manus_reader.py      # 你现有的 Manus SDK 封装
│   ├── tracker_provider.py  # Vive Tracker 接口
│   └── pipeline.py          # 融合主循环
├── recording/
│   ├── recorder.py          # 录制逻辑
│   └── session_meta.py      # 实验元数据
├── data/
│   └── sessions/            # 录制输出
└── main.py
```

---

### 第一层：数据结构（面向实验分析）

```python
# core/data_types.py
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import time

@dataclass
class Pose6DoF:
    position: np.ndarray        # [x, y, z]，单位：米
    quaternion: np.ndarray      # [x, y, z, w]
    timestamp_pc: float         # time.perf_counter()，用于同步
    timestamp_unix: float       # time.time()，用于与实验系统对齐
    is_valid: bool = False

@dataclass
class ManusFingerData:
    """
    直接映射 Manus Core SDK 的输出，字段名和你的 SDK 保持一致。
    joint_angles 的具体 shape 取决于你的 SDK 版本（通常是 20 或 25 个值）。
    """
    joint_angles: np.ndarray    # shape: (N_joints,)
    raw_ergonomics: Optional[np.ndarray] = None  # SDK 有时提供人因评分，预留
    timestamp_pc: float = field(default_factory=time.perf_counter)
    timestamp_unix: float = field(default_factory=time.time)
    is_valid: bool = False

@dataclass
class HandFrame:
    """一帧融合数据，是录制和分析的最小单元"""
    frame_id: int
    hand_side: str                          # "left" / "right"
    finger_data: Optional[ManusFingerData] = None
    wrist_pose: Optional[Pose6DoF] = None
    
    # 两路数据的时间差，用于事后质量评估
    sync_delta_ms: float = 0.0
    
    def to_dict(self) -> dict:
        """序列化，供录制模块使用"""
        return {
            "frame_id": self.frame_id,
            "hand_side": self.hand_side,
            "sync_delta_ms": self.sync_delta_ms,
            "finger": {
                "joint_angles": self.finger_data.joint_angles.tolist()
                    if self.finger_data else None,
                "timestamp_pc": self.finger_data.timestamp_pc
                    if self.finger_data else None,
                "is_valid": self.finger_data.is_valid
                    if self.finger_data else False,
            },
            "wrist": {
                "position": self.wrist_pose.position.tolist()
                    if self.wrist_pose else None,
                "quaternion": self.wrist_pose.quaternion.tolist()
                    if self.wrist_pose else None,
                "timestamp_pc": self.wrist_pose.timestamp_pc
                    if self.wrist_pose else None,
                "is_valid": self.wrist_pose.is_valid
                    if self.wrist_pose else False,
            }
        }
```

---

### 第二层：Tracker 接入（Windows headless 已处理）

```python
# core/tracker_provider.py
import os
import time
import numpy as np
from abc import ABC, abstractmethod
from data_types import Pose6DoF


class TrackerProvider(ABC):
    @abstractmethod
    def start(self): ...
    @abstractmethod
    def stop(self): ...
    @abstractmethod
    def get_pose(self, serial: str) -> Pose6DoF: ...
    @abstractmethod
    def list_trackers(self) -> list[str]: ...


class OpenVRTrackerProvider(TrackerProvider):
    """
    Windows headless 模式。
    前提：SteamVR 已启动，steamvr.vrsettings 中 requireHmd=false。
    """

    def __init__(self):
        self._vr = None
        self._tracker_map: dict[str, int] = {}

    def start(self):
        import openvr
        # VRApplication_Other 是 headless 关键，不检测头显
        self._vr = openvr.init(openvr.VRApplication_Other)
        self._refresh_tracker_map()
        print(f"[Tracker] 已连接，发现 Tracker: {self.list_trackers()}")

    def stop(self):
        import openvr
        if self._vr:
            openvr.shutdown()
            self._vr = None

    def _refresh_tracker_map(self):
        import openvr
        self._tracker_map.clear()
        for i in range(openvr.k_unMaxTrackedDeviceCount):
            cls = self._vr.getTrackedDeviceClass(i)
            if cls == openvr.TrackedDeviceClass_GenericTracker:
                serial = self._vr.getStringTrackedDeviceProperty(
                    i, openvr.Prop_SerialNumber_String)
                self._tracker_map[serial] = i

    def list_trackers(self) -> list[str]:
        self._refresh_tracker_map()
        return list(self._tracker_map.keys())

    def get_pose(self, serial: str) -> Pose6DoF:
        import openvr
        t_pc = time.perf_counter()
        t_unix = time.time()

        idx = self._tracker_map.get(serial)
        if idx is None:
            self._refresh_tracker_map()             # 可能刚连上
            idx = self._tracker_map.get(serial)
        if idx is None:
            return Pose6DoF(np.zeros(3), np.array([0,0,0,1]),
                            t_pc, t_unix, is_valid=False)

        poses, _ = self._vr.getDeviceToAbsoluteTrackingPose(
            openvr.TrackingUniverseStanding, 0,
            openvr.k_unMaxTrackedDeviceCount)

        p = poses[idx]
        if not p.bPoseIsValid:
            return Pose6DoF(np.zeros(3), np.array([0,0,0,1]),
                            t_pc, t_unix, is_valid=False)

        m = p.mDeviceToAbsoluteTracking
        position = np.array([m[0][3], m[1][3], m[2][3]])
        quat = self._rot_matrix_to_quat(m)
        return Pose6DoF(position, quat, t_pc, t_unix, is_valid=True)

    @staticmethod
    def _rot_matrix_to_quat(m) -> np.ndarray:
        from scipy.spatial.transform import Rotation
        rot = np.array([[m[0][0], m[0][1], m[0][2]],
                        [m[1][0], m[1][1], m[1][2]],
                        [m[2][0], m[2][1], m[2][2]]])
        return Rotation.from_matrix(rot).as_quat()  # [x,y,z,w]


class DummyTrackerProvider(TrackerProvider):
    """Tracker 未到货时的占位，Pipeline 照常运行"""
    def start(self): 
        print("[Tracker] 使用 Dummy Provider，位姿数据无效")
    def stop(self): pass
    def list_trackers(self): return []
    def get_pose(self, serial: str):
        return Pose6DoF(np.zeros(3), np.array([0,0,0,1]),
                        time.perf_counter(), time.time(), is_valid=False)
```

---

### 第三层：融合 Pipeline

```python
# core/pipeline.py
import threading
import time
from data_types import HandFrame, ManusFingerData
from tracker_provider import TrackerProvider

class HandDataPipeline:

    def __init__(self, manus_client, tracker: TrackerProvider,
                 tracker_serial: str = None, target_hz: float = 100.0):
        self.manus_client = manus_client
        self.tracker = tracker
        self.tracker_serial = tracker_serial
        self._interval = 1.0 / target_hz
        self._frame_id = 0
        self._running = False
        self._callbacks: list = []

    def register_callback(self, fn):
        """录制器、实时显示等模块在这里注册"""
        self._callbacks.append(fn)

    def start(self):
        self.tracker.start()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self.tracker.stop()

    def _loop(self):
        while self._running:
            t0 = time.perf_counter()

            # --- Manus ---
            raw = self.manus_client.get_latest_frame()
            finger = ManusFingerData(
                joint_angles=raw.joint_angles,
                timestamp_pc=time.perf_counter(),
                timestamp_unix=time.time(),
                is_valid=raw.is_valid
            )

            # --- Tracker ---
            wrist = None
            if self.tracker_serial:
                wrist = self.tracker.get_pose(self.tracker_serial)

            # --- 时间差计算（用于数据质量评估）---
            sync_delta = 0.0
            if wrist is not None:
                sync_delta = abs(finger.timestamp_pc - wrist.timestamp_pc) * 1000

            frame = HandFrame(
                frame_id=self._frame_id,
                hand_side="right",          # 可参数化
                finger_data=finger,
                wrist_pose=wrist,
                sync_delta_ms=sync_delta
            )
            self._frame_id += 1

            for cb in self._callbacks:
                try:
                    cb(frame)
                except Exception as e:
                    print(f"[Pipeline] callback error: {e}")

            # 精确限速
            elapsed = time.perf_counter() - t0
            sleep = self._interval - elapsed
            if sleep > 0:
                time.perf_counter()         # Windows 上 sleep 精度差
                time.sleep(sleep * 0.9)     # 留 10% buffer，避免过冲
```

---

### 第四层：录制模块（面向实验数据分析）

```python
# recording/recorder.py
import json
import time
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

@dataclass
class SessionMeta:
    """实验元数据，人因/认知实验必须有"""
    subject_id: str
    task_name: str
    condition: str          # 实验条件，如 "baseline" / "task_A"
    notes: str = ""
    experimenter: str = ""


class Recorder:
    """
    线程安全的录制器。
    输出格式：JSONL（每行一帧），便于 pandas/numpy 事后分析。
    """

    def __init__(self, output_dir: str = "data/sessions"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._file = None
        self._lock = threading.Lock()
        self._recording = False
        self._frame_count = 0
        self._session_meta: Optional[SessionMeta] = None

    def start_recording(self, meta: SessionMeta):
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{meta.subject_id}_{meta.task_name}_{meta.condition}_{timestamp}.jsonl"
        filepath = self.output_dir / filename
        
        self._file = open(filepath, "w", encoding="utf-8")
        self._session_meta = meta
        self._frame_count = 0
        self._recording = True
        
        # 写入 header 行（方便后续解析时识别元数据）
        header = {
            "type": "session_meta",
            "subject_id": meta.subject_id,
            "task_name": meta.task_name,
            "condition": meta.condition,
            "notes": meta.notes,
            "start_unix": time.time(),
            "start_pc": time.perf_counter()
        }
        self._file.write(json.dumps(header) + "\n")
        print(f"[Recorder] 开始录制 -> {filepath}")

    def on_frame(self, frame):
        """注册到 Pipeline 的 callback"""
        if not self._recording:
            return
        with self._lock:
            d = frame.to_dict()
            d["type"] = "frame"
            self._file.write(json.dumps(d) + "\n")
            self._frame_count += 1

    def stop_recording(self):
        self._recording = False
        with self._lock:
            if self._file:
                footer = {
                    "type": "session_end",
                    "total_frames": self._frame_count,
                    "end_unix": time.time()
                }
                self._file.write(json.dumps(footer) + "\n")
                self._file.close()
                self._file = None
        print(f"[Recorder] 录制结束，共 {self._frame_count} 帧")
```

---

### 第五层：串联使用

```python
# main.py
from core.tracker_provider import OpenVRTrackerProvider, DummyTrackerProvider
from core.pipeline import HandDataPipeline
from recording.recorder import Recorder, SessionMeta

# --- 配置 ---
TRACKER_SERIAL = "LHR-XXXXXXXX"   # 从 list_trackers() 获取
USE_TRACKER = True                  # Tracker 没到货时改 False

# --- 初始化 ---
from your_manus_sdk import ManusClient
manus = ManusClient()
manus.connect()

tracker_provider = OpenVRTrackerProvider() if USE_TRACKER else DummyTrackerProvider()
pipeline = HandDataPipeline(manus, tracker_provider,
                             tracker_serial=TRACKER_SERIAL, target_hz=100)

recorder = Recorder(output_dir="data/sessions")
pipeline.register_callback(recorder.on_frame)

# --- 实验开始 ---
meta = SessionMeta(
    subject_id="P01",
    task_name="grasp_task",
    condition="baseline"
)
pipeline.start()
recorder.start_recording(meta)

input("按 Enter 停止录制...\n")

recorder.stop_recording()
pipeline.stop()
```

---

## 事后分析读取示例

```python
# analysis/load_session.py
import json
import numpy as np
import pandas as pd

def load_session(filepath: str):
    frames = []
    meta = {}
    with open(filepath) as f:
        for line in f:
            row = json.loads(line)
            if row["type"] == "session_meta":
                meta = row
            elif row["type"] == "frame":
                frames.append({
                    "frame_id":       row["frame_id"],
                    "sync_delta_ms":  row["sync_delta_ms"],
                    "finger_valid":   row["finger"]["is_valid"],
                    "wrist_valid":    row["wrist"]["is_valid"],
                    "timestamp_pc":   row["finger"]["timestamp_pc"],
                    # 展开关节角度
                    **{f"joint_{i}": v for i, v in
                       enumerate(row["finger"]["joint_angles"] or [])},
                    # 展开位姿
                    "pos_x": (row["wrist"]["position"] or [None]*3)[0],
                    "pos_y": (row["wrist"]["position"] or [None]*3)[1],
                    "pos_z": (row["wrist"]["position"] or [None]*3)[2],
                })
    return meta, pd.DataFrame(frames)

meta, df = load_session("data/sessions/P01_grasp_task_baseline_....jsonl")
print(df.describe())
```

---

## Windows 上你最需要注意的三件事

**1. SteamVR headless 配置**（最常见的坑）
找到 `C:\Program Files (x86)\Steam\config\steamvr.vrsettings`，加入：
```json
"steamvr": {
    "requireHmd": false,
    "activateMultipleDrivers": true
}
```

**2. `perf_counter` 做时间戳**，绝对不要用 `time.time()` 做同步基准，Windows 上它的精度只有 15ms。

**3. Tracker serial 号的获取**：第一次运行时先跑一下 `tracker_provider.start(); print(tracker_provider.list_trackers())` 把序列号记下来写进配置文件，不要每次手动填。

这个架构你现在可以用 `DummyTrackerProvider` 先把录制和分析流程全部跑通，Tracker 到了之后只需要改一行初始化代码。