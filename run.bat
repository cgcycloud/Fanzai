@echo off
rem 控制台与 Python 统一成 UTF-8（否则日志中文全是乱码）

rem 不要写成 chcp 936 + Python 写 UTF-8 两者会不一致，日志全花

%SystemRoot%\System32\chcp.com 65001 >nul 2>nul

set PYTHONUTF8=1

set PYTHONIOENCODING=utf-8
title 正念饭崽 mindful_meal
cd /d "%~dp0"

set "PORT=8765"
set "PYTHON=.venv\Scripts\python.exe"

rem ========== 1) 找 Python（首次运行需要） ==========
rem 注意：本项目依赖已锁版本（mediapipe 0.10.21 只支持 Python 3.10~3.12），
rem       Python 3.13+ 装不上，所以优先找 3.12/3.11/3.10。
set "PYEXE="
py -3.12 --version >nul 2>nul && set "PYEXE=py -3.12"
if not defined PYEXE (
    py -3.11 --version >nul 2>nul && set "PYEXE=py -3.11"
)
if not defined PYEXE (
    py -3.10 --version >nul 2>nul && set "PYEXE=py -3.10"
)
if not defined PYEXE (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
    ) do (
        if not defined PYEXE if exist "%%~P" set "PYEXE=%%~P"
    )
)
rem 兜底：py -3 或任意能找到的 Python（下面会做版本闸门检查）
if not defined PYEXE (
    py -3 --version >nul 2>nul && set "PYEXE=py -3"
)
if not defined PYEXE (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
    ) do (
        if not defined PYEXE if exist "%%~P" set "PYEXE=%%~P"
    )
)
if not defined PYEXE (
    echo [错误] 未找到 Python 3.10~3.12，请先安装 Python 3.12 并勾选 "Add Python to PATH"
    start "" "https://www.python.org/downloads/release/python-3128/"
    pause
    exit /b 1
)

rem 版本闸门：Python 3.13+ 装不了锁定版本的 mediapipe，提前拦下说清楚
%PYEXE% -c "import sys; sys.exit(0 if sys.version_info[:2] < (3,13) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [提示] 检测到 Python 3.13 或更高版本。
    echo        本项目的视觉依赖（mediapipe 0.10.21）暂不支持 3.13+，
    echo        即将打开 Python 3.12 的下载页面，请安装它（安装时勾选 "Add Python to PATH"）
    echo        然后重新双击本脚本即可。
    start "" "https://www.python.org/downloads/release/python-3128/"
    pause
    exit /b 1
)

if exist "%PYTHON%" goto :deps

echo [初始化] 首次运行，正在创建虚拟环境...
%PYEXE% -m venv .venv
if errorlevel 1 (
    echo [错误] 创建虚拟环境失败
    pause
    exit /b 1
)

:deps
rem ========== 2) 检查依赖（首次运行需要） ==========
%PYTHON% -c "import fastapi, sherpa_onnx, cv2, pypinyin" >nul 2>nul
if errorlevel 1 (
    echo [初始化] 正在安装依赖（首次约 3~10 分钟，之后自动跳过）...
    %PYTHON% -m pip install -q --upgrade pip
    %PYTHON% -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试
        pause
        exit /b 1
    )
)

rem ========== 3) 提示可选项（不影响启动） ==========
rem 发声只走云端 Qwen3-TTS-Flash，本项目不再内置任何本地 TTS 模型：
rem 没有云端 TTS API Key 也没有网络时，只会有文字与提示音，并且不影响启动。
%PYTHON% -c "import sys; sys.path.insert(0, '.'); from app.voice import qwen_tts_engine as _q; sys.exit(0 if _q.configured() else 1)" >nul 2>nul
if errorlevel 1 (
    echo [提示] 尚未配置 TTS API Key，语音播报不可用（文字正常）。
    echo        请在项目目录下 data_local\ai_config.json 里填 tts_api_key（阿里云百炼 DashScope Key），字段说明见 data_local\AI_CONFIG.md
    echo        ^(点开 / 模型 / 设置 / 角色看 README，里面有一步一步的说明^)
)
if not exist "models\sherpa-onnx-paraformer-zh-small\model.int8.onnx" (
    echo [提示] 未找到语音识别模型，识别将不可用。
    echo        修复方法: %PYTHON% -m tools.download_models
)
if not exist "models\sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01\tokens.txt" (
    echo [提示] 未找到语音唤醒模型，将无法语音唤醒。
    echo        修复方法: %PYTHON% tools\download_sherpa_models.py
)
rem 发声走云端服务，本项目不再内置 TTS 模型
rem 早期的 Kokoro / Matcha / VITS / Piper 已全部移除
rem ========== 4) 启动服务 + 延时打开浏览器 ==========
echo.
echo ==============================================
echo   正念饭崽 mindful_meal
echo   地址: http://127.0.0.1:%PORT%/
echo   关闭本窗口即停止服务
echo ==============================================
echo.

rem 服务启动约 5 秒（要加载识别/唤醒模型，冷启动较慢，全自动检测）
rem 端口占用预先提示（最常见的就是上一次的服务还没关干净导致启动失败原因）
set "PORTBUSY="
for /f "tokens=*" %%L in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do set "PORTBUSY=1"
if defined PORTBUSY (
    echo [警告] 端口 %PORT% 已被占用，可能是上次的服务还在运行中。
    echo         请先关闭那个窗口（或结束对应的 python.exe）。
    echo         也可以改本文件开头 set "PORT=8765" 换端口。
    echo.
)

rem 确认服务就绪后再打开浏览器（轮询 /api/health，最长 120 秒）。
rem 固定延时不行，因为冷启动要加载 ASR/KWS 模型，时间不可控。
rem 提前打开浏览器会导致页面卡在"正在连接设备屏…"。
start "open-browser" /min powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\open_when_ready.ps1" -Port %PORT%
%PYTHON% -m app.main serve --port %PORT%

echo.
echo [服务已停止] 服务进程关闭，可能原因是端口 %PORT% 被占用。
echo              请修改本文件开头的 set "PORT=8765" 换一个端口。
pause
