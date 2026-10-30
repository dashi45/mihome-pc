# -*- coding: utf-8 -*-
"""启动速度的回归守卫：端口探测必须走"查监听表"，不许退回"真连一次"。

## 为什么要守这条

本机（以及不少 Windows 机器）对**未监听**的本地端口不回 RST，而是直接丢包。
于是 socket.create_connection 到空端口不是"立刻失败"，而是要烧满 timeout
—— 实测正好 350ms。

launcher 启动路径上 port_open() 会被调用 4~5 次（8765 ×2、8554、1984…），
一旦退回真连实现，就是**白等约 1.4 秒**：双击图标后愣两三秒才出窗口，
而且没有任何报错。这种"慢但没有错误"的退化最难在 review 里看出来，
所以用断言钉住：

  1. 正确性：真在监听 → True；没人听 → False（两边都不能误判）
  2. 速度：单次探测必须远低于 timeout（否则说明又去真连了）
  3. 结构：kill_zombies 必须先过"有没有同名窗口"这道 1ms 的门，
     再进 psutil 全进程扫描（实测 330ms）

纯查表、纯文本断言，不开窗口、不杀进程、不联网。
"""
import os
import socket
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

import launcher  # noqa: E402

FAIL = []


def out(*a):
    print(*a, flush=True)


