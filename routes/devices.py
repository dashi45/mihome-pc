# -*- coding: utf-8 -*-
"""设备：列表/刷新/规格/控制面板/红外/属性读写。"""
from flask import Blueprint

bp = Blueprint('devices', __name__)


from flask import jsonify

from services.guard import _json_body
from services.state import S
from services.devices import build_device_view, fetch_ir_keys
from services.cloud import call
from services.devices import SPECS
from services.cloud import PRIO_CONTROL
from services.devices import save_spec_cache
from services.devices import is_ir_device
from services.devices import get_spec
from panel import build_panel
from services.cloud import cloud
from services.cloud import _note_cloud_error
import netguard
from services.devices import _LIVE_CACHE


@bp.route("/api/devices")
def api_devices():
    # 未登录但有缓存时：先把缓存设备给出去，界面不用空着等
    if S["status"] != "ready":
        if S["cached_devices"]:
            return jsonify({
                "ok": True,
                "devices": S["cached_devices"],
                "from_cache": True,
                "status": S["status"],
            })
        return jsonify({"ok": False, "error": "未登录", "status": S["status"]}), 401
    return jsonify({"ok": True, "devices": build_device_view(S["devices"], S["homes"]),
                    "from_cache": False})


@bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    try:
        async def _r():
            return await S["service"].device_list("full"), await S["service"].home_list()
        devices, homes = call(_r(), timeout=60)
        S["devices"], S["homes"] = devices, homes
        return jsonify({"ok": True, "devices": build_device_view(devices, homes)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/spec/<path:model>")
def api_spec(model):
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    if model in SPECS:
        return jsonify({"ok": True, "spec": SPECS[model], "cached": True})
    try:
        raw = call(S["service"].miot_spec(model), timeout=30, prio=PRIO_CONTROL)
        SPECS[model] = raw
        save_spec_cache(SPECS)
        return jsonify({"ok": True, "spec": raw, "cached": False})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/panel/<path:did>")
def api_panel(did):
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    dev = next((d for d in S["devices"] if str(d.get("did")) == str(did)), None)
    if not dev:
        return jsonify({"ok": False, "error": "设备不存在"}), 404

    # 红外遥控：没有 MIoT 规范，走红外按键接口
    if is_ir_device(dev):
        return jsonify({
            "ok": True, "type": "ir",
            "keys": fetch_ir_keys(str(did)),
            "online": bool(dev.get("isOnline")),
            "controls": [], "camera": None,
        })

    model = dev.get("model") or ""
    spec = get_spec(model)
    if not spec:
        return jsonify({"ok": False, "error": "这个型号没有公开的设备规范"})
    controls, camera = build_panel(spec)
    if controls and dev.get("isOnline"):
        try:
            vals = call(S["service"].miot_get_props(
                str(did), [(c["siid"], c["piid"]) for c in controls]), timeout=25)
            for c, v in zip(controls, vals):
                c["value"] = v
        except Exception:
            pass
    return jsonify({"ok": True, "type": "miot", "controls": controls, "camera": camera,
                    "online": bool(dev.get("isOnline"))})


@bp.route("/api/ir/keys/<path:did>")
def api_ir_keys(did):
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    return jsonify({"ok": True, "keys": fetch_ir_keys(did, force=True)})


@bp.route("/api/ir/press", methods=["POST"])
def api_ir_press():
    """按下一个红外按键：云端让物理红外发射器发码"""
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    data = _json_body()
    if data is None:
        return jsonify({"ok": False, "error": "请求必须是 application/json"}), 415
    did = str(data.get("did") or "")
    key_id = data.get("key_id")
    if not did or key_id is None:
        return jsonify({"ok": False, "error": "缺少参数"}), 400
    try:
        call(S["service"].miio_request(
            "/v2/irdevice/controller/key/click",
            {"did": did, "key_id": int(key_id)}), timeout=25)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/getprops", methods=["POST"])
def api_getprops():
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    data = _json_body()
    if data is None:
        return jsonify({"ok": False, "error": "请求必须是 application/json"}), 415
    did = str(data.get("did"))
    props = data.get("props") or []
    pairs = [(int(p[0]), int(p[1])) for p in props]
    try:
        # 读属性是幂等的，抖一次自动重试，别让详情面板直接报错
        vals = cloud(lambda: S["service"].miot_get_props(did, pairs),
                     timeout=30, prio=PRIO_CONTROL, what="getprops")
        return jsonify({"ok": True, "values": vals})
    except Exception as e:
        _note_cloud_error(e, "getprops")
        return jsonify({"ok": False, "error": netguard.summarize(e)}), 500


@bp.route("/api/setprop", methods=["POST"])
def api_setprop():
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    data = _json_body()
    if data is None:
        return jsonify({"ok": False, "error": "请求必须是 application/json"}), 415
    did = str(data.get("did"))
    try:
        siid, piid = int(data["siid"]), int(data["piid"])
        # set_prop 幂等（把同一个值写两次结果一样），所以允许重试一次。
        # 开关类设备最怕的不是失败，是"点了没反应"——重试能救回大半抖动。
        code = cloud(lambda: S["service"].miot_set_prop(did, (siid, piid), data["value"]),
                     timeout=30, prio=PRIO_CONTROL, attempts=2, what="setprop")
        _LIVE_CACHE["data"] = None
        return jsonify({"ok": code == 0, "code": code})
    except Exception as e:
        _note_cloud_error(e, "setprop")
        return jsonify({"ok": False, "error": netguard.summarize(e)}), 500


@bp.route("/api/action", methods=["POST"])
def api_action():
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    data = _json_body()
    if data is None:
        return jsonify({"ok": False, "error": "请求必须是 application/json"}), 415
    did = str(data.get("did"))
    try:
        # action 不重试：这是"切换""摆头"这类一次性动作，重发一次就是执行两次。
        # 宁可报错让用户再点一下，也不能替他做两遍。
        code = call(S["service"].miot_action(did, (int(data["siid"]), int(data["aiid"])),
                                             data.get("args") or []),
                    timeout=30, prio=PRIO_CONTROL)
        _LIVE_CACHE["data"] = None
        return jsonify({"ok": code == 0, "code": code})
    except Exception as e:
        _note_cloud_error(e, "action")
        return jsonify({"ok": False, "error": netguard.summarize(e)}), 500
