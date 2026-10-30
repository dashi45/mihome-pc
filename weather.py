# -*- coding: utf-8 -*-
"""室外天气 / 空气质量 —— Open-Meteo 免费接口，不需要 API Key。

为什么单独一个模块：
  1. 米家只给室内温湿度，室内外一对比才知道"该不该开窗"；
  2. 天气是外部网络请求，失败率远高于本地设备，必须缓存 + 容错，
     绝不能让一次超时把整个仪表盘拖死（所以全部在后台线程里取，
     取不到就继续用上一次的数据，前端只看到 updated 时间变旧）。

数据源（都是 Open-Meteo，含免费商用额度、国内可直连）：
  - 预报     https://api.open-meteo.com/v1/forecast
  - 空气质量 https://air-quality-api.open-meteo.com/v1/air-quality
  - 地理编码 https://geocoding-api.open-meteo.com/v1/search
  - 首次定位 http://ip-api.com/json/（按出口 IP 猜城市，可随时手改）
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "weather.json")

TTL = 600                 # 天气 10 分钟更新一次就够（Open-Meteo 本身 15 分钟一档）
RETRY_AFTER_FAIL = 120    # 失败后 2 分钟内不重试，避免每次刷新都卡住
TIMEOUT = 8

# WMO 天气代码 -> (中文, 图标名)
# 图标名前端 weather.js 里有对应 SVG，这里只传语义
_WMO = {
    0: ("晴", "sun"),
    1: ("晴间少云", "sun-cloud"),
    2: ("多云", "sun-cloud"),
    3: ("阴", "cloud"),
    45: ("有雾", "fog"),
    48: ("雾凇", "fog"),
    51: ("小毛雨", "drizzle"),
    53: ("毛毛雨", "drizzle"),
    55: ("浓毛雨", "drizzle"),
    56: ("冻毛雨", "sleet"),
    57: ("强冻毛雨", "sleet"),
    61: ("小雨", "rain"),
    63: ("中雨", "rain"),
    65: ("大雨", "rain-2"),
    66: ("冻雨", "sleet"),
    67: ("强冻雨", "sleet"),
    71: ("小雪", "snow"),
    73: ("中雪", "snow"),
    75: ("大雪", "snow-2"),
    77: ("米雪", "snow"),
    80: ("阵雨", "rain"),
    81: ("强阵雨", "rain-2"),
    82: ("暴雨", "rain-2"),
    85: ("阵雪", "snow"),
    86: ("强阵雪", "snow-2"),
    95: ("雷阵雨", "storm"),
    96: ("雷阵雨伴冰雹", "storm"),
    99: ("强雷暴伴冰雹", "storm"),
}

# 中国空气质量分级（HJ 633-2012），用 US AQI 数值分段，和手机天气 App 口径一致
_AQI_LV = (
    (50, "优", "#0E9F72"),
    (100, "良", "#C9A227"),
    (150, "轻度污染", "#E07B39"),
    (200, "中度污染", "#E2564F"),
    (300, "重度污染", "#B0447C"),
    (10 ** 6, "严重污染", "#7C3AED"),
)

_WIND_DIR = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")

_lock = threading.Lock()
_cache = {"at": 0.0, "fail_at": 0.0, "data": None}
_cfg = None


# ---------------- 基础 ----------------
def _qs(d):
    """自己拼查询串：Open-Meteo 是在 URL 解码【之前】按逗号切变量的，
    urlencode 把逗号转成 %2C 之后它就当成一个整体，直接 400。"""
    return urllib.parse.urlencode(d, safe=",")


def _http_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={
        # 有些机房会按 UA 拦 Python-urllib，带个正常 UA 更稳
        "User-Agent": "MiHomePC/1.0 (+local)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _wmo(code):
    try:
        return _WMO.get(int(code), ("未知", "cloud"))
    except Exception:
        return ("未知", "cloud")


def _wind_dir(deg):
    try:
        d = float(deg) % 360
    except Exception:
        return ""
    return _WIND_DIR[int((d + 22.5) // 45) % 8]


def _iso_ts(s, offset_sec):
    """Open-Meteo 给的是不带时区的本地时间（2026-10-24T03:15），
    配合响应里的 utc_offset_seconds 才能还原成真正的 epoch。"""
    try:
        dt = datetime.fromisoformat(str(s))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(seconds=offset_sec or 0)))
    return int(dt.timestamp())


# ---------------- 位置配置 ----------------
def load_cfg():
    global _cfg
    if _cfg is not None:
        return _cfg
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("lat") is not None:
            _cfg = d
            return _cfg
    except Exception:
        pass
    _cfg = {}
    return _cfg


def save_cfg(d):
    global _cfg
    _cfg = d
    try:
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)
    except Exception:
        pass


def detect_place():
    """按出口 IP 猜城市。失败就退回柳州（作者所在），至少能出数据。"""
    try:
        j = _http_json("http://ip-api.com/json/?lang=zh-CN"
                       "&fields=status,city,regionName,country,lat,lon,timezone", timeout=5)
        if j.get("status") == "success" and j.get("lat") is not None:
            name = j.get("city") or j.get("regionName") or "本地"
            if j.get("regionName") and j.get("city") and j["regionName"] not in name:
                name = "%s · %s" % (name, j["regionName"])
            return {"lat": float(j["lat"]), "lon": float(j["lon"]),
                    "name": name, "tz": j.get("timezone") or "Asia/Shanghai",
                    "auto": True}
    except Exception:
        pass
    return {"lat": 24.3255, "lon": 109.415, "name": "柳州", "tz": "Asia/Shanghai",
            "auto": True}


def geocode(name):
    """按城市名查经纬度（支持中文）"""
    q = _qs({"name": name, "count": 1, "language": "zh", "format": "json"})
    j = _http_json("https://geocoding-api.open-meteo.com/v1/search?" + q)
    rs = (j or {}).get("results") or []
    if not rs:
        return None
    r = rs[0]
    label = r.get("name") or name
    extra = r.get("admin1") or ""
    if extra and extra not in label:
        label = "%s · %s" % (label, extra)
    return {"lat": float(r["latitude"]), "lon": float(r["longitude"]),
            "name": label, "tz": r.get("timezone") or "Asia/Shanghai", "auto": False}


def set_place(city=None, lat=None, lon=None, name=None):
    """前端换城市：优先城市名，其次经纬度"""
    if city:
        got = geocode(city)
        if not got:
            raise ValueError("没找到这个城市：%s" % city)
        save_cfg(got)
    elif lat is not None and lon is not None:
        save_cfg({"lat": float(lat), "lon": float(lon),
                  "name": name or "自定义位置", "tz": "Asia/Shanghai", "auto": False})
    else:
        raise ValueError("缺少城市名或经纬度")
    with _lock:
        _cache["at"] = 0.0
        _cache["fail_at"] = 0.0
    return load_cfg()


# ---------------- 取数 ----------------
def _fetch(cfg):
    lat, lon = cfg["lat"], cfg["lon"]
    q = _qs({
        "latitude": lat, "longitude": lon,
        "current": ",".join((
            "temperature_2m", "relative_humidity_2m", "apparent_temperature",
            "is_day", "precipitation", "rain", "weather_code",
            "wind_speed_10m", "wind_direction_10m", "surface_pressure")),
        # 多要一天：曲线左端（24 小时前）也有室外温度可对照
        "hourly": ",".join(("temperature_2m", "relative_humidity_2m",
                            "weather_code", "precipitation_probability", "is_day")),
        "daily": ",".join(("weather_code", "temperature_2m_max", "temperature_2m_min",
                           "sunrise", "sunset", "precipitation_probability_max",
                           "uv_index_max")),
        "timezone": "auto", "past_days": 1, "forecast_days": 7,
    })
    j = _http_json("https://api.open-meteo.com/v1/forecast?" + q)
    off = int(j.get("utc_offset_seconds") or 0)

    cur = j.get("current") or {}
    h = j.get("hourly") or {}
    d = j.get("daily") or {}

    code = cur.get("weather_code")
    text, icon = _wmo(code)
    is_day = cur.get("is_day")
    if not is_day and icon == "sun":
        icon = "moon"

    now = {
        "t": _r(cur.get("temperature_2m"), 1),
        "feels": _r(cur.get("apparent_temperature"), 1),
        "hum": _r(cur.get("relative_humidity_2m"), 0),
        "code": code,
        "text": text,
        "icon": icon,
        "is_day": bool(is_day),
        "wind": _r(cur.get("wind_speed_10m"), 1),
        "wind_dir": _wind_dir(cur.get("wind_direction_10m")),
        "precip": _r(cur.get("precipitation"), 1),
        "pressure": _r(cur.get("surface_pressure"), 0),
        "at": _iso_ts(cur.get("time"), off),
    }

    # ---- 逐时：取"当前小时"前后各一段，前面 24 小时用于和室内曲线对照 ----
    times = h.get("time") or []
    temps = h.get("temperature_2m") or []
    hums = h.get("relative_humidity_2m") or []
    codes = h.get("weather_code") or []
    pops = h.get("precipitation_probability") or []
    days = h.get("is_day") or []
    idx_now = 0
    if times:
        stamp = now["at"] or int(time.time())
        for i, s in enumerate(times):
            ts = _iso_ts(s, off)
            if ts is not None and ts > stamp:
                break
            idx_now = i
    past, hourly = [], []
    for i, s in enumerate(times):
        ts = _iso_ts(s, off)
        if ts is None:
            continue
        v = _r(temps[i], 1) if i < len(temps) else None
        if v is None:
            continue
        if i <= idx_now:
            past.append({"t": ts, "v": v})
        if i >= idx_now and len(hourly) < 24:
            hc = codes[i] if i < len(codes) else None
            ht, hi = _wmo(hc)
            # 夜里 code=0 也是"晴"，图标要换成月亮，否则凌晨三点画个太阳
            if days and i < len(days) and not days[i] and hi == "sun":
                hi = "moon"
            hourly.append({
                "t": ts, "v": v,
                "hum": _r(hums[i], 0) if i < len(hums) else None,
                "code": hc, "text": ht, "icon": hi,
                "pop": _r(pops[i], 0) if i < len(pops) else None,
            })

    # ---- 7 天 ----
    # past_days=1 会让 daily 从"昨天"开始，必须按本地日期找到今天，
    # 否则卡片上"今天"显示的是昨天的最高/最低温。
    dts = d.get("time") or []
    off_dt = datetime.fromtimestamp(now["at"] or time.time(),
                                    timezone(timedelta(seconds=off)))
    today_s = off_dt.date().isoformat()
    i_today = 0
    for i, ds in enumerate(dts):
        if ds == today_s:
            i_today = i
            break

    daily = []
    for i in range(i_today, min(len(dts), i_today + 7)):
        ds = dts[i]
        dc = (d.get("weather_code") or [None] * len(dts))[i]
        dt, di = _wmo(dc)
        try:
            dd = date.fromisoformat(ds)
            dow = dd.weekday()
        except Exception:
            dow = 0
        daily.append({
            "date": ds, "dow": dow,
            "hi": _r((d.get("temperature_2m_max") or [])[i], 0),
            "lo": _r((d.get("temperature_2m_min") or [])[i], 0),
            "code": dc, "text": dt, "icon": di,
            "pop": _r((d.get("precipitation_probability_max") or [])[i], 0),
        })

    def _at(key, i=0):
        arr = d.get(key) or []
        return arr[i] if i < len(arr) else None

    today = {
        "hi": _r(_at("temperature_2m_max", i_today), 0),
        "lo": _r(_at("temperature_2m_min", i_today), 0),
        "sunrise": _at("sunrise", i_today),
        "sunset": _at("sunset", i_today),
        "pop": _r(_at("precipitation_probability_max", i_today), 0),
        "uv": _r(_at("uv_index_max", i_today), 1),
        "code": _at("weather_code", i_today),
    }

    return {"now": now, "today": today, "hourly": hourly,
            "past": past[-26:], "daily": daily,
            "tz": j.get("timezone"), "off": off}


def _r(v, nd=0):
    try:
        if v is None:
            return None
        return round(float(v), nd) if nd else int(round(float(v)))
    except Exception:
        return None


def _fetch_aqi(cfg):
    q = _qs({
        "latitude": cfg["lat"], "longitude": cfg["lon"],
        "current": "pm2_5,pm10,european_aqi,us_aqi", "timezone": "auto"})
    j = _http_json("https://air-quality-api.open-meteo.com/v1/air-quality?" + q)
    cur = j.get("current") or {}
    us = _r(cur.get("us_aqi"), 0)
    lv, color = "—", "#8A8F98"
    if us is not None:
        for edge, name, c in _AQI_LV:
            if us <= edge:
                lv, color = name, c
                break
    return {"us": us, "eu": _r(cur.get("european_aqi"), 0),
            "pm25": _r(cur.get("pm2_5"), 1), "pm10": _r(cur.get("pm10"), 1),
            "lv": lv, "color": color}


def snapshot(force=False):
    """带缓存的全量快照。任何一环失败都退回旧数据，不抛异常给前端。"""
    with _lock:
        now = time.time()
        if not force and _cache["data"] and now - _cache["at"] < TTL:
            return _cache["data"]
        if not force and now - _cache["fail_at"] < RETRY_AFTER_FAIL:
            return _cache["data"] or {"ok": False, "error": "天气接口暂时不可用"}

    cfg = load_cfg()
    if not cfg:
        cfg = detect_place()
        save_cfg(cfg)

    try:
        base = _fetch(cfg)
    except Exception as e:
        with _lock:
            _cache["fail_at"] = time.time()
            if _cache["data"]:
                out = dict(_cache["data"])
                out["stale"] = True
                out["error"] = str(e)
                return out
        return {"ok": False, "error": "取天气失败：%s" % e,
                "place": cfg.get("name"), "lat": cfg["lat"], "lon": cfg["lon"]}

    aqi, aqi_err = None, None
    try:
        aqi = _fetch_aqi(cfg)
    except Exception as e:
        aqi_err = str(e)

    data = {
        "ok": True,
        "place": cfg.get("name") or "本地",
        "auto": bool(cfg.get("auto")),
        "lat": cfg["lat"], "lon": cfg["lon"], "tz": base.get("tz"),
        "updated": int(time.time()),
        "now": base["now"], "today": base["today"],
        "hourly": base["hourly"], "past": base["past"], "daily": base["daily"],
        "aqi": aqi, "aqi_error": aqi_err,
    }
    with _lock:
        _cache["data"] = data
        _cache["at"] = time.time()
        _cache["fail_at"] = 0.0
    return data


def warm():
    """启动时预热一次，第一次打开就不用等网络。"""
    threading.Thread(target=lambda: snapshot(), daemon=True).start()
