# -*- coding: utf-8 -*-
"""一条命令跑完全部**非界面**测试。

## 为什么不是 pytest

tests/ 下的测试是**脚本式**的：顶层直接写判断、打印 ✓/✗、末尾 sys.exit(1)。
这种写法用 pytest 跑会在"收集阶段"就执行整个文件 —— e2e 那几个会当场开窗口、
抢托盘、往真实 Go2RTC 拉流，等于收个集就把你机器点着了。

所以在"把 40 个脚本改写成函数式测试"之前，先给一个行为正确的入口：
每个文件独立进程执行、超时兜底、结果汇总。想上 pytest 随时可以（conftest.py
里已经有公共 fixture），但那是一次单独的迁移，不该混在一次重构里做。

## 用法

    python tests/run_all.py                  # 默认：跳过 e2e / ui / tools
    python tests/run_all.py --all            # 连界面 e2e 一起（会开窗口）
    python tests/run_all.py --only devices   # 只跑某几个目录
    python tests/run_all.py --list           # 只看会跑哪些
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 默认跳过的目录（会开窗口 / 需要真人看屏幕 / 需要摄像头在线）
GUI_DIRS = {"e2e", "ui", "tray", "perf"}
TOOL_DIRS = {"tools"}          # 探针脚本，不是测试
TIMEOUT = 180


def discover(include_all, only):
    picks = []
    for sub in sorted(os.listdir(HERE)):
        d = os.path.join(HERE, sub)
        if not os.path.isdir(d) or sub == "__pycache__":
            continue
        if only and sub not in only:
            continue
        if sub in TOOL_DIRS:
            continue
        if sub in GUI_DIRS and not include_all:
            continue
        for name in sorted(os.listdir(d)):
            if name.startswith("test_") and name.endswith(".py"):
                picks.append((sub, os.path.join(d, name)))
    return picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="包含界面 e2e")
    ap.add_argument("--only", nargs="*", default=None, help="只跑这些目录")
    ap.add_argument("--list", action="store_true", help="只列出将执行的测试")
    args = ap.parse_args()

    picks = discover(args.all, args.only)
    if args.list:
        for sub, path in picks:
            print("%-10s %s" % (sub, os.path.basename(path)))
        return 0

    py = sys.executable
    failed, ok = [], []
    t_all = time.time()
    for sub, path in picks:
        rel = os.path.relpath(path, ROOT)
        t0 = time.time()
        try:
            r = subprocess.run([py, "-u", path], cwd=ROOT, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=TIMEOUT)
            rc = r.returncode
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            rc, out = "TIMEOUT", ""
        dt = time.time() - t0
        # 脚本式测试的判定：退出码 + 输出里有没有 ✗
        bad_line = any(("✗" in ln) or ("❌" in ln) for ln in out.splitlines())
        good = (rc == 0) and not bad_line
        mark = "✓" if good else "✗"
        print("%s %-46s %5.1fs  rc=%s" % (mark, rel, dt, rc))
        if good:
            ok.append(rel)
        else:
            failed.append((rel, rc, out))

    print("\n" + "=" * 60)
    print("通过 %d / 失败 %d / 共 %d，用时 %.1fs" %
          (len(ok), len(failed), len(picks), time.time() - t_all))
    for rel, rc, out in failed:
        print("\n--- %s (rc=%s) ---" % (rel, rc))
        tail = [ln for ln in out.splitlines() if ln.strip()][-8:]
        for ln in tail:
            print("   " + ln)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
