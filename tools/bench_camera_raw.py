# 裸摄像头读取基准：确认相机当前真实帧周期（对照组，不涉及服务代码）
import statistics
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2  # noqa: E402

cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
cap.set(cv2.CAP_PROP_FPS, 30)
print("negotiated:", cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
      cap.get(cv2.CAP_PROP_FPS))
for _ in range(6):  # 预热
    cap.read()

for round_no in range(1, 4):
    reads = []
    for _ in range(20):
        t0 = time.perf_counter()
        ok, frame = cap.read()
        reads.append(time.perf_counter() - t0)
    med = statistics.median(reads) * 1000
    print(f"round {round_no}: median {med:.1f} ms -> {1000 / med:.1f} fps "
          f"(min {min(reads) * 1000:.1f} max {max(reads) * 1000:.1f})")
cap.release()
