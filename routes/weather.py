# -*- coding: utf-8 -*-
"""室外天气与空气质量。"""
from flask import Blueprint

bp = Blueprint('weather', __name__)

from flask import jsonify, request

import weather
from services.guard import _json_body
import evtlog


# ---------- 室外天气 / 空气质量 ----------
# 数据来自 Open-Meteo（免费、无需 Key），模块内部自带 10 分钟缓存和失败退避，
# 所以这里直接同步返回即可，不会因为外网抖动卡住整个仪表盘。
@bp.route("/api/weather")
def api_weather():
    force = request.args.get("force") == "1"
    wx = weather.snapshot(force=force)
    try:
        evtlog.observe_weather(wx)
    except Exception:
        pass
    return jsonify(wx)


@bp.route("/api/weather/place", methods=["POST"])
def api_weather_place():
    d = _json_body()
    if d is None:
        return jsonify({"ok": False, "error": "需要 application/json"}), 415
    try:
        city = (d.get("city") or "").strip()
        if city:
            weather.set_place(city=city)
        else:
            weather.set_place(lat=d.get("lat"), lon=d.get("lon"), name=d.get("name"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": "设置位置失败：%s" % e}), 500
    return jsonify({"ok": True, "weather": weather.snapshot(force=True)})
