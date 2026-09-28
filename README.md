# 饭崽（Fanzai）

饭崽是面向老年人正念饮食场景的陪伴项目：摄像头观察进食过程，麦克风接收语音，应用将视觉与对话结果用于屏幕提示、语音反馈、饮食记录和健康分析。可在 Windows 上开发和体验 Web 界面，也提供树莓派摄像头、音频、GPIO 和圆屏的适配。它是辅助工具，不替代医疗诊断或护理。

## 项目如何工作

```text
摄像头 ──> app/vision（人脸/表情、手口动作、咀嚼、食物） ─┐
麦克风 ──> app/voice（唤醒词、语音识别、语音合成） ───────┼─> app/core（状态、进食事件、记忆、健康数据、AI）
硬件 ────> app/hardware（真实设备或模拟设备） ────────────┘                 │
                                                                            ├─> app/api + app/web（浏览器交互与看板）
                                                                            └─> app/display（圆屏画面与输出）
```

`app/main.py` 是命令行入口，`app/server.py` 组合 Web 服务和各 API 路由。`app/config.py` 集中定义模型、数据与运行参数。系统既有本地推理和规则逻辑，也有可选的云端 AI 服务；**仓库包含模型文件不等于所有功能都能离线使用**。

### 主要目录

| 路径 | 作用 |
| --- | --- |
| `app/vision/` | 摄像头画面、咀嚼及手口动作、情绪和食物相关处理 |
| `app/voice/` | 本地唤醒词、Paraformer 语音识别、语音合成及缓存 |
| `app/core/` | 进食状态、对话、记忆、SQLite 数据、统计与清理 |
| `app/api/`、`app/web/` | 对话、视觉、音频、报告、设置等接口与静态网页 |
| `app/hardware/`、`app/display/` | 树莓派外设/模拟设备及显示输出 |
| `models/` | 随仓库提供的 ONNX 和 MediaPipe 模型资产 |
| `deploy/` | 树莓派安装脚本与 systemd 服务文件 |
| `tests/`、`docs/` | 自检和补充文档 |

### 本地与云端能力

- 本地模型用于语音唤醒、中文语音识别以及部分视觉处理。模型文件较大，克隆仓库需要足够空间和下载时间。
- AI 对话、云端视觉理解和云端语音合成取决于所配置的服务、网络及 API Key；未配置时这些能力不可用或退回本地提示/规则路径。
- 实际摄像头、麦克风、扬声器、GPIO 和圆屏是否可用取决于设备与系统依赖；没有硬件时可以使用项目中的模拟实现开发部分功能。

## Windows 快速开始

需要 Python 3.10 或更高版本。进入仓库根目录，在 PowerShell 中运行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python tests/selfcheck.py
.venv\Scripts\python -m app.main serve --port 8765
```

浏览器打开 <http://127.0.0.1:8765>。也可运行 `run.bat` 来准备环境并启动。硬件自检与业务功能测试不同：缺少某项外设并不代表 Web 服务无法启动。

## 树莓派部署

在树莓派上（不是 Windows PowerShell 中）执行：

```bash
sudo git clone https://github.com/cgcycloud/Fanzai.git /opt/mindful_meal
cd /opt/mindful_meal
sudo ./deploy/setup_pi.sh
./.venv/bin/python tests/selfcheck.py
sudo systemctl start mindful-meal
sudo systemctl status mindful-meal
```

安装脚本安装系统和 Python 依赖、创建树莓派自己的 `.venv`、将仓库的模型复制到 `/data/models`、注册开机服务；**脚本不会自动启动服务**。完成后从同一网络中的浏览器访问 `http://<树莓派实际IP>:8765`。配置和连接外设请参考 [`deploy/`](deploy/) 与 [`docs/`](docs/)；部署仍需根据具体硬件测试，不能把 Windows 上的虚拟环境直接搬到树莓派。

## 数据、配置与安全

Windows 上的运行数据保存在仓库下的 `data_local/`；树莓派安装后的数据放在 `/data/`，模型优先从 `/data/models` 读取。数据库、媒体文件、日志以及 AI 配置属于运行数据，不随仓库公开。`.venv/` 也不上传：它包含针对当前系统安装的依赖，应在目标设备重新创建。

可通过 Web 设置或命令行配置 AI 服务，例如：

```bash
python -m app.main ai-config --api-url <服务地址> --model <模型名> --api-key <你的密钥>
```

请只在自己的设备上填写密钥，**不要把真实密钥、私钥、运行数据或个人健康记录提交到公开仓库**。需要使用云端功能时，还应检查所选服务的数据处理条款。

## 常用检查命令

在当前系统已激活的 Python 环境下，从仓库根目录运行：

```bash
python -m app.main self-test          # 摄像头、音频、GPIO 等硬件自检
python -m app.main init-db            # 初始化数据库
python -m app.main transcribe a.wav   # 本地识别音频文件
python -m app.main wake-test a.wav    # 用音频文件测试唤醒词
python -m app.main weekly-review      # 输出健康周报 JSON
python -m app.main analytics          # 输出健康分析 JSON
```

树莓派若未激活虚拟环境，将上述命令中的 `python` 换成 `./.venv/bin/python`；Windows 则可用 `.venv\Scripts\python`。服务没有正常启动时，可检查 `sudo systemctl status mindful-meal` 和 `/data/logs/` 中的日志。

## 模型与许可

仓库为便于部署包含第三方模型资产；项目代码、依赖和各模型**可能适用不同的许可和使用条款**。公开上传并不代表已替所有第三方资产核实再分发、商业用途或其他授权。使用、再分发或商用前，请分别核查相关上游来源及许可。
