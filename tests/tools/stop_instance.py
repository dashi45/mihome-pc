# -*- coding: utf-8 -*-
"""停掉正在运行的米家实例（整棵树）。只认关键文件 basename + 路径在本程序目录。"""
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
import psutil

OWN = ("launcher.py", "app.py", "米家.exe")
OWN_L = tuple(n.lower() for n in OWN)


def under_base(path):
    if not path:
        return False
    try:
        return os.path.commonpath([os.path.normcase(os.path.abspath(path)),
                                   os.path.normcase(BASE)]) == os.path.normcase(BASE)
    except Exception:
        return False


killed = []
for p in psutil.process_iter(["pid", "name", "cmdline"]):
    try:
        if p.pid == os.getpid():
            continue
        cmd = p.info["cmdline"] or []
        hit = False
        for raw in cmd:
            if not raw:
                continue
            b = os.path.basename(str(raw)).lower()
            if b in OWN_L and under_base(str(raw) if os.path.isabs(str(raw)) else BASE):
                hit = True
                break
        if not hit:
            continue
        for c in p.children(recursive=True):
            try:
                c.kill()
                killed.append(c.pid)
            except Exception:
                pass
        try:
            p.kill()
            killed.append(p.pid)
        except Exception:
            pass
    except Exception:
        pass

time.sleep(2)
import socket
try:
    socket.create_connection(("127.0.0.1", 8765), 0.5)
    print("8765 仍被占用!")
    sys.exit(1)
except OSError:
    pass
print("killed pids:", killed)
print("8765 已释放")
