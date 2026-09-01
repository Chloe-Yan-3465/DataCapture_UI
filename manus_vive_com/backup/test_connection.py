#!/usr/bin/env python3
"""
测试TCP连接 - 简单版本
检查C++客户端是否在发送数据
"""

import socket
import threading
import time

def simple_receiver():
    """简单的TCP接收器，只显示连接状态"""
    host = "127.0.0.1"
    port = 8888

    print(f"[TEST] 启动TCP接收器在 {host}:{port}")
    print("[TEST] 等待C++客户端连接...")
    print("[TEST] 确保已运行: SDKMinimalClient_socket.exe")

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_socket.bind((host, port))
        server_socket.listen(1)

        # 设置超时，避免无限等待
        server_socket.settimeout(10)

        print("[TEST] 服务器已启动，等待连接...")

        try:
            client_socket, client_address = server_socket.accept()
            print(f"[TEST] 客户端已连接: {client_address}")

            # 设置接收超时
            client_socket.settimeout(5)

            # 接收一些数据
            print("[TEST] 等待数据...")
            data_count = 0

            while True:
                try:
                    data = client_socket.recv(4096)
                    if not data:
                        print("[TEST] 客户端断开连接")
                        break

                    data_count += 1
                    if data_count % 10 == 0:
                        print(f"[TEST] 已接收 {data_count} 个数据包")

                    # 尝试解码为JSON
                    try:
                        text = data.decode('utf-8')
                        if data_count == 1:
                            print(f"[TEST] 第一条数据示例: {text[:100]}...")
                    except:
                        pass

                except socket.timeout:
                    print("[TEST] 接收超时")
                    break
                except Exception as e:
                    print(f"[TEST] 接收错误: {e}")
                    break

        except socket.timeout:
            print("[TEST] 连接超时 - C++客户端没有连接")
            print("[TEST] 请检查:")
            print("  1. C++客户端是否正在运行")
            print("  2. C++客户端是否配置为连接到 127.0.0.1:8888")
            print("  3. 防火墙是否阻止了连接")

    except Exception as e:
        print(f"[TEST] 服务器错误: {e}")
    finally:
        server_socket.close()
        print("[TEST] 服务器已关闭")

def check_port():
    """检查端口是否被占用"""
    import socket

    port = 8888
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        sock.bind(("127.0.0.1", port))
        print(f"[TEST] 端口 {port} 可用")
        sock.close()
        return True
    except socket.error as e:
        print(f"[TEST] 端口 {port} 被占用: {e}")
        print("[TEST] 可能已有Python接收器在运行")
        return False

def main():
    print("="*60)
    print("TCP连接测试工具")
    print("="*60)

    # 检查端口
    if not check_port():
        print("\n[TEST] 请关闭其他正在运行的接收器")
        return

    print("\n[TEST] 启动接收器...")
    simple_receiver()

if __name__ == "__main__":
    main()