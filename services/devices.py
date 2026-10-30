# -*- coding: utf-8 -*-
"""设备域：类型推断、规格缓存、设备视图、登录/恢复。

改造前这是 app.py 里最大的一块（400 多行）。它对外只暴露几件事：
`get_spec` / `readable_props` / `build_device_view` / `login` / `restore`，
路由层只调这些，不直接碰米家协议。

规格缓存 spec_cache.json 体积近 100KB、一天只变几次，所以进程内常驻，
并按设备型号缓存"可读属性"的解析结果（`_READ_CACHE`）。
"""
import json
import os
import threading

from miservice import MiAccount, MiIOService
import miservice.miaccount as _mi_account
import miservice.miioservice as _mi_io

import config
import logx
import creds
import devcache
import mihttp
from services.cloud import call
from services.cloud import PRIO_IDLE
from services.state import S
from panel import svc_name
from panel import prop_name
from panel import cn_value
from services.state import otp_callback
from services.state import _LOGIN_LOCK
from services.cloud import PRIO_CONTROL
from services.tokens import backup_token

log = logx.get(__name__)


# miservice 自带的 UA_MIIO 还是 iOS-14.4 / 米家 6.0.103。小米已经把这么旧的
# 客户端判成下线，device_list 会返回 `-127 client withdraw`（wait_second=30），
# 表现就是"扫码/密码都成功，但一台设备都拉不到"。同一个 token 换成现代
# Android 米家 UA 后接口立即正常，所以这里在创建 MiIOService 之前覆盖掉。
# 固定 ID 是刻意的：同一台机器每次启动都用同一个客户端身份，避免风控把
# 每次启动当成新设备。
_MI_UA = (
    "Android-15-11.0.701-Xiaomi-23046RP50C-OS2.0.212.0.VMYCNXM-"
    "D7744744F7AF32F0544445285880DD63E47D9BE9-CN-"
    "84A3F44E137B71AE84A3F44E137B71AE-"
    "D7744744F7AF32F0544445285880DD63E47D9BE9-"
    "SmartHome-MI_APP_STORE-"
    "D7744744F7AF32F0544445285880DD63E47D9BE9|"
    "8816080088160800881608008816080088160800|"
    "0123456789abcdef-64"
)
_mi_account.UA_MIIO = _MI_UA
_mi_io.UA_MIIO = _MI_UA


# ---------- 设备类型推断 ----------
def is_ir_device(dev) -> bool:
    """红外遥控在米家里是虚拟设备：did 以 ir. 开头，model 是 miir.*，
    它没有 MIoT 规范，只能通过红外按键接口控制。"""
    did = str(dev.get("did") or "")
    model = str(dev.get("model") or "")
    return did.startswith("ir.") or model.startswith("miir.")


def guess_kind(model: str, name: str) -> str:
    m = (model or "").lower()
    n = name or ""
    if (model or "").startswith("miir."):
        # 红外设备按名字推断家电类型，图标才对得上
        for keys, kind in ((("灯",), "light"), (("空调",), "ac"), (("电视",), "media"),
                           (("风扇", "扇"), "climate"), (("机顶盒",), "media")):
            if any(k in n for k in keys):
                return kind
        return "other"
    rules = [
        (("mphone", "phone.online"), "phone"),
        (("miwear", ".watch", "band", "bracelet"), "wearable"),
        (("scales", ".scale"), "scale"),
        (("fitting", "charger", "powerbank"), "charger"),
        (("light", "lamp", "ceiling", "bulb", "strip"), "light"),
        (("plug", "socket", "powerstrip", "outlet"), "plug"),
        (("camera", "ipc", "cateye"), "camera"),
        (("vacuum", "sweep", "clean"), "vacuum"),
        (("aircondition", "acpartner", "airconditioner", "acm"), "ac"),
        (("curtain", "window", "cover"), "curtain"),
        (("fan",), "fan"),
        (("heater", "humidifier", "purifier", "airfresh"), "climate"),
        (("lock", "door"), "lock"),
        (("sensor", "magnet", "motion", "temp", "humidity"), "sensor"),
        (("tv", "stb", "speaker", "sound", "box"), "media"),
        (("waterpuri", "kettle", "cooker", "ricecooker"), "appliance"),
    ]
    for keys, kind in rules:
        if any(k in m for k in keys):
            return kind
    if any(k in n for k in ("灯", "台灯", "吊灯", "吸顶灯")):
        return "light"
    if any(k in n for k in ("插座", "开关")):
        return "plug"
    if any(k in n for k in ("空调",)):
        return "ac"
    if any(k in n for k in ("窗帘",)):
        return "curtain"
    if any(k in n for k in ("摄像", "门铃")):
        return "camera"
    return "other"


