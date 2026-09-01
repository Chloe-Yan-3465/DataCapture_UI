# test_coordinate_correction.py
import json
import numpy as np

# 模拟你的数据
wrist_pos = [2.388778, 2.882013, -1.130383]
tracker_pos = [0.0, 0.0, 0.03]

# 计算基准偏移
reference = wrist_pos  # 使用第一次数据作为参考
corrected_wrist = [
    wrist_pos[0] - reference[0],
    wrist_pos[1] - reference[1],
    wrist_pos[2] - reference[2]
]

print(f"原始手腕位置: {wrist_pos}")
print(f"校正后手腕位置: {corrected_wrist}")
print(f"Tracker位置: {tracker_pos}")

error_x = tracker_pos[0] - corrected_wrist[0]
error_y = tracker_pos[1] - corrected_wrist[1]
error_z = tracker_pos[2] - corrected_wrist[2]
distance = np.sqrt(error_x**2 + error_y**2 + error_z**2)

print(f"\n校正后误差: {distance*100:.1f}cm")
print(f"误差向量: [{error_x:.3f}, {error_y:.3f}, {error_z:.3f}] m")