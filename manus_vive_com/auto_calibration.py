#!/usr/bin/env python3
"""
自动校准模块
分析offset误差并提供具体的轴调整建议
"""

import numpy as np
from typing import Dict, List, Any, Tuple, Optional
import statistics


class AutoCalibration:
    """自动校准类，提供具体的轴调整建议"""

    def __init__(self,
                 adjustment_threshold_m: float = 0.005,  # 5mm调整阈值
                 min_samples: int = 50,                  # 最小样本数
                 confidence_level: float = 0.95):        # 置信水平
        """
        初始化校准器

        Args:
            adjustment_threshold_m: 调整阈值（米），小于此值认为无需调整
            min_samples: 最小样本数，达到此数量后才提供可靠建议
            confidence_level: 置信水平（0-1）
        """
        self.adjustment_threshold = adjustment_threshold_m
        self.min_samples = min_samples
        self.confidence_level = confidence_level

        # 误差历史数据
        self.error_history: List[Dict[str, Any]] = []
        self.error_vectors: List[List[float]] = []  # [error_x, error_y, error_z]

        # 统计信息
        self.sample_count = 0
        self.calibration_steps = []

        # 坐标系问题检测
        self.coordinate_issue_detected = False
        self.coordinate_issue_count = 0

        print("[TARGET] 自动校准模块初始化")
        print(f"  调整阈值: {self.adjustment_threshold * 100:.1f}cm")
        print(f"  最小样本: {self.min_samples}")
        print(f"  置信水平: {self.confidence_level * 100:.0f}%")

    def add_error_sample(self, error_info: Dict[str, Any]):
        """
        添加误差样本

        Args:
            error_info: 来自manus_data_receiver.calculate_offset_error()的误差信息
        """
        if not error_info or 'error_vector' not in error_info:
            return

        self.error_history.append(error_info)

        # 检测坐标系问题
        if error_info.get('coordinate_issue', False):
            self.coordinate_issue_count += 1
            if self.coordinate_issue_count >= 10:  # 连续10帧检测到坐标系问题
                self.coordinate_issue_detected = True

        # 优先使用相对误差向量（如果存在且坐标系有问题）
        if error_info.get('coordinate_issue', False) and 'relative_error_vector' in error_info:
            # 使用相对误差（已消除坐标系偏移）
            self.error_vectors.append(error_info['relative_error_vector'])
        else:
            # 使用原始误差
            self.error_vectors.append(error_info['error_vector'])

        self.sample_count += 1

    def get_adjustment_suggestions(self) -> Dict[str, Any]:
        """
        获取轴调整建议

        Returns:
            包含具体调整建议的字典
        """
        if self.sample_count < self.min_samples:
            return {
                'status': 'INSUFFICIENT_DATA',
                'message': f'需要更多数据 (当前: {self.sample_count}/{self.min_samples})',
                'adjustments': None,
                'confidence': 0.0
            }

        # 转换为numpy数组以便计算
        errors_array = np.array(self.error_vectors)

        # 计算各轴的平均误差和标准差
        mean_errors = np.mean(errors_array, axis=0)
        std_errors = np.std(errors_array, axis=0)

        # 计算置信区间
        confidence_intervals = self._calculate_confidence_intervals(errors_array)

        # 生成调整建议
        adjustments = []
        axis_names = ['X', 'Y', 'Z']
        adjustment_vectors = []

        for i, (axis, mean_err, std_err, conf_int) in enumerate(zip(
            axis_names, mean_errors, std_errors, confidence_intervals
        )):
            # 如果平均误差超过阈值，建议调整
            if abs(mean_err) > self.adjustment_threshold:
                # 调整方向与误差方向相反（如果tracker在手腕的+X方向，误差为正，则需要向-X调整）
                adjustment_amount = -mean_err  # 负号：向相反方向调整

                adjustments.append({
                    'axis': axis,
                    'current_error_m': float(mean_err),
                    'suggested_adjustment_m': float(adjustment_amount),
                    'adjustment_cm': float(adjustment_amount * 100),
                    'std_error_m': float(std_err),
                    'confidence_interval': conf_int,
                    'needs_adjustment': True
                })
                adjustment_vectors.append(adjustment_amount)
            else:
                adjustments.append({
                    'axis': axis,
                    'current_error_m': float(mean_err),
                    'suggested_adjustment_m': 0.0,
                    'adjustment_cm': 0.0,
                    'std_error_m': float(std_err),
                    'confidence_interval': conf_int,
                    'needs_adjustment': False
                })
                adjustment_vectors.append(0.0)

        # 计算总体调整幅度和方向
        total_adjustment_vector = np.array(adjustment_vectors)
        total_adjustment_magnitude = np.linalg.norm(total_adjustment_vector)

        # 计算建议的置信度（基于样本数和误差稳定性）
        confidence = self._calculate_suggestion_confidence(errors_array)

        # 提供XML格式调整建议（适用于MANUS Core配置文件）
        xml_suggestions = self._generate_xml_suggestions(adjustments)

        # 生成人类可读建议
        human_readable = self._generate_human_readable_suggestions(adjustments)

        # 添加坐标系问题警告
        if self.coordinate_issue_detected:
            human_readable = f"[警告] 检测到坐标系不匹配。建议可能不准确。请检查MANUS Core中Tracker的配置。\n" + human_readable
            # 在XML建议中也添加注释
            xml_suggestions = f"<!-- 警告: 检测到坐标系不匹配。请检查MANUS Core配置 -->\n" + xml_suggestions

        return {
            'status': 'READY',
            'sample_count': self.sample_count,
            'adjustments': adjustments,
            'total_adjustment_magnitude_m': float(total_adjustment_magnitude),
            'total_adjustment_vector': total_adjustment_vector.tolist(),
            'confidence': confidence,
            'xml_suggestions': xml_suggestions,
            'human_readable': human_readable,
            'coordinate_issue_detected': self.coordinate_issue_detected
        }

    def _calculate_confidence_intervals(self, errors_array: np.ndarray) -> List[Tuple[float, float]]:
        """计算各轴误差的置信区间"""
        from scipy import stats
        import math

        confidence_intervals = []
        n = len(errors_array)

        if n < 2:
            return [(0.0, 0.0) for _ in range(3)]

        # 计算t分布的临界值
        alpha = 1 - self.confidence_level
        t_critical = stats.t.ppf(1 - alpha/2, df=n-1)

        for i in range(3):
            axis_errors = errors_array[:, i]
            mean = np.mean(axis_errors)
            std = np.std(axis_errors)

            margin_of_error = t_critical * std / math.sqrt(n)
            confidence_intervals.append(
                (float(mean - margin_of_error), float(mean + margin_of_error))
            )

        return confidence_intervals

    def _calculate_suggestion_confidence(self, errors_array: np.ndarray) -> float:
        """计算建议的置信度（0-1）"""
        if self.sample_count < self.min_samples:
            return 0.0

        # 基于样本数量
        sample_confidence = min(1.0, self.sample_count / (self.min_samples * 2))

        # 基于误差的稳定性（变异系数）
        cv_scores = []
        for i in range(3):
            axis_errors = errors_array[:, i]
            if np.mean(np.abs(axis_errors)) > 1e-6:
                cv = np.std(axis_errors) / np.mean(np.abs(axis_errors))
                cv_scores.append(min(1.0, cv))  # 变异系数越小越好

        stability_confidence = 1.0 - np.mean(cv_scores) if cv_scores else 0.5

        # 综合置信度
        confidence = 0.6 * sample_confidence + 0.4 * stability_confidence
        return min(1.0, max(0.0, confidence))

    def _generate_xml_suggestions(self, adjustments: List[Dict[str, Any]]) -> str:
        """生成XML格式的调整建议"""
        xml_lines = []
        xml_lines.append('<!-- MANUS Core Offset 调整建议 -->')
        xml_lines.append('<!-- 将以下值添加到对应的tracker配置中 -->')
        xml_lines.append('')

        for adj in adjustments:
            if adj['needs_adjustment']:
                axis_lower = adj['axis'].lower()
                xml_lines.append(f'<!-- 调整{adj["axis"]}轴: {adj["suggested_adjustment_m"]:.4f}m ({adj["adjustment_cm"]:.1f}cm) -->')
                xml_lines.append(f'<offset_{axis_lower}>{adj["suggested_adjustment_m"]:.6f}</offset_{axis_lower}>')

        if not any(adj['needs_adjustment'] for adj in adjustments):
            xml_lines.append('<!-- 当前offset设置良好，无需调整 -->')

        return '\n'.join(xml_lines)

    def _generate_human_readable_suggestions(self, adjustments: List[Dict[str, Any]]) -> str:
        """生成人类可读的调整建议"""
        suggestions = []

        for adj in adjustments:
            if adj['needs_adjustment']:
                direction = "增加" if adj['suggested_adjustment_m'] > 0 else "减少"
                abs_amount = abs(adj['suggested_adjustment_m'])
                suggestions.append(
                    f"{adj['axis']}轴: {direction} {abs_amount:.3f}m ({abs_amount*100:.1f}cm)"
                )

        if suggestions:
            return "建议调整: " + ", ".join(suggestions)
        else:
            return "当前offset设置良好，无需调整"

    def get_detailed_analysis(self) -> Dict[str, Any]:
        """获取详细分析报告"""
        if self.sample_count == 0:
            return {
                'status': 'NO_DATA',
                'message': '尚未收集到误差数据'
            }

        # 基础统计
        errors_array = np.array(self.error_vectors)
        mean_errors = np.mean(errors_array, axis=0)
        std_errors = np.std(errors_array, axis=0)
        max_errors = np.max(np.abs(errors_array), axis=0)

        # 误差分布分析
        error_magnitudes = np.linalg.norm(errors_array, axis=1)
        avg_error_magnitude = np.mean(error_magnitudes)
        max_error_magnitude = np.max(error_magnitudes)

        # 主误差方向分析
        if self.sample_count >= 3:
            # PCA分析主要误差方向
            centered = errors_array - mean_errors
            if len(centered) > 1:
                cov_matrix = np.cov(centered.T)
                eigenvalues, eigenvectors = np.linalg.eig(cov_matrix)

                # 最大特征值对应的特征向量是主要误差方向
                main_error_idx = np.argmax(eigenvalues)
                main_error_direction = eigenvectors[:, main_error_idx]
                main_error_variance = eigenvalues[main_error_idx] / np.sum(eigenvalues)
            else:
                main_error_direction = None
                main_error_variance = 0.0
        else:
            main_error_direction = None
            main_error_variance = 0.0

        return {
            'status': 'ANALYZED',
            'sample_count': self.sample_count,
            'mean_errors_m': mean_errors.tolist(),
            'std_errors_m': std_errors.tolist(),
            'max_errors_m': max_errors.tolist(),
            'avg_error_magnitude_m': float(avg_error_magnitude),
            'max_error_magnitude_m': float(max_error_magnitude),
            'main_error_direction': main_error_direction.tolist() if main_error_direction is not None else None,
            'main_error_variance': float(main_error_variance) if main_error_variance is not None else 0.0,
            'adjustment_suggestions': self.get_adjustment_suggestions()
        }

    def reset(self):
        """重置校准器"""
        self.error_history.clear()
        self.error_vectors.clear()
        self.sample_count = 0
        self.calibration_steps.clear()
        print("[RESET] 校准器已重置")


