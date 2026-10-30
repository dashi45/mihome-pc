# -*- coding: utf-8 -*-
"""关闭行为询问（点 X → 退出 / 最小化到托盘 + 记住选择）的守卫测试。

一部分是源码契约：closing 事件、js_api 桥、页面弹窗、原生兜底，
任何一环被改坏都只会表现为"点 X 直接退出"或"点 X 卡死关不掉"，
用文本断言钉住。

另一部分是真实逻辑：ui_close_pref.json 的读写/清除、close_choice
的分支行为（monkeypatch 掉窗口动作，不碰真实窗口）。
生产文件隔离铁律：_close_pref_path 一律重定向到临时目录，并在收尾
断言生产路径没被碰过。
"""
import io
import json
import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

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


# ---- 生产文件隔离：先记下生产 pref 文件的状态 ----
import launcher
_PROD_PREF = os.path.join(BASE, "ui_close_pref.json")
_prod_before = open(_PROD_PREF, "rb").read() if os.path.isfile(_PROD_PREF) else None
_prod_mtime = os.path.getmtime(_PROD_PREF) if os.path.isfile(_PROD_PREF) else None

TMP = tempfile.mkdtemp(prefix="mihome-close-test-")
launcher._close_pref_path = lambda base=None: os.path.join(TMP, "ui_close_pref.json")

print("=== 1. 关闭行为记忆的读写与清除 ===")
ok(launcher._load_close_pref() is None, "没保存过 → None")
launcher._save_close_pref("tray")
ok(launcher._load_close_pref() == "tray", "保存 tray → 读回 tray")
ok(os.path.isfile(launcher._close_pref_path()), "文件真实落盘")
launcher._save_close_pref("exit")
ok(launcher._load_close_pref() == "exit", "覆盖为 exit → 读回 exit")
launcher._save_close_pref("bogus")
ok(launcher._load_close_pref() is None, "存了非法值 → 清除，读回 None")
launcher._save_close_pref("tray")
os.remove(launcher._close_pref_path())
ok(launcher._load_close_pref() is None, "文件不存在 → None（不炸）")
with open(launcher._close_pref_path(), "w", encoding="utf-8") as f:
    f.write("{oops not json")
ok(launcher._load_close_pref() is None, "文件损坏 → None（宁可多问一次，不当成 tray/exit）")
os.remove(launcher._close_pref_path())

print()
print("=== 2. close_choice 分支（monkeypatch 窗口动作） ===")
calls = {"tray": 0, "quit": 0}
launcher.hide_to_tray = lambda: calls.__setitem__("tray", calls["tray"] + 1)
launcher._tray_quit = lambda: calls.__setitem__("quit", calls["quit"] + 1)
api = launcher._CloseApi()

launcher._FORCE_QUIT = False
api.close_choice("tray", remember=False)
ok(calls["tray"] == 1, "action=tray → 走 hide_to_tray")
ok(not os.path.exists(launcher._close_pref_path()),
   "remember=False 不落盘（用户没让记住）")
api.close_choice("tray", remember=True)
ok(launcher._load_close_pref() == "tray", "remember=True → 记住 tray")
api.close_choice("exit", remember=True)
ok(launcher._load_close_pref() == "exit", "改选 exit + 记住 → 覆盖为 exit")
ok(launcher._FORCE_QUIT is True, "action=exit → _FORCE_QUIT 置位")
ok(calls["quit"] == 0, "close_choice 返回时 destroy 尚未触发（必须先返回，防 JS 桥死锁）")
time.sleep(0.9)   # Timer 0.3s + 回传余量
ok(calls["quit"] == 1, "短暂延迟后 _tray_quit 被调用（异步销毁窗口，绕开 JS 桥死锁）")
launcher._FORCE_QUIT = False
api.close_choice("nonsense", remember=False)
ok(launcher._FORCE_QUIT is False and calls["quit"] == 1 and calls["tray"] == 2,
   "未知 action → 什么都不做（不误退出）")
ok(launcher._CLOSE_ASKING is False, "回传选择后 _CLOSE_ASKING 复位（下次 X 能再弹）")

