# -*- coding: utf-8 -*-
"""请求守卫与响应头：只允许本机访问 + 前端永不缓存。

这是整个程序的安全边界：控制权挂在 127.0.0.1 上，必须挡住 DNS rebinding
和 CSRF。细节见各函数注释（那些注释是踩过坑写下来的，别删）。

以前挂在 `@app.before_request` 上，现在由 app.py 显式注册 —— 拆包之后
装饰器写在模块里就失效了，注册必须回到创建 app 的地方。
"""
from urllib.parse import urlsplit

from flask import Response, jsonify, request

import config
import os


# ---------- 本地访问守卫 ----------
# 这个程序把整屋设备的控制权挂在 127.0.0.1 上，所以要把"只有本机能用"守住。
# 挡的是两类真实攻击：
#   1. DNS rebinding —— 攻击者的域名解析到 127.0.0.1，浏览器认为"同源"，
#      于是能直接读接口、发写请求。挡法：Host 必须是本机地址 + 本程序端口。
#   2. CSRF —— 你浏览器里开着的别的网页，偷偷往 127.0.0.1 发写请求。
#      挡法：Origin / Referer / Sec-Fetch-Site 必须指向本机、且是同一个端口。
#
# 两个容易写错的点（都是实测能绕过去的）：
#   a) 只比主机名、不比端口：那本机任意端口上跑的网页都算可信（各种本地
#      dev server、别的小工具、甚至某个被挂马的本地页面），等于把门开给它们。
#   b) 用字符串前缀判断 127.0.0.1：http://127.0.0.1.evil.com 这种以
#      127.0.0.1 开头，前缀匹配当场被骗。必须真解析 URL、逐项比对。
config.PORT = int(os.environ.get("MIHOME_PORT", "8765"))
config.LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _origin_ok(u):
    """这个 Origin/Referer 是不是本程序自己（协议 + 主机 + 端口全对上）。"""
    if not u or u == "null":
        # null 来自 file:// 页面、sandbox iframe、跨源重定向后的请求，
        # 一律不认。
        return False
    try:
        sp = urlsplit(u)
        host = (sp.hostname or "").lower()
        port = sp.port
    except Exception:
        return False
    return sp.scheme == "http" and host in config.LOCAL_HOSTS and port == config.PORT


def _local_only_guard():
    try:
        hp = urlsplit(request.host_url)
        host_ok = (hp.hostname or "").lower() in config.LOCAL_HOSTS and hp.port == config.PORT
    except Exception:
        host_ok = False
    if not host_ok:
        return Response("forbidden host", status=403, mimetype="text/plain")

    # 外部传感器推数是唯一放行的写接口：ESP32 / 脚本 / 快捷指令不会带
    # Origin，也不可能有米家登录态，浏览器更不会替它们伪造这条请求（
    # 它用独立密钥鉴权，见 extsensors.push）。Host 校验仍然要做，
    # 挡住"把域名解析到 127.0.0.1 再打这个口"的 rebinding。
    if request.path.rstrip("/") == "/api/ext/push":
        return None

    site = (request.headers.get("Sec-Fetch-Site") or "").lower()
    if site and site not in ("same-origin", "none"):
        return Response("cross-site request blocked", status=403, mimetype="text/plain")

    origin = request.headers.get("Origin")
    if origin is not None and not _origin_ok(origin):
        return Response("cross-origin request blocked", status=403, mimetype="text/plain")

    referer = request.headers.get("Referer")
    if referer and not _origin_ok(referer):
        return Response("cross-origin request blocked", status=403, mimetype="text/plain")

    # 写请求必须带正向的"同源"证据：浏览器发同源请求时一定带 Origin
    # （fetch/XHR/表单 POST 都带），三条头全空的写请求只可能来自非浏览器。
    # 这是把"没有信号就当可信"翻成"没有信号就不许写"。
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin is None:
        if not _origin_ok(referer):
            return Response("missing same-origin signal", status=403,
                            mimetype="text/plain")
    return None


def _json_body():
    """读 JSON 请求体，只认 application/json；不是就返回 None（调用方回 415）。

    跨站表单 / <img> / sendBeacon 只能发 text/plain、form-urlencoded 这类
    "简单请求"，浏览器不预检、服务端照收 —— 这就是 CSRF 的门。强制
    application/json 之后，跨站请求必然先预检，而本程序不发任何 CORS 头，
    预检过不去，请求根本到不了业务代码。
    """
    if not request.is_json:
        return None
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ---------- 路由 ----------
def _no_static_cache(resp):
    """前端文件改完立刻生效：否则 WebView 会把旧的 js/css 缓存起来"""
    try:
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        resp.headers.pop("Expires", None)
        # 下面这几个是"守卫之外再兜一层"，任何一个口子漏了也不至于直接失守：
        # 禁 MIME 嗅探；只许同源 iframe（充电器页是同源内嵌）；
        # 跨站不许引用本程序的任何资源（含 /camhls 的摄像头分片，
        # 否则别的网页能拿 <video>/<img> 直接把画面嵌走）。
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        resp.headers["Referrer-Policy"] = "no-referrer"
    except Exception:
        pass
    return resp


def _unhandled(e):
    """兜底：任何未捕获异常都返回结构化 JSON，而不是让前端拿到空响应/崩溃。
    单线程时代的隐患，现在有线程池了仍然留着，保证接口永远有回话。"""
    try:
        code = getattr(e, "code", None)
        if isinstance(code, int) and 400 <= code < 600:
            return jsonify({"ok": False, "error": str(e)}), code
    except Exception:
        pass
    return jsonify({"ok": False, "error": "服务内部错误：%s" % e}), 500
