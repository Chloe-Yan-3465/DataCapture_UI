#!/usr/bin/env python3
"""
平移质量检测模块
分析手腕平移动作的直线度和方向一致性
"""

import numpy as np
from typing import List, Tuple, Dict, Any, Optional
import math


class TranslationQualityAnalyzer:
    """分析平移动作质量，验证tracking稳定性"""

    def __init__(self, linearity_threshold: float = 0.95, deviation_threshold_m: float = 0.02):
        """
        初始化分析器

        Args:
            linearity_threshold: 线性度阈值 (0-1)，大于此值认为平移是直线
            deviation_threshold_m: 偏离直线误差阈值（米），小于此值认为tracking稳定
        """
        self.linearity_threshold = linearity_threshold
        self.deviation_threshold = deviation_threshold_m

        # 平移数据历史
        self.positions: List[np.ndarray] = []  # [x, y, z] 位置坐标
        self.timestamps: List[float] = []
        self.is_collecting = False
        self.translation_direction = None  # 平移方向向量

        # 统计信息
        self.translation_count = 0
        self.linearity_scores: List[float] = []
        self.deviation_errors: List[float] = []

    def start_collection(self):
        """开始收集平移数据"""
        self.is_collecting = True
        self.positions.clear()
        self.timestamps.clear()
        self.translation_direction = None
        print("[MEASURE] 开始收集平移数据...")

    def stop_collection(self):
        """停止收集并分析数据"""
        if not self.is_collecting:
            return

        self.is_collecting = False
        if len(self.positions) >= 10:  # 最少需要10个点
            linearity, avg_deviation, direction = self.analyze_translation()

            self.linearity_scores.append(linearity)
            self.deviation_errors.append(avg_deviation)
            self.translation_count += 1

            print(f"[STATS] 平移分析完成: 线性度={linearity:.3f}, 平均偏差={avg_deviation*100:.1f}cm")

            return {
                'linearity': linearity,
                'avg_deviation_m': avg_deviation,
                'max_deviation_m': self._calculate_max_deviation(),
                'direction': direction.tolist() if direction is not None else None,
                'is_linear': linearity > self.linearity_threshold,
                'is_stable': avg_deviation < self.deviation_threshold,
                'points_analyzed': len(self.positions)
            }
        return None

    def add_position(self, position: List[float], timestamp: float = None):
        """
        添加位置数据

        Args:
            position: [x, y, z] 位置坐标（米）
            timestamp: 可选时间戳
        """
        if not self.is_collecting:
            return

        pos_array = np.array(position, dtype=np.float64)
        self.positions.append(pos_array)

        if timestamp is None:
            import time
            timestamp = time.time()
        self.timestamps.append(timestamp)

    def add_frame_data(self, frame_data: Dict[str, Any], skeleton_index: int = 0):
        """
        从MANUS数据帧中添加手腕位置

        Args:
            frame_data: MANUS数据帧
            skeleton_index: 骨架索引（默认为0）
        """
        if not self.is_collecting or not frame_data or 'skeletons' not in frame_data:
            return

        skeletons = frame_data['skeletons']
        if skeleton_index >= len(skeletons):
            return

        skeleton = skeletons[skeleton_index]
        if 'nodes' not in skeleton or len(skeleton['nodes']) == 0:
            return

        # 节点0为手腕
        wrist_node = skeleton['nodes'][0]
        position = wrist_node['position']  # [x, y, z]

        self.add_position(position)

    def analyze_translation(self) -> Tuple[float, float, Optional[np.ndarray]]:
        """
        分析平移质量

        Returns:
            (线性度, 平均偏差, 方向向量)
        """
        if len(self.positions) < 10:
            return 0.0, 0.0, None

        positions_array = np.array(self.positions)

        # 1. 使用主成分分析(PCA)找到主要平移方向
        centered = positions_array - positions_array.mean(axis=0)
        if len(centered) > 1:
            # 计算协方差矩阵
            cov_matrix = np.cov(centered.T)
            eigenvalues, eigenvectors = np.linalg.eig(cov_matrix)

            # 最大特征值对应的特征向量是主要方向
            main_direction_idx = np.argmax(eigenvalues)
            direction = eigenvectors[:, main_direction_idx]
            self.translation_direction = direction / np.linalg.norm(direction)
        else:
            direction = np.array([1.0, 0.0, 0.0])  # 默认方向

        # 2. 计算每个点到直线的偏差
        deviations = self._calculate_deviations(positions_array, direction)
        avg_deviation = float(np.mean(deviations))

        # 3. 计算线性度（基于方向一致性）
        linearity = self._calculate_linearity(positions_array, direction)

        return linearity, avg_deviation, direction

    def _calculate_deviations(self, positions: np.ndarray, direction: np.ndarray) -> np.ndarray:
        """计算每个点到拟合直线的偏差"""
        if len(positions) < 2:
            return np.array([0.0])

        # 使用起始点作为参考点
        start_point = positions[0]

        # 计算每个点到直线的距离
        deviations = []
        for point in positions:
            # 点到直线的距离公式
            vec_to_point = point - start_point
            projection = np.dot(vec_to_point, direction) * direction
            perpendicular = vec_to_point - projection
            distance = np.linalg.norm(perpendicular)
            deviations.append(distance)

        return np.array(deviations)

    def _calculate_linearity(self, positions: np.ndarray, direction: np.ndarray) -> float:
        """计算平移的线性度（0-1）"""
        if len(positions) < 3:
            return 0.0

        # 方法1：方向一致性
        displacements = np.diff(positions, axis=0)
        if len(displacements) == 0:
            return 0.0

        # 归一化位移向量
        norms = np.linalg.norm(displacements, axis=1)
        valid_indices = norms > 1e-6
        if not np.any(valid_indices):
            return 0.0

        normalized_displacements = displacements[valid_indices] / norms[valid_indices, np.newaxis]

        # 计算与主方向的角度一致性
        dot_products = np.abs(np.dot(normalized_displacements, direction))
        direction_consistency = float(np.mean(dot_products))

        # 方法2：R²值（拟合优度）
        # 将点投影到主方向上
        start_point = positions[0]
        projections = []
        for point in positions:
            vec = point - start_point
            projection = np.dot(vec, direction)
            projections.append(projection)

        # 实际位置在主方向上的变化
        actual_projections = np.array(projections)

        # 理想线性变化（从0到最大投影值均匀分布）
        ideal_projections = np.linspace(0, actual_projections[-1], len(actual_projections))

        # 计算R²
        ss_res = np.sum((actual_projections - ideal_projections) ** 2)
        ss_tot = np.sum((actual_projections - np.mean(actual_projections)) ** 2)

        if ss_tot < 1e-6:
            r_squared = 1.0
        else:
            r_squared = max(0.0, 1.0 - ss_res / ss_tot)

        # 综合线性度得分
        linearity = 0.7 * direction_consistency + 0.3 * r_squared

        return min(1.0, max(0.0, linearity))

    def _calculate_max_deviation(self) -> float:
        """计算最大偏差"""
        if not self.positions or len(self.positions) < 10:
            return 0.0

        positions_array = np.array(self.positions)
        if self.translation_direction is None:
            _, _, direction = self.analyze_translation()
        else:
            direction = self.translation_direction

        deviations = self._calculate_deviations(positions_array, direction)
        return float(np.max(deviations))

    def is_translation_good(self) -> Tuple[bool, Dict[str, float]]:
        """
        判断平移质量是否良好

        Returns:
            (是否良好, 详细指标)
        """
        if len(self.positions) < 10:
            return False, {'status': 'INSUFFICIENT_DATA', 'points': len(self.positions)}

        linearity, avg_deviation, direction = self.analyze_translation()

        is_linear = linearity > self.linearity_threshold
        is_stable = avg_deviation < self.deviation_threshold

        metrics = {
            'linearity': linearity,
            'avg_deviation_m': avg_deviation,
            'max_deviation_m': self._calculate_max_deviation(),
            'direction': direction.tolist() if direction is not None else None,
            'is_linear': is_linear,
            'is_stable': is_stable,
            'linearity_threshold': self.linearity_threshold,
            'deviation_threshold_m': self.deviation_threshold,
            'points_analyzed': len(self.positions)
        }

        return (is_linear and is_stable), metrics

    def get_diagnostic_info(self) -> Dict[str, Any]:
        """获取诊断信息"""
        if len(self.positions) < 10:
            return {
                'status': 'INSUFFICIENT_DATA',
                'points_collected': len(self.positions),
                'message': '需要更多平移数据进行分析'
            }

        is_good, metrics = self.is_translation_good()

        # 计算平移距离和速度
        positions_array = np.array(self.positions)
        total_distance = 0.0
        if len(positions_array) > 1:
            for i in range(1, len(positions_array)):
                total_distance += np.linalg.norm(positions_array[i] - positions_array[i-1])

        avg_speed = 0.0
        if len(self.timestamps) >= 2 and self.timestamps[-1] > self.timestamps[0]:
            total_time = self.timestamps[-1] - self.timestamps[0]
            if total_time > 0:
                avg_speed = total_distance / total_time

        return {
            'status': 'ANALYZED',
            'translation_quality_good': is_good,
            'metrics': metrics,
            'total_distance_m': total_distance,
            'average_speed_mps': avg_speed,
            'translation_count': self.translation_count,
            'recommendation': self._generate_recommendation(metrics)
        }

    def _generate_recommendation(self, metrics: Dict[str, float]) -> str:
        """根据指标生成建议"""
        linearity = metrics['linearity']
        avg_deviation = metrics['avg_deviation_m']

        if linearity > 0.98 and avg_deviation < 0.005:  # < 0.5cm
            return "[OK] 平移质量优秀：轨迹接近完美直线"
        elif linearity > 0.95 and avg_deviation < 0.01:  # < 1cm
            return "[OK] 平移质量良好：tracking稳定"
        elif linearity > 0.90 and avg_deviation < 0.02:  # < 2cm
            return "[WARN] 平移质量一般：存在轻微抖动或弯曲"
        elif linearity > 0.80 and avg_deviation < 0.03:  # < 3cm
            return "[ERROR] 平移质量需要改进：轨迹不够直或抖动明显"
        else:
            return "[ERROR][ERROR] 平移质量差：需要检查tracking系统"

    def reset(self):
        """重置分析器"""
        self.positions.clear()
        self.timestamps.clear()
        self.is_collecting = False
        self.translation_direction = None
        self.linearity_scores.clear()
        self.deviation_errors.clear()
        self.translation_count = 0


