# -*- coding: utf-8 -*-
"""首页与开机自启开关。"""
from flask import Blueprint

bp = Blueprint('system', __name__)

import os
import re

from flask import current_app, jsonify, request, send_from_directory

import autostart


@bp.route("/")
def index():
    """返回 index.html，并给页面上每个 /static/xxx 拼上文件修改时间做版本号。

    为什么不再手写文件清单：改造前这里硬编码了 6 个文件名，而页面实际引用
    11 个脚本 —— 新增一个模块忘了加进清单，它就会一直吃 WebView 缓存，
    改完代码不生效（表现是"改了没反应"，极难查）。现在直接扫描 html 里出现
    的所有 /static/ 引用，命中磁盘文件就加 ?v=<mtime>，永不过期。
    """
    static_dir = current_app.static_folder
    try:
        with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as f:
            txt = f.read()
        resp = current_app.make_response(_stamp_static(txt, static_dir))
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp
    except Exception:
        return send_from_directory(static_dir, "index.html")


_STATIC_REF = re.compile(r"/static/([A-Za-z0-9_.\-]+)")


def _stamp_static(txt, static_dir):
    """把 /static/app.js 变成 /static/app.js?v=1730000000。"""
    def _one(m):
        name = m.group(1)
        try:
            v = int(os.path.getmtime(os.path.join(static_dir, name)))
        except OSError:
            return m.group(0)          # 文件不存在就原样留着，让浏览器 404 说清楚
        return "/static/%s?v=%d" % (name, v)

    return _STATIC_REF.sub(_one, txt)


@bp.route("/api/autostart", methods=["GET", "POST"])
def api_autostart():
    """开机自启开关。

    只写 HKCU\\...\\Run，不碰 HKLM —— 免管理员权限。
    写之前会校验目标文件真实存在，否则注册表项会指向空气。
    """
    if request.method == "GET":
        return jsonify({"ok": True, **autostart.status()})
    body = request.get_json(silent=True) or {}
    want = bool(body.get("enabled"))
    ok, err = autostart.set_enabled(want)
    if not ok:
        return jsonify({"ok": False, "error": err, **autostart.status()}), 400
    return jsonify({"ok": True, **autostart.status()})
