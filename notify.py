# -*- coding: utf-8 -*-
"""
主动推送 —— 让"有事发生"能离开这台电脑。

事件流解决了"记得过去"，但记得住不等于知道：你人在外面，面板上的字你是
看不到的。这个模块把达到级别的事件转发到手机（Bark / ntfy / 企业微信 /
任意 webhook）。

三条设计约束，都是被"推送扰民"这件事逼出来的：
  · 只推 warn / bad，info 级（开关灯、下雨）留在面板里，不震手机；
  · 同类事件 3 分钟内合并成一条（"客厅·台灯 相关 3 条"），不然一次批量
    开关能把手机震到关机；
  · 有免打扰时段，夜里只放 bad 过去。

不引第三方依赖：urllib 够用，且项目本来就只靠标准库跑（aiohttp 是
miservice 带进来的，不想让通知模块也绑上去）。
"""

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# 路径统一由 config 提供（否则搬家后用户的推送配置会被当成没配置过）
import config

CFG_PATH = config.NOTIFY_CFG

LEVELS = {"info": 0, "good": 1, "warn": 2, "bad": 3}

_lock = threading.RLock()
_cfg = None
_q = queue.Queue(maxsize=200)
_worker = None
_started = {"v": False}

# 合并窗口：同 (kind, src) 的重复事件在这段时间内折成一条
MERGE_WINDOW = 180
_pending = {}          # key -> {title, n, first_ts, last_ts, level}


def _load():
    global _cfg
    if _cfg is not None:
        return _cfg
    cfg = {"channels": [], "min_level": "warn",
           "quiet": "23:30-07:30", "quiet_level": "bad"}
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    if not isinstance(cfg.get("channels"), list):
        cfg["channels"] = []
    _cfg = cfg
    return cfg


def _save(cfg):
    global _cfg
    _cfg = cfg
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)


def config():
    """给前端看：URL 里带密钥，只回显到主机名，不回全文。"""
    cfg = _load()
    out = []
    for i, c in enumerate(cfg.get("channels") or []):
        out.append({
            "i": i,
            "type": c.get("type") or "webhook",
            "name": c.get("name") or "",
            "on": bool(c.get("on", True)),
            "hint": _mask(c.get("url") or ""),
        })
    return {
        "channels": out,
        "min_level": cfg.get("min_level", "warn"),
        "quiet": cfg.get("quiet", ""),
        "quiet_level": cfg.get("quiet_level", "bad"),
    }


def _mask(url):
    try:
        u = urllib.parse.urlsplit(url)
        host = u.netloc or "?"
        path = u.path or ""
        if len(path) > 14:
            path = path[:7] + "…" + path[-4:]
        return host + path
    except Exception:
        return "—"


def set_channels(items):
    """整表替换。前端传来的是 [{type,url,name,on,keep}]。

    URL 里含密钥，所以读接口只回脱敏地址（见 _mask）。用户没改某一行时，
    前端提交 url="" 并带上 keep=原来的下标，这里按 keep 把旧地址接回来。
    这样密钥永远不会经过 GET 响应，也不会因为"回显脱敏串"被写坏。
    """
    cfg = _load()
    old = cfg.get("channels") or []
    chans = []
    for c in items or []:
        url = (c.get("url") or "").strip()
        if not url:
            try:
                keep = int(c.get("keep", -1))
            except Exception:
                keep = -1
            if 0 <= keep < len(old):
                url = (old[keep].get("url") or "").strip()
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        chans.append({
            "type": (c.get("type") or "webhook").strip()[:16],
            "url": url[:600],
            "name": (c.get("name") or "").strip()[:30],
            "on": bool(c.get("on", True)),
        })
    cfg["channels"] = chans
    _save(cfg)
    return config()


def set_options(min_level=None, quiet=None, quiet_level=None):
    cfg = _load()
    if min_level in LEVELS:
        cfg["min_level"] = min_level
    if quiet is not None:
        cfg["quiet"] = str(quiet)[:20]
    if quiet_level in LEVELS:
        cfg["quiet_level"] = quiet_level
    _save(cfg)
    return config()


# ------------------------------------------------------------------ 组装
def _fmt(e):
    title = e.get("title") or "家里有事"
    bits = []
    if e.get("detail"):
        bits.append(e["detail"])
    bits.append(datetime.fromtimestamp(e["ts"]).strftime("%H:%M"))
    lv = {"warn": "留意", "bad": "警告", "good": "正常", "info": "提示"}
    bits.append(lv.get(e.get("level"), ""))
    return title, " · ".join([b for b in bits if b])


def _post(url, data, headers, timeout=8):
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.status


