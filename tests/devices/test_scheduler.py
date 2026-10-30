# -*- coding: utf-8 -*-
"""定时开关的判定逻辑测试。

重点验证参考项目那条最重要的设计决策：
    **只认正点那一分钟，错过不补执行。**

这条为什么重要：程序晚上关了、电脑睡了一觉，早上起来客厅灯突然自己亮了，
用户会以为闹鬼或者家里进了人。"错过就只报个醒"比"补执行"体贴得多。
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

import scheduler as SCH      # noqa: E402

failed = 0


def check(name, cond, detail=""):
    global failed
    if cond:
        print("  ✓ " + name)
    else:
        print("  ✗ " + name + ("  → " + str(detail) if detail else ""))
        failed += 1


# 所有"今天/明天"都相对**当前真实日期**算，不写死。
#
# 为什么必须这样：以前 mark_failed 内部记录 done_today / last_time 用的是
# time.time() 真实时间，而 tick() 接受的是构造出来的时间戳。只要测试里
# 钉死某个 HH:MM，那个断言就只在"真实时钟恰好落在那几分钟"时通过 ——
# 于是这个文件天天在别人机器上假失败（已经因为这个假故障排查过两次）。
#
# 现在 mark_failed / mark_fired 都接受 now=，测试把"执行失败的那一刻"也
# 传进去，判定和记账用同一个假时钟，跟真实时刻彻底解耦。新增用例务必
# 走这条路，不要再依赖"跑测试时正好几点"。
TODAY = time.strftime("%Y-%m-%d")
TOMORROW = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))


def ts(hh, mm, day=None):
    return time.mktime(time.strptime((day or TODAY) + " %02d:%02d:00" % (hh, mm),
                                      "%Y-%m-%d %H:%M:%S"))


# 用独立文件，不碰真实规则
SCH.RULES_PATH = os.path.join(BASE, "_test_schedules.json")
if os.path.exists(SCH.RULES_PATH):
    os.unlink(SCH.RULES_PATH)

print("1. 正点那一分钟才触发")
SCH.save([{"id": "r1", "time": "07:00", "enabled": True, "last_fired": ""}])
due, missed = SCH.tick(ts(7, 0))
check("07:00 正好命中 → 到期", len(due) == 1 and due[0]["id"] == "r1", due)
due, missed = SCH.tick(ts(7, 1))
check("07:01 不再命中（不是 >= 判断）", len(due) == 0, due)

print("")
print("2. 错过不补执行，只报一次")
SCH.save([{"id": "r2", "time": "07:00", "enabled": True, "last_fired": ""}])
due, missed = SCH.tick(ts(8, 30))
check("08:30 时不执行（已过点）", len(due) == 0, due)
check("但会报『错过』", len(missed) == 1, missed)
due, missed = SCH.tick(ts(8, 31))
check("同一天不会重复报错过", len(missed) == 0, missed)
due, missed = SCH.tick(ts(9, 0))
check("再晚也不会补执行", len(due) == 0, due)

print("")
print("3. 第二天重新变成待执行")
SCH.save([{"id": "r3", "time": "07:00", "enabled": True, "last_fired": ""}])
SCH.mark_fired("r3", ts(7, 0))
due, _ = SCH.tick(ts(7, 0))
check("今天执行过后同一分钟不再重复", len(due) == 0, due)
due, _ = SCH.tick(ts(7, 0, day=TOMORROW))
check("第二天同一时刻又能触发", len(due) == 1, due)

print("")
print("4. 停用的规则不触发")
SCH.save([{"id": "r4", "time": "07:00", "enabled": False, "last_fired": ""}])
due, missed = SCH.tick(ts(7, 0))
check("enabled=false 不触发", len(due) == 0, due)
check("enabled=false 也不报错过", len(missed) == 0, missed)

print("")
print("5. 失败重试：5 分钟窗口内最多 3 次，之后当天不再试")
SCH.save([{"id": "r5", "time": "07:00", "enabled": True, "last_fired": ""}])
SCH.mark_failed("r5", "设备离线", ts(7, 0))
check("第 1 次失败后不写 last_fired（还能重试）",
      SCH.all_rules()[0].get("last_fired") != TODAY,
      SCH.all_rules()[0].get("last_fired"))
# 失败发生在 07:00 这一分钟，所以 07:01 仍在重试窗口内
due, _ = SCH.tick(ts(7, 1))
check("重试窗口内下一分钟还会再试", len(due) == 1, due)
due, _ = SCH.tick(ts(7, 2))
check("还能继续重试", len(due) == 1, due)
SCH.mark_failed("r5", "设备离线", ts(7, 0))
SCH.mark_failed("r5", "设备离线", ts(7, 0))
check("试够 3 次后标记 done_today",
      SCH.all_rules()[0].get("done_today") == TODAY,
      SCH.all_rules()[0].get("done_today"))
due, _ = SCH.tick(ts(7, 5))
check("试够 3 次后不再触发", len(due) == 0, due)

print("")
print("5b. 重试窗口之外就不再重试（超过 5 分钟）")
# 规则时间用"今天 11:00"、tick 用"今天 12:00"—— 都用固定构造，
# 不依赖跑测试的**真实时刻**。
#
# 之前这里写的是 time="07:00" + tick(真实时间 + 3600)，
# 晚上 23 点多跑的时候 +1 小时就跨到次日 00:20，
# "07:00" 反而成了"未来"，断言必然翻车 —— 是测试自身的时间依赖缺陷，
# 不是产品逻辑错。
noon = ts(12, 0)
SCH.save([{"id": "r5b", "time": "11:00", "enabled": True, "last_fired": ""}])
# 模拟"1 小时前失败过一次"：tries=1、last_time 在重试窗口之外
SCH.mark_failed("r5b", "设备离线")
r5b = SCH.all_rules()[0]
r5b["tries_today"] = 1
r5b["last_time"] = noon - 3600
SCH.save([r5b])
due, missed = SCH.tick(noon)
check("超出重试窗口就不再执行", len(due) == 0, due)
check("超出窗口后报『错过』", len(missed) == 1, missed)

print("")
print("6. 持久化：重启后规则还在，且不会重复执行")
SCH.save([{"id": "r6", "time": "07:00", "enabled": True, "last_fired": ""}])
SCH.mark_fired("r6", ts(7, 0))
SCH._rules = []          # 模拟进程重启：内存清空
SCH.load()
check("重启后规则还在", len(SCH.all_rules()) == 1, SCH.all_rules())
check("重启后 last_fired 也在（不会重复执行）",
      SCH.all_rules()[0].get("last_fired") == TODAY,
      SCH.all_rules()[0].get("last_fired"))
due, _ = SCH.tick(ts(7, 0))
check("重启后同一分钟不会重复触发", len(due) == 0, due)

print("")
print("7. 增删改")
SCH.save([])
r = SCH.add({"time": "22:30", "did": "d1", "value": True, "name": "开客厅灯"})
check("新增返回带 id 的规则", bool(r.get("id")), r)
check("新增默认 enabled", r.get("enabled") is True)
rid = r["id"]
SCH.update(rid, {"time": "23:00", "enabled": False})
check("改时间生效", SCH.all_rules()[0]["time"] == "23:00")
check("停用生效", SCH.all_rules()[0]["enabled"] is False)
check("删除返回 True", SCH.remove(rid) is True)
check("删除后为空", len(SCH.all_rules()) == 0)
check("删除不存在的返回 False", SCH.remove("nope") is False)

print("")
print("8. 下次触发预览")
SCH.save([{"id": "a", "time": "07:00", "enabled": True},
          {"id": "b", "time": "22:30", "enabled": True},
          {"id": "c", "time": "23:00", "enabled": False}])
nxt = SCH.next_runs()
check("按时间排序返回前两条", [x["time"] for x in nxt] == ["07:00", "22:30"], nxt)
check("停用的不出现", all(x["rule"]["id"] != "c" for x in nxt))

print("")
print("9. 规则文件损坏时不能崩")
with open(SCH.RULES_PATH, "w", encoding="utf-8") as f:
    f.write("{坏掉的 JSON")
SCH._rules = []
try:
    got = SCH.load()
    check("坏文件当空列表，不抛异常", got == [], got)
except Exception as e:
    check("坏文件当空列表，不抛异常", False, e)

# 清理
try:
    os.unlink(SCH.RULES_PATH)
except OSError:
    pass

print("")
if failed:
    print("❌ %d 项未通过" % failed)
    sys.exit(1)
print("✅ 定时逻辑全部通过")
