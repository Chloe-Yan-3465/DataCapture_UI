#!/usr/bin/env python3
"""
MANUS Core 数据接收器
从C++客户端接收手部骨骼数据，准备用于虚拟环境
"""

import socket
import json
import threading
import time
from typing import Dict, List, Any, Optional, Tuple

class ManusDataReceiver:
    def __init__(self, host: str = "127.0.0.1", port: int = 8888):
        """初始化数据接收器"""
        self.host = host
        self.port = port
        self.server_socket = None
        self.client_socket = None
        self.running = False
        self.data_buffer = []
        self.latest_data = None
        self.latest_tracker_data = None
        self.callbacks = []

        # 数据统计
        self.frame_count = 0
        self.start_time = None

        # 坐标系校正
        self.coordinate_warning_shown = False
        self.error_history = []
        self.max_error_history = 100

    def start(self):
        """启动TCP服务器"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(1)

        print(f"[OK] MANUS数据接收器启动在 {self.host}:{self.port}")
        print("等待C++客户端连接...")

        self.running = True
        self.start_time = time.time()

        # 接受客户端连接
        self.client_socket, client_address = self.server_socket.accept()
        print(f"[OK] 客户端已连接: {client_address}")

        # 启动数据接收线程
        receive_thread = threading.Thread(target=self._receive_data, daemon=True)
        receive_thread.start()

        return True

    def _receive_data(self):
        """接收数据的线程函数"""
        buffer = ""

        while self.running and self.client_socket:
            try:
                # 接收数据
                data = self.client_socket.recv(4096)
                if not data:
                    print("[WARN]  客户端断开连接")
                    break

                # 解码数据
                buffer += data.decode('utf-8')

                # 按换行符分割完整JSON消息
                while '\n' in buffer:
                    json_str, buffer = buffer.split('\n', 1)

                    if json_str.strip():
                        try:
                            # 解析JSON
                            frame_data = json.loads(json_str)
                            self._process_frame(frame_data)
                        except json.JSONDecodeError as e:
                            print(f"[ERROR] JSON解析错误: {e}")
                            continue

            except (ConnectionResetError, ConnectionAbortedError) as e:
                print(f"[WARN]  连接异常: {e}")
                break
            except Exception as e:
                print(f"[ERROR] 接收数据错误: {e}")
                break

        self.stop()

    def _process_frame(self, frame_data: Dict[str, Any]):
        """处理一帧数据（支持骨架和Tracker混合数据）"""
        self.frame_count += 1

        # 处理骨架数据
        if 'skeletons' in frame_data:
            self.latest_data = frame_data

        # 处理Tracker数据
        if 'trackers' in frame_data:
            self.latest_tracker_data = frame_data

        # 如果是组合数据，更新latest_data以包含trackers
        if 'skeletons' in frame_data and 'trackers' in frame_data:
            self.latest_data = frame_data
            self.latest_tracker_data = frame_data

        self.data_buffer.append(frame_data)

        # 限制缓冲区大小
        if len(self.data_buffer) > 1000:
            self.data_buffer = self.data_buffer[-1000:]

        # 调用所有注册的回调函数
        for callback in self.callbacks:
            try:
                callback(frame_data)
            except Exception as e:
                print(f"回调函数错误: {e}")

        # 每100帧显示一次统计信息
        if self.frame_count % 100 == 0:
            elapsed = time.time() - self.start_time
            fps = self.frame_count / elapsed if elapsed > 0 else 0

            # 统计信息
            skeleton_count = len(frame_data.get('skeletons', []))
            tracker_count = len(frame_data.get('trackers', []))

            print(f"[STATS] 已接收 {self.frame_count} 帧 | FPS: {fps:.1f} | "
                  f"骨架: {skeleton_count} | Tracker: {tracker_count} | "
                  f"时间戳: {frame_data.get('timestamp', 0)}")

    def register_callback(self, callback):
        """注册数据处理回调函数"""
        self.callbacks.append(callback)
        print(f"[OK] 注册回调函数: {callback.__name__ if hasattr(callback, '__name__') else '匿名函数'}")

    def get_latest_data(self):
        """获取最新一帧数据"""
        return self.latest_data

    def get_data_buffer(self, max_frames: int = 100):
        """获取最近的数据帧"""
        return self.data_buffer[-max_frames:] if self.data_buffer else []

    def get_skeleton_data(self, skeleton_index: int = 0):
        """获取指定骨架的数据"""
        if not self.latest_data or 'skeletons' not in self.latest_data:
            return None

        if skeleton_index < len(self.latest_data['skeletons']):
            return self.latest_data['skeletons'][skeleton_index]
        return None

    def get_node_data(self, skeleton_index: int = 0, node_index: int = 0):
        """获取指定骨架指定节点的数据"""
        skeleton = self.get_skeleton_data(skeleton_index)
        if not skeleton or 'nodes' not in skeleton:
            return None

        if node_index < len(skeleton['nodes']):
            return skeleton['nodes'][node_index]
        return None

    def get_tracker_data(self, tracker_index: int = 0):
        """获取指定Tracker的数据"""
        # 尝试从最新数据中获取tracker数据
        if self.latest_tracker_data and 'trackers' in self.latest_tracker_data:
            if tracker_index < len(self.latest_tracker_data['trackers']):
                return self.latest_tracker_data['trackers'][tracker_index]
        # 如果没有专门的tracker数据，尝试从latest_data获取
        elif self.latest_data and 'trackers' in self.latest_data:
            if tracker_index < len(self.latest_data['trackers']):
                return self.latest_data['trackers'][tracker_index]
        return None

    def get_wrist_tracker_data(self):
        """获取手腕（第一个）Tracker的数据（如果有）"""
        # 默认假设第一个tracker是手腕的
        return self.get_tracker_data(0)

    def get_all_trackers(self):
        """获取所有Tracker数据"""
        if self.latest_tracker_data and 'trackers' in self.latest_tracker_data:
            return self.latest_tracker_data['trackers']
        elif self.latest_data and 'trackers' in self.latest_data:
            return self.latest_data['trackers']
        return []

    def get_wrist_position(self, skeleton_index: int = 0):
        """获取手腕位置（骨架节点0）"""
        node_data = self.get_node_data(skeleton_index, 0)
        if node_data and 'position' in node_data:
            return node_data['position']
        return None

    def get_tracker_position(self, tracker_index: int = 0):
        """获取Tracker位置"""
        tracker_data = self.get_tracker_data(tracker_index)
        if tracker_data and 'position' in tracker_data:
            return tracker_data['position']
        return None

    def calculate_offset_error(self, skeleton_index: int = 0, tracker_index: int = 0):
        """计算offset误差（手腕位置与Tracker位置的差异）"""
        wrist_pos = self.get_wrist_position(skeleton_index)
        tracker_pos = self.get_tracker_position(tracker_index)

        if wrist_pos is None or tracker_pos is None:
            return None

        # 计算欧几里得距离
        error_x = tracker_pos[0] - wrist_pos[0]
        error_y = tracker_pos[1] - wrist_pos[1]
        error_z = tracker_pos[2] - wrist_pos[2]
        distance = (error_x**2 + error_y**2 + error_z**2)**0.5

        # 存储误差历史
        error_vector = [error_x, error_y, error_z]
        self.error_history.append({
            'vector': error_vector,
            'distance': distance,
            'wrist_pos': wrist_pos,
            'tracker_pos': tracker_pos
        })

        # 限制历史数据长度
        if len(self.error_history) > self.max_error_history:
            self.error_history = self.error_history[-self.max_error_history:]

        # 检查坐标系问题
        coordinate_issue = False
        relative_error = error_vector  # 默认使用原始误差

        if len(self.error_history) >= 20:
            # 计算历史误差的统计
            import numpy as np
            error_vectors = np.array([e['vector'] for e in self.error_history])
            mean_error = np.mean(error_vectors, axis=0)
            std_error = np.std(error_vectors, axis=0)
            mean_distance = np.mean([e['distance'] for e in self.error_history])

            # 如果平均误差大(>0.5米)但标准差小(<0.1米)，可能是坐标系问题
            if mean_distance > 0.5 and np.mean(std_error) < 0.1:
                coordinate_issue = True
                # 计算相对于平均误差的当前误差（这消除了固定的坐标系偏移）
                relative_error = error_vector - mean_error
                relative_distance = np.linalg.norm(relative_error)

                # 显示警告（仅一次）
                if not self.coordinate_warning_shown:
                    print(f"[WARNING] 检测到可能的坐标系不匹配！")
                    print(f"          平均误差: {mean_distance:.2f}m, 但误差稳定 (标准差: {np.mean(std_error):.3f}m)")
                    print(f"          这表示手腕和Tracker使用不同的坐标系原点。")
                    print(f"          将计算相对误差进行校准分析。")
                    print(f"          请检查MANUS Core中Tracker的配置。")
                    self.coordinate_warning_shown = True

        return {
            'wrist_position': wrist_pos,
            'tracker_position': tracker_pos,
            'error_vector': error_vector,  # 原始误差
            'relative_error_vector': relative_error,  # 相对误差（如果坐标系有问题）
            'distance_m': distance,
            'coordinate_issue': coordinate_issue,
            'has_tracker_data': tracker_pos is not None,
            'has_skeleton_data': wrist_pos is not None
        }

    def stop(self):
        """停止接收器"""
        self.running = False

        if self.client_socket:
            self.client_socket.close()
            self.client_socket = None

        if self.server_socket:
            self.server_socket.close()
            self.server_socket = None

        print("[STOP] 数据接收器已停止")

        # 显示最终统计
        if self.start_time:
            elapsed = time.time() - self.start_time
            print(f"[STATS] 最终统计: {self.frame_count} 帧 | 总时间: {elapsed:.1f}s | 平均FPS: {self.frame_count/elapsed:.1f}")

# ==================== 示例使用方式 ====================

def example_callback(frame_data: Dict[str, Any]):
    """示例回调函数：打印骨架和Tracker数据"""
    # 骨架数据处理
    skeleton_info = ""
    if 'skeletons' in frame_data and frame_data['skeletons']:
        skeleton = frame_data['skeletons'][0]
        node_count = len(skeleton['nodes']) if 'nodes' in skeleton else 0
        skeleton_info = f"骨架:{node_count}节点"

        # 每10帧打印一次手腕位置（节点0）
        if frame_data.get('frame', 0) % 10 == 0 and node_count > 0:
            wrist_node = skeleton['nodes'][0]
            wrist_pos = wrist_node['position']
            print(f"[HAND] 手腕位置: [{wrist_pos[0]:.3f}, {wrist_pos[1]:.3f}, {wrist_pos[2]:.3f}]")

    # Tracker数据处理
    tracker_info = ""
    if 'trackers' in frame_data and frame_data['trackers']:
        tracker_count = len(frame_data['trackers'])
        tracker_info = f"Tracker:{tracker_count}个"

        # 打印第一个tracker的位置（假设是手腕的）
        if tracker_count > 0:
            first_tracker = frame_data['trackers'][0]
            if 'position' in first_tracker:
                tracker_pos = first_tracker['position']
                print(f"🎯 Tracker位置: [{tracker_pos[0]:.3f}, {tracker_pos[1]:.3f}, {tracker_pos[2]:.3f}] "
                      f"有效: {first_tracker.get('valid', False)}")

        # 每10帧计算并显示offset误差
        if frame_data.get('frame', 0) % 10 == 0:
            if skeleton_info and tracker_info:
                # 计算手腕和tracker的offset误差
                wrist_pos = None
                tracker_pos = None

                if 'skeletons' in frame_data and frame_data['skeletons'] and frame_data['skeletons'][0]['nodes']:
                    wrist_pos = frame_data['skeletons'][0]['nodes'][0]['position']
                if 'trackers' in frame_data and frame_data['trackers'] and 'position' in frame_data['trackers'][0]:
                    tracker_pos = frame_data['trackers'][0]['position']

                if wrist_pos is not None and tracker_pos is not None:
                    error_x = tracker_pos[0] - wrist_pos[0]
                    error_y = tracker_pos[1] - wrist_pos[1]
                    error_z = tracker_pos[2] - wrist_pos[2]
                    distance = (error_x**2 + error_y**2 + error_z**2)**0.5
                    print(f"📏 Offset误差: [{error_x:.3f}, {error_y:.3f}, {error_z:.3f}] | 距离: {distance:.3f}m")

    # 每50帧打印完整数据结构样本
    if frame_data.get('frame', 0) % 50 == 0:
        print(f"📋 数据结构样本 [帧:{frame_data.get('frame', 0)}]:")
        print(f"  时间戳: {frame_data.get('timestamp', 0)}")
        print(f"  骨架: {len(frame_data.get('skeletons', []))}个")
        print(f"  Tracker: {len(frame_data.get('trackers', []))}个")
        if 'trackers' in frame_data and frame_data['trackers']:
            print(f"  第一个Tracker deviceId: {frame_data['trackers'][0].get('deviceId', 'N/A')}")
        if 'skeletons' in frame_data and frame_data['skeletons']:
            print(f"  第一个骨架 gloveId: {frame_data['skeletons'][0].get('gloveId', 'N/A')}")

    # 打印基本信息（每帧）
    if skeleton_info or tracker_info:
        info_parts = []
        if skeleton_info:
            info_parts.append(skeleton_info)
        if tracker_info:
            info_parts.append(tracker_info)
        info_str = " | ".join(info_parts)
        print(f"📦 收到数据: {info_str} | 帧:{frame_data.get('frame', 0)} | 时间戳:{frame_data.get('timestamp', 0)}")

def process_for_virtual_environment(frame_data: Dict[str, Any]):
    """为虚拟环境处理数据（示例）"""
    # 这里可以添加您的虚拟环境处理逻辑
    # 例如：转换坐标系、应用平滑滤波、触发虚拟手势等
    pass

if __name__ == "__main__":
    # 创建数据接收器
    receiver = ManusDataReceiver(host="127.0.0.1", port=8888)

    try:
        # 注册回调函数
        receiver.register_callback(example_callback)
        receiver.register_callback(process_for_virtual_environment)

        # 启动接收器（会阻塞直到连接）
        receiver.start()

        # 保持运行（这里可以添加您的虚拟环境主循环）
        print("\n[READY] 数据流已建立，可以开始虚拟环境开发")
        print("按 Ctrl+C 停止接收器\n")

        # 示例：持续检查数据
        while receiver.running:
            # 在这里可以添加您的虚拟环境渲染/逻辑循环
            time.sleep(0.016)  # 约60Hz

    except KeyboardInterrupt:
        print("\n[STOP] 用户中断")
    except Exception as e:
        print(f"[ERROR] 运行错误: {e}")
    finally:
        receiver.stop()