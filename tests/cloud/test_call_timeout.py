# -*- coding: utf-8 -*-
"""call() 超时保护 + 原子写 的回归测试。

对应两处工程加固：
  1. call() 用 asyncio.wait_for 包装，超时后协程被真正 cancel
     （只等不杀会累积"僵尸协程"，拖垮事件循环）
  2. save_spec_cache() 改成 tmp + os.replace 原子写
     （进程被杀不会留半截 JSON）
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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


print("=" * 60)
print("call() 超时保护测试")
print("=" * 60)

import app as A

# --- 1. 正常调用原样返回 ---
async def _ok():
    await asyncio.sleep(0.01)
    return 42


try:
    v = A.call(_ok(), timeout=5)
    check("正常调用返回值不变", v == 42, "got=%r" % (v,))
except Exception as e:
    check("正常调用返回值不变", False, "raised %r" % (e,))

# --- 2. 超时抛 TimeoutError ---
async def _slow():
    await asyncio.sleep(30)
    return "never"


t0 = time.time()
raised = None
try:
    A.call(_slow(), timeout=1)
except BaseException as e:
    raised = e
elapsed = time.time() - t0

check("超时抛出 TimeoutError", isinstance(raised, TimeoutError),
      "raised=%r type=%s" % (raised, type(raised).__name__))
check("超时在 timeout 秒后返回（不是 30 秒后）", elapsed < 6,
      "elapsed=%.2fs" % elapsed)

# --- 3. 关键：超时后协程真的被 cancel，不留僵尸 ---
state = {"cancelled": False, "finished": False}


async def _zombie():
    try:
        await asyncio.sleep(30)
        state["finished"] = True
    except asyncio.CancelledError:
        state["cancelled"] = True
        raise
    finally:
        # 协程体真正结束（被 cancel 后 finally 跑完）才置位
        state["done"] = True


try:
    A.call(_zombie(), timeout=1)
except BaseException:
    pass

# 给事件循环一点时间执行 cancel 回调
for _ in range(30):
    if state.get("cancelled"):
        break
    time.sleep(0.05)

check("超时后协程被 cancel（无僵尸残留）", state.get("cancelled") is True,
      "state=%r" % (state,))
check("被 cancel 的协程没有跑到完成", state.get("finished") is not True)

# --- 4. 超时后事件循环仍能正常服务新请求 ---
async def _after():
    await asyncio.sleep(0.01)
    return "alive"


try:
    v = A.call(_after(), timeout=5)
    check("超时后事件循环仍可用", v == "alive", "got=%r" % (v,))
except Exception as e:
    check("超时后事件循环仍可用", False, "raised %r" % (e,))

# --- 5. 协程内部异常原样透传 ---
async def _boom():
    raise ValueError("boom-msg")


raised = None
try:
    A.call(_boom(), timeout=5)
except BaseException as e:
    raised = e
check("协程异常原样透传", isinstance(raised, ValueError) and "boom-msg" in str(raised),
      "raised=%r" % (raised,))

print()
print("=" * 60)
print("save_spec_cache 原子写测试")
print("=" * 60)

tmpdir = tempfile.mkdtemp(prefix="mihome_atomic_")
orig_spec = A.SPEC_CACHE
try:
    A.SPEC_CACHE = os.path.join(tmpdir, "spec_cache.json")

    # 5.1 正常写入能被读回来
    A.save_spec_cache({"model-a": {"services": [{"iid": 1}]}})
    check("写入后内容正确",
          A.load_spec_cache() == {"model-a": {"services": [{"iid": 1}]}},
          "got=%r" % (A.load_spec_cache(),))
    check("目标文件存在", os.path.isfile(A.SPEC_CACHE))
    check("不残留 .tmp 文件", not os.path.isfile(A.SPEC_CACHE + ".tmp"),
          "leftover tmp")

    # 5.2 写入过程被"杀"（模拟进程中途死掉）→ 旧文件保持完整
    A.save_spec_cache({"old": 1})
    old_content = open(A.SPEC_CACHE, encoding="utf-8").read()

    real_open = open

    class Killer(Exception):
        pass

    def boom_open(*a, **k):
        # 只在写 .tmp 的时候炸，模拟写到一半进程被杀
        if a and isinstance(a[0], str) and a[0].endswith(".tmp"):
            raise Killer("process killed mid-write")
        return real_open(*a, **k)

    import builtins
    builtins.open = boom_open
    try:
        A.save_spec_cache({"new": 2})
    except Exception:
        pass
    finally:
        builtins.open = real_open

    check("写到一半被杀 → 旧文件内容完好",
          open(A.SPEC_CACHE, encoding="utf-8").read() == old_content)
    check("被杀后不会留下半截 .tmp",
          not os.path.isfile(A.SPEC_CACHE + ".tmp"),
          "leftover: %s" % os.listdir(tmpdir))

    # 5.3 残留 .tmp 也不影响下次读取（load 只认目标文件）
    with open(A.SPEC_CACHE + ".tmp", "w", encoding="utf-8") as f:
        f.write("{坏掉的")
    check("残留 .tmp 不影响 load",
          A.load_spec_cache() == {"old": 1},
          "got=%r" % (A.load_spec_cache(),))

    # 5.4 目标文件损坏时 load 返回 {} 而不是崩溃
    with open(A.SPEC_CACHE, "w", encoding="utf-8") as f:
        f.write("{截断的JSON")
    check("目标损坏时 load 返回空字典", A.load_spec_cache() == {})
finally:
    A.SPEC_CACHE = orig_spec
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass

print()
print("=" * 60)
print("结果: %d 通过, %d 失败" % (OK, FAIL))
print("=" * 60)
sys.exit(1 if FAIL else 0)
