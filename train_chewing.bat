@echo off
chcp 936 >nul 2>nul
title mindful_meal - chewing calibration
cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] virtualenv not found: %PYTHON%
    echo         Please run run.bat once to create it, then try again.
    echo.
    pause
    exit /b 1
)

rem The camera can only be used by one program at a time.
"%SystemRoot%\System32\netstat.exe" -ano | "%SystemRoot%\System32\findstr.exe" /R /C:":8765 .*LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [WARN] Port 8765 is still in use ^(run.bat is probably running^).
    echo        Close that window first, otherwise the camera cannot be opened.
    echo.
    pause
    exit /b 1
)

rem Default action is calibration; --detect / --demo / --help are passed through.
"%PYTHON%" tools\calibrate_chewing.py %*

echo.
echo [DONE] Model saved to models\chewing_model.json - restart run.bat to apply.
pause
