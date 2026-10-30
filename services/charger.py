# -*- coding: utf-8 -*-
"""CUKTECH 充电器蓝牙桥：按需拉起、健康检查、功率汇总。

桥接是个独立进程（charger/ 目录），平时不跑 —— 只有真正要读充电器数据时
才拉起，失败后 15 秒内不重试，避免每次请求都卡在"启动蓝牙"上。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import config


# ---------- CUKTECH 充电器（蓝牙桥接，按需启动）----------
config.CHARGER_DIR = os.path.join(config.BASE, "charger")
config.CHARGER_PORT = int(os.environ.get("config.CHARGER_PORT", "8000"))
_CHARGER = {"fail_at": 0.0}          # 启动失败的时间点，短时间内不反复重试
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
config.NO_WIN = 0x08000000 | 0x00000008    # CREATE_NO_WINDOW | DETACHED_PROCESS


def _charger_alive(timeout=1.2):
    try:
        with _OPENER.open("http://127.0.0.1:%d/api/health" % config.CHARGER_PORT,
                          timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# 充电器总功率：主页面要把它并进「当前总功率」。
# 桥接没在跑就返回 None —— 不为一个数字去拉起蓝牙连接；
# 用户打开过充电器页面后桥接会自己活着，这里就持续有值。
# 10 秒缓存的理由：充电器本身 1 秒刷一次，主页 4 秒问一次没必要穿透到底层，
# 也避免每一轮 live 都去敲桥接、把它一直顶着不让 idle 退出。
_CHG_POWER = {"t": 0.0, "w": None}


def _charger_power():
    """充电器各端口功率之和（瓦）。桥接没跑 / 没连上返回 None。"""
    now = time.time()
    if now - _CHG_POWER["t"] < 10.0:
        return _CHG_POWER["w"]
    w = None
    try:
        with _OPENER.open("http://127.0.0.1:%d/api/state" % config.CHARGER_PORT,
                          timeout=0.25) as r:
            if r.status == 200:
                d = json.loads(r.read().decode("utf-8", "ignore"))
                if d.get("status") == "ok":
                    total = 0.0
                    for p in (d.get("ports") or {}).values():
                        if isinstance(p, dict):
                            try:
                                total += float(p.get("power") or 0)
                            except (TypeError, ValueError):
                                pass
                    w = round(total, 1)
    except Exception:
        w = None
    _CHG_POWER["t"], _CHG_POWER["w"] = now, w
    return w


def _ensure_charger():
    """用到才拉起蓝牙桥接服务 —— 平时不占内存、不占蓝牙连接。
    失败后 15 秒内不再重试，避免每次请求都卡一遍。"""
    if _charger_alive():
        return True
    if time.time() - _CHARGER["fail_at"] < 15:
        return False

    script = os.path.join(config.CHARGER_DIR, "bridge.py")
    if not os.path.isfile(script):
        _CHARGER["fail_at"] = time.time()
        return False

    # 必须用 pythonw.exe（无窗口版），不能继承 sys.executable。
    # 原因：CREATE_NO_WINDOW / DETACHED_PROCESS 只对**已经分配了控制台**的
    # 程序有效 —— 当父进程自己是控制台版 python.exe 时，子进程会直接继承
    # 父控制台，标志形同虚设，结果就是屏幕上弹一个黑框。
    # 用 pythonw.exe 就没有控制台可继承，从根上不存在黑框。
    py = ""
    for cand in ("pythonw.exe", "python.exe"):
        p = os.path.join(config.BASE, ".venv", "Scripts", cand)
        if os.path.isfile(p):
            py = p
            break
    if not py:
        # 找不到 .venv 里的解释器，退回当前解释器（打包版场景）
        py = sys.executable
    if not py or not os.path.isfile(py):
        _CHARGER["fail_at"] = time.time()
        return False

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_PROXY"] = "*"
    try:
        log = open(os.path.join(config.CHARGER_DIR, "charger.log"), "ab", buffering=0)
        subprocess.Popen([py, script, "--port", str(config.CHARGER_PORT)],
                         cwd=config.CHARGER_DIR, env=env,
                         stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL,
                         creationflags=config.NO_WIN, close_fds=True)
    except Exception:
        _CHARGER["fail_at"] = time.time()
        return False

    for _ in range(20):               # 最多等 10 秒（蓝牙认证在后台继续，不等它）
        time.sleep(0.5)
        if _charger_alive(0.6):
            return True
    _CHARGER["fail_at"] = time.time()
    return False
