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

# 唤醒(KWS) + 合成(Matcha/VITS) 模型：GitHub Release 在国内常超时，走可用代理。
# 顺序按实测速度排：gh-proxy.com 约 650KB/s，ghfast.top 只有 24KB/s（差 27 倍）。
GH_PROXIES="https://gh-proxy.com/ https://ghfast.top/ https://ghproxy.net/"
# 发声默认用 Kokoro（权重 Apache-2.0，可商用）：树莓派上默认只下 int8 版
# （约 140MB，省内存），设 DOWNLOAD_KOKORO_FP32=1 再额外下 fp32 版（约 350MB，音质最好）。
DOWNLOAD_KOKORO_FP32="${DOWNLOAD_KOKORO_FP32:-0}"
fetch_release() {   # $1=仓库子路径(如 kws-models/xxx.tar.bz2) $2=解压后目录名
    local rel="$1" name="$2"
    if [ -d "$MODEL_DIR/$name" ]; then echo "OK: $name 已存在"; return 0; fi
    for p in $GH_PROXIES ""; do
        echo "下载 $name（经 ${p:-直连}）…"
        if wget -q --show-progress -O "/tmp/$name.tar.bz2" \
                "${p}https://github.com/k2-fsa/sherpa-onnx/releases/download/$rel" \
           && tar -xjf "/tmp/$name.tar.bz2" -C "$MODEL_DIR" \
           && rm -f "/tmp/$name.tar.bz2"; then
            echo "OK: $name"; return 0
        fi
        rm -f "/tmp/$name.tar.bz2"
    done
    echo "WARN: $name 下载失败（可稍后在开发机执行 tools/download_sherpa_models.py 后拷贝）"
    return 1
}
fetch_release "kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2" \
              "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
fetch_release "tts-models/vits-zh-hf-fanchen-C.tar.bz2" "vits-zh-hf-fanchen-C"
# Matcha 22.05kHz：合成最快，但训练数据 Baker **仅限非商用** —— 只作可选项保留
fetch_release "tts-models/matcha-icefall-zh-baker.tar.bz2" "matcha-icefall-zh-baker"
# 默认音色：可商用的 Kokoro（代码会自动在 fp32/int8/v1.0 之间回退，下哪个都能跑）
fetch_release "tts-models/kokoro-int8-multi-lang-v1_1.tar.bz2" "kokoro-int8-multi-lang-v1_1"
if [ "$DOWNLOAD_KOKORO_FP32" = "1" ]; then
    fetch_release "tts-models/kokoro-multi-lang-v1_1.tar.bz2" "kokoro-multi-lang-v1_1"
fi

# Matcha 的模型包里**不含声码器**，必须单独下载一个（缺了这套音色直接不可用）
fetch_vocoder() {   # $1=文件名（vocos-22khz-univ.onnx 51MB / hifigan_v2.onnx 3.6MB）
    local name="$1"
    [ -f "$MODEL_DIR/$name" ] && { echo "OK: $name 已存在"; return 0; }
    for p in $GH_PROXIES ""; do
        echo "下载 $name（经 ${p:-直连}）…"
        if wget -q --show-progress -O "$MODEL_DIR/$name" \
                "${p}https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/$name"; then
            echo "OK: $name"; return 0
        fi
        rm -f "$MODEL_DIR/$name"
    done
    echo "WARN: $name 下载失败（Matcha 音色将不可用，可退回 16kHz VITS）"
}
fetch_vocoder "vocos-22khz-univ.onnx"

# Paraformer 中文模型（识别引擎）：优先 hf-mirror，失败回退 GitHub
if [ ! -f "$MODEL_DIR/$SHERPA_MODEL/model.int8.onnx" ]; then
    mkdir -p "$MODEL_DIR/$SHERPA_MODEL"
    echo "下载 Paraformer 中文模型（约 82MB）…"
    MIRROR="https://hf-mirror.com/$SHERPA_REPO/resolve/main"
    if wget -q --show-progress -O "$MODEL_DIR/$SHERPA_MODEL/model.int8.onnx" "$MIRROR/model.int8.onnx"        && wget -q -O "$MODEL_DIR/$SHERPA_MODEL/tokens.txt" "$MIRROR/tokens.txt"; then
        echo "OK: Paraformer 模型（镜像源）"
    else
        echo "镜像失败，改用 GitHub 发行包…"
        rm -f "$MODEL_DIR/$SHERPA_MODEL/model.int8.onnx"
        wget -q --show-progress -O /tmp/$SHERPA_MODEL.tar.bz2             "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$SHERPA_MODEL-2024-03-09.tar.bz2"           && tar -xjf /tmp/$SHERPA_MODEL.tar.bz2 -C "$MODEL_DIR"           && rm -f /tmp/$SHERPA_MODEL.tar.bz2           && echo "OK: Paraformer 模型（GitHub）"           || echo "WARN: Paraformer 下载失败，识别将不可用（请重试）"
    fi
else
    echo "OK: Paraformer 模型已存在"
fi

# 情绪/咀嚼模型：从项目 models/ 复制到 /data/models（若尚未存在）
for f in emotion-ferplus-8.onnx enet_b0_8_va_mtl.onnx \
         face_detection_yunet_2023mar.onnx face_landmarker.task \
         hand_landmarker.task chewing_model.json; do
    if [ -f "$APP_DIR/models/$f" ] && [ ! -f "$MODEL_DIR/$f" ]; then
        cp "$APP_DIR/models/$f" "$MODEL_DIR/"
    fi
done
[ -d "$APP_DIR/models/emojis" ] && mkdir -p "$MODEL_DIR/emojis" && \
    cp -n "$APP_DIR/models/emojis/"*.png "$MODEL_DIR/emojis/" 2>/dev/null || true
echo "OK: 感知模型已就位"

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
