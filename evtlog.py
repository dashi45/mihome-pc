# -*- coding: utf-8 -*-
"""
事件日志 —— 这个面板里唯一会"记住过去"的东西。

为什么需要它
------------
别的卡片都是快照：此刻多少度、此刻多少瓦。你离开八小时回来，看到的还是
当下这一帧，中间空调开过多久、灯是不是忘关、摄像头掉没掉线，一概不知道。
"监控"的本义是有事发生，不是现在多少。

所以这里做一条最简单的通路：**别人发现变化 -> 往这里丢一句人话**，
落盘、可回看、可统计。所有模块都可以调用 add()，不需要彼此认识。

存储选型
--------
SQLite（evt.db）。理由不是"要数据库"，而是对比 JSON 文件的三个实际问题：
  · JSON 要整份重写，进程被强杀就丢全部（extsensors 已经踩过这个坑）；
  · 追加式 jsonl 不能删旧、不能按条件查，半年后就是几十兆只能全读；
  · 按天分文件则"最近 40 条"要跨文件倒着翻。
单表 + ts 索引，上面三件事都一句话解决，而且够快。

保留策略：最多 5000 条 / 45 天 —— 这是家用面板，不是审计系统。
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

# 路径统一由 config 提供：以前这里用 "evt.db" 是直接拼当前文件所在目录，
# 一旦数据搬到 config/ 或 runtime/ 子目录（config.LAYOUT 就是这么规划的）
# 就会读到另一个（空的）库，历史事件全部"消失"。
import config

DB_PATH = config.EVT_DB

KEEP_ROWS = 5000
KEEP_DAYS = 45
TRIM_EVERY = 150          # 写满这么多条才做一次清理，别每次 INSERT 都扫

_lock = threading.RLock()
_conn = None
_writes = 0
_dedup = {}               # (kind, src, title) -> 上次写入时间
_HOOK = None              # 事件落盘后的回调（notify.py 用它做推送）


def set_hook(fn):
    """注册"事件发生后"的回调。回调会被 fire-and-forget 地调用，
    自己保证不阻塞、不抛异常。"""
    global _HOOK
    _HOOK = fn


# ------------------------------------------------------------------ 底层
def _db():
    global _conn
    if _conn is not None:
        return _conn
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=6.0)
    c.execute("PRAGMA journal_mode=WAL")      # 读写不互斥，查询不卡写入
    c.execute("PRAGMA synchronous=NORMAL")    # 断电最多丢最后几条，够用
    c.execute("""
        CREATE TABLE IF NOT EXISTS ev(
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      REAL NOT NULL,
            day     TEXT NOT NULL,
            kind    TEXT NOT NULL,
            src     TEXT NOT NULL DEFAULT '',
            title   TEXT NOT NULL,
            detail  TEXT NOT NULL DEFAULT '',
            level   TEXT NOT NULL DEFAULT 'info',
            extra   TEXT NOT NULL DEFAULT ''
        )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ev_ts ON ev(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ev_day ON ev(day)")
    c.commit()
    _conn = c
    return c


def add(kind, title, detail="", level="info", src="", extra=None, dedup=0):
    """记一条事件。

    dedup 是同类去抖的秒数：同 kind+src+title 在这段时间内只留第一条。
    家用场景里大量变化是抖的（开关继电器、湿度上下浮动、CPU 瞬时冲高），
    没有去抖的话列表会被同一条重复话术刷满，等于没有信息。

    返回事件 id；被去抖吃掉则返回 None。
    """
    global _writes
    now = time.time()
    key = (kind, src, title)
    with _lock:
        if dedup:
            last = _dedup.get(key)
            if last is not None and now - last < dedup:
                return None
            _dedup[key] = now
        try:
            c = _db()
            cur = c.execute(
                "INSERT INTO ev(ts,day,kind,src,title,detail,level,extra)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (now,
                 datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
                 kind, src or "", title or "", detail or "", level,
                 json.dumps(extra, ensure_ascii=False) if extra else ""))
            c.commit()
            rid = cur.lastrowid
        except Exception:
            return None
        _writes += 1
        if _writes >= TRIM_EVERY:
            _writes = 0
            _trim()
        if len(_dedup) > 4000:            # 长时间运行后清掉过期键
            cut = now - 86400
            for k in [k for k, t in _dedup.items() if t < cut]:
                _dedup.pop(k, None)

    # 推送给外部（notify.py）用回调注册，不在这里 import —— 事件层不该知道
    # 通知层存在，否则想关掉通知就得改事件代码。放在锁外调用，钩子慢也不卡写。
    hook = _HOOK
    if hook is not None:
        try:
            hook({"id": rid, "ts": now, "kind": kind, "src": src or "",
                  "title": title or "", "detail": detail or "",
                  "level": level or "info", "extra": extra or {}})
        except Exception:
            pass
    return rid


