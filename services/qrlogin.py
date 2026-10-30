# -*- coding: utf-8 -*-
"""小米账号扫码登录。

密码登录有两个现实问题：一是账号被风控时 device_list 会返回
`code=-127 client withdraw`，用户只能干等；二是密码要经过本程序，
即使只发小米官方服务器，用户也不一定愿意。扫码登录把这两件事都绕开：
二维码由小米账号服务生成，手机确认后回调只带回 serviceToken。

流程（与 mijiaAPI 的 QRlogin 一致）：

    serviceLogin?_json=true&sid=xiaomiio
        -> 拿到 location 里的 qs/callback/_sign
    longPolling/loginUrl?...
        -> 拿到 qr（直接是 PNG）和 lp（长轮询地址）
    lp 阻塞等待扫码
        -> 返回 location + ssecurity/passToken/userId/cUserId
    GET location
        -> Set-Cookie: serviceToken

最后把结果写成 miservice 的 token 格式：

    {"deviceId": ..., "userId": ..., "passToken": ...,
     "xiaomiio": [ssecurity, serviceToken]}

这样后续的 MiIOService / device_list / 控制逻辑一行都不用改。
"""
import gzip
import http.cookiejar
import json
import random
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import creds
import logx
from services.cloud import PRIO_CONTROL, call
from services.devices import _do_restore, _save_dev_cache, prefetch_specs
from services.state import S, _LOGIN_LOCK
from services.tokens import backup_token

log = logx.get(__name__)

_LOGIN_URL = "https://account.xiaomi.com/longPolling/loginUrl"
_SERVICE_LOGIN = "https://account.xiaomi.com/pass/serviceLogin"
_PREFIX = "&&&START&&&"
_LOCALE = "zh_CN"
_SID = "xiaomiio"

# 和 mijiaAPI 保持同样的移动端 UA 形状。服务端会按 UA 决定返回扫码页
# 还是密码页；这里必须像米家 App，不能像桌面浏览器。
_UA_TMPL = (
    "Android-15-11.0.701-Xiaomi-23046RP50C-OS2.0.212.0.VMYCNXM-"
    "{a}-CN-{b}-{c}-SmartHome-MI_APP_STORE-{a}|{d}|{e}-64"
)

_STATE = {
    "state": "idle",       # idle | starting | waiting | scanned | success | error
    "qr": None,
    "error": None,
    "expires": 0.0,
    "cancel": False,
    "thread": None,
    # 旧的长轮询可能在新二维码生成后才返回；generation 对不上就丢弃，
    # 否则过期会话会把新会话的凭据覆盖掉。
    "gen": 0,
}
_LOCK = threading.RLock()


def _rand(n):
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def _hex(n):
    return "".join(random.choices("0123456789abcdef", k=n))


def _ua():
    return _UA_TMPL.format(a=_rand(40).upper(), b=_rand(32).upper(),
                           c=_rand(32).upper(), d=_rand(40).upper(),
                           e=_hex(16))


def _now():
    return time.time()


def _snapshot_locked():
    left = int(_STATE["expires"] - _now()) if _STATE["expires"] else 0
    return {
        "state": _STATE["state"],
        "qr": _STATE["qr"],
        "error": _STATE["error"],
        "expires": _STATE["expires"],
        "remaining": max(0, left),
    }


def snapshot():
    with _LOCK:
        return _snapshot_locked()


def _set(**kw):
    with _LOCK:
        _STATE.update(kw)


def _cancelled(gen=None):
    with _LOCK:
        if gen is not None and gen != _STATE["gen"]:
            return True
        return bool(_STATE["cancel"])


class _Session:
    """一个带 Cookie 的小会话；长轮询要复用同一组 cookie。"""

    def __init__(self, device_id, pass_o, ua):
        self.device_id = device_id
        self.pass_o = pass_o
        self.ua = ua
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def _headers(self, cookie=None):
        h = {
            "User-Agent": self.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded",
            "Connection": "keep-alive",
        }
        if cookie:
            h["Cookie"] = cookie
        return h

    def get(self, url, timeout=30, cookie=None):
        req = urllib.request.Request(url, headers=self._headers(cookie))
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            raw = e.read()
            raise RuntimeError("小米账号接口 HTTP %s：%s"
                               % (e.code, raw[:180].decode("utf-8", "replace")))

    def get_json(self, url, timeout=30, cookie=None):
        raw = self.get(url, timeout=timeout, cookie=cookie)
        text = raw.decode("utf-8", "replace").strip()
        if text.startswith(_PREFIX):
            text = text[len(_PREFIX):]
        try:
            return json.loads(text)
        except Exception:
            raise RuntimeError("小米账号接口返回了非 JSON 内容：%s" % text[:180])

    def cookies(self):
        return {c.name: c.value for c in self.jar}


def _start_cookie(device_id, pass_o):
    return ("deviceId=%s; pass_o=%s; uLocale=%s; locale=%s"
            % (device_id, pass_o, _LOCALE, _LOCALE))


def _service_login(sess):
    """先走 serviceLogin，拿到二维码 URL 需要的 qs/callback/_sign。"""
    q = urllib.parse.urlencode({"_json": "true", "sid": _SID,
                                "_locale": _LOCALE})
    data = sess.get_json(_SERVICE_LOGIN + "?" + q, timeout=25,
                         cookie=_start_cookie(sess.device_id, sess.pass_o))
    loc = data.get("location")
    if not loc:
        raise RuntimeError("小米账号没有返回登录地址（code=%s）"
                           % data.get("code"))
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    return {k: v[0] for k, v in parsed.items()}, data


