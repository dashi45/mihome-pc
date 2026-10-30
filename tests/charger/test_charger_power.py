# -*- coding: utf-8 -*-
"""充电器功率并入主页面总功率的测试

需求：充电器是蓝牙直连的，不在米家设备表里，但它确实在供电 ——
主页面「当前总功率」必须把它算进去，且**没有功率时不许显示 0W 那一行**。

链路：
    充电器 → bridge /api/state → app._charger_power() → /api/live.charger_w
          → 前端 state.chargerW → energy.powerRows() 里多一行 → total 变大

这里测中间三段（后端取数 + 求和 + 缓存 + 接口暴露）。
前端那段是纯 JS，靠 charger_proxy_test 之外的人工验证。

关键约束：全程 mock，不碰真实桥接，也不留缓存脏数据影响后续测试。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
os.environ["MIHOME_PORT"] = "8799"
os.environ["CHARGER_PORT"] = "18000"

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
    def __init__(self, body):
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
    """按预设返回桥接的 /api/state。calls 记次数，用来验证缓存。"""

    def __init__(self, payload=None, boom=False):
        self.payload = payload
        self.boom = boom
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        if self.boom:
            raise OSError("connection refused")
        body = json.dumps(self.payload).encode("utf-8")
        return FakeResp(body)


def state_ok(ports):
    return {"status": "ok", "ports": ports}


_real_opener = A._OPENER
_real_status = A.S.get("status")
_real_collect = A._collect_live


def reset_cache():
    A._CHG_POWER["t"], A._CHG_POWER["w"] = 0.0, None


print("=== 1. 正常情况：各端口功率求和 ===")

reset_cache()
A._OPENER = FakeOpener(state_ok({
    "C1": {"power": 30.0}, "C2": {"power": 15.0},
    "C3": {"power": 0}, "A": {"power": None},
}))
check("30 + 15 + 0 + None = 45.0", A._charger_power() == 45.0,
      "实际 %r" % (A._charger_power(),))

print("=== 2. 容错：功率是字符串 / 缺字段 / 不是字典 ===")

reset_cache()
A._OPENER = FakeOpener(state_ok({
    "C1": {"power": "12.5"},     # 字符串数字
    "C2": {},                    # 没有 power 字段
    "C3": "garbage",             # 不是字典
    "A": {"power": "abc"},       # 不是数字
}))
check("脏数据不影响求和（12.5）", A._charger_power() == 12.5,
      "实际 %r" % (A._charger_power(),))

print("=== 3. 桥接没跑 / 没连上 → None（不是 0）===")

reset_cache()
A._OPENER = FakeOpener(boom=True)
check("连接被拒返回 None", A._charger_power() is None,
      "实际 %r" % (A._charger_power(),))

reset_cache()
A._OPENER = FakeOpener({"status": "reconnecting", "ports": {}})
check("status 不是 ok 返回 None", A._charger_power() is None,
      "实际 %r" % (A._charger_power(),))

print("=== 4. 10 秒缓存：别每轮 live 都去敲桥接 ===")

reset_cache()
op = FakeOpener(state_ok({"C1": {"power": 20.0}}))
A._OPENER = op
A._charger_power()
A._charger_power()
A._charger_power()
check("10 秒内只请求 1 次（实际 %d 次）" % op.calls, op.calls == 1)

A._CHG_POWER["t"] = 0.0            # 假装缓存过期
A._charger_power()
check("缓存过期后会重新请求（实际 %d 次）" % op.calls, op.calls == 2)

print("=== 5. /api/live 必须把这个值带出去 ===")

reset_cache()
A._OPENER = FakeOpener(state_ok({"C1": {"power": 33.0}, "C2": {"power": 12.0}}))
A.S["status"] = "ready"
A._collect_live = lambda quick=False: {}      # 不真的去云端
A._QUICK_CACHE["data"] = None                 # 绕开 2.5 秒缓存
A._LIVE_CACHE["data"] = None

c = A.app.test_client()
# POST 属于写请求：同源守卫要求正向同源证据（Origin 或 Referer），
# 三条头全空的请求只可能来自非浏览器，一律 403。这是模拟真实浏览器，不是绕过。
H = {"Host": "127.0.0.1:%s" % A._PORT,
     "Origin": "http://127.0.0.1:%s" % A._PORT}
r = c.post("/api/live", json={"quick": 1}, headers=H)
body = r.get_json() or {}
check("/api/live 返回 200", r.status_code == 200, "实际 %s" % r.status_code)
check("响应里有 charger_w 字段", "charger_w" in body, "实际 keys=%s" % list(body))
check("charger_w = 33 + 12 = 45.0", body.get("charger_w") == 45.0,
      "实际 %r" % (body.get("charger_w"),))

print("=== 6. 桥接不在时 /api/live 仍然正常，charger_w 为 null ===")

reset_cache()
A._OPENER = FakeOpener(boom=True)
A._QUICK_CACHE["data"] = None
r = c.post("/api/live", json={"quick": 1}, headers=H)
body = r.get_json() or {}
check("接口本身仍然 200", r.status_code == 200, "实际 %s" % r.status_code)
check("charger_w 为 null（不是 0，前端据此不显示那一行）",
      body.get("charger_w") is None, "实际 %r" % (body.get("charger_w"),))

print("=== 7. 前端确实读了这个字段（源码级契约）===")

with open(os.path.join(BASE, "static", "app.js"), encoding="utf-8") as f:
    appjs = f.read()
with open(os.path.join(BASE, "static", "energy.js"), encoding="utf-8") as f:
    energyjs = f.read()

check("app.js 调 setChargerW(r.charger_w)", "setChargerW(r.charger_w)" in appjs)
check("app.js 有 state.chargerW 字段", "chargerW" in appjs)
check("energy.js 读 state.chargerW", "state.chargerW" in energyjs)
check("energy.js 会加一行「充电器」", '"充电器"' in energyjs)
check("energy.js 总功率含充电器（powerRows 里 push）",
      "charger: true" in energyjs)

# ---- 收尾：把改过的全局都还原 ----
A._OPENER = _real_opener
A._collect_live = _real_collect
if _real_status is not None:
    A.S["status"] = _real_status
A._CHG_POWER["t"], A._CHG_POWER["w"] = 0.0, None
A._QUICK_CACHE["data"] = None
A._LIVE_CACHE["data"] = None

print()
print("=" * 56)
print("通过 %d / 失败 %d" % (total - failed, failed))
sys.exit(1 if failed else 0)
