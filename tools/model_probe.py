"""进食/情绪模型诊断探针 —— 采一段时间画面，逐帧记录模型输出并给出结论。

用于排查两类问题：①咀嚼次数/进食速度不生效 ②情绪识别不稳或不出结果。
它走的是生产同一套代码（VisionService 的手口线程 + 感知线程），只是把
GLM-4V 云端分析关掉，避免调试时产生外部请求与费用。

用法（项目根目录下）:
    .venv\\Scripts\\python.exe tools\\model_probe.py                     # 摄像头采样 20 秒
    .venv\\Scripts\\python.exe tools\\model_probe.py --seconds 60
    .venv\\Scripts\\python.exe tools\\model_probe.py --video demo.mp4    # 用录制视频复现
    .venv\\Scripts\\python.exe tools\\model_probe.py --json data_local\\probe.json

输出：
    1) 模型可用性检查（mediapipe / 手部-面部关键点 / HSEmotion 情绪 / 咀嚼模型）
    2) 逐次采样记录：人脸数、情绪、咀嚼次数/分、送食次数/分、头部移动
    3) 汇总结论：哪个模型没产生非零输出 + 下一步排查建议
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def check_environment() -> dict:
    """模型与依赖是否就位（不触发下载）。"""
    from app import config

    def _has(module: str) -> bool:
        import importlib.util
        return importlib.util.find_spec(module) is not None

    files = {
        "hand_landmarker.task": config.PATHS.models_dir / "hand_landmarker.task",
        "face_landmarker.task": config.PATHS.models_dir / "face_landmarker.task",
        "enet_b0_8_va_mtl.onnx": config.PATHS.models_dir / "enet_b0_8_va_mtl.onnx",
        "face_detection_yunet_2023mar.onnx": config.PATHS.models_dir / "face_detection_yunet_2023mar.onnx",
        "chewing_model.json": config.PATHS.models_dir / "chewing_model.json",
    }
    return {
        "opencv": _has("cv2"),
        "mediapipe": _has("mediapipe"),
        "onnxruntime": _has("onnxruntime"),
        "models_dir": str(config.PATHS.models_dir),
        "files": {name: path.exists() for name, path in files.items()},
    }


def sample_once(vision) -> dict:
    """取一次两个本地模型的原始输出（不经过 GLM-4V）。"""
    hand = dict(vision._hand_result or {})
    percep = dict(vision._perception_result or {})
    emo = dict(percep.get("emotion") or {})
    chew = dict(percep.get("chewing") or {})
    return {
        "face_count": emo.get("face_count", 0),
        "emotion": emo.get("emotion_zh") or emo.get("emotion") or "",
        "confidence": emo.get("confidence"),
        "chew_count": hand.get("bite_count"),
        "chews_per_min": hand.get("chews_per_min"),
        "chew_level": hand.get("chew_level"),
        "bites_per_min": hand.get("bites_per_min"),
        "eat_level": hand.get("level"),
        "head_motion": hand.get("head_motion"),
        "hands": hand.get("hands"),
        "mouth_dist_ratio": hand.get("mouth_dist_ratio"),
        "near_mouth": hand.get("near_mouth"),
        "perception_chewing": chew.get("chewing"),
        "perception_probability": chew.get("probability"),
        "note": hand.get("note") or percep.get("reason") or percep.get("error") or "",
    }


def summarize(samples: list[dict]) -> dict:
    """把逐次采样汇总成可判读的结论。"""
    if not samples:
        return {"samples": 0}
    faces = [s for s in samples if (s.get("face_count") or 0) > 0]
    bites = [s.get("bites_per_min") or 0 for s in samples]
    chews = [s.get("chews_per_min") or 0 for s in samples]
    emotions: dict[str, int] = {}
    for s in samples:
        name = s.get("emotion") or "未识别"
        emotions[name] = emotions.get(name, 0) + 1

    issues = []
    if not faces:
        issues.append("全程没有检测到人脸：确认镜头对准用户、光线充足，"
                      "或先跑 tests/selfcheck.py 的摄像头步骤。")
    if max(chews) <= 0:
        issues.append("咀嚼速率始终为 0：检查 mediapipe 是否可用、"
                      "hand_landmarker.task / face_landmarker.task 是否存在。")
    if max(bites) <= 0:
        issues.append("送食次数始终为 0：需要手部入镜且靠近嘴部；"
                      "可对着镜头做 3 次「手→嘴」动作复测。")
    if not any(s.get("near_mouth") for s in samples):
        issues.append("从未进入「手贴近嘴」状态：看上面的 mouth_dist_ratio"
                      "（指尖到嘴心距离/嘴宽），>1.5 说明手离嘴太远或关键点没跟上。")
    notes = {s.get("note") for s in samples if s.get("note")}
    if notes:
        issues.append("模型提示：" + "；".join(sorted(notes)))
    return {
        "samples": len(samples),
        "face_samples": len(faces),
        "emotion_distribution": emotions,
        "chews_per_min_max": max(chews),
        "bites_per_min_max": max(bites),
        "near_mouth_samples": sum(1 for s in samples if s.get("near_mouth")),
        "mouth_dist_ratio_min": min([s["mouth_dist_ratio"] for s in samples
                                     if s.get("mouth_dist_ratio") is not None],
                                    default=None),
        "perception_chewing_hits": sum(1 for s in samples if s.get("perception_chewing")),
        "issues": issues,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="进食/情绪模型诊断探针")
    ap.add_argument("--seconds", type=float, default=20.0, help="采样时长（秒）")
    ap.add_argument("--interval", type=float, default=0.5, help="采样间隔（秒）")
    ap.add_argument("--video", type=Path, help="改用视频文件复现（循环播放）")
    ap.add_argument("--json", dest="json_path", type=Path,
                    default=Path("data_local") / "model_probe.json")
    args = ap.parse_args()

    env = check_environment()
    print("=== 模型可用性 ===")
    print(f"opencv={env['opencv']} mediapipe={env['mediapipe']} onnxruntime={env['onnxruntime']}")
    print(f"模型目录: {env['models_dir']}")
    for name, ok in env["files"].items():
        print(f"  {'[OK]' if ok else '[缺失]'} {name}")

    from app.vision.service import vision_service

    # 调试期间不调用云端视觉模型（专注本地咀嚼/情绪两个模型）
    vision_service.llm_interval = float("inf")

    cap = None
    if args.video:
        import cv2
        cap = cv2.VideoCapture(str(args.video))
        if not cap.isOpened():
            print(f"[错误] 打不开视频: {args.video}")
            return 2

        def _capture():
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            if not ok:
                raise RuntimeError("视频读取结束")
            return frame

        vision_service._capture_webcam = _capture
        vision_service._probe_mode = lambda: "webcam"
        print(f"[视频模式] {args.video}")

    vision_service.start()
    print("[启动] 视觉服务已启动，等待模型加载…")
    time.sleep(3.0)          # 等 MediaPipe / ONNX 首次加载

    samples: list[dict] = []
    t0 = time.time()
    print("\n=== 逐次采样（人脸 / 情绪 / 咀嚼·分 / 送食·分 / 头动）===")
    try:
        while time.time() - t0 < args.seconds:
            s = sample_once(vision_service)
            s["t"] = round(time.time() - t0, 1)
            samples.append(s)
            print(f"{s['t']:5.1f}s  脸{s['face_count']}  {s['emotion'] or '-':<4}"
                  f"  咀嚼 {s['chews_per_min'] if s['chews_per_min'] is not None else '-':>5}"
                  f"  送食 {s['bites_per_min'] if s['bites_per_min'] is not None else '-':>5}"
                  f"  距离 {s['mouth_dist_ratio'] if s['mouth_dist_ratio'] is not None else '-':>5}"
                  f"  头动 {'是' if s['head_motion'] else '否'}")
            time.sleep(max(0.05, args.interval))
    except KeyboardInterrupt:
        print("\n[中断] 提前结束采样")
    finally:
        vision_service.stop()
        if cap is not None:
            cap.release()

    report = {"environment": env, "samples": samples, "summary": summarize(samples)}
    args.json_path.parent.mkdir(parents=True, exist_ok=True)
    args.json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    print("\n=== 汇总 ===")
    for key, value in report["summary"].items():
        print(f"{key}: {value}")
    print(f"\n完整记录已写入 {args.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
