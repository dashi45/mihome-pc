# -*- coding: utf-8 -*-
"""僵尸实例清理与进程识别的单元测试

场景：后端被杀了，但窗口进程还活着 —— 用户双击图标，程序永远起不来。
验证 launcher.kill_zombies() 能把这种残留清干净，**且不会误杀别的进程**。

安全约束（上一版翻车留下的教训）：
  · 必须用 python -u 跑，输出不缓冲；否则进程被 kill 时缓冲区里的
    print 全丢，命令看起来像"卡死无输出"
  · 先 dry_run 打印命中列表，确认无误杀再真杀
  · 断言"测试脚本自己（父链）没被杀"
"""
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

import launcher  # noqa: E402

# 本测试用 tests/tools/probe_zorder.py 造一个同名窗口，它不在生产的关键文件名单里，
# 这里临时加进来，让它能被识别成"自己人"（等价于 launcher.py / app.py）。
launcher._OWN_FILES = launcher._OWN_FILES + ("probe_zorder.py",)
launcher._OWN_LOWER = tuple(n.lower() for n in launcher._OWN_FILES)


def port_open(p):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", p), 0.3):
            return True
    except OSError:
        return False


def find_window():
    u32 = launcher.ctypes.windll.user32
    return u32.FindWindowW(None, launcher.TITLE)


def out(*a):
    print(*a, flush=True)


out("=== 前置检查 ===")
# 只查 PORT_WEB 是不够的。踩过的坑：用 MIHOME_PORT=8799 跑这个测试时，
# 端口检查看到 8799 空着就放行，但 kill_zombies 是按"窗口标题 + 可执行
# 文件在项目目录里"去匹配的 —— 它把 8765 上那个**正常在跑**的实例当成
# 僵尸清掉了（连 米家.exe 和 WebView2 一起）。测试可以失败，不能杀用户
# 正在用的程序。所以这里改成：只要本项目目录下有 app.py / 米家.exe 在跑，
# 一律不测。
def busy_procs():
    try:
        import psutil
    except ImportError:
        return []
    base = BASE.lower().replace("\\", "/")
    hits = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            nm = (p.info["name"] or "").lower()
            cl = " ".join(p.info["cmdline"] or []).lower().replace("\\", "/")
            if nm == "米家.exe" or ("app.py" in cl and base in cl):
                hits.append((p.info["pid"], p.info["name"]))
        except Exception:
            continue
    return hits


busy = busy_procs()
if port_open(launcher.PORT_WEB) or busy:
    out("❌ 有实例在跑，不能测")
    out("   端口 %d %s" % (launcher.PORT_WEB,
                          "被占" if port_open(launcher.PORT_WEB) else "空闲"))
    if busy:
        out("   本项目进程还在跑: %r" % (busy,))
    out("   原因：kill_zombies 按窗口标题+目录匹配，分不清「真在跑」和「僵尸」，")
    out("        会把你正在用的实例一起清掉。请先正常退出程序再跑这个测试。")
    sys.exit(2)
out("没有实例在跑：端口 %d 空闲，项目进程 0 个" % launcher.PORT_WEB)

# ---- 单元断言：匹配规则必须足够严格 ----
out()
out("=== 匹配规则自检 ===")
dirs = launcher._our_dirs(BASE)
cases = [
    (["python", os.path.join(BASE, "launcher.py")], True, "本目录的 launcher.py"),
    (["python", "C:/somewhere/else/launcher.py"], False, "别的目录的 launcher.py"),
    (["python", "C:/somewhere/else/my_launcher.py.bak"], False, "仅含子串的假文件名"),
    (["python", "C:/tools/米家.exe"], False, "别的目录的 米家.exe"),
    ([], False, "空命令行"),
]
ok_rule = True
for cmd, want, why in cases:
    got = launcher._is_our_launcher(cmd, dirs)
    flag = "OK " if got == want else "FAIL"
    if got != want:
        ok_rule = False
    out("  [%s] %-28s -> %s (期望 %s)" % (flag, why, got, want))
out("匹配规则自检：", "通过" if ok_rule else "不通过")
if not ok_rule:
    sys.exit(3)

# ---- 回归保护：tests/ 里的脚本**绝不能**被当成"自己人" ----
# 踩过的坑：早先的判定是"命令行里有参数在本程序目录下就算自己人"，
# 结果 launcher 启动时 kill_zombies 把正在跑测试的进程（连同它的父进程）
# 全杀了 —— 表现为"程序自己把会话关掉了"。这条断言就是防它再犯。
out()
out("=== 回归保护：非关键文件不得被认成自己人 ===")
out("  当前测试脚本被认成自己人吗:", launcher._is_our_process(os.getpid(), dirs),
    " ← 必须是 False")
