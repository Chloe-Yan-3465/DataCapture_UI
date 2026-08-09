# Windows—Mode2Coordinator 协议对照

本文档对应 `esp32_mode2_sync_timesync.cpp` 的 coordinator 角色。

## 物理接口

- Windows 到中控：USB CDC 串口，115200、8N1、ASCII 行协议。
- 中控到 wearable：BLE，由中控固件独占管理。
- wearable 到 NanoPi：UART。

Windows 不连接 `Mode2Node-1/2/3`，也不使用 wearable 的 GATT UUID。

## 精确授时

```text
TIME_QUERY <uint32_nonzero_seq>\n
TIME_REPLY <seq> <coordinator_receive_us> <coordinator_transmit_us>\n
TIME_SET <seq> <coordinator_ref_us> <utc_ref_ns> <uncertainty_us>\n
TIME_ACCEPT seq=<seq> nodes=<count> uncertainty_us=<value>\n
```

错误响应以 `TIME_ERROR` 开头。程序只在收到相同序号的 `TIME_ACCEPT` 后确认成功。`nodes=<count>` 是本轮实际成功授时的已连接节点数，Windows 接受中控返回的动态数量，不要求固定为 3。

## 控制

```text
SCAN\n
START\n
STOP\n
ABORT\n
STATUS\n
```

Windows 端的正常 UI 暴露 SCAN、START 与 STOP。`SCAN` 只在用户按下键盘 `S` 或点击 UI 扫描按钮时发送，中控空闲时不会周期扫描新 wearable。裸 `START` 的内部 session ID 由中控固件生成。

START 的最终成功行以 `session <id>` 开头并包含独立单词 `ARMED`。Windows 不依赖 `all nodes` 等固定后缀，因此兼容“全部配置节点”和“当前已连接节点”两种中控策略。

## 并行性

Windows 使用后台串口读取任务；控制命令和周期授时分别运行，但所有写操作通过短时互斥锁原子发送。中控返回行按 `seq` 分发给对应授时请求，其他节点状态行持续写入日志，因此周期授时不会阻塞键盘/UI 输入线程。