def _open_qr():
    sess = _Session(_rand(16).upper(), _hex(16), _ua())
    params, _ = _service_login(sess)
    params.update({
        "theme": "",
        "bizDeviceType": "",
        "_hasLogo": "false",
        "_qrsize": "240",
        "_dc": str(int(time.time() * 1000)),
    })
    url = _LOGIN_URL + "?" + urllib.parse.urlencode(params)
    data = sess.get_json(url, timeout=25,
                         cookie=_start_cookie(sess.device_id, sess.pass_o))
    if not data.get("qr") or not data.get("lp"):
        raise RuntimeError("小米账号没有返回二维码（code=%s，%s）"
                           % (data.get("code"), data.get("desc") or ""))
    return sess, data


def begin():
    """生成二维码并开始后台长轮询。返回 {ok, qr, state, remaining}."""
    with _LOCK:
        if _STATE["state"] == "starting":
            return {"ok": False, "error": "正在获取二维码，请稍候"}
        if _STATE["state"] in ("waiting", "scanned"):
            if _STATE["expires"] > _now():
                return dict(_snapshot_locked(), ok=True)
        if S["status"] in ("logging_in", "need_otp"):
            return {"ok": False, "error": "正在登录中，请稍候"}
        if S["status"] == "ready":
            return {"ok": False, "error": "已经登录，无需扫码"}
        gen = _STATE["gen"] + 1
        _STATE.update(state="starting", qr=None, error=None,
                      expires=0.0, cancel=False, gen=gen)

    try:
        sess, data = _open_qr()
    except Exception as e:
        log.warning("扫码登录：获取二维码失败：%s", e)
        _set(state="error", error=str(e), qr=None, expires=0.0)
        return {"ok": False, "error": str(e)}

    timeout = int(data.get("timeout") or 300)
    _set(state="waiting", qr=data["qr"], error=None,
         expires=_now() + timeout, cancel=False)
    t = threading.Thread(target=_wait_worker, args=(sess, data, gen),
                         daemon=True, name="qr-login")
    with _LOCK:
        _STATE["thread"] = t
    t.start()
    return dict(_snapshot_locked(), ok=True)


def cancel():
    """用户切回密码登录：让后台结果作废。"""
    with _LOCK:
        _STATE.update(state="idle", qr=None, error=None,
                      expires=0.0, cancel=True, gen=_STATE["gen"] + 1)
    return {"ok": True}


def _wait_worker(sess, data, gen):
    try:
        raw = sess.get(data["lp"], timeout=125)
        text = raw.decode("utf-8", "replace").strip()
        if text.startswith(_PREFIX):
            text = text[len(_PREFIX):]
        lp = json.loads(text)
        if lp.get("code") not in (0, "0", None):
            raise RuntimeError("扫码未完成：%s"
                               % (lp.get("desc") or lp.get("message")
                                  or lp.get("code")))
        if not lp.get("location"):
            raise RuntimeError("扫码结果缺少回调地址")
        if _cancelled(gen):
            return
        _set(state="scanned", error=None)

        # 回调地址会 Set-Cookie: serviceToken；必须用同一个 session。
        sess.get(lp["location"], timeout=30)
        cookies = sess.cookies()
        service_token = cookies.get("serviceToken")
        if not service_token:
            raise RuntimeError("小米没有返回 serviceToken")

        for key in ("ssecurity", "passToken", "userId"):
            if not lp.get(key):
                raise RuntimeError("扫码结果缺少 %s" % key)

        token = {
            "deviceId": sess.device_id,
            "userId": lp["userId"],
            "passToken": lp["passToken"],
            "cUserId": lp.get("cUserId") or "",
            "xiaomiio": [lp["ssecurity"], service_token],
        }

        if _cancelled(gen):
            return
        if not _LOGIN_LOCK.acquire(blocking=False):
            raise RuntimeError("已有登录/恢复在进行中，请稍候")
        try:
            if not creds.dump_json(config.TOKEN_PATH, token):
                raise RuntimeError("保存登录凭据失败")
            backup_token()
            S["status"] = "logging_in"
            S["error"] = None
            account, service, devices, homes = call(
                _do_restore(), timeout=120, prio=PRIO_CONTROL)
            S["account"], S["service"] = account, service
            S["devices"], S["homes"] = devices, homes
            S["status"] = "ready"
            S["restore_failed"] = False
            S["from_cache"] = False
            backup_token()
            _save_dev_cache()
            threading.Thread(target=prefetch_specs, daemon=True).start()
            if _cancelled(gen):
                return
            _set(state="success", error=None)
            log.info("扫码登录成功：%s 台设备", len(devices))
        except Exception:
            # token 已经过结构校验；恢复失败可能只是网络/云端抖动，
            # 保留它让下次启动还能自动恢复，不要逼用户重新扫码。
            raise
        finally:
            _LOGIN_LOCK.release()
    except Exception as e:
        if _cancelled(gen):
            return
        log.warning("扫码登录失败：%s", e)
        if S["status"] != "ready":
            S["status"] = "idle"
        S["error"] = None
        _set(state="error", error=str(e), qr=None, expires=0.0)