_SWITCH_KINDS = {"light", "plug", "climate", "appliance", "ac", "camera", "fan"}


def load_spec_cache() -> dict:
    if os.path.isfile(config.SPEC_CACHE):
        try:
            with open(config.SPEC_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_spec_cache(cache: dict):
    """原子写：先写 .tmp 再 os.replace。

    借鉴参考项目 app/core/_json_store.py 的做法——直接 open(w) 覆盖目标文件时，
    如果进程正好在写的过程中被杀，spec_cache.json 会剩半截 JSON，
    下次启动直接读不出来（load_spec_cache 吞异常返回 {}，所有设备规范缓存丢失）。
    改成 tmp + replace 后，replace 是原子操作，
    要么是完整旧文件，要么是完整新文件，不存在半截状态。
    """
    tmp = config.SPEC_CACHE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, config.SPEC_CACHE)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass


SPECS = load_spec_cache()
_ON_CACHE = {}
IR_KEYS = {}          # did -> [{id, name, display_name}, ...]
_READ_CACHE = {}      # model -> [(key, siid, piid, labels), ...]
_LIVE_CACHE = {"t": 0.0, "data": None}    # 全量（开关 + 读数）
_QUICK_CACHE = {"t": 0.0, "data": None}   # 只含开关，高频轮询用
# 同一时刻只允许一轮刷新在跑。weak 网络下单轮可能好几秒，
# 不定时器会叠加请求，云端限流后整条链路一起变慢。
_LIVE_BUSY = threading.Lock()
_HIST_CACHE = {}   # hours -> (时间戳, 各设备云端历史点)
_MODEL_ENERGY = {}   # model -> 电量/功率属性定位

# 值得定时读的属性（key 是前端用的字段名）
_READ_PROPS = {
    "temperature": "temp",
    "relative-humidity": "hum",
    "battery-level": "batt",
    "electric-power": "watt",      # 实时功率，插座 / 空调才有
}

# 卡片上要"一眼看到"的调节状态：亮度 / 档位 / 目标温度 / 开合度 / 模式
# 只读，不会下发任何控制；读一次就能让卡片显示"3 档""制冷 27°"
# 摆风单独处理（要和「摇头」按钮指向同一个属性），不在这个表里。
_SUMMARY_PROPS = {
    "brightness": "bright",
    "fan-level": "level",
    "speed-level": "level",
    "target-temperature": "temp_set",
    "position": "pos",
    "percentage": "pos",
    "mode": "mode",
}

# 摆风类属性：卡片上那个「摇头」开关就是它。
# 左右摆风优先（风扇的摇头就是这个），没有的话退到上下摆风（空调常见）。
_SWING_PROPS = ("horizontal-swing", "vertical-swing")

# 只有这些服务里的 temperature/humidity 才是"室温湿度"。
# 插座/充电器也会报 temperature，那是设备自身温度（能到 46℃），不能当室温。
_ENV_SERVICES = {"temperature-humidity-sensor", "air-conditioner", "heater",
                 "humidifier", "air-purifier", "air-fresh"}

# 这些 service 里的 on 才是设备主开关
_ON_SERVICES = {"fan", "light", "air-conditioner", "outlet", "switch",
                "camera-control", "air-purifier", "heater", "humidifier",
                "curtain", "washer", "oven", "kettle", "water-heater"}

# 摆风可能落在主控服务里（风扇的 horizontal-swing），也可能落在 fan-control
# 里（空调伴侣的上下扫风就在那儿），所以这里比 _ON_SERVICES 多认一个。
_SWING_SERVICES = _ON_SERVICES | {"fan-control"}


def get_spec(model):
    """取设备规范，带内存 + 磁盘缓存"""
    if not model:
        return None
    if model in SPECS:
        return SPECS[model]
    try:
        raw = call(S["service"].miot_spec(model), timeout=40)
    except Exception:
        return None
    if isinstance(raw, dict) and "services" in raw:
        SPECS[model] = raw
        save_spec_cache(SPECS)
        return raw
    return None


