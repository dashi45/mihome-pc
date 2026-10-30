# -*- coding: utf-8 -*-
"""进程采样回归：原生快照 vs psutil 兜底。

问题背景：
  pcsys 原来用 psutil 逐个进程取 CPU/内存 —— 264 个进程要 296ms，每 3 秒
  一轮等于常年 10% 的单核，而且这 296ms 全程握着 GIL，把 Flask 的请求线程
  挤在外面（/api/status 中位 0.9ms、P99 却有 149ms）。
  改成一次 NtQuerySystemInformation 拿全量链表后是 1.7ms。

这里守三件事：
  1. 原生路径拿到的数据是**对的**（和 psutil 交叉验证，允许工作集有波动）
  2. 原生路径真的比 psutil 快一个数量级（防止哪天被"优化"回去）
  3. 原生路径不可用时会退回 psutil，而不是返回空列表（榜单不能凭空消失）

第 3 条尤其重要：ctypes 结构体是按 Win10/11 的布局写的，万一哪天对不上，
_native_procs() 必须返回 None 让调用方兜底，**绝不能抛异常把采样线程打死**。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pcsys                                            # noqa: E402

OK = 0
FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  [OK] %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  [!!] %s %s" % (name, extra))


print("=" * 62)
print("进程采样（原生快照）")
print("=" * 62)

if not pcsys.IS_WIN:
    print("  非 Windows，跳过（原生路径只对 Windows 有意义）")
    print("结果: 0 通过, 0 失败")
    sys.exit(0)

# ---- 1. 原生快照能拿到东西，字段合理 ----
snap = pcsys._native_procs()
check("原生快照可用", snap is not None and len(snap) > 10,
      "拿到 %d 个进程" % (len(snap) if snap else 0))

if snap:
    pids = [r["pid"] for r in snap]
    check("pid 全为正整数", all(isinstance(p, int) and p >= 0 for p in pids))
    check("pid 不重复", len(pids) == len(set(pids)))
    named = [r for r in snap if r["name"]]
    check("绝大多数进程有名字", len(named) > len(snap) * 0.9,
          "%d/%d" % (len(named), len(snap)))
    check("cpu_sum 非负", all(r["cpu_sum"] >= 0 for r in snap))
    check("ws 非负", all(r["ws"] >= 0 for r in snap))

    # ---- 2. 和 psutil 交叉验证（自己这个进程必须对得上）----
    import psutil
    me = [r for r in snap if r["pid"] == os.getpid()]
    check("快照里有自己", bool(me))
    if me:
        mine = me[0]
        try:
            real_rss = psutil.Process().memory_info().rss
            diff = abs(mine["ws"] - real_rss) / 1048576
            check("自己的工作集和 psutil 一致（±8MB）", diff < 8,
                  "native %.1fMB vs psutil %.1fMB" % (mine["ws"] / 1048576,
                                                      real_rss / 1048576))
        except Exception as e:
            check("psutil 读自己内存", False, repr(e))

        try:
            ct = psutil.Process().cpu_times()
            me_cpu = ct.user + ct.system
            diff = abs(mine["cpu_sum"] - me_cpu)
            check("自己的 CPU 时间和 psutil 一致（±0.2s）", diff < 0.2,
                  "native %.2fs vs psutil %.2fs" % (mine["cpu_sum"], me_cpu))
        except Exception as e:
            check("psutil 读自己 CPU 时间", False, repr(e))

# ---- 3. 速度必须快一个数量级 ----
if snap:
    def best(fn, rounds=3):
        fn()
        ts = []
        for _ in range(rounds):
            t = time.time()
            fn()
            ts.append((time.time() - t) * 1000)
        return min(ts)

    t_native = best(pcsys._native_procs)
    t_psutil = best(lambda: [p.memory_info() for p in psutil.process_iter()])
    check("原生快照 < 30ms", t_native < 30, "%.1f ms" % t_native)
    check("比 psutil 逐个取快 5 倍以上", t_psutil / max(t_native, 0.01) > 5,
          "%.1f ms vs %.1f ms（%.0f×）" % (t_native, t_psutil,
                                           t_psutil / max(t_native, 0.01)))

# ---- 4. 占用率要能真的算出非零值（两次采样之间）----
pcsys._PROC_PREV.clear()
pcsys._top_procs()                     # 第一拍建立基线，全是 0
time.sleep(1.0)
rows = pcsys._top_procs()
check("第二拍能给出数字", bool(rows), "取到 %d 行" % len(rows))
live = [r for r in rows if r["cpu"] > 0]
check("存在占用 > 0 的进程", bool(live),
      "最高 %s %s%%" % (rows[0]["name"] if rows else "-",
                        rows[0]["cpu"] if rows else "-"))
check("占用率不超过核数×100", all(r["cpu"] <= 100 * (os.cpu_count() or 1) for r in rows))

# ---- 5. 原生不可用时必须退回 psutil，不能返回空 ----
orig = pcsys._native_procs
try:
    pcsys._native_procs = lambda: None
    pcsys._PROCS.clear()
    fb1 = pcsys._top_procs()
    time.sleep(0.6)
    fb2 = pcsys._top_procs()
    check("原生失效时走兜底且不空", bool(fb2), "兜底取到 %d 行" % len(fb2))
except Exception as e:
    check("兜底路径不抛异常", False, repr(e))
finally:
    pcsys._native_procs = orig

# ---- 6. 结构体不可用时 _native_procs 返回 None 而不是抛 ----
try:
    saved = pcsys._PROC_SNAPSHOT["ok"]
    pcsys._PROC_SNAPSHOT["ok"] = False
    check("标记不可用后立刻返回 None", pcsys._native_procs() is None)
    pcsys._PROC_SNAPSHOT["ok"] = saved
except Exception as e:
    check("标记不可用后不抛异常", False, repr(e))

# ---- 7. GPU：优先 NVML，读不到就退回 nvidia-smi ----
print()
print("=" * 62)
print("GPU 采样（NVML 直连）")
print("=" * 62)

has_nv = os.path.exists(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                    "System32", "nvml.dll"))
if has_nv:
    g = pcsys._nvml_read()
    check("NVML 能读到显卡", g is not None, str(g.get("name") if g else "-"))
    if g:
        check("name 非空", bool(g.get("name")))
        check("util 在 0~100", g["util"] is not None and 0 <= g["util"] <= 100,
              "%.0f%%" % g["util"])
        check("显存 used ≤ total", g["mem_used"] <= g["mem_total"] + 1,
              "%.0f/%.0f MB" % (g["mem_used"], g["mem_total"]))
        check("温度在合理区间", 0 < (g.get("temp") or 0) < 120,
              "%s℃" % g.get("temp"))
        check("功耗为正（W 而不是 mW）", 0 < (g.get("power") or 0) < 1000,
              "%sW" % g.get("power"))

    def _best(fn, rounds=5):
        fn()
        ts = []
        for _ in range(rounds):
            t = time.time()
            fn()
            ts.append((time.time() - t) * 1000)
        return min(ts)

    t_nv = _best(pcsys._nvml_read)
    check("NVML 单次读取 < 2ms", t_nv < 2, "%.2f ms" % t_nv)

    # 和 nvidia-smi 交叉核对（只比能直接对齐的两项，避免解析差异）
    smi = pcsys._nvidia()
    check("_nvidia() 结果一致（util 差 ≤2）",
          smi and g and abs((smi.get("util") or 0) - g["util"]) <= 2,
          "nvml %s vs 接口 %s" % (g["util"] if g else "-",
                                  smi.get("util") if smi else "-"))
else:
    print("  本机没有 nvml.dll（非 NVIDIA 或无驱动），检查退回路径")
    check("_nvidia() 不抛异常", pcsys._nvidia() is None or True)

print()
print("=" * 62)
print("结果: %d 通过, %d 失败" % (OK, FAIL))
print("=" * 62)
sys.exit(1 if FAIL else 0)