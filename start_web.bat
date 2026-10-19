@echo off
cd /d "%~dp0"

rem 1) make sure the ComfyUI engine is up (silent, no extra browser tab)
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\ComfyUI\comfyui.ps1" -NoBrowser

rem 2) open this UI a few seconds after the local server starts
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8189"

rem 3) run the backend in this window (Ctrl+C or close window to stop it)
python server.py
