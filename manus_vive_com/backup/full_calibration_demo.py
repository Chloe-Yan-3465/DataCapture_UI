#!/usr/bin/env python3
"""
完整校准演示
集成旋转质量、平移质量、自动校准建议的完整验证系统
"""

import time
import threading
import json
import numpy as np
from datetime import datetime
from typing import Dict, Any, Optional, List

from manus_data_receiver import ManusDataReceiver
from rotation_quality import RotationQualityAnalyzer
from translation_quality import TranslationQualityAnalyzer
from auto_calibration import AutoCalibration


def numpy_to_python(obj):
    """将numpy类型转换为Python原生类型，用于JSON序列化"""
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, (np.integer, np.int32, np.int64)):
        return int(obj)
    elif isinstance(obj, (np.floating, np.float32, np.float64)):
        return float(obj)
    elif isinstance(obj, np.bool_):
        return bool(obj)
    elif isinstance(obj, dict):
        return {k: numpy_to_python(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [numpy_to_python(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(numpy_to_python(item) for item in obj)
    else:
        return obj


class FullCalibrationDemo:
    """完整校准演示类"""

    def __init__(self,
                 host: str = "127.0.0.1",
                 port: int = 8888,
                 rotation_threshold_m: float = 0.01,
                 translation_linearity_threshold: float = 0.90,  # 降低阈值，更宽松
                 translation_deviation_threshold_m: float = 0.02,
                 calibration_threshold_m: float = 0.005):
        """
        初始化完整校准演示

        Args:
            host: TCP服务器主机
            port: TCP服务器端口
            rotation_threshold_m: 旋转半径阈值（米）
            translation_linearity_threshold: 平移线性度阈值
            translation_deviation_threshold_m: 平移偏差阈值（米）
            calibration_threshold_m: 校准调整阈值（米）
        """
        self.receiver = ManusDataReceiver(host=host, port=port)
        self.rotation_analyzer = RotationQualityAnalyzer(rotation_threshold_m=rotation_threshold_m)
        self.translation_analyzer = TranslationQualityAnalyzer(
            linearity_threshold=translation_linearity_threshold,
            deviation_threshold_m=translation_deviation_threshold_m
        )
        self.calibrator = AutoCalibration(
            adjustment_threshold_m=calibration_threshold_m,
            min_samples=50,
            confidence_level=0.95
        )

        # 状态变量
        self.frame_count = 0
        self.is_testing_translation = False
        self.is_testing_rotation = False
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_data = {
            'session_id': self.session_id,
            'start_time': datetime.now().isoformat(),
            'frames': [],
            'rotation_tests': [],
            'translation_tests': [],
            'calibration_suggestions': []
        }

        # 注册回调
        self.receiver.register_callback(self._data_callback)

        print("=" * 70)
        print("🎯 MANUS Core 完整校准验证系统")
        print("=" * 70)
        print(f"📅 Session ID: {self.session_id}")
        print(f"📏 旋转半径阈值: {rotation_threshold_m * 100:.1f}cm")
        print(f"📐 平移线性度阈值: {translation_linearity_threshold}")
        print(f"📏 平移偏差阈值: {translation_deviation_threshold_m * 100:.1f}cm")
        print(f"🎯 校准调整阈值: {calibration_threshold_m * 100:.1f}cm")
        print("=" * 70)
        print("\n🔧 可用命令 (在运行中输入):")
        print("  'r' - 开始/停止旋转测试")
        print("  't' - 开始/停止平移测试")
        print("  's' - 显示当前状态")
        print("  'c' - 显示校准建议")
        print("  'e' - 导出session数据")
        print("  'v' - 切换实时可视化")
        print("  'q' - 退出")
        print("=" * 70)

    def _data_callback(self, frame_data: Dict[str, Any]):
        """数据回调函数"""
        try:
            self.frame_count += 1

            # 更新旋转分析器
            self.rotation_analyzer.add_frame_data(frame_data)

            # 更新平移分析器（如果正在测试）
            if self.is_testing_translation:
                self.translation_analyzer.add_frame_data(frame_data)

            # 计算offset误差并更新校准器
            error_info = self.receiver.calculate_offset_error()
            if error_info:
                self.calibrator.add_error_sample(error_info)

                # 记录session数据
                if self.frame_count % 10 == 0:  # 每10帧记录一次以节省内存
                    frame_record = {
                        'frame': self.frame_count,
                        'timestamp': frame_data.get('timestamp', 0),
                        'error_vector': error_info['error_vector'],
                        'error_distance_m': error_info['distance_m'],
                        'wrist_position': error_info.get('wrist_position'),
                        'tracker_position': error_info.get('tracker_position')
                    }
                    self.session_data['frames'].append(frame_record)

            # 定期显示状态信息
            if self.frame_count % 100 == 0:
                self._display_status_update()
        except Exception as e:
            print(f"[ERROR] _data_callback 异常: {e}")
            import traceback
            traceback.print_exc()

    def _display_status_update(self):
        """显示状态更新"""
        print(f"\n📊 状态更新 [帧: {self.frame_count:05d}]")

        # offset误差统计
        if self.calibrator.sample_count > 0:
            suggestions = self.calibrator.get_adjustment_suggestions()
            if suggestions['status'] == 'READY':
                print(f"  🎯 Offset误差: 平均{suggestions['total_adjustment_magnitude_m']*100:.1f}cm "
                      f"(样本: {suggestions['sample_count']})")

        # 旋转测试状态
        if self.is_testing_rotation:
            rotation_ok, rotation_metrics = self.rotation_analyzer.is_offset_correct()
            status = "✅ 良好" if rotation_ok else "❌ 需调整"
            print(f"  🔄 旋转测试: {status} (半径: {rotation_metrics['mean_radius_m']*100:.1f}cm)")

        # 平移测试状态
        if self.is_testing_translation:
            translation_ok, translation_metrics = self.translation_analyzer.is_translation_good()
            status = "✅ 良好" if translation_ok else "❌ 需改进"
            print(f"  📏 平移测试: {status} (线性度: {translation_metrics['linearity']:.3f})")

    def start_rotation_test(self):
        """开始旋转测试"""
        if self.is_testing_rotation:
            print("🔄 旋转测试已在进行中")
            return

        self.is_testing_rotation = True
        self.rotation_analyzer.reset()
        print("🔄 开始旋转测试 - 请缓慢转动手腕")
        print("  保持手腕位置固定，只进行旋转动作")
        print("  完成后再次按 'r' 停止测试")

    def stop_rotation_test(self):
        """停止旋转测试并显示结果"""
        if not self.is_testing_rotation:
            print("⚠️  没有进行中的旋转测试")
            return

        self.is_testing_rotation = False
        print("\n" + "=" * 60)
        print("🔄 旋转测试结果")

        rotation_ok, rotation_metrics = self.rotation_analyzer.is_offset_correct()

        # 转换为Python原生类型
        rotation_metrics = numpy_to_python(rotation_metrics)

        if rotation_ok:
            print(f"✅ 旋转质量: 优秀")
            print(f"  平均半径: {rotation_metrics['mean_radius_m']*100:.1f}cm")
            print(f"  最大半径: {rotation_metrics['max_radius_m']*100:.1f}cm")
            print(f"  阈值: {rotation_metrics['threshold_m']*100:.1f}cm")
        else:
            print(f"❌ 旋转质量: 需要调整")
            print(f"  平均半径: {rotation_metrics['mean_radius_m']*100:.1f}cm")
            print(f"  最大半径: {rotation_metrics['max_radius_m']*100:.1f}cm")
            print(f"  阈值: {rotation_metrics['threshold_m']*100:.1f}cm")
            print(f"  💡 建议: 检查offset设置或重新标定")

        # 记录到session
        test_record = {
            'test_type': 'rotation',
            'timestamp': datetime.now().isoformat(),
            'frame_count': self.frame_count,
            'metrics': rotation_metrics,
            'is_acceptable': rotation_ok
        }
        self.session_data['rotation_tests'].append(test_record)

        print("=" * 60)

    def start_translation_test(self):
        """开始平移测试"""
        if self.is_testing_translation:
            print("📏 平移测试已在进行中")
            return

        self.is_testing_translation = True
        self.translation_analyzer.start_collection()
        print("📏 开始平移测试 - 请沿直线移动手腕")
        print("  保持手腕方向固定，只进行平移移动")
        print("  移动距离建议: 20-30cm")
        print("  完成后再次按 't' 停止测试")

    def stop_translation_test(self):
        """停止平移测试并显示结果"""
        if not self.is_testing_translation:
            print("⚠️  没有进行中的平移测试")
            return

        self.is_testing_translation = False
        result = self.translation_analyzer.stop_collection()

        # 转换为Python原生类型
        if result:
            result = numpy_to_python(result)

        print("\n" + "=" * 60)
        print("📏 平移测试结果")

        if result:
            print(f"📐 线性度: {result['linearity']:.3f} {'✅' if result['is_linear'] else '❌'}")
            print(f"📏 平均偏差: {result['avg_deviation_m']*100:.2f}cm {'✅' if result['is_stable'] else '❌'}")
            print(f"📏 最大偏差: {result['max_deviation_m']*100:.2f}cm")
            print(f"📍 方向向量: {result['direction']}")

            if result['is_linear'] and result['is_stable']:
                print("✅ 平移质量: 优秀 - 轨迹接近完美直线")
            elif result['is_linear']:
                print("⚠️  平移质量: 良好 - 轨迹直但存在抖动")
            elif result['is_stable']:
                print("⚠️  平移质量: 一般 - 轨迹不够直但tracking稳定")
            else:
                print("❌ 平移质量: 需要改进 - 轨迹弯曲且抖动明显")
        else:
            print("❌ 测试失败: 数据不足或收集被中断")

        # 获取诊断信息
        diag_info = self.translation_analyzer.get_diagnostic_info()
        if diag_info:
            diag_info = numpy_to_python(diag_info)
        if diag_info.get('status') == 'ANALYZED':
            print(f"📏 总移动距离: {diag_info['total_distance_m']*100:.1f}cm")
            print(f"📈 平均速度: {diag_info['average_speed_mps']*100:.1f}cm/s")
            print(f"💡 建议: {diag_info.get('recommendation', 'N/A')}")

        # 记录到session
        test_record = {
            'test_type': 'translation',
            'timestamp': datetime.now().isoformat(),
            'frame_count': self.frame_count,
            'result': result if result else None,
            'diagnostic_info': diag_info
        }
        self.session_data['translation_tests'].append(test_record)

        print("=" * 60)

    def show_calibration_suggestions(self):
        """显示校准建议"""
        print("\n" + "=" * 60)
        print("🎯 Offset校准建议")

        suggestions = self.calibrator.get_adjustment_suggestions()
        suggestions = numpy_to_python(suggestions)

        if suggestions['status'] == 'READY':
            print(f"📈 样本数: {suggestions['sample_count']}")
            print(f"🎯 置信度: {suggestions['confidence']:.1%}")
            print(f"📏 总调整幅度: {suggestions['total_adjustment_magnitude_m']*100:.1f}cm")

            print("\n📋 具体调整建议:")
            for adj in suggestions['adjustments']:
                if adj['needs_adjustment']:
                    sign = '+' if adj['suggested_adjustment_m'] >= 0 else ''
                    print(f"  {adj['axis']}轴: {sign}{adj['suggested_adjustment_m']:.4f}m "
                          f"({sign}{adj['adjustment_cm']:.1f}cm)")
                    print(f"    当前误差: {adj['current_error_m']:.4f}m ± {adj['std_error_m']:.4f}m")

            print(f"\n💡 人类可读建议: {suggestions['human_readable']}")

            print("\n📄 XML格式建议 (可添加到MANUS Core配置):")
            print(suggestions['xml_suggestions'])

            # 记录到session
            self.session_data['calibration_suggestions'].append({
                'timestamp': datetime.now().isoformat(),
                'frame_count': self.frame_count,
                'suggestions': suggestions
            })

        elif suggestions['status'] == 'INSUFFICIENT_DATA':
            print(f"📊 数据不足: {suggestions['message']}")
            print("💡 建议: 多移动一会儿以收集更多数据")

        print("=" * 60)

    def export_session_data(self, filename: Optional[str] = None):
        """导出session数据到JSON文件"""
        if not filename:
            filename = f"manus_calibration_session_{self.session_id}.json"

        # 更新结束时间
        self.session_data['end_time'] = datetime.now().isoformat()
        self.session_data['total_frames'] = self.frame_count
        self.session_data['final_calibration_suggestions'] = numpy_to_python(
            self.calibrator.get_adjustment_suggestions()
        )

        try:
            # 转换为Python原生类型以进行JSON序列化
            session_data_converted = numpy_to_python(self.session_data)
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(session_data_converted, f, indent=2, ensure_ascii=False)

            print(f"✅ Session数据已导出到: {filename}")
            print(f"📊 总帧数: {self.frame_count}")
            print(f"📈 记录的数据点: {len(self.session_data['frames'])}")
            print(f"🔄 旋转测试次数: {len(self.session_data['rotation_tests'])}")
            print(f"📏 平移测试次数: {len(self.session_data['translation_tests'])}")

        except Exception as e:
            print(f"❌ 导出失败: {e}")

    def show_current_status(self):
        """显示当前状态"""
        print("\n" + "=" * 60)
        print("📊 当前系统状态")
        print("=" * 60)

        print(f"📅 Session ID: {self.session_id}")
        print(f"📈 总帧数: {self.frame_count}")
        print(f"📊 误差样本数: {self.calibrator.sample_count}")

        # 旋转测试状态
        if self.is_testing_rotation:
            print("🔄 旋转测试: 进行中")
        elif self.session_data['rotation_tests']:
            last_test = self.session_data['rotation_tests'][-1]
            status = "✅ 通过" if last_test['is_acceptable'] else "❌ 失败"
            print(f"🔄 上次旋转测试: {status}")

        # 平移测试状态
        if self.is_testing_translation:
            print("📏 平移测试: 进行中")
        elif self.session_data['translation_tests']:
            last_test = self.session_data['translation_tests'][-1]
            if last_test['result']:
                status = "✅ 良好" if last_test['result']['is_linear'] and last_test['result']['is_stable'] else "⚠️  一般"
                print(f"📏 上次平移测试: {status}")

        # 最新校准建议
        if self.calibrator.sample_count >= 10:
            suggestions = self.calibrator.get_adjustment_suggestions()
            if suggestions['status'] == 'READY':
                needs_adjustment = any(adj['needs_adjustment'] for adj in suggestions['adjustments'])
                if needs_adjustment:
                    print("🎯 校准状态: 需要调整")
                    print(f"  建议: {suggestions['human_readable']}")
                else:
                    print("🎯 校准状态: ✅ 良好")
            else:
                print(f"🎯 校准状态: {suggestions['message']}")
        else:
            print("🎯 校准状态: 数据不足")

        print("=" * 60)

    def run_interactive(self):
        """运行交互式演示"""
        print("[LAUNCH] 启动完整校准验证系统...")
        print("[1] 确保MANUS Core正在运行")
        print("[2] 确保Vive Tracker已连接并配置")
        print("[3] 确保C++客户端已编译并运行")
        print("4️⃣ 使用以下命令控制测试:")
        print("   r=旋转测试, t=平移测试, s=状态, c=校准建议, e=导出, q=退出")
        print("=" * 70)

        # 启动数据接收器（在后台线程）
        receiver_thread = threading.Thread(target=self.receiver.start, daemon=True)
        receiver_thread.start()

        # 等待连接
        time.sleep(2)

        try:
            # 主交互循环
            while self.receiver.running:
                if not receiver_thread.is_alive():
                    print("⚠️  数据接收器线程已停止")
                    break

                # 检查用户输入
                try:
                    user_input = input("\n请输入命令 (r/t/s/c/e/v/q): ").strip().lower()

                    if user_input == 'q':
                        print("👋 退出系统...")
                        break
                    elif user_input == 'r':
                        if self.is_testing_rotation:
                            self.stop_rotation_test()
                        else:
                            self.start_rotation_test()
                    elif user_input == 't':
                        if self.is_testing_translation:
                            self.stop_translation_test()
                        else:
                            self.start_translation_test()
                    elif user_input == 's':
                        self.show_current_status()
                    elif user_input == 'c':
                        self.show_calibration_suggestions()
                    elif user_input == 'e':
                        self.export_session_data()
                    elif user_input == 'v':
                        print("📊 实时可视化功能待实现")
                    elif user_input:
                        print(f"❓ 未知命令: {user_input}")
                        print("可用命令: r=旋转测试, t=平移测试, s=状态, c=校准建议, e=导出, v=可视化, q=退出")

                except EOFError:
                    # Ctrl+D 处理
                    print("\n👋 输入结束，退出系统...")
                    break
                except KeyboardInterrupt:
                    print("\n👋 用户中断")
                    break

        except Exception as e:
            print(f"❌ 运行错误: {e}")
        finally:
            self.stop()

    def stop(self):
        """停止系统"""
        self.receiver.stop()

        # 显示最终摘要
        print("\n" + "=" * 70)
        print("📊 最终系统摘要")
        print("=" * 70)

        print(f"📅 Session ID: {self.session_id}")
        print(f"📈 总帧数: {self.frame_count}")
        print(f"📊 误差样本数: {self.calibrator.sample_count}")

        # 旋转测试汇总
        if self.session_data['rotation_tests']:
            passed = sum(1 for t in self.session_data['rotation_tests'] if t['is_acceptable'])
            total = len(self.session_data['rotation_tests'])
            print(f"🔄 旋转测试: {passed}/{total} 通过")

        # 平移测试汇总
        if self.session_data['translation_tests']:
            good_tests = 0
            for test in self.session_data['translation_tests']:
                if test.get('result') and test['result'].get('is_linear') and test['result'].get('is_stable'):
                    good_tests += 1
            total = len(self.session_data['translation_tests'])
            print(f"📏 平移测试: {good_tests}/{total} 良好")

        # 最终校准建议
        if self.calibrator.sample_count >= 10:
            suggestions = self.calibrator.get_adjustment_suggestions()
            suggestions = numpy_to_python(suggestions)
            if suggestions['status'] == 'READY':
                print(f"🎯 最终校准建议: {suggestions['human_readable']}")

        # 导出最终数据
        export_file = f"manus_calibration_final_{self.session_id}.json"
        self.export_session_data(export_file)

        print("=" * 70)
        print("🎯 系统运行完成")


# ==================== 主程序 ====================

if __name__ == "__main__":
    # 创建并运行完整校准演示
    demo = FullCalibrationDemo(
        host="127.0.0.1",
        port=8888,
        rotation_threshold_m=0.01,          # 1cm
        translation_linearity_threshold=0.90,  # 降低阈值，更宽松
        translation_deviation_threshold_m=0.02,  # 2cm
        calibration_threshold_m=0.005       # 5mm
    )

    # 运行交互式演示
    demo.run_interactive()