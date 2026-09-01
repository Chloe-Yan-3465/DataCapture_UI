#!/usr/bin/env python3
"""
Tracker数据诊断工具
检查C++客户端是否发送Tracker数据
"""

import socket
import json
import threading
import time

class TrackerDiagnostic:
    """诊断C++客户端的数据流"""

    def __init__(self, host="127.0.0.1", port=8888):
        self.host = host
        self.port = port
        self.server_socket = None
        self.client_socket = None
        self.running = False

        # 统计信息
        self.total_frames = 0
        self.skeleton_frames = 0
        self.tracker_frames = 0
        self.mixed_frames = 0
        self.empty_frames = 0

    def start(self):
        """启动诊断服务器"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(1)

        print("="*70)
        print("Tracker数据诊断工具")
        print("="*70)
        print(f"监听端口: {self.host}:{self.port}")
        print("请确保C++客户端正在运行并连接到这个端口")
        print("="*70)

        self.running = True

        # 接受客户端连接
        self.client_socket, client_address = self.server_socket.accept()
        print(f"[DIAG] 客户端已连接: {client_address}")

        # 启动数据接收线程
        receive_thread = threading.Thread(target=self._receive_data, daemon=True)
        receive_thread.start()

        # 显示统计信息
        self._show_stats_thread()

        return True

    def _receive_data(self):
        """接收数据并分析"""
        buffer = ""

        while self.running and self.client_socket:
            try:
                # 接收数据
                data = self.client_socket.recv(4096)
                if not data:
                    print("[DIAG] 客户端断开连接")
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
                            self._analyze_frame(frame_data)
                        except json.JSONDecodeError as e:
                            print(f"[DIAG] JSON解析错误: {e}")
                            print(f"[DIAG] 原始数据: {json_str[:100]}...")
                            continue

            except (ConnectionResetError, ConnectionAbortedError) as e:
                print(f"[DIAG] 连接异常: {e}")
                break
            except Exception as e:
                print(f"[DIAG] 接收数据错误: {e}")
                break

        self.stop()

    def _analyze_frame(self, frame_data):
        """分析一帧数据"""
        self.total_frames += 1

        has_skeletons = 'skeletons' in frame_data and bool(frame_data['skeletons'])
        has_trackers = 'trackers' in frame_data and bool(frame_data['trackers'])

        if has_skeletons and has_trackers:
            self.mixed_frames += 1
            frame_type = "混合数据 (骨架+Tracker)"
        elif has_skeletons:
            self.skeleton_frames += 1
            frame_type = "仅骨架数据"
        elif has_trackers:
            self.tracker_frames += 1
            frame_type = "仅Tracker数据"
        else:
            self.empty_frames += 1
            frame_type = "空数据"

        # 每10帧显示一次详细信息
        if self.total_frames % 10 == 0:
            print(f"\n[DIAG] 帧 {self.total_frames}: {frame_type}")
            print(f"  时间戳: {frame_data.get('timestamp', 'N/A')}")
            print(f"  帧编号: {frame_data.get('frame', 'N/A')}")

            if has_skeletons:
                skeletons = frame_data['skeletons']
                print(f"  骨架数量: {len(skeletons)}")
                if skeletons:
                    skeleton = skeletons[0]
                    print(f"  手套ID: {skeleton.get('gloveId', 'N/A')}")
                    print(f"  节点数量: {len(skeleton.get('nodes', []))}")

            if has_trackers:
                trackers = frame_data['trackers']
                print(f"  Tracker数量: {len(trackers)}")
                if trackers:
                    tracker = trackers[0]
                    print(f"  设备ID: {tracker.get('deviceId', 'N/A')}")
                    print(f"  是否有效: {tracker.get('valid', 'N/A')}")
                    pos = tracker.get('position', [0, 0, 0])
                    print(f"  位置: [{pos[0]:.3f}, {pos[1]:.3f}, {pos[2]:.3f}]")

    def _show_stats_thread(self):
        """定期显示统计信息"""
        def stats_loop():
            while self.running:
                time.sleep(5)
                self._show_stats()

        stats_thread = threading.Thread(target=stats_loop, daemon=True)
        stats_thread.start()

    def _show_stats(self):
        """显示统计信息"""
        print("\n" + "="*50)
        print("数据流统计")
        print("="*50)
        print(f"总帧数: {self.total_frames}")
        print(f"仅骨架数据: {self.skeleton_frames} ({self._percentage(self.skeleton_frames, self.total_frames)})")
        print(f"仅Tracker数据: {self.tracker_frames} ({self._percentage(self.tracker_frames, self.total_frames)})")
        print(f"混合数据: {self.mixed_frames} ({self._percentage(self.mixed_frames, self.total_frames)})")
        print(f"空数据: {self.empty_frames} ({self._percentage(self.empty_frames, self.total_frames)})")

        if self.total_frames > 0:
            if self.tracker_frames == 0 and self.mixed_frames == 0:
                print("\n[问题] C++客户端没有发送任何Tracker数据!")
                print("可能原因:")
                print("  1. MANUS Core中没有配置Vive Tracker设备")
                print("  2. C++客户端没有注册Tracker回调")
                print("  3. Tracker设备未连接或未启用")
            elif self.mixed_frames > 0:
                print("\n[良好] C++客户端正在发送骨架和Tracker混合数据")
            elif self.tracker_frames > 0:
                print("\n[警告] C++客户端只发送Tracker数据，没有骨架数据")

        print("="*50)

    def _percentage(self, part, total):
        """计算百分比"""
        if total == 0:
            return "0.0%"
        return f"{100.0 * part / total:.1f}%"

    def stop(self):
        """停止诊断"""
        self.running = False

        if self.client_socket:
            self.client_socket.close()
            self.client_socket = None

        if self.server_socket:
            self.server_socket.close()
            self.server_socket = None

        # 显示最终统计
        self._show_stats()
        print("\n[DIAG] 诊断工具已停止")

def quick_check():
    """快速检查端口和连接"""
    print("="*70)
    print("快速连接检查")
    print("="*70)

    import socket

    # 检查端口是否被占用
    port = 8888
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        sock.bind(("127.0.0.1", port))
        sock.close()
        print("[OK] 端口 8888 可用")
    except socket.error as e:
        print(f"[ERROR] 端口 8888 被占用: {e}")
        print("可能已有其他Python程序在运行")
        print("请关闭其他程序后重试")
        return False

    return True

if __name__ == "__main__":
    # 快速检查
    if not quick_check():
        exit(1)

    print("\n启动诊断工具...")
    print("按Ctrl+C停止诊断\n")

    try:
        diagnostic = TrackerDiagnostic()
        diagnostic.start()

        # 保持运行
        while diagnostic.running:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[DIAG] 用户中断")
    except Exception as e:
        print(f"[DIAG] 运行错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("[DIAG] 诊断完成")