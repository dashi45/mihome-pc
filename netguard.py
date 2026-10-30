# -*- coding: utf-8 -*-
"""云端调用的稳定性层：归因 + 重试 + 熔断 + 健康统计。

`app.call()` 已经解决了"等太久"和"超时杀不掉"两件事（见它的注释）。
这一层补另外三件：

  1. **重试** —— 一次 DNS/TLS/500 抖动不该直接变成界面上的一条红字。
     原来整条链路是"一枪打死"：4 秒一轮的轮询撞上一次网络抖动，
     用户看到的就是设备全部变灰，几十秒后才自己好。
  2. **熔断** —— 网络真断的时候，每 4 秒一轮的请求都去撞 25 秒超时，
     8 个 waitress 线程很快被占满，界面反而比断网前更卡。
     连续失败到阈值后直接快失败 + 冷却，冷却结束放一个探针出去试。
  3. **归因** —— 失败要说清楚是"没网""米家云在抖"还是"登录态失效了"。
     前两种该重试，第三种重试一万次也没用，必须让用户重新登录。

为什么只能靠字符串判类型：miservice 抛的全是 `Exception("Error {url}: {resp}")`
这种裸异常，没有 401/超时之类的类型可用。所以 `classify()` 必须被测试钉住 ——
判错两个方向都有害：把"该重试的"判成 AUTH，用户会被无缘无故踢去重新登录；
把"登录失效"判成 NET，就会在断网时疯狂重试，把限流招来。

模块刻意不 import app，避免循环依赖：调用方把 `submit(coro)` 传进来。
"""
import asyncio
import random
import socket
import threading
import time
import urllib.error

# ---------- 失败类型 ----------
NET = "net"          # 断网、DNS、TLS、连接被重置
TIMEOUT = "timeout"  # 请求发出去了但没回来
SERVER = "server"    # 米家云 5xx
RATE = "rate"        # 限流 429 / 请求过于频繁
AUTH = "auth"        # 登录态失效（重试无用，要重新登录）
UNKNOWN = "unknown"  # 认不出来：按"不重试"处理，宁可少打一次云端

RETRYABLE = (NET, TIMEOUT, SERVER, RATE)
# 只有这几类算"云端/网络不可用"，才推动熔断。
# AUTH 不推动熔断：登录失效时熔断只会让用户没法重新登录。
BREAKING = (NET, TIMEOUT, SERVER)

_TIMEOUT_HINTS = (
    "timed out", "timeout", "超时", "等待超时",
)
_NET_HINTS = (
    "connection reset", "connection refused", "connection aborted",
    "connectionreseterror", "connectionrefusederror",
    "getaddrinfo", "name or service not known", "temporary failure in name",
    "network is unreachable", "no route to host", "broken pipe",
    "remote end closed", "eof occurred", "urlopen error",
    "ssl", "certificate verify", "handshake",
)
_AUTH_HINTS = (
    "login auth failed", "login failed", "auth error", "auth err",
    "invalid session", "session expired", "passtoken", "pass token",
    "未登录", "需要重新登录", "登录已失效", "登录态",
)
_RATE_HINTS = ("429", "too many requests", "请求过于频繁", "频率限制", "rate limit")
_SERVER_HINTS = (" 500", " 502", " 503", " 504", "internal server error",
                 "bad gateway", "service unavailable", "gateway timeout")


def classify(exc):
    """把异常归到上面几类。异常文案是唯一线索，所以顺序和关键词都要稳。"""
    if exc is None:
        return UNKNOWN
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, socket.timeout)):
        return TIMEOUT
    if isinstance(exc, socket.gaierror):
        return NET
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return AUTH
        if exc.code == 429:
            return RATE
        if 500 <= exc.code < 600:
            return SERVER
        return UNKNOWN
    if isinstance(exc, urllib.error.URLError):
        return NET
    if isinstance(exc, (ConnectionError, ConnectionResetError, ConnectionAbortedError)):
        return NET

    t = ("%s %s" % (type(exc).__name__, exc)).lower()
    # AUTH 排最前：miservice 的 "Error url: Login failed: ..." 里带 url，
    # 先判状态码会被 " 502" 之类的偶然子串骗走。
    if any(h in t for h in _AUTH_HINTS):
        return AUTH
    for code in (" 401", " 403"):
        if code in t:
            return AUTH
    if any(h in t for h in _RATE_HINTS):
        return RATE
    if any(h in t for h in _SERVER_HINTS):
        return SERVER
    if any(h in t for h in _TIMEOUT_HINTS):
        return TIMEOUT
    if any(h in t for h in _NET_HINTS):
        return NET
    return UNKNOWN


def retryable(kind):
    return kind in RETRYABLE


def _delay(attempt, kind):
    """指数退避 + 抖动。抖动是必需的：多台设备/多路轮询同时失败时，
    整齐的 0.4s 会让它们在同一毫秒一起重打云端，等于自己制造尖峰。"""
    base = 0.4 if kind != RATE else 2.0
    d = min(base * (3 ** attempt), 6.0)
    return d * (0.75 + random.random() * 0.5)


class BreakerOpen(Exception):
    """熔断打开期间的快速失败。继承 Exception，调用方的 except 照常能接。"""

    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(
            "云端连续失败，已暂停请求 %.0f 秒（网络或米家云不可用，恢复后自动继续）"
            % max(0.0, retry_after))


