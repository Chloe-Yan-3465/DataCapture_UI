#!/usr/bin/env python3
"""
简单校准脚本 - 非交互式版本
只收集数据并提供校准建议
"""

import time
import sys
from manus_data_receiver import ManusDataReceiver
from auto_calibration import AutoCalibration

def simple_calibration_demo():
    """简单校准演示 - 只收集数据和分析"""
    print("="*60)
    print("MANUS Core 简单校准工具")
    print("="*60)
    print("功能:")
    print("  1. 接收C++客户端数据")
    print("  2. 计算offset误差")
    print("  3. 提供具体轴调整建议")
    print("="*60)

    # 创建接收器和校准器
    receiver = ManusDataReceiver(host="127.0.0.1", port=8888)
    calibrator = AutoCalibration(
        adjustment_threshold_m=0.005,
        min_samples=50,
        confidence_level=0.95
    )

    frame_count = 0
    error_samples = 0

    # 简单的回调函数
    def simple_callback(frame_data):
        nonlocal frame_count, error_samples

        frame_count += 1

        # 计算offset误差
        error_info = receiver.calculate_offset_error()

        if error_info:
            calibrator.add_error_sample(error_info)
            error_samples += 1

            # 每50帧显示一次状态
            if error_samples % 50 == 0:
                error_m = error_info['distance_m']
                error_cm = error_m * 100
                print(f"[DATA] 帧: {frame_count:04d}, 误差: {error_cm:.1f}cm, 样本: {error_samples}")

    # 注册回调
    receiver.register_callback(simple_callback)

    print("\n[INFO] 启动数据接收器...")
    print("[INFO] 确保C++客户端正在运行")
    print("[INFO] 按Ctrl+C停止收集\n")

    try:
        # 启动接收器（阻塞调用）
        receiver.start()

    except KeyboardInterrupt:
        print("\n[INFO] 用户中断")
    except Exception as e:
        print(f"[ERROR] 运行错误: {e}")
    finally:
        receiver.stop()

        # 显示校准结果
        print("\n" + "="*60)
        print("校准分析结果")
        print("="*60)

        print(f"总帧数: {frame_count}")
        print(f"误差样本数: {calibrator.sample_count}")

        if calibrator.sample_count >= calibrator.min_samples:
            suggestions = calibrator.get_adjustment_suggestions()

            if suggestions['status'] == 'READY':
                print(f"\n校准建议 (置信度: {suggestions['confidence']:.1%}):")

                for adj in suggestions['adjustments']:
                    if adj['needs_adjustment']:
                        direction = "增加" if adj['suggested_adjustment_m'] > 0 else "减少"
                        abs_amount = abs(adj['suggested_adjustment_m'])
                        print(f"  {adj['axis']}轴: {direction} {abs_amount:.3f}m ({abs_amount*100:.1f}cm)")
                        print(f"    当前误差: {adj['current_error_m']*100:.1f}cm")

                print(f"\n人类可读建议: {suggestions['human_readable']}")

                print("\nXML格式建议 (复制到MANUS Core配置文件):")
                print("-" * 40)
                print(suggestions['xml_suggestions'])
                print("-" * 40)

            else:
                print(f"\n数据不足: {suggestions['message']}")

        else:
            print(f"\n[WARN] 数据不足，需要至少{calibrator.min_samples}个样本")
            print(f"当前只有{calibrator.sample_count}个样本")
            print("建议: 多移动一会儿以收集更多数据")

        # 显示详细分析
        if calibrator.sample_count > 0:
            print("\n" + "="*60)
            print("详细误差分析")
            print("="*60)

            detailed = calibrator.get_detailed_analysis()

            if detailed['status'] == 'ANALYZED':
                print("平均误差向量 (X, Y, Z):")
                errors = detailed['mean_errors_m']
                print(f"  X: {errors[0]*100:+.1f}cm, Y: {errors[1]*100:+.1f}cm, Z: {errors[2]*100:+.1f}cm")

                print("\n误差标准差 (稳定性):")
                stds = detailed['std_errors_m']
                print(f"  X: {stds[0]*100:.1f}cm, Y: {stds[1]*100:.1f}cm, Z: {stds[2]*100:.1f}cm")

                print(f"\n平均误差幅度: {detailed['avg_error_magnitude_m']*100:.1f}cm")
                print(f"最大误差幅度: {detailed['max_error_magnitude_m']*100:.1f}cm")

        print("\n" + "="*60)
        print("校准完成")
        print("="*60)

def quick_test():
    """快速连接测试"""
    print("="*60)
    print("快速连接测试")
    print("="*60)

    import socket

    # 检查端口
    port = 8888
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        sock.bind(("127.0.0.1", port))
        sock.close()
        print("[TEST] 端口 8888 可用")
    except:
        print("[TEST] 端口 8888 被占用")
        print("[TEST] 可能已有其他接收器在运行")
        return False

    # 尝试启动简单接收器
    print("\n[TEST] 尝试接收数据...")
    print("[TEST] 按Ctrl+C停止测试")
    print("[TEST] 如果长时间无连接，请检查C++客户端")

    try:
        receiver = ManusDataReceiver()

        # 简单回调
        def test_callback(frame_data):
            print(f"[TEST] 收到数据: 帧{frame_data.get('frame', 0)}")

        receiver.register_callback(test_callback)

        # 设置超时
        import threading
        receiver_thread = threading.Thread(target=receiver.start, daemon=True)
        receiver_thread.start()

        # 等待10秒
        for i in range(10):
            print(f"[TEST] 等待连接... {10-i}秒")
            time.sleep(1)
            if not receiver_thread.is_alive():
                print("[TEST] 接收器线程已停止")
                break

        receiver.stop()

    except Exception as e:
        print(f"[TEST] 测试错误: {e}")
        return False

    return True

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        quick_test()
    else:
        simple_calibration_demo()