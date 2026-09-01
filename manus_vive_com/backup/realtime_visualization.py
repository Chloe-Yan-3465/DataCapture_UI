#!/usr/bin/env python3
"""
实时可视化模块
提供实时的误差、旋转半径和平移质量的图表显示
"""

import threading
import time
import numpy as np
from typing import Dict, List, Any, Optional
from collections import deque
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation


class RealTimeVisualization:
    """实时可视化类"""

    def __init__(self,
                 max_data_points: int = 500,
                 update_interval_ms: int = 100):
        """
        初始化可视化器

        Args:
            max_data_points: 最大数据点数
            update_interval_ms: 更新间隔（毫秒）
        """
        self.max_data_points = max_data_points
        self.update_interval = update_interval_ms / 1000.0

        # 数据缓冲区
        self.error_magnitudes = deque(maxlen=max_data_points)
        self.error_vectors_x = deque(maxlen=max_data_points)
        self.error_vectors_y = deque(maxlen=max_data_points)
        self.error_vectors_z = deque(maxlen=max_data_points)
        self.timestamps = deque(maxlen=max_data_points)

        # 旋转测试数据
        self.rotation_radii = deque(maxlen=50)
        self.rotation_timestamps = deque(maxlen=50)

        # 平移测试数据
        self.translation_linearities = deque(maxlen=50)
        self.translation_deviations = deque(maxlen=50)
        self.translation_timestamps = deque(maxlen=50)

        # 状态
        self.is_running = False
        self.visualization_thread = None
        self.fig = None
        self.axes = None
        self.animation = None

        print("📊 实时可视化模块初始化")
        print(f"  最大数据点: {max_data_points}")
        print(f"  更新间隔: {update_interval_ms}ms")

    def start(self):
        """启动实时可视化"""
        if self.is_running:
            print("⚠️  可视化已在运行中")
            return

        self.is_running = True
        self.visualization_thread = threading.Thread(
            target=self._visualization_loop,
            daemon=True
        )
        self.visualization_thread.start()

        print("📊 实时可视化已启动")

    def stop(self):
        """停止实时可视化"""
        self.is_running = False
        if self.visualization_thread:
            self.visualization_thread.join(timeout=2.0)

        if self.animation:
            self.animation.event_source.stop()

        if self.fig:
            plt.close(self.fig)

        print("📊 实时可视化已停止")

    def add_error_data(self, error_info: Dict[str, Any], timestamp: Optional[float] = None):
        """添加误差数据"""
        if not error_info or 'error_vector' not in error_info:
            return

        if timestamp is None:
            timestamp = time.time()

        error_vector = error_info['error_vector']
        error_magnitude = error_info.get('distance_m',
                                        np.linalg.norm(error_vector))

        self.error_magnitudes.append(error_magnitude)
        self.error_vectors_x.append(error_vector[0])
        self.error_vectors_y.append(error_vector[1])
        self.error_vectors_z.append(error_vector[2])
        self.timestamps.append(timestamp)

    def add_rotation_data(self, radius_m: float, timestamp: Optional[float] = None):
        """添加旋转半径数据"""
        if timestamp is None:
            timestamp = time.time()

        self.rotation_radii.append(radius_m)
        self.rotation_timestamps.append(timestamp)

    def add_translation_data(self, linearity: float, deviation_m: float,
                            timestamp: Optional[float] = None):
        """添加平移质量数据"""
        if timestamp is None:
            timestamp = time.time()

        self.translation_linearities.append(linearity)
        self.translation_deviations.append(deviation_m)
        self.translation_timestamps.append(timestamp)

    def _visualization_loop(self):
        """可视化主循环"""
        try:
            # 创建图形
            self.fig, self.axes = plt.subplots(2, 2, figsize=(12, 8))
            self.fig.suptitle('MANUS Core 实时监控', fontsize=14)

            # 设置子图
            ax1 = self.axes[0, 0]  # 误差幅度
            ax2 = self.axes[0, 1]  # 各轴误差
            ax3 = self.axes[1, 0]  # 旋转半径
            ax4 = self.axes[1, 1]  # 平移质量

            # 初始化线条
            line1, = ax1.plot([], [], 'b-', label='误差幅度')
            line2_x, = ax2.plot([], [], 'r-', label='X轴误差')
            line2_y, = ax2.plot([], [], 'g-', label='Y轴误差')
            line2_z, = ax2.plot([], [], 'b-', label='Z轴误差')
            line3, = ax3.plot([], [], 'o-', label='旋转半径')
            line4_lin, = ax4.plot([], [], 'r-', label='线性度')
            line4_dev, = ax4.plot([], [], 'b--', label='偏差')

            # 设置图形属性
            ax1.set_title('误差幅度 (米)')
            ax1.set_xlabel('时间 (秒)')
            ax1.set_ylabel('误差 (m)')
            ax1.grid(True, alpha=0.3)
            ax1.legend()

            ax2.set_title('各轴误差分量')
            ax2.set_xlabel('时间 (秒)')
            ax2.set_ylabel('误差 (m)')
            ax2.grid(True, alpha=0.3)
            ax2.legend()

            ax3.set_title('旋转半径 (米)')
            ax3.set_xlabel('测试序号')
            ax3.set_ylabel('半径 (m)')
            ax3.grid(True, alpha=0.3)
            ax3.legend()

            ax4.set_title('平移质量')
            ax4.set_xlabel('测试序号')
            ax4.set_ylabel('指标值')
            ax4.grid(True, alpha=0.3)
            ax4.legend()

            plt.tight_layout()

            # 更新函数
            def update(frame):
                # 更新误差幅度图
                if self.timestamps:
                    rel_times = [t - self.timestamps[0] for t in self.timestamps]
                    line1.set_data(rel_times[-self.max_data_points:],
                                 list(self.error_magnitudes)[-self.max_data_points:])
                    ax1.relim()
                    ax1.autoscale_view()

                # 更新各轴误差图
                if self.timestamps:
                    rel_times = [t - self.timestamps[0] for t in self.timestamps]
                    line2_x.set_data(rel_times[-self.max_data_points:],
                                   list(self.error_vectors_x)[-self.max_data_points:])
                    line2_y.set_data(rel_times[-self.max_data_points:],
                                   list(self.error_vectors_y)[-self.max_data_points:])
                    line2_z.set_data(rel_times[-self.max_data_points:],
                                   list(self.error_vectors_z)[-self.max_data_points:])
                    ax2.relim()
                    ax2.autoscale_view()

                # 更新旋转半径图
                if self.rotation_timestamps:
                    test_nums = list(range(len(self.rotation_radii)))
                    line3.set_data(test_nums, list(self.rotation_radii))
                    ax3.relim()
                    ax3.autoscale_view()

                # 更新平移质量图
                if self.translation_timestamps:
                    test_nums = list(range(len(self.translation_linearities)))
                    line4_lin.set_data(test_nums, list(self.translation_linearities))
                    line4_dev.set_data(test_nums, list(self.translation_deviations))
                    ax4.relim()
                    ax4.autoscale_view()

                return (line1, line2_x, line2_y, line2_z, line3, line4_lin, line4_dev)

            # 创建动画
            self.animation = FuncAnimation(
                self.fig, update,
                interval=self.update_interval * 1000,
                blit=True
            )

            plt.show()

        except Exception as e:
            print(f"❌ 可视化错误: {e}")
            self.is_running = False

    def get_statistics(self) -> Dict[str, Any]:
        """获取当前统计数据"""
        stats = {
            'error_data_points': len(self.error_magnitudes),
            'rotation_tests': len(self.rotation_radii),
            'translation_tests': len(self.translation_linearities)
        }

        if self.error_magnitudes:
            error_array = np.array(self.error_magnitudes)
            stats['error_stats'] = {
                'mean_m': float(np.mean(error_array)),
                'std_m': float(np.std(error_array)),
                'max_m': float(np.max(error_array)),
                'min_m': float(np.min(error_array))
            }

        if self.error_vectors_x:
            stats['axis_stats'] = {
                'x_mean': float(np.mean(self.error_vectors_x)),
                'y_mean': float(np.mean(self.error_vectors_y)),
                'z_mean': float(np.mean(self.error_vectors_z)),
                'x_std': float(np.std(self.error_vectors_x)),
                'y_std': float(np.std(self.error_vectors_y)),
                'z_std': float(np.std(self.error_vectors_z))
            }

        if self.rotation_radii:
            radius_array = np.array(self.rotation_radii)
            stats['rotation_stats'] = {
                'mean_radius_m': float(np.mean(radius_array)),
                'max_radius_m': float(np.max(radius_array))
            }

        if self.translation_linearities:
            linearity_array = np.array(self.translation_linearities)
            deviation_array = np.array(self.translation_deviations)
            stats['translation_stats'] = {
                'mean_linearity': float(np.mean(linearity_array)),
                'mean_deviation_m': float(np.mean(deviation_array))
            }

        return stats

    def save_current_plot(self, filename: str = None):
        """保存当前图表"""
        if not self.fig:
            print("⚠️  没有活动的图表可保存")
            return

        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"manus_visualization_{timestamp}.png"

        try:
            self.fig.savefig(filename, dpi=150, bbox_inches='tight')
            print(f"✅ 图表已保存到: {filename}")
        except Exception as e:
            print(f"❌ 保存图表失败: {e}")


