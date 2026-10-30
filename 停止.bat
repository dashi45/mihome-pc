@echo off
title 米家 · 停止
taskkill /F /IM 米家.exe >nul 2>&1
taskkill /F /IM go2rtc-tk.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8765"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8554"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8000"') do taskkill /F /PID %%a >nul 2>&1
echo 米家（窗口、摄像头转流、充电器桥接）已停止。
ping -n 2 127.0.0.1 >nul 2>&1
