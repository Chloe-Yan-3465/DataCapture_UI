# Codex 工作上下文

最后更新：2026-07-21（Asia/Shanghai）

## 项目目标

在 Windows 台式机上实现本地 BLE 授时程序，使 Windows 成为采集系统主时钟。程序读取 Windows Unix Epoch 时间并通过 BLE 写入 ESP32；ESP32 固件负责设置自身时间，并通过 115200 波特率 UART 向 NanoPi 发送 `TIMESYNC`。

程序不得修改 Windows 系统时间，也不得修改参考微信小程序源码 `reference/ble-miniProgram`。Windows 程序与手机小程序不能同时连接 ESP32。

## 今日完成情况

1. 已完整阅读 `prompt.txt`，并检查参考小程序的以下主要源码：
   - `reference/ble-miniProgram/utils/ble.js`
   - `reference/ble-miniProgram/utils/time-sync.js`
   - `reference/ble-miniProgram/pages/index/index.js`
   - `reference/ble-miniProgram/app.js`
2. 已确认并迁移参考实现中的 BLE UUID、12 字节请求、32 字节响应、校准流程、串行请求以及 Service UUID 优先扫描/全扫描回退逻辑。
3. 已创建 Windows Python 工程：
   - `app/main.py`：`scan`、`inspect`、`once`、`run`、`run --no-compensation` CLI。
   - `app/ble_client.py`：扫描、连接、GATT 检查、Notify、严格请求匹配、断线处理。
   - `app/protocol.py`：`<QI` 请求与 `<QqQQ` 响应协议。
   - `app/time_sync.py`：RTT、ESP 处理时间、净 RTT、单向延迟和两种补偿计算。
   - `app/logging_utils.py`：CSV、JSONL 和 `latest.log`。
   - `app/config.py`、`config/config.json`：配置加载与校验。
   - `scripts/install.ps1`、`scripts/run.ps1`。
   - `README.md`、`requirements.txt` 和离线测试。
4. Notify 回调会首先使用 `time.perf_counter_ns()` 记录 T4，再解析数据。响应必须严格等于 32 字节；截断或超长响应均拒绝解析。若状态特征支持 Read，可读取完整响应作为回退，但绝不解析截断数据。
5. 每次只允许一个 pending 请求，并使用响应中的 `phone_us` 匹配当前请求，避免迟到或无关 Notify 错配。
6. `run` 已实现断线后重新扫描、连接并重新校准。Ctrl+C 清理流程包括 `stop_notify` 和断开连接。
7. `scan` 的无蓝牙适配器错误已改为明确提示，不再向用户直接输出冗长异常堆栈。

## Python 与依赖环境

- 已按用户批准卸载 Python 3.13.14（64 位）。
- 已安装 Python 3.12.10（64 位）到：
  `C:\Users\yuxin\AppData\Local\Programs\Python\Python312`
- 用户级 `PATH` 已加入：
  - `C:\Users\yuxin\AppData\Local\Programs\Python\Python312`
  - `C:\Users\yuxin\AppData\Local\Programs\Python\Python312\Scripts`
  - Python Launcher 路径
- 项目虚拟环境：`D:\BLE-TimeSync\.venv`
- 已安装：
  - bleak 3.0.2
  - pip 26.1.2
  - bleak 所需 WinRT 传递依赖
- `pip check` 结果：`No broken requirements found.`
- 未安装 `pybluez`、`pyserial` 或 `pywin32`。

## 检查与测试结果

使用 `.venv` 中的 Python 3.12 执行：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

结果：

- 静态编译通过。
- 单元测试 6/6 通过。
- 测试覆盖请求端序与长度、带符号 ESP offset、32 字节严格校验、ESP 时间顺序、默认配置以及两种校准补偿算法。

## BLE 扫描结果与当前阻塞

已执行：

```powershell
.\.venv\Scripts\python.exe -m app.main scan
```

扫描结果：Windows/WinRT 报告没有可用 Bluetooth adapter，退出码为 2。沙箱内和沙箱外结果一致，因此不是沙箱设备隔离造成的误报。

进一步只读诊断结果：

- Windows 蓝牙支持服务 `bthserv` 正在运行。
- 当前即插即用设备列表中没有 Bluetooth 设备。
- 用户已确认本机没有蓝牙模块，计划购买蓝牙适配器/模块并安装对应驱动后再测试。

本次扫描没有连接 ESP32，也没有写入任何时间。尚未执行 `inspect`、`once` 或 `run`，因此真实 BLE 协议、Notify 和持续授时尚未经过硬件验证。目前只有 `logs/latest.log`；尚无实际同步会话产生的 CSV/JSONL 数据。

## 下次继续时的严格顺序

1. 确认蓝牙适配器已插入/启用，驱动安装正常，并能在 Windows 设备列表中看到。
2. 关闭手机授时小程序，确保手机未连接 `ESP32S3-TimeSync`，然后重启 ESP32。
3. 先执行且只执行：

   ```powershell
   .\.venv\Scripts\python.exe -m app.main scan
   ```

4. 向用户报告扫描结果。
5. 只有得到用户明确批准后，才能执行 `inspect`。
6. 注意：`inspect` 为验证 Notify 长度，会连接 ESP32 并发送一次不带补偿的时间探测请求，但不会持续授时。
7. 报告 GATT、目标 UUID、Characteristic 属性和 Notify 长度检查结果。
8. 未经再次明确批准，不得执行 `once`、`run` 或任何持续授时。

## 仍需现场验证

- Windows 能否稳定发现并连接 `ESP32S3-TimeSync`。
- Service、写 Characteristic、Notify/Read Characteristic 的实际属性是否符合配置。
- ESP32 Notify 是否严格为 32 字节，`phone_us` 是否匹配请求。
- 5 次校准、补偿授时、断线重连和 Ctrl+C 清理是否符合现场行为。
- NanoPi 是否以 115200 波特率持续收到 UART `TIMESYNC`。
- MVN 动作帧时间戳是否真正使用 Windows 系统时间；此项必须单独验证，不能因 BLE/UART 授时成功而默认成立。
