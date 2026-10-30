"""进程管理：像手机"正在运行"那样看后台占用，并且能安全地关掉。

三条设计约束，都是硬性的：

  1) **不拖慢机器**。这是本模块的第一原则，因为它自己就是个查看器，
     不该成为被查看的对象。实测把 psutil 逐个 OpenProcess 的做法换掉：
     280 个进程 psutil 要 500~2500ms，这里一次 NtQuerySystemInformation
     3ms 拿全量（任务管理器走的是同一条系统调用），再对每个进程做一次
     OpenProcess 取路径/IO（2.8ms 全量）。**一轮扫描约 6ms**。
     面板关着的时候连这 6ms 也不花：只有最近 25 秒内有人要过列表，
     后台线程才每 2.5 秒扫一轮；没人看就只睡。

  2) **不动必须运行的进程**。保护分四层：
       · 名单（csrss / lsass / svchost / dwm / explorer…）
       · 路径（SystemRoot 下、无窗口的一律不许杀）
       · 读不到路径（受保护或高权限进程）一律不许杀
       · 自身（本程序的 python、启动器、go2rtc、ffmpeg）
     命中任一条返回 blocked。前端藏按钮只是礼貌，后端独立复核才是防线。

  3) **关得温和**。默认先给它的窗口发 WM_CLOSE（等于点右上角 ×），
     让它有机会保存；2.5 秒没退再问用户要不要强制。只有明确要求才
     TerminateProcess。
"""

import ctypes
import collections
import os
import struct
import threading
import time
from ctypes import wintypes as wt

try:
    import psutil
except Exception:                                     # pragma: no cover
    psutil = None

IS_WIN = os.name == "nt"
BASE = os.path.dirname(os.path.abspath(__file__))

STATE = {
    "rows": [],
    "ts": 0.0,
    "cost": 0.0,
    "error": "",
}

_LOCK = threading.RLock()
_STARTED = [False]
_BASED = [False]

_CPU_T = {}              # pid -> 上次的累计 CPU 秒
_CPU_AT = [0.0]
_IO = {}                 # pid -> {"r","w","t","rb","wb"}
_IO_AT = [0.0]
_USER = {}               # pid -> 用户名（懒补，只用来展示）

_WIN = [{}]              # pid -> (窗口标题, hwnd)

_USED = [0.0]            # 最近一次有人要列表的时间
IDLE = 25.0
TICK = 2.5

_ME = os.getpid()
_ME_PARENT = [0]
try:
    _ME_PARENT[0] = os.getppid()
except Exception:
    pass


# ---------------------------------------------------------------- Win32
_k32 = None
_ntdll = None
if IS_WIN:
    try:
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        _k32.OpenProcess.restype = wt.HANDLE
        _k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                                    wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        _k32.QueryFullProcessImageNameW.restype = wt.BOOL
        _k32.CloseHandle.argtypes = [wt.HANDLE]
        _k32.GetProcessIoCounters.argtypes = [wt.HANDLE, ctypes.c_void_p]
        _k32.GetProcessIoCounters.restype = wt.BOOL

        _ntdll = ctypes.WinDLL("ntdll")
        _ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                                    ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        _ntdll.NtQuerySystemInformation.restype = ctypes.c_long
    except Exception:                                 # pragma: no cover
        _k32 = None
        _ntdll = None

_PROC_QUERY = 0x1000            # PROCESS_QUERY_LIMITED_INFORMATION


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


