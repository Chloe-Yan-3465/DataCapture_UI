#!/usr/bin/env python3
"""
调试版本 - 添加详细日志，修复已知问题
"""

import time
import json
import numpy as np
from datetime import datetime
from typing import Dict, Any, Optional

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


class DebugCalibrationDemo:
    """调试版本，添加详细日志"""

    def __init__(self):
        self.receiver = ManusDataReceiver(host="127.0.0.1", port=8888)
        self.rotation_analyzer = RotationQualityAnalyzer(rotation_threshold_m=0.01)
        self.translation_analyzer = TranslationQualityAnalyzer(
            linearity_threshold=0.90,  # 降低阈值，更宽松
            deviation_threshold_m=0.02
        )
        self.calibrator = AutoCalibration(
            adjustment_threshold_m=0.005,
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

        # 调试信息
        self.debug_info = {
            'last_frame_data': None,
            'wrist_positions': [],
            'tracker_positions': [],
            'error_history': []
        }

        # 注册回调
        self.receiver.register_callback(self._data_callback)

        print("=" * 70)
        print("[DEBUG] MANUS Core 调试版本")
        print("=" * 70)
        print(f"[DEBUG] Session ID: {self.session_id}")
        print("=" * 70)

    def _data_callback(self, frame_data: Dict[str, Any]):
        """数据回调函数 - 添加调试信息"""
        try:
            self.frame_count += 1

            # 保存最后几帧用于调试
            if self.frame_count % 10 == 0:
                self.debug_info['last_frame_data'] = {
                    'frame': self.frame_count,
                    'has_skeletons': 'skeletons' in frame_data and bool(frame_data['skeletons']),
                    'has_trackers': 'trackers' in frame_data and bool(frame_data['trackers']),
                    'timestamp': frame_data.get('timestamp')
                }

            # 获取手腕和Tracker位置用于调试
            wrist_pos = self.receiver.get_wrist_position()
            tracker_pos = self.receiver.get_tracker_position()

            if wrist_pos:
                self.debug_info['wrist_positions'].append(wrist_pos)
                if len(self.debug_info['wrist_positions']) > 100:
                    self.debug_info['wrist_positions'] = self.debug_info['wrist_positions'][-100:]

            if tracker_pos:
                self.debug_info['tracker_positions'].append(tracker_pos)
                if len(self.debug_info['tracker_positions']) > 100:
                    self.debug_info['tracker_positions'] = self.debug_info['tracker_positions'][-100:]

            # 每100帧显示调试信息
            if self.frame_count % 100 == 0:
                self._show_debug_info(wrist_pos, tracker_pos)

            # 更新旋转分析器
            self.rotation_analyzer.add_frame_data(frame_data)

            # 更新平移分析器（如果正在测试）
            if self.is_testing_translation:
                self.translation_analyzer.add_frame_data(frame_data)

            # 计算offset误差并更新校准器
            error_info = self.receiver.calculate_offset_error()
            if error_info:
                self.calibrator.add_error_sample(error_info)
                self.debug_info['error_history'].append(error_info)

                # 记录session数据（使用转换函数）
                if self.frame_count % 10 == 0:
                    frame_record = {
                        'frame': self.frame_count,
                        'timestamp': frame_data.get('timestamp', 0),
                        'error_vector': error_info['error_vector'],
                        'error_distance_m': float(error_info['distance_m']),
                        'wrist_position': error_info.get('wrist_position'),
                        'tracker_position': error_info.get('tracker_position')
                    }
                    # 转换为Python原生类型
                    frame_record = numpy_to_python(frame_record)
                    self.session_data['frames'].append(frame_record)
            elif self.frame_count % 100 == 0 and wrist_pos:
                # 如果没有Tracker数据，记录警告（每100帧）
                print(f"[DEBUG] 警告: 没有Tracker数据，无法计算offset误差")
                print(f"[DEBUG] 请确保C++客户端配置了Tracker设备并发送Tracker数据")
        except Exception as e:
            print(f"[DEBUG] _data_callback 异常: {e}")
            import traceback
            traceback.print_exc()

    def _show_debug_info(self, wrist_pos, tracker_pos):
        """显示调试信息"""
        print(f"\n[DEBUG] 帧: {self.frame_count}")
        print(f"[DEBUG] 手腕位置: {wrist_pos}")
        print(f"[DEBUG] Tracker位置: {tracker_pos}")

        if wrist_pos and tracker_pos:
            error_x = tracker_pos[0] - wrist_pos[0]
            error_y = tracker_pos[1] - wrist_pos[1]
            error_z = tracker_pos[2] - wrist_pos[2]
            distance = (error_x**2 + error_y**2 + error_z**2)**0.5
            print(f"[DEBUG] Offset误差: {distance*100:.1f}cm ({error_x*100:.1f}, {error_y*100:.1f}, {error_z*100:.1f})cm")
        elif wrist_pos and not tracker_pos:
            print(f"[DEBUG] 警告: Tracker数据缺失，无法计算offset误差")
            print(f"[DEBUG] 请检查C++客户端是否配置了Vive Tracker设备")

        print(f"[DEBUG] 误差样本数: {self.calibrator.sample_count}")
        print(f"[DEBUG] 手腕位置样本数: {len(self.debug_info['wrist_positions'])}")
        print(f"[DEBUG] Tracker位置样本数: {len(self.debug_info['tracker_positions'])}")

    def start_translation_test(self):
        """开始平移测试"""
        print(f"[DEBUG] start_translation_test() 被调用，当前is_testing_translation={self.is_testing_translation}")
        if self.is_testing_translation:
            print("[DEBUG] 平移测试已在进行中")
            return

        self.is_testing_translation = True
        print("[DEBUG] 设置is_testing_translation=True")
        self.translation_analyzer.start_collection()
        print("[DEBUG] 开始平移测试")
        print("[DEBUG] 请沿直线移动手腕，保持手腕方向固定")
        print("[DEBUG] 移动距离建议: 20-30cm")
        print("[DEBUG] 完成后再次按 't' 停止测试")

    def stop_translation_test(self):
        """停止平移测试并显示结果"""
        print(f"[DEBUG] stop_translation_test() 被调用，当前is_testing_translation={self.is_testing_translation}")
        if not self.is_testing_translation:
            print("[DEBUG] 没有进行中的平移测试")
            return

        self.is_testing_translation = False
        print("[DEBUG] 设置is_testing_translation=False")
        try:
            result = self.translation_analyzer.stop_collection()
        except Exception as e:
            print(f"[DEBUG] stop_collection() 抛出异常: {e}")
            import traceback
            traceback.print_exc()
            result = None

        print("\n" + "=" * 60)
        print("[DEBUG] 平移测试结果")

        if result:
            # 转换为Python原生类型
            result = numpy_to_python(result)

            print(f"[DEBUG] 线性度: {result['linearity']:.3f}")
            print(f"[DEBUG] 平均偏差: {result['avg_deviation_m']*100:.2f}cm")
            print(f"[DEBUG] 最大偏差: {result['max_deviation_m']*100:.2f}cm")
            print(f"[DEBUG] 方向向量: {result['direction']}")
            print(f"[DEBUG] 是否线性: {result['is_linear']}")
            print(f"[DEBUG] 是否稳定: {result['is_stable']}")

            if result['is_linear'] and result['is_stable']:
                print("[DEBUG] 平移质量: 优秀 - 轨迹接近完美直线")
            elif result['is_linear']:
                print("[DEBUG] 平移质量: 良好 - 轨迹直但存在抖动")
            elif result['is_stable']:
                print("[DEBUG] 平移质量: 一般 - 轨迹不够直但tracking稳定")
            else:
                print("[DEBUG] 平移质量: 需要改进 - 轨迹弯曲且抖动明显")
        else:
            print("[DEBUG] 测试失败: 数据不足或收集被中断")

        # 获取诊断信息
        diag_info = self.translation_analyzer.get_diagnostic_info()
        diag_info = numpy_to_python(diag_info)

        if diag_info.get('status') == 'ANALYZED':
            print(f"[DEBUG] 总移动距离: {diag_info['total_distance_m']*100:.1f}cm")
            print(f"[DEBUG] 平均速度: {diag_info['average_speed_mps']*100:.1f}cm/s")
            print(f"[DEBUG] 建议: {diag_info.get('recommendation', 'N/A')}")

        # 记录到session
        test_record = {
            'test_type': 'translation',
            'timestamp': datetime.now().isoformat(),
            'frame_count': self.frame_count,
            'result': result if result else None,
            'diagnostic_info': diag_info
        }
        test_record = numpy_to_python(test_record)
        self.session_data['translation_tests'].append(test_record)

        print("=" * 60)

    def start_rotation_test(self):
        """开始旋转测试"""
        print(f"[DEBUG] start_rotation_test() 被调用，当前is_testing_rotation={self.is_testing_rotation}")
        if self.is_testing_rotation:
            print("[DEBUG] 旋转测试已在进行中")
            return

        self.is_testing_rotation = True
        print("[DEBUG] 设置is_testing_rotation=True")
        self.rotation_analyzer.reset()
        print("[DEBUG] 开始旋转测试 - 请缓慢转动手腕")
        print("[DEBUG] 保持手腕位置固定，只进行旋转动作")
        print("[DEBUG] 完成后再次按 'r' 停止测试")

    def stop_rotation_test(self):
        """停止旋转测试并显示结果"""
        print(f"[DEBUG] stop_rotation_test() 被调用，当前is_testing_rotation={self.is_testing_rotation}")
        if not self.is_testing_rotation:
            print("[DEBUG] 没有进行中的旋转测试")
            return

        self.is_testing_rotation = False
        print("[DEBUG] 设置is_testing_rotation=False")
        try:
            rotation_ok, rotation_metrics = self.rotation_analyzer.is_offset_correct()
        except Exception as e:
            print(f"[DEBUG] is_offset_correct() 抛出异常: {e}")
            import traceback
            traceback.print_exc()
            rotation_ok = False
            rotation_metrics = {'error': str(e)}

        # 转换为Python原生类型
        rotation_metrics = numpy_to_python(rotation_metrics)

        print("\n" + "=" * 60)
        print("[DEBUG] 旋转测试结果")

        if rotation_ok:
            print("[DEBUG] 旋转质量: 优秀")
            print(f"[DEBUG] 平均半径: {rotation_metrics['mean_radius_m']*100:.1f}cm")
            print(f"[DEBUG] 最大半径: {rotation_metrics['max_radius_m']*100:.1f}cm")
            print(f"[DEBUG] 阈值: {rotation_metrics['threshold_m']*100:.1f}cm")
        else:
            print("[DEBUG] 旋转质量: 需要调整")
            print(f"[DEBUG] 平均半径: {rotation_metrics['mean_radius_m']*100:.1f}cm")
            print(f"[DEBUG] 最大半径: {rotation_metrics['max_radius_m']*100:.1f}cm")
            print(f"[DEBUG] 阈值: {rotation_metrics['threshold_m']*100:.1f}cm")
            print("[DEBUG] 建议: 检查offset设置或重新标定")

        # 记录到session
        test_record = {
            'test_type': 'rotation',
            'timestamp': datetime.now().isoformat(),
            'frame_count': self.frame_count,
            'metrics': rotation_metrics,
            'is_acceptable': bool(rotation_ok)  # 确保是Python布尔类型
        }
        test_record = numpy_to_python(test_record)
        self.session_data['rotation_tests'].append(test_record)

        print("=" * 60)

    def show_calibration_suggestions(self):
        """显示校准建议"""
        print("\n" + "=" * 60)
        print("[DEBUG] Offset校准建议")

        suggestions = self.calibrator.get_adjustment_suggestions()
        suggestions = numpy_to_python(suggestions)

        if suggestions['status'] == 'READY':
            print(f"[DEBUG] 样本数: {suggestions['sample_count']}")
            print(f"[DEBUG] 置信度: {suggestions['confidence']:.1%}")
            print(f"[DEBUG] 总调整幅度: {suggestions['total_adjustment_magnitude_m']*100:.1f}cm")

            print("\n[DEBUG] 具体调整建议:")
            for adj in suggestions['adjustments']:
                if adj['needs_adjustment']:
                    sign = '+' if adj['suggested_adjustment_m'] >= 0 else ''
                    print(f"[DEBUG]   {adj['axis']}轴: {sign}{adj['suggested_adjustment_m']:.4f}m "
                          f"({sign}{adj['adjustment_cm']:.1f}cm)")
                    print(f"[DEBUG]     当前误差: {adj['current_error_m']:.4f}m ± {adj['std_error_m']:.4f}m")

            print(f"\n[DEBUG] 人类可读建议: {suggestions['human_readable']}")

            print("\n[DEBUG] XML格式建议:")
            print(suggestions['xml_suggestions'])

            # 记录到session
            self.session_data['calibration_suggestions'].append({
                'timestamp': datetime.now().isoformat(),
                'frame_count': self.frame_count,
                'suggestions': suggestions
            })

        elif suggestions['status'] == 'INSUFFICIENT_DATA':
            print(f"[DEBUG] 数据不足: {suggestions['message']}")
            print("[DEBUG] 建议: 多移动一会儿以收集更多数据")

        print("=" * 60)

    def export_session_data(self, filename: Optional[str] = None):
        """导出session数据到JSON文件"""
        # 确保calibration_results文件夹存在
        import os
        os.makedirs("calibration_results", exist_ok=True)

        if not filename:
            filename = f"calibration_results/manus_calibration_session_{self.session_id}.json"
        else:
            # 如果用户提供了文件名，但没有路径，则添加到calibration_results文件夹
            if not os.path.isabs(filename) and not ('/' in filename or '\\' in filename):
                filename = os.path.join("calibration_results", filename)

        # 更新结束时间
        self.session_data['end_time'] = datetime.now().isoformat()
        self.session_data['total_frames'] = self.frame_count

        # 获取校准建议并转换
        suggestions = self.calibrator.get_adjustment_suggestions()
        self.session_data['final_calibration_suggestions'] = numpy_to_python(suggestions)

        try:
            # 转换整个session数据
            session_data_converted = numpy_to_python(self.session_data)

            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(session_data_converted, f, indent=2, ensure_ascii=False)

            print(f"[DEBUG] Session数据已导出到: {filename}")
            print(f"[DEBUG] 总帧数: {self.frame_count}")
            print(f"[DEBUG] 记录的数据点: {len(self.session_data['frames'])}")
            print(f"[DEBUG] 旋转测试次数: {len(self.session_data['rotation_tests'])}")
            print(f"[DEBUG] 平移测试次数: {len(self.session_data['translation_tests'])}")

        except Exception as e:
            print(f"[DEBUG] 导出失败: {e}")
            import traceback
            traceback.print_exc()

    def run_interactive(self):
        """运行交互式演示"""
        print("[DEBUG] 启动调试版本...")
        print("[DEBUG] 启动顺序:")
        print("[DEBUG]  1. 先运行此Python程序")
        print("[DEBUG]  2. 再运行C++客户端")
        print("[DEBUG]  3. 等待连接建立后按命令操作")
        print("[DEBUG] 命令: r=旋转测试, t=平移测试, c=校准建议, e=导出, q=退出")
        print("=" * 70)

        # 启动数据接收器（在后台线程）
        import threading
        receiver_thread = threading.Thread(target=self.receiver.start, daemon=True)
        receiver_thread.start()

        # 等待连接
        print("[DEBUG] 等待C++客户端连接...")
        time.sleep(3)

        # 检查连接状态 - 给接收器更多时间启动
        time.sleep(2)  # 额外等待2秒
        print("[DEBUG] 接收器启动检查完成，等待客户端连接...")

        try:
            # Windows上的非阻塞输入
            try:
                import msvcrt
                has_msvcrt = True
            except ImportError:
                has_msvcrt = False
                print("[DEBUG] 警告: 无法导入msvcrt，使用标准input()")

            loop_count = 0
            last_input_time = time.time()
            # 主交互循环
            while self.receiver.running:
                loop_count += 1
                if loop_count % 100 == 0:  # 减少日志频率
                    print(f"[DEBUG] 主循环第{loop_count}次迭代，receiver.running={self.receiver.running}")
                # 注意：receiver_thread是启动接收器的线程，不是接收数据线程
                # 接收数据线程在接收器内部，通过self.receiver.running检查状态

                # 非阻塞检查用户输入
                user_input = None
                if has_msvcrt:
                    if msvcrt.kbhit():  # 检查是否有按键
                        try:
                            key = msvcrt.getch().decode('utf-8').lower()
                            if key in ['r', 't', 'c', 'e', 'q']:
                                user_input = key
                                print(f"\n[DEBUG] 检测到按键: {key}")
                        except:
                            pass
                else:
                    # 每10秒检查一次输入（减少阻塞时间）
                    if time.time() - last_input_time > 10:
                        try:
                            # 使用带超时的input
                            import sys
                            print("\n[DEBUG] 等待命令输入 (r/t/c/e/q, 按Enter继续): ", end='', flush=True)
                            # 简单方法：只读取一个字符
                            user_input = sys.stdin.read(1).strip().lower()
                            if user_input:
                                print(f"[DEBUG] 收到命令: {user_input}")
                        except:
                            pass
                        last_input_time = time.time()

                if user_input:
                    if user_input == 'q':
                        print("[DEBUG] 退出系统...")
                        break
                    elif user_input == 'r':
                        print(f"[DEBUG] 按下'r'，当前is_testing_rotation={self.is_testing_rotation}")
                        if self.is_testing_rotation:
                            self.stop_rotation_test()
                        else:
                            self.start_rotation_test()
                    elif user_input == 't':
                        print(f"[DEBUG] 按下't'，当前is_testing_translation={self.is_testing_translation}")
                        if self.is_testing_translation:
                            self.stop_translation_test()
                        else:
                            self.start_translation_test()
                    elif user_input == 'c':
                        self.show_calibration_suggestions()
                    elif user_input == 'e':
                        self.export_session_data()
                    elif user_input:
                        print(f"[DEBUG] 未知命令: {user_input}")
                        print("[DEBUG] 可用命令: r=旋转测试, t=平移测试, c=校准建议, e=导出, q=退出")

                # 短暂休眠，减少CPU使用
                time.sleep(0.1)

        except Exception as e:
            print(f"[DEBUG] 运行错误: {e}")
            import traceback
            traceback.print_exc()
        finally:
            print("[DEBUG] 进入finally块，准备停止系统")
            self.stop()

    def stop(self):
        """停止系统"""
        print("[DEBUG] 调用stop()方法")
        self.receiver.stop()

        # 显示最终摘要
        print("\n" + "=" * 70)
        print("[DEBUG] 最终系统摘要")
        print("=" * 70)

        print(f"[DEBUG] Session ID: {self.session_id}")
        print(f"[DEBUG] 总帧数: {self.frame_count}")
        print(f"[DEBUG] 误差样本数: {self.calibrator.sample_count}")

        # 旋转测试汇总
        if self.session_data['rotation_tests']:
            passed = sum(1 for t in self.session_data['rotation_tests'] if t.get('is_acceptable', False))
            total = len(self.session_data['rotation_tests'])
            print(f"[DEBUG] 旋转测试: {passed}/{total} 通过")

        # 平移测试汇总
        if self.session_data['translation_tests']:
            good_tests = 0
            for test in self.session_data['translation_tests']:
                if test.get('result') and test['result'].get('is_linear') and test['result'].get('is_stable'):
                    good_tests += 1
            total = len(self.session_data['translation_tests'])
            print(f"[DEBUG] 平移测试: {good_tests}/{total} 良好")

        # 最终校准建议
        if self.calibrator.sample_count >= 10:
            suggestions = self.calibrator.get_adjustment_suggestions()
            suggestions = numpy_to_python(suggestions)
            if suggestions['status'] == 'READY':
                print(f"[DEBUG] 最终校准建议: {suggestions['human_readable']}")

        # 导出最终数据
        export_file = f"calibration_results/manus_calibration_final_{self.session_id}.json"
        self.export_session_data(export_file)

        print("=" * 70)
        print("[DEBUG] 系统运行完成")


if __name__ == "__main__":
    demo = DebugCalibrationDemo()
    demo.run_interactive()