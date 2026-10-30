# -*- coding: utf-8 -*-
"""外部传感器（ESP32 / 脚本 / 快捷指令接入）。"""
from flask import Blueprint

bp = Blueprint('ext', __name__)

from flask import jsonify, request

import extsensors
from services.guard import _json_body


# ---------- 外部传感器（非米家设备接入） ----------
# 这条路不要求米家登录：ESP32 / 树莓派 / 快捷指令不会登录，用独立密钥鉴权。
@bp.route("/api/ext/push", methods=["GET", "POST"])
def api_ext_push():
    if request.method == "POST":
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            # 有些单片机只会发表单，兜一下
            d = request.form.to_dict() or {}
    else:
        d = request.args.to_dict()
    ok, msg = extsensors.push(d)
    if not ok:
        return jsonify({"ok": False, "error": msg}), (403 if "密钥" in msg else 400)
    return jsonify({"ok": True, "id": msg})


@bp.route("/api/ext/sensors")
def api_ext_sensors():
    return jsonify({
        "ok": True,
        "key": extsensors.key(),
        "sensors": extsensors.listing(),
        "template": extsensors.template(),
    })


@bp.route("/api/ext/update", methods=["POST"])
def api_ext_update():
    d = _json_body()
    if d is None:
        return jsonify({"ok": False, "error": "需要 application/json"}), 415
    ok, msg = extsensors.update(str(d.get("id") or ""), d.get("name"), d.get("room"))
    return jsonify({"ok": ok, "error": None if ok else msg,
                    "sensors": extsensors.listing()})


@bp.route("/api/ext/remove", methods=["POST"])
def api_ext_remove():
    d = _json_body()
    if d is None:
        return jsonify({"ok": False, "error": "需要 application/json"}), 415
    ok, msg = extsensors.remove(str(d.get("id") or ""))
    return jsonify({"ok": ok, "error": None if ok else msg,
                    "sensors": extsensors.listing()})


@bp.route("/api/ext/key", methods=["POST"])
def api_ext_key():
    return jsonify({"ok": True, "key": extsensors.reset_key(),
                    "template": extsensors.template()})
