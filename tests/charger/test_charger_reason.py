# -*- coding: utf-8 -*-
"""充电器桥接：失败原因分类 + 状态字段 + 日志节流的测试（纯离屏，不碰蓝牙）

要挡住的 bug（2026-10-25 实际发生）：
  这台机器的 USB 蓝牙适配器掉了，设备管理器里变成一个
  "未知 USB 设备(设备描述符请求失败)"，bleak 报 No Bluetooth adapter found。
  但界面只显示"设备断开，30 秒后重连"，那个重连永远不会成功 ——
  用户一直等，还以为充电器坏了。

根因在 bridge 的 `_run()`：先 `_set(status='error', detail=msg)` 存了真实原因，
紧接着又 `_set(status='reconnecting', detail='设备断开…')` 把它盖掉，
detail 是界面上唯一能看到的字段 → 真实原因永远到不了界面。

这里钉死三件事：
  1. 三类错误能被分类成 no-adapter / device-away / unknown
  2. snapshot 一定带 reason / reason_title / hint / last_error（原因不再丢）
  3. 同一条错误不会每轮刷一行日志（以前一晚上能刷几千行，把新信息埋掉）

跑法：.venv\\Scripts\\python.exe -u tests/charger_reason_test.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_bridge():
    """按文件路径加载 charger/bridge.py。

    不能直接 `import bridge`：它会读配置、起线程。用 spec 加载后不调 main()，
    模块级只有常量，安全。
    """
    path = os.path.join(BASE, "charger", "bridge.py")
    spec = importlib.util.spec_from_file_location("cuk_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cuk_bridge"] = mod
    spec.loader.exec_module(mod)
    return mod


B = _load_bridge()

PASS = FAIL = 0


def ok(cond, name, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ %s" % name)
    else:
        FAIL += 1
        print("  ❌ %s %s" % (name, detail))


def out(msg=""):
    print(msg)


# ---------------------------------------------------------------- 1. 分类
out("\n=== 1. 错误分类：先认「没有适配器」，再认「设备找不到」===")

# 实际日志里抓到的原文（bleak 0.x 在 Windows 上的两种形态）
REAL_NO_ADAPTER = [
    "('No Bluetooth adapter found', "
    "<BleakBluetoothNotAvailableReason.NO_BLUETOOTH: 1>)",
    "Failed to start scanner. Is Bluetooth turned on?",
    "Bluetooth is not available on this system",
]
for msg in REAL_NO_ADAPTER:
    ok(B.classify_error(msg) == "no-adapter",
       "no-adapter ← %s" % msg[:46], B.classify_error(msg))

# 充电器不在范围 / 被手机占着 —— 这类等一会儿确实会好，不能跟上面混为一谈
GONE = [
    "Device with address AA:BB:CC:DD:EE:FF was not found",
    "No matching devices found",
    "connect timeout after 20s",
    "Device not in range",
]
for msg in GONE:
    ok(B.classify_error(msg) == "device-away",
       "device-away ← %s" % msg[:46], B.classify_error(msg))

ok(B.classify_error("认证失败 — Token 可能不正确") == "unknown",
   "认证失败归为 unknown（不是设备不见了，重连也没用）")
ok(B.classify_error("") == "unknown", "空字符串不抛异常，归 unknown")
ok(B.classify_error(None) == "unknown", "None 不抛异常，归 unknown")

# 顺序检查：一句话里同时出现两类关键词时，必须判 no-adapter
ok(B.classify_error("Failed to start scanner: No Bluetooth adapter found") ==
   "no-adapter", "同一句里两类关键词都在 → 判 no-adapter（顺序正确）")


# ---------------------------------------------------------------- 2. 文案
out("\n=== 2. 每种原因都必须有「标题 + 该怎么办」 ===")
for key in ("no-adapter", "device-away", "unknown"):
    info = B.REASONS.get(key) or {}
    ok(bool(info.get("title")), "%s 有 title" % key)
    ok(len(info.get("hint") or "") >= 8, "%s 有可执行的 hint" % key, len(info.get("hint") or ""))
    ok(isinstance(info.get("cap"), (int, float)) and info["cap"] >= 30,
       "%s 重连上限 >= 30s" % key, info.get("cap"))

# 没有适配器时重连不可能成功，退避要比其它原因更长
ok(B.REASONS["no-adapter"]["cap"] > B.REASONS["device-away"]["cap"],
   "no-adapter 退避更久（60s > 30s），不做无意义的快重连")


# ---------------------------------------------------------------- 3. 状态
out("\n=== 3. snapshot 必须把真实原因带出去 ===")
br = B.Bridge("AA:BB:CC:DD:EE:FF", b"0123456789ab", "测试充电器")
snap = br.snapshot()
for k in ("reason", "reason_title", "hint", "last_error"):
    ok(k in snap, "snapshot 有 %s 字段" % k, sorted(snap.keys()))
ok(snap["reason"] == "", "刚启动时 reason 为空（还没失败过）")

# 模拟一轮失败后的状态（照 _run 里 except 分支的写法）
err = REAL_NO_ADAPTER[0]
reason = B.classify_error(err)
br._set(reason=reason, last_error=err,
        reason_title=B.REASONS[reason]["title"],
        hint=B.REASONS[reason]["hint"])
snap = br.snapshot()
ok(snap["reason"] == "no-adapter", "失败原因被记住")
ok(snap["last_error"] == err, "底层原文被记住（排查靠它）")
ok("适配器" in (snap["reason_title"] or ""), "标题是人话", snap["reason_title"])
ok("适配器" in (snap["hint"] or ""), "hint 指向适配器", snap["hint"])

# 关键回归：reconnecting 这轮不许把原因清掉（原来就是这里丢的）
detail = "本机没有蓝牙适配器 · 2 秒后复查"
br._set(status="reconnecting", detail=detail, reason=reason,
        reason_title=B.REASONS[reason]["title"], hint=B.REASONS[reason]["hint"])
snap = br.snapshot()
ok(snap["status"] == "reconnecting", "状态已切到 reconnecting")
ok(snap["reason"] == "no-adapter", "★ reconnecting 之后 reason 仍在（这就是原来的 bug）")
ok(snap["last_error"] == err, "★ reconnecting 之后 last_error 仍在")
ok("复查" in snap["detail"] and "重连" not in snap["detail"],
   "no-adapter 的文案说「复查」而不是「重连」", snap["detail"])

# 连上后要清干净，别把上一次的失败一直挂在界面上
br._set(status="ok", detail="已认证 · 实时监控中",
        reason="", reason_title="", hint="", last_error="")
snap = br.snapshot()
ok(snap["reason"] == "" and snap["hint"] == "" and snap["last_error"] == "",
   "连上后原因被清空（界面不会一直挂红条）")


# ---------------------------------------------------------------- 4. 节流
out("\n=== 4. 日志节流：同一条错误不许每轮刷一行 ===")
br2 = B.Bridge("AA:BB:CC:DD:EE:FF", b"0123456789ab")
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    for _ in range(6):                      # 模拟 6 轮同样的失败
        br2._log_throttled(err, "no-adapter")
lines = [l for l in buf.getvalue().splitlines() if l.strip()]
ok(len(lines) == 1, "同样错误打 6 次只留 1 行", "实际 %d 行" % len(lines))
ok("no-adapter" in lines[0], "日志里带原因码，方便 grep", lines[0][:70])

buf2 = io.StringIO()
with contextlib.redirect_stderr(buf2):
    br2._log_throttled("另一条完全不同的错误", "device-away")   # 消息变了
ok(len([l for l in buf2.getvalue().splitlines() if l.strip()]) == 1,
   "消息变了立刻打（不会被节流吞掉）")

# 超过 300 秒窗口后会再打一次，避免长时间静默让人以为进程死了
br2._log_t -= 400
buf3 = io.StringIO()
with contextlib.redirect_stderr(buf3):
    br2._log_throttled("另一条完全不同的错误", "device-away")
ok(len([l for l in buf3.getvalue().splitlines() if l.strip()]) == 1,
   "超过 5 分钟窗口会再打一次（证明进程还活着）")


# ---------------------------------------------------------------- 5. 前端契约
out("\n=== 5. 前端契约：字段名必须与 bridge 一致 ===")
js = io.open(os.path.join(BASE, "charger", "app.js"), encoding="utf-8").read()
html = io.open(os.path.join(BASE, "charger", "index.html"), encoding="utf-8").read()
for field in ("s2.reason", "s2.hint", "s2.reason_title", "s2.detail"):
    ok(field in js, "app.js 读 %s" % field)
for eid in ("hintBar", "hintTitle", "hintText"):
    ok(('id="%s"' % eid) in html, "index.html 有 #%s" % eid)
ok("showHint(s2)" in js, "失败分支调用了 showHint(s2)")
ok("showHint(null)" in js, "成功分支会收起提示条")
ok("no-adapter" in js, "前端认得 no-adapter（红条 + 曲线空状态文案）")

out("\n" + "=" * 56)
out("通过 %d / 失败 %d" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
