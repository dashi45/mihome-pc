@echo off
setlocal
cd /d "%~dp0"
title 米家 · 电脑版

rem 首次运行先装依赖
if not exist ".venv\Scripts\pythonw.exe" (
  echo 首次运行，正在准备运行环境……
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

rem 打开桌面窗口（后台服务由 launcher 自己负责启动）
start "" ".venv\Scripts\pythonw.exe" "%~dp0launcher.py"
exit /b 0
