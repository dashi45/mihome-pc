# -*- coding: utf-8 -*-
"""对比 HLS 分片时长 1 秒 vs 0.5 秒：首片时间、分片有效性、CPU/内存。

为什么要测：分片越短，起播越快、端到端延迟越低（云台转完能更快看到画面）；
但分片太短会让每片都很小，抗抖动变差，还可能因为没有关键帧而出现空片
—— 这正是本项目当初放弃 go2rtc 自带 HLS 的原因，不能重蹈覆辙。
"""
import glob
import os
import shutil
import subprocess
import sys
import time

import psutil

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FF = os.path.join(BASE, "go2rtc", "ffmpeg.exe")
RTSP = "rtsp://127.0.0.1:8554/camera"
NO_WINDOW = 0x08000000

try:
    import urllib.request
    urllib.request.urlopen(
        "http://127.0.0.1:8765/api/cam/stop/" + os.environ.get("CAM_DID", "1234567890"),
        timeout=5).read()
except Exception:
    pass
for pr in psutil.process_iter(["pid", "name"]):
    try:
        if (pr.info["name"] or "").lower() == "ffmpeg.exe":
            pr.kill()
    except Exception:
        pass
time.sleep(2)


def run(tag, seg_secs, list_size, secs=24):
    d = os.path.join(BASE, "_segtest")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    gop = max(2, int(round(20 * seg_secs)))      # 20fps
    cmd = [
        FF, "-hide_banner", "-loglevel", "warning",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-probesize", "1000000", "-analyzeduration", "1000000",
        "-rtsp_transport", "tcp", "-i", RTSP,
        "-c:v", "h264_mf", "-b:v", "2000k", "-maxrate", "2500k", "-bufsize", "4000k",
        "-force_key_frames", "expr:gte(t,n_forced*%s)" % seg_secs,
        "-vf", "scale=1280:720", "-g", str(gop), "-bf", "0", "-pix_fmt", "yuv420p",
        "-an", "-f", "hls",
        "-hls_time", str(seg_secs), "-hls_list_size", str(list_size),
        "-hls_flags", "delete_segments+independent_segments+omit_endlist",
        "-hls_segment_filename", os.path.join(d, "seg%03d.ts"),
        os.path.join(d, "index.m3u8"),
    ]
    logp = os.path.join(d, "ff.log")
    peak = 0
    first = None
    t0 = time.time()
    with open(logp, "wb", buffering=0) as lf:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=lf,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        pr = psutil.Process(p.pid)
        c0 = None
        for _ in range(secs * 2):
            time.sleep(0.5)
            if p.poll() is not None:
                break
            try:
                peak = max(peak, pr.memory_info().rss)
            except Exception:
                break
            if c0 is None and time.time() - t0 > 8:
                c0 = pr.cpu_times()
            if first is None and glob.glob(os.path.join(d, "seg*.ts")):
                first = time.time() - t0
        cpu = 0.0
        if c0 is not None:
            c1 = pr.cpu_times()
            cpu = (c1.user - c0.user) + (c1.system - c0.system)
    finally:
        try:
            p.kill()
        except Exception:
            pass

    segs = sorted(glob.glob(os.path.join(d, "seg*.ts")))
    sizes = [os.path.getsize(s) for s in segs]
    empties = sum(1 for s in sizes if s < 5000)
    playlist = ""
    pl = os.path.join(d, "index.m3u8")
    if os.path.exists(pl):
        playlist = open(pl, encoding="utf-8", errors="ignore").read()

    print()
    print("===== %s =====" % tag)
    print("  首片出现   : %s" % ("%.1fs" % first if first else "未出现"))
    print("  分片数     : %d" % len(sizes))
    print("  大小 min/avg/max : %s" % (
        "%d/%d/%d" % (min(sizes), sum(sizes) // len(sizes), max(sizes)) if sizes else "-"))
    print("  空片(<5KB) : %d %s" % (empties, "← 有问题!" if empties else "✓"))
    print("  内存峰值   : %.1f MB" % (peak / 1048576))
    print("  CPU(2秒窗) : %.2f 秒" % cpu)
    if playlist:
        for line in playlist.splitlines()[:8]:
            if line.strip():
                print("    |", line.strip())
    shutil.rmtree(d, ignore_errors=True)
    return dict(first=first, n=len(sizes), empties=empties,
                mem=peak / 1048576, sizes=sizes)


a = run("A. 当前：1.0 秒分片 / list 5", 1, 5)
time.sleep(3)
b = run("B. 试验：0.5 秒分片 / list 10", 0.5, 10)

print()
print("=== 结论 ===")
if b["first"] and a["first"]:
    print("  首片提速: %.1fs -> %.1fs" % (a["first"], b["first"]))
print("  0.5 秒方案是否可用:", "可以" if (b["empties"] == 0 and b["n"] >= 8) else "不行")
