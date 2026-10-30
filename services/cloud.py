# -*- coding: utf-8 -*-
"""异步调用层：事件循环、优先级队列、重试/熔断/健康账本。

miservice 是 async 的，Flask 是同步的。全部云端调用都从 `call()` 进、
在专属事件循环线程里执行，HTTP 请求本身丢到 executor 省出 loop。

两个队列按优先级分（用户操作 > 界面轮询 > 后台活），同优先级按到达顺序，
这样"点开关"永远不会排在"后台预取规格"后面。
"""
import asyncio
import concurrent.futures
import itertools
import threading
import time

import netguard
import atexit
from services.state import S


# ---------- 异步事件循环（miservice 是 async 的，Flask 是同步的）----------

# 请求优先级（参考 MiHome-Windows 的 jobs.py：CONTROL / DATA / IDLE 三级）
#
# 为什么需要：轮询和用户点击原来抢同一个池子、平权排队。弱网下一次
# /api/live 要 3 秒、事件补采要 150 秒，赶上后台在跑，用户点一下开关
# 要等十几秒 —— "按了没反应"比"慢一点"伤人得多。
#
# 光有优先级还不够：worker 一旦被长轮询占住，control 排到队首也只能干等。
# 更糟的是**用信号量拦**——worker 在等额度时自己也卡死了，等于把 worker 变成
# 了排队的人。所以这里用**双池隔离**（比参考项目更进一步，它只有优先级，
# 并发打满时 CONTROL 一样要等）：
#   · control 专属池 3 个 worker：用户操作永远有地方立刻执行
#   · data/idle 池 2 个 worker：内部再按优先级排，轮询让位给后台
#   · 任务进队时带 deadline：排队途中已经超时的直接扔掉，不再打一次云端
#     （弱网下队列会积压，过期的轮询数据本来也没用了）
PRIO_CONTROL = 0     # 用户操作：开关、云台、设备详情、登录
PRIO_DATA = 1        # 界面轮询：/api/live、电脑状态
PRIO_IDLE = 2        # 后台活：spec 预取、红外按键、事件补采

_PRIO_Q_CTL = None              # asyncio 对象必须建在 _loop 线程里
_PRIO_Q_DATA = None
_PRIO_SEQ = itertools.count()   # 同优先级按到达顺序；不能让队列去比较协程对象
_PRIO_TASKS = []
_PRIO_CTL_WORKERS = 3
_PRIO_DATA_WORKERS = 2

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()


async def _prio_bootstrap():
    """建队列 + 起两组 worker。必须在 _loop 线程里执行。"""
    global _PRIO_Q_CTL, _PRIO_Q_DATA
    _PRIO_Q_CTL = asyncio.PriorityQueue()
    _PRIO_Q_DATA = asyncio.PriorityQueue()
    for _ in range(_PRIO_CTL_WORKERS):
        _PRIO_TASKS.append(asyncio.ensure_future(_prio_worker(_PRIO_Q_CTL)))
    for _ in range(_PRIO_DATA_WORKERS):
        _PRIO_TASKS.append(asyncio.ensure_future(_prio_worker(_PRIO_Q_DATA)))


async def _prio_worker(q):
    while True:
        prio, _seq, fut, coro, timeout, deadline = await q.get()
        if fut.done() or time.monotonic() > deadline:
            # 调用方已经放弃了（外层超时），或者排队途中就过期了。
            # 别再白跑一次云端请求 —— 弱网下队列积压时，这条能省掉
            # 一堆"数据早就没人要了还在发"的请求。
            #
            # 这里必须 close 的是**原始** coro（不是外层包的 wait_for）：
            # 只关外层的话，里面的协程没人 await 也没人关，退出时会刷
            # "coroutine was never awaited" 警告，真出问题反而被噪音盖住。
            try:
                coro.close()
            except Exception:
                pass
            continue
        try:
            fut.set_result(await asyncio.wait_for(coro, timeout=timeout))
        except asyncio.CancelledError:
            # worker 自己被取消（进程退出）。把结果转给等待方再走。
            if not fut.done():
                fut.cancel()
            return
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)


async def _prio_submit(coro, prio, timeout, deadline):
    """把任务放进对应的池子，等 worker 交回结果。"""
    fut = _loop.create_future()
    q = _PRIO_Q_CTL if prio == PRIO_CONTROL else _PRIO_Q_DATA
    await q.put((prio, next(_PRIO_SEQ), fut, coro, timeout, deadline))
    try:
        return await fut
    except asyncio.CancelledError:
        if not fut.done():
            fut.cancel()
        raise


asyncio.run_coroutine_threadsafe(_prio_bootstrap(), _loop).result(10)


def _prio_shutdown():
    """退出时把 worker 收掉。

    不收的话解释器退出时 asyncio 会为每个还在 await 的 worker 打一行
    "Task was destroyed but it is pending" —— 无害，但每次启动/退出都在
    日志里刷五条，真出问题时的关键行反而被这些噪音埋掉。
    """
    try:
        for t in _PRIO_TASKS:
            t.cancel()
        _loop.call_soon_threadsafe(_loop.stop)
    except Exception:
        pass