# ==================== 集成示例 ====================

def example_integration():
    """示例集成方式"""
    print("🎯 自动校准模块示例")
    print("=" * 60)

    # 创建校准器
    calibrator = AutoCalibration(
        adjustment_threshold_m=0.005,  # 5mm
        min_samples=30,
        confidence_level=0.95
    )

    # 模拟误差数据（实际应从manus_data_receiver获取）
    print("\n📊 模拟误差数据...")
    import random

    for i in range(50):
        # 模拟X轴有+2cm系统误差，Y轴有-1cm系统误差，Z轴误差较小
        error_x = 0.02 + random.gauss(0, 0.005)  # 2cm ± 0.5cm
        error_y = -0.01 + random.gauss(0, 0.003) # -1cm ± 0.3cm
        error_z = random.gauss(0, 0.002)         # 随机小误差

        error_info = {
            'error_vector': [error_x, error_y, error_z],
            'distance_m': (error_x**2 + error_y**2 + error_z**2)**0.5,
            'wrist_position': [0, 0, 0],
            'tracker_position': [error_x, error_y, error_z]
        }

        calibrator.add_error_sample(error_info)

    # 获取调整建议
    print("\n🔍 分析调整建议...")
    suggestions = calibrator.get_adjustment_suggestions()

    if suggestions['status'] == 'READY':
        print(f"📈 样本数: {suggestions['sample_count']}")
        print(f"🎯 置信度: {suggestions['confidence']:.1%}")
        print(f"📏 总调整幅度: {suggestions['total_adjustment_magnitude_m']*100:.1f}cm")

        print("\n📋 各轴调整建议:")
        for adj in suggestions['adjustments']:
            if adj['needs_adjustment']:
                sign = '+' if adj['suggested_adjustment_m'] >= 0 else ''
                print(f"  {adj['axis']}轴: {sign}{adj['suggested_adjustment_m']:.4f}m "
                      f"({sign}{adj['adjustment_cm']:.1f}cm)")
                print(f"    当前误差: {adj['current_error_m']:.4f}m ± {adj['std_error_m']:.4f}m")
                print(f"    置信区间: [{adj['confidence_interval'][0]:.4f}, {adj['confidence_interval'][1]:.4f}]")
            else:
                print(f"  {adj['axis']}轴: 无需调整 (误差: {adj['current_error_m']:.4f}m)")

        print(f"\n💡 人类可读建议: {suggestions['human_readable']}")

        print("\n📄 XML格式建议:")
        print(suggestions['xml_suggestions'])

    # 详细分析报告
    print("\n" + "=" * 60)
    print("📊 详细分析报告")
    detailed = calibrator.get_detailed_analysis()

    if detailed['status'] == 'ANALYZED':
        print(f"📈 平均误差向量: {detailed['mean_errors_m']}")
        print(f"📊 误差标准差: {detailed['std_errors_m']}")
        print(f"📏 平均误差幅度: {detailed['avg_error_magnitude_m']*100:.1f}cm")
        print(f"📍 主要误差方向: {detailed['main_error_direction']}")
        print(f"📐 主要方向方差占比: {detailed['main_error_variance']:.1%}")


def integrate_with_validation_demo():
    """
    与offset_validation_demo.py集成的方法

    在offset_validation_demo.py中添加:

    from auto_calibration import AutoCalibration

    class OffsetValidationDemo:
        def __init__(self):
            # ... 现有代码 ...
            self.calibrator = AutoCalibration()

        def _validation_callback(self, frame_data):
            # ... 现有代码 ...
            error_info = self.receiver.calculate_offset_error()

            if error_info:
                # 添加到校准器
                self.calibrator.add_error_sample(error_info)

                # 定期显示调整建议
                if self.frame_count % 100 == 0:
                    suggestions = self.calibrator.get_adjustment_suggestions()
                    if suggestions['status'] == 'READY':
                        print(f"🎯 校准建议: {suggestions['human_readable']}")
    """
    pass


if __name__ == "__main__":
    example_integration()