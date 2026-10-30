# -*- coding: utf-8 -*-
"""设备缓存秒开。

借鉴自 MiHome-Windows 的 devices_cache.json 设计，但要按本项目的实际形态改：

参考项目缓存的是「设备对象 + 开关记忆」，本项目缓存的应该是**已经算好的
设备视图**（build_device_view 的输出），因为 on_siid/on_piid 这类字段是算出来的，
重启后重新算一遍要再去拉 spec，而 spec 已经有本地缓存（spec_cache.json），
所以直接存视图更省事。

三个必须遵守的原则（参考项目的注释里踩过坑，这里照抄）：

1. **缓存只加速、不添堵**：读不到、读坏了、版本对不上——一律当没有缓存，
   走在线加载。绝不能因为缓存问题让程序起不来。
2. **结构变了要作废缓存**：加一个字段就得升版本号，否则老缓存里缺字段，
   前端拿到 undefined 会静默出错（参考项目因为上游 API 改格式，
   缓存版本从 2 升到 3 作废重来）。
3. **写失败不阻塞主流程**：缓存只是加速手段，写不进去下次刷新再写。

写法用原子写（tmp + os.replace），进程被杀不会留半截 JSON。
"""

import json
import os
import tempfile
import time

# 路径统一由 config 提供（缓存搬家后不会变成"没有缓存"→每次启动都重新全量拉设备）
import config

CACHE_PATH = config.DEV_CACHE

# 视图结构变了就 +1，旧缓存自动作废
#
# v2（2026-10-25）：开关能力判定改成三态（True/False/None）。v1 时代规范
# 拉不到会按设备类型猜一个结论直接落盘，猜错的那些快照里写着"这台设备
# 没有开关"，读回来就永远不会重新判定 —— 必须整份作废重来。
CACHE_VERSION = 2

# 缓存有效期：超过这个时间就不用了（设备增减、在线状态会变）
MAX_AGE = 6 * 3600


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _atomic_write(path, data):
    """原子写：先写同目录临时文件再 os.replace。

    不用 tempfile.mkstemp 是因为它会在系统临时目录建文件，
    跨盘 os.replace 会失败；这里固定写同目录。
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def load(max_age=MAX_AGE):
    """读缓存的设备视图。返回 (devices, homes, meta) 或 None。

    None 表示"没有可用缓存"，调用方应当走在线加载。
    """
    raw = _read(CACHE_PATH)
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != CACHE_VERSION:
        return None      # 结构变了，旧缓存作废
    ts = raw.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    age = time.time() - ts
    if age < -60 or age > max_age:
        # ts 在未来（机器时间被调过）也当无效，否则会一直命中
        return None
    devices = raw.get("devices")
    homes = raw.get("homes")
    if not isinstance(devices, list) or not isinstance(homes, list):
        return None
    # 元素结构也要合理：每台设备至少得有 did。
    # 只查类型不够——列表里塞一堆缺字段的字典，前端会渲染出一排无名卡片，
    # 比"没有缓存"更难排查。只要有一台不对，整份作废走在线加载。
    for d in devices:
        if not isinstance(d, dict) or not d.get("did"):
            return None
    meta = raw.get("meta")
    return devices, homes, (meta if isinstance(meta, dict) else {})


def save(devices, homes, meta=None):
    """写缓存。失败返回 False，不抛异常。"""
    data = {
        "version": CACHE_VERSION,
        "ts": time.time(),
        "devices": devices,
        "homes": homes,
        "meta": meta or {},
    }
    return _atomic_write(CACHE_PATH, data)


def clear():
    try:
        os.unlink(CACHE_PATH)
    except OSError:
        pass
