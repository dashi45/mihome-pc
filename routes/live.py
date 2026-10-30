# -*- coding: utf-8 -*-
"""实时状态与气候历史（前端 2 秒/5 秒轮询的落点）。"""
from flask import Blueprint

bp = Blueprint('live', __name__)

import time

from flask import jsonify, request

import services.charger as charger
import services.live as live_svc
from services.guard import _json_body
from services.state import S
from services.devices import _QUICK_CACHE
from services.devices import _LIVE_CACHE
from services.cloud import _NG
from services.devices import _LIVE_BUSY
import evtlog
from services.live import _dev_meta
from services.cloud import _note_cloud_error
import netguard
from services.devices import _HIST_CACHE
import json
from services.devices import readable_props
import asyncio
from services.cloud import call


@bp.route("/api/live", methods=["POST"])
def api_live():
    """一次拿回所有在线设备的实时数据。

    支持两种粒度（前端分层轮询用）：

      quick=1  只读开关状态。一次请求、十几个 param，几十毫秒就回来，
               所以可以 4 秒刷一轮 —— 手机 App 或实体按键改了状态，
               屏幕上也能很快跟上。
      默认     开关 + 温度/湿度/电量/功率/档位/模式。这些读数变化慢，
               整页重绘也重，45 秒一轮足够。

    以前是"一刀切 20 秒"，结果是两头不讨好：开关跟随慢到 20 秒，
    而温湿度这种五分钟都不会变的读数每 20 秒白读一次。
    """
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401

    body = _json_body() or {}
    quick = bool(body.get("quick"))
    cache = _QUICK_CACHE if quick else _LIVE_CACHE

    now = time.time()
    if cache["data"] is not None and now - cache["t"] < 2.5:
        return jsonify({"ok": True, "live": cache["data"], "cached": True,
                        "conn": _NG.state, "quick": quick,
                        "charger_w": charger._charger_power()})

    # 防重入：上一轮还没回来就别再发一轮。
    # 弱网下一次全量刷新可能要好几秒，20 秒定时器照样往下打，
    # 请求会越堆越多（云端还会限流），最后整条链路一起变慢。
    if not _LIVE_BUSY.acquire(blocking=False):
        if cache["data"] is not None:
            return jsonify({"ok": True, "live": cache["data"], "busy": True,
                            "conn": _NG.state, "quick": quick,
                            "charger_w": charger._charger_power()})
        return jsonify({"ok": False, "error": "上一轮刷新还没结束"}), 429
    try:
        data = live_svc._collect_live(quick=quick)
        cache["data"], cache["t"] = data, time.time()
        # 变化检测只在全量刷新时做：quick 只带回 on，缺读数会让事件流
        # 把"温湿度从 26 变成 None"当成一次变化
        if not quick:
            try:
                evtlog.observe_devices(data, _dev_meta())
            except Exception:
                pass
        return jsonify({"ok": True, "live": data, "quick": quick,
                        "conn": _NG.state, "charger_w": charger._charger_power()})
    except Exception as e:
        # 不再把 "Error https://api.io.mi.com/..." 原样丢给界面，
        # 用户看不懂，客服也看不懂
        _note_cloud_error(e, "/api/live")
        # 手上有上一轮数据就先顶上，别因为一次抖动把整页设备打灰。
        # 前端不认识 stale 字段也没关系，它照常渲染 live。
        stale = cache.get("data")
        if stale is not None:
            return jsonify({"ok": True, "live": stale, "quick": quick,
                            "stale": True, "conn": _NG.state,
                            "error": netguard.summarize(e),
                            "charger_w": charger._charger_power()})
        return jsonify({"ok": False, "error": netguard.summarize(e),
                        "conn": _NG.state}), 500
    finally:
        _LIVE_BUSY.release()


@bp.route("/api/climate/history")
def api_climate_history():
    """温湿度云端历史：米家 App 里那条 24 小时曲线就是这些点。

    本地只能按 20 秒采样，程序刚装上时图上只有一小截，
    所以必须把云端的原始上报记录并进来，才能看到一整天的走势。

    米家接口：POST /v2/user/get_device_data_raw
      {did, uid, type:"prop", key:"siid.piid", time_start, time_end, limit<=500}
    返回形如 {time, value:"[26.299999]"} 的点，按时间倒序。"""
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    try:
        hours = int(request.args.get("hours") or 24)
    except Exception:
        hours = 24
    hours = max(1, min(24, hours))

    now = time.time()
    hit = _HIST_CACHE.get(hours)
    if hit and now - hit[0] < 120:
        return jsonify({"ok": True, "hours": hours, "devices": hit[1], "cached": True})

    def raw_value(v):
        """云端 value 是 '"[26.299999]"' 这样的字符串，取第一个数"""
        try:
            arr = json.loads(v) if isinstance(v, str) else v
            if isinstance(arr, (list, tuple)) and arr:
                return float(arr[0])
            return float(arr)
        except Exception:
            return None

    # 只问真正带温湿度属性的设备，别为无关设备发请求
    targets = []
    for d in S["devices"]:
        iids = {k: (si, pi) for k, si, pi, _ in readable_props(d.get("model"))
                if k in ("temp", "hum")}
        if iids:
            targets.append((d, iids))

    async def one(d, iids):
        did = str(d.get("did"))
        out = {}
        for k, (si, pi) in iids.items():
            try:
                rows = await S["service"].miio_request(
                    "/v2/user/get_device_data_raw",
                    {"did": did, "uid": "", "type": "prop",
                     "key": f"{si}.{pi}",
                     "time_start": int(now - hours * 3600),
                     "time_end": int(now), "limit": 500})
            except Exception:
                continue
            pts = []
            for r in rows or []:
                v = raw_value(r.get("value"))
                t = r.get("time")
                if v is None or not t:
                    continue
                pts.append([int(t) * 1000, round(v, 1)])
            if pts:
                pts.sort(key=lambda x: x[0])
                out[k] = pts
        return did, out

    async def _gather():
        sem = asyncio.Semaphore(6)

        async def wrap(d, iids):
            async with sem:
                try:
                    return await one(d, iids)
                except Exception:
                    return str(d.get("did")), {}

        return dict(await asyncio.gather(*[wrap(d, i) for d, i in targets]))

    try:
        data = call(_gather(), timeout=60)
        _HIST_CACHE[hours] = (time.time(), data)
        return jsonify({"ok": True, "hours": hours, "devices": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