class Breaker:
    """连续失败到阈值就开闸，冷却结束放一个探针；探针成功才关闸。

    单用户本地程序不需要更复杂的模型：半开状态只允许一个请求，是因为
    worker 池本来就只有 5 个，放一堆探针进去等于没熔断。
    """

    def __init__(self, threshold=4, cooldown=20.0):
        self.threshold = max(1, int(threshold))
        self.cooldown = float(cooldown)
        self._fails = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def allow(self):
        with self._lock:
            if self._open_until:
                left = self._open_until - time.monotonic()
                if left > 0:
                    return False
                # 冷却结束：进入半开，放行这一次
                self._open_until = 0.0
            return True

    def retry_after(self):
        with self._lock:
            return max(0.0, self._open_until - time.monotonic())

    def success(self):
        with self._lock:
            self._fails = 0
            self._open_until = 0.0

    def failure(self, kind):
        if kind not in BREAKING:
            return
        with self._lock:
            self._fails += 1
            if self._fails >= self.threshold:
                self._open_until = time.monotonic() + self.cooldown
                self._fails = 0

    @property
    def is_open(self):
        with self._lock:
            return self._open_until > 0

    def snapshot(self):
        with self._lock:
            return {"open": self._open_until > 0,
                    "retry_after": max(0.0, self._open_until - time.monotonic())}


class Health:
    """云端调用的健康账本。给 /api/status 用，也给日志用。

    没有这个的时候，"其实登录早失效了"和"米家云刚好抖了一下"在界面上
    长得一模一样 —— 都只是设备不刷新。有了账本，后者会自己恢复，
    前者会被 `app._maybe_self_heal` 抓去重新恢复登录态。
    """

    def __init__(self, name="cloud"):
        self.name = name
        self._lock = threading.Lock()
        self.ok_count = 0
        self.fail_count = 0
        self.consecutive = 0
        self.last_ok = 0.0
        self.last_fail = 0.0
        self.last_kind = None
        self.last_error = None
        self.by_kind = {}

    def ok(self):
        with self._lock:
            self.ok_count += 1
            self.consecutive = 0
            self.last_ok = time.time()
            self.last_kind = None

    def fail(self, kind, message=""):
        with self._lock:
            self.fail_count += 1
            self.consecutive += 1
            self.last_fail = time.time()
            self.last_kind = kind
            self.last_error = str(message)[:300]
            self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    @property
    def state(self):
        with self._lock:
            if self.consecutive == 0:
                return "ok"
            if self.consecutive < 3:
                return "degraded"
            return "down"

    def snapshot(self):
        with self._lock:
            now = time.time()
            return {
                "state": ("ok" if self.consecutive == 0
                          else ("degraded" if self.consecutive < 3 else "down")),
                "consecutive_fail": self.consecutive,
                "ok": self.ok_count,
                "fail": self.fail_count,
                "last_kind": self.last_kind,
                "last_error": self.last_error,
                "last_ok_age": (round(now - self.last_ok, 1)
                                if self.last_ok else None),
                "by_kind": dict(self.by_kind),
            }


def run(factory, submit, attempts=3, health=None, breaker=None, what=""):
    """同步重试入口。

    factory 每次调用都要**新建协程** —— 协程只能 await 一次，抛异常之后
    不能重跑。这是这一层唯一容易用错的地方，所以签名强制传函数。
    """
    attempts = max(1, int(attempts))
    if breaker is not None and not breaker.allow():
        raise BreakerOpen(breaker.retry_after())

    last = None
    for attempt in range(attempts):
        try:
            res = submit(factory())
        except BaseException as e:  # noqa: BLE001 - 连 CancelledError 也要记账
            last = e
            kind = classify(e)
            if breaker is not None:
                breaker.failure(kind)
            if health is not None:
                health.fail(kind, "%s %s" % (what or "-", e))
            if attempt + 1 >= attempts or not retryable(kind):
                raise
            time.sleep(_delay(attempt, kind))
        else:
            if breaker is not None:
                breaker.success()
            if health is not None:
                health.ok()
            return res
    raise last


async def a_run(factory, attempts=3, health=None, breaker=None, what="",
                budget=None):
    """协程内部用的异步重试。不经过线程池，不占 worker。

    budget: 整段重试的时间上限（秒）。批量子请求用它兜住，
    避免重试把 `call()` 那层的 25 秒预算吃穿。
    """
    attempts = max(1, int(attempts))
    started = time.monotonic()
    if breaker is not None and not breaker.allow():
        raise BreakerOpen(breaker.retry_after())

    last = None
    for attempt in range(attempts):
        try:
            res = await factory()
        except BaseException as e:  # noqa: BLE001
            last = e
            kind = classify(e)
            if breaker is not None:
                breaker.failure(kind)
            if health is not None:
                health.fail(kind, "%s %s" % (what or "-", e))
            if attempt + 1 >= attempts or not retryable(kind):
                raise
            delay = _delay(attempt, kind)
            if budget is not None and time.monotonic() - started + delay > budget:
                raise
            await asyncio.sleep(delay)
        else:
            if breaker is not None:
                breaker.success()
            if health is not None:
                health.ok()
            return res
    raise last


def summarize(exc):
    """给界面用的一句话。原来界面直接把 str(e) 摆出来，
    用户看到的是 `Error https://api.io.mi.com/app/...: ...` 这种天书。"""
    if isinstance(exc, BreakerOpen):
        return str(exc)
    kind = classify(exc)
    return {
        AUTH: "登录态已失效，需要重新登录",
        NET: "连不上米家云（网络不通），恢复后会自动重试",
        TIMEOUT: "米家云响应超时，稍后自动重试",
        SERVER: "米家云暂时不可用（服务端错误），稍后自动重试",
        RATE: "请求太频繁被米家云限流，稍等一会儿",
        UNKNOWN: "云端请求失败：%s" % (exc,),
    }.get(kind, str(exc))
