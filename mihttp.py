# -*- coding: utf-8 -*-
"""带连接复用的 HTTP 会话（替代 miservice 自带的 biohttp 逐请求建连）。

## 为什么

miservice 自带的 `biohttp` 每次请求都 `build_opener()` 新建连接，也就是每个
请求都重做一遍 TCP + TLS 握手。本机实测到 api.io.mi.com（2026-10-26）：

    DNS 1.1ms + TCP 52.5ms + TLS 122.3ms + RTT 51.6ms = 227.5ms
    复用连接后                                 = 51.9ms
    单次省 175ms（-77%）

## 为什么不直接改 biohttp

登录流程（`account.xiaomi.com`）依赖它自己的**重定向 + Set-Cookie 捕获**
（`_CookieCapturingRedirectHandler`），`serviceToken` 就是靠那条链路拿到的。
那一段是全程序最不能出意外的地方，所以：

  · `account.xiaomi.com` → **原样走 biohttp**，一个字节都不碰
  · `api.io.mi.com`     → 走这里的池化会话

分流在 `RouterSession` 里，按 URL 主机名判断。要整段关掉也很简单：
环境变量 `MIHOME_HTTP_POOL=0`，全部退回原实现（出问题时的逃生口）。

## 复用是安全的（三条保证）

1. **只复用读干净的连接**：每次都 `read()` 到结尾，没读完绝不还池。
   串包是最脏的失败模式（读到上一次的 body），从根上避免。
2. **复用前体检**：空闲超过 `IDLE_LIMIT` 秒、或 socket 已经可读（说明服务端
   发了 EOF / 有残留），直接丢弃重建。这一步挡掉绝大多数"服务端悄悄关了
   连接、客户端还往里写"。
3. **出错即弃**：任何连接层异常都把这个连接扔掉再抛，绝不放回池子。
   上层（netguard）照自己的策略决定重不重试 —— 传输层不擅自重放请求，
   否则 `action`（切换/云台）可能被执行两次。

## 不做什么

不重试、不改写请求体、不缓冲整文件（这些接口都是小 JSON）。
线程安全靠 thread-local：请求本来就在 executor 线程里跑，一个线程一条连接，
天然没有"两个协程共用一条连接"的错位问题。
"""
import asyncio
import http.client
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
import json
import os
import select
import socket
import ssl
import threading
import time
from urllib import parse

# 空闲超过这么久就当服务端已经关了。小米的 SLB 一般 60 秒上下回收，
# 取 25 秒是留一半余量：宁可偶尔多重连一次，也不要撞上"半死连接"。
IDLE_LIMIT = 25.0

# 连接层异常（这些一旦出现，这条连接就不可信了）
_CONN_ERRORS = (
    ConnectionError, socket.timeout, ssl.SSLError, OSError,
    http.client.HTTPException,
)


class _Resp:
    """和 biohttp.ClientResponse 同形的响应对象。"""

    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = headers
        self._body = body
        self.url = url
        sc = SimpleCookie()
        for raw in (headers.get_all("Set-Cookie") or []):
            try:
                sc.load(raw)
            except Exception:
                pass
        self.cookies = sc

    async def read(self):
        return self._body

    async def text(self, encoding="utf-8"):
        return self._body.decode(encoding, "replace")

    async def json(self, content_type=None):
        return json.loads(self._body)

    def close(self):
        pass


