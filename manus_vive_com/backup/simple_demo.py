#!/usr/bin/env python3
"""
简单演示 - 展示核心功能：
1. 平移质量检测
2. 自动校准建议（具体调整哪个轴多少距离）
"""

import numpy as np
import random
import time

def demo_translation_quality():
    """演示平移质量检测"""
    print("\n" + "="*60)
    print("1. 平移质量检测演示")
    print("="*60)

    # 创建平移质量分析器
    from translation_quality import TranslationQualityAnalyzer
    analyzer = TranslationQualityAnalyzer(
        linearity_threshold=0.95,
        deviation_threshold_m=0.02
    )

    print("模拟直线平移数据...")
    analyzer.start_collection()

    # 模拟理想的直线平移（X轴方向，有轻微噪声）
    for i in range(100):
        x = i * 0.002  # 总行程20cm
        y = random.gauss(0, 0.0005)  # 微小随机误差
        z = random.gauss(0, 0.0005)
        analyzer.add_position([x, y, z])

    result = analyzer.stop_collection()

    if result:
        print(f"\n平移质量分析结果:")
        print(f"  线性度: {result['linearity']:.3f} ({'合格' if result['is_linear'] else '不合格'})")
        print(f"  平均偏差: {result['avg_deviation_m']*100:.2f}cm ({'稳定' if result['is_stable'] else '不稳定'})")
        print(f"  最大偏差: {result['max_deviation_m']*100:.2f}cm")
        print(f"  方向向量: [{result['direction'][0]:.3f}, {result['direction'][1]:.3f}, {result['direction'][2]:.3f}]")

        if result['is_linear'] and result['is_stable']:
            print("  [结论] 平移质量: 优秀 - 轨迹接近完美直线")
        else:
            print("  [结论] 平移质量: 需要改进")
    else:
        print("分析失败: 数据不足")

    return analyzer

def demo_auto_calibration():
    """演示自动校准建议"""
    print("\n" + "="*60)
    print("2. 自动校准建议演示")
    print("="*60)

    from auto_calibration import AutoCalibration
    calibrator = AutoCalibration(
        adjustment_threshold_m=0.005,  # 5mm
        min_samples=30,
        confidence_level=0.95
    )

    print("模拟offset误差数据...")
    print("假设当前系统有误差:")
    print("  X轴: +2cm 系统误差")
    print("  Y轴: -1cm 系统误差")
    print("  Z轴: +0.5cm 系统误差")

    # 模拟误差数据
    for i in range(50):
        # 模拟系统误差加随机噪声
        error_x = 0.02 + random.gauss(0, 0.003)  # 2cm系统误差 ± 0.3cm噪声
        error_y = -0.01 + random.gauss(0, 0.002) # -1cm系统误差 ± 0.2cm噪声
        error_z = 0.005 + random.gauss(0, 0.002) # 0.5cm系统误差 ± 0.2cm噪声

        error_info = {
            'error_vector': [error_x, error_y, error_z],
            'distance_m': (error_x**2 + error_y**2 + error_z**2)**0.5
        }

        calibrator.add_error_sample(error_info)

    print("\n分析误差数据并提供调整建议...")
    suggestions = calibrator.get_adjustment_suggestions()

    if suggestions['status'] == 'READY':
        print(f"\n校准分析报告:")
        print(f"  样本数: {suggestions['sample_count']}")
        print(f"  置信度: {suggestions['confidence']:.1%}")

        print(f"\n具体轴调整建议:")
        for adj in suggestions['adjustments']:
            if adj['needs_adjustment']:
                direction = "增加" if adj['suggested_adjustment_m'] > 0 else "减少"
                abs_amount = abs(adj['suggested_adjustment_m'])
                print(f"  {adj['axis']}轴: {direction} {abs_amount:.3f}m ({abs_amount*100:.1f}cm)")
                print(f"    当前误差: {adj['current_error_m']*100:.1f}cm")
                print(f"    误差标准差: {adj['std_error_m']*100:.1f}cm")
            else:
                print(f"  {adj['axis']}轴: 无需调整 (误差: {adj['current_error_m']*100:.1f}cm)")

        print(f"\n人类可读建议: {suggestions['human_readable']}")

        print("\nXML格式建议 (可用于MANUS Core配置文件):")
        print(suggestions['xml_suggestions'])
    else:
        print(f"数据不足: {suggestions['message']}")

    return calibrator

def demo_integration():
    """演示集成功能"""
    print("\n" + "="*60)
    print("3. 集成功能演示")
    print("="*60)

    print("集成功能包括:")
    print("  1. 数据接收 - 从C++客户端接收骨架和Tracker数据")
    print("  2. 旋转验证 - 分析旋转半径验证offset正确性")
    print("  3. 平移验证 - 分析平移直线度验证tracking稳定性")
    print("  4. 自动校准 - 提供具体的轴调整建议")
    print("  5. Session对齐 - 确保多次实验数据一致性")
    print("  6. 数据导出 - 保存测试结果用于分析")
    print("  7. 实时可视化 - 实时显示误差和性能指标")

    print("\n主要文件:")
    print("  manus_data_receiver.py - 数据接收器")
    print("  rotation_quality.py - 旋转质量分析")
    print("  translation_quality.py - 平移质量分析")
    print("  auto_calibration.py - 自动校准建议")
    print("  session_alignment.py - Session对齐")
    print("  realtime_visualization.py - 实时可视化")
    print("  offset_validation_demo.py - 基本验证演示")
    print("  full_calibration_demo.py - 完整集成演示")

    print("\n使用流程:")
    print("  1. 启动MANUS Core软件")
    print("  2. 运行C++客户端 (SDKMinimalClient_socket.exe)")
    print("  3. 运行Python演示: python full_calibration_demo.py")
    print("  4. 按照提示进行旋转测试(r)和平移测试(t)")
    print("  5. 查看校准建议(c)并调整offset参数")
    print("  6. 导出数据(e)用于进一步分析")

def main():
    print("="*60)
    print("MANUS Core 验证系统 - 核心功能演示")
    print("="*60)
    print("作者需求:")
    print("  1. 平移移动检测 [OK]")
    print("  2. 提示具体调整哪个轴多少距离 [OK]")
    print("  3. 自动标定辅助 [OK]")
    print("  4. Session对齐 [OK]")
    print("  5. 数据导出 [OK]")
    print("  6. 实时可视化 [OK]")
    print("="*60)

    # 演示平移质量检测
    demo_translation_quality()

    # 演示自动校准建议
    demo_auto_calibration()

    # 演示集成功能
    demo_integration()

    print("\n" + "="*60)
    print("演示完成")
    print("="*60)
    print("\n下一步:")
    print("  1. 运行完整演示: python full_calibration_demo.py")
    print("  2. 或运行基本验证: python offset_validation_demo.py")
    print("  3. 查看C++客户端修改: SDKMinimalClient_Windows/SDKMinimalClient_socket.cpp")
    print("\n注意: 如果遇到emoji编码问题，请修改print语句中的特殊字符")

if __name__ == "__main__":
    main()