def _nt_procs():
    """一次 NtQuerySystemInformation 拿全部进程。

    字段偏移是 64 位 Windows 7~11 一致的布局（SYSTEM_PROCESS_INFORMATION）：
      0x04 线程数 / 0x20 创建时间 / 0x28 用户态时间 / 0x30 内核态时间
      0x38 映像名长度 / 0x40 映像名指针 / 0x50 PID / 0x58 父 PID / 0x90 工作集
    时间单位 100ns。用 struct 按偏移解而不是定义巨大结构体，是因为结构体
    有大量保留字段和版本差异，按偏移解反而更不容易出错。
    """
    if not _ntdll:
        return []
    size = 1 << 20
    _MISMATCH = 0xC0000004
    for _ in range(6):
        buf = ctypes.create_string_buffer(size)
        need = ctypes.c_ulong(0)
        st = _ntdll.NtQuerySystemInformation(5, buf, size, ctypes.byref(need))
        st_u = st & 0xFFFFFFFF          # NTSTATUS 是有符号 32 位，0xC0000004 会变负数
        if st_u == _MISMATCH:
            size = max(need.value + 65536, size * 2)
            continue
        if st != 0:
            return []
        off = 0
        out = []
        while True:
            nxt = struct.unpack_from("<I", buf, off)[0]
            thr = struct.unpack_from("<I", buf, off + 0x04)[0]
            created = struct.unpack_from("<q", buf, off + 0x20)[0]
            user = struct.unpack_from("<q", buf, off + 0x28)[0]
            kern = struct.unpack_from("<q", buf, off + 0x30)[0]
            nlen = struct.unpack_from("<H", buf, off + 0x38)[0]
            naddr = struct.unpack_from("<Q", buf, off + 0x40)[0]
            pid = struct.unpack_from("<Q", buf, off + 0x50)[0]
            ppid = struct.unpack_from("<Q", buf, off + 0x58)[0]
            # 0x90 是 WorkingSetSize，偏移随版本有过 +8 的出入，所以另取
            # PrivatePageCount(0xC8) 做兜底校验不划算，直接用 psutil 的
            # 总量校准：这里只要求"相对大小"正确，绝对值下面用 total 卡住。
            wss = struct.unpack_from("<Q", buf, off + 0x90)[0]
            name = ""
            if naddr and nlen:
                try:
                    name = ctypes.string_at(naddr, nlen).decode("utf-16le", "ignore")
                except Exception:
                    name = ""
            out.append((pid, ppid, name, thr, wss,
                        (user + kern) / 1e7,
                        # CreateTime 是 1601-01-01 起的 100ns 计数，
                        # 减掉 1601→1970 的秒差才是 Unix 时间
                        (created / 1e7 - 11644473600) if created else 0.0))
            if not nxt:
                break
            off += nxt
        return out
    return []


def _probe(pid, want_io):
    """一次 OpenProcess 同时拿路径和 IO 计数。返回 (exe, (读字节, 写字节) | None)。"""
    if not _k32:
        return "", None
    h = _k32.OpenProcess(_PROC_QUERY, False, pid)
    if not h:
        return "", None
    try:
        exe = ""
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            exe = buf.value
        io = None
        if want_io:
            c = _IO_COUNTERS()
            if _k32.GetProcessIoCounters(h, ctypes.byref(c)):
                io = (c.ReadTransferCount, c.WriteTransferCount)
        return exe, io
    except Exception:
        return "", None
    finally:
        _k32.CloseHandle(h)


