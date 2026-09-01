#!/usr/bin/env python3
"""
旋转质量验证模块
基于手腕位置（节点0）分析offset是否正确
"""

import numpy as np
import json
import time
from typing import List, Tuple, Dict, Any
import math


class RotationQualityAnalyzer:
    """分析手腕旋转质量，验证offset是否正确"""

    def __init__(self, rotation_threshold_m: float = 0.01):
        """
        初始化分析器

        Args:
            rotation_threshold_m: 旋转半径阈值（米），小于此值认为offset正确
        """
        self.rotation_threshold = rotation_threshold_m
        self.wrist_positions: List[np.ndarray] = []  # 存储手腕位置历史
        self.frame_timestamps: List[float] = []  # 存储时间戳
        self.is_calibrated = False
        self.calibration_center = None

    def add_wrist_position(self, position: List[float], timestamp: float = None):
        """
        添加手腕位置数据

        Args:
            position: [x, y, z] 位置坐标（米）
            timestamp: 可选时间戳
        """
        pos_array = np.array(position, dtype=np.float64)
        self.wrist_positions.append(pos_array)

        if timestamp is None:
            timestamp = time.time()
        self.frame_timestamps.append(timestamp)

        # 限制历史数据长度
        max_history = 1000
        if len(self.wrist_positions) > max_history:
            self.wrist_positions = self.wrist_positions[-max_history:]
            self.frame_timestamps = self.frame_timestamps[-max_history:]

    def add_frame_data(self, frame_data: Dict[str, Any], skeleton_index: int = 0):
        """
        从MANUS数据帧中添加手腕位置

        Args:
            frame_data: MANUS数据帧
            skeleton_index: 骨架索引（默认为0）
        """
        if not frame_data or 'skeletons' not in frame_data:
            return

        skeletons = frame_data['skeletons']
        if skeleton_index >= len(skeletons):
            return

        skeleton = skeletons[skeleton_index]
        if 'nodes' not in skeleton or len(skeleton['nodes']) == 0:
            return

        # 节点0为手腕（根据MANUS文档）
        wrist_node = skeleton['nodes'][0]
        position = wrist_node['position']  # [x, y, z]

        # 使用帧时间戳或当前时间
        timestamp = frame_data.get('timestamp', time.time())

        self.add_wrist_position(position, timestamp)

    def calculate_rotation_center(self, recent_frames: int = None) -> np.ndarray:
        """
        计算旋转中心（最近N帧的平均位置）

        Args:
            recent_frames: 使用的最近帧数，None表示使用所有数据

        Returns:
            旋转中心坐标 [x, y, z]
        """
        if not self.wrist_positions:
            return np.zeros(3)

        if recent_frames is not None and recent_frames > 0:
            positions = self.wrist_positions[-recent_frames:]
        else:
            positions = self.wrist_positions

        if not positions:
            return np.zeros(3)

        center = np.mean(positions, axis=0)
        return center

    def calculate_rotation_radius(self, recent_frames: int = 50) -> Tuple[float, float, float]:
        """
        计算旋转半径指标

        Args:
            recent_frames: 分析的最近帧数

        Returns:
            (平均半径, 半径标准差, 最大半径)
        """
        if len(self.wrist_positions) < 10:
            return 0.0, 0.0, 0.0

        # 使用最近的数据
        positions = self.wrist_positions[-recent_frames:] if recent_frames > 0 else self.wrist_positions
        if len(positions) < 5:
            return 0.0, 0.0, 0.0

        center = self.calculate_rotation_center(len(positions))

        # 计算每个点到中心的距离
        distances = [np.linalg.norm(pos - center) for pos in positions]

        mean_radius = float(np.mean(distances))
        std_radius = float(np.std(distances))
        max_radius = float(np.max(distances))

        return mean_radius, std_radius, max_radius

    def is_offset_correct(self, recent_frames: int = 50) -> Tuple[bool, Dict[str, float]]:
        """
        判断offset是否正确

        Args:
            recent_frames: 分析的最近帧数

        Returns:
            (是否正确, 详细指标)
        """
        mean_radius, std_radius, max_radius = self.calculate_rotation_radius(recent_frames)

        # 判断逻辑
        is_correct = mean_radius < self.rotation_threshold

        metrics = {
            'mean_radius_m': mean_radius,
            'std_radius_m': std_radius,
            'max_radius_m': max_radius,
            'threshold_m': self.rotation_threshold,
            'is_correct': is_correct,
            'frames_analyzed': min(recent_frames, len(self.wrist_positions))
        }

        return is_correct, metrics

    def get_diagnostic_info(self) -> Dict[str, Any]:
        """
        获取诊断信息

        Returns:
            包含各种诊断指标的字典
        """
        if len(self.wrist_positions) < 10:
            return {
                'status': 'INSUFFICIENT_DATA',
                'frames_collected': len(self.wrist_positions),
                'message': '需要更多数据进行分析'
            }

        is_correct, metrics = self.is_offset_correct()

        # 计算位置变化范围
        positions_array = np.array(self.wrist_positions[-100:] if len(self.wrist_positions) > 100 else self.wrist_positions)
        pos_range = np.ptp(positions_array, axis=0)  # 各轴的变化范围

        # 计算运动速度（如果时间戳可用）
        avg_speed = 0.0
        if len(self.frame_timestamps) >= 2:
            recent_timestamps = self.frame_timestamps[-10:]
            recent_positions = self.wrist_positions[-10:]
            speeds = []
            for i in range(1, len(recent_timestamps)):
                dt = recent_timestamps[i] - recent_timestamps[i-1]
                if dt > 0:
                    distance = np.linalg.norm(recent_positions[i] - recent_positions[i-1])
                    speeds.append(distance / dt)
            if speeds:
                avg_speed = np.mean(speeds)

        center = self.calculate_rotation_center(50)

        return {
            'status': 'ANALYZED',
            'offset_correct': is_correct,
            'rotation_metrics': metrics,
            'position_range_m': {
                'x_range': float(pos_range[0]),
                'y_range': float(pos_range[1]),
                'z_range': float(pos_range[2])
            },
            'estimated_center_m': center.tolist(),
            'average_speed_mps': float(avg_speed),
            'total_frames': len(self.wrist_positions),
            'recommendation': self._generate_recommendation(metrics)
        }

    def _generate_recommendation(self, metrics: Dict[str, float]) -> str:
        """根据指标生成调整建议"""
        mean_radius = metrics['mean_radius_m']

        if mean_radius < 0.005:  # < 5mm
            return "✅ OFFSET优秀：旋转中心非常稳定"
        elif mean_radius < 0.01:  # < 1cm
            return "✅ OFFSET良好：在可接受范围内"
        elif mean_radius < 0.02:  # < 2cm
            return "⚠️ OFFSET需要微调：旋转半径稍大"
        elif mean_radius < 0.05:  # < 5cm
            return "❌ OFFSET需要调整：旋转半径明显偏大"
        else:  # >= 5cm
            return "❌❌ OFFSET严重错误：需要重新标定"

    def reset(self):
        """重置分析器"""
        self.wrist_positions.clear()
        self.frame_timestamps.clear()
        self.is_calibrated = False
        self.calibration_center = None


