@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================================
echo   ComfyWeb Studio · 本地 AI 创作台启动中...
echo ========================================================

rem 1) 智能探测 ComfyUI 根目录路径（优先探测平级、子级或环境变量）
set "COMFY_PATH="
if exist "%~dp0..\ComfyUI" set "COMFY_PATH=%~dp0..\ComfyUI"
if not defined COMFY_PATH if exist "%~dp0ComfyUI" set "COMFY_PATH=%~dp0ComfyUI"
if not defined COMFY_PATH if defined COMFY_DIR if exist "%COMFY_DIR%" set "COMFY_PATH=%COMFY_DIR%"
if not defined COMFY_PATH if exist "E:\ComfyUI" set "COMFY_PATH=E:\ComfyUI"

rem 2) 启动 ComfyUI 推理引擎（如果尚未运行）
if defined COMFY_PATH (
    echo [INFO] 已定位 ComfyUI 目录: %COMFY_PATH%
    if exist "%COMFY_PATH%\comfyui.ps1" (
        powershell -NoProfile -ExecutionPolicy Bypass -File "%COMFY_PATH%\comfyui.ps1" -NoBrowser
    ) else if exist "%COMFY_PATH%\run_nvidia_gpu.bat" (
        start "" /b cmd /c "cd /d \"%COMFY_PATH%\" && run_nvidia_gpu.bat"
    ) else if exist "%COMFY_PATH%\main.py" (
        start "" /b cmd /c "cd /d \"%COMFY_PATH%\" && python main.py --windows-standalone-build"
    )
) else (
    echo [WARN] 未能自动定位 ComfyUI 本地目录，假定 ComfyUI 已在 http://127.0.0.1:8188 独立运行。
)

rem 3) 延迟 3 秒自动在默认浏览器中打开控制台
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8189"

rem 4) 启动 ComfyWeb 本地轻量服务调度器
echo [INFO] 正在启动 ComfyWeb 后端调度服务...
python server.py

