# -*- coding: utf-8 -*-
"""后台线程：自愈、事件补采、自动恢复、定时任务。

这些是"页面没人看的时候顶班"的逻辑。共同特征：daemon 线程 + 只读写共享
状态、不碰 Flask 上下文，所以能安全地从 app.py 里挪出来。
"""
import os
import threading
import time

import config
import logx
import devcache
import evtlog
import netguard
import scheduler
import services.live as live_svc
from services.state import S
from services.cloud import _METRICS
from services.state import _LOGIN_LOCK
from services.tokens import restore_token_from_backup
from services.cloud import call
from services.devices import _do_restore
from services.tokens import backup_token
from services.devices import _save_dev_cache
from services.cloud import _NG
from services.live import _dev_meta
from services.devices import _LIVE_CACHE
from services.live import _DEV_META
from services.devices import prefetch_specs
from services.cloud import PRIO_CONTROL

log = logx.get(__name__)


_SELFHEAL_TS = [0.0]
_SELFHEAL_COOLDOWN = 300.0


def _maybe_self_heal(reason):
    """登录态疑似失效时，后台自己捞一次（不动用户界面）。

    miservice 遇到一次认证异常会把 token 文件**删掉**、再用空用户名重新登录
    （必然失败），也就是一次 401 就足够把整条链路废到进程重启为止。
    这里用本地副本 + 重新 device_list 把它捞回来，用户最多看到几秒"正在恢复"，
    而不是被要求重新输密码。

    冷却 5 分钟：如果 token 是真过期了，反复捞只是刷日志。捞一次失败就把
    状态切到"需要重新登录"，把决定权交回用户。
    """
    now = time.monotonic()
    if now - _SELFHEAL_TS[0] < _SELFHEAL_COOLDOWN:
        return False
    if S["status"] != "ready":
        return False
    _SELFHEAL_TS[0] = now
    _METRICS["heals"] += 1
    threading.Thread(target=_self_heal_worker, args=(reason,), daemon=True).start()
    return True


def _self_heal_worker(reason):
    if not _LOGIN_LOCK.acquire(blocking=False):
        return
    try:
        S["status"] = "logging_in"
        S["restore_tries"] = 1
        restore_token_from_backup()
        account, service, devices, homes = call(_do_restore(), timeout=90)
        S["account"], S["service"] = account, service
        S["devices"], S["homes"] = devices, homes
        S["status"] = "ready"
        S["restore_failed"] = False
        S["error"] = None
        S["from_cache"] = False
        backup_token()
        _save_dev_cache()
        log.info("自愈完成：%s", reason)
    except Exception as e:
        kind = netguard.classify(e)
        log.warning("自愈失败（%s）：%s", kind, e)
        if kind == netguard.AUTH:
            S["status"] = "idle"
            S["restore_failed"] = True
            S["error"] = "登录态已失效，请重新输入账号密码"
        else:
            # 只是网络/服务端问题：别把界面切成"未登录"，等下一轮自己好
            S["status"] = "ready" if S["service"] else "idle"
    finally:
        _LOGIN_LOCK.release()


def _evt_worker():
    """页面关着的时候，也把变化记下来。

    设计上有个取舍：只要前端在轮询 /api/live（20 秒一次），这个线程就完全不
    动手 —— 重复拉一遍米家云端既慢又可能触发限流，而且事件已经记过了。
    连续 3 分钟没人访问，才自己补一次，间隔 4 分钟。
    也就是说：有人看时它休眠，没人看时它顶班，正好覆盖"我离开了一会儿"。
    """
    tick = 0
    while True:
        time.sleep(30)
        tick += 1
        try:
            if S["status"] != "ready" or not S["service"]:
                continue
            if evtlog.idle_live_seconds() < 180:
                continue
            try:
                data = live_svc._collect_live()
            except Exception as e:
                # 以前这里是 `except: pass` —— 安静到连"登录早就失效了"
                # 都看不出来，界面只是设备不刷新。
                kind = netguard.classify(e)
                _NG.fail(kind, "evt-worker %s" % e)
                if kind == netguard.AUTH:
                    _maybe_self_heal("后台采集发现登录态失效")
                continue
            _NG.ok()
            evtlog.observe_devices(data, _dev_meta())
            _LIVE_CACHE["data"], _LIVE_CACHE["t"] = data, time.time()
            # 在线/离线只能靠 device_list（属性查询只覆盖在线设备），
            # 它比较重，所以比属性采集更慢：约 12 分钟一次。
            if tick % 24 == 0:
                try:
                    devices = call(S["service"].device_list("full"), timeout=90)
                    if isinstance(devices, tuple):
                        devices = devices[0]
                    now_on = {str(d.get("did")) for d in devices if d.get("isOnline")}
                    names = {str(d.get("did")): (d.get("name") or str(d.get("did")))
                             for d in devices}
                    evtlog.observe_online(
                        {did: names.get(did, did) for did in names
                         if did not in now_on},
                        now_on)
                    S["devices"] = devices        # 顺手让设备列表保持新鲜
                    _DEV_META["t"] = 0.0
                    _NG.ok()
                except Exception as e:
                    kind = netguard.classify(e)
                    _NG.fail(kind, "device_list %s" % e)
                    if kind == netguard.AUTH:
                        _maybe_self_heal("device_list 返回认证失败")
        except Exception:
            pass


