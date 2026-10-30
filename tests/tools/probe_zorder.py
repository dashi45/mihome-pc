# -*- coding: utf-8 -*-
"""造一个标题为「米家 · 电脑版」的顶层窗口（纯 Win32，不依赖 tkinter）。

为什么不用 tkinter：venv 里根本没装 tkinter，而且它一崩整个脚本就退，
根本造不出"窗口还活着、后端已死"的假象。
"""
import ctypes
from ctypes import wintypes

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


u32.CreateWindowExW.restype = wintypes.HWND
# 不设 argtypes 的话，64 位的 HMODULE 会被按 c_int 传，直接 OverflowError
u32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
u32.RegisterClassW.restype = wintypes.WORD
u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                             wintypes.UINT, wintypes.UINT]
# LPARAM 是 64 位，不设 argtypes 会被按 c_int 传 → OverflowError →
# 窗口过程一收到消息就崩，连 GetWindowText（内部发 WM_GETTEXT）都读不到标题
u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
u32.DefWindowProcW.restype = ctypes.c_long
k32.GetModuleHandleW.restype = wintypes.HMODULE
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


def _wndproc(hwnd, msg, wparam, lparam):
    if msg == 0x0002:          # WM_DESTROY
        u32.PostQuitMessage(0)
        return 0
    return u32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ctypes 要求字段是 WinFunctionType 实例，而且必须保留引用，
# 否则回调被 GC 掉，窗口一收到消息就崩。
_PROC = WNDPROC(_wndproc)


def main():
    hinst = k32.GetModuleHandleW(None)
    cls = WNDCLASS()
    cls.lpfnWndProc = _PROC
    cls.hInstance = hinst
    cls.lpszClassName = "MiHomePCTestWin"
    u32.RegisterClassW(ctypes.byref(cls))

    hwnd = u32.CreateWindowExW(
        0, "MiHomePCTestWin", "米家 · 电脑版",
        0x00CF0000, 100, 100, 320, 140,
        None, None, hinst, None)
    if not hwnd:
        raise ctypes.WinError()

    msg = wintypes.MSG()
    while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u32.TranslateMessage(ctypes.byref(msg))
        u32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
