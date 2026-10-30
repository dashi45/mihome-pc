# -*- coding: utf-8 -*-
"""测摄像头首屏耗时：冷启动 vs 短时间内再次打开。

验证两处改动：
  · camhls.MIN_SEGMENTS 2 -> 1：不用白等第 2 个分片
  · camhls.IDLE_STOP 45 -> 120：中途退出再进来能复用已经握好的 P2P 通道
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DID = os.environ.get("CAM_DID", "1234567890")
EXPLORER = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "explorer.exe")


def out(*a):
    print(*a, flush=True)


def port_open(p):
    try:
        with socket.create_connection(("127.0.0.1", p), 0.4):
            return True
    except OSError:
        return False


def kill_all():
    """清场。必须连 launcher.py 本身一起清 —— 只杀设备和端口的话，
    残留的 launcher 会占着互斥量，新实例被判成"已在运行"直接退出，
    表现就是"启动后端口时有时无"。但只能用 pythonw.exe 匹配：
    venv 的 python.exe 是 shim，和真解释器成对出现、cmdline 一模一样，
    用 python.exe 匹配会把正在跑的测试脚本自己干掉（踩过）。"""
    import psutil
    for pr in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            nm = (pr.info["name"] or "").lower()
            exe = os.path.basename(pr.info["exe"] or "")
            cl = " ".join(pr.info["cmdline"] or [])
            hit = (nm in ("go2rtc-tk.exe", "ffmpeg.exe")
                   or exe == "米家.exe"
                   or (nm == "pythonw.exe" and "launcher.py" in cl))
            if hit:
                pr.kill()
        except Exception:
            pass
    for p in (8765, 8554):
        for c in psutil.net_connections(kind="tcp"):
            try:
                if c.laddr and c.laddr.port == p and c.pid:
                    psutil.Process(c.pid).kill()
            except Exception:
                pass


def wait_ready(limit=90):
    """等到米家登录完成 **且** 两个端口都稳定（连续两次都成功才算）。

    为什么要连续两次：刚启动那一下端口时通时断（后端在拉起 go2rtc、
    互斥量在交接），单次判断会误判"已就绪"，随后接口又变 401。
    另外必须用**同样受登录门禁**的接口判断：/api/devices 在未登录时
    也返回 200，拿它当就绪条件会直接跳过一个 401。
    """
    t0 = time.time()
    hits = 0
    while time.time() - t0 < limit:
        try:
            if port_open(8765) and port_open(8554):
                urllib.request.urlopen(
                    "http://127.0.0.1:8765/api/cam-diag", timeout=5).read()
                hits += 1
                if hits >= 2:
                    return True
            else:
                hits = 0
        except urllib.error.HTTPError as e:
            if e.code != 401:
                hits += 1
                if hits >= 2:
                    return True
        except Exception:
            hits = 0
        time.sleep(0.5)
    return False


def open_cam():
    t0 = time.time()
    try:
        r = urllib.request.urlopen(
            "http://127.0.0.1:8765/api/cam/" + DID, timeout=90)
        d = json.loads(r.read())
        return time.time() - t0, d
    except urllib.error.HTTPError as e:
        return time.time() - t0, {"ok": False, "http": e.code,
                                  "body": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return time.time() - t0, {"ok": False, "err": repr(e)}


out("=== 1. 清场并冷启动（源码版，含凭据注入）===")
kill_all()
time.sleep(2)
subprocess.Popen([EXPLORER, os.path.join(BASE, "启动米家.bat")])
if not wait_ready():
    out("❌ 后端没就绪")
    sys.exit(1)
out("   后端就绪，8765=%s 8554=%s" % (port_open(8765), port_open(8554)))

out()
out("=== 2. 冷启动首次打开（会话不存在，要走 P2P 握手）===")
t1, d1 = open_cam()
out("   耗时 %.1fs  ok=%s  segments=%s" % (
    t1, d1.get("ok"), (d1.get("warm") or {}).get("segments")))
if not d1.get("ok"):
    out("   ❌", json.dumps(d1, ensure_ascii=False)[:400])
    sys.exit(2)

out()
out("=== 3. 等 8 秒不取分片（模拟用户切走又切回来）===")
time.sleep(8)

out()
out("=== 4. 再次打开（通道应该还活着，IDLE_STOP=120）===")
t2, d2 = open_cam()
out("   耗时 %.1fs  ok=%s  segments=%s" % (
    t2, d2.get("ok"), (d2.get("warm") or {}).get("segments")))
if not d2.get("ok"):
    out("   ❌", json.dumps(d2, ensure_ascii=False)[:400])
    sys.exit(3)

out()
out("=== 结论 ===")
out("  冷启动首屏 : %.1fs  （改动前实测 8.1~9.3s）" % t1)
out("  复开首屏   : %.1fs  （改动前同样要重走握手，约 8s）" % t2)
ok = d1.get("ok") and d2.get("ok") and t2 < t1
out("  %s" % ("✅ 复开明显复用通道" if ok else "⚠ 复开没有明显变快，需检查会话是否被回收"))
sys.exit(0 if ok else 4)
