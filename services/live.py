# -*- coding: utf-8 -*-
"""实时数据采集：批量读属性、组装全量/快速视图。

这是被优化得最狠的一段，值得说清楚为什么是现在这个样子：

  · 云端 `/miotspec/prop/get` 的 params 本来就是**数组**，一次能带多台设备。
    miservice 的封装把它拆成单 did，于是"刷新一轮"变成 11 个 HTTP 往返。
    这里直接发原始数组（`_batch_read`），整张属性表压成 1~2 个请求。
  · 前端有两个节奏：高频轮询只要开关（`quick`），低频才要读数。
    所以有 `_LIVE_CACHE` 和 `_QUICK_CACHE` 两份缓存，各按自己的 TTL 复用。
"""
import time

import netguard
from services.state import S
from services.cloud import _NG
from services.cloud import _NG_BRK
from services.devices import is_ir_device
from services.devices import readable_props
from services.cloud import call


# 米家云 /miotspec/prop/get 的 params 本身就是**数组**，一次请求能带多台设备。
# miservice 的 miot_get_props 把它封成单 did，于是"一次全量刷新"变成了每台
# 设备一个 HTTP 往返：11 台在线设备 = 11 个请求，还得靠信号量压并发。
# 这里直接走 miot_request 发原始数组，把整张属性表压成 1~2 个请求。
#
# 分批按**设备数**算：云端限制的是单次请求里的设备量级，同一台设备的多个
# 属性共用一个 did，摊平后 15 台（几十个 params）是安全的。
_BATCH_DEVS = 15


# 上一轮批量读的统计：多少台设备、多少个属性、发了几次请求、耗时多久。
# 挂在 /api/status 上暴露出去 —— 否则"请求数降没降"只能靠猜。
# 这是可观测性，不是调试残留：以后有人问"一轮刷新打几次米家云"，看这里就行。
_BATCH_STAT = {"t": 0.0, "devices": 0, "params": 0, "requests": 0,
               "failed": 0, "ms": 0}


async def _batch_read(plan):
    """一次请求读回多台设备的属性。

    plan:  [(did, [(siid, piid), ...]), ...]
    返回:  {did: {(siid, piid): 值}}

    读失败、设备离线、没有该属性的条目**不进结果**——调用方按"取不到就是
    没有"处理，别记成 None 再缓存（那会把"这次没读到"永久变成"这台设备
    没有这个能力"）。

    回包**按 (did, siid, piid) 回填**，不按顺序 zip：同一台多路设备（多键
    面板 / 多路插座）有多个属性落在同一个 did 上，只按 did 或只按顺序对齐
    都可能把 A 路的值写到 B 路上。仍取不到时退回按顺序兜底，兼容个别
    回包不带 siid/piid 的老型号。
    """
    out = {}
    devices = params_all = requests = failed = 0
    last_err = None
    started = time.time()
    for start in range(0, len(plan), _BATCH_DEVS):
        chunk = plan[start:start + _BATCH_DEVS]
        params = [{"did": did, "siid": siid, "piid": piid}
                  for did, iids in chunk for siid, piid in iids]
        if not params:
            continue
        try:
            # 一批最多重试一次、总预算 12 秒：既扛得住一次抖动，
            # 又不会把调用方（/api/live 的 25 秒）吃穿。
            rets = await netguard.a_run(
                lambda: S["service"].miot_request("prop/get", params),
                attempts=2, health=_NG, breaker=_NG_BRK,
                what="prop/get", budget=12)
        except Exception as e:
            # 单批失败**不拖垮整轮**：这一批当"没读到"跳过，其余设备照常显示。
            # 原来是整轮抛出去，一个抖动就让整页设备全灰。
            failed += 1
            last_err = e
            continue
        rets = rets if isinstance(rets, list) else []
        devices += len(chunk)
        params_all += len(params)
        requests += 1
        # 云端按请求顺序返回（miservice 自己就是按序 zip 的），先认顺序；
        # 再用 (did, siid, piid) 索引纠偏，防止个别回包顺序不稳时串到别的设备
        by_key = {}
        for item in rets:
            if isinstance(item, dict):
                by_key[(str(item.get("did")), item.get("siid"),
                        item.get("piid"))] = item
        for index, req in enumerate(params):
            item = by_key.get((req["did"], req["siid"], req["piid"]))
            if item is None and index < len(rets) and isinstance(rets[index], dict):
                item = rets[index]
            if item is not None and item.get("code") == 0:
                out.setdefault(req["did"], {})[(req["siid"], req["piid"])] = \
                    item.get("value")
    if requests or failed:
        _BATCH_STAT.update(t=time.time(), devices=devices, params=params_all,
                           requests=requests, failed=failed,
                           ms=int((time.time() - started) * 1000))
    if failed and not requests:
        # 全部批次都失败：这不是"某台设备抖了一下"，是整条链路断了
        # （断网 / 登录态失效 / 米家云挂了）。必须抛出去 ——
        # /api/live 要能报错，看门狗要能看见，而不是安静地返回空数据。
        raise last_err if last_err is not None else RuntimeError("批量读取全部失败")
    return out