print()
print("=== 3. 源码契约：closing 拦截与兜底 ===")
L = rd("launcher.py")
ok("win.events.closing += on_closing" in L, "closing 事件接到 on_closing")
ok("js_api=_CloseApi()" in L, "窗口带 js_api 桥（页面弹窗回传选择的通道）")
ok("_FORCE_QUIT" in L, "有强制退出标志")
_on_closing = L[L.index("def on_closing():"):L.index("def _ask_close")]
ok(_on_closing.index("if _FORCE_QUIT") < _on_closing.index("_load_close_pref()"),
   "强制退出优先于一切（托盘菜单退出不能再被拦）")
ok("win.evaluate_js(" not in _on_closing,
   "on_closing（GUI 线程）不同步 evaluate_js——Python 信号量等待不泵消息、"
   "JS 完成回调排在 GUI 队列没人取，窗口未响应（实测必现）")
ok("threading.Thread(target=hide_to_tray" in _on_closing
   and "threading.Thread(target=_ask_close" in _on_closing,
   "关窗动作（进托盘/弹询问）全部丢后台线程执行")
ok(_on_closing.index('if pref == "tray"') < _on_closing.index("threading.Thread"),
   "记住「最小化到托盘」的隐藏动作也不走 GUI 线程（同一死锁面）")
_cc = L[L.index("def close_choice"):L.index('elif action == "tray"')]
ok("threading.Timer" in _cc and "_tray_quit" in _cc,
   "exit 分支经 Timer 异步销毁（JS 桥线程里同步 destroy 会互等成环、窗口未响应）")
ok("_quit_watchdog" in L and "_QUIT_DONE" in L,
   "destroy 有看门狗兜底：主循环卡死也能清理子进程并强制退出")
_ac = L[L.index("def _ask_close"):L.index("win.events.loaded")]
ok("MiHomeCloseAsk" in _ac and "evaluate_js" in _ac,
   "后台线程把弹窗画到页面上")
ok("MessageBoxW" in _ac and "0x24" in _ac,
   "页面没就绪时原生三键弹窗兜底（绝不把用户困在关不掉的窗口里）")
ok("_tray_quit()" in _ac,
   "原生弹窗选「退出」与页面弹窗同路（_FORCE_QUIT + destroy）")
ok("_CLOSE_ASKING" in L, "有防重复触发标志（弹窗开着再点 X 不叠加）")
ok("on_error=lambda w, e: log_err(w, e)" in L,
   "托盘异常接到 log_err（不再静默吞成'点了没反应'）")
ok("_tray_show" in L and "log_err(\"tray.show\"" in L,
   "托盘呼出回调自身也包了错误落盘")
ok("_sync_tray_extra()" in L and "_reset_close_pref" in L,
   "记住的选择有托盘菜单恢复入口")
ok("os.replace(tmp, _close_pref_path())" in L,
   "pref 用 tmp+replace 原子写（不留半截文件）")

TRAY = rd("tray.py")
ok("MENU_EXTRA" in TRAY and "extra_label" in TRAY,
   "托盘菜单支持附加项渲染")

APP = rd(os.path.join("static", "app.js"))
ok("window.MiHomeCloseAsk" in APP, "前端暴露 MiHomeCloseAsk 给 evaluate_js")
ok("pywebview.api.close_choice" in APP, "前端经 pywebview.api 回传选择")
ok("close-remember" in APP, "记住选择复选框被读取并回传")

IDX = rd(os.path.join("static", "index.html"))
ok('id="close-modal"' in IDX and 'id="close-to-tray"' in IDX
   and 'id="close-exit"' in IDX and 'id="close-cancel"' in IDX,
   "index.html 有完整弹窗结构（托盘/退出/取消三键 + 复选框）")
CSS = rd(os.path.join("static", "style.css"))
ok(".close-acts" in CSS and ".close-remember" in CSS, "弹窗样式就位")

print()
print("=== 4. 生产文件守卫 ===")
ok(not os.path.exists(_PROD_PREF) and _prod_before is None
   or (os.path.isfile(_PROD_PREF) and open(_PROD_PREF, "rb").read() == _prod_before),
   "生产 ui_close_pref.json 没被测试碰过")
ok(not os.listdir(TMP) or all(n.endswith(".json") for n in os.listdir(TMP)),
   "临时目录里只有测试自己的 json")
try:
    os.remove(launcher._close_pref_path())
except OSError:
    pass

print()
print("通过 %d / 失败 %d" % (_n["ok"], _n["fail"]))
sys.exit(1 if _n["fail"] else 0)
