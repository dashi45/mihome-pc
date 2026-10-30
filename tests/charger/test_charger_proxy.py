# -*- coding: utf-8 -*-
"""充电器代理的「写请求通道」测试

2026-10-25 实际故障：`/charger/<path:sub>` 的装饰器漏了 methods=["GET","POST"]，
Flask 默认只放行 GET。结果是页面能打开、读数也正常，但**所有写操作**
（场景切换 / 接口开关 / 定时设置 / 屏显）一律 405。前端日志里只有
「场景切换失败: 405 Method Not Allowed」，完全看不出是路由没放行 ——
先怀疑过桥接、怀疑过蓝牙，绕了一大圈。

这个测试钉住四件事：
  1. POST /charger/api/set 不被 405 拦掉
  2. 转发出去的方法仍然是 POST（不能悄悄降级成 GET）
  3. body 原样带过去（丢了桥接会收到空 JSON，症状是"点了没反应"）
  4. GET /charger/api/state 仍然正常（别修好 POST 把 GET 弄坏）

全程 mock 掉桥接，不依赖 8000 端口有没有在跑，也不碰 charger.log。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
os.environ["MIHOME_PORT"] = "8799"
os.environ["CHARGER_PORT"] = "18000"      # 别撞真实的 8000

import app as A                            # noqa: E402

failed = 0
total = 0


def check(name, cond, extra=""):
    global failed, total
    total += 1
    if cond:
        print("  ✓ %s" % name)
    else:
        failed += 1
        print("  ✗ %s %s" % (name, extra))


class FakeResp:
    def __init__(self, body=b'{"ok":true}'):
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """替掉 _OPENER：只记录最后一次转发出去的请求，不发网络。"""

    def __init__(self):
        self.last = None

    def open(self, req, timeout=None):
        self.last = req
        return FakeResp()


_real_ensure = A._ensure_charger
_real_opener = A._OPENER
A._ensure_charger = lambda *a, **k: True
fake = FakeOpener()
A._OPENER = fake

c = A.app.test_client()
HOST = "127.0.0.1:%s" % A._PORT
H = {"Host": HOST}
# 写请求要带 Origin：同源守卫要求正向同源证据，空头只可能是非浏览器。
H_POST = {"Host": HOST, "Origin": "http://%s" % HOST}

print("=== 1. 路由必须放行 POST（本次故障的根因）===")

r = c.post("/charger/api/set", data='{"piid":5,"value":2}',
           content_type="application/json", headers=H_POST)
check("POST /charger/api/set 不是 405", r.status_code != 405,
      "实际 %s" % r.status_code)
check("POST /charger/api/set 成功透传（200）", r.status_code == 200,
      "实际 %s" % r.status_code)

print("=== 2. 转发出去的方法与 body 必须保真 ===")

if fake.last is not None:
    check("转发方法是 POST（没被降级成 GET）", fake.last.method == "POST",
          "实际 %s" % fake.last.method)
    check("body 原样带过去",
          fake.last.data == b'{"piid":5,"value":2}',
          "实际 %r" % (fake.last.data,))
    check("目标地址是桥接的 /api/set",
          fake.last.full_url == "http://127.0.0.1:%s/api/set" % A.CHARGER_PORT,
          "实际 %s" % fake.last.full_url)
else:
    check("转发发生了", False, "根本没走到转发")

print("=== 3. 定时/屏显这些也走同一条路 ===")

for path, payload in (
        ("/charger/api/set", '{"piid":9,"value":60}'),
        ("/charger/api/set", '{"piid":17,"value":1}')):
    r = c.post(path, data=payload, content_type="application/json",
               headers=H_POST)
    check("POST %s 放行" % path, r.status_code == 200,
          "实际 %s" % r.status_code)
    check("  body 保真", fake.last.data == payload.encode(),
          "实际 %r" % (fake.last.data,))

print("=== 4. GET 不能被修坏 ===")

fake.last = None
r = c.get("/charger/api/state", headers=H)
check("GET /charger/api/state 返回 200", r.status_code == 200,
      "实际 %s" % r.status_code)
check("转发方法是 GET", fake.last is not None and fake.last.method == "GET")
check("GET 不带 body", fake.last is not None and fake.last.data is None,
      "实际 %r" % (fake.last.data if fake.last else None))

print("=== 5. 静态文件与页面入口 ===")

fake.last = None
r = c.get("/charger/", headers=H)
check("GET /charger/ 返回 200", r.status_code == 200, "实际 %s" % r.status_code)

r = c.get("/charger/app.js", headers=H)
check("GET /charger/app.js 返回 200", r.status_code == 200,
      "实际 %s" % r.status_code)

print("=== 6. 绝对路径兜底也还在 ===")

fake.last = None
r = c.post("/api/set", data='{"piid":5,"value":3}',
           content_type="application/json", headers=H_POST)
check("POST /api/set 兜底可用", r.status_code == 200, "实际 %s" % r.status_code)
check("兜底也按 POST 转发", fake.last is not None and fake.last.method == "POST")

# ---- 收尾：恢复被替换的对象，别影响同进程的其它测试 ----
A._ensure_charger = _real_ensure
A._OPENER = _real_opener

print()
print("=" * 56)
print("通过 %d / 失败 %d" % (total - failed, failed))
sys.exit(1 if failed else 0)