def find_on_iid(model):
    """从规范里找真正的开关 siid/piid（不同设备不一定是 2-1）"""
    if model and model in _ON_CACHE:
        return _ON_CACHE[model]
    spec = SPECS.get(model)
    if not (isinstance(spec, dict) and spec.get("services")):
        # 规范还没拉到。这里返回 (2, 1) 只是**本次**的兜底值，
        # 绝对不能写进 _ON_CACHE —— 否则规范后来拉到了也不会重算，
        # 之后每一次点开关都会发到错误的属性上，而且这个错误会一直
        # 活到进程退出（比落盘的错误更隐蔽，重启才能好）。
        return (2, 1)
    result = (2, 1)
    for svc in spec.get("services") or []:
        if svc_name(svc) in _ON_SERVICES:
            for p in svc.get("properties") or []:
                if prop_name(p) == "on":
                    result = (svc.get("iid"), p.get("iid"))
                    break
        if result != (2, 1):
            break
    if model:
        _ON_CACHE[model] = result
    return result


def readable_props(model):
    """每台设备值得定时读的属性：主开关 + 温度/湿度/电量 + 卡片摘要。
    返回 [(key, siid, piid, 枚举标签表)]，只有真正能读的才收。"""
    if model in _READ_CACHE:
        return _READ_CACHE[model]
    out, seen = [], set()
    spec = SPECS.get(model)
    loaded = isinstance(spec, dict) and bool(spec.get("services"))
    if loaded:
        for svc in spec.get("services") or []:
            sn = svc_name(svc)
            if sn == "device-information":
                continue
            for p in svc.get("properties") or []:
                pn = prop_name(p)
                if pn == "on":
                    # 只认主控服务的 on，别把指示灯/童锁的 on 当成开关
                    if sn not in _ON_SERVICES:
                        continue
                    key = "on"
                elif pn in ("temperature", "relative-humidity"):
                    # 温湿度只认环境类服务，别把插座的自身温度算进来
                    if sn not in _ENV_SERVICES and "sensor" not in sn:
                        continue
                    key = "temp" if pn == "temperature" else "hum"
                elif pn == "battery-level":
                    key = "batt"
                elif pn == "electric-power":
                    # 插座 / 空调的实时功率（W）
                    key = "watt"
                elif pn in _SWING_PROPS:
                    # 摆风必须和卡片上那个「摇头」按钮指向**同一个**属性：
                    # 同时有左右/上下摆风的设备，读的是 A、按的是 B 的话，
                    # 卡片上的开关会永远对不上（按了没反应，或者状态反着）。
                    if sn not in _SWING_SERVICES:
                        continue
                    want = swing_action(model)
                    if not want or want.get("siid") != svc.get("iid") \
                            or want.get("piid") != p.get("iid"):
                        continue
                    key = "swing"
                elif pn in _SUMMARY_PROPS:
                    # mode 只取主控服务的，避免各种子服务的同名属性撞车
                    if pn == "mode" and sn not in _ON_SERVICES:
                        continue
                    key = _SUMMARY_PROPS[pn]
                else:
                    continue
                ac = p.get("access") or []
                if "read" not in ac and "notify" not in ac:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                labels = {v.get("value"): cn_value(v.get("description") or "")
                          for v in (p.get("value-list") or [])}
                out.append((key, svc.get("iid"), p.get("iid"), labels))
    # 规范还没拉到时**不缓存**：否则这一台设备的实时数据会永久空着
    # （on / 档位 / 温湿度全读不到），只能重启进程才能恢复。
    # 参考 _has_on / find_on_iid 里的同一条教训。
    if loaded:
        _READ_CACHE[model] = out
    return out


def energy_props(model):
    """有电量 / 功率属性的设备才进用电统计。
    返回 {"energy": (siid, piid), "watt": (siid, piid)}"""
    if model in _MODEL_ENERGY:
        return _MODEL_ENERGY[model]
    out = {}
    spec = SPECS.get(model)
    loaded = isinstance(spec, dict) and bool(spec.get("services"))
    if loaded:
        for svc in spec.get("services") or []:
            for p in svc.get("properties") or []:
                pn = prop_name(p)
                if pn == "power-consumption":
                    key = "energy"
                elif pn == "electric-power":
                    key = "watt"
                else:
                    continue
                if key in out:
                    continue
                if "read" not in (p.get("access") or []):
                    continue
                out[key] = (svc.get("iid"), p.get("iid"))
    # 同 readable_props：规范没拉到时不缓存，否则这台设备永远不进用电统计
    if loaded:
        _MODEL_ENERGY[model] = out
    return out


