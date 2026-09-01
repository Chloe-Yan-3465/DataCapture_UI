# 联合数据采集 UI

这是一个仅在 Windows 本机运行的 Web 控制台，用于协调两条采集链路：

- `BLE-TimeSync`：Windows 通过 USB 串口连接 `Mode2Coordinator`，持续给中控和 wearable/NanoPi 授时，并发送 `START` / `STOP`。
- `manus_vive_com`：联合采集 MANUS RawSkeleton 和 SteamVR/OpenVR Vive Tracker 位姿。

服务只监听 `127.0.0.1`，默认地址为 <http://127.0.0.1:8765>。

## 启动前准备

1. 将 Mode2 中控连接到电脑，并在 `BLE-TimeSync/config/config.json` 中配置正确的 `serial_port`。
2. 确保 `BLE-TimeSync/.venv` 可用，并已安装该项目依赖。
3. 启动 MANUS Core、Steam 和 SteamVR，确认手套及 Tracker 均已连接且可以正常定位。
4. 在 `manus_vive_com/capture_config.yaml` 中填写手套 ID 和 Tracker serial。
5. 确认 `manus_vive_com/Output/x64/Debug/SDKMinimalClient_Windows_2_4_120hz.exe` 存在。

UI 会在执行相关操作时检查入口和工具是否存在，并直接显示失败原因。串口、中控、wearable 以及 OpenXR 的真实运行情况显示在对应状态区和日志终端中。

## 启动 UI

在工作区根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\capture_ui\start_ui.ps1
```

更换端口或不自动打开浏览器：

```powershell
.\capture_ui\start_ui.ps1 -Port 8877
.\capture_ui\start_ui.ps1 -NoBrowser
```

## 操作逻辑

### 1. 开启授时和常驻数据流

点击 **开启授时 + 数据流**。UI 先启动 Mode2 常驻授时，待中控串口
`[READY]` 后立即启动 `capture_recorder.py` 和 MANUS C++ 客户端。recorder
持续接收 MANUS RawSkeleton 并轮询 OpenVR Tracker，但此时不创建 episode、
不写任何采集文件。

Mode2 实际执行：

```powershell
# 工作目录：BLE-TimeSync
.\.venv\Scripts\python.exe -u -m app.main run --control-stdin
```

recorder 输出 `[STREAMING]` 后页面进入“授时与数据流就绪”。授时进程按配置
周期持续重试；Tracker 横栏也会在录制前持续显示实时连接/追踪状态。

点击 **扫描 wearable** 会向常驻 Mode2 进程发送键盘命令 `s`，进而通过串口发送 `SCAN`。中控仅在收到该命令时执行一次扫描，平时不会自动扫描新设备；已经连接的 wearable 仍持续接收时钟同步。

### 2. 开始录制

点击 **开始录制** 后：

1. UI 读取当前选择的 `task_name` 与 `complex_level`；自定义任务通过 `+`
   添加后保存在 `capture_ui/config/task_options.json`，下次启动仍可选择。
2. UI 向常驻 recorder 发送 `start <task_name> <complex_level>`；recorder 此时才在
   `vr_data/<task_name>/<complex_level>/ep_...` 下创建文件并开始写盘。
3. recorder 输出 `[RECORDING]` 后，UI 向 Mode2 发送同样任务元数据的 START。
4. Mode2 等待当前已连接的 wearable 进入 `ARMED`。
5. 只有收到 `[START] Connected wearable nodes armed...` 后，页面才显示“正在录制”。
6. 任一步骤失败时只关闭本轮 VR 文件，Tracker/MANUS 数据流继续运行。

### 3. 停止录制

点击 **停止录制** 后：

1. UI 向 `capture_recorder.py` stdin 发送 `stop`，recorder flush 并关闭本轮 episode
   文件，但不注销 MANUS socket、SDK callback 或 OpenVR 通道。
2. UI 向 Mode2 进程 stdin 发送键盘命令 `0`，对应 `STOP`。
3. UI 等待 Mode2 输出 `[STOP] ...`。未确认 STOP 时会在页面显示警告，并保留控制日志供检查。
4. 本轮结束后，Mode2 授时和 Tracker/MANUS 数据流均不退出，可立即开始下一轮。

### 4. 停止数据流与授时

所有录制轮次结束后，点击 **停止 Tracker & MANUS 数据流**。该按钮发送
`shutdown`，由 recorder 通知 C++ 客户端停止 SDK callback、排空 RawSkeleton
发送队列并关闭 socket，同时退出 OpenVR。

数据流停止后可以点击 **重新启动 Tracker & MANUS 数据流**，无需重启授时。
只有数据流停止后才能点击 **停止常驻授时**，关闭 Mode2 串口。

录制进行中不能直接停止常驻授时，必须先停止并保存当前录制。

## 页面状态和日志

Mode2 状态栏显示：

- 当前中控名称和串口；
- 授时状态：`STOPPED`、`SYNCING`、`SYNCED`、`RETRYING`；
- 控制状态：`OFFLINE`、`IDLE`、`STARTING`、`RUNNING`、`STOPPING`、`ERROR`；
- 中控报告的 `utc_map` 状态；
- 各 wearable 节点的连接和运行状态。
- 紧凑 Tracker 横栏：绿色表示在线且位姿有效，红色表示掉线或追踪丢失。

页面将日志分为三个独立终端：

- **Mode2 授时与串口**：串口连接、`TIME_QUERY/TIME_REPLY`、`TIME_SET/TIME_ACCEPT`、RTT、UTC 映射、周期授时和重试错误。
- **Mode2 录制控制**：UI 发送的 `s/1/0`、SCAN、START/STOP、已连接节点 ARMED、拒绝、失败和超时。
- **MANUS + Vive Tracker**：recorder、C++ SDK 客户端、Core Local 选择、RawSkeleton/OpenVR 初始化、输出目录和保存结果。

“清空屏幕日志”只清除 UI 内存中的显示内容，不删除项目日志或采集数据。

## 数据位置

Mode2 授时日志：

```text
BLE-TimeSync\logs\latest.log
BLE-TimeSync\logs\timesync_YYYYMMDD_HHMMSS.csv
BLE-TimeSync\logs\timesync_YYYYMMDD_HHMMSS.jsonl
```

MANUS + Vive Tracker 每轮录制：

```text
F:\ASC_vla\CCF-A\DataCapture_UI\vr_data\
└── <task_name>\
    └── <complex_level>\
        └── ep_YYYYMMDD_HHMMSS_<session_id>\
            ├── session_metadata.json
            ├── manus_raw_skeleton.csv
            ├── manus_raw_skeleton.jsonl
            ├── tracker_openvr.csv
            └── tracker_openvr.jsonl
```

不再注册或保存 MANUS SDK `TrackerStream`；Tracker 位姿仅来自 SteamVR/OpenVR。

## 开发校验

UI 本身只使用 Python 标准库。运行测试：

```powershell
cd .\capture_ui
..\BLE-TimeSync\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
node .\tests\frontend_smoke.js
```

测试使用模拟子进程和模拟 Mode2 日志，不会连接真实串口、发送硬件 START/STOP 或启动 OpenXR。
