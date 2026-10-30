# -*- coding: utf-8 -*-
"""
把 MIoT spec 翻译成前端能直接渲染的控制面板描述。
所有设备型号共用同一套逻辑，不针对单一型号硬编码。
"""

# 属性名 -> (显示名, 单位, 类型提示)
PROP_LABELS = {
    "on": ("开关", "", "bool"),
    "brightness": ("亮度", "%", "range"),
    "color-temperature": ("色温", "K", "range"),
    "color": ("颜色", "", "skip"),
    "mode": ("模式", "", "enum"),
    "fan-level": ("风速档位", "", "enum"),
    "speed-level": ("无级风速", "", "range"),
    "horizontal-swing": ("左右摆风", "", "bool"),
    "vertical-swing": ("上下摆风", "", "bool"),
    "horizontal-swing-included-angle": ("摆风角度", "°", "enum"),
    "target-temperature": ("目标温度", "°C", "range"),
    "temperature": ("当前温度", "°C", "read"),
    "relative-humidity": ("湿度", "%", "read"),
    "pm2.5-density": ("PM2.5", "μg/m³", "read"),
    "battery-level": ("电量", "%", "read"),
    "off-delay-time": ("延时关机", "分钟", "range"),
    "volume": ("音量", "%", "range"),
    "position": ("开合度", "%", "range"),
    "percentage": ("开合度", "%", "range"),
    "motor-control": ("电机方向", "", "enum"),
    "night-shot": ("夜视模式", "", "enum"),
    "motion-tracking": ("移动追踪", "", "bool"),
    "recording-mode": ("录制模式", "", "enum"),
    "wdr-mode": ("宽动态", "", "bool"),
    "glimmer-full-color": ("微光全彩", "", "bool"),
    "time-watermark": ("时间水印", "", "bool"),
    "image-rollover": ("画面翻转", "°", "enum"),
    "alarm": ("报警", "", "bool"),
    "physical-controls-locked": ("童锁", "", "bool"),
    "mode-control": ("工作模式", "", "enum"),
    "target-humidity": ("目标湿度", "%", "range"),
    "water-level": ("水位", "%", "read"),
    "filter-life-level": ("滤芯剩余", "%", "read"),
    "air-quality": ("空气质量", "", "read"),
}

# 特定 service 下的属性要用专属标签（避免三个 on 撞车）
SERVICE_OVERRIDES = {
    ("indicator-light", "on"): ("指示灯", "", "bool"),
    ("physical-controls-locked", "physical-controls-locked"): ("童锁", "", "bool"),
    ("alarm", "alarm"): ("报警", "", "bool"),
    ("camera-control", "on"): ("摄像头开关", "", "bool"),
    ("fan-control", "fan-level"): ("风速档位", "", "enum"),
    ("fan-control", "vertical-swing"): ("上下摆风", "", "bool"),
    ("off-delay-time", "off-delay-time"): ("延时关机", "分钟", "range"),
}

# 只在这些 service 里找属性（过滤掉设备信息、调试服务等噪音）
CORE_SERVICES = {
    "light", "fan", "fan-control", "air-conditioner", "heater", "humidifier",
    "air-purifier", "curtain", "motor-control", "camera-control", "indicator-light",
    "motion-detection", "speaker", "off-delay-time", "physical-controls-locked",
    "alarm", "screen", "television", "washer", "oven", "kettle", "water-heater",
    "vacuum", "pet-feeder", "air-fresh", "temperature-humidity-sensor",
}

# 枚举值翻译
VALUE_LABELS = {
    "Auto": "自动", "Cool": "制冷", "Dry": "除湿", "Heat": "制热", "Fan": "送风",
    "Low": "低", "Medium": "中", "High": "高",
    "Straight Wind": "直吹风", "Natural Wind": "自然风",
    "Level1": "1 档", "Level2": "2 档", "Level3": "3 档", "Level4": "4 档",
    "Level5": "5 档", "Level6": "6 档",
    "On": "开", "Off": "关",
    "Normal": "正常", "Not Exist": "无卡", "Full": "已满",
    "All Record": "全程录制", "Motion Record": "移动侦测录制", "No Record": "不录制",
    "Low": "低", "High": "高",
    "Close": "关闭", "Slow": "慢", "Fast": "快",
    "Manual": "手动", "Smart": "智能",
}

# 摄像头相关 service
CAMERA_STREAM_SERVICES = {
    "camera-stream-for-google-home": {"start": ("start-hls-stream", "stream-address", "stream-auth-token")},
    "camera-stream-for-amazon-alexa": {"start": ("start-rtsp-stream", "stream-address", None)},
}

# ---------------- 界面层级 ----------------
# 主调节项（能直接用滑块/步进器调的物理量）：越靠前越重要
PRIMARY_PROPS = {"brightness", "color-temperature", "fan-level", "speed-level",
                 "target-temperature", "position", "percentage", "volume"}