def _deliver(chan, title, body):
    """按通道类型拼各自的格式。各家的机器人协议一个比一个不一样，
    这里只用最小公用字段，不追花哨（markdown 排版在手机通知里也看不清）。"""
    t = (chan.get("type") or "webhook").lower()
    url = chan["url"]

    if t == "bark":
        # Bark 支持把标题放 path：https://api.day.app/KEY/标题/内容
        base = url.rstrip("/")
        u = base + "/" + urllib.parse.quote(title) + "/" + urllib.parse.quote(body)
        return _post(u, b"", {"Content-Type": "application/json"})

    if t == "ntfy":
        return _post(url, body.encode("utf-8"), {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": urllib.parse.quote(title.encode("utf-8")),
            "Priority": "default",
        })

    if t in ("wecom", "qyweixin"):
        payload = {"msgtype": "text",
                   "text": {"content": "%s\n%s" % (title, body)}}
    elif t in ("feishu", "lark"):
        payload = {"msg_type": "text",
                   "content": {"text": "%s\n%s" % (title, body)}}
    elif t == "dingtalk":
        payload = {"msgtype": "text",
                   "text": {"content": "%s\n%s" % (title, body)}}
    else:
        payload = {"title": title, "body": body, "text": "%s\n%s" % (title, body),
                   "source": "mihome-pc", "ts": int(time.time())}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _post(url, raw, {"Content-Type": "application/json; charset=utf-8"})


def _in_quiet(cfg):
    q = (cfg.get("quiet") or "").strip()
    if not q or "-" not in q:
        return False
    try:
        a, b = q.split("-", 1)
        h1, m1 = [int(x) for x in a.split(":")]
        h2, m2 = [int(x) for x in b.split(":")]
    except Exception:
        return False
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    lo, hi = h1 * 60 + m1, h2 * 60 + m2
    return (lo <= cur or cur < hi) if lo > hi else (lo <= cur < hi)


def _should_send(e):
    cfg = _load()
    if not cfg.get("channels"):
        return False
    lv = LEVELS.get(e.get("level"), 0)
    floor = LEVELS.get(cfg.get("min_level") or "warn", 2)
    if _in_quiet(cfg):
        floor = max(floor, LEVELS.get(cfg.get("quiet_level") or "bad", 3))
    return lv >= floor


# ------------------------------------------------------------------ 队列
def _loop():
    while True:
        job = _q.get()
        if job is None:
            return
        title, body = job
        cfg = _load()
        for chan in cfg.get("channels") or []:
            if not chan.get("on", True) or not chan.get("url"):
                continue
            for attempt in (0, 1):
                try:
                    _deliver(chan, title, body)
                    break
                except Exception:
                    if attempt:
                        break
                    time.sleep(1.2)


def start():
    global _worker
    with _lock:
        if _started["v"]:
            return
        _started["v"] = True
        _worker = threading.Thread(target=_loop, daemon=True)
        _worker.start()


def on_event(e):
    """evtlog 造出一条事件后回调这里。必须快 —— 它在采集线程里被调用。"""
    if not _should_send(e):
        return
    key = (e.get("kind"), e.get("src") or e.get("title"))
    now = time.time()
    with _lock:
        slot = _pending.get(key)
        if slot and now - slot["first"] < MERGE_WINDOW:
            slot["n"] += 1
            slot["last"] = now
            slot["detail"] = e.get("detail") or slot.get("detail")
            return
        _pending[key] = {"first": now, "n": 1,
                         "title": e.get("title") or "",
                         "detail": e.get("detail") or "",
                         "level": e.get("level")}
    # 延迟 1 秒再入队：给同一批变化一个合并的机会
    threading.Timer(1.0, _flush, args=(key,)).start()


def _flush(key):
    with _lock:
        slot = _pending.pop(key, None)
    if not slot:
        return
    title = slot["title"]
    body = slot.get("detail") or ""
    if slot["n"] > 1:
        # 不要用 split(" ")[0] 截标题 —— "CPU 温度偏高" 会被截成 "CPU"，
        # 手机上看到一条"CPU（3 条相关）"根本不知道发生了什么。
        title = "%s（%d 次）" % (slot["title"], slot["n"])
        body = body + "｜共 %d 次" % slot["n"]
    try:
        _q.put_nowait((title, body))
    except queue.Full:
        pass


def test():
    """点"测试"时立刻发一条，不看级别、不受免打扰限制 —— 用户就是要确认
    通道能不能用，这时候被安静策略拦下来只会让人以为坏了。"""
    cfg = _load()
    done, fail = [], []
    for chan in cfg.get("channels") or []:
        if not chan.get("url"):
            continue
        try:
            _deliver(chan, "测试通知", "如果你在手机上看到这条，通道就是通的。")
            done.append(chan.get("name") or chan.get("type"))
        except Exception as ex:
            fail.append("%s: %s" % (chan.get("name") or chan.get("type"), ex))
    return {"ok": bool(done), "sent": done, "failed": fail,
            "n_channels": len(cfg.get("channels") or [])}
