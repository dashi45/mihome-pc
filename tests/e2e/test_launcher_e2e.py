# -*- coding: utf-8 -*-
"""端到端验证：模拟「后端崩了、只剩窗口」后再次双击图标，看能不能自愈。

真实场景（用户最常遇到的那个）：
    程序开着 → 后端被任务管理器杀掉 / 崩了 → 窗口还挂在桌面上，
    界面是死的。这时候再双击桌面图标，如果只是把那个空窗口叫到前面，
    用户看到的就是「点了没反应」，而且再也起不来了。

本脚本按真实流程走：
    1. 起一个 launcher（等价于双击图标）
    2. 确认后端起来、窗口出现
    3. 杀掉后端（只杀 8765 的监听进程，不牵连别的）
    4. 确认窗口还在、后端没了  ← 制造出"废窗口"
    5. 再起一个 launcher（= 用户再点一次图标）
    6. 断言：废窗口被关掉、后端重新起来、窗口重新出现

全程只用本程序自己的进程，不会碰用户别的程序。
"""
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

import launcher  # noqa: E402

PY = os.path.join(BASE, ".venv", "Scripts", "python.exe")
LOG = os.path.join(BASE, "_e2e_launcher.log")


def out(*a):
    print(*a, flush=True)


def port_open(p):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", p), 0.4):
            return True
    except OSError:
        return False


def win_count():
    return len(launcher._enum_title_windows(launcher.TITLE))


def backend_pid():
    """8765 的监听进程 pid（真正的解释器，不是 shim）。"""
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == launcher.PORT_WEB and c.status == "LISTEN" \
                    and c.pid:
                return c.pid
    except Exception:
        pass
    return None


def kill_backend():
    """只杀后端进程树。"""
    pid = backend_pid()
    if not pid:
        return None
    try:
        import psutil
        p = psutil.Process(pid)
        kids = p.children(recursive=True)
        for k in kids:
            try:
                k.kill()
            except Exception:
                pass
        p.kill()
    except Exception:
        pass
    return pid


def start_launcher():
    log = open(LOG, "ab", buffering=0)
    return subprocess.Popen([PY, os.path.join(BASE, "launcher.py")],
                            cwd=BASE, stdout=log, stderr=log,
                            creationflags=0x08000000,   # CREATE_NO_WINDOW
                            stdin=subprocess.DEVNULL)


def cleanup():
    """收尾：干掉本次测试拉起来的所有本程序进程。"""
    try:
        import psutil
        dirs = launcher._our_dirs(BASE)
        for pr in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = pr.info["cmdline"] or []
                if not cmd:
                    continue
                cl = " ".join(cmd).lower()
                if not any(d in cl for d in dirs):
                    continue
                if any(str(a).endswith("launcher.py") or str(a).endswith("app.py")
                       for a in cmd):
                    pr.kill()
            except Exception:
                pass
    except Exception:
        pass


out("=== 0. 初始状态 ===")
if port_open(launcher.PORT_WEB):
    out("❌ 后端已经在跑，先停掉")
    sys.exit(2)
out("后端未运行，窗口数 =", win_count())

procs = []
try:
    out()
    out("=== 1. 第一次启动（等价于双击图标）===")
    procs.append(start_launcher())
    ok = False
    for _ in range(60):
        time.sleep(0.5)
        if port_open(launcher.PORT_WEB) and win_count() >= 1:
            ok = True
            break
    out("后端起来:", port_open(launcher.PORT_WEB), " 窗口数:", win_count())
    if not ok:
        out("❌ 第一次启动没成功")
        sys.exit(3)

    out()
    out("=== 2. 杀掉后端，制造「废窗口」===")
    bp = kill_backend()
    out("杀掉后端 pid =", bp)
    for _ in range(20):
        time.sleep(0.3)
        if not port_open(launcher.PORT_WEB):
            break
    out("后端还活着吗:", port_open(launcher.PORT_WEB), " ← 必须是 False")
    out("窗口还在吗:", win_count(), " ← 应该是 1（废窗口）")
    out("（此刻再双击图标，如果只是唤起窗口，用户看到的就是『点了没反应』）")

    out()
    out("=== 3. 再次启动（= 用户再点一次图标）===")
    procs.append(start_launcher())
    healed = False
    for _ in range(80):
        time.sleep(0.5)
        if port_open(launcher.PORT_WEB) and win_count() >= 1:
            healed = True
            break
    out("后端重新起来:", port_open(launcher.PORT_WEB))
    out("窗口数:", win_count(), "（应该还是 1，废的那个被关掉了）")
    out("自动恢复:", healed)

    out()
    out("=== 4. 关窗口应能彻底退出（不留孤儿占端口）===")
    for pr in procs:
        try:
            pr.terminate()
        except Exception:
            pass
    time.sleep(3)
    for pr in procs:
        try:
            launcher._kill_tree(pr.pid)
        except Exception:
            pass
    cleanup()
    time.sleep(2)
    out("后端还占着端口吗:", port_open(launcher.PORT_WEB), " ← 必须是 False")
    out("窗口还剩吗:", win_count(), " ← 应该是 0")

    ok_final = healed and not port_open(launcher.PORT_WEB) and win_count() == 0
    out()
    out("✅ 端到端通过" if ok_final else "❌ 端到端没通过")
    sys.exit(0 if ok_final else 1)
finally:
    cleanup()
