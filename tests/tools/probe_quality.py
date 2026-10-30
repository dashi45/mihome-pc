# -*- coding: utf-8 -*-
"""画质 vs 开销：测 720p/1080p、yuv420p/nv12 四种组合。

背景：用户要"画质更好"，但唯一能用的编码器是 h264_mf（软件路径），
提分辨率/码率会同时推高 CPU 和内存。需要一组实测数据来选折中点，
而不是拍脑袋定 1080p。

nv12 值得一试：它是 Windows Media Foundation 的原生格式，
用 yuv420p 会多一次像素格式转换，可能既费 CPU 又多占内存。
"""
import glob
import os
import shutil
import subprocess
import time

import psutil

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FF = os.path.join(BASE, "go2rtc", "ffmpeg.exe")
RTSP = "rtsp://127.0.0.1:8554/camera"
NO_WINDOW = 0x08000000

for pr in psutil.process_iter(["pid", "name"]):
    try:
        if (pr.info["name"] or "").lower() == "ffmpeg.exe":
            pr.kill()
    except Exception:
        pass
time.sleep(2)


def run(tag, w, h, br, pix, secs=20):
    d = os.path.join(BASE, "_cq")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    cmd = [
        FF, "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-probesize", "1000000", "-analyzeduration", "1000000",
        "-rtsp_transport", "tcp", "-i", RTSP,
        "-c:v", "h264_mf", "-b:v", br, "-maxrate", br, "-bufsize", br,
        "-force_key_frames", "expr:gte(t,n_forced*1)",
        "-vf", "scale=%d:%d" % (w, h), "-g", "20", "-bf", "0",
        "-pix_fmt", pix,
        "-an", "-f", "hls", "-hls_time", "1", "-hls_list_size", "5",
        "-hls_flags", "delete_segments+independent_segments+omit_endlist",
        "-hls_segment_filename", os.path.join(d, "seg%03d.ts"),
        os.path.join(d, "index.m3u8"),
    ]
    peak = 0
    first = None
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    p1 = None
    try:
        pr = psutil.Process(p.pid)
        for _ in range(secs * 2):
            time.sleep(0.5)
            if p.poll() is not None:
                break
            try:
                peak = max(peak, pr.memory_info().rss)
            except Exception:
                break
            if p1 is None and time.time() - t0 > 8:
                p1 = pr.cpu_times()
            if first is None and glob.glob(os.path.join(d, "seg*.ts")):
                first = time.time() - t0
        cpu = 0.0
        if p1 is not None:
            p2 = pr.cpu_times()
            cpu = (p2.user - p1.user) + (p2.system - p1.system)
    finally:
        try:
            p.kill()
        except Exception:
            pass

    segs = sorted(glob.glob(os.path.join(d, "seg*.ts")))
    sizes = [os.path.getsize(s) for s in segs]
    print("%-26s 内存=%6.1fMB  首片=%s  CPU/秒=%.2f核  片均=%s" % (
        tag, peak / 1048576,
        ("%.1fs" % first) if first else "无",
        cpu / 2.0 if cpu else 0,
        ("%dKB" % (sum(sizes) // len(sizes) // 1024)) if sizes else "-"))
    shutil.rmtree(d, ignore_errors=True)
    return peak / 1048576, cpu / 2.0, sizes


print("每组跑 %d 秒，CPU 取稳定后 2 秒窗口折算成核数\n" % 20)
run("720p 2000k yuv420p（当前）", 1280, 720, "2000k", "yuv420p")
time.sleep(3)
run("720p 2000k nv12", 1280, 720, "2000k", "nv12")
time.sleep(3)
run("1080p 3000k yuv420p", 1920, 1080, "3000k", "yuv420p")
time.sleep(3)
run("1080p 3000k nv12", 1920, 1080, "3000k", "nv12")
