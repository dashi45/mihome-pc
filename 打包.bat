@echo off
rem ============================================================
rem   打包 → 正式版   一键打包脚本
rem
rem   用法：双击本文件，或在终端里执行 打包.bat
rem
rem   产物：dist\米家\米家.exe （onedir，要把整个 dist\米家 文件夹一起用）
rem
rem   注意：
rem     1. 打包前会先关掉正在跑的 米家.exe，否则文件被占用会写不进去
rem     2. 只打包 launcher.py，后端 app.py 和 static/ 不进包 —— 它们留在
rem        源码目录，launcher 运行时用 find_dir() 去找，所以改了前端 js
rem        不用重新打包
rem     3. 分发/备份时**拷整个 dist\米家 文件夹**。只拷 米家.exe 会缺
rem        _internal\ 里的 Python 和 WebView2 运行时，双击只会闪一下就没
rem ============================================================
setlocal
cd /d "%~dp0"
title 米家 - 打包

echo.
echo === 1/5 停止正在运行的实例 ===
taskkill /F /IM 米家.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8765"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8554"') do taskkill /F /PID %%a >nul 2>&1
timeout /t 1 /nobreak >nul

echo === 2/5 检查环境 ===
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 没有 .venv，先双击 启动米家.bat 初始化
    pause
    exit /b 1
)

echo === 3/5 打包（约 1-2 分钟）===
rem 以 米家.spec 为唯一事实来源：--onefile/--onedir、hidden-import、
rem excludes 这些都写在 spec 里，命令行只给路径，避免两边打架。
rem 注意 --clean 会把 dist\米家 整个删掉重建，所以不存在"新 exe 配旧 DLL"。
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --distpath dist --workpath build 米家.spec
if errorlevel 1 (
    echo.
    echo [错误] 打包失败，看上面的报错
    pause
    exit /b 1
)

echo === 4/5 校验产物 ===
rem onedir 少一个文件就起不来，所以这里逐项卡住，别等用户双击了才发现。
if not exist "dist\米家\米家.exe" (
    echo [错误] 没找到 dist\米家\米家.exe
    pause
    exit /b 1
)
if not exist "dist\米家\_internal\base_library.zip" (
    echo [错误] _internal 不完整：缺 base_library.zip
    pause
    exit /b 1
)
if not exist "dist\米家\_internal\Python.Runtime.dll" (
    echo [错误] _internal 不完整：缺 Python.Runtime.dll（pythonnet）
    echo        launcher 里 import clr 会失败，窗口根本起不来
    pause
    exit /b 1
)
echo 产物：%CD%\dist\米家\米家.exe
for %%f in ("dist\米家\米家.exe") do echo 大小：%%~zf 字节

echo === 5/5 更新快捷方式 ===
rem 不再把 exe 拷到桌面：onedir 是"一个文件夹"，单拷 exe 就是个必然崩的包。
rem 桌面放快捷方式，指向 dist\米家\米家.exe。
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\make_shortcut.ps1" -Target "%CD%\dist\米家\米家.exe" -LnkPath "%USERPROFILE%\Desktop\米家.lnk" -WorkDir "%CD%\dist\米家"
if errorlevel 1 echo [提示] 桌面快捷方式没建成功，可以手动拖一个

echo.
echo 完成。双击桌面/开始菜单的「米家」即可运行。
echo 升级时请整目录替换 dist\米家，不要只覆盖 exe。
pause
