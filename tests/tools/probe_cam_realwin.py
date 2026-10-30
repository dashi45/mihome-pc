# -*- coding: utf-8 -*-
"""用**真实窗口**（非 headless）验证摄像头能正常播放，并抓图。

为什么不用 headless：headless 下 `--disable-gpu` 走软解，720p H.264 解不动，
hls.js 会一直停在 "缓冲中"，看起来像"前端改坏了"，其实环境问题。
真实窗口有 GPU 解码，能反映用户实际看到的状态。
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = "http://127.0.0.1:8765/#did=" + os.environ.get("CAM_DID", "1234567890")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   "_截图", "20-摄像头-真实窗口.png")

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
g32 = ctypes.windll.gdi32

u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
u32.GetWindowTextLengthW.restype = ctypes.c_int
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                         ctypes.POINTER(wintypes.DWORD)]
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def windows():
    out = []

    def cb(h, _l):
        if u32.IsWindowVisible(h):
            n = u32.GetWindowTextLengthW(h)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(h, buf, n + 1)
                pid = wintypes.DWORD(0)
                u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                r = wintypes.RECT()
                u32.GetWindowRect(h, ctypes.byref(r))
                out.append((h, pid.value, buf.value,
                            r.right - r.left, r.bottom - r.top))
        return True

    c = ENUM(cb)
    u32.EnumWindows(c, 0)
    return out


def shot(hwnd, w, h, path):
    from PIL import Image
    hdc_screen = u32.GetDC(None)
    hdc = g32.CreateCompatibleDC(hdc_screen)
    bmp = g32.CreateCompatibleBitmap(hdc_screen, w, h)
    g32.SelectObject(hdc, bmp)
    u32.PrintWindow(hwnd, hdc, 2)          # PW_RENDERFULLCONTENT
    buf = ctypes.create_string_buffer(w * h * 4)

    class BMI(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    bi = BMI()
    bi.biSize = ctypes.sizeof(BMI)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    g32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
    img.convert("RGB").save(path)
    g32.DeleteObject(bmp)
    g32.DeleteDC(hdc)
    u32.ReleaseDC(None, hdc_screen)


def main():
    before = {h for h, *_ in windows()}
    # --app 开独立窗口（无地址栏），--user-data-dir 隔离，避免复用已有实例
    prof = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "_edgeprofile")
    subprocess.Popen([EDGE, "--app=" + URL, "--user-data-dir=" + prof,
                      "--window-size=430,780", "--no-first-run",
                      "--disable-features=Translate"])
    print("已启动真实窗口，等待 18 秒让 P2P + 起播完成…", flush=True)
    time.sleep(18)

    new = [w for w in windows() if w[0] not in before]
    cand = [w for w in new if w[3] > 300 and w[4] > 400]
    if not cand:
        print("❌ 没找到新窗口。现有窗口：")
        for h, pid, t, ww, hh in windows()[:15]:
            print("   %s %r %dx%d" % (pid, t[:50], ww, hh))
        sys.exit(1)

    hwnd, pid, title, w, h = max(cand, key=lambda x: x[3] * x[4])
    print("窗口: pid=%s title=%r %dx%d" % (pid, title[:60], w, h))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    shot(hwnd, w, h, OUT)
    print("截图:", OUT, os.path.getsize(OUT), "字节")

    # 关掉这个窗口，别在用户桌面上留东西
    u32.PostMessageW(hwnd, 0x0010, 0, 0)
    print("已关闭测试窗口")


main()
