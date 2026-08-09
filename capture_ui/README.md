# 联合数据采集 UI

这是一个仅在 Windows 本机运行的 Web 控制台，用于协调两条独立链路：

- `BLE-TimeSync`：Windows 通过 USB 串口连接 `Mode2Coordinator`，持续给中控和 wearable/NanoPi 授时，并发送 `START` / `STOP`。
- `VIVE-Tracker_capture`：通过 SteamVR/OpenXR 采集 VIVE Tracker 位姿并保存 CSV/JSON。

服务只监听 `127.0.0.1`，默认地址为 <http://127.0.0.1:8765>。

## 启动前准备

1. 将 Mode2 中控连接到电脑，并在 `BLE-TimeSync/config/config.json` 中配置正确的 `serial_port`。
2. 确保 `BLE-TimeSync/.venv` 可用，并已安装该项目依赖。
3. 启动 Steam、SteamVR 和 VIVE Hub，确认 Tracker 均已连接且可以正常定位。
4. 确认 `VIVE-Tracker_capture/tracker_roles.json` 与当前 Tracker/角色一致；需要时可在 UI 中重新绑定。

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

### 1. 启动常驻授时

点击 **启动常驻授时**。UI 实际执行：

```powershell
# 工作目录：BLE-TimeSync
.\.venv\Scripts\python.exe -u -m app.main run --control-stdin
```

程序连接 Mode2 中控、完成首次授时并输出 `[READY]` 后，页面进入“Mode2 持续授时中”。此后授时进程常驻，并按配置周期持续授时。

### 2. 开始录制

点击 **开始录制** 后：

1. UI 按原有参数启动 VIVE OpenXR 采集进程。
2. VIVE 创建输出目录并开始记录后，UI 向常驻 Mode2 进程 stdin 发送键盘命令 `1`。
3. Mode2 执行录制前授时、等待应用窗口、发送 `START`，并等待所有 wearable 进入 `ARMED`。
4. 只有收到 `[START] All wearable nodes armed...` 后，页面才显示“正在录制”。
5. START 被拒绝、失败或超时时，UI 会停止并保存刚启动的 Tracker 进程，常驻授时继续运行。

### 3. 停止录制

点击 **停止录制** 后：

1. UI 向 VIVE 进程发送 `stop`，让它完成保存并退出。
2. UI 向 Mode2 进程 stdin 发送键盘命令 `0`，对应 `STOP`。
3. UI 等待 Mode2 输出 `[STOP] ...`。未确认 STOP 时会在页面显示警告，并保留控制日志供检查。
4. 本轮结束后，Mode2 授时进程不退出，仍可继续下一轮录制。

### 4. 停止常驻授时

所有录制轮次结束后点击 **停止常驻授时**。UI 向 Mode2 进程发送 `quit` 并关闭串口。

录制进行中不能直接停止常驻授时，必须先停止并保存当前录制。

## 页面状态和日志

Mode2 状态栏显示：

- 当前中控名称和串口；
- 授时状态：`STOPPED`、`SYNCING`、`SYNCED`、`RETRYING`；
- 控制状态：`OFFLINE`、`IDLE`、`STARTING`、`RUNNING`、`STOPPING`、`ERROR`；
- 中控报告的 `utc_map` 状态；
- 各 wearable 节点的连接和运行状态。

页面将日志分为三个独立终端：

- **Mode2 授时与串口**：串口连接、`TIME_QUERY/TIME_REPLY`、`TIME_SET/TIME_ACCEPT`、RTT、UTC 映射、周期授时和重试错误。
- **Mode2 录制控制**：UI 发送的 `1/0`、START/STOP、节点状态、全部节点 ARMED、拒绝、失败和超时。
- **VIVE Tracker**：OpenXR 初始化、Tracker 列表、采样进度、输出目录和保存结果。

“清空屏幕日志”只清除 UI 内存中的显示内容，不删除项目日志或采集数据。

## 数据位置

Mode2 授时日志：

```text
BLE-TimeSync\logs\latest.log
BLE-TimeSync\logs\timesync_YYYYMMDD_HHMMSS.csv
BLE-TimeSync\logs\timesync_YYYYMMDD_HHMMSS.jsonl
```

VIVE 每轮录制：

```text
VIVE-Tracker_capture\vr_captures\vr_capture_YYYYMMDD_HHMMSS\
├── tracker_poses.csv
└── metadata.json
```

## 开发校验

UI 本身只使用 Python 标准库。运行测试：

```powershell
cd .\capture_ui
..\BLE-TimeSync\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
node .\tests\frontend_smoke.js
```

测试使用模拟子进程和模拟 Mode2 日志，不会连接真实串口、发送硬件 START/STOP 或启动 OpenXR。