def _trim():
    try:
        c = _db()
        c.execute("DELETE FROM ev WHERE id <= (SELECT MAX(id) - ? FROM ev)", (KEEP_ROWS,))
        c.execute("DELETE FROM ev WHERE ts < ?",
                  (time.time() - KEEP_DAYS * 86400,))
        c.commit()
    except Exception:
        pass


# ------------------------------------------------------------------ 查询
def _row2dict(r):
    d = {
        "id": r[0], "ts": r[1], "day": r[2], "kind": r[3],
        "src": r[4], "title": r[5], "detail": r[6], "level": r[7],
    }
    if r[8]:
        try:
            d["extra"] = json.loads(r[8])
        except Exception:
            pass
    return d


def query(since_id=0, limit=60, before_id=0, kinds=None):
    """取事件。三种取法共用一个函数：
       since_id > 0        增量（前端轮询只要新的）
       before_id > 0       往前翻页（看历史）
       都为 0              最近 limit 条
    """
    try:
        limit = max(1, min(300, int(limit or 60)))
    except Exception:
        limit = 60
    sql = ("SELECT id,ts,day,kind,src,title,detail,level,extra FROM ev")
    where, args = [], []
    if since_id:
        where.append("id > ?")
        args.append(int(since_id))
    if before_id:
        where.append("id < ?")
        args.append(int(before_id))
    if kinds:
        ks = [k for k in kinds if k]
        if ks:
            where.append("kind IN (%s)" % ",".join("?" * len(ks)))
            args += ks
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    c = _db()
    with _lock:
        rows = list(c.execute(sql, args))
    # 一律按时间倒序（新的在前）。前端是"往前插"的列表，两种取法用同一种
    # 顺序才不会在首屏和增量之间翻转 —— 这里翻过一次，结果最新的 8 条被切掉了。
    return [_row2dict(r) for r in rows]


def total():
    try:
        c = _db()
        with _lock:
            return list(c.execute("SELECT COUNT(*) FROM ev"))[0][0]
    except Exception:
        return 0


def digest(hours=24):
    """过去 N 小时发生了什么：给一句话统计 + 分类计数。

    不做花哨的 NLG。人想知道的就是"哪几类事、各几件、最近一件是什么"。
    """
    now = time.time()
    since = now - hours * 3600
    try:
        c = _db()
        with _lock:
            rows = list(c.execute(
                "SELECT kind,level,COUNT(*) FROM ev WHERE ts >= ? GROUP BY kind,level",
                (since,)))
            first = list(c.execute(
                "SELECT ts FROM ev WHERE ts >= ? ORDER BY ts ASC LIMIT 1", (since,)))
            last = list(c.execute(
                "SELECT id,ts,day,kind,src,title,detail,level,extra FROM ev"
                " WHERE ts >= ? ORDER BY ts DESC LIMIT 1", (since,)))
    except Exception:
        rows, first, last = [], [], []

    by_kind, by_level = {}, {"info": 0, "good": 0, "warn": 0, "bad": 0}
    n = 0
    for kind, level, cnt in rows:
        by_kind[kind] = by_kind.get(kind, 0) + cnt
        by_level[level] = by_level.get(level, 0) + cnt
        n += cnt
    return {
        "hours": hours,
        "n": n,
        "by_kind": by_kind,
        "by_level": by_level,
        "since": first[0][0] if first else None,
        "last": _row2dict(last[0]) if last else None,
    }


# ------------------------------------------------------------------ 变化检测
# 这些函数被 app.py 在处理已有请求时顺手调用（不额外发网络请求）。
# 每个检测器都自己保存"上一次的样子"，进程重启后第一次只建立基线，
# 不会把全部设备当成"刚变化"刷一遍。

_DEV = {}       # did -> 上次观测到的精简状态
_PCADD = {}     # "mem"/"temp"/... -> 上次记录时间，用于长去抖
_WX = {}        # 天气上次的降雨/空气质量等级
_READY = {"dev": False, "wx": False}
_EVT_LAST_REAL = {"live": 0.0}    # 上一次真的观测到设备数据（毫秒级时间戳）


