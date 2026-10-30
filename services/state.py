# -*- coding: utf-8 -*-
"""进程级单例：登录态 S、锁、摄像头映射表、验证码回调。

放这里的东西有两个共同点：**全进程只有一份**、**谁都要读**。
真正的业务逻辑不在这里（那是 services/devices.py 的事），
这里只有"被共享的状态"和唯一一个直接操作它的协程。
"""
import asyncio
import threading
import time

import json
import os

import config


# 本机 go2rtc 转流服务（负责小米 P2P 取流、HEVC→H264 转码）
# 注意：它的 HTTP API 已整个关闭（见 go2rtc.yaml 的 api.listen 注释），
# 本程序只通过下面这个 RTSP 入口跟它打交道。
# go2rtc 暴露每个流的 RTSP 入口。camhls 拉的是 camera_h264（已转成 H264），
# 只做 -c copy 切片，不再自己解码/编码
config.RTSP_BASE = os.environ.get("GO2RTC_RTSP", "rtsp://127.0.0.1:8554")
_CAM_CFG = config.CAM_CFG


def _load_cam_cfg():
    try:
        with open(_CAM_CFG, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _load_cam_map():
    """did -> go2rtc 流名"""
    return {str(k): str(v) for k, v in (_load_cam_cfg().get("streams") or {}).items()}


def _load_flip_map():
    """did -> 画面朝向（none/vertical/horizontal/rotate180）"""
    return {str(k): str(v).strip().lower()
            for k, v in (_load_cam_cfg().get("flip") or {}).items()}


CAM_MAP = _load_cam_map()
FLIP_MAP = _load_flip_map()


# ---------- 全局状态 ----------
S = {
    "status": "idle",        # idle | logging_in | need_otp | ready | error
    "error": None,
    "otp_method": None,
    "otp_code": None,
    "account": None,
    "service": None,
    "devices": [],
    "homes": [],
    "user": None,
    "restore_failed": False,  # 后台自动恢复：明确失败（不是"还在进行"）
    "restore_tries": 0,       # 已经试过几次
    # 启动秒开：先用上次缓存的设备视图顶上，真实数据到了再覆盖
    "cached_devices": None,
    "cached_homes": None,
    "from_cache": False,      # 当前展示的是不是缓存数据
}
_LOGIN_LOCK = threading.Lock()


async def otp_callback(method: str) -> str:
    """miservice 在需要二次验证时调用这里，我们把验证码请求转给前端"""
    S["status"] = "need_otp"
    S["otp_method"] = method
    S["otp_code"] = None
    deadline = time.time() + 600
    while time.time() < deadline:
        if S.get("otp_code"):
            code = S["otp_code"]
            S["otp_code"] = None
            S["otp_method"] = None
            return code
        await asyncio.sleep(0.3)
    S["otp_method"] = None
    raise Exception("验证码等待超时（10 分钟）")
