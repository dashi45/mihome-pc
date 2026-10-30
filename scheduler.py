# -*- coding: utf-8 -*-
"""定时开关。

借鉴自 MiHome-Windows 的 scheduler.py，但按本项目的形态改造。

**最关键的一条设计决策：只认"正点那一分钟"，错过不补执行。**

参考项目作者的注释写得很直白：
    应用关掉或电脑休眠时错过的定时，醒来后不会补开灯——白天突然亮灯
    比不亮更烦人。错过会单独报出来，让用户知道"今天这条没执行"，
    而不是悄悄跳过。

这条我原样采纳，并且加了更严的一层：
    不是"当前时刻 >= 规则时刻"就算错过，而是**只在正点那一分钟判到期**。
    这样"8:30 启动，7:00 的规则"就明确是错过，不会因为程序中途启动
    而意外触发一批"本该昨晚执行"的规则。

**失败重试是一个例外**，这里刻意放宽：
    设备临时离线导致的失败，值得在几分钟内重试一下——这跟"睡醒后补执行"
    是两回事。区别在于：
      - 补执行 = 程序根本没跑，醒来后突然开灯（惊吓）
      - 重试   = 程序正在跑，刚才那一刻没写成功，离线恢复后马上补（合理）
    所以重试窗口限制在 5 分钟内，且当天最多试 3 次。

判定逻辑与 IO 完全分离（纯函数），这样可以脱离网络做测试。

另外两条从参考项目学来的：
- 同一��天只执行一次由 last_fired 保证（落盘，重启也不会重复）。
- "错过"也要报出来，否则用户永远不知道自己错过了一次。
"""

import json
import os
import threading
import time

# 路径统一由 config 提供（同 evtlog：别自己拼 BASE，否则搬家后读不到旧规则）
import config

RULES_PATH = config.SCHEDULES

# 失败后多久内还愿意重试（秒）
RETRY_WINDOW = 300
# 一天最多试几次
MAX_TRIES = 3

_lock = threading.RLock()
_rules = []            # list[dict]
_reported_miss = set()  # (rule_id, "YYYY-MM-DD")，同一天只报一次错过


# ---------------------------------------------------------------- 持久化
def _read():
    try:
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("id")]


def _atomic_write(rules):
    """原子写：进程被杀不会留半截 JSON（规则丢一半比全丢更麻烦）。"""
    tmp = RULES_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=1)
        os.replace(tmp, RULES_PATH)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def load():
    global _rules
    with _lock:
        _rules = _read()
        return list(_rules)


def all_rules():
    with _lock:
        return list(_rules)


def save(rules):
    global _rules
    with _lock:
        _rules = [r for r in (rules or []) if isinstance(r, dict)]
        _atomic_write(_rules)
        return list(_rules)


def add(rule):
    """新增一条规则。返回落库后的完整规则。"""
    with _lock:
        rule = dict(rule)
        rule["id"] = str(rule.get("id") or ("r%d%d" % (int(time.time()), len(_rules) + 1)))
        rule.setdefault("enabled", True)
        rule.setdefault("last_fired", "")
        rule.setdefault("tries_today", 0)
        rule.setdefault("created", time.time())
        _rules.append(rule)
        _atomic_write(_rules)
        return dict(rule)


def update(rule_id, patch):
    global _rules
    with _lock:
        for r in _rules:
            if str(r.get("id")) == str(rule_id):
                r.update(patch or {})
                _atomic_write(_rules)
                return dict(r)
        return None


def remove(rule_id):
    global _rules
    with _lock:
        before = len(_rules)
        _rules = [r for r in _rules if str(r.get("id")) != str(rule_id)]
        if len(_rules) != before:
            _atomic_write(_rules)
        return before != len(_rules)


