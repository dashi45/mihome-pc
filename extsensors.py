# -*- coding: utf-8 -*-
"""外部传感器接入 —— 让"不局限于米家设备"这件事真的能用。

小米生态之外的设备（ESP32、树莓派、Arduino、OpenWrt、甚至手机上的
快捷指令/HTTP 小组件）都能往这个面板里推温湿度，和环境卡里的米家
传感器混在一条曲线上看：

    POST http://127.0.0.1:8765/api/ext/push
    Content-Type: application/json
    {"key":"面板里显示的接入密钥","id":"esp32-living",
     "name":"客厅 ESP32","room":"客厅","temp":24.6,"hum":52}

也给不方便发 JSON 的设备留了 GET 口（单片机拼字符串最省事）：

    GET /api/ext/push?k=KEY&id=esp32-living&name=客厅ESP32&room=客厅&t=24.6&h=52

`id` 是设备唯一标识（自己起，稳定就行），第一次推会自动注册；
同一个 id 再推就是更新读数。`room` 可以和米家的房间对上，前端会自动
并进同一组；对不上就单独成一个新分组。

安全：写接口不要求米家登录（传感器不会登录），改用一把本地密钥；
密钥只存在本机 ext.json 里，前端页面能看到、也能一键重置。
"""

import atexit
import json
import os
import secrets
import threading
import time

# 路径统一由 config 提供（外部传感器配置和历史要一起搬，别只搬一半）
import config

CFG_PATH = config.EXT_CFG
HIST_PATH = config.EXT_HIST

KEEP_MS = 26 * 3600 * 1000      # 和本机采样一致：留 26 小时
MIN_GAP_MS = 15 * 1000          # 15 秒内的推包合并，避免刷爆历史
MAX_PTS = 4000

_lock = threading.Lock()
_cfg = None
_hist = None
_dirty = [0.0]


# ---------------- 存储 ----------------
def _load():
    global _cfg, _hist
    if _cfg is None:
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            _cfg = d if isinstance(d, dict) else {}
        except Exception:
            _cfg = {}
        _cfg.setdefault("key", secrets.token_hex(8))
        _cfg.setdefault("sensors", {})
    if _hist is None:
        try:
            with open(HIST_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            _hist = d if isinstance(d, dict) else {}
        except Exception:
            _hist = {}
    return _cfg


def _save(force=False):
    now = time.time()
    if not force and now - _dirty[0] < 8:
        return
    _dirty[0] = now
    try:
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)
    except Exception:
        pass
    try:
        tmp = HIST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_hist, f, separators=(",", ":"))
        os.replace(tmp, HIST_PATH)
    except Exception:
        pass


# ---------------- 读 ----------------
def key():
    _load()
    return _cfg["key"]


def sensors():
    _load()
    return _cfg["sensors"]


def _num(v, nd=None, lo=-100, hi=10000):
    try:
        if v is None or v == "":
            return None
        f = float(v)
        if f != f or f < lo or f > hi:      # NaN / 离谱值直接丢
            return None
        return round(f, nd) if nd is not None else int(round(f))
    except Exception:
        return None


def history_of(sid):
    _load()
    pts = _hist.get(sid) or []
    cut = int(time.time() * 1000) - KEEP_MS
    return [p for p in pts if p and p[0] >= cut]


def listing():
    """给前端：设备清单 + 各自 26 小时历史（拆成温度和湿度两条）"""
    _load()
    out = []
    for sid, s in _cfg["sensors"].items():
        pts = history_of(sid)
        out.append({
            "id": sid,
            "did": "ext:" + sid,
            "name": s.get("name") or sid,
            "room": s.get("room") or "外部设备",
            "temp": s.get("temp"),
            "hum": s.get("hum"),
            "co2": s.get("co2"),
            "pm25": s.get("pm25"),
            "at": s.get("at"),
            "src": s.get("src") or "http",
            "hist": {
                "temp": [[p[0], p[1]] for p in pts if len(p) > 1 and p[1] is not None],
                "hum": [[p[0], p[2]] for p in pts if len(p) > 2 and p[2] is not None],
            },
        })
    out.sort(key=lambda x: (x["room"], x["name"]))
    return out