def _has_on(model, kind):
    """有没有主开关（决定卡片上要不要显示开关按钮）。

    返回 True / False / **None**。None 专门表示"规范还没拉到，现在不知道" ——
    注意这和 False（"确认了，这型号没有开关"）是两件事，绝不能混。
    参考项目的 `_json_store` 就是在这里栽过：一次网络抖动把"没读到"当成
    "没有这个能力"落了盘，那台设备的开关从此永久消失。所以这里返回 None，
    由调用方决定"先猜着显示，但不许写缓存"。
    """
    spec = SPECS.get(model)
    if isinstance(spec, dict) and spec.get("services"):
        for svc in spec.get("services") or []:
            if svc_name(svc) in _ON_SERVICES:
                if any(prop_name(p) == "on" for p in svc.get("properties") or []):
                    return True
        return False
    return None      # 规范缺失 = 未确认，不是"没有"


def prefetch_specs():
    """登录后在后台把所有设备规范拉一遍，之后点设备就是秒开"""
    for d in list(S["devices"]):
        model = d.get("model")
        if model and model not in SPECS:
            get_spec(model)
    for d in list(S["devices"]):
        find_on_iid(d.get("model"))
        if is_ir_device(d):
            fetch_ir_keys(str(d.get("did")))


# ---------- 卡片第一层就能点的档位 / 模式 ----------
# 为什么只有这两类：档位和模式是"按一下就到"的离散状态，摆成一排按钮比进详情页
# 快得多。空调的温度得用滑块、灯的亮度是连续量，硬做成按钮反而更难按，所以不进
# 这一层（详情页里该有的都有）。
_QUICK_KINDS = {"fan", "climate"}
_QUICK_MAX_LEVELS = 5
_QUICK_MAX_MODES = 3
# 卡片按钮（档位/模式/摇头）的推导结果缓存，纯内存、活一次就够。
#
# ⚠ 这个名字以前是 `_QUICK_CACHE`，跟上面 :129 那个"只含开关的 live 缓存"
# **重名**，于是这个空 dict 把那个缓存整个覆盖掉了 → routes/live.py 里
# `cache["data"]` 每次都 KeyError → /api/live {quick:1} 永久 500，而前端
# `if (!r.ok) return` 把它静默吞了，表现成"设备状态就是不刷新"。
# 两个缓存语义完全不同（一个存 dict-of-live，一个存 tuple-keyed 按钮列表），
# 改名并保持现状，别再让 import 方摸到同一个名字。
_CARD_CACHE = {}


def _enum_opts(p, limit):
    """value-list → [(值, 中文标签)]，最多 limit 个"""
    out = []
    for v in (p.get("value-list") or [])[:limit]:
        if v.get("value") is None:
            continue
        out.append((v.get("value"),
                    cn_value(v.get("description") or str(v.get("value")))))
    return out


def _range_presets(p):
    """无级风速这种 range 属性：给低/中/高三个点。

    1~100 摆成 100 个按钮显然不行，而"低/中/高"正好是用户心里的三档。
    """
    r = p.get("value-range") or []
    if len(r) < 2:
        return []
    lo, hi = r[0], r[1]
    if hi is None or lo is None or hi <= lo:
        return []
    mid = lo + (hi - lo) / 2.0
    if isinstance(lo, int) and isinstance(hi, int):
        mid = int(round(mid))
    out, seen = [], set()
    for v, t in ((lo, "低"), (mid, "中"), (hi, "高")):
        if v in seen:
            continue
        seen.add(v)
        out.append((v, t))
    return out


