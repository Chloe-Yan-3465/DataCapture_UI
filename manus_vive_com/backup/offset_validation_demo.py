#!/usr/bin/env python3
"""
MANUS Core offset验证演示
集成骨架数据、Tracker数据和旋转质量分析
"""

import time
import threading
from typing import Dict, Any, Optional

from manus_data_receiver import ManusDataReceiver
from rotation_quality import RotationQualityAnalyzer


class OffsetValidationDemo:
    """Offset验证演示类"""

    def __init__(self):
        """初始化验证演示"""
        self.receiver = ManusDataReceiver(host="127.0.0.1", port=8888)
        self.rotation_analyzer = RotationQualityAnalyzer(rotation_threshold_m=0.01)

        # 统计信息
        self.frame_count = 0
        self.error_history = []  # 存储offset误差历史
        self.max_history = 1000

        # 注册回调函数
        self.receiver.register_callback(self._validation_callback)

        print("🎯 MANUS Core offset验证演示")
        print(f"📏 旋转质量阈值: {self.rotation_analyzer.rotation_threshold * 100:.1f}cm")
        print("=" * 60)

    def _validation_callback(self, frame_data: Dict[str, Any]):
        """验证回调函数"""
        self.frame_count += 1

        # 处理骨架数据用于旋转质量分析
        self.rotation_analyzer.add_frame_data(frame_data)

        # 计算当前帧的offset误差
        error_info = self.receiver.calculate_offset_error()

        if error_info:
            self.error_history.append(error_info)

            # 限制历史数据长度
            if len(self.error_history) > self.max_history:
                self.error_history = self.error_history[-self.max_history:]

            # 每10帧显示一次信息
            if self.frame_count % 10 == 0:
                self._display_frame_info(frame_data, error_info)

        # 每50帧进行一次综合验证
        if self.frame_count % 50 == 0:
            self._perform_comprehensive_validation()

    def _display_frame_info(self, frame_data: Dict[str, Any], error_info: Dict[str, Any]):
        """显示帧信息"""
        frame_num = frame_data.get('frame', 0)

        # 获取手腕和Tracker位置
        wrist_pos = self.receiver.get_wrist_position()
        tracker_pos = self.receiver.get_tracker_position()

        if wrist_pos and tracker_pos:
            print(f"📊 帧 {frame_num:04d}:")
            print(f"  👋 手腕: [{wrist_pos[0]:.3f}, {wrist_pos[1]:.3f}, {wrist_pos[2]:.3f}]")
            print(f"  🎯 Tracker: [{tracker_pos[0]:.3f}, {tracker_pos[1]:.3f}, {tracker_pos[2]:.3f}]")
            print(f"  📏 误差距离: {error_info['distance_m']:.3f}m")
            print(f"  📐 误差向量: [{error_info['error_vector'][0]:.3f}, "
                  f"{error_info['error_vector'][1]:.3f}, {error_info['error_vector'][2]:.3f}]")

    def _perform_comprehensive_validation(self):
        """执行综合验证"""
        print("\n" + "=" * 60)
        print("🔍 综合验证报告")

        # 旋转质量分析
        rotation_ok, rotation_metrics = self.rotation_analyzer.is_offset_correct()

        if rotation_ok:
            print(f"✅ 旋转质量: 优秀 (半径: {rotation_metrics['mean_radius_m']*100:.1f}cm)")
        else:
            print(f"❌ 旋转质量: 需要调整 (半径: {rotation_metrics['mean_radius_m']*100:.1f}cm)")

        # offset误差分析
        if self.error_history:
            avg_distance = sum(e['distance_m'] for e in self.error_history) / len(self.error_history)
            max_distance = max(e['distance_m'] for e in self.error_history)
            min_distance = min(e['distance_m'] for e in self.error_history)

            print(f"📏 Offset误差统计:")
            print(f"   平均: {avg_distance:.3f}m ({avg_distance*100:.1f}cm)")
            print(f"   最大: {max_distance:.3f}m ({max_distance*100:.1f}cm)")
            print(f"   最小: {min_distance:.3f}m ({min_distance*100:.1f}cm)")

            # 提供调整建议
            if avg_distance < 0.01:  # < 1cm
                print("🎯 建议: Offset设置良好，无需调整")
            elif avg_distance < 0.03:  # < 3cm
                print("🎯 建议: Offset可微调以进一步提升精度")
            else:  # >= 3cm
                print("🎯 建议: 需要重新标定offset参数")

        # 显示诊断信息
        diag_info = self.rotation_analyzer.get_diagnostic_info()
        if diag_info.get('status') == 'ANALYZED':
            print(f"📈 分析帧数: {diag_info.get('total_frames', 0)}")
            print(f"💡 建议: {diag_info.get('recommendation', 'N/A')}")

        print("=" * 60 + "\n")

    def get_validation_summary(self) -> Dict[str, Any]:
        """获取验证摘要"""
        summary = {
            'frame_count': self.frame_count,
            'error_history_count': len(self.error_history),
            'rotation_data_frames': len(self.rotation_analyzer.wrist_positions)
        }

        # 旋转质量指标
        if self.rotation_analyzer.wrist_positions:
            rotation_ok, rotation_metrics = self.rotation_analyzer.is_offset_correct()
            summary['rotation_quality'] = {
                'is_correct': rotation_ok,
                'mean_radius_m': rotation_metrics['mean_radius_m'],
                'max_radius_m': rotation_metrics['max_radius_m'],
                'threshold_m': rotation_metrics['threshold_m']
            }

        # offset误差指标
        if self.error_history:
            distances = [e['distance_m'] for e in self.error_history]
            summary['offset_error'] = {
                'avg_distance_m': sum(distances) / len(distances),
                'max_distance_m': max(distances),
                'min_distance_m': min(distances),
                'std_distance_m': (sum((d - sum(distances)/len(distances))**2 for d in distances) / len(distances))**0.5
            }

        return summary

    def start(self):
        """启动验证演示"""
        print("🚀 启动验证演示...")
        print("1️⃣ 确保MANUS Core正在运行")
        print("2️⃣ 确保Vive Tracker已连接并配置")
        print("3️⃣ 确保C++客户端已编译并准备运行")
        print("4️⃣ 按Ctrl+C停止演示\n")

        try:
            # 启动接收器
            self.receiver.start()

            # 保持运行
            while self.receiver.running:
                time.sleep(0.1)

        except KeyboardInterrupt:
            print("\n👋 用户中断")
        except Exception as e:
            print(f"❌ 运行错误: {e}")
        finally:
            self.stop()

    def stop(self):
        """停止验证演示"""
        self.receiver.stop()

        # 显示最终摘要
        print("\n" + "=" * 60)
        print("📊 最终验证摘要")

        summary = self.get_validation_summary()

        print(f"📈 总帧数: {summary['frame_count']}")
        print(f"📊 错误分析帧数: {summary['error_history_count']}")
        print(f"🔄 旋转分析帧数: {summary['rotation_data_frames']}")

        if 'rotation_quality' in summary:
            rq = summary['rotation_quality']
            status = "✅ 良好" if rq['is_correct'] else "❌ 需要调整"
            print(f"🎯 旋转质量: {status} (半径: {rq['mean_radius_m']*100:.1f}cm)")

        if 'offset_error' in summary:
            oe = summary['offset_error']
            print(f"📏 Offset误差: 平均{oe['avg_distance_m']*100:.1f}cm, "
                  f"最大{oe['max_distance_m']*100:.1f}cm, 标准差{oe['std_distance_m']*100:.1f}cm")

        print("=" * 60)
        print("🎯 验证演示完成")


# ==================== 主程序 ====================

if __name__ == "__main__":
    demo = OffsetValidationDemo()

    # 可选：在后台线程中运行
    # demo_thread = threading.Thread(target=demo.start, daemon=True)
    # demo_thread.start()

    # 或者直接运行
    demo.start()