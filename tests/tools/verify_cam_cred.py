# -*- coding: utf-8 -*-
"""验证「凭据外置后摄像头仍可用」：清场 -> 用源码版 launcher 启动 -> 实测拉流。

不复用桌面 exe：exe 里是旧 launcher，不包含凭据注入逻辑。
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
sys.path.insert(0, BASE)

DID = os.environ.get("CAM_DID", "1234567890")   # 探测目标摄像头，环境变量可覆盖

DETACHED = 0x00000008
NO_WINDOW = 0x08000000


def out(*a):
    print(*a, flush=True)


def port_open(p, host="127.0.0.1"):
    try:
        with socket.create_connection((host, p), 0.4):
            return True
    except OSError:
        return False


def kill_all():
    import psutil
    killed = []
    for pr in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (pr.info["name"] or "").lower()
            exe = pr.info["exe"] or ""
            if name in ("go2rtc-tk.exe", "ffmpeg.exe") or \
                    "米家.exe" in os.path.basename(exe):
                pr.kill()
                killed.append((pr.info["pid"], name))
        except Exception:
            pass
    for p in (8765, 8554, 8000):
        for c in psutil.net_connections(kind="tcp"):
            try:
                if c.laddr and c.laddr.port == p and c.pid:
                    psutil.Process(c.pid).kill()
                    killed.append((c.pid, "port%d" % p))
            except Exception:
                pass
    return killed


out("=== 1. 清场 ===")
for pid, nm in kill_all():
    out("   杀掉", pid, nm)
time.sleep(2)
out("   8765:", port_open(8765), " 8554:", port_open(8554))

out()
out("=== 2. 用源码版 launcher 启动（含凭据注入）===")
pyw = os.path.join(BASE, ".venv", "Scripts", "pythonw.exe")
log = open(os.path.join(BASE, "_verify_launcher.log"), "wb", buffering=0)
subprocess.Popen([pyw, os.path.join(BASE, "launcher.py")], cwd=BASE,
                 stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                 creationflags=DETACHED | NO_WINDOW, close_fds=True)

ok = False
for i in range(90):
    time.sleep(0.5)
    if port_open(8765):
        ok = True
        out("   后端就绪，耗时 %.1fs" % ((i + 1) * 0.5))
        break
if not ok:
    out("   ❌ 后端没起来，看 _verify_launcher.log 与 launcher.log")
    sys.exit(1)

out("   RTSP 8554:", port_open(8554), "（go2rtc 起来了才能拉流）")
if not port_open(8554):
    out("   ❌ go2rtc 没起来 —— 很可能是占位符没被替换，检查注入")
    sys.exit(2)

out()
out("=== 3. 实测拉流（凭据外置后）===")
# 后端端口起来 ≠ 米家登录完成：登录是异步的，未就绪时接口统一返回 401。
# 所以要轮询到非 401，而不是拿"端口通了"当"能用了"。
d = None
t0 = time.time()
last = ""
while time.time() - t0 < 75:
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8765/api/cam/" + DID,
                                   timeout=60)
        d = json.loads(r.read())
        break
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        last = "HTTP %s %s" % (e.code, body[:200])
        if e.code == 401:
            time.sleep(1.0)          # 还在登录中，继续等
            continue
        out("   ❌ 耗时 %.1fs  %s" % (time.time() - t0, last))
        sys.exit(4)
    except Exception as e:
        last = repr(e)
        time.sleep(1.0)

if d is None:
    out("   ❌ 等待超时，最后状态：%s" % last)
    sys.exit(4)

out("   耗时 %.1fs  ok=%s" % (time.time() - t0, d.get("ok")))
out("   segments =", (d.get("warm") or {}).get("segments"))
if not d.get("ok"):
    out("   ❌ 拉流失败：", json.dumps(d, ensure_ascii=False)[:600])
    sys.exit(3)

out()
out("=== 4. 分片有效性 ===")
import re
m = urllib.request.urlopen("http://127.0.0.1:8765/camhls/%s/index.m3u8" % DID,
                           timeout=10).read().decode("utf-8", "replace")
segs = [l.strip() for l in m.splitlines() if l.strip().endswith(".ts")]
bad = 0
for s in segs:
    body = urllib.request.urlopen("http://127.0.0.1:8765/camhls/%s/" % DID + s,
                                  timeout=10).read()
    flag = "← 空片!" if len(body) < 5000 else "✓"
    if len(body) < 5000:
        bad += 1
    out("   %s  %d 字节 %s" % (s, len(body), flag))

out()
out("=== 5. 验证配置里确实没有明文凭据 ===")
cfg = open(os.path.join(BASE, "go2rtc", "go2rtc.yaml"), encoding="utf-8").read()
leak = re.findall(r"V1:[A-Za-z0-9+/=]{40,}|192\.168\.\d+\.\d+", cfg)
out("   配置文件明文凭据:", leak if leak else "无 ✓")

final = (bad == 0) and (not leak) and len(segs) >= 1
out()
out("✅ 凭据外置成功，摄像头正常" if final else "❌ 有问题")
sys.exit(0 if final else 6)
