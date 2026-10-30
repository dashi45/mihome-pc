# -*- coding: utf-8 -*-
"""单实例命名管道（IPC）的单元测试

要证明的事：
  1. 没有实例在听时，`ipc_call` 明确返回 (None, None) —— 调用方据此走
     "全新启动"，而不是误以为有实例在跑。
  2. 有实例在听时，`show` 带回三种真实结果：shown / starting / dead。
     这三种结果分别对应"窗口已提到前台""我还在建窗口，等我""我废了，你接管"。
  3. 同一个管道名只能有一个监听者（单实例的硬保证，内核级，不靠 fd 锁文件）。
  4. **监听进程被强杀后管道自动消失** —— 没有 stale lock。这是选命名管道
     而不是 QLockFile 的核心原因：锁文件要靠判 pid 才能识别残留，管道不用。

隔离：管道名按程序目录 hash 得出，本测试全程用一个临时目录当 base，
算出来的名字和生产程序的管道完全不同。末尾有守卫断言。

跑法：.venv/Scripts/python.exe -u tests/ipc_test.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

import launcher  # noqa: E402

PASS = FAIL = 0


def ok(cond, what, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ %s" % what, flush=True)
    else:
        FAIL += 1
        print("  ❌ %s  %s" % (what, extra), flush=True)


def out(*a):
    print(*a, flush=True)


SANDBOX = tempfile.mkdtemp(prefix="mihome-ipc-")
ADDR = launcher._ipc_addr(SANDBOX)
PROD_ADDR = launcher._ipc_addr(BASE)

out("=== 0. 隔离检查 ===")
ok(ADDR != PROD_ADDR, "测试管道名与生产管道名不同",
   "%r vs %r" % (ADDR, PROD_ADDR))
ok(ADDR.startswith("\\\\.\\pipe\\mihome-pc-"), "管道名前缀正确", ADDR)
out("   测试管道: %s" % ADDR)

# ---- 用可替换的钩子模拟"窗口在不在""后端通不通" ----
STATE = {"hwnd": None, "backend": False}
launcher._our_dirs = lambda base: set()
launcher._front_window = lambda dirs: STATE["hwnd"]
launcher.port_open = lambda p, host="127.0.0.1", timeout=0.35: STATE["backend"]


out("\n=== 1. 没有实例在听 ===")
reply, err = launcher.ipc_call(SANDBOX, "show", timeout=1.0)
ok(reply is None and err is None, "返回 (None, None)，无错误",
   "reply=%r err=%r" % (reply, err))

out("\n=== 2. 起一个监听者（模拟已在运行的实例）===")
up = launcher.ipc_serve(SANDBOX)
ok(up is True, "ipc_serve 成功")

reply, err = launcher.ipc_call(SANDBOX, "ping")
ok(reply and reply.get("state") == "alive", "ping → alive", repr(reply))
ok(reply and reply.get("backend") is False, "ping 如实报告后端不通")

out("\n=== 3. show 的三种结果 ===")
STATE["hwnd"], STATE["backend"] = None, False
reply, _ = launcher.ipc_call(SANDBOX, "show")
ok(reply and reply.get("ok") is False and reply.get("state") == "dead",
   "没窗口 + 后端不通 → dead（让对方接管重启）", repr(reply))

# 最关键的一条：窗口还挂着、后端已死 = 废实例。
# 这时候绝不能回 shown —— 把死界面叫到前面就是"双击没反应"。
STATE["hwnd"], STATE["backend"] = 0x999, False
reply, _ = launcher.ipc_call(SANDBOX, "show")
ok(reply and reply.get("state") == "dead",
   "**有窗口但后端不通 → 仍然 dead**（废窗口不能算活着）", repr(reply))

STATE["hwnd"], STATE["backend"] = None, True
reply, _ = launcher.ipc_call(SANDBOX, "show")
ok(reply and reply.get("ok") and reply.get("state") == "starting",
   "没窗口 + 后端在跑 → starting（正在建窗口，等我）", repr(reply))

STATE["hwnd"], STATE["backend"] = 0x1234, True
reply, _ = launcher.ipc_call(SANDBOX, "show")
ok(reply and reply.get("ok") and reply.get("state") == "shown",
   "有窗口 → shown（已提到前台）", repr(reply))
ok(reply and reply.get("hwnd") == 0x1234, "shown 带回真实 hwnd")

out("\n=== 4. 未知指令不应崩服务 ===")
reply, _ = launcher.ipc_call(SANDBOX, "rm -rf /")
ok(reply and reply.get("state") == "unknown-cmd", "未知指令 → unknown-cmd", repr(reply))
reply, _ = launcher.ipc_call(SANDBOX, "ping")
ok(reply and reply.get("state") == "alive", "发过未知指令后服务仍然可用")

out("\n=== 5. 同名校验：只能有一个监听者 ===")
dup = None
try:
    from multiprocessing.connection import Listener
    dup = Listener(ADDR, family="AF_PIPE", authkey=launcher._IPC_KEY)
    ok(False, "第二个 Listener 应该建不起来，但它成功了 —— 单实例失效")
except OSError as e:
    ok(True, "第二个监听者被内核拒绝（单实例硬保证）：WinError %s"
       % getattr(e, "winerror", "?"))
finally:
    if dup is not None:
        try:
            dup.close()
        except Exception:
            pass

out("\n=== 6. 强杀监听进程后管道自动消失（无 stale lock）===")
launcher.ipc_stop()
time.sleep(0.8)          # 句柄关闭到内核销毁管道对象有个极短的延迟
still = [t for t in __import__("threading").enumerate()
         if t.name == "ipc" and t.is_alive()]
ok(not still, "ipc_stop 后服务线程已退出（不会在清空的队列上空转）",
   "还有 %d 个存活" % len(still))
reply, err = launcher.ipc_call(SANDBOX, "show", timeout=1.0)
ok(reply is None and err is None, "本进程关闭后立刻没有监听者了",
   "reply=%r err=%r" % (reply, err))

# 用一个子进程真正建管道，再 taskkill /F 强杀，模拟任务管理器结束进程
child_src = os.path.join(SANDBOX, "holder.py")
with open(child_src, "w", encoding="utf-8") as f:
    f.write(
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "import launcher\n"
        "launcher.ipc_serve(%r)\n"
        "print('READY', flush=True)\n"
        "time.sleep(120)\n" % (BASE, SANDBOX))

proc = subprocess.Popen([sys.executable, "-u", child_src],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=0x08000000)
line = proc.stdout.readline().decode("utf-8", "ignore").strip()
ok(line == "READY", "子进程已建好管道", repr(line))
reply, _ = launcher.ipc_call(SANDBOX, "ping", timeout=2.0)
ok(reply and reply.get("state") == "alive", "子进程的管道可连通")

subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
               capture_output=True, creationflags=0x08000000)
try:
    proc.wait(timeout=8)
except Exception:
    pass

time.sleep(0.8)
reply, err = launcher.ipc_call(SANDBOX, "ping", timeout=1.5)
ok(reply is None and err is None,
   "被强杀后管道自动消失，新实例可直接接管（对比 QLockFile 需判 pid 清 stale）",
   "reply=%r err=%r" % (reply, err))

out("\n=== 7. 守卫：生产管道没被碰过 ===")
reply, err = launcher.ipc_call(BASE, "ping", timeout=1.0)
# 生产实例可能正在跑（那就 alive），也可能没跑（None, None）。
# 无论哪种，都不该出现"auth 失败"之类的异常 —— 说明我们的测试没串名。
ok(err is None or not err.startswith("auth"),
   "生产管道未受测试影响", "reply=%r err=%r" % (reply, err))

# ---- 清理 ----
launcher.ipc_stop()
try:
    shutil.rmtree(SANDBOX, ignore_errors=True)
except Exception:
    pass

out("\n" + "=" * 46)
out("通过 %d / 失败 %d" % (PASS, FAIL))
if FAIL:
    out("❌ 有失败项")
    sys.exit(1)
out("✅ 全部通过")