def quick_actions(model, kind):
    """卡片上直接能按的档位 / 模式按钮。

    返回 [{name, siid, piid, value, label, grp}]，grp ∈ {"lvl", "mode"}：
    前端靠 grp 区分"当前选中态该怎么判断"（档位看 live.level，模式看 live.mode）。

    全部从 spec 推导，不对某个型号硬编码 —— 换台风扇、加湿器照样能出按钮。
    只认主控服务里的可写属性：指示灯、童锁那类服务也带 mode，但那不是用户
    想在卡片上按的东西。
    """
    key = (model, kind)
    if key in _CARD_CACHE:
        return _CARD_CACHE[key]
    levels, modes = [], []
    spec = SPECS.get(model)
    if kind in _QUICK_KINDS and isinstance(spec, dict):
        for svc in spec.get("services") or []:
            if svc_name(svc) not in _ON_SERVICES:
                continue
            siid = svc.get("iid")
            for p in svc.get("properties") or []:
                if "write" not in (p.get("access") or []):
                    continue
                pn = prop_name(p)
                if pn in ("fan-level", "speed-level") and not levels:
                    opts, grp = _enum_opts(p, _QUICK_MAX_LEVELS), "lvl"
                    if not opts:
                        opts = _range_presets(p)
                elif pn == "mode" and not modes:
                    opts, grp = _enum_opts(p, _QUICK_MAX_MODES), "mode"
                else:
                    continue
                bucket = levels if grp == "lvl" else modes
                for v, t in opts:
                    bucket.append({"name": pn, "siid": siid, "piid": p.get("iid"),
                                   "value": v, "label": t, "grp": grp})
    out = levels + modes
    if isinstance(spec, dict):
        # 规范还没拉到时不缓存：否则这台设备的按钮会永久空着，直到重启
        _CARD_CACHE[key] = out
    return out


def swing_action(model):
    """卡片上那个「摇头」开关指向哪个属性。

    返回 {siid, piid, name} 或 None。全部从 spec 推导，不认型号：
      · 只认主控服务（fan / air-conditioner / heater …）里的摆风属性，
        别的子服务里同名的东西不是用户想按的；
      · 必须是可写 bool —— 摆风角度那种枚举不是开关；
      · 左右摆风优先，没有才用上下摆风。
    规范还没拉到时返回 None（卡片上就不出现这个开关），
    和 quick_actions 一样：拉不到就别硬猜一个 iid 发出去。
    """
    key = ("swing", model)
    if key in _CARD_CACHE:
        return _CARD_CACHE[key]
    spec = SPECS.get(model)
    if not isinstance(spec, dict):
        return None
    found = {}
    for svc in spec.get("services") or []:
        if not isinstance(svc, dict):
            continue
        if svc_name(svc) not in _SWING_SERVICES:
            continue
        for p in svc.get("properties") or []:
            if not isinstance(p, dict):
                continue
            pn = prop_name(p)
            if pn not in _SWING_PROPS or pn in found:
                continue
            if "write" not in (p.get("access") or []):
                continue
            if (p.get("format") or "") != "bool":
                continue
            found[pn] = {"siid": svc.get("iid"), "piid": p.get("iid"), "name": pn}
    result = None
    for pn in _SWING_PROPS:
        if pn in found:
            result = found[pn]
            break
    _CARD_CACHE[key] = result
    return result


def build_device_view(devices, homes):
    """把原始设备列表 + 房间层级 合成前端要的结构"""
    room_of = {}
    for home in homes or []:
        for room in home.get("rooms") or []:
            for did in room.get("dids") or []:
                room_of[str(did)] = room.get("name") or "其他"

    # 红外按键数量要显示在卡片上：第一次没缓存就顺手拉一下（之后走缓存）
    for d in devices or []:
        did = str(d.get("did"))
        if is_ir_device(d) and did not in IR_KEYS:
            fetch_ir_keys(did)

    out = []
    for d in devices or []:
        did = str(d.get("did"))
        model = d.get("model") or ""
        on_siid, on_piid = find_on_iid(model)
        ir = is_ir_device(d)
        kind = guess_kind(model, d.get("name"))
        # 能力判定是**三态**的：True / False / None（规范还没拉到，只能猜）。
        # None 时显示上按设备类型猜一个（不然 spec 还没到的设备连开关都不给，
        # 看着像坏了），但这份结论贴了 _on_guess 标签，不允许进缓存 ——
        # 参考项目踩过这个坑：一次读失败被写成"这台设备没有 on 属性"，
        # 那台设备的开关就永久消失且重启也回不来。
        has_on = _has_on(model, kind)
        on_guess = has_on is None
        if on_guess:
            has_on = kind in _SWITCH_KINDS
        out.append({
            "did": did,
            "name": d.get("name") or "未命名设备",
            "model": model,
            "online": bool(d.get("isOnline")),
            "room": d.get("room_name") or room_of.get(did, "未分组"),
            "kind": kind,
            "ir": ir,
            "ir_keys": len(IR_KEYS.get(did) or []) if ir else 0,
            # 卡片第一层级直接摆出这些自定义按键，点一下就发码
            "ir_key_list": ([{"id": k.get("id"),
                              "name": k.get("display_name") or k.get("name") or "按键"}
                             for k in (IR_KEYS.get(did) or [])][:6] if ir else []),
            # 红外设备是无状态遥控器，卡片上不该给开关
            "switchable": (not ir) and bool(d.get("isOnline")) and has_on,
            # 风扇/取暖器这类：档位、模式直接摆到卡片第一层（和红外按键同一套观感）
            "quick": [] if ir else quick_actions(model, kind),
            # 摇头开关：spec 里有可写的摆风属性才给，红外遥控没有状态不给
            "swing": None if ir else swing_action(model),
            "_on_guess": on_guess,
            "on_siid": on_siid,
            "on_piid": on_piid,
            "parent": d.get("parent_device") or None,
        })
    out.sort(key=lambda x: (not x["online"], x["room"], x["name"]))
    return out