if launcher._is_our_process(os.getpid(), dirs):
    out("❌ 关键文件名单被放宽了，会误杀无关进程")
    sys.exit(7)

# ---- 造一个"窗口在、后端死"的僵尸实例 ----
# 注意：这里**不能**复制 launcher.py 再追加代码 —— 那样会把 launcher 的
# main() 一起执行，那造出来的就不是"僵尸"，而是一个完整实例（后端都起来了），
# kill_zombies 反而会正确地拒绝动手。必须是独立脚本：只有同名窗口，没有后端。
# 用 tests/tools/probe_zorder.py（纯 Win32 实现，venv 里没装 tkinter）。
tmp = os.path.join(BASE, "tests", "tools", "probe_zorder.py")
if not os.path.isfile(tmp):
    print("缺少窗口脚本:", tmp)
    sys.exit(5)

out()
out("=== 造僵尸窗口 ===")
p = subprocess.Popen([sys.executable, "-u", tmp],
                     creationflags=0x08000000,   # CREATE_NO_WINDOW
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)
out("shim pid =", p.pid)
wins = launcher._enum_title_windows(launcher.TITLE)
win_pids = {pid for _h, pid in wins}
out("同名窗口数 =", len(wins), " 窗口所属 pid =", win_pids)
out("窗口在吗:", bool(wins))
out("后端在吗:", port_open(launcher.PORT_WEB))
if not win_pids:
    out("❌ 没找到窗口，测不了")
    p.kill()
    sys.exit(6)

# ---- dry-run：只查不杀 ----
out()
out("=== dry_run（只查不杀）===")
hits = launcher.kill_zombies(dry_run=True)
out("命中:", hits)
hit_pids = {h[0] for h in hits} if isinstance(hits, list) else set()
dirs = launcher._our_dirs(BASE)
# 窗口真正所属的解释器必须在命中名单里（shim 也会被命中，一并清掉才对）
out("命中包含窗口所属进程:", win_pids <= hit_pids)
# 命中的每一个都必须经得起"是不是自己人"的复核
out("命中项全是自己人:", all(launcher._is_our_process(p, dirs) for p in hit_pids))
out("脚本自己（%d）在命中名单里吗（绝不该在）: %s" % (os.getpid(), os.getpid() in hit_pids))

if not (win_pids <= hit_pids) or os.getpid() in hit_pids:
    out("❌ 命中名单不符合预期，不执行清理")
    try:
        p.kill()
    except Exception:
        pass
    sys.exit(4)

# ---- 真杀：先验证 kill_zombies 本身 ----
out()
out("=== kill_zombies 真清理 ===")
n = launcher.kill_zombies()
time.sleep(1.5)
alive = p.poll() is None
left = launcher._enum_title_windows(launcher.TITLE)
out("清理数 =", n, " 必须 >= 1")
out("僵尸还活着吗:", alive, " 必须为 False")
out("窗口还在吗:", bool(left), " 必须为 False")
out("脚本自己还活着吗:", True, "（能跑到这行就说明父链没被误杀）")

ok_kill = (n >= 1) and (not alive) and not left

for pid in win_pids:
    try:
        os.kill(pid, 9)
    except Exception:
        pass
try:
    p.kill()
except Exception:
    pass
# tmp 是常驻的测试辅助脚本（tests/tools/probe_zorder.py），不删

# ---- 再单独验证 focus_existing：窗口在、后端死 → 必须判定为"废窗口" ----
out()
out("=== focus_existing() 判定（另造一个废窗口）===")
p2 = subprocess.Popen([sys.executable, "-u", tmp],
                      creationflags=0x08000000,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)
out("后端在吗（必须 False）:", port_open(launcher.PORT_WEB))
fe = launcher.focus_existing()
out("focus_existing() =", fe, " 必须为 False（废窗口不能当成'已在运行'）")
time.sleep(1.5)
out("废窗口被关掉了吗:", not launcher._enum_title_windows(launcher.TITLE))
try:
    p2.kill()
except Exception:
    pass

out()
ok = ok_kill and (fe is False)
out("✅ 僵尸清理有效" if ok else "❌ 僵尸清理没生效")
sys.exit(0 if ok else 1)
