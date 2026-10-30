# -*- coding: utf-8 -*-
"""桌面 exe 端到端验收：双击图标 → 后端起来 → 界面能开 → **没有黑框**。

「黑框」就是那个刺眼的黑色控制台窗口。判定方法不是看有没有 python.exe
（打包版的解释器本来就叫这个），而是枚举桌面上**可见的**控制台类窗口
（class = ConsoleWindowClass），拿启动前后的差集 —— 新冒出来的那个就是黑框。

顺便核对整条进程链：
    米家.exe (打包的窗口外壳)
      └─ 后端解释器（app.py）
           └─ charger bridge（充电桩桥接）
    后端必须是 **pythonw.exe**（无窗口版），出现 python.exe 就说明有黑框。
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DESKTOP_EXE = os.path.join(os.path.expanduser("~"), "Desktop", "米家.exe")
sys.path.insert(0, BASE)

u32 = ctypes.windll.user32
u32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                                wintypes.LPARAM),
                             wintypes.LPARAM]
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]


def out(*a):
    print(*a, flush=True)


def visible_consoles():
    """桌面上当前可见的控制台类窗口（黑框候选）。"""
    found = []
    ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _l):
        if not u32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, buf, 256)
        if buf.value == "ConsoleWindowClass":
            found.append(hwnd)
        return True

    c = ENUMPROC(cb)
    u32.EnumWindows(c, 0)
    return set(found)


def find_title_window(title):
    """按标题找**可见**的顶层窗口，返回 (hwnd, pid)。找不到返回 (None, None)。

    自己实现而不用 launcher._our_windows()：那段代码判断"是不是自己人"
    依赖运行时上下文（打包版才含桌面目录），在源码环境里识别不到桌面 exe。
    """
    hit = [None, None]
    ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextLengthW.restype = ctypes.c_int
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def cb(hwnd, _l):
        try:
            n = u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == title:
                    pid = wintypes.DWORD(0)
                    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    hit[0] = hwnd
                    hit[1] = pid.value
                    return False
        except Exception:
            pass
        return True

    c = ENUMPROC(cb)
    u32.EnumWindows(c, 0)
    return hit[0], hit[1]


def kill_mihome():
    """收尾：关掉本程序的所有进程（桌面 exe + 后端 + 桥接）。"""
    try:
        import psutil
        base = "b:\\下载\\ai生成文件处\\2026-10-23-15-49-15\\mihome-pc"
        for pr in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = pr.info["cmdline"] or []
                cl = " ".join(cmd).lower()
                if not cmd:
                    continue
                if ("米家.exe" in cl or "app.py" in cl or "launcher.py" in cl
                        or "charger" in cl):
                    pr.kill()
            except Exception:
                pass
    except Exception:
        pass


def port_open(p):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", p), 0.4):
            return True
    except OSError:
        return False


def proc_tree():
    """本程序相关进程的 (pid, name, cmdline) 列表。"""
    out_list = []
    try:
        import psutil
        for pr in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
            try:
                cmd = pr.info["cmdline"] or []
                cl = " ".join(cmd)
                if ("米家.exe" in cl or "app.py" in cl or "launcher.py" in cl
                        or "charger" in cl.lower()):
                    out_list.append((pr.info["pid"], pr.info["name"], cl[:150],
                                     pr.info["ppid"]))
            except Exception:
                pass
    except Exception:
        pass
    return out_list


out("=== 0. 前置检查 ===")
out("桌面 exe 存在:", os.path.isfile(DESKTOP_EXE))
if not os.path.isfile(DESKTOP_EXE):
    sys.exit(2)
out("后端已占用 8765:", port_open(8765), " ← 应为 False")

before = visible_consoles()
out("启动前可见控制台窗口数:", len(before))

out()
out("=== 1. 双击桌面图标 ===")
# 用 explorer 打开 = 用户在资源管理器里双击（比 cmd /c start 更贴近真实场景）
EXPLORER = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "explorer.exe")
subprocess.Popen([EXPLORER, DESKTOP_EXE], creationflags=0x08000000)

ok = False
for _ in range(80):
    time.sleep(0.5)
    if port_open(8765):
        ok = True
        break
out("后端起来:", port_open(8765))
if not ok:
    out("❌ 后端没起来")
    sys.exit(3)

time.sleep(4)   # 给界面窗口一点时间

out()
out("=== 2. 黑框检测 ===")
after = visible_consoles()
new_consoles = after - before
out("启动后可见控制台窗口数:", len(after))
out("**新冒出来的控制台窗口（黑框）:", len(new_consoles), "**")
if new_consoles:
    for h in new_consoles:
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(h, buf, 512)
        out("   hwnd=%s 标题=%r" % (h, buf.value))
out("判定:", "❌ 有黑框" if new_consoles else "✅ 没有黑框")

out()
out("=== 3. 进程链核对（后端必须是 pythonw.exe）===")
tree = proc_tree()
for pid, name, cl, ppid in tree:
    out("  pid=%-6s ppid=%-6s %-14s %s" % (pid, ppid, name, cl))
bad = [t for t in tree if t[1].lower() == "python.exe" and "app.py" in t[2]]
out("用 python.exe（有黑框）跑后端的:", len(bad), " ← 必须是 0")
bad2 = [t for t in tree if t[1].lower() == "python.exe" and "app.py" not in t[2]
        and "米家.exe" not in t[2] and "launcher.py" not in t[2]]
out("其它 python.exe 进程:", len(bad2))

out()
out("=== 4. 界面能访问 ===")
try:
    import urllib.request
    body = urllib.request.urlopen("http://127.0.0.1:8765/", timeout=5).read()
    out("首页返回字节数:", len(body), "← 应 > 1000")
    page_ok = len(body) > 1000
except Exception as e:
    out("首页访问失败:", e)
    page_ok = False

out()
out("=== 5. 界面窗口必须真的出现（可见）===")
hwnd, wpid = find_title_window("米家 · 电脑版")
out("找到『米家 · 电脑版』窗口:", bool(hwnd), " 所属 pid =", wpid)
out("窗口可见:", bool(hwnd and u32.IsWindowVisible(hwnd)))
win_ok = bool(hwnd) and bool(u32.IsWindowVisible(hwnd))
out("判定:", "✅ 界面窗口正常" if win_ok else "❌ 界面窗口没出来")

out()
out("=== 6. 摄像头（重点：验证打包版也注入了凭据）===")
# 这一步专治一类回归：凭据从 go2rtc.yaml 挪到 cam_secret.json 后，
# 只有**新版 launcher** 会去读取并注入环境变量。如果桌面 exe 没重打包，
# 它还是旧 launcher、不注入 -> go2rtc 拿到的是字面量 ${MI_XIAOMI_TOKEN}
# -> 认证失败 -> 画面永远出不来。跑源码版测不出来，必须测 exe 版。
import json as _json
import urllib.error as _urlerr
import urllib.request as _urlreq

def _cam_did():
    """摄像头 did 从 cam.json 读 —— 测试也不该假设某台具体设备。"""
    try:
        with open(os.path.join(BASE, "cam.json"), encoding="utf-8") as f:
            cfg = json.load(f) or {}
        did = str(cfg.get("default") or "").strip()
        if did:
            return did
        return str(next(iter(cfg.get("streams") or {})))
    except Exception:
        return ""


CAM_DID = _cam_did()
cam_ok = False
cam_detail = ""
t0 = time.time()
while time.time() - t0 < 80:
    try:
        _urlreq.urlopen("http://127.0.0.1:8765/api/cam-diag", timeout=5).read()
        break
    except _urlerr.HTTPError as e:
        if e.code != 401:
            break
        time.sleep(1.0)
    except Exception:
        time.sleep(1.0)

try:
    t1 = time.time()
    r = _urlreq.urlopen("http://127.0.0.1:8765/api/cam/" + CAM_DID, timeout=90)
    d = _json.loads(r.read())
    cam_ok = bool(d.get("ok"))
    cam_detail = "耗时 %.1fs  segments=%s" % (
        time.time() - t1, (d.get("warm") or {}).get("segments"))
    if not cam_ok:
        cam_detail = "ok=false " + _json.dumps(d, ensure_ascii=False)[:300]
except _urlerr.HTTPError as e:
    cam_detail = "HTTP %s %s" % (e.code, e.read().decode("utf-8", "replace")[:300])
except Exception as e:
    cam_detail = repr(e)
out("拉流:", cam_detail)
out("判定:", "✅ 摄像头可用" if cam_ok else "❌ 摄像头不可用（若是打包版，先确认 exe 是新的）")

out()
out("=== 7. 收尾：关掉测试拉起来的实例 ===")
kill_mihome()
time.sleep(2)
out("后端还占着端口吗:", port_open(8765), " ← 必须是 False")

out()
ok_final = (not new_consoles) and (not bad) and page_ok and win_ok and cam_ok
out("✅ 桌面端验收通过" if ok_final else "❌ 桌面端验收不通过")
sys.exit(0 if ok_final else 1)