# ==================== 示例使用 ====================

def example_usage():
    """示例使用方式"""
    analyzer = RotationQualityAnalyzer(rotation_threshold_m=0.01)

    print("🎯 旋转质量分析器已初始化")
    print(f"阈值: {analyzer.rotation_threshold * 100:.1f}cm")
    print("\n使用方法:")
    print("1. 在manus_data_receiver.py的回调中调用:")
    print("   analyzer.add_frame_data(frame_data)")
    print("2. 定期检查结果:")
    print("   is_ok, metrics = analyzer.is_offset_correct()")
    print("3. 获取详细诊断:")
    print("   info = analyzer.get_diagnostic_info()")

    # 模拟数据
    print("\n📊 模拟数据分析:")
    import random
    for i in range(100):
        # 模拟旋转动作（围绕原点的小幅运动）
        angle = i * 0.1
        radius = 0.005  # 5mm半径（良好的offset）
        x = radius * math.cos(angle) + random.gauss(0, 0.001)
        y = radius * math.sin(angle) + random.gauss(0, 0.001)
        z = random.gauss(0, 0.001)
        analyzer.add_wrist_position([x, y, z])

    is_ok, metrics = analyzer.is_offset_correct()
    print(f"✅ Offset是否正确: {is_ok}")
    print(f"📏 平均旋转半径: {metrics['mean_radius_m'] * 100:.2f}cm")
    print(f"📊 半径标准差: {metrics['std_radius_m'] * 100:.2f}cm")
    print(f"📈 最大半径: {metrics['max_radius_m'] * 100:.2f}cm")

    # 显示诊断信息
    print("\n🔍 诊断信息:")
    diag = analyzer.get_diagnostic_info()
    for key, value in diag.items():
        if key == 'rotation_metrics':
            print(f"  {key}:")
            for k, v in value.items():
                print(f"    {k}: {v}")
        else:
            print(f"  {key}: {value}")


def integrate_with_manus_receiver():
    """
    与现有manus_data_receiver.py集成的方法

    在manus_data_receiver.py中添加:

    from rotation_quality import RotationQualityAnalyzer

    analyzer = RotationQualityAnalyzer()

    def validation_callback(frame_data):
        analyzer.add_frame_data(frame_data)

        # 每50帧检查一次
        if analyzer.total_frames % 50 == 0:
            is_ok, metrics = analyzer.is_offset_correct()
            if is_ok:
                print(f"✅ Offset正确: 半径={metrics['mean_radius_m']*100:.1f}cm")
            else:
                print(f"❌ Offset需要调整: 半径={metrics['mean_radius_m']*100:.1f}cm")

    # 在接收器中注册回调
    receiver.register_callback(validation_callback)
    """
    pass


if __name__ == "__main__":
    example_usage()