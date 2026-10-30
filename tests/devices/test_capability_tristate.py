# -*- coding: utf-8 -*-
"""开关能力判定三态 + 缓存落盘守卫的测试（纯离屏，不联网、不碰生产文件）

要挡住的 bug（参考项目真实踩过，我这个项目当时也有同样的路径）：
  网络抖动 → 某台设备的 miot_spec 没拉到 → "不知道有没有 on 属性"
  → 被当成"确认没有" → 写进 devices_cache.json
  → 下次启动直接读缓存渲染 → **那台设备的开关永久消失，重启也不回来**
  （因为缓存只在"没有缓存"时才走在线加载，错的快照不会自愈）

这里用假 SPECS 覆盖三条路径：
  1. 规范缺失 → 判定必须是 None（未确认），不是 False（确认没有）
  2. 规范缺失时 find_on_iid 不许把兜底值记住（否则点开关发错属性）
  3. 有未确认设备时整份缓存不落盘，且上一份好快照不被破坏

跑法：.venv\\Scripts\\python.exe -u tests/tristate_test.py
"""
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

import app        # noqa: E402
import devcache   # noqa: E402

PASS = FAIL = 0


def ok(cond, msg, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ %s" % msg, flush=True)
    else:
        FAIL += 1
        print("  ❌ %s  %s" % (msg, extra), flush=True)


# ---------- 隔离：缓存路径重定向到临时目录 ----------
SANDBOX = tempfile.mkdtemp(prefix="mihome-tristate-")
PROD_CACHE = devcache.CACHE_PATH
devcache.CACHE_PATH = os.path.join(SANDBOX, "devices_cache.json")

SPEC_WITH_ON = {
    "services": [
        {"iid": 2, "type": "urn:miot-spec-v2:service:light:00007802:test",
         "properties": [{"iid": 1, "type": "urn:miot-spec-v2:property:on:00000006:test"}]}
    ]
}
SPEC_NO_ON = {
    "services": [
        {"iid": 2, "type": "urn:miot-spec-v2:service:device-information:00007801:test",
         "properties": [{"iid": 1, "type": "urn:miot-spec-v2:property:manufacturer:00000001:test"}]},
        {"iid": 3, "type": "urn:miot-spec-v2:service:temperature-humidity-sensor:00007809:test",
         "properties": [{"iid": 1, "type": "urn:miot-spec-v2:property:temperature:00000020:test"}]},
    ]
}

print("=== 0. 隔离检查 ===")
ok(devcache.CACHE_PATH != PROD_CACHE, "缓存路径已重定向到临时目录")
ok(devcache.CACHE_VERSION == 2, "缓存版本已升到 2（作废可能被污染的 v1 快照）")

print("\n=== 1. _has_on 的真值表 ===")
app.SPECS.clear()
app.SPECS.update({"test.light.v1": SPEC_WITH_ON, "test.sensor.v1": SPEC_NO_ON})

ok(app._has_on("test.light.v1", "light") is True, "规格里有 on → True")
ok(app._has_on("test.sensor.v1", "sensor") is False, "规格里确认没有 on → False")
ok(app._has_on("test.unknown.v9", "light") is None,
   "**规格缺失 → None（未确认），不是 False**",
   repr(app._has_on("test.unknown.v9", "light")))

print("\n=== 2. find_on_iid 不许记住兜底值 ===")
app._ON_CACHE.clear()
app.SPECS.pop("test.unknown.v9", None)
got = app.find_on_iid("test.unknown.v9")
ok(got == (2, 1), "规格缺失时本次返回兜底 (2,1)", repr(got))
ok("test.unknown.v9" not in app._ON_CACHE,
   "**兜底值没有被写进内存缓存**（否则规格later到了也不会重算）")

# 规格"后来"拉到了：必须能给出正确答案，不能被前面的兜底值卡住
app.SPECS["test.unknown.v9"] = SPEC_WITH_ON
ok("test.unknown.v9" not in app._ON_CACHE, "重算前缓存仍然是空的")
ok(app.find_on_iid("test.unknown.v9") == (2, 1), "有 on 时 siid/piid = 2/1")
ok("test.unknown.v9" in app._ON_CACHE, "确认过的结果才允许记住")

# 有 on 但 iid 不是 2/1 的情况
spec_odd = {"services": [
    {"iid": 3, "type": "urn:miot-spec-v2:service:outlet:00007805:test",
     "properties": [{"iid": 4, "type": "urn:miot-spec-v2:property:on:00000006:test"}]}]}
app.SPECS["test.odd.v1"] = spec_odd
ok(app.find_on_iid("test.odd.v1") == (3, 4), "非 2/1 的正确 siid/piid 能被找到")

print("\n=== 3. 视图里未确认的设备要打标记 ===")
app._ON_CACHE.clear()
app.SPECS.clear()
app.SPECS["test.light.v1"] = SPEC_WITH_ON

DEVS = [
    {"did": "d1", "model": "test.light.v1", "name": "客厅灯", "isOnline": True},
    {"did": "d2", "model": "yeelink.light.ghost", "name": "神秘吊灯", "isOnline": True},
]
HOMES = [{"rooms": [{"name": "客厅", "dids": ["d1", "d2"]}]}]
view = app.build_device_view(DEVS, HOMES)
by = {d["did"]: d for d in view}

ok(by["d1"]["_on_guess"] is False, "规格在的设备：不标 guess")
ok(by["d1"]["switchable"] is True, "规格在的设备：switchable=True")
ok(by["d2"]["_on_guess"] is True, "规格缺失的设备：标记 _on_guess=True")
ok(by["d2"]["switchable"] is True,
   "但显示上仍按类型猜（灯 → 有开关），界面上不会突然少一个开关")

print("\n=== 4. 有未确认设备时不许落盘 ===")
app.S["devices"] = DEVS
app.S["homes"] = HOMES
ok(not os.path.exists(devcache.CACHE_PATH), "写之前没有缓存文件")

# 先造一份"上一份好快照"
good_view = [dict(by["d1"], _on_guess=False)]
devcache.save(good_view, HOMES)
ok(os.path.exists(devcache.CACHE_PATH), "上一份好快照已存在")
before = open(devcache.CACHE_PATH, encoding="utf-8").read()

wrote = app._save_dev_cache()
ok(wrote is False, "有未确认设备 → _save_dev_cache 返回 False（不写）")
after = open(devcache.CACHE_PATH, encoding="utf-8").read()
ok(after == before, "**上一份好快照原封不动（没有被半成品覆盖）**")

print("\n=== 5. 全部确认后才写，且不写内部标记 ===")
app.SPECS["yeelink.light.ghost"] = SPEC_WITH_ON
view2 = app.build_device_view(DEVS, HOMES)
ok(all(not d["_on_guess"] for d in view2), "现在两台都确认了")
wrote = app._save_dev_cache()
ok(wrote is True, "全部确认 → 写缓存成功")

raw = json.load(open(devcache.CACHE_PATH, encoding="utf-8"))
ok(raw.get("version") == 2, "落盘的版本号是 2")
saved = raw.get("devices") or []
ok(len(saved) == 2, "两台设备都写进去了")
ok(all("_on_guess" not in d for d in saved), "内部标记 _on_guess 没有写进文件")
ok(all("switchable" in d for d in saved), "switchable 字段完整")

print("\n=== 6. 旧版本缓存必须被拒绝 ===")
raw["version"] = 1
json.dump(raw, open(devcache.CACHE_PATH, "w", encoding="utf-8"), ensure_ascii=False)
ok(devcache.load() is None, "v1 快照读不出来（强制走在线加载重算）")

print("\n=== 7. 守卫：生产缓存文件没被碰过 ===")
ok(os.path.abspath(devcache.CACHE_PATH) != os.path.abspath(PROD_CACHE),
   "所有写入都落在临时目录", devcache.CACHE_PATH)

# ---- 清理 ----
devcache.CACHE_PATH = PROD_CACHE
shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n" + "=" * 46)
print("通过 %d / 失败 %d" % (PASS, FAIL))
if FAIL:
    print("❌ 有失败项")
    sys.exit(1)
print("✅ 全部通过")