def _save_dev_cache():
    """把当前设备视图写进缓存。

    写的是 build_device_view 的结果（前端直接能用的结构），
    下次启动就能直接渲染，不用等 device_list。

    **三态守卫**：只要有一台设备的开关能力还没确认（规范没拉到），
    这一份就不落盘。原因是缓存是整份替换的 —— 哪怕只写错一台，
    下次启动读到的就是"这台设备没有开关"，而且它自己不会自愈
    （缓存只在没有缓存时才走在线加载）。宁可保留上一份好快照。
    """
    try:
        view = build_device_view(S["devices"], S["homes"])
        unconfirmed = [d.get("name") or d.get("did") for d in view
                       if d.get("_on_guess")]
        if unconfirmed:
            log.info("跳过写设备缓存：%d 台设备的开关能力未确认（%s）"
                  % (len(unconfirmed), "、".join(str(x) for x in unconfirmed[:3])))
            return False
        for d in view:
            d.pop("_on_guess", None)      # 内部标记，不写进文件
        devcache.save(view, S["homes"] or [])
        return True
    except Exception:
        return False


async def _do_login(username, password):
    account = MiAccount(mihttp.make_session(), username, password,
                        token_store=creds.make_token_store(config.TOKEN_PATH),
                        otp_callback=otp_callback)
    ok = await account.login("xiaomiio")
    if not ok:
        raise Exception("登录失败：账号或密码不正确，或账号被风控")
    service = MiIOService(account)
    devices = await service.device_list("full")
    homes = await service.home_list()
    return account, service, devices, homes


def _login_worker(username, password):
    # 和 /api/restore、_auto_restore 共用一把锁：两条链路都会写同一个
    # .mi.token，同时跑会把 token 写坏（然后表现为"莫名其妙需要重新登录"）。
    if not _LOGIN_LOCK.acquire(blocking=False):
        S["status"] = "error"
        S["error"] = "已有登录/恢复在进行中，请稍候"
        return
    try:
        S["status"] = "logging_in"
        S["error"] = None
        account, service, devices, homes = call(_do_login(username, password),
                                                 timeout=180, prio=PRIO_CONTROL)
        S["account"] = account
        S["service"] = service
        S["devices"] = devices
        S["homes"] = homes
        S["status"] = "ready"
        S["restore_failed"] = False
        S["from_cache"] = False
        backup_token()
        _save_dev_cache()
        threading.Thread(target=prefetch_specs, daemon=True).start()
    except Exception as e:
        S["status"] = "error"
        S["error"] = str(e)
    finally:
        _LOGIN_LOCK.release()


async def _do_restore():
    """用已保存的 token 直接拉数据，不需要密码"""
    account = MiAccount(mihttp.make_session(), "", "",
                        token_store=creds.make_token_store(config.TOKEN_PATH),
                        otp_callback=otp_callback)
    service = MiIOService(account)
    devices = await service.device_list("full")
    homes = await service.home_list()
    return account, service, devices, homes


def fetch_ir_keys(did, force=False):
    """红外遥控的按键列表（云端按设备保存，缓存起来）"""
    if not force and did in IR_KEYS:
        return IR_KEYS[did]
    try:
        r = call(S["service"].miio_request("/v2/irdevice/controller/keys", {"did": did}),
                 timeout=30, prio=PRIO_IDLE)
        keys = (r or {}).get("keys") or []
        IR_KEYS[did] = keys
        return keys
    except Exception:
        return IR_KEYS.get(did, [])