def _collect_live(quick=False):
    """拉一轮所有在线设备的实时属性。

    /api/live 和后台补采线程共用同一份逻辑 —— 后台没有 request 上下文，
    不能直接调路由函数，所以把这层抽出来。

    quick=True 时每台设备只读开关那一个属性（plan 里只有一条），
    整轮就十几个 param，是高频轮询能用得起的重量。

    整轮只发 1~2 个请求（见 _batch_read）。原来每台设备一个请求时，11 台
    就是 11 个往返 + 8 路并发抢限流；现在压成一批，顺带把"网络差时后几台
    设备要等前一屏返回"的排队也消掉了。
    """
    async def _gather():
        plan = []      # [(did, [(siid, piid), ...]), ...]
        metas = {}     # did -> readable_props 的原始列表
        for d in S["devices"]:
            if not d.get("isOnline") or is_ir_device(d):
                continue
            did = str(d.get("did"))
            props = readable_props(d.get("model"))
            if quick:
                # 只留主开关。不支持开关的设备（温度计、门磁、摄像头）
                # 自然被筛掉，不会白占一个 param
                props = [p for p in props if p[0] == "on"]
            if not props:
                continue
            plan.append((did, [(si, pi) for _, si, pi, _ in props]))
            metas[did] = props
        if not plan:
            return {}

        got = await _batch_read(plan)
        result = {}
        for did, iids in plan:
            vals = got.get(did) or {}
            out = {}
            for (key, si, pi, labels) in metas[did]:
                v = vals.get((si, pi))
                if v is None:
                    continue
                if key == "on":
                    out["on"] = bool(v)
                elif key == "mode":
                    out["mode"] = v
                    # 顺带把枚举翻译好，卡片上才能显示"制冷"而不是 1
                    if labels.get(v):
                        out["mode_t"] = labels[v]
                else:
                    out[key] = round(v, 1) if isinstance(v, float) else v
            result[did] = out
        return result

    return call(_gather(), timeout=25)


_DEV_META = {"t": 0.0, "m": {}}


def _dev_meta():
    """did -> {name, room}，事件里写"客厅 · 台灯"比裸 did 好认。
    缓存 5 分钟：房间结构不会秒变，而 /api/live 是 20 秒一次的热路径。"""
    now = time.time()
    if _DEV_META["m"] and now - _DEV_META["t"] < 300:
        return _DEV_META["m"]
    room_of = {}
    for home in S.get("homes") or []:
        for room in home.get("rooms") or []:
            for rid in room.get("dids") or []:
                room_of[str(rid)] = room.get("name") or ""
    m = {}
    for d in S["devices"]:
        did = str(d.get("did"))
        m[did] = {"name": d.get("name") or did,
                  "room": d.get("room_name") or room_of.get(did, "")}
    _DEV_META["t"], _DEV_META["m"] = now, m
    return m
