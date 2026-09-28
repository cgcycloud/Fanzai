#!/bin/bash
# 正念饭崽（mindful_meal）树莓派一键部署
# 用法：chmod +x deploy/setup_pi.sh && sudo ./deploy/setup_pi.sh
set -e

APP_DIR="${APP_DIR:-/opt/mindful_meal}"
DATA_DIR="/data"
MODEL_DIR="$DATA_DIR/models"
SHERPA_MODEL="sherpa-onnx-paraformer-zh-small"
SHERPA_REPO="csukuangfj/sherpa-onnx-paraformer-zh-small-2024-03-09"

echo "=== 正念饭崽树莓派部署开始: $(date) ==="

# ---------- 1. 系统依赖 ----------
echo ""
echo "【1/5】安装系统依赖…"
apt-get update -qq
apt-get install -y python3-pip python3-venv python3-opencv \
    python3-lgpio python3-gpiozero python3-picamera2 \
    alsa-utils unzip wget ffmpeg
echo "OK: 系统依赖完成（arecord/aplay 来自 alsa-utils，ffmpeg 供音频转码）"

# ---------- 2. Python 依赖 ----------
echo ""
echo "【2/5】安装 Python 依赖…"
cd "$APP_DIR"
python3 -m venv .venv --system-site-packages   # 复用系统 picamera2/lgpio
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
./.venv/bin/pip install -q -r requirements-pi.txt
echo "OK: Python 依赖完成"

# ---------- 3. 模型资产 ----------
echo ""
echo "【3/5】准备模型资产…"
mkdir -p "$MODEL_DIR" "$DATA_DIR/media" "$DATA_DIR/logs"
cp -rn "$APP_DIR/models/." "$MODEL_DIR/"
for f in sherpa-onnx-paraformer-zh-small/model.int8.onnx \
         sherpa-onnx-paraformer-zh-small/tokens.txt \
         sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/encoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx \
         sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/decoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx \
         sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/joiner-epoch-99-avg-1-chunk-16-left-64.int8.onnx \
         emotion-ferplus-8.onnx enet_b0_8_va_mtl.onnx \
         face_detection_yunet_2023mar.onnx face_landmarker.task hand_landmarker.task; do
    [ -s "$MODEL_DIR/$f" ] || { echo "ERROR: 缺少模型 $MODEL_DIR/$f"; exit 1; }
done
echo "OK: 仓库模型已复制到 $MODEL_DIR"

# ---------- 4. 权限 ----------
echo ""
echo "【4/5】设置权限…"
chmod -R 777 "$DATA_DIR"
echo "OK"

# ---------- 5. systemd 服务 ----------
echo ""
echo "【5/5】安装 systemd 服务…"
sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/mindful-meal.service" \
    > /etc/systemd/system/mindful-meal.service
systemctl daemon-reload
systemctl enable mindful-meal.service
echo "OK: 服务已注册（未自动启动，请先配置 AI Key）"
echo ""
echo "=== 部署完成 ==="
echo "下一步："
echo "  1) 配置 AI Key（加密落盘）:"
echo "     cd $APP_DIR && ./.venv/bin/python -m app.main ai-config \\"
echo "        --api-url https://open.bigmodel.cn/api/paas/v4 --model glm-4-flash --api-key <你的Key>"
echo "  2) 自检: ./.venv/bin/python tests/selfcheck.py"
echo "  3) 启动: sudo systemctl start mindful-meal"
echo "  4) 浏览器访问: http://<树莓派IP>:8765"
