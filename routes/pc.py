# -*- coding: utf-8 -*-
"""本机硬件状态与进程管理。"""
from flask import Blueprint

bp = Blueprint('pc', __name__)


from flask import jsonify, request

import pcsys
import procman
from services.guard import _json_body
import evtlog


# ---------- 本机硬件状态（CPU / GPU / 内存 / 磁盘 / 网络） ----------
# 采样在 pcsys 的后台线程里跑（1 秒一拍），这里只返回快照，所以前端刷多快都不心疼。
@bp.route("/api/pc")
def api_pc():
    snap = pcsys.snapshot()
    try:
        evtlog.observe_pc(snap)      # 内部带长去抖，2 秒一次也不会刷屏
    except Exception:
        pass
    return jsonify(snap)


@bp.route("/api/pc/sensor", methods=["POST"])
def api_pc_sensor():
    """接入 CPU 温度传感器：以管理员身份拉起 Core Temp（会弹一次 UAC）。

    这是本程序唯一会去启动外部程序的地方，所以门槛写死：
      · 必须是 POST + application/json（跨站根本发不出来）
      · 只认白名单里的路径（pcsys._TOOLS），不接受前端传路径
    """
    if _json_body() is None:
        return jsonify({"ok": False, "error": "需要 application/json"}), 415
    r = pcsys.start_sensor()
    return jsonify(r)


# ---------- 进程：像手机后台那样看，并且能关掉 ----------
# 扫描在 procman 的后台线程里做（按需：只有人看着才扫），这里只读快照。
# 一轮扫描约 15ms（一次 NtQuerySystemInformation + 每进程一次 OpenProcess），
# 比 psutil 逐个读快两个数量级 —— 一个"查看器"不该成为被查看的对象。
@bp.route("/api/pc/procs")
def api_pc_procs():
    d = procman.view(
        group=request.args.get("group", "all"),
        sort=request.args.get("sort", "cpu"),
        q=(request.args.get("q") or "")[:40],
        limit=request.args.get("limit", type=int) or 0,
    )
    return jsonify(d)


@bp.route("/api/pc/procs/kill", methods=["POST"])
def api_pc_procs_kill():
    """结束一个进程。

    这是本程序第二个"会改变系统状态"的接口（第一个是开摄像头），所以
    门槛与它一致：必须 POST + application/json（跨站发不出来），并且
    procman.kill 内部会独立复核保护规则——前端藏按钮不算防线。
    """
    body = _json_body()
    if body is None:
        return jsonify({"ok": False, "error": "需要 application/json"}), 415
    try:
        pid = int(body.get("pid") or 0)
    except Exception:
        pid = 0
    if not pid:
        return jsonify({"ok": False, "error": "缺少 pid"}), 400
    r = procman.kill(pid, force=bool(body.get("force")))
    if r.get("ok"):
        try:
            mb = int(r.get("mem") or 0)
            evtlog.add("proc", r.get("msg") or "结束了一个进程",
                       ("释放约 %d MB 内存" % mb) if mb >= 50 else "",
                       level="info")
        except Exception:
            pass
    return jsonify(r)


@bp.route("/api/pc/tools")
def api_pc_tools():
    return jsonify({"ok": True, "tools": pcsys.tool_hint()})