def _f(v):
    try:
        return float(v)
    except Exception:
        return None


def observe_devices(live, meta=None):
    """设备实时数据变化 -> 事件。

    live: {did: {on, mode, mode_t, temp, hum, batt, watt}}   (/api/live 的结果)
    meta: {did: {name, room}}                                 (展示用)
    第一次调用只建基线。
    """
    live = live or {}
    meta = meta or {}
    now = time.time()
    first = not _READY["dev"]
    if live:
        _EVT_LAST_REAL["live"] = now

    for did, cur in live.items():
        if not isinstance(cur, dict):
            continue
        info = meta.get(did) or {}
        name = info.get("name") or did
        room = info.get("room") or ""
        label = ("%s · %s" % (room, name)) if room else name
        prev = _DEV.get(did)

        if prev is None:
            _DEV[did] = dict(cur, _t=now)
            continue

        # ① 开关
        if "on" in cur and "on" in prev and bool(cur["on"]) != bool(prev["on"]):
            on = bool(cur["on"])
            watt = _f(cur.get("watt"))
            # 只有真·大功率（空调、热水器、充电器）才值得说"开始运行"；
            # 台灯 12W 说"开始运行"听着别扭，说"已开启"才对。
            heavy = watt is not None and watt > 30
            title = "%s %s" % (label, ("开始运行" if heavy else "已开启") if on
                               else ("停止运行" if heavy else "已关闭"))
            add("power", title, level="info" if on else "good",
                src=did, dedup=25,
                extra={"did": did, "on": on,
                       "watt": round(watt, 1) if watt is not None else None})

        # ② 功耗启停（开关状态没变，但功率从 0 跳起来 —— 空调压缩机、充电器都这样）
        w0, w1 = _f(prev.get("watt")), _f(cur.get("watt"))
        if w0 is not None and w1 is not None:
            if w0 < 3 and w1 >= 15:
                add("energy", "%s 开始用电" % label,
                    "%d W" % round(w1), src=did, dedup=120,
                    extra={"did": did})
            elif w0 >= 15 and w1 < 3:
                add("energy", "%s 停止用电" % label,
                    "峰值后归零", src=did, dedup=120, extra={"did": did})

        # ③ 温度突变：只按"跨过 2°C 且间隔 25 分钟以上"记，不然就是刷屏
        t0, t1 = _f(prev.get("temp")), _f(cur.get("temp"))
        if t0 is not None and t1 is not None:
            base = prev.get("_tref") or prev.get("_t") or now
            if abs(t1 - t0) >= 2.0 and now - base >= 1500:
                d = t1 - t0
                add("temp", "%s 温度%s" % (label, "上升" if d > 0 else "下降"),
                    "%.1f° → %.1f°" % (t0, t1),
                    level="warn" if abs(d) >= 4 else "info",
                    src=did, dedup=600, extra={"did": did})
                _DEV[did] = dict(cur, _t=now, _tref=base)
                continue

        # ④ 湿度越界（干到起皮 / 湿到发霉），3 小时才提醒一次
        h = _f(cur.get("hum"))
        if h is not None:
            if h < 35:
                add("hum", "%s 空气偏干" % label, "湿度 %.0f%%" % h,
                    level="warn", src=did, dedup=3 * 3600, extra={"did": did})
            elif h > 72:
                add("hum", "%s 空气偏潮" % label, "湿度 %.0f%%" % h,
                    level="warn", src=did, dedup=3 * 3600, extra={"did": did})

        # ⑤ 电量告急
        b = _f(cur.get("batt"))
        if b is not None and b <= 20:
            add("batt", "%s 电量偏低" % label, "%d%%" % round(b),
                level="warn" if b > 10 else "bad", src=did,
                dedup=6 * 3600, extra={"did": did})

        # ⑥ 模式变化（空调制冷/制热、风扇档位这类）
        if cur.get("mode_t") and prev.get("mode_t") and cur["mode_t"] != prev["mode_t"]:
            add("mode", "%s 切换到%s" % (label, cur["mode_t"]),
                src=did, dedup=20, extra={"did": did})

        new = dict(cur, _t=now, _tref=_DEV.get(did, {}).get("_tref") or now)
        if abs((_f(cur.get("temp")) or 0) - (_f(prev.get("temp")) or 0)) >= 2.0:
            new["_tref"] = now
        _DEV[did] = new

    _READY["dev"] = True
    if first:
        return
    _prune_dev(live)


