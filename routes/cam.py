# -*- coding: utf-8 -*-
"""摄像头：取流地址、HLS 分片、诊断、云台。"""
from flask import Blueprint

bp = Blueprint('cam', __name__)

import json
import os
import time

from flask import Response, jsonify, request

import camhls
import config
from services.state import S
from services.state import CAM_MAP
from services.cloud import _METRICS
import socket
from services.state import FLIP_MAP
from services.cloud import call
from services.cloud import PRIO_CONTROL


@bp.route("/api/cam/<path:did>")
def api_cam(did):
    """摄像头实时画面：camhls 把 go2rtc 的 H264 流转封装成 HLS 分片。

    分工（2026-10-26 调整）：go2rtc 的 camera_h264 负责转码，camhls 只做
    `-c copy` 切片。不再用 go2rtc 自带的 /api/stream.m3u8 —— 一是它的 HTTP
    API 整个关着（会吐账号 token），二是它自带的 HLS 在 GOP 不齐时会吐空片。
    本模块的 independent_segments + 1 秒切点就是为这个存在的。
    """
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401

    stream = CAM_MAP.get(str(did))
    if not stream:
        return jsonify({"ok": False, "error": "这台摄像头还没有配置实时画面"})

    if not _go2rtc_alive():
        return jsonify({"ok": False, "error": "转流服务没在运行，请用「启动米家.bat」重新启动",
                        "hint": "go2rtc"})

    try:
        wait = float(os.environ.get("CAM_WAIT", "22"))
    except ValueError:
        wait = 22.0

    t_cam = time.time()
    ok, info = camhls.ensure(str(did), "%s/%s" % (config.RTSP_BASE, stream), wait=wait)
    cam_ms = int((time.time() - t_cam) * 1000)
    if ok:
        _METRICS["cam_first_ms"] = cam_ms
        _METRICS["cam_ok"] += 1
    else:
        _METRICS["cam_fail"] += 1
    if not ok:
        return jsonify({"ok": False, "error": _cam_error_text(info), "detail": info}), 503

    dev = next((d for d in S["devices"] if str(d.get("did")) == str(did)), None)
    ptz_ok, ptz_why = _ptz_ready(dev) if dev else (False, "设备不在列表里")

    return jsonify({
        "ok": True,
        # 同源相对路径：由 Flask 提供，不跨域、也不受 go2rtc API 端口变化影响
        "hls": "/camhls/%s/index.m3u8" % did,
        "stream": stream,
        "ptz": ptz_ok,
        "ptz_reason": ptz_why,
        # 画面朝向：前端只用来说明"方向盘已按画面方向校正"，方向换算在后端做
        "flip": _flip_of(did),
        "warm": {"segments": info.get("segments"), "uptime": info.get("uptime")},
    })