# ==================== 简化控制台可视化 ====================

class ConsoleVisualization:
    """控制台可视化（无需matplotlib）"""

    def __init__(self, update_interval_frames: int = 10):
        """
        初始化控制台可视化

        Args:
            update_interval_frames: 更新间隔（帧数）
        """
        self.update_interval = update_interval_frames
        self.error_history = deque(maxlen=100)
        self.frame_count = 0

        print("📊 控制台可视化初始化")
        print(f"  更新间隔: 每{update_interval_frames}帧")

    def add_error_data(self, error_info: Dict[str, Any]):
        """添加误差数据"""
        if not error_info or 'error_vector' not in error_info:
            return

        self.error_history.append(error_info)
        self.frame_count += 1

        if self.frame_count % self.update_interval == 0:
            self._display_console_update()

    def _display_console_update(self):
        """显示控制台更新"""
        if not self.error_history:
            return

        # 计算统计信息
        errors = list(self.error_history)
        magnitudes = [e.get('distance_m',
                           np.linalg.norm(e['error_vector']))
                     for e in errors]

        if not magnitudes:
            return

        avg_magnitude = np.mean(magnitudes)
        max_magnitude = np.max(magnitudes)

        # 各轴误差
        error_vectors = [e['error_vector'] for e in errors]
        avg_x = np.mean([ev[0] for ev in error_vectors])
        avg_y = np.mean([ev[1] for ev in error_vectors])
        avg_z = np.mean([ev[2] for ev in error_vectors])

        # 创建简单的ASCII图表
        print(f"\n📊 实时状态 [帧: {self.frame_count}]")
        print(f"📏 平均误差: {avg_magnitude*100:.1f}cm")
        print(f"📏 最大误差: {max_magnitude*100:.1f}cm")
        print(f"📐 各轴误差: X:{avg_x*100:+.1f}cm Y:{avg_y*100:+.1f}cm Z:{avg_z*100:+.1f}cm")

        # 简单的误差幅度条形图
        bar_length = 20
        normalized_magnitude = min(1.0, avg_magnitude / 0.1)  # 假设0.1m为最大值

        filled = int(bar_length * normalized_magnitude)
        bar = '█' * filled + '░' * (bar_length - filled)

        print(f"📊 误差幅度: [{bar}] {normalized_magnitude*100:.0f}%")

        # 提供调整建议
        threshold = 0.02  # 2cm
        if avg_magnitude < threshold:
            print("💡 状态: ✅ 良好")
        elif avg_magnitude < threshold * 2:
            print("💡 状态: ⚠️  可优化")
        else:
            print("💡 状态: ❌ 需要调整")

    def get_summary(self) -> Dict[str, Any]:
        """获取摘要信息"""
        if not self.error_history:
            return {'status': 'NO_DATA'}

        errors = list(self.error_history)
        magnitudes = [e.get('distance_m',
                           np.linalg.norm(e['error_vector']))
                     for e in errors]

        error_vectors = [e['error_vector'] for e in errors]

        return {
            'frame_count': self.frame_count,
            'error_samples': len(errors),
            'avg_error_m': float(np.mean(magnitudes)),
            'max_error_m': float(np.max(magnitudes)),
            'avg_error_vector': [
                float(np.mean([ev[0] for ev in error_vectors])),
                float(np.mean([ev[1] for ev in error_vectors])),
                float(np.mean([ev[2] for ev in error_vectors]))
            ]
        }


# ==================== 示例使用 ====================

def example_usage():
    """示例使用方式"""
    print("📊 实时可视化示例")
    print("=" * 60)

    # 创建控制台可视化（不需要matplotlib）
    console_viz = ConsoleVisualization(update_interval_frames=5)

    print("\n📈 模拟数据流...")
    import random

    for i in range(50):
        # 模拟误差数据
        error_x = random.gauss(0.02, 0.005)
        error_y = random.gauss(-0.01, 0.003)
        error_z = random.gauss(0.0, 0.002)

        error_info = {
            'error_vector': [error_x, error_y, error_z],
            'distance_m': (error_x**2 + error_y**2 + error_z**2)**0.5
        }

        console_viz.add_error_data(error_info)
        time.sleep(0.1)

    print("\n📊 最终摘要:")
    summary = console_viz.get_summary()
    for key, value in summary.items():
        if isinstance(value, float):
            if 'error' in key.lower():
                print(f"  {key}: {value*100:.1f}cm")
            else:
                print(f"  {key}: {value}")
        elif isinstance(value, list):
            print(f"  {key}: {[v*100 for v in value]}")
        else:
            print(f"  {key}: {value}")


if __name__ == "__main__":
    example_usage()