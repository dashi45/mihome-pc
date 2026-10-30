# -*- coding: utf-8 -*-
"""账号：状态、登录、验证码、退出、从备份还原登录态。"""
from flask import Blueprint

bp = Blueprint('auth', __name__)

import os
import threading
import time

from flask import jsonify

import config
from services.guard import _json_body
from services.cloud import _mark_ready
from services.state import S
from services.cloud import _T0
from services.live import _BATCH_STAT
from services.cloud import _NG
from services.cloud import _NG_BRK
from services.cloud import _http_stats
from services.cloud import _METRICS
from services.state import _LOGIN_LOCK
from services.tokens import restore_token_from_backup
from services.cloud import call
from services.devices import _do_restore
from services.tokens import backup_token
from services.devices import _save_dev_cache
from services.devices import prefetch_specs
from services.devices import _login_worker
from services.qrlogin import begin as qr_begin
from services.qrlogin import cancel as qr_cancel
from services.qrlogin import snapshot as qr_status


@bp.route("/api/status")
def api_status():
    _mark_ready()
    return jsonify({
        "status": S["status"],
        "error": S["error"],
        "otp_method": S["otp_method"],
        "has_token": os.path.isfile(config.TOKEN_PATH),
        "device_count": len(S["devices"]),
        # 前端靠这两个字段判断"该继续等"还是"真的需要重新登录"
        "restoring": S["status"] == "logging_in",
        "restore_failed": S["restore_failed"],
        "restore_tries": S["restore_tries"],
        # 启动秒开：前端拿这两个字段决定"先显示缓存设备"还是"显示空列表"
        "from_cache": S["from_cache"],
        "has_cached_devices": bool(S["cached_devices"]),
        # 上一轮属性刷新的实际开销：设备数 / 属性数 / 云端请求次数 / 耗时。
        # 逐台读时 requests 会等于设备数；批量读之后应该是个小个位数。
        "batch": dict(_BATCH_STAT),
        # 云端链路健康度：连续失败几次、最后一次是哪类错误、熔断开没开。
        # 界面上"设备不刷新"有三种完全不同的原因（断网 / 米家云抖 / 登录失效），
        # 光看界面分不出来，看这里能分。
        "health": _NG.snapshot(),
        "breaker": _NG_BRK.snapshot(),
        # 连接池开销：requests 涨、reconnects 不跟着涨 = 复用生效了。
        # 两者接近 1:1 说明每次都在重连（池化没起作用或一直在被服务端关）。
        "http": _http_stats(),
        "metrics": dict(_METRICS, uptime=round(time.time() - _T0, 1)),
        # 扫码登录的独立状态：等待扫码时 S["status"] 仍是 idle，
        # 不能靠它判断，前端轮询 /api/qr/status 或本字段。
        "qr": qr_status(),
    })


@bp.route("/api/restore", methods=["POST"])
def api_restore():
    if S["status"] in ("logging_in", "need_otp"):
        return jsonify({"ok": False, "error": "正在登录中", "pending": True})
    if not _LOGIN_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "正在登录中", "pending": True})
    try:
        restore_token_from_backup()
        S["status"] = "logging_in"
        account, service, devices, homes = call(_do_restore(), timeout=120)
        S["account"], S["service"] = account, service
        S["devices"], S["homes"] = devices, homes
        S["status"] = "ready"
        S["restore_failed"] = False
        S["from_cache"] = False
        backup_token()
        _save_dev_cache()
        threading.Thread(target=prefetch_specs, daemon=True).start()
        return jsonify({"ok": True})
    except Exception as e:
        S["status"] = "idle"
        S["error"] = None
        S["restore_failed"] = True
        return jsonify({"ok": False, "error": str(e)})
    finally:
        _LOGIN_LOCK.release()


@bp.route("/api/qr/start", methods=["POST"])
def api_qr_start():
    r = qr_begin()
    return jsonify(r), (200 if r.get("ok") else 503)


@bp.route("/api/qr/status")
def api_qr_status():
    return jsonify(dict(qr_status(), ok=True))


@bp.route("/api/qr/cancel", methods=["POST"])
def api_qr_cancel():
    return jsonify(qr_cancel())


@bp.route("/api/login", methods=["POST"])
def api_login():
    data = _json_body()
    if data is None:
        return jsonify({"ok": False, "error": "请求必须是 application/json"}), 415
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"ok": False, "error": "请输入小米账号和密码"}), 400
    if S["status"] in ("logging_in", "need_otp"):
        return jsonify({"ok": False, "error": "登录进行中，请稍候"}), 409
    # 用户改用密码：让还在等待的二维码作废，避免手机端确认后
    # 后台又悄悄把另一套凭据写进 .mi.token。
    if qr_status().get("state") in ("starting", "waiting", "scanned"):
        qr_cancel()
    threading.Thread(target=_login_worker, args=(username, password), daemon=True).start()
    return jsonify({"ok": True})


@bp.route("/api/otp", methods=["POST"])
def api_otp():
    data = _json_body()
    if data is None:
        return jsonify({"ok": False, "error": "请求必须是 application/json"}), 415
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "验证码为空"}), 400
    S["otp_code"] = code
    if S["status"] == "need_otp":
        S["status"] = "logging_in"
    return jsonify({"ok": True})


@bp.route("/api/logout", methods=["POST"])
def api_logout():
    for p in (config.TOKEN_PATH, config.TOKEN_BAK):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass
    S.update(status="idle", account=None, service=None, devices=[], homes=[],
             error=None, restore_failed=False, restore_tries=0)
    return jsonify({"ok": True})
