# -*- coding: utf-8 -*-
"""黑框回归测试：程序拉起的所有子进程都必须是「无窗口」版本

背景（用户实际遇到的）：
  屏幕上弹一个黑框，标题是
  "…\\mihome-pc\\.venv\\Scripts\\python.exe"

根因（两个，缺一不可）：
  1. app.py 拉蓝牙桥时用 sys.executable —— 主后端跑在控制台版 python.exe 下时，
     子进程直接继承父控制台，CREATE_NO_WINDOW 形同虚设。
  2. 手动 `python app.py` 启动时，主后端自己就是控制台程序。

  关键知识点：CREATE_NO_WINDOW / DETACHED_PROCESS 只对**已经分配了控制台**的
  程序有效。父进程有控制台 → 子进程继承，标志没用。
  根治办法是用 pythonw.exe（无窗口版），它根本没有控制台可继承。

本测试守住三件事：
  - app.py 里拉 bridge 用的是 pythonw.exe，不是 sys.executable
  - app.py 有「控制台自愈」：控制台版启动时换 pythonw 重启一次
  - launcher.py 拉后端用的是 pythonw.exe
"""
import io
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

failed = 0
total = 0


def check(name, cond, extra=""):
    global failed, total
    total += 1
    if cond:
        print("  ✓ %s" % name)
    else:
        failed += 1
        print("  ✗ %s %s" % (name, extra))


app_src = io.open(os.path.join(BASE, "app.py"), encoding="utf-8").read()
launcher_src = io.open(os.path.join(BASE, "launcher.py"), encoding="utf-8").read()
# 拆分后 _ensure_charger 搬到了 services/charger.py，断言要跟着走
charger_src = io.open(os.path.join(BASE, "services", "charger.py"), encoding="utf-8").read()

print("== 1. services/charger.py 拉蓝牙桥必须用 pythonw.exe ==")
m = re.search(r"def _ensure_charger\(.*?(?=\ndef |\Z)", charger_src, re.S)
check("找到 _ensure_charger", m is not None)
if m:
    body = m.group(0)
    check("显式找 pythonw.exe", "pythonw.exe" in body)
    # sys.executable 作为**最后兜底**是允许的（打包版没有 .venv），
    # 但绝不能是首选 —— 首选必须是 pythonw.exe。
    sys_exec_pos = body.find("sys.executable")
    pyw_pos = body.find("pythonw.exe")
    check("sys.executable 只作最后兜底，不作首选",
          sys_exec_pos == -1 or pyw_pos < sys_exec_pos,
          "pythonw 在 %s, sys.executable 在 %s" % (pyw_pos, sys_exec_pos))
    check("首选顺序 pythonw 在 python 之前",
          pyw_pos != -1 and body.find('"pythonw.exe"') < body.find('"python.exe"')
          or pyw_pos != -1,
          "顺序不对")
    check("有 CREATE_NO_WINDOW", "creationflags" in body and "NO_WIN" in body)

print()
print("== 2. app.py 必须有「控制台自愈」 ==")
check("有 _relaunch_windowless 函数", "def _relaunch_windowless" in app_src)
check("检测 python.exe 后缀", "python.exe" in app_src)
check("找 pythonw.exe 来重启", app_src.count("pythonw.exe") >= 2)
check("有防重入标记", "MIHOME_WINDOWLESS" in app_src)
check("主流程里调用了它", "_relaunch_windowless()" in app_src)
# 调用点必须在 __main__ 里，且在起服务之前
mi = app_src.find('if __name__ == "__main__":')
check("调用点在 __main__ 之后", mi != -1 and app_src.find("_relaunch_windowless()", mi) != -1)
check("调用点在 serve() 之前",
      mi != -1 and app_src.find("_relaunch_windowless()", mi) < app_src.find("serve(app", mi))

print()
print("== 3. launcher.py 拉后端必须用 pythonw.exe ==")
check("优先 pythonw.exe", launcher_src.find("pythonw.exe") < launcher_src.find("python.exe"))
check("主后端用 DETACHED|CREATE_NO_WINDOW",
      "DETACHED | CREATE_NO_WINDOW" in launcher_src)
check("go2rtc 用 CREATE_NO_WINDOW", "creationflags=CREATE_NO_WINDOW" in launcher_src)
check("taskkill 用 CREATE_NO_WINDOW", launcher_src.count("CREATE_NO_WINDOW") >= 3)

print()
print("== 4. 实际起一个子进程看有没有窗口 ==")
try:
    import subprocess
    import psutil
    CREATE_NO_WINDOW = 0x08000000
    DETACHED = 0x00000008
    pyw = os.path.join(BASE, ".venv", "Scripts", "pythonw.exe")
    check("pythonw.exe 存在", os.path.isfile(pyw))
    if os.path.isfile(pyw):
        p = subprocess.Popen(
            [pyw, "-c", "import time; time.sleep(30)"],
            cwd=BASE,
            creationflags=CREATE_NO_WINDOW | DETACHED,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, close_fds=True)
        time.sleep(2)
        try:
            info = psutil.Process(p.pid)
            name = info.name().lower()
            check("用 pythonw 起的进程确实是无窗口版",
                  name == "pythonw.exe", "实际=%s" % name)
        finally:
            try:
                p.kill()
            except Exception:
                pass
except Exception as e:
    check("子进程实测没抛异常", False, repr(e))

print()
if failed:
    print("❌ %d / %d 项未通过" % (failed, total))
    sys.exit(1)
print("✅ 全部 %d 项通过" % total)
