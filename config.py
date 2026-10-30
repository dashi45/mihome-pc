# -*- coding: utf-8 -*-
"""集中配置：所有落盘路径、端口、目录常量都从这里取。

## 为什么要这个模块

改造前，路径散落在 app.py / launcher.py / devcache.py / weather.py / notify.py /
extsensors.py / evtlog.py / scheduler.py 各自写一遍 `os.path.join(BASE, "...")`，
launcher.py 里还硬编码了一个绝对路径。换台机器、换个目录就得改源码。
现在只有这里知道"文件放在哪"，其他模块问它要。

## 三个目录概念

    BASE        程序所在目录（打包后是 exe 所在目录，源码运行是项目根）
    CONFIG_DIR  <BASE>/config —— **配置**（用户偏好、账号、摄像头）
    RUNTIME_DIR <BASE>/runtime —— **运行时缓存**（设备缓存、规格缓存、事件库）

这两类刻意分开：清缓存不会把用户的偏好和登录态一起清掉，备份时也能只挑一类。

## 路径解析顺序（向后兼容）

每个文件都按 `<CONFIG_DIR>/<name>` → `<BASE>/<name>` 的顺序找，
**先存在的那个说了算**。所以：

  · 什么都不做 → 行为和改造前完全一致（文件还在 BASE 根下）
  · 跑一次 `python tools/migrate_config.py` → 全部搬进 config/ + runtime/

迁移是显式动作，不在 import 时偷偷做 —— 导入时移文件在"程序正在跑"的时候
会把别人正打开的文件挪走。

## 怎么改路径

一律用 `config.XXX_PATH` 这种属性访问，不要 `from config import XXX_PATH`。
用属性访问，测试里 `app.TOKEN_PATH = <临时目录>` 才能真的换掉（见 app.py 的
兼容层说明）；`from ... import` 拿到的是 import 那一刻的字符串副本，改了不生效。
"""
import os

# ---------------------------------------------------------------- 目录

def _detect_base():
    """程序根目录。打包后 sys.frozen 为真，取 exe 所在目录。

    允许 MIHOME_HOME 覆盖：把数据放到别的盘（比如 SSD 腾空间）时不用改源码。
    """
    env = os.environ.get("MIHOME_HOME", "").strip()
    if env:
        return os.path.abspath(env)
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE = _detect_base()
CONFIG_DIR = os.path.join(BASE, "config")
RUNTIME_DIR = os.path.join(BASE, "runtime")
STATIC_DIR = os.path.join(BASE, "static")
CHARGER_DIR = os.path.join(BASE, "charger")


def _resolve(name, kind="config"):
    """按 config/ → 根目录 的顺序找一个已存在的文件，返回它的完整路径。

    两个位置都没有时返回"将来应该在的位置"（根目录），保持旧行为：
    调用方第一次写文件时还是写到老地方，不会因为升级而突然找不到数据。
    """
    if kind == "config":
        cands = (os.path.join(CONFIG_DIR, name), os.path.join(BASE, name))
    else:
        cands = (os.path.join(RUNTIME_DIR, name), os.path.join(BASE, name))
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[1]


# ---------------------------------------------------------------- 凭据（config）

TOKEN_PATH = _resolve(".mi.token")
TOKEN_BAK = _resolve(".mi.token.bak")
CAM_SECRET = _resolve("cam_secret.json")

# ---------------------------------------------------------------- 用户偏好（config）

CAM_CFG = _resolve("cam.json")
WEATHER_CFG = _resolve("weather.json")
NOTIFY_CFG = _resolve("notify.json")
EXT_CFG = _resolve("ext.json")
EXT_HIST = _resolve("ext_hist.json")
CLOSE_PREF = _resolve("ui_close_pref.json")
SCHEDULES = _resolve("schedules.json")

# ---------------------------------------------------------------- 运行时缓存（runtime）