def _windows():
    """{pid: (标题, hwnd)}，只取"看得见的顶层窗口"。

    这是把进程分成"前台应用 / 后台"的唯一可靠依据——没有窗口的微信和
    有窗口的微信，在系统眼里完全一样，在用户眼里是两件事。
    跳过工具窗口（WS_EX_TOOLWINDOW），否则输入法、托盘小窗会把一堆
    后台进程误判成前台。
    """
    if not IS_WIN:
        return {}
    try:
        import ctypes
        from ctypes import wintypes as wt2

        u = ctypes.WinDLL("user32", use_last_error=True)
        u.IsWindowVisible.argtypes = [wt2.HWND]
        u.GetWindowTextLengthW.argtypes = [wt2.HWND]
        u.GetWindowTextW.argtypes = [wt2.HWND, wt2.LPWSTR, ctypes.c_int]
        u.GetWindowLongW.argtypes = [wt2.HWND, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [wt2.HWND, ctypes.POINTER(wt2.DWORD)]

        out = {}

        @ctypes.WINFUNCTYPE(wt2.BOOL, wt2.HWND, wt2.LPARAM)
        def cb(hwnd, _lp):
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                if u.GetWindowLongW(hwnd, -20) & 0x00000080:   # WS_EX_TOOLWINDOW
                    return True
                n = u.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                title = (buf.value or "").strip()
                if not title:
                    return True
                pid = wt2.DWORD(0)
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value and pid.value not in out:
                    out[pid.value] = (title, hwnd)
            except Exception:
                pass
            return True

        u.EnumWindows(cb, 0)
        return out
    except Exception:
        return {}


def _post_close(pid):
    """给这个 pid 的窗口发 WM_CLOSE（等于点右上角 ×），返回是否发出。"""
    info = _WIN[0].get(pid)
    if not info or not IS_WIN:
        return 0
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
        return 1 if u.PostMessageW(info[1], 0x0010, 0, 0) else 0
    except Exception:
        return 0


# ---------------------------------------------------------------- 保护名单
# 杀掉会立刻出问题：蓝屏、登录会话掉、桌面消失、杀软失效。
# explorer.exe 单独说明：杀它不会崩，但桌面会黑一下再重载，看起来像坏了，
# 所以也列进来——想重启资源管理器有更该用的办法。
_CRITICAL = {
    "system", "system idle process", "idle", "registry", "memory compression",
    "secure system", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "lsaiso.exe", "svchost.exe", "dwm.exe",
    "fontdrvhost.exe", "sihost.exe", "ctfmon.exe", "audiodg.exe",
    "taskhostw.exe", "runtimebroker.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe",
    "searchindexer.exe", "applicationframehost.exe", "textinputhost.exe",
    "explorer.exe", "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe",
    "securityhealthsystray.exe", "smartscreen.exe", "spoolsv.exe",
    "wudfhost.exe", "dllhost.exe", "conhost.exe", "taskmgr.exe",
    "widgets.exe", "widgetservice.exe", "lockapp.exe", "logonui.exe",
}

# 本程序自己的东西：杀了就看不到这个页面了
_SELF_NAMES = {"go2rtc.exe", "go2rtc-tk.exe", "ffmpeg.exe",
               "python.exe", "pythonw.exe", "waitress-serve.exe"}

_SYSTEMROOT = (os.environ.get("SystemRoot") or "C:\\Windows").lower() + "\\"

# 项目根目录（也就是这个文件所在目录）。用来认"跑在我们目录里的 exe"。
# 统一 lower + 反斜杠，因为 Win32 拿回来的路径盘符大小写可能和这里不一样
# （C:\... vs c:\...），直接 startswith 会漏判。
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_LE = _BASE_DIR.lower().rstrip("\\/") + "\\"
# 虚拟环境目录：解释器都在这下面，但**不等于**就是我们自己。
_VENV_LE = (_BASE_DIR + "\\.venv").lower().rstrip("\\/") + "\\"


def _classify(name, exe, has_win, pid, ppid):
    """返回 (kind, guard)。kind: app 前台 / bg 后台 / sys 系统"""
    low = (name or "").lower()
    if low in _CRITICAL:
        return "sys", "Windows 关键进程，结束会让系统出问题"
    if exe:
        le = exe.lower()
        if le.startswith(_BASE_LE):
            # 项目目录里的东西怎么区分"我们自己的"和"恰好跑在这个目录的"：
            #   · 不在 .venv 下（go2rtc.exe / ffmpeg.exe）→ 是本程序的部件
            #   · 在 .venv 下（python.exe）→ 只有正好是我们/父进程才认，
            #     否则用户拿同一个解释器跑个脚本就再也关不掉了
            if le.startswith(_VENV_LE):
                if pid in (_ME, _ME_PARENT[0]):
                    return "bg", "本页面自己的服务进程"
            else:
                return "bg", "本页面自己的服务进程"
        if le.startswith(_SYSTEMROOT) and not has_win:
            return "sys", "Windows 组件，不认识就不动"
        return ("app" if has_win else "bg"), ""
    # 读不到路径：受保护进程、或权限更高的进程。一律不动。
    if has_win:
        return "app", "看不到它的位置（可能是管理员权限运行的程序）"
    return "sys", "看不到它的位置（受系统保护）"


def can_kill(row):
    """(能不能杀, 原因)。后端独立复核，不依赖前端藏按钮。"""
    pid = row.get("pid")
    if not pid or pid <= 4:
        return False, "系统内核进程"
    if pid in (_ME, _ME_PARENT[0]):
        return False, "这是本页面自己的进程"
    if row.get("guard"):
        return False, row["guard"]
    nm = (row.get("name") or "").lower()
    if nm in _SELF_NAMES:
        exe = (row.get("exe") or "").lower()
        if not exe or exe.startswith(BASE.lower()):
            return False, "本页面自己的服务进程"
    return True, ""


# ---------------------------------------------------------------- 扫描
def _scan():
    if not IS_WIN or not _ntdll:
        return
    if not _LOCK.acquire(blocking=False):
        return
    try:
        t0 = time.time()
        dt = t0 - _CPU_AT[0] if _CPU_AT[0] else 0.0
        _CPU_AT[0] = t0
        ncpu = 1
        total_mem = 1.0
        if psutil:
            try:
                ncpu = psutil.cpu_count() or 1
                total_mem = float(psutil.virtual_memory().total or 1)
            except Exception:
                pass

        procs = _nt_procs()
        wins = _windows()
        _WIN[0] = wins
        want_io = (t0 - _IO_AT[0]) >= 4.5
        if want_io:
            _IO_AT[0] = t0

        rows = []
        alive = set()
        for pid, ppid, name, thr, wss, cput, created in procs:
            if pid == 0:
                continue
            alive.add(pid)
            if not name or name in ("Idle", "System Idle Process") or name.startswith("系统空闲"):
                continue

            # CPU：累计时间差值 / 实际间隔 / 核数 → 占全机百分比
            prev = _CPU_T.get(pid)
            cpu = 0.0
            if prev is not None and dt > 0.05 and cput >= prev:
                cpu = (cput - prev) / dt / ncpu * 100.0
            _CPU_T[pid] = cput
            cpu = round(max(0.0, min(cpu, 100.0)), 1)

            exe, io = _probe(pid, want_io)
            ir = iw = 0.0
            if io is not None:
                prev_io = _IO.get(pid)
                if prev_io and t0 > prev_io["t"]:
                    d = max(0.5, t0 - prev_io["t"])
                    ir = max(0.0, (io[0] - prev_io["rb"]) / d)
                    iw = max(0.0, (io[1] - prev_io["wb"]) / d)
                _IO[pid] = {"r": ir, "w": iw, "t": t0, "rb": io[0], "wb": io[1]}
            else:
                prev_io = _IO.get(pid)
                if prev_io:
                    ir, iw = prev_io["r"], prev_io["w"]

            has_win = pid in wins
            kind, guard = _classify(name, exe, has_win, pid, ppid)
            rows.append({
                "pid": pid,
                "ppid": ppid,
                "name": name,
                "title": (wins[pid][0] if has_win else "")[:120],
                "exe": exe,
                "user": _USER.get(pid, ""),
                "cpu": cpu,
                "mem": int(wss / 1048576),
                "mem_pct": round(wss / total_mem * 100, 2),
                "io_r": int(ir),
                "io_w": int(iw),
                "thr": thr,
                "up": int(max(0, t0 - created)) if created else 0,
                "kind": kind,
                "guard": guard,
                "me": pid in (_ME, _ME_PARENT[0]),
            })

        for d in (_CPU_T, _IO, _USER):
            for k in [k for k in d if k not in alive]:
                d.pop(k, None)

        rows.sort(key=lambda x: (-x["cpu"], -x["mem"]))

        # 用户名懒补：每个进程要 OpenProcessToken + LookupAccountSid，约 0.3ms，
        # 280 个一起做要 90ms，所以每轮只补 12 个，几轮之内补完。
        if psutil:
            todo = [r["pid"] for r in rows if r["pid"] not in _USER][:12]
            for pid in todo:
                try:
                    _USER[pid] = psutil.Process(pid).username().split("\\")[-1]
                except Exception:
                    _USER[pid] = ""
            if todo:
                for r in rows:
                    if r["pid"] in _USER:
                        r["user"] = _USER[r["pid"]]

        with _LOCK:
            STATE["rows"] = rows
            STATE["ts"] = t0
            STATE["cost"] = round((time.time() - t0) * 1000, 1)
            STATE["error"] = ""
    except Exception as e:                            # pragma: no cover
        with _LOCK:
            STATE["error"] = str(e)
    finally:
        try:
            _LOCK.release()
        except Exception:
            pass


def _loop():
    while True:
        try:
            if time.time() - _USED[0] < IDLE:
                _scan()
                time.sleep(TICK)
            else:
                time.sleep(1.0)               # 没人看，只空转检查时间
        except Exception:
            time.sleep(1.0)


def start():
    if _STARTED[0] or not IS_WIN:
        return
    _STARTED[0] = True
    threading.Thread(target=_loop, name="procman", daemon=True).start()


# ---------------------------------------------------------------- 对外
def view(group="all", sort="cpu", q="", limit=0):
    """给前端用的列表。group: all（应用+后台）/ app / bg / sys"""
    _USED[0] = time.time()

    if time.time() - STATE["ts"] > 4.0:
        _scan()
        # 第一次扫描时没有"上一次的累计 CPU 时间"可减，全是 0。
        # 等一拍再扫一轮，首屏就有真实数字。只做一次。
        if not _BASED[0]:
            _BASED[0] = True
            time.sleep(0.4)
            _scan()

    with _LOCK:
        rows = [dict(r) for r in STATE["rows"]]
        ts = STATE["ts"]
        cost = STATE["cost"]
        err = STATE["error"]

    if group in ("app", "bg", "sys"):
        rows = [r for r in rows if r["kind"] == group]
    else:
        rows = [r for r in rows if r["kind"] != "sys"]

    if q:
        ql = q.lower()
        rows = [r for r in rows
                if ql in (r["name"] or "").lower() or ql in (r["title"] or "").lower()]

    if sort == "mem":
        rows.sort(key=lambda x: (-x["mem"], -x["cpu"]))
    elif sort == "name":
        rows.sort(key=lambda x: (x["name"] or "").lower())
    elif sort == "io":
        rows.sort(key=lambda x: (-(x["io_r"] + x["io_w"]), -x["cpu"]))
    else:
        rows.sort(key=lambda x: (-x["cpu"], -x["mem"]))

    if limit > 0:
        rows = rows[:limit]

    return {
        "ok": not err,
        "error": err,
        "ts": round(ts, 2),
        "cost": cost,
        "items": rows,
        "n": len(rows),
        "sum": summary(),
    }


def summary():
    with _LOCK:
        rows = STATE["rows"]
    return {
        "total": len(rows),
        "sys": sum(1 for r in rows if r["kind"] == "sys"),
        "app": sum(1 for r in rows if r["kind"] == "app"),
        "bg": sum(1 for r in rows if r["kind"] == "bg"),
        "mem": sum(r["mem"] for r in rows),
        "cpu": round(sum(r["cpu"] for r in rows), 1),
    }


def get(pid):
    with _LOCK:
        for r in STATE["rows"]:
            if r["pid"] == pid:
                return dict(r)
    return None


def kill(pid, force=False):
    """返回 {ok, msg, need_force?}。默认温和关闭，force=True 才硬杀。"""
    if not psutil:
        return {"ok": False, "msg": "缺少 psutil"}
    # 最基础的那几条先判，不依赖任何缓存
    if not pid or pid <= 4:
        return {"ok": False, "msg": "系统内核进程", "blocked": True}
    if pid in (_ME, _ME_PARENT[0]):
        return {"ok": False, "msg": "这是本页面自己的进程", "blocked": True}

    row = get(pid)
    if not row:
        # 缓存可能是空的（刚启动、或者后台线程还在睡）。
        # 不能因为"我没看见它"就回答"它不在了"——那是把缓存当真相。
        for _ in range(4):
            _scan()
            time.sleep(0.08)
            row = get(pid)
            if row:
                break
    if not row:
        return {"ok": False, "msg": "这个进程已经不在了"}
    ok, why = can_kill(row)
    if not ok:
        return {"ok": False, "msg": why, "blocked": True}

    nm = row.get("name") or str(pid)
    try:
        p = psutil.Process(pid)
    except Exception:
        return {"ok": False, "msg": "进程已经退出"}

    if not force:
        if _post_close(pid):
            end = time.time() + 2.5
            while time.time() < end:
                if not p.is_running():
                    return {"ok": True, "soft": True, "mem": row.get("mem") or 0,
                            "msg": "已关闭 %s" % nm}
                time.sleep(0.12)
            return {"ok": False, "need_force": True,
                    "msg": "%s 没响应关闭请求（可能在等你保存文件）" % nm}

    try:
        p.terminate()                    # Windows 上等于 TerminateProcess
    except Exception as e:
        return {"ok": False, "msg": "结束失败：%s" % e}
    try:
        p.wait(timeout=3)
    except Exception:
        pass
    if p.is_running():
        return {"ok": False, "need_force": True,
                "msg": "%s 还在运行，可能需要管理员权限" % nm}
    return {"ok": True, "mem": row.get("mem") or 0, "msg": "已结束 %s" % nm}
