# -*- coding: utf-8 -*-
"""抓取正在运行的「米家 · 电脑版」窗口，存成 PNG（临时脚本）。

优先 PrintWindow(PW_RENDERFULLCONTENT) —— 窗口被挡住也能抓到内容。
抓不到就退回到 ImageGrab 抓屏幕区域。
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(BASE, "_截图")
TITLE = "米家 · 电脑版"

u32 = ctypes.windll.user32
g32 = ctypes.windll.gdi32
k32 = ctypes.windll.kernel32

u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
u32.GetWindowTextLengthW.restype = ctypes.c_int
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                         ctypes.POINTER(wintypes.DWORD)]
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u32.GetWindowDC.argtypes = [wintypes.HWND]
u32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
g32.CreateCompatibleDC.restype = wintypes.HDC
g32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
g32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
g32.SelectObject.restype = wintypes.HGDIOBJ
g32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                          wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                          wintypes.UINT]
g32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
g32.DeleteDC.argtypes = [wintypes.HDC]
u32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def find_window(title):
    hit = [None]
    ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        n = u32.GetWindowTextLengthW(h)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, buf, n + 1)
            if buf.value == title:
                hit[0] = h
                return False
        return True

    c = ENUM(cb)
    u32.EnumWindows(c, 0)
    return hit[0]


def grab(hwnd, path):
    from PIL import Image
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        print("窗口尺寸异常:", w, h)
        return False
    print("窗口尺寸:", w, "x", h)

    hdc = u32.GetWindowDC(hwnd)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    old = g32.SelectObject(mdc, bmp)
    ok = u32.PrintWindow(hwnd, mdc, 2)      # PW_RENDERFULLCONTENT
    print("PrintWindow ->", ok)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h            # 负数 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)

    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    img.save(path)
    print("已保存:", path, os.path.getsize(path), "字节")

    g32.SelectObject(mdc, old)
    g32.DeleteObject(bmp)
    g32.DeleteDC(mdc)
    u32.ReleaseDC(hwnd, hdc)
    return True


def main():
    # 必须声明 DPI 感知，否则 GetWindowRect/PrintWindow 拿到的是**被系统
    # 虚拟化过的**坐标：150% 缩放下窗口实际 1900px 宽，没声明时报告 1520，
    # 抓出来的位图也只有左边 80%，看图会误判成"右侧卡片被裁掉了"。
    # 这个坑先后两次把人带偏，别删。
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    os.makedirs(OUT_DIR, exist_ok=True)
    hwnd = find_window(TITLE)
    if not hwnd:
        print("❌ 没找到窗口")
        return 1
    print("窗口 hwnd =", hwnd)
    # 抓之前先把它叫到前台，PrintWindow 抓不到时还能退回屏幕抓图
    try:
        u32.ShowWindow(hwnd, 9)
        u32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(1.5)
    # 输出名带下划线前缀：这是随手抓的"当前窗口"，不是归档截图。
    # 以前写死成某张归档图的文件名，每抓一次就把那张历史图覆盖掉。
    path = os.path.join(OUT_DIR, "_shot-当前窗口.png")
    if grab(hwnd, path):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
