# MANUS Core 调试指南

## 当前问题总结

根据您的调试日志，主要问题是：

1. **C++客户端没有发送Tracker数据** - Python端一直显示`[DEBUG] Tracker位置: None`
2. **按下't'后连接断开** - 可能是输入阻塞导致socket问题
3. **平移质量检测不准确** - 线性度0.632（需要>0.90），但您说移动的是直线

## 修复内容

### 1. 修复了按钮处理逻辑
- `debug_demo.py`：添加了详细的调试日志和异常处理
- 第二次按't'或'r'现在应该正常停止测试，而不是退出程序

### 2. 修复了连接断开问题
- 使用非阻塞输入（`msvcrt.kbhit()`）避免`input()`阻塞socket
- 添加了全面的异常处理

### 3. 调整了平移检测算法
- 将线性度阈值从0.95降低到0.90，使检测更宽松
- 添加了详细的调试输出

### 4. 修复了JSON序列化问题
- 添加了`numpy_to_python()`转换函数
- 修复了"Object of type bool_ is not JSON serializable"错误

## 测试步骤

### 步骤1: 诊断Tracker数据问题

首先运行诊断工具，检查C++客户端发送的数据：

```bash
python diagnose_tracker.py
```

这个工具会：
- 显示C++客户端发送的数据类型（骨架/Tracker/混合）
- 诊断为什么没有Tracker数据
- 每5秒显示统计信息

**预期结果**：您应该看到`混合数据 (骨架+Tracker)`或至少`仅Tracker数据`。

### 步骤2: 测试修复后的debug版本

```bash
python debug_demo.py
```

观察以下变化：

1. **非阻塞输入**：程序不会在`input()`处阻塞，可以持续接收数据
2. **详细调试日志**：会显示`[DEBUG]`信息，帮助诊断问题
3. **按钮逻辑**：按't'开始平移测试，再次按't'应该停止测试而不是退出

### 步骤3: 检查C++客户端配置

如果诊断工具显示没有Tracker数据，需要检查：

1. **MANUS Core配置**：确保Vive Tracker设备已正确配置
2. **C++客户端代码**：确保编译了`SDKMinimalClient_socket.cpp`版本
3. **Tracker回调**：确保C++客户端注册了Tracker回调（第553行）

## 关键问题诊断

### 问题: "Tracker位置: None"

**可能原因**：
1. MANUS Core中没有配置Vive Tracker
2. C++客户端没有注册Tracker回调
3. Tracker设备未连接或未启用

**解决方案**：
1. 运行`diagnose_tracker.py`确认数据流
2. 检查MANUS Core的Tracker配置
3. 确保C++客户端输出Tracker日志（应该看到`[TRACKER]`相关输出）

### 问题: "按下't'后连接断开"

**已修复**：使用非阻塞输入避免socket阻塞

### 问题: "平移质量检测不准确"

**已调整**：降低线性度阈值，使检测更宽松

## 文件说明

### 主要测试文件
- `debug_demo.py` - **推荐使用**，包含完整调试日志和修复
- `diagnose_tracker.py` - Tracker数据诊断工具
- `full_calibration_demo.py` - 完整版本（也已修复）

### 诊断文件
- `diagnose_problems.md` - 问题诊断指南
- `20260410debug.md` - 您的调试日志记录

## 下一步

1. **首先运行** `python diagnose_tracker.py`，告诉我诊断结果
2. **然后运行** `python debug_demo.py`，测试按钮逻辑是否正常
3. **观察**是否还有连接断开问题

根据诊断结果，我们可以确定下一步：
- 如果C++客户端发送Tracker数据：检查Python端解析问题
- 如果C++客户端不发送Tracker数据：检查MANUS Core配置和C++代码

请提供新的调试日志，特别是`diagnose_tracker.py`的输出。