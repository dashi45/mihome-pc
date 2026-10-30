# -*- coding: utf-8 -*-
"""验证"启动秒开"的三个关键行为。

真实场景：米家云 device_list 冷启动要 20-30 秒，这段时间界面是空的。
有了设备缓存，页面一打开就能看到上次的设备列表。

用 Flask 测试客户端直接控制后端状态，精确复现"登录还没完成"这一刻：
  - 场景 A：登录未完成 + 有缓存 → 应返回 200 + 缓存设备 + from_cache=true
  - 场景 B：登录未完成 + 无缓存 → 应返回 401（不能瞎编数据）
  - 场景 C：登录已完成        → 应返回真实设备 + from_cache=false

再验证缓存模块自身的健壮性（借鉴参考项目的教训）：
  - 缓存文件损坏 → 当没有缓存，不让程序起不来
  - 缓存版本过期 → 作废重来
  - 缓存太旧     → 不用
"""
import json
import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
os.environ["MIHOME_PORT"] = "8799"

import app as A          # noqa: E402
import devcache          # noqa: E402

# 关键：把缓存文件重定向到临时目录。
# 这个测试要反复写坏缓存、删缓存、造过期缓存——如果直接用生产路径
# devices_cache.json，跑完测试生产缓存里就剩一堆假设备，
# 下次启动界面会先渲染出一堆"测试灯"（真实症状：启动秒开时列表是乱的）。
# 测试必须和真实数据完全隔离。
_TMP_DIR = tempfile.mkdtemp(prefix="mihome_cache_test_")
devcache.CACHE_PATH = os.path.join(_TMP_DIR, "devices_cache.json")


def _cleanup():
    try:
        import shutil
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
    except Exception:
        pass


import atexit            # noqa: E402
atexit.register(_cleanup)

# 记录生产缓存的原始内容，测试结束后比对（守卫见文件末尾）
_PROD_CACHE = os.path.join(BASE, "devices_cache.json")
_prod_before = None
if os.path.isfile(_PROD_CACHE):
    with open(_PROD_CACHE, "rb") as f:
        _prod_before = f.read()

failed = 0


def check(name, cond, detail=""):
    global failed
    if cond:
        print("  ✓ " + name)
    else:
        print("  ✗ " + name + ("  → " + str(detail) if detail else ""))
        failed += 1


c = A.app.test_client()

# 测试客户端默认发 Host: localhost，而守卫要求 Host 是 127.0.0.1 + 当前端口。
# 这不是绕过安全机制——真实浏览器访问 http://127.0.0.1:8799 时就是这么发的。
_HOST = {"Host": "127.0.0.1:%s" % A._PORT}


def api_get(path):
    return c.get(path, headers=_HOST)

print("1. 登录未完成 + 有缓存 → 应立刻给出设备")
# 造一份缓存
fake = [{"did": "d1", "name": "测试灯", "model": "m1", "online": True,
         "room": "客厅", "kind": "light", "ir": False, "switchable": True,
         "on_siid": 2, "on_piid": 1, "parent": None}]
devcache.save(fake, [{"id": "h1", "name": "家", "rooms": []}])

A.S["status"] = "logging_in"
A.S["cached_devices"] = fake
A.S["cached_homes"] = []
A.S["from_cache"] = True

r = api_get("/api/devices")
j = r.get_json()
check("返回 200", r.status_code == 200, r.status_code)
check("from_cache 标记为 true", j.get("from_cache") is True, j.get("from_cache"))
check("设备列表非空", len(j.get("devices") or []) == 1, j.get("devices"))

print("")
print("2. 登录未完成 + 无缓存 → 应返回 401，不能瞎编数据")
A.S["cached_devices"] = None
A.S["from_cache"] = False
r = api_get("/api/devices")
check("返回 401", r.status_code == 401, r.status_code)
check("错误信息是未登录", (r.get_json() or {}).get("error") == "未登录")

print("")
print("3. 登录已完成 → 应返回真实设备，from_cache=false")
A.S["status"] = "ready"
A.S["devices"] = []
A.S["homes"] = []
r = api_get("/api/devices")
j = r.get_json()
check("返回 200", r.status_code == 200, r.status_code)
check("from_cache 为 false", j.get("from_cache") is False, j.get("from_cache"))