# 工作模式
MODE_PROPS = {"mode", "mode-control"}
# 摆风 / 角度这类辅助动作
SWING_PROPS = {"horizontal-swing", "vertical-swing",
               "horizontal-swing-included-angle"}
# 日常几乎不动的设置项，默认收进「更多设置」
EXTRA_SERVICES = {"indicator-light", "physical-controls-locked", "alarm",
                  "off-delay-time", "screen", "off-delay"}


def control_weight(item: dict) -> int:
    name = item.get("name") or ""
    svc = (item.get("key") or "").split(":")[0]
    # 指示灯/童锁/报警这些服务里的 on 也是 on，但属于"平时不动"的设置
    if svc in EXTRA_SERVICES:
        return 90
    if name == "on":
        return 0
    if svc in EXTRA_SERVICES:
        return 90
    if name in PRIMARY_PROPS:
        return 10
    if name in MODE_PROPS:
        return 20
    if name in SWING_PROPS:
        return 30
    return 40


def svc_name(svc: dict) -> str:
    parts = (svc.get("type") or "").split(":")
    return parts[3] if len(parts) > 3 else ""


def prop_name(p: dict) -> str:
    parts = (p.get("type") or "").split(":")
    return parts[3] if len(parts) > 3 else ""


def cn_value(desc: str) -> str:
    return VALUE_LABELS.get(desc, desc)


def build_panel(spec: dict):
    """
    返回 (controls, camera)
    controls: [{siid, piid, key, label, type, unit, options, min, max, step, writable}]
    camera: None 或 {siid, aiid, stream_siid, addr_piid}
    """
    if not isinstance(spec, dict) or "services" not in spec:
        return [], None

    controls = []
    camera = None
    seen = set()

    for svc in spec.get("services") or []:
        sname = svc_name(svc)
        siid = svc.get("iid")

        # 找摄像头推流服务
        if sname in CAMERA_STREAM_SERVICES:
            conf = CAMERA_STREAM_SERVICES[sname]
            start_act, addr_prop, token_prop = conf["start"]
            aiid = None
            for a in svc.get("actions") or []:
                if prop_name(a) == start_act:
                    aiid = a.get("iid")
                    break
            addr_piid = None
            token_piid = None
            for p in svc.get("properties") or []:
                pn = prop_name(p)
                if pn == addr_prop:
                    addr_piid = p.get("iid")
                elif pn == token_prop:
                    token_piid = p.get("iid")
            if aiid and addr_piid:
                camera = {
                    "siid": siid, "aiid": aiid,
                    "addr_piid": addr_piid, "token_piid": token_piid,
                    "proto": "hls" if "google" in sname else "rtsp",
                }
            continue

        if sname not in CORE_SERVICES:
            continue

        for p in svc.get("properties") or []:
            pname = prop_name(p)
            label, unit, ctype = SERVICE_OVERRIDES.get(
                (sname, pname), PROP_LABELS.get(pname, (None, "", None))
            )
            if not label or ctype == "skip":
                continue

            writable = "write" in (p.get("access") or [])
            if not writable and ctype not in ("read",):
                continue

            key = "%s:%s" % (sname, pname)
            if key in seen:
                continue
            seen.add(key)

            fmt = p.get("format") or ""
            item = {
                "siid": siid, "piid": p.get("iid"),
                "key": key, "name": pname, "label": label,
                "unit": unit, "writable": writable,
                "type": ctype,
            }
            if writable and (ctype == "bool" or fmt == "bool"):
                item["type"] = "bool"
            elif p.get("value-list") and writable:
                item["type"] = "enum"
                item["options"] = [
                    {"v": v.get("value"), "label": cn_value(v.get("description") or str(v.get("value")))}
                    for v in p["value-list"]
                ]
            elif p.get("value-range") and writable:
                r = p["value-range"]
                item["type"] = "range"
                item["min"], item["max"], item["step"] = r[0], r[1], (r[2] if len(r) > 2 else 1)
                if isinstance(item["step"], float) and item["step"] < 1:
                    item["float"] = True
            elif ctype == "read" or not writable:
                # 只读属性（温度、湿度、电量…）只显示数值，绝不能变成可拖动的滑块
                item["type"] = "read"
            else:
                continue   # string 等复杂类型先不渲染

            item["weight"] = control_weight(item)
            item["group"] = "extra" if item["weight"] >= 90 else "main"
            item["_ord"] = len(controls)
            controls.append(item)

    # 排序：开关最前 → 主调节 → 模式 → 摆风 → 其余；同权重保持 spec 顺序
    controls.sort(key=lambda c: (c["weight"], c["_ord"]))
    for c in controls:
        c.pop("_ord", None)
    # 只读项排在最后
    controls.sort(key=lambda c: 1 if c["type"] == "read" else 0)
    return controls, camera
