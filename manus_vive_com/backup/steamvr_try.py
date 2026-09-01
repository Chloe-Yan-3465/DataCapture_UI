import openvr
import numpy as np
import time
import math
from scipy.spatial.transform import Rotation

class ViveTrackerReader:
    def __init__(self):
        """初始化SteamVR连接"""
        self.vr = None
        self.trackers = {}
        self.initialize_vr()
        
    def initialize_vr(self):
        """初始化OpenVR"""
        try:
            self.vr = openvr.init(openvr.VRApplication_Other)
            print("SteamVR连接成功")
            print(f"OpenVR版本: {self.vr.getRuntimeVersion()}")
        except Exception as e:
            print(f"SteamVR初始化失败: {e}")
            raise
    
    def list_all_devices(self):
        """列出所有连接的设备"""
        print("\n=== 已连接的设备 ===")
        for i in range(openvr.k_unMaxTrackedDeviceCount):
            if self.vr.isTrackedDeviceConnected(i):
                device_class = self.vr.getTrackedDeviceClass(i)
                device_name = self.get_device_name(i)
                device_serial = self.get_device_serial(i)
                
                device_types = {
                    openvr.TrackedDeviceClass_Invalid: "无效设备",
                    openvr.TrackedDeviceClass_HMD: "头显",
                    openvr.TrackedDeviceClass_Controller: "控制器",
                    openvr.TrackedDeviceClass_GenericTracker: "追踪器",
                    openvr.TrackedDeviceClass_TrackingReference: "基站",
                    openvr.TrackedDeviceClass_DisplayRedirect: "显示重定向"
                }
                
                print(f"设备 {i}: {device_types.get(device_class, '未知')}")
                print(f"  名称: {device_name}")
                print(f"  序列号: {device_serial}")
                print("---")
    
    def get_device_name(self, device_index):
        """获取设备名称"""
        try:
            result, error = self.vr.getStringTrackedDeviceProperty(
                device_index, 
                openvr.Prop_ModelNumber_String
            )
            return result if result else f"设备_{device_index}"
        except:
            return f"设备_{device_index}"
    
    def get_device_serial(self, device_index):
        """获取设备序列号"""
        try:
            result, error = self.vr.getStringTrackedDeviceProperty(
                device_index, 
                openvr.Prop_SerialNumber_String
            )
            return result if result else "未知序列号"
        except:
            return "未知序列号"
    
    def matrix_to_rotation_euler(self, matrix):
        """将旋转矩阵转换为欧拉角（度）"""
        try:
            # 从3x3矩阵创建Rotation对象
            rot = Rotation.from_matrix(matrix)
            euler = rot.as_euler('xyz', degrees=True)  # xyz顺序，输出角度
            return euler
        except Exception as e:
            print(f"旋转转换错误: {e}")
            return np.array([0, 0, 0])
    
    def matrix_to_quaternion(self, matrix):
        """将旋转矩阵转换为四元数"""
        try:
            rot = Rotation.from_matrix(matrix)
            quat = rot.as_quat()  # [x, y, z, w] 格式
            return quat
        except Exception as e:
            print(f"四元数转换错误: {e}")
            return np.array([0, 0, 0, 1])
    
    def get_tracker_poses(self, universe_type=openvr.TrackingUniverseStanding):
        """获取所有Tracker的位姿"""
        # 清空之前的追踪器数据
        self.trackers = {}
        
        # 获取所有设备的位姿
        poses = self.vr.getDeviceToAbsoluteTrackingPose(
            universe_type, 
            0,  # 预测时间（秒），0表示使用最新数据
            openvr.k_unMaxTrackedDeviceCount
        )
        
        for i in range(openvr.k_unMaxTrackedDeviceCount):
            pose = poses[i]
            
            # 检查设备是否连接且位姿有效
            if not pose.bPoseIsValid or not pose.bDeviceIsConnected:
                continue
            
            # 检查设备类型
            device_class = self.vr.getTrackedDeviceClass(i)
            
            if device_class == openvr.TrackedDeviceClass_GenericTracker:
                try:
                    # 获取设备信息
                    device_name = self.get_device_name(i)
                    device_serial = self.get_device_serial(i)
                    
                    # 提取位姿矩阵
                    matrix = pose.mDeviceToAbsoluteTracking
                    
                    # 转换为numpy数组（4x4齐次坐标矩阵）
                    pose_matrix = np.array([
                        [matrix[0][0], matrix[0][1], matrix[0][2], matrix[0][3]],
                        [matrix[1][0], matrix[1][1], matrix[1][2], matrix[1][3]],
                        [matrix[2][0], matrix[2][1], matrix[2][2], matrix[2][3]],
                        [0.0, 0.0, 0.0, 1.0]
                    ])
                    
                    # 提取位置（单位：米）
                    position = pose_matrix[:3, 3]
                    
                    # 提取旋转矩阵
                    rotation_matrix = pose_matrix[:3, :3]
                    
                    # 转换为欧拉角和四元数
                    euler_angles = self.matrix_to_rotation_euler(rotation_matrix)
                    quaternion = self.matrix_to_quaternion(rotation_matrix)
                    
                    # 提取速度信息（单位：米/秒）
                    velocity = np.array([
                        pose.vVelocity[0], 
                        pose.vVelocity[1], 
                        pose.vVelocity[2]
                    ])
                    
                    # 提取角速度信息（单位：弧度/秒）
                    angular_velocity = np.array([
                        pose.vAngularVelocity[0], 
                        pose.vAngularVelocity[1], 
                        pose.vAngularVelocity[2]
                    ])
                    
                    # 计算速度大小
                    speed = np.linalg.norm(velocity)
                    
                    # 计算角速度大小
                    angular_speed = np.linalg.norm(angular_velocity)
                    
                    # 存储追踪器数据
                    self.trackers[device_serial] = {
                        'device_index': i,
                        'device_name': device_name,
                        'serial': device_serial,
                        'position': position,            # [x, y, z] 米
                        'rotation_matrix': rotation_matrix,  # 3x3旋转矩阵
                        'euler_angles': euler_angles,    # [roll, pitch, yaw] 度
                        'quaternion': quaternion,        # [x, y, z, w]
                        'velocity': velocity,           # [vx, vy, vz] 米/秒
                        'angular_velocity': angular_velocity, # [wx, wy, wz] 弧度/秒
                        'speed': speed,                 # 标量速度
                        'angular_speed': angular_speed,  # 标量角速度
                        'timestamp': time.time(),       # 时间戳
                        'pose_is_valid': pose.bPoseIsValid,
                        'device_is_connected': pose.bDeviceIsConnected
                    }
                    
                except Exception as e:
                    print(f"处理追踪器 {i} 时出错: {e}")
                    continue
        
        return self.trackers
    
    def format_output(self, tracker_data):
        """格式化输出追踪器数据"""
        output = []
        output.append(f"序列号: {tracker_data['serial']}")
        output.append(f"名称: {tracker_data['device_name']}")
        output.append(f"设备索引: {tracker_data['device_index']}")
        output.append("")
        output.append(f"位置 (米):")
        output.append(f"  X: {tracker_data['position'][0]:.4f}")
        output.append(f"  Y: {tracker_data['position'][1]:.4f}")
        output.append(f"  Z: {tracker_data['position'][2]:.4f}")
        output.append("")
        output.append(f"欧拉角 (度):")
        output.append(f"  翻滚(Roll/X): {tracker_data['euler_angles'][0]:.2f}°")
        output.append(f"  俯仰(Pitch/Y): {tracker_data['euler_angles'][1]:.2f}°")
        output.append(f"  偏航(Yaw/Z): {tracker_data['euler_angles'][2]:.2f}°")
        output.append("")
        output.append(f"四元数:")
        output.append(f"  X: {tracker_data['quaternion'][0]:.4f}")
        output.append(f"  Y: {tracker_data['quaternion'][1]:.4f}")
        output.append(f"  Z: {tracker_data['quaternion'][2]:.4f}")
        output.append(f"  W: {tracker_data['quaternion'][3]:.4f}")
        output.append("")
        output.append(f"速度 (米/秒):")
        output.append(f"  Vx: {tracker_data['velocity'][0]:.4f}")
        output.append(f"  Vy: {tracker_data['velocity'][1]:.4f}")
        output.append(f"  Vz: {tracker_data['velocity'][2]:.4f}")
        output.append(f"  总速度: {tracker_data['speed']:.4f}")
        output.append("")
        output.append(f"角速度 (弧度/秒):")
        output.append(f"  Wx: {tracker_data['angular_velocity'][0]:.4f}")
        output.append(f"  Wy: {tracker_data['angular_velocity'][1]:.4f}")
        output.append(f"  Wz: {tracker_data['angular_velocity'][2]:.4f}")
        output.append(f"  总角速度: {tracker_data['angular_speed']:.4f}")
        output.append("-" * 50)
        
        return "\n".join(output)
    
    def continuous_tracking(self, interval=0.1, duration=None):
        """
        持续跟踪追踪器
        interval: 更新间隔（秒）
        duration: 跟踪持续时间（秒），None表示无限
        """
        print(f"\n开始追踪 (间隔: {interval}s)")
        start_time = time.time()
        frame_count = 0
        
        try:
            while True:
                if duration and (time.time() - start_time > duration):
                    print(f"跟踪完成，共 {frame_count} 帧")
                    break
                
                # 清屏（可选）
                # print("\033[2J\033[H")  # Linux/Mac
                # os.system('cls' if os.name == 'nt' else 'clear')  # Windows/Linux/Mac
                
                # 获取位姿
                trackers = self.get_tracker_poses()
                
                print(f"\n=== 帧 {frame_count + 1} | 时间: {time.time():.3f}s ===")
                
                if not trackers:
                    print("未检测到追踪器")
                else:
                    for serial, data in trackers.items():
                        print(self.format_output(data))
                
                frame_count += 1
                time.sleep(interval)
                
        except KeyboardInterrupt:
            print("\n用户中断跟踪")
        finally:
            self.cleanup()
    
    def single_measurement(self):
        """单次测量并输出"""
        print("\n=== 单次测量 ===")
        trackers = self.get_tracker_poses()
        
        if not trackers:
            print("未检测到追踪器")
            return
        
        print(f"检测到 {len(trackers)} 个追踪器:\n")
        
        for serial, data in trackers.items():
            print(self.format_output(data))
    
    def save_trajectory(self, filename="tracker_trajectory.csv", duration=10, interval=0.1):
        """保存轨迹到CSV文件"""
        print(f"\n开始记录轨迹到 {filename} (持续 {duration} 秒)")
        
        import csv
        
        with open(filename, 'w', newline='') as csvfile:
            fieldnames = [
                'timestamp', 'serial', 'x', 'y', 'z', 
                'roll', 'pitch', 'yaw',
                'qx', 'qy', 'qz', 'qw',
                'vx', 'vy', 'vz', 'speed',
                'wx', 'wy', 'wz', 'angular_speed'
            ]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            
            start_time = time.time()
            
            try:
                while time.time() - start_time < duration:
                    trackers = self.get_tracker_poses()
                    
                    for serial, data in trackers.items():
                        row = {
                            'timestamp': data['timestamp'],
                            'serial': data['serial'],
                            'x': data['position'][0],
                            'y': data['position'][1],
                            'z': data['position'][2],
                            'roll': data['euler_angles'][0],
                            'pitch': data['euler_angles'][1],
                            'yaw': data['euler_angles'][2],
                            'qx': data['quaternion'][0],
                            'qy': data['quaternion'][1],
                            'qz': data['quaternion'][2],
                            'qw': data['quaternion'][3],
                            'vx': data['velocity'][0],
                            'vy': data['velocity'][1],
                            'vz': data['velocity'][2],
                            'speed': data['speed'],
                            'wx': data['angular_velocity'][0],
                            'wy': data['angular_velocity'][1],
                            'wz': data['angular_velocity'][2],
                            'angular_speed': data['angular_speed']
                        }
                        writer.writerow(row)
                    
                    time.sleep(interval)
                
                print(f"轨迹保存完成: {filename}")
                
            except KeyboardInterrupt:
                print("用户中断记录")
    
    def cleanup(self):
        """清理资源"""
        if self.vr:
            openvr.shutdown()
            print("SteamVR连接已关闭")


# 使用示例
if __name__ == "__main__":
    # 安装所需库:
    # pip install openvr numpy scipy
    
    try:
        # 创建追踪器读取器
        tracker_reader = ViveTrackerReader()
        
        # 1. 列出所有设备
        tracker_reader.list_all_devices()
        
        # 2. 单次测量
        tracker_reader.single_measurement()
        
        # 3. 持续跟踪5秒
        print("\n准备开始持续跟踪，按Ctrl+C停止...")
        time.sleep(2)
        tracker_reader.continuous_tracking(interval=0.5, duration=5)
        
        # 4. 保存轨迹到文件
        # tracker_reader.save_trajectory(duration=5, interval=0.1)
        
    except Exception as e:
        print(f"程序出错: {e}")
    finally:
        if 'tracker_reader' in locals():
            tracker_reader.cleanup()