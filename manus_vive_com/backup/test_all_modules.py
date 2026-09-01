#!/usr/bin/env python3
"""
测试所有模块的导入和基本功能
"""

import sys
import importlib

def test_module_import(module_name, class_names=None):
    """测试模块导入"""
    try:
        module = importlib.import_module(module_name)
        print(f"[OK] {module_name}: 导入成功")

        if class_names:
            for class_name in class_names:
                if hasattr(module, class_name):
                    print(f"   [+] 类 '{class_name}' 存在")
                else:
                    print(f"   [-] 类 '{class_name}' 不存在")
        return True
    except Exception as e:
        print(f"[ERROR] {module_name}: 导入失败 - {e}")
        return False

def main():
    print("=== 测试所有模块导入 ===")
    print("=" * 60)

    modules_to_test = [
        ("manus_data_receiver", ["ManusDataReceiver"]),
        ("rotation_quality", ["RotationQualityAnalyzer"]),
        ("translation_quality", ["TranslationQualityAnalyzer"]),
        ("auto_calibration", ["AutoCalibration"]),
        ("session_alignment", ["SessionAlignment"]),
        ("realtime_visualization", ["ConsoleVisualization"]),
        ("offset_validation_demo", ["OffsetValidationDemo"]),
        ("full_calibration_demo", ["FullCalibrationDemo"])
    ]

    success_count = 0
    total_count = len(modules_to_test)

    for module_name, class_names in modules_to_test:
        if test_module_import(module_name, class_names):
            success_count += 1

    print("\n" + "=" * 60)
    print(f"[STATS] 测试结果: {success_count}/{total_count} 个模块导入成功")

    if success_count == total_count:
        print("[OK] 所有模块导入测试通过!")
    else:
        print("[WARN]  部分模块导入失败")

    # 测试模块间依赖
    print("\n[LINK] 测试模块间依赖关系...")
    try:
        from manus_data_receiver import ManusDataReceiver
        from rotation_quality import RotationQualityAnalyzer
        from translation_quality import TranslationQualityAnalyzer
        from auto_calibration import AutoCalibration

        print("[OK] 核心模块依赖关系正常")

        # 测试类实例化
        print("\n[TEST] 测试类实例化...")

        try:
            analyzer1 = RotationQualityAnalyzer(rotation_threshold_m=0.01)
            print("[OK] RotationQualityAnalyzer: 实例化成功")
        except Exception as e:
            print(f"[ERROR] RotationQualityAnalyzer: 实例化失败 - {e}")

        try:
            analyzer2 = TranslationQualityAnalyzer(
                linearity_threshold=0.95,
                deviation_threshold_m=0.02
            )
            print("[OK] TranslationQualityAnalyzer: 实例化成功")
        except Exception as e:
            print(f"[ERROR] TranslationQualityAnalyzer: 实例化失败 - {e}")

        try:
            calibrator = AutoCalibration(
                adjustment_threshold_m=0.005,
                min_samples=50,
                confidence_level=0.95
            )
            print("[OK] AutoCalibration: 实例化成功")
        except Exception as e:
            print(f"[ERROR] AutoCalibration: 实例化失败 - {e}")

        try:
            from session_alignment import SessionAlignment
            aligner = SessionAlignment()
            print("[OK] SessionAlignment: 实例化成功")
        except Exception as e:
            print(f"[ERROR] SessionAlignment: 实例化失败 - {e}")

        try:
            from realtime_visualization import ConsoleVisualization
            viz = ConsoleVisualization(update_interval_frames=10)
            print("[OK] ConsoleVisualization: 实例化成功")
        except Exception as e:
            print(f"[ERROR] ConsoleVisualization: 实例化失败 - {e}")

    except ImportError as e:
        print(f"[ERROR] 模块依赖错误: {e}")

    print("\n[TARGET] 系统功能总结:")
    print("1. 📡 数据接收: ManusDataReceiver - 从C++客户端接收数据")
    print("2. 🔄 旋转验证: RotationQualityAnalyzer - 验证offset旋转半径")
    print("3. 📏 平移验证: TranslationQualityAnalyzer - 验证平移直线度")
    print("4. [TARGET] 自动校准: AutoCalibration - 提供轴调整建议")
    print("5. [LINK] Session对齐: SessionAlignment - 跨session数据一致性")
    print("6. [STATS] 实时可视化: ConsoleVisualization - 控制台实时监控")
    print("7. 🎮 完整演示: FullCalibrationDemo - 集成所有功能的交互式演示")

    print("\n[TIP] 使用建议:")
    print("1. 首先运行: python full_calibration_demo.py")
    print("2. 按照提示进行旋转测试(r)和平移测试(t)")
    print("3. 查看校准建议(c)和应用调整")
    print("4. 导出数据(e)用于进一步分析")

if __name__ == "__main__":
    main()