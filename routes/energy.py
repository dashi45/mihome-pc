# -*- coding: utf-8 -*-
"""用电统计接口。"""
from flask import Blueprint
from datetime import date

bp = Blueprint('energy', __name__)

from flask import jsonify, request

from services.state import S
from services.energy import _EN_STAT
from services.energy import _en_window
from services.devices import energy_props
from services.energy import _en_stat_days
import asyncio
from services.energy import _EN_DAY
from services.energy import _en_raw_day
import time
from services.energy import _en_save_days
from services.cloud import call
from services.cloud import PRIO_IDLE


@bp.route("/api/energy")
def api_energy():
    """能源页：日均 / 总量 / 环比 / 每日用电 / 设备排行。

    ?range=month|week&offset=N&refresh=1
    offset=0 为当前月（周），n 表示往前推 n 个周期。
    """
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401

    rng = (request.args.get("range") or "month").lower()
    if rng not in ("week", "month"):
        rng = "month"
    try:
        offset = max(0, min(24, int(request.args.get("offset") or 0)))
    except Exception:
        offset = 0
    if request.args.get("refresh") == "1":
        _EN_STAT.clear()

    days, label = _en_window(rng, offset)
    pdays, plabel = _en_window(rng, offset + 1)
    today = date.today()

    room_of = {}
    for home in S.get("homes") or []:
        for room in home.get("rooms") or []:
            for rid in room.get("dids") or []:
                room_of[str(rid)] = room.get("name") or "其他"

    targets = []
    for d in S["devices"]:
        ap = energy_props(d.get("model"))
        if ap:
            targets.append((d, ap))
    if not targets:
        return jsonify({"ok": True, "empty": True, "days": [], "devices": []})

    cur_days = [d for d in days if d <= today]
    budget = {"n": 8}       # 每次请求最多补 8 天历史，首屏不至于干等

    async def one(d, ap):
        did = str(d.get("did"))
        out = {"did": did, "name": d.get("name") or "设备",
               "room": d.get("room_name") or room_of.get(did, ""),
               "model": d.get("model"), "method": "stat",
               "daily": {}, "prev": {}}

        if "energy" in ap:
            si, pi = ap["energy"]
            key = "%d.%d" % (si, pi)
            try:
                out["daily"] = await _en_stat_days(did, key, days)
            except Exception:
                out["daily"] = {}
            try:
                out["prev"] = await _en_stat_days(did, key, pdays)
            except Exception:
                out["prev"] = {}
            return out

        out["method"] = "estimate"
        si, pi = ap["watt"]
        key = "%d.%d" % (si, pi)
        sem = asyncio.Semaphore(3)

        async def day_kwh(day, allow_fetch):
            ds = day.strftime("%Y-%m-%d")
            ck = "%s|%s" % (did, ds)
            if ck in _EN_DAY and ds != today.strftime("%Y-%m-%d"):
                return ds, _EN_DAY[ck]
            if not allow_fetch or budget["n"] <= 0:
                return ds, None
            budget["n"] -= 1
            async with sem:
                try:
                    v = await _en_raw_day(did, key,
                                          int(time.mktime(day.timetuple())))
                except Exception:
                    return ds, None
            if ds != today.strftime("%Y-%m-%d"):
                _EN_DAY[ck] = v
                _en_save_days()
            return ds, v

        jobs = [day_kwh(x, True) for x in cur_days]
        # 上个月的同期数据只在缓存里找，预算还剩就顺带补几天
        jobs += [day_kwh(x, budget["n"] > 2) for x in pdays]
        prev_set = {x.strftime("%Y-%m-%d") for x in pdays}
        for ds, v in await asyncio.gather(*jobs):
            if v is None:
                continue
            if ds in prev_set:
                out["prev"][ds] = v
            else:
                out["daily"][ds] = v
        return out

    async def _gather():
        return await asyncio.gather(*[one(d, ap) for d, ap in targets])

    try:
        rows = call(_gather(), timeout=150, prio=PRIO_IDLE)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    _en_save_days(force=True)

    cur_keys = [x.strftime("%Y-%m-%d") for x in cur_days]
    prev_keys = [x.strftime("%Y-%m-%d") for x in pdays]

    devs, partial = [], False
    for r in rows:
        daily = r.get("daily") or {}
        missing = [k for k in cur_keys if k not in daily]
        series = [daily.get(k) for k in cur_keys]
        prev_map = r.get("prev") or {}
        prev_series = [prev_map.get(k) for k in prev_keys]
        covered = [v for v in series if v is not None]
        prev_cov = [v for v in prev_series if v is not None]
        cur_sum = sum(covered)
        prev_sum = sum(prev_cov)
        # 历史天没补全（当前周期或上周期）都要让前端再来一轮
        if r["method"] == "estimate" and (
                missing or len(prev_cov) < len(prev_keys)):
            partial = True
        devs.append({
            "did": r["did"], "name": r["name"], "room": r["room"],
            "model": r["model"], "method": r["method"],
            "series": series,
            "kwh": round(cur_sum, 2),
            "avg": round(cur_sum / len(covered), 3) if covered else 0,
            "prevAvg": round(prev_sum / len(prev_cov), 3) if prev_cov else None,
            "prevCover": len(prev_cov), "prevDays": len(prev_keys),
            "partial": bool(missing),
        })

    total_series = []
    for i in range(len(cur_keys)):
        s = 0.0
        for dv in devs:
            v = dv["series"][i]
            if v:
                s += v
        total_series.append(round(s, 3))

    # 日均 = 各设备日平均之和（部分日期没补全时也不至于被拉低）
    avg = sum(dv["avg"] for dv in devs)
    # 有设备的上月数据还没补齐就先不给环比，免得数字跳来跳去
    lack = [dv for dv in devs if dv["prevAvg"] is None
            and dv["prevCover"] < dv["prevDays"] * 0.6]
    prev_avg = None if lack else sum(dv["prevAvg"] or 0 for dv in devs)
    delta = round(avg - prev_avg, 2) if prev_avg is not None else None

    ranked = sorted([d for d in devs if d["kwh"] > 0], key=lambda x: -x["kwh"])[:10]
    for d in ranked:
        d.pop("series", None)

    return jsonify({
        "ok": True, "range": rng, "offset": offset,
        "label": label, "prevLabel": plabel,
        "days": [x.strftime("%m/%d") for x in days],
        "dates": cur_keys,
        "today": today.strftime("%Y-%m-%d"),
        "total": total_series,
        "sum": round(sum(total_series), 2),
        "avg": round(avg, 2),
        "prevAvg": None if prev_avg is None else round(prev_avg, 2),
        "delta": delta,
        "daysCounted": len(cur_keys),
        "devices": ranked,
        "partial": partial,
    })