def _prune_dev(live):
    """设备被删掉/长期不上报时清掉基线，避免字典无限长。"""
    now = time.time()
    for did in [d for d, v in _DEV.items()
                if d not in live and now - (v.get("_t") or 0) > 86400]:
        _DEV.pop(did, None)


def observe_online(offline_names, online_names):
    """设备在线状态变化（需要 device_list，开销比属性查询大，慢一点调）。"""
    prev = _READY.get("online")
    cur = {str(x): True for x in online_names or []}
    if prev is None:
        _READY["online"] = cur
        return
    for did in cur:
        if did not in prev:
            add("online", "%s 上线" % (offline_names.get(did) or did),
                src=did, level="good", dedup=60, extra={"did": did})
    for did in prev:
        if did not in cur:
            add("offline", "%s 离线" % (offline_names.get(did) or did),
                src=did, level="bad", dedup=60, extra={"did": did})
    _READY["online"] = cur


def observe_pc(snap):
    """本机状态越界。每个指标单独长去抖，宁可漏也不要吵。"""
    if not snap or not snap.get("ok"):
        return
    now = time.time()
    cpu = snap.get("cpu") or {}
    mem = snap.get("mem") or {}

    def once(key, gap):
        if now - (_PCADD.get(key) or 0) < gap:
            return False
        _PCADD[key] = now
        return True

    pct = _f(cpu.get("pct"))
    if pct is not None and pct >= 92 and once("cpu", 1800):
        add("pc", "CPU 占用打满", "%.0f%% · %s" % (pct, cpu.get("name") or ""),
            level="warn", dedup=0, extra={"pct": round(pct)})

    t = _f(cpu.get("temp"))
    if t is not None and t >= 88 and once("ctemp", 1800):
        add("pc", "CPU 温度偏高", "%.0f°C" % t, level="warn", dedup=0)

    mp = _f(mem.get("pct"))
    if mp is not None and mp >= 92 and once("mem", 1800):
        add("pc", "内存快满了", "%.0f%% · 已用 %s / %s" % (
            mp, _gb(mem.get("used")), _gb(mem.get("total"))),
            level="warn", dedup=0)

    for p in ((snap.get("disk") or {}).get("parts") or []):
        free_pct = _f(p.get("pct"))
        if free_pct is not None and (100 - free_pct) < 8 and once("disk" + str(p.get("mnt")), 86400):
            add("pc", "%s 空间不足" % p.get("mnt"), "剩余 %s" % _gb(p.get("free")),
                level="bad", dedup=0, extra={"mnt": p.get("mnt")})


def _gb(v):
    v = _f(v)
    if v is None:
        return "—"
    if v >= 1024 ** 3:
        return "%.1f G" % (v / 1024 ** 3)
    return "%.0f M" % (v / 1024 ** 2)


def observe_weather(wx):
    """降雨起止、空气质量跨级。"""
    if not wx or not wx.get("ok"):
        return
    now_d = wx.get("now") or {}
    txt = (now_d.get("text") or "")
    aqi = wx.get("aqi") or {}
    lv = aqi.get("lv") or ""
    first = not _READY["wx"]

    rain = any(k in txt for k in ("雨", "雪", "雷", "阵雨", "毛毛"))
    prev_rain = _WX.get("rain")
    prev_lv = _WX.get("lv")

    if not first:
        if rain and prev_rain is False:
            add("wx", "开始下雨了", txt, level="info", src="wx", dedup=1800,
                extra={"text": txt})
        elif prev_rain is True and not rain:
            add("wx", "雨停了", txt or "转为%s" % (txt or "晴"), level="good",
                src="wx", dedup=1800)
        if lv and prev_lv and lv != prev_lv:
            order = ["优", "良", "轻度污染", "中度污染", "重度污染", "严重污染"]
            try:
                worse = order.index(lv) > order.index(prev_lv)
            except ValueError:
                worse = False
            add("wx", "空气质量%s" % lv,
                "AQI %s（原 %s）" % (round(_f(aqi.get("us")) or 0), prev_lv),
                level="warn" if worse else "good", src="wx", dedup=3600)

    _WX["rain"] = rain
    _WX["lv"] = lv
    _READY["wx"] = True


def mark_seen(kind):
    """给后台采集器判断"刚刚有前端在采"，避免重复打云端。"""
    if kind == "live":
        _EVT_LAST_REAL["live"] = time.time()


def idle_live_seconds():
    t = _EVT_LAST_REAL["live"] or 0
    return time.time() - t if t else 1e9
