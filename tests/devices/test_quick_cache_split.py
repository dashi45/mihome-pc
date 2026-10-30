# -*- coding: utf-8 -*-
"""两个同名缓存的回归守卫：`_QUICK_CACHE` 不许再被卡片按钮缓存顶掉。

## 曾经的 bug

`services/devices.py` 里先后定义了两个东西：

    :129  _QUICK_CACHE = {"t": 0.0, "data": None}   # 只含开关的 live 缓存
    :350  _QUICK_CACHE = {}                          # 卡片按钮（档位/模式）缓存

第二个把第一个整个覆盖掉了。于是 `routes/live.py` 里

    cache = _QUICK_CACHE if quick else _LIVE_CACHE
    if cache["data"] is not None and now - cache["t"] < TTL:

每个 `{quick:1}` 请求都 `KeyError('data')` → 500。而前端
`static/app.js` 的 `_refreshQuick()` 只写 `if (!r.ok) return;`，
把失败静默吞掉 —— 用户看到的是"设备状态就是不刷新"，
没有任何报错，日志里也只有一行 500。

`tests/charger/test_charger_power.py` 当年手动塞了
`A._QUICK_CACHE["data"] = None` 绕开缓存，正好把这个 bug 遮住。

## 这个测试守什么

  1. `routes/live.py` 拿到的 `_QUICK_CACHE` 就是 devices 里那个**带 data 的** dict；
  2. 卡片按钮的缓存是另一个对象（现在叫 `_CARD_CACHE`）；
  3. 真发一次 `POST /api/live {"quick":1}` 必须 200 —— 这条直接钉住当年那个 500；
  4. 调 `quick_actions()` / `swing_action()` 之后，live 缓存里仍然有 `data` 键
     （防止以后有人再往同一个名字上挂第二个 dict）。

全程 mock，不出网、不开窗口。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
os.environ["MIHOME_PORT"] = "8798"
os.environ["CHARGER_PORT"] = "18001"

from services import devices                      # noqa: E402
from routes import live as live_routes            # noqa: E402
import app as A                                   # noqa: E402

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


print("=== 1. 两个缓存必须是两个对象 ===")

check("devices._QUICK_CACHE 带 data 键（live 开关缓存）",
      isinstance(devices._QUICK_CACHE, dict) and "data" in devices._QUICK_CACHE,
      "实际 %r" % (devices._QUICK_CACHE,))
check("routes.live 拿到的就是那个 live 缓存",
      live_routes._QUICK_CACHE is devices._QUICK_CACHE)
check("卡片按钮缓存是另一个 dict",
      hasattr(devices, "_CARD_CACHE") and devices._CARD_CACHE is not devices._QUICK_CACHE,
      "devices 里还有 _CARD_CACHE 吗: %s" % hasattr(devices, "_CARD_CACHE"))

print("=== 2. 真发一次 quick 轮询：不许 500 ===")

_real_collect = live_routes.live_svc._collect_live
_real_status = live_routes.S.get("status")
try:
    live_routes.live_svc._collect_live = lambda quick=False: {}
    live_routes.S["status"] = "ready"          # 否则接口在 401 就返回了，测不到缓存那段
    devices._QUICK_CACHE["data"] = None        # 绕开 2.5 秒 TTL，逼它真走一遍代码
    devices._QUICK_CACHE["t"] = 0.0

    c = A.app.test_client()
    # POST 是写请求：同源守卫要求正向同源证据（Origin/Referer），
    # 三条头全空只可能来自非浏览器 → 403。这是模拟真实浏览器，不是绕过。
    H = {"Host": "127.0.0.1:%s" % A._PORT,
         "Origin": "http://127.0.0.1:%s" % A._PORT}
    r = c.post("/api/live", json={"quick": 1}, headers=H)
    body = r.get_json() or {}
    check("/api/live?quick=1 返回 200", r.status_code == 200, "实际 %s" % r.status_code)
    check("响应里没有服务端异常", not body.get("error"), "实际 %r" % (body.get("error"),))
    check("响应带 live 字段", "live" in body, "实际 keys=%s" % list(body))

    print("=== 3. 卡片按钮缓存不许再碰 live 缓存 ===")

    # 造一个不依赖云端的假 spec，走 quick_actions / swing_action 的缓存写入分支
    fake_model = "__test_fan__"
    devices.SPECS[fake_model] = {
        "services": [{
            "iid": 2, "type": "urn:miot-spec-v2:service:fan:0000780A",
            "properties": [
                {"iid": 1, "type": "urn:miot-spec-v2:property:fan-level:1",
                 "access": ["read", "write", "notify"], "format": "uint8",
                 "value-list": [{"value": 1, "description": "Low"},
                                {"value": 2, "description": "High"}]},
            ],
        }]
    }
    got = devices.quick_actions(fake_model, "fan")
    check("quick_actions 能推出档位按钮", bool(got), "实际 %r" % (got,))
    check("写进的是 _CARD_CACHE", (fake_model, "fan") in devices._CARD_CACHE)
    check("没污染 live 开关缓存",
          "data" in devices._QUICK_CACHE and "data" in live_routes._QUICK_CACHE,
          "实际 %r" % (devices._QUICK_CACHE,))
    check("摇头缓存也走同一个卡片缓存",
          (("swing", fake_model) in devices._CARD_CACHE) or devices.swing_action(fake_model) is None)
finally:
    live_routes.live_svc._collect_live = _real_collect
    if _real_status is None:
        live_routes.S.pop("status", None)
    else:
        live_routes.S["status"] = _real_status
    devices.SPECS.pop("__test_fan__", None)
    devices._CARD_CACHE.pop(("__test_fan__", "fan"), None)
    devices._CARD_CACHE.pop(("swing", "__test_fan__"), None)
    devices._QUICK_CACHE["t"], devices._QUICK_CACHE["data"] = 0.0, None
    devices._LIVE_CACHE["t"], devices._LIVE_CACHE["data"] = 0.0, None

print()
print("=" * 56)
print("通过 %d / 失败 %d" % (total - failed, failed))
sys.exit(1 if failed else 0)
