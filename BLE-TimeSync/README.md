# Windows BLE 授时程序

## 三网关键盘控制

当前 `run` 模式同时管理 `ESP32S3-Gateway-68`、`ESP32S3-Gateway-69` 和
`ESP32S3-Gateway-70`。程序扫描一次并按完整名称建立映射，为三台设备分别建立
BLE 连接、订阅 Time Status 和 Gateway Status，并依次执行 5 次预热和 25 次正式
校准。校准完成后，每台设备继续每 10 秒授时一次，三台写入错开 300 ms。

```powershell
.\.venv\Scripts\python.exe -m app.main run
```

控制台显示 `[READY]` 后可直接按单键控制，无需按 Enter：

- `1`：为本次采集生成统一的 `session_id`，向三台设备发送 START，并分别等待
  ESP32 `FORWARDED` 和 Linux `ACK+START`。
- `0`：使用当前 `session_id` 向三台设备发送 STOP，并分别等待 Linux
  `ACK+STOP` 和帧数。
- `Ctrl+C`：停止 Notify、断开三台 BLE 连接并退出。

程序不要求三台设备全部在线。完成连接、身份校验并返回 `SYNC=YES` 的设备会进入
在线集合；按 `1` 或 `0` 时只向当前在线集合发送，并打印本次目标和未发送的离线
设备。缺失设备会在后台继续发现，出现后单独连接和校准，但不会补发此前的 START。
某台断线后只重连和重新校准该设备，重连后先查询 STATUS，不自动重放 START。
完整控制协议见 `docs/windows_ble_gateway_protocol.md`。

本程序让采集 MVN 数据的 Windows 台式机成为授时主时钟。程序只读取 Windows 的 Unix Epoch 时间，通过 BLE 写入 ESP32；ESP32 固件设置自身时间后，再以 UART `TIMESYNC` 报文授时 NanoPi。

**本程序不会修改 Windows 系统时间。** Windows 程序会取代微信小程序成为 ESP32 的唯一 BLE Central，禁止手机小程序和本程序同时连接 ESP32。

## 系统边界

- 不修改 `reference/ble-miniProgram` 中的微信小程序源码。
- 不使用 `pybluez`、`pyserial` 或 `pywin32`，不需要管理员权限。
- ESP32 当前 UART 波特率为 **115200**。
- ESP32 断线后可能不会自动恢复 BLE 广播；重连扫描一直失败时应重启 ESP32。
- **MVN 动作帧时间戳是否确实使用 Windows 系统时间仍需单独验证。** 完成 BLE/UART 授时不等于已经证明 MVN 时间戳与 Windows 同源。

## 运行环境与安装

要求 Windows、64 位 Python 3.11 或 3.12，以及 `bleak>=2,<4`。所有 Python 包只安装到项目内的 `.venv`，不修改系统范围 Python 配置。

缺失 Python 或依赖时，应先取得操作者对每项依赖及安装位置的批准，再执行安装。获得批准后，在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

安装脚本会检查 Python 版本、创建 `.venv`、升级虚拟环境内的 pip，并安装 `requirements.txt`。

## 命令

以下命令均从 `D:\BLE-TimeSync` 执行：

```powershell
# 单次无过滤主动扫描（最长 30 秒）；不会连接，也不会写入时间
.\.venv\Scripts\python.exe -m app.main scan

# 连接、打印 GATT，并发送一次不带补偿的探测请求以验证 Notify 长度
.\.venv\Scripts\python.exe -m app.main inspect

# 执行 5 次校准，再完成一次带补偿的授时，然后退出
.\.venv\Scripts\python.exe -m app.main once

# 校准后每 10 秒持续授时
.\.venv\Scripts\python.exe -m app.main run

# 对照实验：仍执行校准测量，但后续发送不应用补偿
.\.venv\Scripts\python.exe -m app.main run --no-compensation
```

也可运行 `scripts/run.ps1` 激活虚拟环境并启动持续授时。

`inspect` 会写入一次当前 Windows 时间，以便 ESP32 产生 Notify；它不是持续授时。必须先报告 `scan` 结果并得到现场操作者确认后，才可执行 `inspect`。同样，未经明确确认不得执行 `once` 或 `run`。

## 现场运行顺序

1. 关闭手机授时微信小程序。
2. 确保手机没有连接 `ESP32S3-TimeSync`；必要时关闭手机 BLE。
3. 重启 ESP32，使其重新广播。
4. NanoPi 以 **115200** 波特率启动 `timesync_receiver`。
5. Windows 执行 `python -m app.main run`（使用项目虚拟环境的 Python）。
6. 等待默认 5 次校准完成。
7. 确认 Windows 持续收到 32 字节 Notify。
8. 确认 NanoPi 持续收到 UART `TIMESYNC`。
9. 最后再启动 `yuv_v3` 和 MVN 录制。

扫描直接使用 Bleak 的单次无过滤主动 `discover()` 路径并完整运行 30 秒，结束后再根据已合并的广播与 Scan Response 匹配目标。名称匹配不区分大小写，并允许名称前后存在附加文本，但不会把任意 unnamed 设备当作目标。

如扫描不到设备，依次检查：关闭浏览器在线蓝牙调试工具和手机授时小程序、断开手机 BLE、重启 ESP32。

## 协议与计时方法

- 设备名前缀：`ESP32S3-TimeSync`
- Service：`12345678-1234-1234-1234-123456789abc`
- Windows 写特征：`12345678-1234-1234-1234-123456789abd`
- ESP32 Notify/Read 特征：`12345678-1234-1234-1234-123456789abe`
- 请求固定为 12 字节小端 `<QI`：`uint64 sec`、`uint32 usec`。
- 响应严格为 32 字节小端 `<QqQQ`：`phone_us`、`offset_us`、`esp_receive_us`、`esp_process_us`。
- 写入使用 write-with-response。

绝对 Unix 时间来自 `time.time_ns()`；RTT 只使用 `time.perf_counter_ns()`。Notify 回调一进入就记录 T4。响应长度不是恰好 32 字节时不会解析；若状态特征支持 Read，可以读取一份完整值作为回退，但仍不会解析截断数据。响应的 `phone_us` 还必须匹配当前唯一 pending 请求。

校准默认发送 5 个不带补偿的请求，间隔 500 ms，同时计算：

- 兼容小程序的 `平均 total RTT / 2`；
- 推荐的 `net RTT 单向延迟中位数`。

默认选用推荐值。`net RTT = total RTT - ESP处理时间`，单向延迟为 `max(net RTT / 2, 0)`。断线后 `run` 会重新扫描、连接并重新校准；如 ESP32 没有恢复广播，仍需人工重启。

## 配置与日志

配置位于 `config/config.json`。同步会话生成：

- `logs/timesync_YYYYMMDD_HHMMSS.csv`
- `logs/timesync_YYYYMMDD_HHMMSS.jsonl`
- `logs/latest.log`

CSV 和 JSONL 记录序号、Windows UTC、发送时间、T1/T4、总 RTT、ESP 处理时间、净 RTT、估算单向延迟、补偿、ESP offset、成功状态及错误。运行期间控制台持续显示最近的 RTT、单向延迟、补偿、ESP offset 和累计成功/失败次数。所有捕获到的运行异常都会写入 `latest.log`。

## 开发检查

不接触 BLE 的离线检查：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

现场验证必须遵守顺序：静态检查 → `scan` → 报告扫描结果并等待确认 → `inspect`。之后只有在再次得到明确授权时，才执行持续授时。