class _HostConn:
    """一条（按线程、按主机）可复用的连接。"""

    __slots__ = ("host", "port", "scheme", "timeout", "conn", "idle_since",
                 "reconnects", "requests")

    def __init__(self, host, port, scheme, timeout):
        self.host, self.port, self.scheme = host, port, scheme
        self.timeout = timeout
        self.conn = None
        self.idle_since = time.monotonic()   # 不是 0：否则第一次用就被判"空闲太久"
        self.reconnects = 0
        self.requests = 0

    def _new(self):
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port,
                                               timeout=self.timeout)
        return http.client.HTTPConnection(self.host, self.port,
                                          timeout=self.timeout)

    def _stale(self):
        """这条连接还能用吗？两个判据：空闲太久 / socket 已经可读。"""
        c = self.conn
        if c is None:
            return True
        if time.monotonic() - self.idle_since > IDLE_LIMIT:
            return True
        sock = getattr(c, "sock", None)
        if sock is None:
            return True
        try:
            # 可读 = 服务端已经关了或发了不该有的东西，这条连接不可信
            r, _, _ = select.select([sock], [], [], 0)
            return bool(r)
        except Exception:
            return True

    def get(self):
        if self._stale():
            self.drop()
        if self.conn is None:
            self.conn = self._new()
            self.reconnects += 1
        return self.conn

    def drop(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    def done(self):
        self.idle_since = time.monotonic()


def _perform(session, method, url, headers, body):
    """在 executor 线程里跑：发一个请求并读干净。异常时把连接扔掉再抛。

    **连接必须在这里取，不能在事件循环线程里取。**
    踩过一次：`_holder()` 原来写在协程里（跑在 loop 线程），thread-local
    于是对所有协程返回同一个 holder —— 12 个并发任务共用一条连接，
    立刻互相踩：`CannotSendRequest('Request-sent')`、`ResponseNotReady('Idle')`，
    重连计数也爆炸。挪进 executor 线程后，一个线程一条连接，天然不共享。
    """
    sp = parse.urlsplit(url)
    host = (sp.hostname or "").lower()
    scheme = sp.scheme or "https"
    port = sp.port or (443 if scheme == "https" else 80)
    path = sp.path or "/"
    if sp.query:
        path += "?" + sp.query

    holder = session._holder(host, port, scheme)
    before = holder.reconnects
    conn = holder.get()
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()          # 必须读干净，否则下一个请求会读到残包
        status, hdrs, will_close = resp.status, resp.headers, resp.will_close
    except _CONN_ERRORS:
        holder.drop()
        raise
    holder.requests += 1
    if will_close:
        holder.drop()               # 服务端说不复用，那就别复用
    else:
        holder.done()
    return status, hdrs, data, holder.reconnects - before


class PooledSession:
    """aiohttp/biohttp 兼容的会话，按 (线程, 主机) 复用连接。"""

    name = "pooled"

    def __init__(self, timeout=30.0):
        self.timeout = timeout
        self._local = threading.local()
        self._jar = {}              # host -> {name: value}，和 biohttp 的分域一致
        self._jar_lock = threading.Lock()
        self._stat_lock = threading.Lock()
        self._stats = {"requests": 0, "reconnects": 0, "errors": 0,
                       "last_error": None}

    # ---- 统计（给 /api/status 看）----
    def _bump(self, key, n=1):
        with self._stat_lock:
            self._stats[key] = self._stats.get(key, 0) + n

    def stats(self):
        with self._stat_lock:
            out = dict(self._stats)
        out["idle_limit"] = IDLE_LIMIT
        return out

    # ---- 连接池 ----
    def _holder(self, host, port, scheme):
        d = getattr(self._local, "conns", None)
        if d is None:
            d = {}
            self._local.conns = d
        key = (host, port, scheme)
        h = d.get(key)
        if h is None:
            h = d[key] = _HostConn(host, port, scheme, self.timeout)
        return h

    def _cookies_for(self, host):
        with self._jar_lock:
            return dict(self._jar.get(host) or {})

    def _store_cookies(self, host, resp):
        if not resp.cookies:
            return
        with self._jar_lock:
            jar = self._jar.setdefault(host, {})
            for k, m in resp.cookies.items():
                jar[k] = m.value

    def request(self, method, url, **kwargs):
        @asynccontextmanager
        async def ctx():
            sp = parse.urlsplit(url)
            host = (sp.hostname or "").lower()
            scheme = sp.scheme or "https"
            port = sp.port or (443 if scheme == "https" else 80)

            headers = dict(kwargs.get("headers") or {})
            cookies = self._cookies_for(host)
            cookies.update(kwargs.get("cookies") or {})
            if cookies:
                headers["Cookie"] = "; ".join("%s=%s" % kv for kv in cookies.items())

            data = kwargs.get("data")
            body = None
            if isinstance(data, dict):
                body = parse.urlencode(data).encode()
                headers.setdefault("Content-Type",
                                   "application/x-www-form-urlencoded")
            elif isinstance(data, str):
                body = data.encode()
            elif isinstance(data, (bytes, bytearray)):
                body = bytes(data)

            loop = asyncio.get_running_loop()
            try:
                status, hdrs, payload, new_conns = await loop.run_in_executor(
                    None, _perform, self, method, url, headers, body)
            except _CONN_ERRORS as e:
                self._bump("errors")
                with self._stat_lock:
                    self._stats["last_error"] = "%s: %s" % (type(e).__name__, e)
                raise
            self._bump("requests")
            if new_conns:
                self._bump("reconnects", new_conns)
            resp = _Resp(status, hdrs, payload, url)
            self._store_cookies(host, resp)
            try:
                yield resp
            finally:
                resp.close()

        return ctx()

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    async def close(self):
        d = getattr(self._local, "conns", None)
        if d:
            for h in list(d.values()):
                h.drop()
            d.clear()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        await self.close()


class RouterSession:
    """按主机分流：**只有 api.io.mi.com 走池化**，其余一律原实现。

    为什么是白名单而不是黑名单：盘点过一遍，这条链路上出现的域名不止登录那一个 ——
    `account.xiaomi.com`（登录/验证码）、`sts.api.io.mi.com`（换 serviceToken）、
    `miot-spec.org`（拉设备规格，虽然本地有缓存）。后两个我第一次写成了"走池化"，
    其中 sts 那一步是**靠重定向链路上的 Set-Cookie** 拿 `serviceToken` 的，
    池化会话不做重定向跟随，一旦轮到它就直接登录失败 —— 而这条路径平时不触发，
    测不出来，只会在某次真过期后爆发。白名单把这种"想不到的域名"全部挡在外面。

    代价：规格拉取那点握手开销不再省（有 spec_cache，一天也就几次）。
    """

    # 只认这一个：日常 99% 的请求（属性读写、设备表、能量历史）都在它下面
    POOLED_HOSTS = ("api.io.mi.com",)

    def __init__(self, pooled=None, legacy=None):
        self.pooled = pooled or PooledSession()
        if legacy is None:
            from miservice import biohttp
            legacy = biohttp.ClientSession()
        self.legacy = legacy

    def _use_pool(self, url):
        try:
            host = (parse.urlsplit(url).hostname or "").lower()
        except Exception:
            return False
        return host in self.POOLED_HOSTS

    def request(self, method, url, **kwargs):
        if self._use_pool(url):
            return self.pooled.request(method, url, **kwargs)
        return self.legacy.request(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    async def close(self):
        try:
            await self.pooled.close()
        except Exception:
            pass
        try:
            await self.legacy.close()
        except Exception:
            pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        await self.close()

    def stats(self):
        try:
            return self.pooled.stats()
        except Exception:
            return {}


def enabled():
    """连接池**默认关闭**（2026-10-26 改）。

    原先默认开，理由是"单次省 175ms"。但那笔收益只落在点击手感上，
    却把"登录会不会要重来"的风险引了进来。在"稳妥"和"快一点"之间，
    这个项目现在选稳妥 —— 而且 175ms 在一个家庭控制面板上没人能感知。

    想开：启动前设 MIHOME_HTTP_POOL=1。
    开了之后如果出现任何怪问题，第一步就是把它关掉再复现：
    关掉即回到 miservice 自带实现，零改动、零风险。
    """
    return (os.environ.get("MIHOME_HTTP_POOL", "0").strip().lower()
            not in ("0", "false", "off", "no", ""))


def make_session():
    """给 miservice.MiAccount 用的会话。返回 None 表示"用库自己的"。"""
    if not enabled():
        return None
    return RouterSession()
