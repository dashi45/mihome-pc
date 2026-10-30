# -*- coding: utf-8 -*-
"""批量属性读取的解析正确性（纯离屏，不联网、不碰生产文件）。

为什么必须测这个：把"每台设备一个请求"改成"一次请求读多台"，收益是
请求数从 N 降到 1，**风险全压在解析上**——云端回包是多个 did 混在一起的
一个数组，一旦串位，用户看到的就是"点了客厅灯，卧室灯亮"，而且是静默的、
不会报错的。所以这里用假 service 覆盖各种回包形态，并把"批量结果"和
"逐台读的结果"做等价断言。

跑法：.venv\\Scripts\\python.exe tests/batch_read_test.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app  # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s" % msg)
    else:
        FAIL += 1
        print("  [!!] %s" % msg)


class FakeService:
    """按请求的 params 构造回包。

    order="reverse" 用来验证"回包顺序和请求不一致时不能串"；
    bad 里的 key 回 code=-1，用来验证"读失败不写进结果"。
    """

    def __init__(self, values, order="normal", bad=(), drop=()):
        self.values = values
        self.order = order
        self.bad = set(bad)
        self.drop = set(drop)      # 这些 key 干脆不出现在回包里（设备离线）
        self.calls = []            # 每次请求带了多少条 param

    async def miot_request(self, cmd, params):
        assert cmd == "prop/get", "命令写错了: %r" % (cmd,)
        self.calls.append(len(params))
        items = []
        for p in params:
            k = (p["did"], p["siid"], p["piid"])
            if k in self.drop:
                continue
            if k in self.bad:
                items.append({"did": k[0], "siid": k[1], "piid": k[2], "code": -1,
                              "message": "offline"})
            else:
                items.append({"did": k[0], "siid": k[1], "piid": k[2], "code": 0,
                              "value": self.values.get(k)})
        if self.order == "reverse":
            items.reverse()
        return items


def run_batch(plan, fake):
    old = app.S.get("service")
    app.S["service"] = fake
    try:
        return asyncio.run(app._batch_read(plan))
    finally:
        app.S["service"] = old


# ---------------- 1. 同设备多属性 ----------------
print("\n=== 1. 单设备三属性 ===")
plan = [("devA", [(2, 1), (2, 2), (3, 1)])]
vals = {("devA", 2, 1): True, ("devA", 2, 2): 27, ("devA", 3, 1): 2}
fake = FakeService(vals)
got = run_batch(plan, fake)
ok(got["devA"][(2, 1)] is True, "开关值正确")
ok(got["devA"][(2, 2)] == 27, "温度值正确")
ok(got["devA"][(3, 1)] == 2, "模式值正确")
ok(fake.calls == [3], "一次请求读完 3 个属性（实际 %s）" % fake.calls)

# ---------------- 2. 多设备混在一批 ----------------
print("\n=== 2. 两台设备混批，值不能串 ===")
plan = [("A", [(2, 1)]), ("B", [(2, 1)]), ("C", [(2, 1)])]
vals = {("A", 2, 1): True, ("B", 2, 1): False, ("C", 2, 1): True}
got = run_batch(plan, FakeService(vals))
ok(got["A"][(2, 1)] is True and got["B"][(2, 1)] is False
   and got["C"][(2, 1)] is True, "同 siid/piid 不同 did 各归各位")

# ---------------- 3. 回包乱序 ----------------
print("\n=== 3. 回包顺序被打乱 ===")
plan = [("A", [(2, 1), (2, 2)]), ("B", [(2, 1), (2, 2)])]
vals = {("A", 2, 1): 1, ("A", 2, 2): 2, ("B", 2, 1): 3, ("B", 2, 2): 4}
got = run_batch(plan, FakeService(vals, order="reverse"))
ok(got["A"] == {(2, 1): 1, (2, 2): 2}, "A 设备解析正确 %s" % (got["A"],))
ok(got["B"] == {(2, 1): 3, (2, 2): 4}, "B 设备解析正确 %s" % (got["B"],))

# ---------------- 4. 读失败不写进结果 ----------------
print("\n=== 4. code != 0 的条目被丢弃 ===")
plan = [("A", [(2, 1), (2, 2)]), ("B", [(2, 1)])]
vals = {("A", 2, 1): True, ("A", 2, 2): 30, ("B", 2, 1): True}
got = run_batch(plan, FakeService(vals, bad=[("B", 2, 1)]))
ok(got["A"][(2, 1)] is True, "正常设备仍在")
ok("B" not in got or (2, 1) not in got["B"], "读失败不写入（不记成 None）")

# ---------------- 5. 设备整体缺席（离线） ----------------
print("\n=== 5. 设备离线时回包完全没有它 ===")
plan = [("A", [(2, 1)]), ("OFF", [(2, 1)])]
vals = {("A", 2, 1): True, ("OFF", 2, 1): True}
got = run_batch(plan, FakeService(vals, drop=[("OFF", 2, 1)]))
ok(got.get("A", {}).get((2, 1)) is True, "在线设备正常")
ok(not got.get("OFF"), "离线设备不会被塞进假值")

# ---------------- 6. 超过单批上限要分批 ----------------
print("\n=== 6. 超过 _BATCH_DEVS 自动分批 ===")
limit = app._BATCH_DEVS
plan = [("D%02d" % i, [(2, 1)]) for i in range(limit * 2 + 3)]
vals = {("D%02d" % i, 2, 1): bool(i % 2) for i in range(limit * 2 + 3)}
fake = FakeService(vals)
got = run_batch(plan, fake)
expect_batches = -(-(limit * 2 + 3) // limit)
ok(len(fake.calls) == expect_batches,
   "%d 台设备分成 %d 批（实际 %s）" % (limit * 2 + 3, expect_batches, fake.calls))
ok(len(got) == limit * 2 + 3, "每台都回来了（%d 台）" % len(got))

# ---------------- 7. 与逐台读结果等价 ----------------
print("\n=== 7. 批量结果 == 逐台读结果 ===")
devs = ["E%d" % i for i in range(9)]
plan = [(d, [(2, 1), (2, 2), (3, 1)]) for d in devs]
vals = {}
for i, d in enumerate(devs):
    vals[(d, 2, 1)] = bool(i % 2)
    vals[(d, 2, 2)] = 20 + i
    vals[(d, 3, 1)] = i % 4

batch = run_batch(plan, FakeService(vals))
one_by_one = {}
for d, iids in plan:
    one_by_one.update(run_batch([(d, iids)], FakeService(vals)))
ok(batch == one_by_one, "9 台 × 3 属性，两条路径结果完全一致")

# ---------------- 8. 空计划 ----------------
print("\n=== 8. 空计划不发请求 ===")
fake = FakeService({})
got = run_batch([], fake)
ok(got == {} and fake.calls == [], "没有可读设备时不打云端")

print("\n" + "=" * 52)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
