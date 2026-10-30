# -*- coding: utf-8 -*-
"""摄像头 HLS 会话管理（只切片，不编码）

## 这一版改了什么（2026-10-26）

原来这里是一个"自己解码 + 自己编码 + 自己切片"的完整管线：按 nvenc → amf →
qsv → mf → libx264 挨个探测编码器，探测失败要拉黑换下一个，显卡打不开要
降级软解，等等。那 200 多行探测/降级代码换来的是**一整类只在特定机器上
复现的故障**（qsv 在合成源上"通过"、一接真实流就 MFX session 不支持；
h264_mf 的 GOP 与分片错位……）。

现在分工改成：

    go2rtc  camera_h264 = ffmpeg:camera#video=h264      ← 负责转码（含缩放/码率/GOP）
              │  rtsp://127.0.0.1:8554/camera_h264
              v
    camhls   -c copy  → 切成 1 秒一片的 HLS             ← 只做封装，不解码不编码

好处：
  · 编码参数只存在于 go2rtc.yaml 一处，不再散落在 Python 里
  · 本模块不再依赖任何显卡/编码器，`-c copy` 到哪台机器都一个行为
  · 省掉一个解码 + 一个编码进程的内存，切换编码器的那类 bug 从根上没了

代价（如实写在这里，别指望它不存在）：
  · 转码发生在 go2rtc 的子进程里，本模块看不到它的详细日志，只能通过
    "迟迟没有分片"判断上游失败；诊断信息因此变少（见 _info 的 upstream 字段）
  · 多了一次 RTSP 中转（回环，几十毫秒，可以忽略）

## 为什么还自己切片，不用 go2rtc 自带的 /api/stream.m3u8

两个原因，都写死在项目里了：
  1. go2rtc 的 HTTP API 是**整个关掉**的（go2rtc.yaml 的 api.listen: ""）——
     它没有鉴权、不校验 Host，`/api/config` 会原样吐出账号 token，开回来
     就等于把 DNS rebinding 的口子重新打开。
  2. 它自带的 HLS 在 GOP 与分片不对齐时会吐 376 字节的空片，hls.js 直接
     致命错误。本模块的 `-hls_flags independent_segments` + 1 秒精确切点
     就是为了根除这个，别退回去。

## 会话生命周期

每个摄像头最多一个 ffmpeg，多页面共用；停播 IDLE_STOP 秒没人取分片就结束，
避免一直占着摄像头的 P2P 通道（小米摄像头并发连接数很低）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

import logx

log = logx.get(__name__)

import config

CACHE_DIR = config.CAMLIVE_DIR
FFMPEG = config.FFMPEG

HLS_TIME = 1            # 分片时长（秒）
LIST_SIZE = 5           # 播放列表保留的分片数
IDLE_STOP = 15          # 无人取分片多久后结束 ffmpeg（秒）
                        # 2026-10-26：120 → 15。没人看的时候不该占着 300MB 内存
                        # 和摄像头那点并发连接。代价是"离开 20 秒再回来"要重新
                        # 走一次 P2P + 转码启动（约 5~10 秒）。
START_TIMEOUT = 22      # 等首屏可播的最长时间（秒）
MAX_RESTART = 3         # 60 秒内最多重启几次

# 等几个分片就算"可播"。1 片够 hls.js 解析出 playlist 并开始加载分片，
# 2 片能避免刚起播就把第一片放完导致卡一下。
MIN_SEGMENTS = 2

# 分片文件名：seg00001.ts 这种；index.m3u8 单独判断
_SEG_RE = re.compile(r"^seg\d+\.ts$")

_LOCK = threading.RLock()
_SESSIONS = {}
_WATCH_STARTED = [False]


class Session:
    """一个摄像头的转封装进程。"""

    def __init__(self, did, rtsp_url):
        self.did = did
        self.rtsp_url = rtsp_url
        self.dir = os.path.join(CACHE_DIR, did)
        self.proc = None
        self.started = 0.0
        self.touched = time.time()
        self.restarts = 0
        self.last_restart = 0.0
        self.error = None
        self._log = []
        self._reader = None

    # ---- 状态 ----
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def seg_count(self):
        try:
            return sum(1 for n in os.listdir(self.dir) if _SEG_RE.match(n))
        except Exception:
            return 0

    def log_tail(self, n=8):
        with _LOCK:
            return list(self._log[-n:])

    # ---- 生命周期 ----
    def stop(self):
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        # 进程退了再删目录；目录名带时间戳，删不掉也不影响下一次（每次都新建）
        try:
            shutil.rmtree(self.dir, ignore_errors=True)
        except Exception:
            pass


def _safe(did):
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(did))


def _spawn(sess):
    """起一个只做封装的 ffmpeg：拉 H264 RTSP，原样切成 HLS。"""
    sess.error = None
    # 每次起进程用一个**新目录**：上一轮被强杀的 ffmpeg 可能还活着，还攥着旧
    # 分片的句柄（Windows 删不掉正被打开的文件）。共用目录的话，新会话会把
    # 僵尸留下的旧片当成"我已经有画面了"，ensure() 立刻返回成功，而实际
    # 一条流都没在跑。换目录之后旧片再也骗不到人，代价只是几 MB 垃圾，
    # 由 _sweep_orphans() 和 watchdog 顺手清掉。
    stamp = "%d_%d" % (int(time.time() * 1000), os.getpid())
    d = os.path.join(CACHE_DIR, _safe(sess.did), stamp)
    sess.dir = d
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        sess.error = "建分片目录失败：%s" % e
        return False

    args = [
        FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin",
        # 拉上游 RTSP：走 TCP（回环，丢包重传比 UDP 稳），不缓冲
        "-rtsp_transport", "tcp",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        # 读超时：上游卡死时别让进程吊着（微秒）。
        # 注意这里必须是 RTSP 解复用器的 `-timeout`，不是通用的 `-rw_timeout` ——
        # 自带的 ffmpeg 6.1.1（gyan essentials）对后者直接报
        # "Option rw_timeout not found" 然后退出，一分片都不出。
        "-timeout", "15000000",
        "-i", sess.rtsp_url,
        # 关键：不解码、不编码，只换封装。上游 go2rtc 已经出 H264 720p
        "-c", "copy",
        "-f", "hls",
        "-hls_time", str(HLS_TIME),
        "-hls_list_size", str(LIST_SIZE),
        # delete_segments：只保留列表内的片，磁盘不会涨
        # independent_segments：告诉播放器每片都能独立起播
        # omit_endlist：直播流不写 EXT-X-ENDLIST，否则 hls.js 播完就停
        # temp_file：半截分片不暴露给播放器（否则会拿到残缺的 .ts）
        "-hls_flags", "delete_segments+independent_segments+omit_endlist+temp_file",
        "-hls_segment_filename", os.path.join(d, "seg%05d.ts"),
        os.path.join(d, "index.m3u8"),
    ]
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        proc = subprocess.Popen(
            args, cwd=d, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            startupinfo=si, creationflags=0x08000000)   # CREATE_NO_WINDOW
    except Exception as e:
        sess.error = "ffmpeg 启动失败：%s" % e
        return False

    sess.proc = proc
    sess.started = time.time()
    sess.restarts += 1
    sess.last_restart = time.time()
    with _LOCK:
        sess._log = ["[launch] %s" % " ".join(args[1:])]

    def _pump():
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                with _LOCK:
                    sess._log.append(line)
                    if len(sess._log) > 80:
                        del sess._log[:40]
        except Exception:
            pass

    sess._reader = threading.Thread(target=_pump, daemon=True)
    sess._reader.start()
    return True


def _recover(sess):
    """进程没了 / 卡住时重启。同一个 60 秒窗口里别无限重启。"""
    now = time.time()
    if sess.restarts >= MAX_RESTART and now - sess.last_restart < 60:
        sess.error = sess.error or "连续重启 %d 次仍失败，已暂停（1 分钟后再试）" % MAX_RESTART
        return False
    if now - sess.last_restart < 1.0:       # 别原地疯狂起进程
        return sess.alive()
    sess.stop()
    return _spawn(sess)


def ensure(did, rtsp_url, wait=START_TIMEOUT):
    """确保这个摄像头有可播的 HLS，返回 (ok, info)。

    已经有 MIN_SEGMENTS 个分片立即返回；还没起来就等，最多 wait 秒。
    """
    _start_watchdog()
    with _LOCK:
        sess = _SESSIONS.get(did)
        if sess is None:
            sess = Session(did, rtsp_url)
            _SESSIONS[did] = sess
        sess.rtsp_url = rtsp_url
        sess.touched = time.time()

        if not sess.alive():
            if not _recover(sess):
                return False, _info(sess)

    deadline = time.time() + wait
    while time.time() < deadline:
        with _LOCK:
            sess.touched = time.time()
        if sess.seg_count() >= MIN_SEGMENTS:
            return True, _info(sess)
        if not sess.alive():
            with _LOCK:
                if not _recover(sess):
                    return False, _info(sess)
        time.sleep(0.3)

    return False, _info(sess)


def touch(did):
    sess = _SESSIONS.get(did)
    if sess:
        sess.touched = time.time()


def stop(did):
    with _LOCK:
        sess = _SESSIONS.pop(did, None)
    if sess:
        sess.stop()


def stop_all():
    with _LOCK:
        items = list(_SESSIONS.values())
        _SESSIONS.clear()
    for s in items:
        s.stop()


def state(did):
    sess = _SESSIONS.get(did)
    return _info(sess) if sess else {"running": False}


def _info(sess):
    return {
        "did": sess.did,
        "running": sess.alive(),
        "segments": sess.seg_count(),
        "uptime": round(time.time() - sess.started, 1) if sess.started else 0,
        "idle": round(time.time() - sess.touched, 1),
        "restarts": sess.restarts,
        # 这一版不再"挑编码器"：编码在上游 go2rtc，这里只封装
        "mode": "copy",
        "encoder": "copy",
        "decoder": "go2rtc",
        "error": sess.error,
        "log": sess.log_tail(),
        "cmd_source": sess.rtsp_url,
    }


def _watchdog():
    """没人看就收工：小米摄像头并发连接很少，留着不放会拖累下次打开。"""
    while True:
        time.sleep(5)
        now = time.time()
        dead = []
        with _LOCK:
            for did, sess in list(_SESSIONS.items()):
                if sess.alive() and now - sess.touched > IDLE_STOP:
                    dead.append(sess)
                elif not sess.alive() and now - sess.touched > 10:
                    dead.append(sess)
                elif not sess.alive() and sess.restarts >= MAX_RESTART:
                    dead.append(sess)
            for sess in dead:
                _SESSIONS.pop(sess.did, None)
        for sess in dead:
            sess.stop()


def _sweep_orphans():
    """清掉上一轮被强杀的 backend 留下的 ffmpeg。

    父进程被 kill -9 / 任务管理器结束时，子进程不会跟着死（Windows 没有
    进程组），它会继续攥着小米 P2P 通道和分片文件。这些"僵尸取流"看着
    无害，实际会占掉摄像头那极少的并发连接数，让下一次打开一直转圈。
    """
    try:
        import psutil
    except Exception:
        return 0
    killed = 0
    with _LOCK:
        live = {s.proc.pid for s in _SESSIONS.values() if s.proc is not None}
    marker = CACHE_DIR.lower()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (p.info.get("name") or "").lower() != "ffmpeg.exe":
                continue
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            if marker not in cmd or p.pid in live:
                continue
            p.kill()
            killed += 1
        except Exception:
            continue
    if killed:
        log.info("清掉 %d 个上次残留的 ffmpeg", killed)
    return killed


def _start_watchdog():
    if _WATCH_STARTED[0]:
        return
    _WATCH_STARTED[0] = True
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except Exception:
        pass
    _sweep_orphans()
    threading.Thread(target=_watchdog, daemon=True).start()


def is_segment(name):
    return bool(_SEG_RE.match(name))


def file_path(did, name):
    if name != "index.m3u8" and not is_segment(name):
        return None
    sess = _SESSIONS.get(did)
    if not sess:
        return None
    p = os.path.join(sess.dir, name)
    return p if os.path.isfile(p) else None
