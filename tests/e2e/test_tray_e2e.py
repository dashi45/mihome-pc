# -*- coding: utf-8 -*-
"""托盘端到端：真实启动 → 最小化入托盘 → 量内存 → 呼出 → 验前端解冻。

铁律（项目笔记）：
  · 起真实 GUI 必须用 ShellExecuteW —— Popen 起的会跟着终端作业对象被回收；
  · 启动/测量/收尾必须在**同一个脚本**里做完；
  · 访问 127.0.0.1 要绕系统代理；
  · 收尾必须杀**整棵进程树**（venv 的 python.exe 是 shim，真解释器是子进程）。

跳过条件：8765 已被占用（用户的实例正在跑，别去动它）。
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "tests", "tools"))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tests"))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
# 慢步日志阈值调到 0.05 秒：这次要抓"呼出为什么慢"，全量打点
os.environ["MIHOME_SLOW_FLOOR"] = "0.05"
os.environ["MIHOME_TRACE"] = "1"   # 无条件入口打点，看到底谁执行了恢复
# WebView2 开调试端口，前端挂起状态要用 CDP 验
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=9222"

u32 = ctypes.windll.user32
u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
u32.FindWindowW.restype = wintypes.HWND
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.IsWindowVisible.restype = wintypes.BOOL
u32.IsIconic.argtypes = [wintypes.HWND]
u32.IsIconic.restype = wintypes.BOOL
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                             wintypes.LPARAM]

WM_APP_TRAY = 0x8001      # 与 tray.py 的回调消息一致
WM_LBUTTONUP = 0x0202

TITLE = "米家 · 电脑版"
SW_MINIMIZE = 6

_n = {"ok": 0, "fail": 0}


def ok(cond, msg, detail=""):
    _n["ok" if cond else "fail"] += 1
    print(("  ✓ " if cond else "  ✗ ") + msg + ((" —— " + detail) if detail else ""))


def rss_tree(root_pid):
    """整棵子树的物理内存总和（MB）。"""
    import psutil
    total = 0
    try:
        procs = [psutil.Process(root_pid)] + \
            psutil.Process(root_pid).children(recursive=True)
    except Exception:
        return 0.0
    for p in procs:
        try:
            total += p.memory_info().rss
        except Exception:
            pass
    return total / 1048576.0


def tree_names(root_pid):
    import psutil
    names = []
    try:
        for c in psutil.Process(root_pid).children(recursive=True):
            try:
                names.append(c.name())
            except Exception:
                pass
    except Exception:
        pass
    return names


def kill_tree(pid):
    import psutil
    try:
        root = psutil.Process(pid)
        kids = root.children(recursive=True)
        for c in kids:
            try:
                c.kill()
            except Exception:
                pass
        try:
            root.kill()
        except Exception:
            pass
    except Exception:
        pass


def cdp_eval(js, timeout=8):
    """CDP 取页面状态。连不上返回 None（调用方自行决定算不算失败）。"""
    try:
        import cdp
        w = cdp.connect()
        w.s.settimeout(timeout)
        return cdp.eval_js(w, js)
    except Exception as e:
        print("    (CDP 不可用: %s)" % e)
        return None


def find_tray_window():
    """托盘隐藏窗口：标题是 mihome-pc-tray，但 EnumWindows 才找得到
    被藏起来的窗口 —— 简化：全枚举一次按标题比对。"""
    import launcher
    wins = launcher._enum_title_windows("mihome-pc-tray")
    return wins[0][0] if wins else None


def main():
    import launcher

    print("==== 0. 前置检查 ====")
    if launcher.port_open(8765, timeout=0.5):
        print("  8765 已被占用（你的实例在跑）。端到端需要独占端口，跳过。")
        print("通过 0 / 失败 0 (SKIPPED)")
        return 0

    exe = os.path.join(BASE, ".venv", "Scripts", "pythonw.exe")
    if not os.path.isfile(exe):
        print("  找不到 venv 的 pythonw，跳过")
        print("通过 0 / 失败 0 (SKIPPED)")
        return 0

    sh32 = ctypes.windll.shell32
    sh32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, ctypes.c_int]
    # launcher.py 传**绝对路径**：_is_own_cmdline 用 os.path.abspath() 解析命令行
# 参数，相对路径会按"跑测试时的 cwd"展开 —— 从项目根以外的地方跑，窗口
# 识别不出来，wait_window 60 秒空转后报"窗口没出现"（假故障，排查过两次）。
    rc = sh32.ShellExecuteW(None, "open", exe,
                            '"%s"' % os.path.join(BASE, "launcher.py"), BASE, 1)
    ok(rc > 32, "ShellExecuteW 启动实例（rc=%s）" % rc)
    if rc <= 32:
        print("通过 %d / 失败 %d" % (_n["ok"], _n["fail"]))
        return 1

    print("==== 1. 等窗口起来（最多 40 秒）====")
    hwnd = None
    t0 = time.time()
    while time.time() - t0 < 40:
        wins = launcher._our_windows(launcher._our_dirs(BASE))
        if wins:
            hwnd = wins[0][0]
            root_pid = wins[0][1]
            if launcher.port_open(8765, timeout=0.5):
                break
        time.sleep(1.0)
    ok(hwnd is not None, "窗口出现")
    ok(launcher.port_open(8765, timeout=1.2), "后端 8765 就绪")
    if not hwnd:
        kill_tree_pid = None
        print("通过 %d / 失败 %d" % (_n["ok"], _n["fail"]))
        return 1
    time.sleep(2.5)          # 等前端 loaded + 托盘起完

    print("==== 2. 初始状态 ====")
    ok(bool(u32.IsWindowVisible(hwnd)), "窗口可见")
    ok(find_tray_window() is not None, "托盘隐藏窗口已建（图标在）")
    mb0 = rss_tree(root_pid)
    names0 = tree_names(root_pid)
    print("    进程树内存: %.0f MB，成员 %d 个: %s" %
          (mb0, len(names0), ", ".join(sorted(set(names0))[:8])))

    print("==== 3. 最小化 → 应入托盘 ====")
    u32.ShowWindow(hwnd, SW_MINIMIZE)
    time.sleep(4.0)          # 挂起前端(停摄像头) → 隐藏 → trim，都要时间
    ok(not u32.IsWindowVisible(hwnd), "窗口已隐藏（不在任务栏，只剩托盘）")
    ok(find_tray_window() is not None, "托盘还在")
    mb1 = rss_tree(root_pid)
    print("    内存 %.0f MB → %.0f MB（%.0f MB）" % (mb0, mb1, mb0 - mb1))
    ok(mb1 < mb0 - 8, "整树工作集下降（修剪生效）")
    ok(launcher.port_open(8765, timeout=1.2), "后端还活着（挂起≠退出）")
    webview_alive = any(n.startswith("msedgewebview2") for n in names0) and \
        any(n.startswith("msedgewebview2") for n in tree_names(root_pid))
    ok(webview_alive, "WebView2 进程没被杀（呼出才能快）")

    susp = cdp_eval("!!window.__miSuspended")
    if susp is not None:
        ok(susp is True, "前端已挂起（__miSuspended === true）", repr(susp))
    else:
        print("  ⚠ CDP 连不上，前端挂起改由 tray_test 源码断言覆盖")

    print("==== 3b. 真实托盘点击（消息直接投给托盘窗口）====")
    # 事故回归：窗口曾建在主线程、消息循环跑在托盘线程 —— GetMessageW
    # 只收「调用线程自己创建的窗口」的消息，点击全躺在主线程队列里没人取
    # （webview.start 后主线程 Join 不泵消息），表现就是点托盘永远没反应。
    tray_hwnd = find_tray_window()
    ok(tray_hwnd is not None, "托盘窗口在")
    r = u32.PostMessageW(tray_hwnd, WM_APP_TRAY, 1, WM_LBUTTONUP)
    ok(bool(r), "点击消息已投递（PostMessage 成功）")
    time.sleep(3.0)          # 呼出 + 解冻前端
    ok(bool(u32.IsWindowVisible(hwnd)), "点托盘图标 → 窗口重新可见")
    ok(not u32.IsIconic(hwnd), "且不是最小化态（restore 生效）")

    print("==== 3c. 再藏回去，给第 4 步一个托盘态 ====")
    u32.ShowWindow(hwnd, SW_MINIMIZE)
    time.sleep(4.0)
    ok(not u32.IsWindowVisible(hwnd), "窗口已重新隐藏")

    print("==== 4. 呼出（走真实 IPC 路径）====")
    t0 = time.time()
    pong, perr = launcher.ipc_call(BASE, "ping", timeout=10.0)
    print("    ping: %.2f 秒 -> %r (%s)" % (time.time() - t0, pong, perr))
    ok(pong is not None and pong.get("ok"), "IPC ping 应答（管道层正常）")
    t0 = time.time()
    reply, _err = launcher.ipc_call(BASE, "show", timeout=25.0)
    dt = time.time() - t0
    ok(reply is not None and reply.get("state") == "shown",
       "IPC show 应答 shown", repr(reply))
    time.sleep(1.5)
    ok(bool(u32.IsWindowVisible(hwnd)), "窗口重新可见")
    ok(not u32.IsIconic(hwnd), "窗口不是最小化态（restore 生效）")
    ok(dt < 6.0, "呼出耗时 %.1f 秒（<6s）" % dt)
    if susp is not None:
        susp2 = cdp_eval("!!window.__miSuspended")
        ok(susp2 is False, "前端已解冻（__miSuspended === false）", repr(susp2))

    print("==== 4b. 关闭询问弹窗（真实 js_api 桥）====")
    # 点 X 的第一段：closing 拦截 → evaluate_js MiHomeCloseAsk → 页面弹窗出现
    v = cdp_eval("window.MiHomeCloseAsk ? MiHomeCloseAsk() : false")
    ok(v is True, "MiHomeCloseAsk 返回 true（页面弹窗已触发）", repr(v))
    hidden = cdp_eval(
        "document.querySelector('#close-modal').classList.contains('hidden')")
    ok(hidden is False, "#close-modal 可见", repr(hidden))
    # 第二段：选「最小化到托盘」（不记住）→ 经 pywebview.api 回传 Python
    cdp_eval("pywebview.api.close_choice('tray', false)")
    time.sleep(4.0)          # suspend → hide → trim
    ok(not u32.IsWindowVisible(hwnd), "选「最小化到托盘」→ 窗口隐藏（没有直接退出）")
    ok(launcher.port_open(8765, timeout=1.2), "后端还活着（托盘 ≠ 退出）")
    susp3 = cdp_eval("!!window.__miSuspended")
    if susp3 is not None:
        ok(susp3 is True, "选托盘 → 前端挂起（与最小化入托盘同一状态）", repr(susp3))
    pref = os.path.join(BASE, "ui_close_pref.json")
    ok(not os.path.isfile(pref), "不记住 → ui_close_pref.json 没被写出来")

    print("==== 5. 收尾 ====")
    kill_tree(root_pid)
    time.sleep(1.0)
    print("通过 %d / 失败 %d" % (_n["ok"], _n["fail"]))
    return 1 if _n["fail"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # 保险：不管怎么退出都别把用户的 8765 占着（只有本测试起的才杀得掉，
        # 用户实例在开跑前就被前置检查挡住了）
        pass
