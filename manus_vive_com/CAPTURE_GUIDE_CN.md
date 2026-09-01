# MANUS RawSkeleton + Vive Tracker 独立采集

本采集路径用于小规模实验：MANUS Quantum MetaGloves 通过 MANUS SDK 输出
RawSkeleton，四个 Vive Tracker 直接通过 SteamVR/OpenVR 输出位姿。两条数据流
独立保存，不配对、不插值、不融合。

## 数据来源

- MANUS：`RawSkeletonStream`，`HandMotion_Auto`。没有外部 Tracker 时使用手套 IMU
  旋转。每个 SDK publish event 包含一只
  或两只手套，每个节点保存位置和 `x,y,z,w` 四元数。
- Vive Tracker：Python OpenVR 后台应用直接查询所有
  `TrackedDeviceClass_GenericTracker`，按硬件 serial 映射角色。
- MANUS Core 的 `TrackerStream` 不再注册。

注意：MANUS 坐标系和 OpenVR 坐标系不会由采集器进行标定或转换。采集器只保留
原值和坐标系元数据，空间标定由后处理完成。

## 环境准备

1. 安装并启动 MANUS Core，完成左右手套连接与校准。
2. 安装并启动 SteamVR，确认四个 Tracker 都在线且能正常追踪。
3. 确认 MANUS Core 2.4.0.1 SDK 位于
   `MANUS_Core_2.4.0.1_SDK/SDKMinimalClient_Windows/ManusSDK`。当前工程固定从该
   目录读取头文件、导入库和 DLL，避免与其他 SDK 版本混用。
4. 安装 Python 3.10 或更新版本，然后执行：

```powershell
py -m pip install -r requirements_capture.txt
```

5. 复制并填写配置：

```powershell
Copy-Item capture_config.example.yaml capture_config.yaml
```

填写两只手套的 `glove_id` 和四个 Tracker 的 SteamVR serial。Tracker serial
一般形如 `LHR-XXXXXXXX`。`strict_device_mapping: true` 时，只要配置中的 Tracker
缺失，采集器就拒绝开始。

## 编译

使用 Visual Studio 打开 `SDKClient.sln`，编译 x64 Debug 或 Release。项目
`SDKMinimalClient_Windows` 已配置使用 `SDKMinimalClient_socket.cpp`。

## 启动顺序

### 仅测试 MANUS MetaGloves

不启动 SteamVR/OpenVR、也不采集 Vive Tracker 时，运行：

```powershell
py capture_recorder.py --config capture_config.yaml --manus-only
```

看到 `[READY]` 后，再按所需频率启动 `SDKMinimalClient_Windows_2_4_120hz.exe`
或 `SDKMinimalClient_Windows_2_4_60hz.exe`，并选择 `Core Local`。不带频率后缀的
`SDKMinimalClient_Windows_2_4.exe` 是保留的原版 SDK 客户端，不包含本采集器所需的
限频 TCP 输出改装。
此模式只读取 YAML 中的 `session` 和 `manus` 配置，不校验 `openvr` 配置；会话目录
只生成 `manus_raw_skeleton.csv`、`manus_raw_skeleton.jsonl` 和
`session_metadata.json`。其中元数据的 `capture_mode` 为 `manus_raw_skeleton_only`。

### MANUS + Vive Tracker 联合采集

先运行中央采集器：

```powershell
py capture_recorder.py --config capture_config.yaml
```

看到 `[READY]` 后，再运行：

```powershell
Output\x64\Debug\SDKMinimalClient_Windows_2_4_120hz.exe
# 或：Output\x64\Debug\SDKMinimalClient_Windows_2_4_60hz.exe
```

选择 `Core Local`。采集器收到第一帧 RawSkeleton 后输出 `[STREAMING]`。直接运行
模式会自动开始一轮 `test/L_test` 写盘；UI 控制模式通过 stdin 反复控制 episode：