# ---------------------------------------------------------------- 判定
def tick(now=None):
    """按当前时刻判定该执行 / 该报错的规则。

    返回 (due, missed)：
      due    —— 现在该执行的规则（含失败后的重试）
      missed —— 今天已错过、且本次首次报出的规则（只报一次，不刷屏）

    只认"当前 HH:MM == 规则时刻"作为首次到期：
      - 6:59 启动 → 7:00 的规则在 7:00 那一分钟触发
      - 8:30 启动 → 7:00 的规则判为错过，只报"错过"，不执行

    例外是失败重试：刚失败过、还在重试窗口内、且今天没试够 3 次的规则，
    会在之后几分钟里继续出现在 due 里。
    """
    ts = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    hhmm = time.strftime("%H:%M", time.localtime(ts))
    due, missed = [], []
    with _lock:
        for rule in _rules:
            if not rule.get("enabled", True):
                continue
            # 今天已经成功执行过，或今天已经试够了，都不再动它
            if rule.get("last_fired") == day or rule.get("done_today") == day:
                continue
            rule_time = str(rule.get("time") or "")
            if not rule_time:
                continue
            tries = int(rule.get("tries_today") or 0)
            last_time = float(rule.get("last_time") or 0)

            if rule_time == hhmm:
                due.append(dict(rule))
            elif rule_time < hhmm:
                # 已经过了点：如果之前失败过且还在重试窗口内，继续重试
                if 0 < tries < MAX_TRIES and (ts - last_time) <= RETRY_WINDOW:
                    due.append(dict(rule))
                else:
                    # 今天不会再执行了，报告"错过"。
                    # 两种情况都算错过：程序当时没跑（tries==0），
                    # 或者试过了但都失败了（tries>0 且已出重试窗口）。
                    key = (str(rule.get("id")), day)
                    if key not in _reported_miss:
                        _reported_miss.add(key)
                        missed.append(dict(rule))
    return due, missed


def mark_fired(rule_id, now=None):
    """记下"这条今天成功执行过了"。

    必须在**实际执行成功后**调用，否则失败了就再也不会重试。

    now 是给测试用的：tick(now) 用固定时刻判定，写回的时间戳也得是同一个
    时刻。以前签名收了 now 却只用它算日期、last_time 仍取真实时钟 ——
    "判定用假时间、记账用真时间"，两边必然对不上（tests/devices/
    test_scheduler.py 因此只能在早上 7 点前后跑通，其余时间必挂）。
    """
    stamp = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    with _lock:
        for r in _rules:
            if str(r.get("id")) == str(rule_id):
                r["last_fired"] = day
                r["last_result"] = "ok"
                r["last_time"] = stamp
                r["tries_today"] = 0
                r.pop("done_today", None)
                _atomic_write(_rules)
                return True
    return False


def mark_failed(rule_id, msg="", now=None):
    """执行失败：不写 last_fired（还能重试），但记一次尝试。

    当天试够 MAX_TRIES 次后标记 done_today，今天不再试。

    now 同理是给测试用的（见 mark_fired）：重试窗口是拿 last_time 和
    tick 的 now 相减算的，两边必须用同一个时钟。
    """
    stamp = now or time.time()
    with _lock:
        for r in _rules:
            if str(r.get("id")) == str(rule_id):
                tries = int(r.get("tries_today") or 0) + 1
                r["tries_today"] = tries
                r["last_result"] = "fail"
                r["last_error"] = str(msg)[:200]
                r["last_time"] = stamp
                if tries >= MAX_TRIES:
                    r["done_today"] = time.strftime("%Y-%m-%d",
                                                    time.localtime(stamp))
                _atomic_write(_rules)
                return True
    return False


def reset_daily():
    """跨天时清掉"今天已试几次"的计数。

    每天 00:00 之后第一次 tick 会调用。
    """
    with _lock:
        changed = False
        for r in _rules:
            if r.get("tries_today"):
                r["tries_today"] = 0
                changed = True
            if r.get("done_today"):
                r.pop("done_today", None)
                changed = True
        if changed:
            _atomic_write(_rules)


def next_runs(limit=5, now=None):
    """接下来会触发的几条（给界面显示"下次 07:30 开灯"）。"""
    ts = now or time.time()
    out = []
    with _lock:
        rules = list(_rules)
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        t = str(rule.get("time") or "")
        if not t or ":" not in t:
            continue
        out.append((t, rule))
    out.sort(key=lambda x: x[0])
    return [{"time": t, "rule": r} for t, r in out[:limit]]
