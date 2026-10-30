# -*- coding: utf-8 -*-
"""云端稳定性层（netguard + token 原子校验 + 批量读取容错）的回归测试。

钉住四件事：
  1. 失败归因 —— 判错方向两边都有害：把网络抖动判成 AUTH，用户被无故要求
     重新登录；把登录失效判成 NET，断网时疯狂重试招限流。
  2. 重试只对幂等/可重试类生效，AUTH 绝不重试。
  3. 熔断：连续失败后快速失败，冷却结束放一个探针。
  4. token 半截时能还原备份；好文件绝不被坏内容覆盖。
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error

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
print("netguard 归因")
print("=" * 60)

import netguard as G

cases = [
    ("超时（内置 TimeoutError）", TimeoutError("timed out"), G.TIMEOUT),
    ("超时（asyncio）", asyncio.TimeoutError(), G.TIMEOUT),
    ("socket 超时", socket.timeout("timed out"), G.TIMEOUT),
    ("连接被重置", ConnectionResetError("reset"), G.NET),
    ("DNS 失败", socket.gaierror("getaddrinfo failed"), G.NET),
    ("URLError", urllib.error.URLError("urlopen error"), G.NET),
    ("登录失败文案", Exception("Error https://api.io.mi.com/app/x: Login failed: bad"), G.AUTH),
    ("认证错误文案", Exception("Auth error on request, relogin..."), G.AUTH),
    ("HTTP 401", urllib.error.HTTPError("u", 401, "unauth", {}, None), G.AUTH),
    ("HTTP 503", urllib.error.HTTPError("u", 503, "unavail", {}, None), G.SERVER),
    ("HTTP 429", urllib.error.HTTPError("u", 429, "slow down", {}, None), G.RATE),
    ("米家 500 文案", Exception("Error https://api.io.mi.com/app/prop/get: 500 server"), G.SERVER),
    ("认不出来", Exception("code=-1 something weird"), G.UNKNOWN),
]
for name, exc, want in cases:
    got = G.classify(exc)
    check("归因：%s → %s" % (name, want), got == want, "got=%s" % got)

check("可重试集合含 net/timeout/server/rate",
      set(G.RETRYABLE) == {G.NET, G.TIMEOUT, G.SERVER, G.RATE})
check("AUTH 不可重试", not G.retryable(G.AUTH))
check("未知不可重试", not G.retryable(G.UNKNOWN))
check("AUTH 不推动熔断", G.AUTH not in G.BREAKING)

print()
print("=" * 60)
print("重试策略")
print("=" * 60)

calls = {"n": 0}


def flaky():
    calls["n"] += 1
    if calls["n"] == 1:
        raise ConnectionResetError("reset")
    return "ok"


r = G.run(flaky, submit=lambda v: v, attempts=3)
check("一次网络抖动后自动重试成功", r == "ok", "r=%r" % (r,))
check("确实只打了两次", calls["n"] == 2, "n=%d" % calls["n"])

calls["n"] = 0


def always_auth():
    calls["n"] += 1
    raise Exception("Login failed: bad password")


raised = None
try:
    G.run(always_auth, submit=lambda v: v, attempts=3)
except Exception as e:
    raised = e
check("AUTH 不重试（只打一次）", calls["n"] == 1, "n=%d" % calls["n"])
check("AUTH 原异常透传", raised is not None and "Login failed" in str(raised))

calls["n"] = 0


def boom_unknown():
    calls["n"] += 1
    raise Exception("code -1 weird")


try:
    G.run(boom_unknown, submit=lambda v: v, attempts=3)
except Exception:
    pass
check("认不出来的错误不重试（不白打云端）", calls["n"] == 1, "n=%d" % calls["n"])

print()
print("=" * 60)
print("熔断")
print("=" * 60)

br = G.Breaker(threshold=2, cooldown=0.4)
check("初始放行", br.allow() is True)
br.failure(G.NET)
check("失败一次仍放行", br.allow() is True)
br.failure(G.NET)
check("到阈值后开闸", br.allow() is False)
check("开闸时给出剩余冷却时间", br.retry_after() > 0)

raised = None
try:
    G.run(lambda: 1, submit=lambda v: v, breaker=br)
except G.BreakerOpen as e:
    raised = e
check("开闸期间快速失败（不再打云端）", isinstance(raised, G.BreakerOpen))

time.sleep(0.45)
check("冷却结束放行探针", br.allow() is True)
br.success()
check("探针成功后恢复", br.allow() is True and br.is_open is False)

br2 = G.Breaker(threshold=1, cooldown=30)
br2.failure(G.AUTH)
check("AUTH 不推动熔断（否则用户没法重新登录）", br2.is_open is False)

print()
print("=" * 60)
print("健康账本")
print("=" * 60)

h = G.Health("t")
check("初始 ok", h.snapshot()["state"] == "ok")
h.fail(G.NET, "x")
check("失败一次 degraded", h.snapshot()["state"] == "degraded")
h.fail(G.NET, "x")
h.fail(G.NET, "x")
check("连续三次 down", h.snapshot()["state"] == "down")
check("按类型计数", h.snapshot()["by_kind"].get(G.NET) == 3)
h.ok()
check("成功后回到 ok", h.snapshot()["state"] == "ok")
check("成功一次连续计数归零", h.snapshot()["consecutive_fail"] == 0)

print()
print("=" * 60)
print("token 原子校验 / 备份")
print("=" * 60)

import app as A
import creds

tmp = tempfile.mkdtemp(prefix="mihome_token_")
orig_tok, orig_bak = A.TOKEN_PATH, A.TOKEN_BAK
A.TOKEN_PATH = os.path.join(tmp, ".mi.token")
A.TOKEN_BAK = os.path.join(tmp, ".mi.token.bak")

GOOD = {"deviceId": "dev1", "userId": "u1", "passToken": "p",
        "xiaomiio": ["ssec", "stoken"], "cUserId": "c"}


def write(path, obj_or_text):
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(obj_or_text, str):
            f.write(obj_or_text)
        else:
            json.dump(obj_or_text, f)


try:
    # 1. 正常备份
    write(A.TOKEN_PATH, GOOD)
    check("有效 token 能备份", A.backup_token() is True)
    check("备份内容与主文件一致",
          creds.load_json(A.TOKEN_BAK) == GOOD)

    # 2. 主文件损坏时不覆盖好副本
    write(A.TOKEN_PATH, '{"deviceId": "dev1", "userId"')
    ok = A.backup_token()
    check("半截 token 拒绝备份", ok is False)
    check("好副本没有被坏内容覆盖",
          creds.load_json(A.TOKEN_BAK) == GOOD)

    # 3. 主文件损坏 → 从副本还原
    restored = A.restore_token_from_backup()
    check("主文件损坏时从副本还原", restored is True)
    check("还原后内容正确",
          creds.load_json(A.TOKEN_PATH) == GOOD)
    check("不残留 .tmp", not os.path.isfile(A.TOKEN_PATH + ".tmp"))

    # 4. 主文件正常时不动它（避免无谓重写把好文件写坏）
    before = open(A.TOKEN_PATH).read()
    check("主文件正常时不还原", A.restore_token_from_backup() is False)
    check("主文件字节未被改动", open(A.TOKEN_PATH).read() == before)

    # 5. 主文件被删（miservice 认证异常时就这么干）→ 还原
    os.remove(A.TOKEN_PATH)
    check("主文件被删时还原", A.restore_token_from_backup() is True)

    # 6. 主副本都坏 → 不假装成功
    write(A.TOKEN_PATH, "{坏")
    write(A.TOKEN_BAK, "{也坏")
    check("无法还原时返回 False（让界面提示重新登录）",
          A.restore_token_from_backup() is False)

    # 7. 非 dict / 缺关键字段的 token 视为无效
    write(A.TOKEN_PATH, {"deviceId": "x"})
    write(A.TOKEN_BAK, {"deviceId": "x"})
    check("缺 userId/xiaomiio 视为无效", A._token_read(A.TOKEN_PATH) is None)
finally:
    A.TOKEN_PATH, A.TOKEN_BAK = orig_tok, orig_bak

print()
print("=" * 60)
print("批量读取容错")
print("=" * 60)


class Svc:
    """指定的一批设备永远失败（重试也救不回来），其余正常返回。"""

    def __init__(self, dead=None):
        self.dead = dead or (lambda did: False)
        self.n = 0

    async def miot_request(self, method, params):
        self.n += 1
        if all(self.dead(p["did"]) for p in params):
            raise ConnectionResetError("reset")
        return [{"did": p["did"], "siid": p["siid"], "piid": p["piid"],
                 "code": 0, "value": 1} for p in params]


def plan_of(n):
    return [("did%02d" % i, [(2, 1)]) for i in range(n)]


orig_service = A.S["service"]
orig_brk = A._NG_BRK
try:
    A._NG_BRK = G.Breaker(threshold=99, cooldown=1)   # 测试里别触发熔断
    # 20 台 = 2 批（_BATCH_DEVS=15）：did00~did14 永远失败，did15~did19 正常
    A.S["service"] = Svc(dead=lambda did: int(did[3:]) < 15)
    got = asyncio.run(A._batch_read(plan_of(20)))
    check("一批彻底失败，另一批照常返回", len(got) == 5, "got=%d 台" % len(got))
    check("失败那批不写入结果", "did00" not in got)
    check("成功那批数据完整", got.get("did15") == {(2, 1): 1})
    check("失败批次被计入统计", A._BATCH_STAT.get("failed", 0) >= 1)

    A.S["service"] = Svc(dead=lambda did: True)
    raised = None
    try:
        asyncio.run(A._batch_read(plan_of(20)))
    except Exception as e:
        raised = e
    check("全部批次都失败时抛错（不能安静返回空）", raised is not None)
finally:
    A.S["service"] = orig_service
    A._NG_BRK = orig_brk
    try:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass

print()
print("=" * 60)
print("结果: %d 通过, %d 失败" % (OK, FAIL))
print("=" * 60)
sys.exit(1 if FAIL else 0)
