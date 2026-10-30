# -*- coding: utf-8 -*-
"""退出未响应回归：真实窗口 → 点 ✕(WM_CLOSE) → 页面弹窗 → 真实点按钮。

复刻用户报障路径（本测试曾只 CDP 直调 close_choice，绕过了 GUI 线程
死锁环节，把 on_closing 里的同步 evaluate_js 漏网了——那才是真正死因）：

  历史死因：on_closing 在 GUI 线程同步 evaluate_js，Python 信号量等待
  不泵 Windows 消息，JS 完成回调排在 GUI 队列没人取 → 窗口未响应，
  弹窗之后的任何点击（经 WebMessageReceived，也要 GUI 线程）全无效。

  现行为：on_closing 只决策 + 置标志，evaluate_js / hide_to_tray 全丢
  后台线程；close_choice(exit) 立即返回、0.3s 后经 Timer 异步销毁。

判定口径（防假阴性）：
  · 退出 = 标题窗口消失；进托盘 = 窗口隐藏（句柄还在）；
  · 「窗口消失后 20s 内 root 进程退出且 8765 关闭」= 收尾干净；
  · 任一超时 → 判定未响应复现，记失败并杀整棵树收尾。

跳过条件：8765 已被占用（用户的实例正在跑，别去动它）。

铁律（项目笔记）：
  · 起真实 GUI 必须用 ShellExecuteW —— Popen 起的会跟着终端作业对象被回收；
  · 收尾必须杀**整棵进程树**（venv 的 python.exe 是 shim，真解释器是子进程）。
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
# WebView2 开调试端口，弹窗可见性/按钮点击都经 CDP 驱动
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=9222"

WM_CLOSE = 0x0010
QUIT_WINDOW_TIMEOUT = 15.0    # 点按钮后窗口这么久不变 = 未响应
QUIT_PROCESS_TIMEOUT = 20.0   # 窗口消失后 root 进程这么久不退 = 收尾卡死

_n = {"ok": 0, "fail": 0}


def ok(cond, msg, detail=""):
    _n["ok" if cond else "fail"] += 1
    print(("  ✓ " if cond else "  ✗ ") + msg + ((" —— " + detail) if detail else ""))


def pid_alive(pid):
    import psutil
    try:
        psutil.Process(pid)
        return True
    except Exception:
        return False


def kill_tree(pid):
    import psutil
    try:
        for c in psutil.Process(pid).children(recursive=True):
            try:
                c.kill()
            except Exception:
                pass
        psutil.Process(pid).kill()
    except Exception:
        pass


def cdp_eval(js, timeout=8):
    try:
        import cdp
        w = cdp.connect()
        w.s.settimeout(timeout)
        return cdp.eval_js(w, js)
    except Exception as e:
        print("    (CDP 不可用: %s)" % e)
        return None


def wait_window(timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        wins = launcher._our_windows(launcher._our_dirs(BASE))
        if wins and launcher.port_open(8765, timeout=0.5):
            return wins[0]
        time.sleep(1.0)
    return None


def wait_js_bridge(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if cdp_eval("!!(window.pywebview && window.pywebview.api)") is True:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def dialog_visible():
    try:
        return cdp_eval("!!(document.querySelector('#close-modal') && "
                        "!document.querySelector('#close-modal').classList.contains('hidden'))")
    except Exception:
        return None


def scenario(btn_sel, name, u32):
    """点 ✕ → 等弹窗 → 真实点击按钮。返回结果串。"""
    wins = launcher._our_windows(launcher._our_dirs(BASE))
    if not wins:
        ok(False, "%s：窗口不在" % name)
        return "NO-WINDOW"
    hwnd = wins[0][0]
    to_tray = btn_sel == "#close-to-tray"

    def done():
        if to_tray:
            return bool(u32.IsWindow(hwnd)) and not bool(u32.IsWindowVisible(hwnd))
        return not launcher._our_windows(launcher._our_dirs(BASE))

    u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    shown, t0 = False, time.time()
    while time.time() - t0 < 10:
        if dialog_visible() is True:
            shown = True
            break
        time.sleep(0.5)
    ok(shown, "%s：点 ✕ 后询问弹窗显示" % name,
       "" if shown else "弹窗没出来（on_closing 链路异常）")
    if not shown:
        return "NO-DIALOG"

    t0 = time.time()
    cdp_eval("document.querySelector('%s').click()" % btn_sel)
    result = "TIMEOUT"
    while time.time() - t0 < QUIT_WINDOW_TIMEOUT:
        if done():
            result = "%s in %.1fs" % ("TRAYED" if to_tray else "CLOSED",
                                      time.time() - t0)
            break
        time.sleep(0.5)
    ok(result != "TIMEOUT",
       "%s：%s%s（超时 %ds = 未响应复现）"
       % (name, "进托盘" if to_tray else "退出",
          " %s" % result if result != "TIMEOUT" else "", QUIT_WINDOW_TIMEOUT),
       "" if result != "TIMEOUT" else "按钮点击后无反应")
    return result


def main():
    global launcher
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

    u32 = ctypes.windll.user32
    root_pid = None
    try:
        print("==== 1. 等窗口 + 前端就绪（最多 60 秒）====")
        got = wait_window()
        ok(got is not None, "窗口出现且后端 8765 就绪")
        if not got:
            print("通过 %d / 失败 %d" % (_n["ok"], _n["fail"]))
            return 1
        root_pid = got[1]
        ok(wait_js_bridge(), "pywebview JS 桥就绪")

        print("==== 2. 点 ✕ → 弹窗 → 退出程序（用户报障路径）====")
        r = scenario("#close-exit", "退出", u32)

        if r.startswith(("CLOSED", "TRAYED")):
            t1 = time.time()
            exited = False
            while time.time() - t1 < QUIT_PROCESS_TIMEOUT:
                if not pid_alive(root_pid):
                    exited = True
                    break
                time.sleep(0.5)
            ok(exited, "root 进程在窗口关闭后 %.1fs 内退干净（杀后端/收托盘/关管道）"
               % (time.time() - t1 if exited else QUIT_PROCESS_TIMEOUT),
               "" if exited else "进程残留，收尾卡死（看门狗应兜底强退）")
            ok(not launcher.port_open(8765, timeout=0.5),
               "后端 8765 已随退出关闭（子进程树被收干净）")
        else:
            ok(False, "退出路径未走通，无法做收尾断言")
    finally:
        if root_pid and pid_alive(root_pid):
            kill_tree(root_pid)
            time.sleep(1.0)
            ok(not pid_alive(root_pid), "收尾：进程树已清（无残留窗口/孤儿后端）")

    print("通过 %d / 失败 %d" % (_n["ok"], _n["fail"]))
    return 1 if _n["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