# ==================== 示例使用 ====================

def example_usage():
    """示例使用方式"""
    analyzer = TranslationQualityAnalyzer(
        linearity_threshold=0.95,
        deviation_threshold_m=0.01
    )

    print("[MEASURE] 平移质量分析器已初始化")
    print(f"线性度阈值: {analyzer.linearity_threshold}")
    print(f"偏差阈值: {analyzer.deviation_threshold * 100:.1f}cm")
    print("\n使用方法:")
    print("1. 开始收集数据: analyzer.start_collection()")
    print("2. 进行直线平移动作")
    print("3. 停止收集并分析: analyzer.stop_collection()")
    print("4. 获取结果: analyzer.get_diagnostic_info()")

    # 模拟直线平移数据
    print("\n[STATS] 模拟数据分析:")
    analyzer.start_collection()

    import random
    # 模拟理想的直线平移（X轴方向）
    for i in range(100):
        x = i * 0.01  # 10cm总行程
        y = random.gauss(0, 0.001)  # 微小随机误差
        z = random.gauss(0, 0.001)
        analyzer.add_position([x, y, z])

    result = analyzer.stop_collection()
    if result:
        print(f"[MEASURE] 线性度: {result['linearity']:.3f}")
        print(f"[RULER] 平均偏差: {result['avg_deviation_m'] * 100:.2f}cm")
        print(f"[PIN] 方向向量: {result['direction']}")
        print(f"[OK] 是否线性: {result['is_linear']}")
        print(f"[OK] 是否稳定: {result['is_stable']}")

    # 显示诊断信息
    print("\n[SEARCH] 诊断信息:")
    diag = analyzer.get_diagnostic_info()
    for key, value in diag.items():
        if key == 'metrics':
            print(f"  {key}:")
            for k, v in value.items():
                print(f"    {k}: {v}")
        else:
            print(f"  {key}: {value}")


def integrate_with_manus_receiver():
    """
    与现有manus_data_receiver.py集成的方法

    在manus_data_receiver.py中添加:

    from translation_quality import TranslationQualityAnalyzer

    analyzer = TranslationQualityAnalyzer()

    def translation_test_callback(frame_data):
        # 当按下特定键时开始/停止平移测试
        pass

    # 在接收器中注册回调
    receiver.register_callback(translation_test_callback)
    """
    pass


if __name__ == "__main__":
    example_usage()