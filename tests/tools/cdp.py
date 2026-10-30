# -*- coding: utf-8 -*-
"""CDP 调试通道 —— 连上正在运行的窗口，量指标、切主题、截图。

为什么需要它：这个程序的界面跑在 EdgeWebView2 里，外面看只有一个窗口，
想量"到底占了多少 DOM / 是不是真的没溢出 / 改动后长什么样"，只能从
DevTools 协议进去。headless 浏览器不行 —— 登录态在 localStorage，
干净 profile 打开就是登录页。

用法：
    1. 带调试端口启动（WebView2 认这个环境变量）：
         set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222
         启动米家.bat
    2. python tests/cdp.py            # 打印页面指标 + 存两张主题截图

venv 里没有 websocket 库，这里手写了一个极简 RFC6455 客户端：
只用得上「发一条文本帧、收几条文本帧」，不值得为它装依赖。
"""
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

PORT = 9222
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bypass_proxy():
    """本地回环必须绕开系统代理。

    机器上装了 Clash 一类的代理时，http_proxy 是全局环境变量，urllib 会把
    http://127.0.0.1:9222 也丢给代理，拿回来的是 502 Bad Gateway 或者
    "目标计算机积极拒绝"——看起来像"调试端口根本没开"，实际端口是好的。
    这个坑排查起来很费时间（curl 也一样中招），所以在这里一次性堵上。
    """
    for k in ("NO_PROXY", "no_proxy"):
        cur = os.environ.get(k, "")
        if "127.0.0.1" in cur:
            continue
        os.environ[k] = ("127.0.0.1,localhost," + cur).strip(",")


_bypass_proxy()



def http_json(path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


class WS:
    def __init__(self, url):
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=20)
        self._mid = 0
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key))
        self.s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("握手失败: %r" % head[:120])

    def send(self, obj):
        data = json.dumps(obj).encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.s.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("连接关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        b1, b2 = self._read(2)
        op = b1 & 0x0F
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        payload = self._read(ln)
        if op == 0x8:
            raise ConnectionError("收到关闭帧")
        if op == 0x9:              # ping 要跳过（CDP 目前不发，留着防以后）
            return self.recv()
        return json.loads(payload.decode("utf-8"))

    def call(self, method, params=None, timeout=30):
        self._mid += 1
        mid = self._mid
        self.send({"id": mid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result") or {}
        raise TimeoutError(method)


def page_ws():
    for t in http_json("/json/list"):
        if t.get("type") == "page" and "127.0.0.1" in (t.get("url") or ""):
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("没找到页面 target")


def connect():
    return WS(page_ws())


def eval_js(ws, expr):
    r = ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                     "awaitPromise": True})
    if r.get("exceptionDetails"):
        raise RuntimeError(r["exceptionDetails"].get("text"))
    return (r.get("result") or {}).get("value")


def shot(ws, path, full=False):
    r = ws.call("Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": bool(full)}, timeout=40)
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    return path


def metrics(ws):
    ws.call("Performance.enable")
    r = ws.call("Performance.getMetrics")
    return {m["name"]: m["value"] for m in r["metrics"]}


PROBE = r"""
(function(){
  var d=document.documentElement, all=document.querySelectorAll('*');
  var main=document.getElementById('scroller');
  var layers={fixed:0,sticky:0,filter:0,backdrop:0,willChange:0}, shadow=0, grad=0;
  for (var i=0;i<all.length;i++){
    var cs=getComputedStyle(all[i]);
    if(cs.position==='fixed') layers.fixed++;
    if(cs.position==='sticky') layers.sticky++;
    if(cs.filter && cs.filter!=='none') layers.filter++;
    if(cs.backdropFilter && cs.backdropFilter!=='none') layers.backdrop++;
    if(cs.willChange && cs.willChange!=='auto') layers.willChange++;
    if(cs.boxShadow && cs.boxShadow!=='none') shadow++;
    if(cs.backgroundImage && cs.backgroundImage.indexOf('gradient')>=0) grad++;
  }
  return JSON.stringify({
    vw: innerWidth, vh: innerHeight, dpr: devicePixelRatio,
    elements: all.length,
    overflowX: main.scrollWidth - main.clientWidth,
    layers: layers, shadowElems: shadow, gradientElems: grad,
    cssRules: (function(){ var n=0; for(var i=0;i<document.styleSheets.length;i++){
      try{ n+=document.styleSheets[i].cssRules.length; }catch(e){} } return n; })()
  });
})()
"""


def report(ws):
    data = json.loads(eval_js(ws, PROBE))
    m = metrics(ws)
    print("== 页面 ==")
    print("  视口 %sx%s @%.2fx   元素 %d 个   CSS 规则 %d 条" %
          (data["vw"], data["vh"], data["dpr"], data["elements"], data["cssRules"]))
    print("  横向溢出 %d px  %s" % (data["overflowX"],
                                   "✅ 无" if data["overflowX"] <= 0 else "⚠ 有"))
    print("  合成层线索 %s；带阴影元素 %d，带渐变元素 %d" %
          (data["layers"], data["shadowElems"], data["gradientElems"]))
    print("== 性能 ==")
    for k in ("JSHeapUsedSize", "Nodes", "Documents", "LayoutCount", "RecalcStyleCount"):
        if k in m:
            v = m[k]
            print("  %-18s %s" % (k, ("%.1f MB" % (v / 1048576)) if "Size" in k else int(v)))
    return data


if __name__ == "__main__":
    w = connect()
    report(w)
    outs = []
    for theme in ("dark", "light"):
        eval_js(w, 'document.documentElement.dataset.theme="%s"' % theme)
        time.sleep(1.0)
        outs.append(shot(w, os.path.join(BASE, "_截图", "_cdp-%s.png" % theme)))
    eval_js(w, 'document.documentElement.dataset.theme=localStorage.getItem("mi-theme")||"dark"')
    print("\n截图:")
    for o in outs:
        print("  " + o)