EVT_DB = _resolve("evt.db", "runtime")
DEV_CACHE = _resolve("devices_cache.json", "runtime")
SPEC_CACHE = _resolve("spec_cache.json", "runtime")
ENERGY_DAYS = _resolve(".energy_days.json", "runtime")

# ---------------------------------------------------------------- 外部资源位置
# 硬编码散在各模块里的路径都收在这里：换机器/换盘只需要动 config。
# （camhls 用的 HLS 分片目录和 ffmpeg 二进制）
CAMLIVE_DIR = os.path.join(BASE, "_camhls")
FFMPEG = os.path.join(BASE, "go2rtc", "ffmpeg.exe")
GO2RTC_DIR = os.path.join(BASE, "go2rtc")

# ---------------------------------------------------------------- 服务地址 / 端口

PORT = int(os.environ.get("MIHOME_PORT", "8765"))
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# go2rtc 转流服务：HTTP API 已关闭，只走 RTSP 入口
RTSP_BASE = os.environ.get("GO2RTC_RTSP", "rtsp://127.0.0.1:8554")

# CUKTECH 充电器蓝牙桥（按需启动）
CHARGER_PORT = int(os.environ.get("CHARGER_PORT", "8000"))

# 拉起子进程时不要控制台窗口（CREATE_NO_WINDOW | DETACHED_PROCESS）
# 充电器桥和 pythonw 重启都要用；定义在这里，避免谁再写一遍魔数。
NO_WIN = 0x08000000 | 0x00000008

# ---------------------------------------------------------------- 日志

LOG_DIR = os.path.join(BASE, "logs")
LOG_PATH = os.path.join(LOG_DIR, "mihome.log")


def set_log_dir(path):
    """换日志目录（进程内生效，只改 LOG_DIR / LOG_PATH 两个值）。

    为什么需要：打包版 launcher 的 BASE 是 exe 所在目录（dist\米家），而后端
    app.py 是以源码身份跑的（BASE = 源码目录）。不统一的话两边各写一份同名
    mihome.log，时间线对不上，排查时要同时翻两个文件；onedir 升级是整目录
    替换，还会把 launcher 那份连同崩溃现场一起覆盖掉。

    所以 launcher 一找到源码目录就调这里，把日志收拢到 <源码>/logs ——
    config/ 和 runtime/ 也在那儿，升级不会动它。
    """
    global LOG_DIR, LOG_PATH
    LOG_DIR = os.path.abspath(path)
    LOG_PATH = os.path.join(LOG_DIR, "mihome.log")
    return LOG_DIR


# ---------------------------------------------------------------- 迁移

#: 运行时缓存与用户偏好的归属，迁移脚本按这张表搬
LAYOUT = {
    "config": (CONFIG_DIR, (
        ".mi.token", ".mi.token.bak", "cam.json", "cam_secret.json",
        "weather.json", "notify.json", "ext.json", "ext_hist.json",
        "ui_close_pref.json", "schedules.json",
    )),
    "runtime": (RUNTIME_DIR, (
        "evt.db", "evt.db-wal", "evt.db-shm",
        "devices_cache.json", "spec_cache.json", ".energy_days.json",
    )),
}


def migrate(dry_run=False):
    """把散在根目录的文件搬进 config/ 与 runtime/，返回 [(src, dst, ok)]。

    只搬"源存在、目标不存在"的。目标已存在就跳过 —— 迁移过一半中断、
    再跑一次也不会互相覆盖。跨盘移动用 copy+replace 保证原子性。
    """
    import shutil
    report = []
    for kind, (dst_dir, names) in LAYOUT.items():
        if not dry_run:
            os.makedirs(dst_dir, exist_ok=True)
        for name in names:
            src = os.path.join(BASE, name)
            dst = os.path.join(dst_dir, name)
            if not os.path.exists(src) or os.path.exists(dst):
                continue
            if dry_run:
                report.append((src, dst, None))
                continue
            try:
                shutil.move(src, dst)
                report.append((src, dst, True))
            except Exception as e:
                report.append((src, dst, str(e)))
    return report
