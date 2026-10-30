@echo off
rem ============================================================
rem  调试启动 —— 与「启动米家.bat」相同，额外打开 CDP 端口
rem
rem  用途：让 tests\cdp.py 能连进页面量布局、切主题、截图。
rem  平时不需要它，双击「启动米家.bat」即可。
rem
rem  注意：调试端口 9222 只监听 127.0.0.1，但任何本机进程都能连上
rem  操控这个页面，所以别把它做成日常启动方式。
rem ============================================================
set "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222"
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "launcher.py"