@bp.route("/camhls/<path:did>/<name>")
def camhls_file(did, name):
    """HLS 播放列表与分片（同源）。

    播放列表必须 no-store：hls.js 每次重新拉都要拿到最新的 MEDIA-SEQUENCE，
    一旦被 WebView 缓存住，播放就会卡在固定的几个分片上不动。
    """
    path = camhls.file_path(str(did), name)
    if not path:
        return Response("not found", status=404, mimetype="text/plain")
    camhls.touch(str(did))
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return Response("not found", status=404, mimetype="text/plain")
    resp = Response(data, mimetype=("application/vnd.apple.mpegurl"
                                    if name.endswith(".m3u8") else "video/mp2t"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    # 这里故意不发 Access-Control-Allow-Origin：
    # 分片是同源播放，本来不需要 CORS；一旦放开，本机任意网页都能跨域
    # 把摄像头画面下载走。保持同源，等于关掉这条路。
    return resp


@bp.route("/api/cam-diag")
def api_cam_diag():
    """摄像头自检：出问题时先看这里，不用猜。

    未登录不给看：里面有设备 did、流名、ffmpeg 命令行和日志尾巴，
    对排查够用了，没必要对任何路过的人开放。
    """
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    out = {
        "rtsp": config.RTSP_BASE,
        "rtsp_port_open": _port_open(8554),
        # go2rtc 的 HTTP API 已在 go2rtc.yaml 里整个关掉（listen 为空）。
        # 它没有鉴权、也不校验 Host，是 token 唯一的外泄出口（/api/config），
        # 而本程序取流只走 RTSP，根本不需要它。
        "go2rtc_api": "disabled",
        "map": CAM_MAP,
        "ffmpeg": camhls.FFMPEG,
        "ffmpeg_exists": os.path.isfile(camhls.FFMPEG),
        "sessions": {str(d): camhls.state(str(d)) for d in CAM_MAP},
    }
    return jsonify(out)


def _go2rtc_alive(timeout=2.5):
    """转流服务活着 = RTSP 8554 在听（HTTP API 已关闭，不能再探 1984）。"""
    return _port_open(8554, timeout=timeout)


def _port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _cam_error_text(info):
    """把 ffmpeg / go2rtc 的原始报错翻译成能照着做的提示。"""
    logs = " ".join(info.get("log") or [])
    low = logs.lower()
    if info.get("error"):
        return info["error"]
    if not _port_open(8554):
        return ("转流服务的 RTSP 端口 8554 没起来：可能有另一个 go2rtc 实例占着 1984 "
                "却没占到 8554（go2rtc 会把 rtsp 模块整个禁用）。先结束所有 "
                "go2rtc-tk.exe 再重新启动。")
    if "connection refused" in low or "connection reset" in low:
        return "连不上转流服务（rtsp 8554）。先重启「启动米家.bat」。"
    if "401" in low or "unauthorized" in low or "auth" in low:
        return "小米云端鉴权失败：go2rtc.yaml 里的账号 token 可能已过期，需要在米家重新抓一次。"
    if "timed out" in low or "timeout" in low:
        return "取流超时：摄像头可能休眠、被其它 App 占用，或与电脑不在同一网络。"
    if "no route to host" in low or "network is unreachable" in low:
        return "连不上摄像头的局域网地址：电脑和摄像头是不是同一个网络？"
    if "404" in low or "not found" in low or "no such stream" in low:
        return ("go2rtc 里没有这条流：检查 cam.json 的流名和 go2rtc.yaml 的 streams "
                "是否对上（现在应该是 camera_h264）。")
    if "conversion failed" in low or "error while opening" in low or "unsupported" in low:
        return ("转封装失败：上游给的可能不是 H.264。检查 go2rtc 的 camera_h264 "
                "是否真的转成了 H264（见 go2rtc\\go2rtc.log）。")
    tail = (info.get("log") or [""])[-1:]
    return "画面没能起来，ffmpeg 报错：%s" % (" / ".join(tail) or "原因未知")


# 注：原来这里有个 _go2rtc_get()，用来直接访问 go2rtc 的 HTTP API
# （/api/streams 等）。那个 API 已在 go2rtc.yaml 里整个关掉 —— 它没有鉴权、
# 也不校验 Host，是账号 token 唯一的外泄出口。取流走 RTSP，用不到它。


# ---------------- 云台控制（云端 miIO 转发）----------------
# 云台方向不在 MIoT 规格里（029a02 的 spec 只有 camera-control / p2p-stream），
# 所以 miot_action 根本调不到它。米家 App 异地也能控，是因为它压根没走局域网，
# 而是走云端 miIO 转发：
#     POST /home/rpc/{did}   {"method": "set_motor", "params": {"operation": n}}
# 小米云通过设备主动维系的长连接把指令下发，和"是否同一局域网"无关。
#
# 实测（小米智能摄像机 云台版2K / chuangmi.camera.029a02 / 固件 4.5.3_0460）：
#   operation  1=右  2=左  3=上  4=下
#   （注意与老款 ipc019 的 1左/2右 相反，方向是按实测画面位移定的）
#   参数必须是对象 {"operation": n}；写成 [{"operation": n}] 设备回 "operation is NULL"
#   没有"停止"指令：传 0 回 "unknown operation"，每次调用转固定一小步
#   转到限位回 code=-4000，信息里带 up/down/left/right limit
#
# ---- 倒挂安装时方向会反，必须做坐标换算 ----
# 上面这套 1/2/3/4 是【设备机身坐标】：机身的"上"就发 3，不管机器怎么装。
# 这台摄像头是倒挂的（设备侧 image-rollover=180 把画面转正了），于是：
#     机身"上" = 房间里的"下"      → 按"上"键，画面反而往下看
#     机身"右" = 房间里的"左"      → 左右同理
# 所以不能把机身坐标直接给前端用。这里统一改成【画面坐标】下指令：
#     前端只发 dir=up/down/left/right，后端按该设备的画面朝向换算成机身 operation。
# 这样换安装方式只需改 cam.json 的 flip（或让程序读设备 image-rollover），
# 前端一行都不用动，也不会出现"某个型号方向反了"的散装 bug。

_PTZ_OPS = (1, 2, 3, 4)
_PTZ_OP_NAME = {1: "右", 2: "左", 3: "上", 4: "下"}
# 画面方向 -> 设备机身 operation（未翻转时的基准映射，按实测位移定的）
_PTZ_DIR_OP = {"up": 3, "down": 4, "left": 2, "right": 1}
_PTZ_OP_DIR = {v: k for k, v in _PTZ_DIR_OP.items()}

_FLIP_MODES = ("none", "vertical", "horizontal", "rotate180")
_FLIP_CACHE = {}        # did -> (读取时间, 朝向)
_FLIP_TTL = 300         # 设备属性缓存 5 分钟，避免每次点方向都去云端读一遍


def _flip_of(did):
    """这台摄像头画面相对机身转了多少：none / vertical / horizontal / rotate180。

    优先用 cam.json 的 flip（人工确认过，最可靠）；没配就去读设备的
    image-rollover（siid=2, piid=2），读不到按 none 处理。
    """
    did = str(did)
    ov = FLIP_MAP.get(did)
    if ov in _FLIP_MODES:
        return ov

    ts, mode = _FLIP_CACHE.get(did, (0.0, None))
    if mode and time.time() - ts < _FLIP_TTL:
        return mode

    mode = "none"
    if S["status"] == "ready" and S["service"] is not None:
        try:
            vals = call(S["service"].miot_get_props(did, [(2, 2)]), timeout=6)
            deg = int(float(vals[0])) % 360 if vals and vals[0] is not None else 0
            mode = "rotate180" if deg == 180 else "none"
        except Exception:
            mode = "none"
    _FLIP_CACHE[did] = (time.time(), mode)
    return mode


def _ptz_op_for(did, direction):
    """把"用户看到的画面方向"翻译成"设备机身 operation"。

    rotate180（倒挂最常见）：上下、左右都要反过来；
    vertical/horizontal：只反一个轴。
    """
    mode = _flip_of(did)
    d = direction
    if mode in ("vertical", "rotate180") and d in ("up", "down"):
        d = "down" if d == "up" else "up"
    if mode in ("horizontal", "rotate180") and d in ("left", "right"):
        d = "right" if d == "left" else "left"
    return _PTZ_DIR_OP[d]


def _ptz_ready(dev):
    """云台能不能控：只要设备在列表里、did 有效，云端转发就能用。

    注意这里不再检查 localip / token / 同网段 —— 那条路本来就走不通，
    也不是米家 App 的做法。
    """
    if not dev:
        return False, "设备不在列表里"
    if not dev.get("did"):
        return False, "缺少设备 did"
    return True, None


@bp.route("/api/camera/start/<path:did>", methods=["POST"])
def api_camera_start(did):
    """重连用：把这一路 HLS 会话整个重开一次（前端 hls.js 连续失败时调用）。

    注意这里不再去调小米云端的 start-hls-stream —— 那条链路是给"云存回放"
    用的，和本地转流互不相干，调它反而会多发一次云端请求、拖慢重连。
    """
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    stream = CAM_MAP.get(str(did))
    if not stream:
        return jsonify({"ok": False, "error": "这台摄像头还没有配置实时画面"})
    if not _go2rtc_alive():
        return jsonify({"ok": False, "error": "转流服务没在运行"})

    camhls.stop(str(did))
    ok, info = camhls.ensure(str(did), "%s/%s" % (config.RTSP_BASE, stream), wait=12.0)
    if not ok:
        return jsonify({"ok": False, "error": _cam_error_text(info), "detail": info})
    return jsonify({"ok": True, "hls": "/camhls/%s/index.m3u8" % did})


@bp.route("/api/camera/motor/<path:did>", methods=["POST"])
def api_camera_motor(did):
    """云台方向控制：云端 miIO 转发 set_motor（异地可用，不要求同一局域网）。

    参数用【画面方向】：{"dir": "up" | "down" | "left" | "right"}。
    后端会按 cam.json 的 flip 换算成设备 operation，所以倒挂/镜像安装时
    "上"键在画面上就是"上"。旧客户端仍可传 {"operation": 1-4}，按设备坐标原样下发。

    设备是步进式的：每调一次转固定一小步，没有"停止"指令；到限位回 code=-4000。
    """
    if S["status"] != "ready":
        return jsonify({"ok": False, "error": "未登录"}), 401
    dev = next((d for d in S["devices"] if str(d.get("did")) == str(did)), None)
    ready, why = _ptz_ready(dev)
    if not ready:
        return jsonify({"ok": False, "error": why, "ptz": False})

    # 只接受 JSON：跨站表单/图片标签发不出 application/json（会先预检，预检必失败），
    # 这样浏览器里的第三方页面就没法偷偷转你的摄像头。
    if not request.is_json:
        return jsonify({"ok": False, "error": "请求必须是 application/json"}), 415

    data = request.get_json(silent=True) or {}
    direction = str(data.get("dir") or "").strip().lower()
    if direction in _PTZ_DIR_OP:
        op = _ptz_op_for(did, direction)
    else:
        # 兼容旧调用：operation 是设备坐标，不做翻转
        try:
            op = int(data.get("operation"))
        except Exception:
            return jsonify({"ok": False, "error": "dir 需要 up / down / left / right"})
        if op not in _PTZ_OPS:
            return jsonify({"ok": False, "error": "operation 只接受 1右 2左 3上 4下"})
        direction = _PTZ_OP_DIR[op]

    try:
        # 云端往返实测约 0.35s，超时给 6s 就够：卡住时尽快把错误交回界面，
        # 别让按钮傻等 12 秒（12s 是历史值，纯属浪费时间）
        res = call(S["service"].home_request(str(did), "set_motor", {"operation": op}), prio=PRIO_CONTROL,
                   timeout=6)
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "limit" in low:
            side = ("上" if "up limit" in low else "下" if "down limit" in low
                    else "左" if "left limit" in low else "右" if "right limit" in low else "")
            return jsonify({"ok": False, "limit": True,
                            "error": ("已经转到%s极限了" % side) if side else "云台已到限位"})
        if "unknown operation" in low:
            return jsonify({"ok": False, "error": "这台设备不支持云台转动"})
        if "-4000" in msg:
            return jsonify({"ok": False, "limit": True, "error": "云台已到限位"})
        return jsonify({"ok": False, "error": "云台指令没发出去：%s" % msg[:120]})

    txt = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
    if "ok" in txt.lower():
        # 这里原来会把整个 /api/live 缓存清空 —— 一次点击就让 13 台设备全部重读云端，
        # 既拖慢下一条云台指令，也白白多打一堆云端请求。云台位置不在 live 属性里，
        # 根本不需要失效缓存，直接去掉。
        return jsonify({"ok": True, "dir": direction, "op": op,
                        "flip": _flip_of(did)})
    return jsonify({"ok": False, "error": "设备返回了意外结果：%s" % txt[:120]})
