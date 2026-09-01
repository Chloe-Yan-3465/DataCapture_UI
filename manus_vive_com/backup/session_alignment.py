#!/usr/bin/env python3
"""
Session对齐模块
确保多次实验数据的一致性，支持跨session比较和分析
"""

import json
import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
import statistics


class SessionAlignment:
    """Session对齐类，用于跨session数据一致性处理"""

    def __init__(self, reference_session_data: Optional[Dict[str, Any]] = None):
        """
        初始化Session对齐器

        Args:
            reference_session_data: 参考session数据（可选）
        """
        self.reference_session = reference_session_data
        self.alignment_transformation = None
        self.alignment_quality = None

        print("🔄 Session对齐模块初始化")
        if reference_session_data:
            print(f"  参考session: {reference_session_data.get('session_id', '未知')}")
        else:
            print("  未设置参考session，将以当前session为基准")

    def load_session_from_file(self, filepath: str) -> Dict[str, Any]:
        """
        从文件加载session数据

        Args:
            filepath: JSON文件路径

        Returns:
            session数据字典
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                session_data = json.load(f)

            print(f"✅ 加载session: {session_data.get('session_id', '未知')}")
            print(f"  帧数: {session_data.get('total_frames', 0)}")
            print(f"  时间范围: {session_data.get('start_time', '未知')} 到 {session_data.get('end_time', '未知')}")

            return session_data
        except Exception as e:
            print(f"❌ 加载session失败: {e}")
            return {}

    def set_reference_session(self, session_data: Dict[str, Any]):
        """设置参考session"""
        self.reference_session = session_data
        print(f"🎯 设置参考session: {session_data.get('session_id', '未知')}")

    def align_sessions(self,
                      current_session_data: Dict[str, Any],
                      alignment_method: str = 'error_based') -> Dict[str, Any]:
        """
        对齐两个session的数据

        Args:
            current_session_data: 当前session数据
            alignment_method: 对齐方法 ('error_based', 'position_based', 'hybrid')

        Returns:
            对齐后的session数据
        """
        if not self.reference_session:
            print("⚠️  未设置参考session，返回原始数据")
            return current_session_data

        print(f"\n🔄 开始session对齐")
        print(f"  参考session: {self.reference_session.get('session_id', '未知')}")
        print(f"  当前session: {current_session_data.get('session_id', '未知')}")
        print(f"  对齐方法: {alignment_method}")

        aligned_session = current_session_data.copy()

        if alignment_method == 'error_based':
            transformation = self._align_by_error_pattern(current_session_data)
        elif alignment_method == 'position_based':
            transformation = self._align_by_position_pattern(current_session_data)
        elif alignment_method == 'hybrid':
            transformation = self._align_hybrid(current_session_data)
        else:
            print(f"❌ 未知对齐方法: {alignment_method}")
            return current_session_data

        if transformation:
            self.alignment_transformation = transformation
            self.alignment_quality = self._calculate_alignment_quality(
                current_session_data, transformation
            )

            # 应用变换到误差数据
            aligned_session = self._apply_transformation(
                current_session_data, transformation
            )

            print(f"✅ 对齐完成")
            print(f"  变换矩阵: {transformation.get('translation', '无')}")
            print(f"  对齐质量: {self.alignment_quality:.3f}")

            # 添加对齐元数据
            aligned_session['alignment_metadata'] = {
                'reference_session_id': self.reference_session.get('session_id'),
                'alignment_method': alignment_method,
                'transformation': transformation,
                'alignment_quality': self.alignment_quality,
                'alignment_time': datetime.now().isoformat()
            }

        return aligned_session

    def _align_by_error_pattern(self, current_session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """基于误差模式对齐session"""
        ref_errors = self._extract_error_vectors(self.reference_session)
        curr_errors = self._extract_error_vectors(current_session)

        if len(ref_errors) < 10 or len(curr_errors) < 10:
            print("⚠️  误差数据不足，无法进行误差模式对齐")
            return None

        # 计算误差的统计特性
        ref_mean = np.mean(ref_errors, axis=0)
        curr_mean = np.mean(curr_errors, axis=0)

        # 计算变换（当前session误差减去参考session误差的均值）
        translation = curr_mean - ref_mean

        return {
            'method': 'error_based',
            'translation': translation.tolist(),
            'scale': [1.0, 1.0, 1.0],
            'ref_error_mean': ref_mean.tolist(),
            'curr_error_mean': curr_mean.tolist()
        }

    def _align_by_position_pattern(self, current_session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """基于位置模式对齐session"""
        ref_positions = self._extract_wrist_positions(self.reference_session)
        curr_positions = self._extract_wrist_positions(current_session)

        if len(ref_positions) < 10 or len(curr_positions) < 10:
            print("⚠️  位置数据不足，无法进行位置模式对齐")
            return None

        # 计算位置的中心点
        ref_center = np.mean(ref_positions, axis=0)
        curr_center = np.mean(curr_positions, axis=0)

        # 计算变换
        translation = curr_center - ref_center

        return {
            'method': 'position_based',
            'translation': translation.tolist(),
            'scale': [1.0, 1.0, 1.0],
            'ref_center': ref_center.tolist(),
            'curr_center': curr_center.tolist()
        }

    def _align_hybrid(self, current_session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """混合对齐方法"""
        error_transformation = self._align_by_error_pattern(current_session)
        position_transformation = self._align_by_position_pattern(current_session)

        if not error_transformation and not position_transformation:
            return None
        elif not error_transformation:
            return position_transformation
        elif not position_transformation:
            return error_transformation

        # 结合两种变换（加权平均）
        error_weight = 0.6
        position_weight = 0.4

        error_trans = np.array(error_transformation['translation'])
        position_trans = np.array(position_transformation['translation'])

        combined_translation = (error_weight * error_trans +
                               position_weight * position_trans)

        return {
            'method': 'hybrid',
            'translation': combined_translation.tolist(),
            'scale': [1.0, 1.0, 1.0],
            'weights': {'error': error_weight, 'position': position_weight},
            'error_translation': error_transformation['translation'],
            'position_translation': position_transformation['translation']
        }

    def _extract_error_vectors(self, session_data: Dict[str, Any]) -> np.ndarray:
        """从session数据中提取误差向量"""
        error_vectors = []

        if 'frames' in session_data:
            for frame in session_data['frames']:
                if 'error_vector' in frame:
                    error_vectors.append(frame['error_vector'])

        return np.array(error_vectors) if error_vectors else np.array([])

    def _extract_wrist_positions(self, session_data: Dict[str, Any]) -> np.ndarray:
        """从session数据中提取手腕位置"""
        positions = []

        if 'frames' in session_data:
            for frame in session_data['frames']:
                if 'wrist_position' in frame and frame['wrist_position'] is not None:
                    positions.append(frame['wrist_position'])

        return np.array(positions) if positions else np.array([])

    def _calculate_alignment_quality(self,
                                    session_data: Dict[str, Any],
                                    transformation: Dict[str, Any]) -> float:
        """计算对齐质量（0-1）"""
        if 'frames' not in session_data or not session_data['frames']:
            return 0.0

        # 获取变换后的误差数据
        aligned_errors = []
        for frame in session_data['frames']:
            if 'error_vector' in frame:
                error_vec = np.array(frame['error_vector'])
                translation = np.array(transformation.get('translation', [0, 0, 0]))
                aligned_error = error_vec - translation
                aligned_errors.append(aligned_error)

        if len(aligned_errors) < 2:
            return 0.0

        # 计算对齐后的误差与参考session误差的相似度
        aligned_array = np.array(aligned_errors)

        # 计算误差的稳定性（方差越小越好）
        variances = np.var(aligned_array, axis=0)
        avg_variance = np.mean(variances)

        # 转换为质量分数（0-1），方差越小质量越高
        # 假设方差小于0.01（10cm²）为优秀，大于0.1（1m²）为差
        quality = max(0.0, min(1.0, 1.0 - (avg_variance / 0.1)))

        return quality

    def _apply_transformation(self,
                             session_data: Dict[str, Any],
                             transformation: Dict[str, Any]) -> Dict[str, Any]:
        """应用变换到session数据"""
        aligned_data = session_data.copy()

        if 'frames' in aligned_data and aligned_data['frames']:
            translation = np.array(transformation.get('translation', [0, 0, 0]))
            scale = np.array(transformation.get('scale', [1, 1, 1]))

            for frame in aligned_data['frames']:
                # 变换误差向量
                if 'error_vector' in frame:
                    error_vec = np.array(frame['error_vector'])
                    transformed_error = (error_vec - translation) * scale
                    frame['error_vector'] = transformed_error.tolist()

                # 变换手腕位置
                if 'wrist_position' in frame and frame['wrist_position']:
                    wrist_pos = np.array(frame['wrist_position'])
                    transformed_wrist = (wrist_pos - translation) * scale
                    frame['wrist_position'] = transformed_wrist.tolist()

                # 变换tracker位置
                if 'tracker_position' in frame and frame['tracker_position']:
                    tracker_pos = np.array(frame['tracker_position'])
                    transformed_tracker = (tracker_pos - translation) * scale
                    frame['tracker_position'] = transformed_tracker.tolist()

        return aligned_data

    def compare_sessions(self,
                        session1: Dict[str, Any],
                        session2: Dict[str, Any],
                        metrics: List[str] = None) -> Dict[str, Any]:
        """
        比较两个session的性能

        Args:
            session1: 第一个session数据
            session2: 第二个session数据
            metrics: 要比较的指标列表

        Returns:
            比较结果
        """
        if metrics is None:
            metrics = ['error_magnitude', 'rotation_radius', 'translation_linearity']

        print(f"\n📊 比较session:")
        print(f"  Session 1: {session1.get('session_id', '未知')}")
        print(f"  Session 2: {session2.get('session_id', '未知')}")

        comparison_results = {
            'session1_id': session1.get('session_id'),
            'session2_id': session2.get('session_id'),
            'comparison_time': datetime.now().isoformat(),
            'metrics': {}
        }

        # 误差幅度比较
        if 'error_magnitude' in metrics:
            error1 = self._calculate_average_error_magnitude(session1)
            error2 = self._calculate_average_error_magnitude(session2)

            improvement = ((error1 - error2) / error1 * 100) if error1 > 0 else 0

            comparison_results['metrics']['error_magnitude'] = {
                'session1_avg_m': error1,
                'session2_avg_m': error2,
                'improvement_percent': improvement,
                'better_session': 'session2' if error2 < error1 else 'session1'
            }

            print(f"📏 误差幅度比较:")
            print(f"  Session 1: {error1*100:.1f}cm")
            print(f"  Session 2: {error2*100:.1f}cm")
            print(f"  改进: {improvement:.1f}%")

        # 旋转半径比较
        if 'rotation_radius' in metrics:
            radius1 = self._calculate_average_rotation_radius(session1)
            radius2 = self._calculate_average_rotation_radius(session2)

            comparison_results['metrics']['rotation_radius'] = {
                'session1_avg_m': radius1,
                'session2_avg_m': radius2,
                'better_session': 'session2' if radius2 < radius1 else 'session1'
            }

            print(f"🔄 旋转半径比较:")
            print(f"  Session 1: {radius1*100:.1f}cm")
            print(f"  Session 2: {radius2*100:.1f}cm")

        # 平移线性度比较
        if 'translation_linearity' in metrics:
            linearity1 = self._calculate_average_translation_linearity(session1)
            linearity2 = self._calculate_average_translation_linearity(session2)

            comparison_results['metrics']['translation_linearity'] = {
                'session1_avg': linearity1,
                'session2_avg': linearity2,
                'better_session': 'session2' if linearity2 > linearity1 else 'session1'
            }

            print(f"📐 平移线性度比较:")
            print(f"  Session 1: {linearity1:.3f}")
            print(f"  Session 2: {linearity2:.3f}")

        return comparison_results

    def _calculate_average_error_magnitude(self, session_data: Dict[str, Any]) -> float:
        """计算平均误差幅度"""
        if 'frames' not in session_data or not session_data['frames']:
            return 0.0

        error_magnitudes = []
        for frame in session_data['frames']:
            if 'error_distance_m' in frame:
                error_magnitudes.append(frame['error_distance_m'])

        return statistics.mean(error_magnitudes) if error_magnitudes else 0.0

    def _calculate_average_rotation_radius(self, session_data: Dict[str, Any]) -> float:
        """计算平均旋转半径"""
        if 'rotation_tests' not in session_data or not session_data['rotation_tests']:
            return 0.0

        radii = []
        for test in session_data['rotation_tests']:
            if 'metrics' in test and 'mean_radius_m' in test['metrics']:
                radii.append(test['metrics']['mean_radius_m'])

        return statistics.mean(radii) if radii else 0.0

    def _calculate_average_translation_linearity(self, session_data: Dict[str, Any]) -> float:
        """计算平均平移线性度"""
        if 'translation_tests' not in session_data or not session_data['translation_tests']:
            return 0.0

        linearities = []
        for test in session_data['translation_tests']:
            if 'result' in test and test['result'] and 'linearity' in test['result']:
                linearities.append(test['result']['linearity'])

        return statistics.mean(linearities) if linearities else 0.0

    def generate_comparison_report(self,
                                  session1: Dict[str, Any],
                                  session2: Dict[str, Any]) -> str:
        """生成比较报告"""
        comparison = self.compare_sessions(session1, session2)

        report_lines = []
        report_lines.append("=" * 60)
        report_lines.append("📊 Session比较报告")
        report_lines.append("=" * 60)
        report_lines.append(f"Session 1: {session1.get('session_id', '未知')}")
        report_lines.append(f"Session 2: {session2.get('session_id', '未知')}")
        report_lines.append(f"比较时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report_lines.append("")

        for metric_name, metric_data in comparison['metrics'].items():
            report_lines.append(f"📈 {metric_name.upper()} 比较:")

            if metric_name == 'error_magnitude':
                s1_val = metric_data['session1_avg_m'] * 100
                s2_val = metric_data['session2_avg_m'] * 100
                report_lines.append(f"  Session 1: {s1_val:.1f}cm")
                report_lines.append(f"  Session 2: {s2_val:.1f}cm")
                if 'improvement_percent' in metric_data:
                    report_lines.append(f"  改进: {metric_data['improvement_percent']:.1f}%")

            elif metric_name == 'rotation_radius':
                s1_val = metric_data['session1_avg_m'] * 100
                s2_val = metric_data['session2_avg_m'] * 100
                report_lines.append(f"  Session 1: {s1_val:.1f}cm")
                report_lines.append(f"  Session 2: {s2_val:.1f}cm")

            elif metric_name == 'translation_linearity':
                s1_val = metric_data['session1_avg']
                s2_val = metric_data['session2_avg']
                report_lines.append(f"  Session 1: {s1_val:.3f}")
                report_lines.append(f"  Session 2: {s2_val:.3f}")

            report_lines.append(f"  更好的session: {metric_data['better_session']}")
            report_lines.append("")

        # 总体结论
        error_improvement = comparison['metrics'].get('error_magnitude', {}).get('improvement_percent', 0)
        if error_improvement > 10:
            conclusion = "✅ Session 2 明显优于 Session 1"
        elif error_improvement > 0:
            conclusion = "✅ Session 2 略有改进"
        elif error_improvement < -10:
            conclusion = "❌ Session 1 明显优于 Session 2"
        else:
            conclusion = "⚠️  两个session性能相似"

        report_lines.append(f"💡 总体结论: {conclusion}")
        report_lines.append("=" * 60)

        return '\n'.join(report_lines)


# ==================== 示例使用 ====================

def example_usage():
    """示例使用方式"""
    print("🔄 Session对齐模块示例")
    print("=" * 60)

    # 创建对齐器
    aligner = SessionAlignment()

    # 模拟两个session数据
    print("\n📊 创建模拟session数据...")

    session1 = {
        'session_id': 'test_session_001',
        'start_time': '2024-01-01T10:00:00',
        'end_time': '2024-01-01T10:05:00',
        'total_frames': 100,
        'frames': []
    }

    session2 = {
        'session_id': 'test_session_002',
        'start_time': '2024-01-01T11:00:00',
        'end_time': '2024-01-01T11:05:00',
        'total_frames': 100,
        'frames': []
    }

    import random

    # 为session1生成模拟数据（有系统误差）
    for i in range(100):
        error_x = 0.02 + random.gauss(0, 0.005)  # 2cm系统误差
        error_y = -0.01 + random.gauss(0, 0.003) # -1cm系统误差
        error_z = random.gauss(0, 0.002)

        session1['frames'].append({
            'frame': i,
            'error_vector': [error_x, error_y, error_z],
            'error_distance_m': (error_x**2 + error_y**2 + error_z**2)**0.5,
            'wrist_position': [random.random(), random.random(), random.random()]
        })

    # 为session2生成模拟数据（有轻微不同的系统误差）
    for i in range(100):
        error_x = 0.025 + random.gauss(0, 0.005)  # 2.5cm系统误差
        error_y = -0.008 + random.gauss(0, 0.003) # -0.8cm系统误差
        error_z = 0.005 + random.gauss(0, 0.002)  # 0.5cm系统误差

        session2['frames'].append({
            'frame': i,
            'error_vector': [error_x, error_y, error_z],
            'error_distance_m': (error_x**2 + error_y**2 + error_z**2)**0.5,
            'wrist_position': [random.random(), random.random(), random.random()]
        })

    # 设置参考session
    aligner.set_reference_session(session1)

    # 对齐session
    print("\n🔄 对齐session...")
    aligned_session2 = aligner.align_sessions(session2, alignment_method='error_based')

    # 比较session
    print("\n📊 比较session性能...")
    comparison = aligner.compare_sessions(session1, aligned_session2)

    # 生成报告
    report = aligner.generate_comparison_report(session1, aligned_session2)
    print(report)

    print("\n✅ 示例完成")


if __name__ == "__main__":
    example_usage()