def ok(cond, label, detail=""):
    out(("  ✅ " if cond else "  ❌ ") + label + (("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(label)


def ms(t0):
    return (time.perf_counter() - t0) * 1000.0


out("=== 1. 监听表可用吗（ctypes → iphlpapi）===")
ports = launcher._listen_ports()
out("  监听表:", "可用，共 %d 个 LISTEN 端口" % len(ports) if ports is not None else "不可用（None）")
ok(ports is not None, "本机能拿到 LISTEN 端口表",
   "拿不到时 port_open 会回退真连，启动会慢 1.4s")
if ports is not None and launcher.PORT_WEB in ports:
    out("  注意：%d 上有别的实例在听（不影响本测试）" % launcher.PORT_WEB)

out()
out("=== 2. 正确性：真监听 vs 空端口 ===")
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(8)
live_port = srv.getsockname()[1]
out("  造一个真在监听的端口:", live_port)

# 缓存 20ms，探测前先等一下，别吃到上一次的表快照
time.sleep(0.05)
ok(launcher.port_open(live_port) is True, "真监听 → True", "port=%d" % live_port)

# 找一个确实空着的端口：先 bind 到 0 拿到号，再关掉，保证没人听
probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
probe.bind(("127.0.0.1", 0))
dead_port = probe.getsockname()[1]
probe.close()
time.sleep(0.05)
ok(launcher.port_open(dead_port) is False, "没人听 → False", "port=%d" % dead_port)
srv.close()
time.sleep(0.05)

out()
out("=== 3. 速度：探测空端口不许等超时 ===")
if launcher._listen_ports() is None:
    out("  跳过：本机监听表不可用，port_open 只能真连（有兜底，非致命）")
else:
    t0 = time.perf_counter()
    launcher.port_open(dead_port)
    one = ms(t0)
    # timeout 是 350ms；真连实测 354~362ms。留足余量：50ms 已经是被拖慢的信号。
    ok(one < 50, "单次探测空端口 < 50ms", "实测 %.1fms（真连会烧满 ~350ms）" % one)

    t0 = time.perf_counter()
    for _ in range(20):
        launcher.port_open(dead_port)
    many = ms(t0)
    ok(many < 100, "连续 20 次探测空端口 < 100ms", "合计 %.1fms" % many)

out()
out("=== 4. 结构：kill_zombies 先过窗口门，再做全进程扫描 ===")
src = None
try:
    with open(os.path.join(BASE, "launcher.py"), encoding="utf-8") as f:
        src = f.read()
except OSError as e:
    out("  读不到 launcher.py:", e)

if src:
    # 只看 kill_zombies 的函数体，别被文件里别处的 _enum_title_windows 误导
    start = src.find("def kill_zombies(")
    body = src[start:] if start >= 0 else ""
    tail = body.find("\ndef ", 10)
    if tail > 0:
        body = body[:tail]
    def first_code_pos(text, needle):
        """按行找 needle 的位置，跳过注释行 —— 注释里提到 process_iter 不算数
        （第一版断言就是被自己的注释坑了）。"""
        off = 0
        for line in text.splitlines(True):
            if needle in line and not line.lstrip().startswith("#"):
                return off + line.find(needle)
            off += len(line)
        return -1

    gate = first_code_pos(body, "if not _enum_title_windows(TITLE)")
    scan = first_code_pos(body, "psutil.process_iter(")
    out("  kill_zombies 里的窗口门位置:", gate, " 全进程扫描位置:", scan)
    ok(gate > 0, "有『一个同名窗口都没有就直接返回』的快速门")
    ok(gate > 0 and (scan < 0 or gate < scan), "快速门在 psutil 全进程扫描之前",
       "顺序反了就白扫 235 个进程（~330ms）")

    # 端口探测不许再退回 socket.create_connection（除了兜底分支）
    start = src.find("def port_open(")
    body = src[start:] if start >= 0 else ""
    tail = body.find("\ndef ", 10)
    if tail > 0:
        body = body[:tail]
    ok("create_connection" in body, "保留了真连兜底", "监听表拿不到时还得能判断")
    ok("_LISTEN_CACHE" in src, "有监听表的短缓存实现")

    # ── find_dir()：onedir 比 onefile 深一层，候选表必须跟着认 ──────────
    # onedir 产物是 <源码>\dist\米家\米家.exe（exe 的上两级才是源码目录），
    # 而 onefile 是 <源码>\dist\米家.exe（上一级就是）。少了 up2 这层候选，
    # 打包版会弹"找不到程序目录（缺 app.py）"—— 用户会以为文件被删了。
    import tempfile
    import shutil

    tmp = tempfile.mkdtemp(prefix="mihome_finddir_")
    real_app_dir, real_file = launcher.APP_DIR, launcher.__file__
    real_frozen, real_exe = getattr(sys, "frozen", False), sys.executable
    try:
        root = os.path.join(tmp, "源码树", "mihome-pc")     # 假装的项目根
        os.makedirs(root)
        with open(os.path.join(root, "app.py"), "w", encoding="utf-8") as f:
            f.write("# fake\n")

        # ① onedir：<root>\dist\米家\米家.exe（_internal 里放 launcher.py）
        onedir_exe = os.path.join(root, "dist", "米家", "米家.exe")
        os.makedirs(os.path.dirname(onedir_exe))
        sys.frozen = True
        sys.executable = onedir_exe
        launcher.APP_DIR = os.path.dirname(onedir_exe)
        launcher.__file__ = os.path.join(os.path.dirname(onedir_exe), "_internal", "launcher.py")
        got = launcher.find_dir()
        ok(got == root, "find_dir 认得 onedir 布局（深一层）",
           "实得 %r" % (got,))

        # ② onefile：<root>\dist\米家.exe 不能被这条改动改坏
        onefile_exe = os.path.join(root, "dist", "米家.exe")
        sys.executable = onefile_exe
        launcher.APP_DIR = os.path.dirname(onefile_exe)
        got = launcher.find_dir()
        ok(got == root, "find_dir 仍认得 onefile 布局", "实得 %r" % (got,))

        # ③ 已知限制：bundle 搬出源码树（桌面/Program Files）找不到 app.py。
        #    这条断言是**故意钉住的现状**：exe 里只有 launcher，后端和 static\
        #    都在源码目录。哪天把后端也打进包了，这条会变红，届时改成断言能命中。
        away = os.path.join(tmp, "桌面", "米家")
        os.makedirs(away)
        away_exe = os.path.join(away, "米家.exe")
        sys.executable = away_exe
        launcher.APP_DIR = away
        launcher.__file__ = os.path.join(away, "_internal", "launcher.py")
        got = launcher.find_dir()
        ok(got is None, "bundle 搬出源码树时返回 None（现状，不是回归）",
           "实得 %r" % (got,))
    finally:
        sys.frozen = real_frozen
        sys.executable = real_exe
        launcher.APP_DIR = real_app_dir
        launcher.__file__ = real_file
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 日志必须收拢到源码目录（否则 onedir 升级会连崩溃现场一起覆盖）──
    ok("logx.redirect(" in src and 'os.path.join(base, "logs")' in src,
       "main() 会把日志重定向到源码目录",
       "打包版 launcher 的 BASE 是 exe 目录，后端是源码目录，不统一就有两份 mihome.log")
    try:
        with open(os.path.join(BASE, "logx.py"), encoding="utf-8") as f:
            logx_src = f.read()
    except OSError:
        logx_src = ""
    ok("_drop_empty" in logx_src,
       "redirect 之后会清掉旧路径的空日志壳",
       "否则 dist\\米家\\logs 里会永远躺着一个 0 字节的 mihome.log")

out()
if FAIL:
    out("❌ 启动快路径退化：%d 条断言没过" % len(FAIL))
    for f in FAIL:
        out("   ·", f)
    sys.exit(1)
out("✅ 启动快路径完好")
sys.exit(0)
