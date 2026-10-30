# -*- coding: utf-8 -*-
"""进程分类（_classify）回归测试。

问题背景：
  上一轮把 _classify 的签名从 (name, exe, has_win) 改成 (name, exe, has_win, pid, ppid)，
  但调用点忘了一起改 → 进程页整个接口 500（"_classify() missing 2 required positional arguments"）。
  同时新逻辑引用了 _BASE_LE / _VENV_LE 两个常量，但没定义 → NameError。
  这两个都是"改了签名/常量忘了同步"的低级错误，靠测试兜住。

判定原则（跟 UI 承诺一致）：
  · Windows 关键进程 → sys，一律保护
  · 项目目录里的部件（go2rtc/ffmpeg）→ bg，保护
  · 项目 .venv 里的解释器 → 只有正好是自己/父进程才算"自己"并保护；
    别人拿同一个解释器跑脚本 → 正常可杀（否则用户自己的脚本再也关不掉）
  · 系统目录里没有窗口的进程 → sys，保护（不认识就不动）
  · 有窗口的普通程序 → app，可杀
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import procman as P

OK = 0
FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


BASE = P._BASE_DIR
VENV = os.path.join(BASE, ".venv")
WIN = os.environ.get("SystemRoot") or "C:\\Windows"

print("=" * 62)
print("_classify 分类回归测试")
print("=" * 62)
print("_BASE_LE  =", P._BASE_LE)
print("_VENV_LE  =", P._VENV_LE)
print("_SYSTEMROOT =", P._SYSTEMROOT)
print()

# --- 常量存在性 ---
check("_BASE_LE 已定义", isinstance(getattr(P, "_BASE_LE", None), str) and P._BASE_LE.endswith("\\"))
check("_VENV_LE 已定义", isinstance(getattr(P, "_VENV_LE", None), str) and P._VENV_LE.endswith("\\"))
check("_VENV_LE 在 _BASE_LE 之内", P._VENV_LE.startswith(P._BASE_LE))
check("_SYSTEMROOT 已定义", isinstance(getattr(P, "_SYSTEMROOT", None), str))

# --- Windows 关键进程 ---
for nm in ("smss.exe", "lsass.exe", "svchost.exe", "explorer.exe", "System"):
    kind, guard = P._classify(nm, os.path.join(WIN, "System32", nm), False, 999001, 0)
    check("关键进程 %-14s → sys 且受保护" % nm, kind == "sys" and bool(guard),
          "got=%r" % ((kind, guard),))

# --- 项目目录里的部件 ---
kind, guard = P._classify("go2rtc.exe", os.path.join(BASE, "go2rtc", "go2rtc.exe"), False, 999002, 0)
check("项目内 go2rtc → bg 且受保护", kind == "bg" and bool(guard), "got=%r" % ((kind, guard),))

kind, guard = P._classify("ffmpeg.exe", os.path.join(BASE, "go2rtc", "ffmpeg.exe"), False, 999003, 0)
check("项目内 ffmpeg → bg 且受保护", kind == "bg" and bool(guard), "got=%r" % ((kind, guard),))

# --- .venv 解释器：只有自己才受保护 ---
me_py = os.path.join(VENV, "Scripts", "python.exe")
kind, guard = P._classify("python.exe", me_py, False, P._ME, 0)
check(".venv python（自己）→ bg 且受保护", kind == "bg" and bool(guard), "got=%r" % ((kind, guard),))

kind, guard = P._classify("python.exe", me_py, False, P._ME_PARENT[0], 0)
check(".venv python（父进程）→ bg 且受保护", kind == "bg" and bool(guard), "got=%r" % ((kind, guard),))

kind, guard = P._classify("python.exe", me_py, False, 999004, 0)
check(".venv python（别人的脚本）→ 可杀（不带保护）", guard == "", "guard=%r" % (guard,))
check(".venv python（别人的脚本）→ 归为 bg", kind == "bg", "kind=%r" % (kind,))

# --- 系统目录里无窗口的进程 → sys 保护 ---
kind, guard = P._classify("sihost.exe", os.path.join(WIN, "System32", "sihost.exe"), False, 999005, 0)
check("系统目录无窗口进程 → sys 且受保护", kind == "sys" and bool(guard),
      "got=%r" % ((kind, guard),))

# --- 普通前台程序 → app 可杀 ---
kind, guard = P._classify("notepad.exe", "C:\\Program Files\\Notepad\\notepad.exe", True, 999006, 0)
check("有窗口的普通程序 → app 可杀", kind == "app" and guard == "", "got=%r" % ((kind, guard),))

kind, guard = P._classify("Code.exe", "C:\\Program Files\\Microsoft VS Code\\Code.exe", True, 999007, 0)
check("VS Code → app 可杀", kind == "app" and guard == "", "got=%r" % ((kind, guard),))

# --- 读不到路径的进程：受保护进程不能杀 ---
kind, guard = P._classify("Protected.exe", "", False, 999008, 0)
check("读不到路径 + 无窗口 → sys 受保护", kind == "sys" and bool(guard), "got=%r" % ((kind, guard),))

kind, guard = P._classify("BigApp.exe", "", True, 999009, 0)
check("读不到路径 + 有窗口 → app 但提示管理员权限", kind == "app" and bool(guard),
      "got=%r" % ((kind, guard),))

# --- 真实扫描：接口能跑通、分类自洽 ---
print()
print("=" * 62)
print("真实扫描自检")
print("=" * 62)
try:
    d = P.view(group="all", sort="cpu", q="", limit=0)
    items = d.get("items") or []
    check("view() 不报错", d.get("ok") is True, "err=%s" % (d.get("error"),))
    check("扫到进程", len(items) > 0, "n=%d" % len(items))
    check("每条都有 kind", all(p.get("kind") in ("app", "bg", "sys") for p in items))

    # group=all 是"应用 + 后台"，系统进程默认折叠（UI 顶部会显示"已折叠 N 个"）
    check("group=all 默认不含系统进程",
          all(p.get("kind") != "sys" for p in items),
          "混进了 %d 个 sys" % sum(1 for p in items if p.get("kind") == "sys"))

    # group=sys 才把系统进程端出来
    ds = P.view(group="sys", sort="cpu", q="", limit=0)
    sysitems = ds.get("items") or []
    check("group=sys 能取到系统进程", len(sysitems) > 0, "n=%d" % len(sysitems))
    check("group=sys 全是 sys", all(p.get("kind") == "sys" for p in sysitems))

    # 自己被标记为 me，而且**不能被结束**（这才是真正的防线断言）
    me_rows = [p for p in items if p.get("me")]
    check("自己被标记为 me", len(me_rows) > 0, "没找到自己")
    check("自己不能被结束（can_kill 拒绝）",
          all(not P.can_kill(p)[0] for p in me_rows),
          "can_kill 竟然放行了自己")
    check("自己的进程带保护说明",
          all(p.get("guard") for p in me_rows if p.get("exe", "").lower().startswith(P._VENV_LE)),
          "自己没被保护")

    # 关键进程一个都不能标成可杀
    bad = [p for p in items
           if (p.get("name") or "").lower() in P._CRITICAL and not p.get("guard")]
    check("没有关键进程漏保护", not bad, "漏=%r" % ([p.get("name") for p in bad],))

    # 系统进程组里也必须全部受保护
    bad2 = [p for p in sysitems if not p.get("guard")]
    check("系统进程全部受保护", not bad2,
          "漏=%r" % ([p.get("name") for p in bad2][:5],))

    # 性能：一次扫描必须在 200ms 以内（实测 ~10ms）
    cost = d.get("cost")
    check("扫描耗时 < 200ms", cost is not None and cost < 200, "cost=%s ms" % cost)
    print("  扫描 %d 个进程（应用+后台 %d，系统 %d），耗时 %.1f ms，分类：%s" % (
        len(items), len(items), len(sysitems), cost or 0,
        {k: sum(1 for p in items if p.get("kind") == k) for k in ("app", "bg")}))
except Exception as e:
    check("view() 不报错", False, "raised %r" % (e,))

print()
print("=" * 62)
print("结果: %d 通过, %d 失败" % (OK, FAIL))
print("=" * 62)
sys.exit(1 if FAIL else 0)
