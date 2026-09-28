# 饭崽（Fanzai）

面向老年人的 AI 正念饮食陪伴项目，可在 Windows 开发环境和树莓派运行。项目通过摄像头识别进食状态，以语音和屏幕提供陪伴，并记录饮食与健康数据。

## 功能

- 咀嚼、手口送食、情绪和食物识别
- 中文语音唤醒与离线语音识别
- AI 对话、语音播报、饮食记录和健康报告
- 树莓派摄像头、GPIO 与圆屏支持；无硬件时可使用模拟模式

## Windows 快速开始

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python tests/selfcheck.py
.venv\Scripts\python -m app.main serve --port 8765
```

浏览器访问 <http://127.0.0.1:8765>。也可运行 `run.bat` 自动准备环境并启动。

## 树莓派部署

在树莓派上克隆仓库并运行部署脚本：

```bash
git clone https://github.com/cgcycloud/Fanzai.git /opt/mindful_meal
cd /opt/mindful_meal
sudo ./deploy/setup_pi.sh
```

启动服务：

```bash
sudo systemctl start mindful-meal
sudo systemctl status mindful-meal
```

部署脚本会安装系统与 Python 依赖、从仓库复制模型到 `/data/models` 并配置服务。树莓派硬件支持及配置见 [`deploy/`](deploy/) 和 [`docs/`](docs/)。

## 模型与数据

推理模型放在 [`models/`](models/) 并随仓库提供。`.venv` 不纳入版本控制；在目标设备按依赖清单重新创建即可。运行数据、数据库、日志和 API 配置保存在 `data_local/`（树莓派为 `/data/`），不会提交到仓库。

AI 云端对话、视觉或语音服务需自行配置相应 API Key；不配置时部分云端能力不可用。请勿将密钥提交到 GitHub。

## 常用命令

```bash
python -m app.main self-test       # 硬件自检
python -m app.main init-db         # 初始化数据库
python -m app.main transcribe a.wav
python -m app.main weekly-review
```

## 许可与模型来源

项目代码及第三方组件、模型分别遵循各自许可。使用或再分发前请查阅对应上游项目的许可与使用条款；模型来源说明见项目文档及模型目录。
