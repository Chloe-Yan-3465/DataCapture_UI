# 联合数据采集 UI

这是一个仅在本机运行的 Web 控制台，分别管理常驻 BLE 授时进程和每一轮联合采集：

- `BLE-TimeSync`：扫描、连接并校准 BLE 网关，向在线网关发送 `START` / `STOP`。
- `VIVE-Tracker_capture`：通过 OpenXR 采集 VIVE Tracker 位姿并保存 CSV/JSON。

服务只监听 `127.0.0.1`，默认地址为 <http://127.0.0.1:8765>，不会对局域网开放。

## 启动前准备

1. 关闭会占用 ESP32 BLE 连接的手机小程序或浏览器蓝牙工具，开启并重启需要的 ESP32 网关。
2. 确保 `BLE-TimeSync\.venv` 已按该项目 README 安装完成。
3. 启动 Steam、SteamVR 和 VIVE Hub，确认 Tracker 均已连接并可以正常定位。
4. Tracker 或角色发生变化时，需要重新生成 `VIVE-Tracker_capture\tracker_roles.json`。可以直接在 UI 中完成，方法见下文；也可以继续使用原命令：

   ```powershell
   .\01_绑定Tracker角色.ps1
   ```

页面顶部会显示 6 项静态启动检查。它能检查入口、Python、`uv` 和角色映射，但无法在不启动采集的情况下保证 BLE 设备在线或 OpenXR 定位正常；这两类运行状态会显示在对应终端中。

## 启动 UI

在工作区根目录 `F:\ASC_vla\CCF-A\DataCapture_UI` 打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\capture_ui\start_ui.ps1
```

脚本会启动服务并自动打开浏览器。保持这个 PowerShell 窗口运行；关闭服务时在该窗口按 `Ctrl+C`。

如需更换端口或不自动打开浏览器：

```powershell
.\capture_ui\start_ui.ps1 -Port 8877
.\capture_ui\start_ui.ps1 -NoBrowser
```

如果浏览器没有自动打开，手动访问 <http://127.0.0.1:8765>。

## 在 UI 中绑定 Tracker 角色

角色绑定已经集成到页面中，直接调用原项目的 `VIVE-Tracker_capture\01_绑定Tracker角色.ps1`：

1. 保持 SteamVR 运行，并确认所有 Tracker 已连接、可正常定位且已在 SteamVR 中分配角色。
2. 点击 **绑定 Tracker 角色**。
3. 绑定脚本会导出 SteamVR 当前显式分配的全部 Tracker 角色；OpenXR/OpenVR 检测过程和最终角色映射会实时显示在右侧 VIVE 终端中。
4. 成功后页面显示“Tracker 角色绑定完成”，并自动刷新顶部预检状态。生成结果仍位于 `VIVE-Tracker_capture\tracker_roles.json`。

绑定角色与正式采集不能同时运行。绑定过程中可以点击 **取消角色绑定**；重新绑定会更新原角色映射文件。

## BLE 授时与每轮采集

### 1. 启动 BLE 总进程

点击 **启动 BLE 授时**。UI 启动 `BLE-TimeSync`，等待网关扫描、连接、校准和 `[READY]`。页面显示“BLE 持续授时中”后，该进程保持常驻并继续每 10 秒周期授时；此时还没有向网关发送原键盘事件 `1`，也没有启动 Tracker 采集。

BLE 启动后，按钮会变为 **停止 BLE 授时**。采集中不能直接停止 BLE，必须先停止并保存当前采集。

### 2. 开始一轮采集

1. 根据需要设置 Tracker 采样率，默认 `120 Hz`。
2. 点击 **开始**。
3. UI 启动 VIVE OpenXR 采集，等待它完成初始化并创建本轮输出目录。
4. Tracker 真正开始记录后，UI 向常驻 BLE 进程发送 `1`，完全对应 BLE 原来的单键 `1` 逻辑。

### 3. 停止本轮采集

点击 **停止**：

- UI 向 BLE 发送 `0`，完全对应原来的单键 `0` 逻辑，并等待网关 ACK/帧数输出。
- UI 同时通知 Tracker 结束当前采样、刷新 CSV 并写入 `metadata.json`，效果等价于原脚本收到 `Ctrl+C` 后保存退出。
- Tracker 退出后，BLE 总进程不会退出，仍继续周期授时。页面回到“BLE 持续授时中”，可以再次点击 **开始** 采下一轮。

### 4. 最后停止 BLE

所有采集轮次完成后，点击 **停止 BLE 授时**。只有这一步会向 BLE 控制进程发送 `quit`、停止 Notify 并断开 BLE。

不要直接关闭 UI 的 PowerShell 窗口来代替页面上的 **停止**。误按 `Ctrl+C` 时服务仍会尝试保存 Tracker、发送 BLE `0` 并退出，但页面操作能给两个项目更完整的收尾时间。

## 页面说明

- 顶部状态依次可能为：`准备就绪`、`BLE 启动中`、`BLE 持续授时中`、`Tracker 启动中`、`正在采集`、`正在停止采集`、`采集异常`。
- 顶部“蓝牙网关”栏只显示当前已连接设备，例如 `[68] IDLE`；断线设备会从该栏移除，重连后自动恢复。
- “绑定 Tracker 角色”调用原 `01_绑定Tracker角色.ps1`，角色读取与错误输出显示在右侧终端。
- 左侧终端显示 BLE 扫描、连接、校准、周期同步、网关 ACK 和错误。
- 右侧终端显示 OpenXR Runtime、Tracker 列表、采样帧计数、保存目录和错误。
- Tracker 意外退出时，UI 会向 BLE 发送 `0`，但保留 BLE 周期授时进程；BLE 意外退出时，UI 会停止 Tracker 以保存已有数据。
- “清空屏幕日志”只清除 UI 内存中的显示内容，不会删除项目日志或采集数据。

蓝牙网关状态灯：

| 灯色 | 状态 | 含义 |
|---|---|---|
| 蓝色 | `IDLE` | 空闲，未采集 |
| 黄色 | `WAIT_START_ACK` / `WAIT_STOP_ACK` | 已向 Linux 发送命令，等待确认 |
| 绿色 | `RUNNING` | Linux 已回复 START 成功，正在采集 |
| 红色 | `ERROR` | Linux 报错或 ACK 超时 |
| 紫色 | OTA | 正在 OTA 模式 |

## 数据位置

BLE 日志仍由原项目写入：

```text
BLE-TimeSync\logs\latest.log
BLE-TimeSync\logs\timesync_YYYYMMDD_HHMMSS.csv
BLE-TimeSync\logs\timesync_YYYYMMDD_HHMMSS.jsonl
```

VIVE 每次采集仍写入：

```text
VIVE-Tracker_capture\vr_captures\vr_capture_YYYYMMDD_HHMMSS\
├── tracker_poses.csv
└── metadata.json
```

页面底部会显示本次 VIVE 输出目录。

## 实际执行的入口

UI 使用原项目环境和入口，不复制采集实现：

```powershell
# Tracker 角色绑定（UI 使用非交互模式，原手动用法保持不变）
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\01_绑定Tracker角色.ps1 -NonInteractive

