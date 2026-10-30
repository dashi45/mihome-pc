# -*- coding: utf-8 -*-
"""连接池（mihttp.PooledSession）的回归测试。

用本地 keep-alive 服务端量"连接数"，钉住四件事：
  1. N 次请求只握手 1 次（这才是池化存在的意义）
  2. Cookie 能存下来、能带回去
  3. 服务端说 close 时能被识别，下一次自动重连而不是报错
  4. 服务端**悄悄**断开（不发 Connection: close，典型的 SLB 回收）时，
     下一次请求要能自己发现并重连 —— 这是最容易被忽略、也最容易
     变成"偶发失败"的一条
"""
import asyncio
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

OK = 0
FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.server.conns += 1

    def log_message(self, *a):
        pass

    def _send(self, body, extra_headers=None, close=False):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        if close:
            self.close_connection = True

    def do_GET(self):
        if self.path == "/close":
            self._send(b'{"ok":1}', close=True)
            return
        if self.path == "/setcookie":
            self._send(b'{"ok":1}', {"Set-Cookie": "sid=abc123; Path=/"})
            return
        if self.path == "/kill":
            # 最刁钻的一种：正常回包，但不声明 Connection: close，然后直接断
            self._send(b'{"ok":1}')
            try:
                self.connection.close()
            except Exception:
                pass
            self.close_connection = True
            return
        body = json.dumps({"path": self.path,
                           "cookie": self.headers.get("Cookie", "")}).encode()
        self._send(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        body = json.dumps({"path": self.path, "body": raw.decode(),
                           "ctype": self.headers.get("Content-Type", "")}).encode()
        self._send(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.conns = 0

    def handle_error(self, request, client_address):
        pass          # /kill 故意砸连接，别刷栈


import mihttp as M

srv = Server(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port

print("=" * 62)
print("1. 复用：N 次请求只握手 1 次")
print("=" * 62)

sess = M.PooledSession()


async def many(n):
    out = []
    for i in range(n):
        async with sess.request("GET", base + "/e%d" % i) as r:
            out.append(r.status)
            await r.json()
    return out


statuses = asyncio.run(many(8))
check("8 次请求全部成功", statuses == [200] * 8, "got=%r" % (statuses[:3],))
# 不卡死"必须正好 1 条"：executor 偶尔会换一个工作线程，那就多一条连接。
# 真正要钉的是"连接数远小于请求数"（池化没生效的话这里会是 8）。
check("8 次请求只握手 1~2 次", srv.conns <= 2, "conns=%d" % srv.conns)
st = sess.stats()
check("统计：requests=8", st["requests"] == 8, st)
check("统计：reconnects 远小于请求数",
      st["reconnects"] <= 2, st)

print()
print("=" * 62)
print("2. Cookie")
print("=" * 62)


async def cookie_flow():
    async with sess.request("GET", base + "/setcookie") as r:
        await r.json()
    async with sess.request("GET", base + "/echo") as r:
        return (await r.json())["cookie"]


ck = asyncio.run(cookie_flow())
check("Set-Cookie 被记住并在下一次带回", "sid=abc123" in ck, repr(ck))

print()
print("=" * 62)
print("3. 服务端声明 close → 下次自动重连")
print("=" * 62)

before = srv.conns


async def close_flow():
    async with sess.request("GET", base + "/close") as r:
        await r.json()
    after_close = srv.conns        # 收到 Connection: close 之后的连接数
    async with sess.request("GET", base + "/echo") as r:
        await r.json()
    return after_close, srv.conns


c1, c2 = asyncio.run(close_flow())
check("close 之后的请求仍然成功（不是报错）", c2 >= c1)
check("close 之后只重连了一次", c2 == c1 + 1,
      "after_close=%d after_next=%d" % (c1, c2))

print()
print("=" * 62)
print("4. 服务端悄悄断开（SLB 回收）→ 自己发现并重连")
print("=" * 62)

before = srv.conns


async def kill_flow():
    async with sess.request("GET", base + "/kill") as r:
        await r.json()
    await asyncio.sleep(0.05)
    async with sess.request("GET", base + "/echo") as r:
        return r.status


raised = None
try:
    st_after_kill = asyncio.run(kill_flow())
except Exception as e:
    raised = e
check("悄悄断开后下一次请求成功", raised is None and st_after_kill == 200,
      "raised=%r" % (raised,))
check("悄悄断开后重建了连接", srv.conns >= before + 1,
      "before=%d after=%d" % (before, srv.conns))

print()
print("=" * 62)
print("5. POST 表单编码")
print("=" * 62)


async def post_flow():
    async with sess.request("POST", base + "/p",
                            data={"a": "1", "b": "中文"}) as r:
        return await r.json()


d = asyncio.run(post_flow())
check("body 被 urlencode", "a=1" in d["body"] and "b=%E4%B8%AD%E6%96%87" in d["body"],
      d["body"])
check("Content-Type 自动补成表单",
      "application/x-www-form-urlencoded" in d["ctype"], d["ctype"])

print()
print("=" * 62)
print("6. 分流：登录域名走原实现，业务域名走池化")
print("=" * 62)


class Fake:
    def __init__(self, tag):
        self.tag = tag
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        return ("fake:" + self.tag)


pooled, legacy = Fake("pooled"), Fake("legacy")
router = M.RouterSession(pooled=pooled, legacy=legacy)
router.request("GET", "https://api.io.mi.com/app/x")
router.request("GET", "https://account.xiaomi.com/pass/x")
router.request("GET", "https://sts.api.io.mi.com/sts")
router.request("GET", "https://miot-spec.org/miot-spec-v2/instances")
check("api.io.mi.com → 池化",
      len(pooled.calls) == 1 and pooled.calls[0][1].startswith("https://api.io.mi.com"),
      pooled.calls)
check("其它域名（登录/sts/规格）→ 原实现",
      [c[1] for c in legacy.calls] == [
          "https://account.xiaomi.com/pass/x",
          "https://sts.api.io.mi.com/sts",
          "https://miot-spec.org/miot-spec-v2/instances"],
      legacy.calls)

print()
print("=" * 62)
print("7. 逃生口：MIHOME_HTTP_POOL=0")
print("=" * 62)

old = os.environ.get("MIHOME_HTTP_POOL")
try:
    os.environ["MIHOME_HTTP_POOL"] = "0"
    check("关掉后 make_session() 返回 None（miservice 用自带的）",
          M.make_session() is None)
    check("enabled() 跟着变 False", M.enabled() is False)
    os.environ["MIHOME_HTTP_POOL"] = "1"
    check("打开后返回池化会话", isinstance(M.make_session(), M.RouterSession))
finally:
    if old is None:
        os.environ.pop("MIHOME_HTTP_POOL", None)
    else:
        os.environ["MIHOME_HTTP_POOL"] = old

srv.shutdown()

print()
print("=" * 62)
print("结果: %d 通过, %d 失败" % (OK, FAIL))
print("=" * 62)
sys.exit(1 if FAIL else 0)