def _auto_restore():
    """启动时自动用已保存的 token 恢复登录。

    小米云的 device_list 有时要 20-30 秒（尤其冷启动 / 网络慢），
    所以这里重试 3 次；并且如果 miservice 在认证异常时把 token 删了，
    从本地副本还原再试 —— 一次网络抖动不该让用户重新输密码。
    """
    restore_token_from_backup()
    if not os.path.isfile(config.TOKEN_PATH):
        return

    if not _LOGIN_LOCK.acquire(blocking=False):
        return
    try:
        S["status"] = "logging_in"
        S["restore_failed"] = False
        S["error"] = None
        last = None
        for attempt in range(3):
            S["restore_tries"] = attempt + 1
            try:
                account, service, devices, homes = call(_do_restore(), timeout=120)
                S["account"], S["service"] = account, service
                S["devices"], S["homes"] = devices, homes
                S["status"] = "ready"
                S["restore_failed"] = False
                S["from_cache"] = False
                backup_token()
                _save_dev_cache()
                # 放后台：13 台设备顺序拉规范要 10–20 秒，同步跑会一直占着
                # _LOGIN_LOCK，期间 /api/restore 只能返回 pending。
                threading.Thread(target=prefetch_specs, daemon=True).start()
                return
            except Exception as e:
                last = e
                kind = netguard.classify(e)
                if kind == netguard.AUTH:
                    # 登录态本身坏了，重试三次没有任何意义，只是让界面
                    # 在 "logging_in" 上多挂 6 分钟。直接交给用户重新登录。
                    S["error"] = "登录态已失效，请重新输入账号密码"
                    break
                restore_token_from_backup()
                if attempt < 2:
                    time.sleep(2 + attempt * 3)

        S["status"] = "idle"
        S["restore_failed"] = True
        if last is not None and not S["error"]:
            S["error"] = netguard.summarize(last)
    finally:
        _LOGIN_LOCK.release()


def _sched_worker():
    """定时开关的执行线程：每 15 秒判一次。

    15 秒而不是 60 秒：正点那一分钟有 4 次机会判定，
    偶尔撞上一次网络抖动漏掉了，下一次还能补上（仍在同一分钟内）。
    """
    last_day = ""
    while True:
        try:
            now = time.time()
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            if day != last_day:
                # 跨天：清掉"今天试了几次"的计数
                scheduler.reset_daily()
                last_day = day

            due, missed = scheduler.tick(now)
            for rule in due:
                _run_sched_rule(rule)
            for rule in missed:
                _note_sched_missed(rule)
        except Exception:
            pass
        time.sleep(15)


def _run_sched_rule(rule):
    """执行一条到期规则。"""
    rid = str(rule.get("id"))
    did = str(rule.get("did") or "")
    label = rule.get("name") or rule.get("title") or did
    try:
        siid = int(rule.get("siid"))
        piid = int(rule.get("piid"))
    except (TypeError, ValueError):
        scheduler.mark_failed(rid, "规则缺少 siid/piid")
        return

    if S["status"] != "ready":
        # 还没登录好，不能算"失败"（那是网络/登录问题，不是规则问题），
        # 也不标记失败——留在 due 里下次再试。
        return

    value = rule.get("value")
    try:
        code = call(S["service"].miot_set_prop(did, (siid, piid), value),
                    timeout=30, prio=PRIO_CONTROL)
        ok = (code in (0, None)) or (isinstance(code, dict) and code.get("code") in (0, 1))
    except Exception as e:
        ok = False
        msg = str(e)
    else:
        msg = ""

    if ok:
        scheduler.mark_fired(rid)
        _sched_event("定时执行成功", "%s · %s" % (label, _sched_action(value)), "info")
    else:
        scheduler.mark_failed(rid, msg)
        _sched_event("定时执行失败", "%s · %s" % (label, msg[:60] or "设备无响应"), "warn")


def _sched_action(value):
    if value is True:
        return "开启"
    if value is False:
        return "关闭"
    return str(value)


def _note_sched_missed(rule):
    """错过了：只记录，不执行。

    白天突然亮灯比不亮更烦人——睡了一觉醒来发现灯自己开了，
    用户会以为家里进了人。错过了就在"动态"里报一声。
    """
    label = rule.get("name") or rule.get("title") or rule.get("did")
    _sched_event("定时错过了", "%s · %s（程序当时没运行）"
                 % (label, rule.get("time")), "warn")


def _sched_event(title, detail, level="info"):
    """把定时事件记进事件流。"""
    try:
        evtlog.add("sched", title, detail, level=level)
    except Exception:
        pass


def _load_cached_devices():
    """启动时读设备缓存，让界面立刻有东西可显示。

    米家云的 device_list 在冷启动/网络慢时要 20–30 秒，
    这段时间界面是空的。有了缓存就能先渲染出上次��设备列表，
    真实数据到了再覆盖。

    读不到、读坏了、过期了——一律返回 False，正常走在线加载。
    缓存只加速，不添堵。
    """
    try:
        got = devcache.load()
    except Exception as e:
        log.warning("设备缓存读取异常：%r", e)
        return False
    if not got:
        log.info("没有可用的设备缓存，等云端数据")
        return False
    devices, homes, _meta = got
    if not devices:
        log.info("设备缓存里没有设备")
        return False
    S["cached_devices"] = devices
    S["cached_homes"] = homes
    S["from_cache"] = True
    log.info("已载入设备缓存：%d 台设备 / %d 个房间", len(devices), len(homes))
    return True
