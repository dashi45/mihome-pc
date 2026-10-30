# -*- coding: utf-8 -*-
"""充电器反向代理 + 降级兜底接口。"""
from flask import Blueprint

bp = Blueprint('charger', __name__)

import urllib.request

from flask import Response, redirect, request

import config
import services.charger as charger


@bp.route("/charger")
def charger_root():
    return redirect("/charger/")


@bp.route("/charger/", defaults={"sub": ""}, methods=["GET", "POST"])
@bp.route("/charger/<path:sub>", methods=["GET", "POST"])
def charger_proxy(sub):
    """把 /charger/* 原样转给本地桥接服务，iframe 与米家同源，不会有跨域问题。

    注意 methods 必须显式写 GET 和 POST：Flask 默认只接受 GET，
    漏了的话页面能打开、读数据也正常，但所有写操作（场景切换 / 接口开关 /
    定时设置 / 屏显）一律 405 Method Not Allowed —— 前端只会看到
    「场景切换失败: 405」，看不出路由没放行。
    """
    if not charger._ensure_charger():
        return Response(
            "<meta charset='utf-8'>"
            "<div style='font:15px/1.8 system-ui;padding:40px;color:#333'>"
            "<h3 style='margin:0 0 12px'>充电器服务没起来</h3>"
            "<p style='color:#666'>蓝牙桥接进程启动失败，常见原因：</p>"
            "<ul style='color:#666'>"
            "<li>系统蓝牙未开启，或充电器被手机米家 App 占用</li>"
            "<li>缺少 bleak / cryptography 依赖</li></ul>"
            "<p style='color:#999;font-size:13px'>详情见 charger/charger.log</p>"
            "</div>",
            status=503, mimetype="text/html")

    qs = request.query_string.decode("utf-8", "ignore")
    url = "http://127.0.0.1:%d/%s" % (config.CHARGER_PORT, sub)
    if qs:
        url += "?" + qs

    data = request.get_data() if request.method in ("POST", "PUT", "PATCH") else None
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() in ("content-type", "accept", "accept-language")}
    req = urllib.request.Request(url, data=data, headers=fwd, method=request.method)

    try:
        with charger._OPENER.open(req, timeout=30) as r:
            body = r.read()
            resp = Response(body, status=r.status)
            resp.headers["Content-Type"] = r.headers.get("Content-Type",
                                                          "application/octet-stream")
            resp.headers["Cache-Control"] = "no-store"
            return resp
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code,
                        mimetype=e.headers.get("Content-Type", "text/plain"))
    except Exception as e:
        return Response("桥接服务无响应：%s" % e, status=502, mimetype="text/plain")


# 兜底：充电器页面里若有绝对路径的旧请求，也一并转给桥接服务
@bp.route("/api/state")
def charger_state_fallback():
    return charger_proxy("api/state")


@bp.route("/api/set", methods=["POST"])
def charger_set_fallback():
    return charger_proxy("api/set")
