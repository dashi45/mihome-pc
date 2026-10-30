# -*- coding: utf-8 -*-
"""事件流与推送（把事件送到手机上）。"""
from flask import Blueprint

bp = Blueprint('events', __name__)

from flask import jsonify, request

import evtlog
import notify
from services.guard import _json_body


# ---------- 事件流 ----------
# 这个面板别的卡片都是快照，只有这里记得"发生过什么"。
@bp.route("/api/evt")
def api_evt():
    """?since=<id> 增量拉新 / ?before=<id> 往前翻 / 不带参 = 最近 N 条"""
    since = request.args.get("since", type=int) or 0
    before = request.args.get("before", type=int) or 0
    limit = request.args.get("limit", type=int) or 60
    kinds = [k for k in (request.args.get("kind") or "").split(",") if k] or None
    items = evtlog.query(since_id=since, limit=limit,
                         before_id=before, kinds=kinds)
    return jsonify({
        "ok": True,
        "events": items,
        "total": evtlog.total(),
        # 前端拿它当游标，比自己算 max(id) 稳（空列表时不会退回去重拉）
        "cursor": items[-1]["id"] if items else since,
        "more": len(items) >= limit,
    })


# ---------- 推送（把事件送到手机上） ----------
@bp.route("/api/notify")
def api_notify():
    return jsonify({"ok": True, "config": notify.config()})


@bp.route("/api/notify/save", methods=["POST"])
def api_notify_save():
    d = _json_body()
    if d is None:
        return jsonify({"ok": False, "error": "需要 application/json"}), 415
    try:
        if "channels" in d:
            notify.set_channels(d.get("channels"))
        notify.set_options(d.get("min_level"), d.get("quiet"),
                           d.get("quiet_level"))
        return jsonify({"ok": True, "config": notify.config()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """立刻发一条。点测试的人是在验证通道，所以不看级别、不受免打扰限制。"""
    if _json_body() is None:
        return jsonify({"ok": False, "error": "需要 application/json"}), 415
    try:
        return jsonify(notify.test())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/evt/digest")
def api_evt_digest():
    hours = request.args.get("hours", type=int) or 24
    return jsonify({"ok": True, "digest": evtlog.digest(max(1, min(720, hours)))})
