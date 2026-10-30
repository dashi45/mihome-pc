# -*- coding: utf-8 -*-
"""用电统计：按天累计电量 + 功率积分。

两种取数口径：
  1. 有 power-consumption（插座）→ 云端 /v2/user/statistics 按天给累计电量，准确
  2. 只有 electric-power（空调伴侣）→ 云端不存统计，只能把原始功率点积分成电量
     （结果与米家 App 的月用电相差约 4%，App 也是这么估的）

已算出的"设备+日期 → kWh"落在 `.energy_days.json`，避免每次打开页面都重算。
"""
import json
import os
import time

from datetime import date, timedelta

import config


# ---------- 用电统计（能源） ----------
# 两种取数口径：
#   1. 有 power-consumption（插座）→ 云端 /v2/user/statistics 按天给累计电量，准确
#   2. 只有 electric-power（空调伴侣）→ 云端不存统计，只能把原始功率点积分成电量，
#      结果与米家 App 的月用电相差约 4%（App 也是这么估的）
from services.state import S

_EN_DAY = {}          # "did|YYYY-MM-DD" -> kWh（0 也算已知，区别于"没查过"）
_EN_DAY_PATH = config.ENERGY_DAYS
_EN_DAY_SAVE = [0.0]
_EN_STAT = {}         # did|key|t0|t1 -> (ts, {date: kwh})


def _en_load_days():
    try:
        with open(_EN_DAY_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            _EN_DAY.update(d)
    except Exception:
        pass


def _en_save_days(force=False):
    now = time.time()
    if not force and now - _EN_DAY_SAVE[0] < 20:
        return
    _EN_DAY_SAVE[0] = now
    try:
        tmp = _EN_DAY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_EN_DAY, f)
        os.replace(tmp, _EN_DAY_PATH)
    except Exception:
        pass


_en_load_days()


def _en_window(rng, offset):
    """返回 (日期列表, 标题)"""
    today = date.today()
    if rng == "week":
        start = today - timedelta(days=today.weekday()) - timedelta(weeks=offset)
        days = [start + timedelta(days=i) for i in range(7)]
        return days, "%d月%d日 – %d月%d日" % (
            days[0].month, days[0].day, days[-1].month, days[-1].day)
    y, m = today.year, today.month - offset
    while m <= 0:
        m += 12
        y -= 1
    start = date(y, m, 1)
    nxt = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
    days, d = [], start
    while d < nxt:
        days.append(d)
        d += timedelta(days=1)
    return days, "%d年%d月" % (y, m)


async def _en_stat_days(did, key, days):
    """按天统计电量（power-consumption，单位 0.01kWh）"""
    t0 = int(time.mktime(days[0].timetuple()))
    t1 = int(time.mktime((days[-1] + timedelta(days=1)).timetuple()))
    ck = "%s|%s|%d|%d" % (did, key, t0, t1)
    hit = _EN_STAT.get(ck)
    if hit and time.time() - hit[0] < 300:
        return dict(hit[1])
    rows = await S["service"].miio_request("/v2/user/statistics", {
        "did": did, "key": key, "data_type": "stat_day_v3",
        "limit": min(500, len(days) + 3),
        "time_start": t0, "time_end": t1}) or []
    out = {}
    for r in rows:
        v = r.get("value")
        try:
            vals = json.loads(v) if isinstance(v, str) else v
        except Exception:
            continue
        if not isinstance(vals, (list, tuple)) or not vals or not r.get("time"):
            continue
        try:
            kwh = max(float(x) for x in vals) / 100.0
        except Exception:
            continue
        ds = time.strftime("%Y-%m-%d", time.localtime(int(r["time"])))
        out[ds] = round(max(out.get(ds, 0.0), kwh), 3)
    _EN_STAT[ck] = (time.time(), dict(out))
    return out


async def _en_raw_day(did, key, d0):
    """把一天的原始功率点积分成 kWh；点数被 500 截断时自动二分重取"""
    async def fetch(t0, t1):
        return await S["service"].miio_request("/v2/user/get_device_data_raw", {
            "did": did, "uid": "", "type": "prop", "key": key,
            "time_start": int(t0), "time_end": int(t1), "limit": 500}) or []

    async def rec(t0, t1, depth):
        rows = await fetch(t0, t1)
        if len(rows) >= 495 and depth < 3 and t1 - t0 > 3600:
            mid = (t0 + t1) // 2
            a = await rec(t0, mid, depth + 1)
            b = await rec(mid, t1, depth + 1)
            return a + b
        return rows

    rows = await rec(d0, d0 + 86400, 0)
    vals = {}
    for r in rows:
        try:
            v = r.get("value")
            vv = json.loads(v) if isinstance(v, str) else v
            if isinstance(vv, (list, tuple)):
                vv = vv[0] if vv else None
            p = float(vv)
            t = int(r["time"])
        except Exception:
            continue
        if p < 0:
            continue
        vals[t] = p if t not in vals else max(vals[t], p)
    arr = sorted(vals.items())
    kwh = 0.0
    for i in range(len(arr) - 1):
        dt = arr[i + 1][0] - arr[i][0]
        if 0 < dt <= 7200:      # 超过 2 小时的间隔按停机算，不往下顺延
            kwh += arr[i][1] * dt / 3600.0 / 1000
    return round(kwh, 3)
