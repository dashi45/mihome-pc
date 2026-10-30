# -*- coding: utf-8 -*-
"""开机自启开关的端到端测试（走真实 HTTP + 真实注册表）

用 Flask test_client 打真实路由，验证：
  - GET  返回 enabled / target / fail_count
  - POST enabled=true  → 注册表真写进去了，enabled 变 true
  - POST enabled=false → 注册表真删了，enabled 变 false
  - 跨源请求被同源守卫拦掉（403）—— 不能让别的网页改自启

最后无条件把注册表恢复到测试前的状态。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
os.environ["MIHOME_PORT"] = "8799"

import app as A          # noqa: E402
import autostart        # noqa: E402

failed = 0
total = 0
H = {"Host": "127.0.0.1:%s" % A._PORT}
JSON = {"Content-Type": "application/json"}
# 真实浏览器发同源 POST 一定带 Origin。这里必须带上 ——
# 同源守卫对「写请求」要求正向同源证据（Origin 或 Referer 至少有一个），
# 三条头全空的写请求只可能来自非浏览器，一律 403。
# 这不是绕过，是模拟真实客户端。
ORIGIN = {"Host": "127.0.0.1:%s" % A._PORT,
          "Origin": "http://127.0.0.1:%s" % A._PORT,
          "Content-Type": "application/json"}


def check(name, cond, extra=""):
    global failed, total
    total += 1
    if cond:
        print("  ✓ %s" % name)
    else:
        failed += 1
        print("  ✗ %s %s" % (name, extra))


c = A.app.test_client()

# 记录原始状态，最后恢复
orig_on = autostart.is_enabled()
orig_value = autostart._read_value()

print("== 1. GET /api/autostart ==")
r = c.get("/api/autostart", headers=H)
j = r.get_json() or {}
check("返回 200", r.status_code == 200, r.status_code)
check("有 ok=True", j.get("ok") is True)
check("有 enabled 字段", "enabled" in j)
check("有 target 字段", "target" in j)
check("有 fail_count 字段", "fail_count" in j)
check("supported=True（Windows）", j.get("supported") is True)

print()
print("== 2. POST 打开自启 ==")
r = c.post("/api/autostart", json={"enabled": True}, headers=ORIGIN)
j = r.get_json() or {}
check("返回 200", r.status_code == 200, r.status_code)
check("enabled=True", j.get("enabled") is True)
check("注册表真的有值了", autostart._read_value() is not None)
check("指向的文件真实存在", os.path.isfile(autostart._parse_path(autostart._read_value() or "")))

print()
print("== 3. GET 复查应为 true ==")
j = (c.get("/api/autostart", headers=H).get_json() or {})
check("enabled=True", j.get("enabled") is True)
check("target 不为空", bool(j.get("target")))

print()
print("== 4. POST 关闭自启 ==")
r = c.post("/api/autostart", json={"enabled": False}, headers=ORIGIN)
j = r.get_json() or {}
check("返回 200", r.status_code == 200, r.status_code)
check("enabled=False", j.get("enabled") is False)
check("注册表值已删除", autostart._read_value() is None)

print()
print("== 5. 跨源请求必须被拦 ==")
evil = {"Host": "127.0.0.1:%s" % A._PORT, "Origin": "http://evil.example.com",
        "Content-Type": "application/json"}
r = c.post("/api/autostart", json={"enabled": True}, headers=evil)
check("跨源 POST 返回 403", r.status_code == 403, r.status_code)
check("注册表没被跨源请求改动", autostart._read_value() is None)

print()
print("== 6. 缺 Origin 但 Host 正确（浏览器同源也会这样带 Referer）==")
r = c.post("/api/autostart", json={"enabled": True},
           headers={"Host": "127.0.0.1:%s" % A._PORT,
                    "Referer": "http://127.0.0.1:%s/" % A._PORT,
                    "Content-Type": "application/json"})
check("带同源 Referer 的 POST 放行", r.status_code == 200, r.status_code)
check("确实写进去了", autostart._read_value() is not None)

# 恢复
print()
print("== 7. 恢复原状 ==")
try:
    if orig_on:
        autostart.enable()
    elif orig_value:
        k = autostart._run_key()
        with k:
            import winreg
            winreg.SetValueEx(k, autostart.VALUE_NAME, 0, winreg.REG_SZ, orig_value)
    else:
        autostart.disable()
    check("已恢复到测试前状态", autostart.is_enabled() == orig_on)
except Exception as e:
    check("恢复没出错", False, repr(e))

print()
if failed:
    print("❌ %d / %d 项未通过" % (failed, total))
    sys.exit(1)
print("✅ 全部 %d 项通过" % total)
