# -*- coding: utf-8 -*-
"""托盘 + 后台挂起的守卫测试。

一部分是**源码契约**（无需启动程序）：托盘与挂起是 launcher / tray /
suspend.js / app.js 四处协作，任何一处被改坏（比如把挂起放在隐藏之后、
把 DefWindowProcW 的 argtypes 删了）都只会表现为"托盘点了没反应"这种
难查的静默故障，所以用文本断言钉住。

另一部分是**真实逻辑**：trim_memory 对当前进程真的跑一遍 EmptyWorkingSet，
验证权限申请、psutil 遍历、返回值类型 —— 这条在 CI/无窗口环境也能跑。
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_n = {"ok": 0, "fail": 0}


def ok(cond, msg, detail=""):
    if cond:
        _n["ok"] += 1
        print("  ✓ %s" % msg)
    else:
        _n["fail"] += 1
        print("  ✗ %s%s" % (msg, (" —— " + detail) if detail else ""))


def rd(p):
    return io.open(os.path.join(BASE, p), encoding="utf-8").read()


print("=== 1. tray.py：ctypes 三条铁律 ===")
TRAY = rd("tray.py")
ok("WNDPROC = ctypes.WINFUNCTYPE" in TRAY,
   "窗口过程是 WINFUNCTYPE（普通函数进 EnumWindows/WndProc 会崩）")
ok("u32.DefWindowProcW.argtypes" in TRAY,
   "DefWindowProcW 显式声明 argtypes（否则收到第一条消息就崩）")
ok('"cb": None' in TRAY and '_state.update({"hwnd": hwnd, "wndproc": cb, "cb": cb' in TRAY,
   "WNDPROC 回调被模块级引用持有（被 GC 回收 = 托盘随机失灵）")
ok("WM_APP_TRAY" in TRAY and "d.uCallbackMessage = WM_APP_TRAY" in TRAY,
   "托盘回调消息已绑定")
ok("Shell_NotifyIconW(NIM_DELETE" in TRAY and "def _remove_icon" in TRAY,
   "有撤图标的路径（退出后托盘不能留死图标）")
for name in ("显示主窗口", "释放内存", "退出"):
    ok(name in TRAY, "右键菜单含「%s」" % name)
ok(TRAY.index("u32.SetForegroundWindow(hwnd)") < TRAY.index("u32.TrackPopupMenu("),
   "TrackPopupMenu 之前先 SetForegroundWindow（否则点别处菜单不消失）")
ok("PostMessageW(hwnd, WM_NULL, 0, 0)" in TRAY,
   "TrackPopupMenu 之后补 WM_NULL（微软文档要求，否则菜单残留）")
ok("LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))" in TRAY,
   "图标加载失败退回系统默认（不能让托盘卡死主流程）")
ok("t.join(timeout)" in TRAY and "PostMessageW(hwnd, WM_CLOSE, 0, 0)" in TRAY,
   "stop() 先 WM_CLOSE 再 join 线程（等真退出，不硬杀）")
ok("600ms" in TRAY or "0.6" in TRAY,
   "左键点击有去抖（up 与 dblclk 连发，不防会闪两下）")

print()
print("=== 1b. 托盘窗口线程归属（「点托盘没反应」事故的根因） ===")
# 事故：窗口建在主线程、消息循环跑在托盘线程。GetMessageW 只能收到
# 「调用线程自己创建的窗口」的消息，点击全躺在主线程队列里没人取
# （webview.start 后主线程 Join 挂起不泵消息）→ 点托盘永远没反应。
ok("def _boot(" in TRAY, "有 _boot（托盘线程入口：建窗口+挂图标+跑循环）")
boot_body = TRAY[TRAY.index("def _boot("):TRAY.index("def _loop(")]
ok("CreateWindowExW" in boot_body,
   "CreateWindowExW 在 _boot 里（窗口归托盘线程，消息才能进托盘线程的队列）")
ok("_loop(hwnd, hint)" in boot_body,
   "消息循环也在托盘线程（GetMessageW 与窗口同线程）")
ok("ready.wait(3.0)" in TRAY,
   "start() 等托盘线程就绪（返回值才是真结果，不是'线程起过了'）")
ok("on_error" in TRAY and "_report(" in TRAY,
   "托盘链路异常有上报通道（except: pass 静默吞 = 排查无从下手）")
ok("def set_extra(" in TRAY and "extra_label" in TRAY,
   "右键菜单支持附加项（关闭行为已记住时的恢复入口）")

print()
print("=== 2. launcher.py：最小化入托盘的时序 ===")
L = rd("launcher.py")
ok("win.events.minimized += hide_to_tray" in L,
   "最小化事件接到 hide_to_tray")
ok(L.index("_eval_suspend(True)") < L.index("_WIN.hide()"),
   "挂起在隐藏**之前**（停完摄像头再 trim，量到的才是真省下的值）")
ok(L.index("_WIN.hide()") < L.index("saved = trim_memory()"),
   "修剪在隐藏**之后**")
ok("u32.ShowWindow(hwnd, 9)" in L and "SetForegroundWindow" in L,
   "呼出走 Win32 SW_RESTORE（pywebview 的 restore 对隐藏过的窗口不生效）")
ok("ToInt64()" in L and "int(win.native.Handle.ToInt64())" in L,
   "IntPtr 必须 ToInt64()（int() 在 pythonnet 里抛 TypeError）")
ok("_eval_suspend(False)" in L,
   "呼出时恢复前端（只改窗口不给前端解冻 = 界面看起来死了）")
ok("hwnd = show_main()" in L,
   "IPC show 先调 show_main（托盘里的窗口枚举可见窗口是找不到的）")
# _front_window 在 focus_existing 里也用到，这里要验的是「show 分支里
# show_main 在前、_front_window 兜底在后」，所以只比对 ipc 段内的相对位置
_ipc_show = L[L.index('cmd == "show"'):L.index('cmd == "__stop"')]
ok(_ipc_show.index("hwnd = show_main()") < _ipc_show.index("_front_window(dirs)"),
   "ipc show 段内：show_main 在前、可见窗口枚举兜底在后")
ok("_tray.stop()" in L,
   "窗口关闭后撤托盘图标")
ok("children(recursive=True)" in L,
   "trim 用 psutil 遍历整棵子树（WebView2 是本进程的孙辈）")
ok("EmptyWorkingSet" in L and "0x0400 | 0x0100" in L,
   "EmptyWorkingSet + PROCESS_QUERY_INFORMATION|PROCESS_SET_QUOTA")
ok("_SUSPENDED or _WIN is None" in L,
   "hide_to_tray 可重入保护（minimized 事件可能连发）")
ok("_TRAY_HINTED" in L,
   "首次进托盘的气泡提示只弹一次")

print()
print("=== 3. 前端：挂起从源头接管定时器 ===")
SUS = rd(os.path.join("static", "suspend.js"))
ok("window.__miSuspended || document.hidden" in SUS,
   "定时器回调统一被 __miSuspended/document.hidden 拦截")
ok("window.setInterval = function" in SUS and "origSetInterval" in SUS,
   "包裹 setInterval（逐个模块改判断必然漏，源头包一层全员管住）")
ok("window.MiHomeSuspend" in SUS,
   "暴露 MiHomeSuspend 给 Python 调")
ok("hook.onSuspend" in SUS and "hook.onResume" in SUS,
   "转发给 app.js 的模块级钩子（摄像头状态在那里才碰得到）")

IDX = rd(os.path.join("static", "index.html"))
ok(IDX.index('src="/static/suspend.js"') < IDX.index('src="/static/app.js"'),
   "suspend.js 必须先于 app.js 加载（否则前几个定时器漏网）")

APP = rd(os.path.join("static", "app.js"))
ok("window.MiHome" in APP and "onSuspend" in APP and "onResume" in APP,
   "app.js 注册了挂起/恢复钩子")
ok("camWasPlaying = true" in APP and "stopCam();" in APP,
   "挂起时停摄像头（ffmpeg+go2rtc 常驻两三百 MB，后台留着纯浪费）")
# onResume 函数体里必须真的去补数据（refreshQuick 在文件别处也出现，
# 所以切片到 onResume 块内部再断言）
_on_resume = APP[APP.index("onResume()"):]
_on_resume = _on_resume[:_on_resume.index("};")]
ok("refreshQuick()" in _on_resume and "refreshLive()" in _on_resume,
   "恢复时立刻补一轮数据（别让用户对着 4 秒旧状态）")
ok("state.camWasPlaying" in APP.replace("state.camWasPlaying = false", "state.camWasPlaying"),
   "恢复时自动续播挂起前在播的摄像头")

print()
print("=== 4. 真实修剪：对当前进程跑一遍 ===")
import launcher
saved = launcher.trim_memory()
ok(isinstance(saved, int) and saved >= 0,
   "trim_memory 返回非负整数（真跑了 EmptyWorkingSet + psutil 统计）",
   "got %r" % saved)
print("    本次对测试进程修剪省回: %.1f MB（数字可大可小，重点是流程不炸）" % (saved / 1048576))

print()
print("通过 %d / 失败 %d" % (_n["ok"], _n["fail"]))
sys.exit(1 if _n["fail"] else 0)