```text
start <task_name> <complex_level>  # 开始写盘，数据流此前已经存在
stop                              # 保存本轮，数据流保持运行
shutdown                          # 才真正关闭 MANUS/OpenVR 数据流
```

## 会话文件

每轮 start/stop 会生成独立目录：

```text
F:/ASC_vla/CCF-A/DataCapture_UI/vr_data/
└── <task_name>/<complex_level>/ep_YYYYMMDD_HHMMSS_<session_id>/
    ├── session_metadata.json
    ├── manus_raw_skeleton.csv
    ├── manus_raw_skeleton.jsonl
    ├── tracker_openvr.csv
    └── tracker_openvr.jsonl
```

- CSV 使用 UTF-8 BOM，便于 Excel 直接打开。
- JSONL 每行是一个完整源事件，便于复查原始层级结构。
- `session_metadata.json` 保存配置快照、坐标系说明、时钟说明、设备枚举结果、
  帧数、sequence 缺口、无效追踪数量和轮询超时数量。

## 时间字段

### MANUS

- `manus_publish_time_raw`：RawSkeleton 的 `publishTime.time` 原值，不能直接当成
  Unix 纳秒。
- `manus_publish_time_unix_ns`：通过 SDK `CoreSdk_GetTimestampInfo` 解码后的 UTC
  纳秒值；若 Core 输出的是
  timecode 而非 UTC 日期时间，该字段留空。
- `callback_system_time_unix_ns`：进入 C++ SDK 回调时立即读取的 Windows UTC
  系统时间，最接近 MANUS 数据进入本程序的 system time。
- `callback_steady_time_ns`：同一位置读取的单调时钟，适合计算时间差。
- `serialize_system_time_unix_ns`：后台线程开始序列化该帧的时间。
- `receiver_system_time_unix_ns`：Python 收到完整 JSON 行的时间。
- `transport_latency_estimate_ns`：receiver system time 减 callback system time；
  仅作本机软件传输延迟诊断。

### OpenVR Tracker

OpenVR 的该查询接口不会为每个 Tracker 返回独立设备采样时间。因此明确保存：

- `host_query_system_time_unix_ns`：API 调用前后 system time 的 midpoint；
- `host_query_monotonic_ns`：API 调用前后 steady time 的 midpoint；
- `query_span_ns`：API 调用耗时。

这些字段是主机查询时间，不是 Tracker 硬件采样时间。

所有主要时间都保存为整数纳秒，ISO 8601 字符串只用于人工查看。

## 频率语义

`openvr.poll_hz: 120` 表示程序每秒查询约 120 次，不代表 Vive Tracker 硬件产生
120 个全新样本。程序不去重、不插值，原样保存每次 API 查询结果。MANUS 仍由 SDK
回调驱动，但 RawSkeleton 发送端以所选 exe 的 120 Hz 或 60 Hz 单调时钟为周期，
每个周期只输出收到的最新完整帧；低于目标频率的源流不会被补帧或插值。
`sequence` 是连续输出序号，
`callback_sequence` 是限频前的 SDK 回调序号，`callbacks_coalesced_into_frame` 表示
合并进当前输出帧的较早 callback 数量。MANUS TrackerStream 已完全移除。

## 首次试采检查

第一次建议录制 30 秒，然后检查：

1. `session_metadata.json` 的 `status` 是否为 `complete`。
2. MANUS 客户端退出汇总的 `dropped` 是否为 0；高频源流下 `coalesced` 大于 0
   是预期的目标频率合并，不是丢帧。
3. `manus.sequence_gaps`、`manus.parse_errors` 和
   `manus.callback_sequence_gaps_unexplained` 是否为 0。
4. `observed_glove_ids` 是否与 YAML 完全一致。
5. `discovered_trackers` 是否为四个指定 serial。
6. `invalid_pose_rows` 是否仅出现在可解释的遮挡区间。
7. 两个 CSV 中的 system time 是否单调；如 Windows 在采集中被校时，应以
   steady time 计算局部时间差。
