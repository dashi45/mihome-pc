# -*- coding: utf-8 -*-
"""打包版启动诊断：清场 → 带环境变量启动 → 监视 → CDP 查 WebView2。"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "tests", "tools"))
sys.path.insert(0, BASE)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["MIHOME_TRACE"] = "1"
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=9222"

import launcher
import psutil

# 0. 清场（杀残留 backend / exe）
subprocess.run([sys.executable, os.path.join(BASE, "tests", "_stop_instance.py")],
               cwd=BASE, timeout=30)

sh32 = ctypes.windll.shell32
sh32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                               wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
rc = sh32.ShellExecuteW(None, "open",
                        os.path.join(os.path.expanduser("~"), "Desktop", "米家.exe"),
                        None, os.path.join(os.path.expanduser("~"), "Desktop"), 1)
print("ShellExecuteW rc =", rc)

u32 = ctypes.windll.user32
dirs = launcher._our_dirs(BASE)
t0 = time.time()
last = None
hwnd = None
while time.time() - t0 < 60:
    ps_ = [p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "") == "米家.exe"]
    backend = launcher.port_open(8765, timeout=0.3)
    wins = launcher._our_windows(dirs)
    st = (len(ps_), backend, len(wins))
    if st != last:
        print("t=%5.1fs exe=%d 后端=%s 主窗口=%d" % (time.time() - t0, st[0], st[1], st[2]))
        last = st
    if wins:
        hwnd = wins[0][0]
        print(">>> 窗口出现 hwnd=%s 可见=%s" % (hwnd, bool(u32.IsWindowVisible(hwnd))))
        break
    if not ps_:
        print(">>> exe 进程死了")
        break
    time.sleep(1)

if not wins:
    # 窗口没出现：查枚举原始结果 + CDP
    print("---- 按标题枚举（不过滤归属）----")
    for h, p in launcher._enum_title_windows("米家 · 电脑版"):
        print("  hwnd=%s pid=%s 可见=%s" % (h, p, bool(u32.IsWindowVisible(h))))
    print("---- CDP ----")
    try:
        sys.path.insert(0, os.path.join(BASE, "tests"))
        import cdp
        w = cdp.connect()
        w.s.settimeout(10)
        info = cdp.eval_js(w, "location.href + ' | readyState=' + document.readyState")
        print("  页面:", info)
    except Exception as e:
        print("  CDP 不可用: %s" % e)
else:
    print("==== 窗口正常出现，再做托盘点击回归 ====")
    tray = launcher._enum_title_windows("mihome-pc-tray")
    print("托盘窗口:", "在" if tray else "缺失")
    if tray:
        u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u32.PostMessageW(tray[0][0], 0x8001, 1, 0x0202)
        time.sleep(2.0)
        u32.ShowWindow(hwnd, 6)  # SW_MINIMIZE 再藏回去，交给用户时是托盘态
        time.sleep(3.0)
        print("回归后: 可见=%s" % bool(u32.IsWindowVisible(hwnd)))