# ---------------- 写 ----------------
def push(payload, src="http"):
    """推一条读数进来。payload 里的 key 必须对。"""
    _load()
    if not isinstance(payload, dict):
        return False, "payload 不是对象"
    if str(payload.get("key") or payload.get("k") or "").strip() != _cfg["key"]:
        return False, "接入密钥不对"

    sid = str(payload.get("id") or payload.get("sid") or "").strip()
    if not sid:
        return False, "缺少 id"
    if len(sid) > 64:
        sid = sid[:64]

    temp = _num(payload.get("temp", payload.get("t")), 1, -50, 80)
    hum = _num(payload.get("hum", payload.get("h")), 0, 0, 100)
    co2 = _num(payload.get("co2"), 0, 0, 20000)
    pm25 = _num(payload.get("pm25"), 1, 0, 2000)
    if temp is None and hum is None and co2 is None and pm25 is None:
        return False, "没有可用的读数（temp/hum/co2/pm25 至少要有一个）"

    now_ms = int(time.time() * 1000)
    is_new = sid not in _cfg["sensors"]
    s = _cfg["sensors"].get(sid) or {}
    if payload.get("name"):
        s["name"] = str(payload["name"])[:40]
    if payload.get("room") is not None and payload.get("room") != "":
        s["room"] = str(payload["room"])[:40]
    s.setdefault("name", sid)
    s.setdefault("room", "外部设备")
    s["src"] = src
    s["at"] = now_ms // 1000
    for k, v in (("temp", temp), ("hum", hum), ("co2", co2), ("pm25", pm25)):
        if v is not None:
            s[k] = v
    _cfg["sensors"][sid] = s

    # 历史：同上一条太近就只更新最后一点，别把 26 小时塞满
    arr = _hist.get(sid) or []
    if arr and now_ms - arr[-1][0] < MIN_GAP_MS:
        arr[-1] = [now_ms,
                   temp if temp is not None else arr[-1][1],
                   hum if hum is not None else arr[-1][2]]
    else:
        arr.append([now_ms, temp, hum])
    cut = now_ms - KEEP_MS
    if arr and arr[0][0] < cut:
        arr = [p for p in arr if p[0] >= cut]
    if len(arr) > MAX_PTS:
        step = len(arr) // MAX_PTS + 1
        arr = arr[::step]
    _hist[sid] = arr
    # 新设备立刻落盘：_save() 默认有 30 秒节流，程序被强杀时
    # 刚接入的设备就会丢（我调试时就撞上过），所以注册这条必须绕过节流。
    _save(force=is_new)
    return True, sid


def update(sid, name=None, room=None):
    _load()
    s = _cfg["sensors"].get(sid)
    if not s:
        return False, "没有这个设备"
    if name:
        s["name"] = str(name)[:40]
    if room is not None:
        s["room"] = str(room)[:40] or "外部设备"
    _save(True)
    return True, "ok"


def remove(sid):
    _load()
    if sid not in _cfg["sensors"]:
        return False, "没有这个设备"
    _cfg["sensors"].pop(sid, None)
    _hist.pop(sid, None)
    _save(True)
    return True, "ok"


def reset_key():
    _load()
    _cfg["key"] = secrets.token_hex(8)
    _save(True)
    return _cfg["key"]


def flush():
    """退出前把内存里的读数写盘，别让最后几十分钟的数据白攒。"""
    try:
        if _cfg is not None:
            _save(True)
    except Exception:
        pass


atexit.register(flush)


def template(sid="esp32-temp"):
    """给用户抄的推送示例（前端也会拼，但这里存一份权威写法）"""
    k = key()
    return {
        "post": {
            "url": "http://127.0.0.1:8765/api/ext/push",
            "headers": {"Content-Type": "application/json"},
            "body": {"key": k, "id": sid, "name": "客厅传感器",
                     "room": "客厅", "temp": 24.6, "hum": 52},
        },
        "get": "http://127.0.0.1:8765/api/ext/push?k=%s&id=%s&name=客厅传感器&room=客厅&t=24.6&h=52"
               % (k, sid),
    }