atexit.register(_prio_shutdown)


def call(coro, timeout=60, prio=PRIO_DATA):
    """在后台事件循环里执行协程并等待结果。

    用 asyncio.wait_for 包一层，保证超时后协程被**真正取消**：
    run_coroutine_threadsafe(...).result(timeout=) 超时只是"放弃等待"，
    协程仍然挂在事件循环里继续跑，继续占着 HTTP 连接和云端配额。
    云端长时间不响应时，这些"僵尸协程"会越积越多，
    事件循环被拖垮，后面的调用全部排队卡死——只等不杀等于没保护。

    参考项目 net_guard.py 是给 requests 补默认 timeout（解决"等太久"），
    这里补上"取消"这一半，语义更严格：
      - 超时 → 抛 TimeoutError，调用方走 except 分支
      - 协程被 cancel → 底层 socket 关闭，不再占资源

    第三个参数 prio 决定进哪个池（见 PRIO_* 常量）。默认 DATA —— 新增
    调用点忘了传也不会把用户操作挤到后面（控制类调用要显式标 CONTROL）。

    默认 60 秒：所有调用点都显式传了 timeout（最长 180 秒是登录），
    这个默认值只兜住"新增调用忘了传"的场景，900 秒太长没有意义。
    """
    # 外层比内层多给 6 秒余量（原来是 5 秒，多出来的 1 秒留给排队）：
    # 让 wait_for 自己去 cancel 并抛 TimeoutError，而不是外层先放弃——
    # 外层放弃就又会退化成"僵尸协程"。
    deadline = time.monotonic() + timeout
    cfut = asyncio.run_coroutine_threadsafe(
        _prio_submit(coro, prio, timeout, deadline), _loop)
    try:
        return cfut.result(timeout=timeout + 6)
    except concurrent.futures.TimeoutError:
        # 已经在跑的协程 cancel 不掉（concurrent Future 只能取消未开始的），
        # 但内层 wait_for 会自己超时并取消，兜底逻辑不依赖这一句。
        cfut.cancel()
        raise TimeoutError("云端请求超过 %.0f 秒未返回" % (timeout + 6))


# ---------- 云端调用：重试 / 熔断 / 健康账本 ----------
# call() 管"等太久"和"杀不掉"，这一层管"抖一下就失败"和"挂了还硬撞"。
# 细节见 netguard.py 顶部注释。
_NG = netguard.Health("cloud")
_NG_BRK = netguard.Breaker(threshold=4, cooldown=20.0)

# ---------- 三个"能看见"的指标 ----------
# 前几轮改动都在降故障率，体感为零；而我把摄像头冷启动从 2.5s 改到 12s 时
# 也没有任何东西跳出来拦 —— 因为没数字。这三个进 /api/status、侧栏直接显示：
#   首屏 ready 秒数 / 首次出画面毫秒 / 自愈次数（异常代理指标）
# 以后谁把哪条弄慢了，刷新一下就看得见。
_T0 = time.time()
_METRICS = {"ready_at": 0.0, "cam_first_ms": None, "cam_ok": 0,
            "cam_fail": 0, "heals": 0}


def _mark_ready():
    if not _METRICS["ready_at"] and S.get("status") == "ready":
        _METRICS["ready_at"] = round(time.time() - _T0, 2)


def cloud(factory, timeout=60, prio=PRIO_DATA, attempts=3, what=""):
    """带重试/熔断/健康统计的云端调用。

    factory 必须是**函数**，每次调用新建协程 —— 协程抛异常后不能重跑，
    直接把建好的协程塞进来是这一层唯一容易用错的地方。

    哪些能重试：
      · 读（属性、设备表、历史）—— 随便重试，幂等
      · set_prop —— 可以，写同一个值两次结果一样
      · action（切换/云台/场景）—— **一次都不能重打**，重发"切换"就是切两次
    """
    return netguard.run(
        factory,
        submit=lambda c: call(c, timeout=timeout, prio=prio),
        attempts=attempts, health=_NG, breaker=_NG_BRK, what=what)


def _note_cloud_error(e, where):
    """前台请求失败时顺手判断一句：是不是登录失效了？

    是的话立刻叫一次自愈 —— 不然用户只能看到"设备不刷新"或者一条报错，
    而真正的原因（token 被 miservice 删了）根本看不见。
    """
    kind = netguard.classify(e)
    if kind == netguard.AUTH:
        # 延迟导入：workers 依赖本模块，直接 import 会成环
        from services.workers import _maybe_self_heal
        _maybe_self_heal("%s 认证失败" % where)
    return kind


def _http_stats():
    """连接池的实际开销：发了几次请求、重连几次、出错几次。

    没有这个就只能靠"感觉快了"来判断池化有没有生效 ——
    而"重连数 ≈ 请求数"恰恰说明池化没在工作（每次都新建连接）。
    """
    try:
        sess = getattr(S.get("account"), "_session", None)
        if sess is not None and hasattr(sess, "stats"):
            return sess.stats()
    except Exception:
        pass
    return {"pool": False}