print("")
print("4. 缓存模块健壮性（借鉴参考项目的教训：缓存坏掉不能让程序起不来）")
devcache.save(fake, [])
raw = open(devcache.CACHE_PATH, encoding="utf-8").read()
check("正常缓存能读出来", devcache.load() is not None)

# 损坏
with open(devcache.CACHE_PATH, "w", encoding="utf-8") as f:
    f.write("{这不是合法 JSON")
check("损坏的缓存当没有（不抛异常）", devcache.load() is None)

# 版本过期
with open(devcache.CACHE_PATH, "w", encoding="utf-8") as f:
    json.dump({"version": 999, "ts": time.time(), "devices": fake, "homes": []},
              f, ensure_ascii=False)
check("版本对不上就作废", devcache.load() is None)

# 太旧
with open(devcache.CACHE_PATH, "w", encoding="utf-8") as f:
    json.dump({"version": devcache.CACHE_VERSION,
               "ts": time.time() - 999999, "devices": fake, "homes": []},
              f, ensure_ascii=False)
check("过期缓存不用", devcache.load() is None)

# 时间戳在未来（机器时间被调过）
with open(devcache.CACHE_PATH, "w", encoding="utf-8") as f:
    json.dump({"version": devcache.CACHE_VERSION,
               "ts": time.time() + 999999, "devices": fake, "homes": []},
              f, ensure_ascii=False)
check("未来时间戳也当无效（否则会一直命中）", devcache.load() is None)

# 结构不对
with open(devcache.CACHE_PATH, "w", encoding="utf-8") as f:
    json.dump({"version": devcache.CACHE_VERSION, "ts": time.time(),
               "devices": "不是列表", "homes": []}, f, ensure_ascii=False)
check("结构不对就当没有", devcache.load() is None)

# 元素结构不对：有一台设备缺 did 就整份作废
with open(devcache.CACHE_PATH, "w", encoding="utf-8") as f:
    json.dump({"version": devcache.CACHE_VERSION, "ts": time.time(),
               "devices": [{"did": "d1", "name": "好的"}, {"name": "缺 did 的"}],
               "homes": []}, f, ensure_ascii=False)
check("有设备缺 did 就整份作废", devcache.load() is None)

# 全都缺 did，同样作废
with open(devcache.CACHE_PATH, "w", encoding="utf-8") as f:
    json.dump({"version": devcache.CACHE_VERSION, "ts": time.time(),
               "devices": [{"name": "全是假的"}], "homes": []}, f, ensure_ascii=False)
check("设备全是假的就当没有", devcache.load() is None)

# 文件不存在
try:
    os.unlink(devcache.CACHE_PATH)
except OSError:
    pass
check("文件不存在也不抛异常", devcache.load() is None)

print("")
print("5. 原子写：中途被杀不会留半截文件")
devcache.save(fake, [{"id": "h1"}])
check("写完后能读回来", devcache.load() is not None)
leftover = os.path.exists(devcache.CACHE_PATH + ".tmp")
check("没有残留的 .tmp 文件", not leftover)
check("文件是完整 JSON", isinstance(json.load(open(devcache.CACHE_PATH, encoding="utf-8")), dict))

devcache.clear()

# 守卫：这个测试全程只碰临时目录，生产缓存必须原封不动。
_PROD_CACHE = os.path.join(BASE, "devices_cache.json")
_prod_after = None
if os.path.isfile(_PROD_CACHE):
    with open(_PROD_CACHE, "rb") as f:
        _prod_after = f.read()
if _prod_before is not None:
    check("生产缓存文件没被测试碰过", _prod_after == _prod_before,
          "生产缓存被改写了！")
elif _prod_after is None:
    check("生产缓存文件没被测试碰过（本来就没有）", True)
else:
    check("生产缓存文件没被测试碰过（本来就没有，现在不该有）", False,
          "测试凭空造出了生产缓存")

print("")
if failed:
    print("❌ %d 项未通过" % failed)
    sys.exit(1)
print("✅ 全部通过")
