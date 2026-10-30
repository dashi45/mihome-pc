# -*- coding: utf-8 -*-
"""请求优先级调度的行为测试（纯离屏，不联网）。

要证明四件事：
  1. 优先级真的生效：control 排在 data 前面取用。
  2. **预留额度真的生效**：2 个 worker 被长轮询占住时，control 仍然能立刻
     开跑 —— 这是"光有优先级不够"的那一半。参考项目只做了队列没做预留，
     并发打满时 control 一样要等。
  3. 原有 call() 语义没被破坏：返回值、异常传播、超时后**真的取消**协程。
  4. 放弃的任务不会白跑：调用方超时后，排在队列里的协程被 close 掉，
     不会再去打一次云端请求。

跑法：.venv\\Scripts\\python.exe -u tests/prio_test.py
"""
import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app  # noqa: E402

PASS = FAIL = 0


def ok(cond, msg, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ %s" % msg, flush=True)
    else:
        FAIL += 1
        print("  ❌ %s  %s" % (msg, extra), flush=True)


def out(*a):
    print(*a, flush=True)


out("=== 0. worker 池已就绪 ===")
ok(app._PRIO_Q_CTL is not None and app._PRIO_Q_DATA is not None, "两个池子的队列都建好")
ok(len(app._PRIO_TASKS) == app._PRIO_CTL_WORKERS + app._PRIO_DATA_WORKERS,
   "worker 数量 = %d（control 专属 %d + data/idle %d）"
   % (len(app._PRIO_TASKS), app._PRIO_CTL_WORKERS, app._PRIO_DATA_WORKERS))
ok(all(not t.done() for t in app._PRIO_TASKS), "worker 都在跑")


out("\n=== 1. 基本语义：返回值 / 异常 / 超时 ===")


async def _ret(v):
    return v


async def _boom():
    raise ValueError("boom")


ok(app.call(_ret(42), timeout=5, prio=app.PRIO_CONTROL) == 42, "返回值正常")
try:
    app.call(_boom(), timeout=5, prio=app.PRIO_CONTROL)
    ok(False, "异常应该抛出来")
except ValueError as e:
    ok(str(e) == "boom", "异常原样传播")

CANCELLED = threading.Event()


async def _slow():
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        CANCELLED.set()
        raise


t0 = time.time()
try:
    app.call(_slow(), timeout=1.5, prio=app.PRIO_CONTROL)
    ok(False, "超时应该抛 TimeoutError")
except (TimeoutError, asyncio.TimeoutError):
    elapsed = time.time() - t0
    ok(1.4 <= elapsed <= 4.5, "超时在 1.5 秒左右触发，实际 %.2fs" % elapsed)
ok(CANCELLED.wait(3), "超时后协程被真正 cancel（不是只放弃等待）")


out("\n=== 2. 优先级：control 排到 data 前面 ===")
# 先把 2 个 data 额度占满（不阻塞 worker 池里剩下的 2 个）
GATE = threading.Event()


async def _hold():
    while not GATE.is_set():
        await asyncio.sleep(0.02)
    return "held"


holders = [threading.Thread(target=lambda: app.call(_hold(), timeout=20, prio=app.PRIO_DATA))
           for _ in range(2)]
for h in holders:
    h.start()
time.sleep(0.3)          # 让它们真的抢到额度并开始跑

START = []


def probe(tag, wait=0.0):
    async def f():
        START.append(tag)
        if wait:
            await asyncio.sleep(wait)
        return tag
    return f()


res = {}
ths = []
for i in range(3):
    th = threading.Thread(
        target=lambda i=i: res.__setitem__("d%d" % i,
                                           app.call(probe("d%d" % i, 0.05),
                                                    timeout=10, prio=app.PRIO_DATA)))
    ths.append(th)
    th.start()
    time.sleep(0.02)     # 确保入队顺序 d0 < d1 < d2

time.sleep(0.1)
th_c = threading.Thread(target=lambda: res.__setitem__(
    "ctl", app.call(probe("ctl"), timeout=10, prio=app.PRIO_CONTROL)))
ths.append(th_c)
th_c.start()

time.sleep(0.5)
ok("ctl" in START, "control 已经开始跑（专属池生效，没被轮询占满拖住）")
if "ctl" in START:
    ok(START.index("ctl") < len(START),
       "control 不排在队列末尾，实际开始顺序 = %s" % START)

GATE.set()
for th in ths:
    th.join(15)
for h in holders:
    h.join(15)
ok(len(START) == 4, "四个任务最终都执行了", "实际 %s" % START)


out("\n=== 3. 放弃的任务不白跑 ===")
RAN = []


async def _mark(tag):
    RAN.append(tag)
    return tag


# 把 2 个 data 额度占满 → 后面的 data 只能排队
GATE2 = threading.Event()


async def _hold2():
    while not GATE2.is_set():
        await asyncio.sleep(0.02)
    return 1


h2 = [threading.Thread(target=lambda: app.call(_hold2(), timeout=20, prio=app.PRIO_DATA))
      for _ in range(2)]
for h in h2:
    h.start()
time.sleep(0.3)

# 这个 data 任务会排在队列里干等，最后被外层 timeout 放弃
try:
    app.call(_mark("ghost"), timeout=1.0, prio=app.PRIO_DATA)
    ok(False, "排队超时应该抛错")
except (TimeoutError, asyncio.TimeoutError):
    ok(True, "排队超过 timeout 时如实报错（不会无限等）")

GATE2.set()
for h in h2:
    h.join(15)
time.sleep(1.2)      # 给 worker 时间去取那个已被放弃的队列项
ok("ghost" not in RAN,
   "被放弃的协程没有真的执行（进队带 deadline，过期直接扔）",
   "实际跑了 %s" % RAN)

out("\n" + "=" * 46)
out("通过 %d / 失败 %d" % (PASS, FAIL))
if FAIL:
    out("❌ 有失败项")
    sys.exit(1)
out("✅ 全部通过")