# 工作目录：BLE-TimeSync
.\.venv\Scripts\python.exe -u -m app.main run --control-stdin

# 工作目录：VIVE-Tracker_capture
uv run --script .\collect_openxr_tracker_poses_and_triggers.py `
  --role-map .\tracker_roles.json `
  --output-root .\vr_captures `
  --tracker-rate 120 `
  --control-stdin
```

`--control-stdin` 是为 UI 新增的可选桥接模式。BLE 原来的单键 `1/0` 用法和 VIVE 原来的 `Ctrl+C` 用法均保持不变。

## 常见问题

### 页面显示 BLE 一直准备中

BLE 项目一次主动扫描最长约 30 秒，且没有可用网关时会继续重试。检查 ESP32 是否供电并广播、手机是否占用连接，以及终端里的在线/离线设备信息。BLE 进程已经创建后，可以点击 **停止 BLE 授时** 取消启动。

### VIVE 初始化后立即异常退出

通常需要检查 SteamVR 是否运行、是否为当前 OpenXR Runtime、Tracker 是否正常定位，以及 `tracker_roles.json` 是否与当前设备一致。具体异常会保留在右侧终端。

### `uv` 首次启动较慢

VIVE 脚本通过 PEP 723 声明 `pyopenxr==1.1.5301`。`uv` 第一次运行可能需要准备并缓存 Python/依赖，以后会直接复用缓存。

### 端口被占用

换一个端口启动，例如：

```powershell
.\capture_ui\start_ui.ps1 -Port 8877
```

## 开发校验

UI 本身只使用 Python 标准库，不需要额外安装 Web 框架。运行测试：

```powershell
cd .\capture_ui
..\BLE-TimeSync\.venv\Scripts\python.exe -m compileall -q .
..\BLE-TimeSync\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖日志增量读取、独立子进程的 stdout/stdin 控制、`BLE 启动 → BLE 1 + Tracker 开始 → BLE 0 + Tracker 停止 → BLE 保持运行 → BLE quit` 的拆分生命周期，以及实际绑定随机本地端口后访问首页、状态接口和启动预检接口。硬件采集仍需在 ESP32、SteamVR 和 Tracker 均在线时进行现场验证。
