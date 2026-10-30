# -*- coding: utf-8 -*-
"""定时开关：规则的增删改查。"""
from flask import Blueprint

bp = Blueprint('sched', __name__)


from flask import jsonify

import scheduler
from services.guard import _json_body


@bp.route("/api/sched")
def api_sched():
    """定时开关规则列表 + 下次触发预览。"""
    rules = scheduler.all_rules()
    return jsonify({
        "ok": True,
        "rules": rules,
        "next": [{"time": x["time"], "rule": x["rule"]} for x in scheduler.next_runs(8)],
    })


@bp.route("/api/sched/save", methods=["POST"])
def api_sched_save():
    """新增或修改一条规则。

    body: {id?, did, name, time:"HH:MM", siid, piid, value, enabled?}
    传了 id 就是修改，不传就是新增。
    """
    data = _json_body()
    if data is None:
        return jsonify({"ok": False, "error": "请求必须是 application/json"}), 415
    time_s = str(data.get("time") or "")
    # 格式 + 范围都要查：光看格式的话 "25:99" 会被放进去，
    # 而它永远匹配不到任何一分钟，等于一条静默失效的规则。
    if (len(time_s) != 5 or time_s[2] != ":"
            or not time_s.replace(":", "").isdigit()
            or not (0 <= int(time_s[:2]) <= 23)
            or not (0 <= int(time_s[3:]) <= 59)):
        return jsonify({"ok": False, "error": "时间格式应为 HH:MM（00:00 ~ 23:59）"}), 400
    if not data.get("did"):
        return jsonify({"ok": False, "error": "缺少设备"}), 400
    try:
        siid = int(data.get("siid"))
        piid = int(data.get("piid"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "缺少属性位置（siid/piid）"}), 400
    # siid/piid 是 0 的话写不进任何设备，等于一条静默失效的规则
    if siid <= 0 or piid <= 0:
        return jsonify({"ok": False, "error": "属性位置不合法"}), 400
    payload = {
        "did": str(data.get("did")),
        "name": str(data.get("name") or "")[:40],
        "time": time_s,
        "siid": siid,
        "piid": piid,
        "value": data.get("value"),
        "enabled": bool(data.get("enabled", True)),
    }
    if data.get("id"):
        rule = scheduler.update(str(data["id"]), payload)
        if not rule:
            return jsonify({"ok": False, "error": "规则不存在"}), 404
    else:
        rule = scheduler.add(payload)
    return jsonify({"ok": True, "rule": rule})


@bp.route("/api/sched/delete", methods=["POST"])
def api_sched_delete():
    data = _json_body()
    if data is None:
        return jsonify({"ok": False, "error": "请求必须是 application/json"}), 415
    rid = str(data.get("id") or "")
    if not rid:
        return jsonify({"ok": False, "error": "缺少规则 id"}), 400
    return jsonify({"ok": scheduler.remove(rid)})
