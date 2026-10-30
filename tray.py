# -*- coding: utf-8 -*-
"""系统托盘图标 —— 纯 ctypes 实现，不新增任何依赖。

为什么不用 pystray / pywin32：
  · 打包只带 launcher.py 及其 import 树，多一个三方库就多一份 exe 体积
    和一条 PyInstaller 收集失败的路径；
  · 需要的行为很少：左键呼出窗口、右键菜单、气泡提示一次，
    三四十行 Win32 就够了，而且行为完全可控。

项目里用 ctypes 碰 Win32 的三条铁律（都吃过亏，见项目笔记）：
  1. 回调必须是 WINFUNCTYPE 实例，且模块级留引用 —— 被 GC 回收后
     窗口过程一收到消息就崩，而且异常会被吞掉，看起来"托盘不响应"；
  2. 所有 user32 函数显式写 argtypes/restype，64 位句柄按 c_int 传
     会截断成另一个不存在的窗口；
  3. DefWindowProcW 不设 argtypes 会让窗口过程收到第一条消息就崩。
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
sh32 = ctypes.WinDLL("shell32", use_last_error=True)

# ---- 常量（winuser.h / shellapi.h）----
WM_APP_TRAY = 0x8001          # WM_APP+1：托盘回调消息
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_USER = 0x0400

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
IDI_APPLICATION = 32512
CS_VREDRAW, CS_HREDRAW = 0x1, 0x2
WS_OVERLAPPED = 0x00000000
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON = 0x2
TPM_RETURNCMD = 0x100
SM_CXSMICON, SM_CYSMICON = 49, 50

MENU_SHOW, MENU_RELEASE, MENU_QUIT, MENU_EXTRA = 1, 2, 3, 4

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),        # 与 uTimeout 的 union
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
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


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def _bind():
    """显式声明 argtypes / restype。见模块头注释第 2、3 条。"""
    u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    u32.RegisterClassW.restype = wintypes.ATOM
    u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                    wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wintypes.HWND,
                                    wintypes.HMENU, wintypes.HINSTANCE,
                                    wintypes.LPVOID]
    u32.CreateWindowExW.restype = wintypes.HWND
    u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
    u32.DefWindowProcW.restype = LRESULT
    u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT]
    u32.GetMessageW.restype = ctypes.c_int
    u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u32.PostQuitMessage.argtypes = [ctypes.c_int]
    u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                               wintypes.UINT, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT]
    u32.LoadImageW.restype = wintypes.HANDLE
    u32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    u32.LoadIconW.restype = wintypes.HICON
    u32.DestroyIcon.argtypes = [wintypes.HICON]
    u32.CreatePopupMenu.restype = wintypes.HMENU
    u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                ctypes.c_size_t, wintypes.LPCWSTR]
    u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.LPVOID]
    u32.TrackPopupMenu.restype = ctypes.c_int   # TPM_RETURNCMD 时是菜单 id
    u32.DestroyMenu.argtypes = [wintypes.HMENU]
    u32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    u32.GetSystemMetrics.argtypes = [ctypes.c_int]
    u32.GetSystemMetrics.restype = ctypes.c_int

    k32.GetCurrentProcessId.restype = wintypes.DWORD
    k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k32.GetModuleHandleW.restype = wintypes.HMODULE

    sh32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                       ctypes.POINTER(NOTIFYICONDATAW)]
    sh32.Shell_NotifyIconW.restype = wintypes.BOOL


_bind()

_state = {"hwnd": None, "thread": None, "wndproc": None,
          "cb": None, "icon": None, "stopped": False,
          "on_show": None, "on_release": None, "on_quit": None,
          "on_error": None,
          "tooltip": "", "hint_shown": False, "last_click": 0.0,
          "extra_label": None, "extra_cb": None}


def _report(where, exc):
    """错误上报钩子（launcher 传 log_err 进来）。托盘链路绝对不能把
    异常吞成"点了没反应"——这次事故的排查难度就是这么来的。"""
    cb = _state.get("on_error")
    if cb:
        try:
            cb(where, exc)
        except Exception:
            pass


def _load_icon(path):
    """托盘用 16x16 小图标；加载失败退回系统默认（不能让托盘卡死主流程）。"""
    cx = u32.GetSystemMetrics(SM_CXSMICON) or 16
    cy = u32.GetSystemMetrics(SM_CYSMICON) or 16
    if path and os.path.isfile(path):
        h = u32.LoadImageW(None, path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
        if h:
            return h
    return u32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))


def _wnd_proc(hwnd, msg, wparam, lparam):
    """托盘隐藏窗口的消息过程。所有交互都从这里进来。"""
    if msg == WM_APP_TRAY:
        m = lparam & 0xFFFF
        now = time.time()
        # 左键：up 和 dblclk 会连着来，600ms 内的第二下不理，避免闪两下
        if m in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
            if now - _state["last_click"] > 0.6:
                _state["last_click"] = now
                try:
                    _state["on_show"]()
                except Exception as e:
                    _report("tray.click_show", e)
        elif m == WM_RBUTTONUP:
            _show_menu(hwnd)
        return 0
    if msg == WM_COMMAND:
        mid = wparam & 0xFFFF
        try:
            if mid == MENU_SHOW:
                _state["on_show"]()
            elif mid == MENU_RELEASE:
                _state["on_release"]()
            elif mid == MENU_QUIT:
                _state["on_quit"]()
            elif mid == MENU_EXTRA:
                _state["extra_cb"]()
        except Exception as e:
            _report("tray.command", e)
        return 0
    if msg == WM_DESTROY:
        _remove_icon()
        u32.PostQuitMessage(0)
        return 0
    return u32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _show_menu(hwnd):
    """右键菜单。必须先把托盘窗口设为前台，否则点别处菜单不消失。"""
    hm = u32.CreatePopupMenu()
    if not hm:
        return
    u32.AppendMenuW(hm, MF_STRING, MENU_SHOW, "显示主窗口")
    extra = _state.get("extra_label")
    if extra:
        u32.AppendMenuW(hm, MF_STRING, MENU_EXTRA, str(extra)[:120])
    u32.AppendMenuW(hm, MF_SEPARATOR, 0, None)
    u32.AppendMenuW(hm, MF_STRING, MENU_RELEASE, "释放内存")
    u32.AppendMenuW(hm, MF_SEPARATOR, 0, None)
    u32.AppendMenuW(hm, MF_STRING, MENU_QUIT, "退出")
    u32.SetForegroundWindow(hwnd)
    pt = POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    mid = u32.TrackPopupMenu(hm, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                             pt.x, pt.y, 0, hwnd, None)
    u32.PostMessageW(hwnd, WM_NULL, 0, 0)
    u32.DestroyMenu(hm)
    try:
        if mid == MENU_SHOW:
            _state["on_show"]()
        elif mid == MENU_RELEASE:
            _state["on_release"]()
        elif mid == MENU_QUIT:
            _state["on_quit"]()
        elif mid == MENU_EXTRA:
            _state["extra_cb"]()
    except Exception as e:
        _report("tray.menu", e)


def _add_icon(hwnd, hicon, tooltip):
    d = NOTIFYICONDATAW()
    d.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    d.hWnd = hwnd
    d.uID = 1
    d.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    d.uCallbackMessage = WM_APP_TRAY
    d.hIcon = hicon
    d.szTip = tooltip or ""
    return bool(sh32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(d)))


def _remove_icon():
    hwnd = _state.get("hwnd")
    if not hwnd:
        return
    d = NOTIFYICONDATAW()
    d.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    d.hWnd = hwnd
    d.uID = 1
    sh32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(d))


def notify(title, text):
    """气泡提示。只给安静时间尊重的 NIIF_INFO，失败静默（不必要的东西）。"""
    hwnd = _state.get("hwnd")
    if not hwnd or _state.get("stopped"):
        return
    d = NOTIFYICONDATAW()
    d.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    d.hWnd = hwnd
    d.uID = 1
    d.uFlags = NIF_INFO
    d.szInfo = (text or "")[:255]
    d.szInfoTitle = (title or "")[:63]
    d.dwInfoFlags = NIIF_INFO
    sh32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(d))


def start(icon_path, tooltip, on_show, on_release, on_quit, hint=None,
          on_error=None):
    """起托盘线程。返回 True = 图标已加上。

    on_show / on_release / on_quit 在托盘线程里被调用，必须是线程安全的。

    ★ 窗口必须在**托盘线程里创建**（见 _boot）：GetMessageW 只能收到
    「调用线程自己创建的窗口」的消息。旧实现把窗口建在主线程、把消息
    循环跑在托盘线程 —— 点击消息全躺在主线程的队列里没人取
    （webview.start() 之后主线程 Join 挂起，不泵消息），表现就是
    "点托盘图标永远没反应、右键菜单也不出"。这是真实发生过的事故。
    """
    if _state["thread"]:
        return True
    _state.update({"on_show": on_show, "on_release": on_release,
                   "on_quit": on_quit, "on_error": on_error,
                   "tooltip": tooltip or "", "hint_shown": False,
                   "stopped": False, "hwnd": None})
    ready = threading.Event()
    t = threading.Thread(target=_boot, args=(icon_path, tooltip or "", hint, ready),
                         name="tray", daemon=True)
    _state["thread"] = t
    t.start()
    if not ready.wait(3.0) or not _state.get("hwnd"):
        return False
    return True


def _boot(icon_path, tooltip, hint, ready):
    """托盘线程入口：注册类 → 建窗口 → 挂图标 → 消息循环。

    RegisterClassW / CreateWindowExW 必须和 GetMessageW 在同一线程：
    窗口归哪个线程，它的消息就进哪个线程的队列。
    """
    try:
        cls_name = "MiHomePCTray%d" % (k32.GetCurrentProcessId() & 0xFFFF)
        cb = WNDPROC(_wnd_proc)
        wc = WNDCLASSW()
        wc.style = CS_VREDRAW | CS_HREDRAW
        wc.lpfnWndProc = cb
        wc.hInstance = k32.GetModuleHandleW(None)
        wc.lpszClassName = cls_name
        if not u32.RegisterClassW(ctypes.byref(wc)):
            _report("tray.boot", "RegisterClassW 失败（err=%d）" % u32.GetLastError())
            ready.set()
            return
        hwnd = u32.CreateWindowExW(
            0, cls_name, "mihome-pc-tray", WS_OVERLAPPED,
            0, 0, 0, 0, None, None, wc.hInstance, None)
        if not hwnd:
            _report("tray.boot", "CreateWindowExW 失败（err=%d）" % u32.GetLastError())
            ready.set()
            return
        hicon = _load_icon(icon_path)
        if not _add_icon(hwnd, hicon, tooltip):
            _report("tray.boot", "Shell_NotifyIconW(NIM_ADD) 失败")
            u32.DestroyWindow(hwnd)
            ready.set()
            return
        # 回调必须留在模块级引用上（_state），被 GC 回收后窗口过程一收消息就崩
        _state.update({"hwnd": hwnd, "wndproc": cb, "cb": cb, "icon": hicon})
        ready.set()
        _loop(hwnd, hint)
    except Exception as e:
        _state["hwnd"] = None
        _report("tray.boot", e)
        ready.set()


def _loop(hwnd, hint):
    msg = wintypes.MSG()
    while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u32.TranslateMessage(ctypes.byref(msg))
        u32.DispatchMessageW(ctypes.byref(msg))
    _state["thread"] = None
    _state["hwnd"] = None


def set_extra(label, cb):
    """右键菜单附加项（label 为 None 时移除）。

    用于"关闭行为已记住，点此恢复询问"这类可逆设置。菜单每次右键
    都是现建的，这里只改 _state，下一次弹菜单自然生效。
    """
    _state["extra_label"] = label
    _state["extra_cb"] = cb


def stop(timeout=2.0):
    """撤图标、结束托盘线程。窗口正常关闭和主动退出都要调，不然图标残留。"""
    hwnd = _state.get("hwnd")
    if not hwnd:
        return
    _state["stopped"] = True
    u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    t = _state.get("thread")
    if t:
        t.join(timeout)
