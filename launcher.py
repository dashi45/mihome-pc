# -*- coding: utf-8 -*-
"""米家 · 电脑版 —— 桌面程序入口

双击后：
  1. 单实例检查：已经开着就把那个窗口叫到前面来
  2. 摄像头转流（go2rtc，RTSP 8554）没开就拉起来
  3. 后端（Flask，8765）没开就拉起来，等它可用
  4. 开一个原生窗口（WebView2）装界面 —— 没有地址栏、没有标签页，
     任务栏一个图标，关掉窗口程序就完全退出

打包：pyinstaller --onefile --noconsole --name 米家 --icon <ico 绝对路径>
"""
import ctypes
import hashlib
import json
import os
import threading
import traceback
import socket
import subprocess
import sys
import time
from ctypes import wintypes

import creds
import logx
import tray as _tray

def _default_dir():
    """程序目录的默认值：源码运行取本文件所在目录，打包后取 exe 所在目录。

    以前这里写死了一条绝对路径（开发机上的下载目录），而且它是 find_dir()
    候选表里的第一项 —— 等于"优先相信自己写死的路径，而不是眼前这个 exe"。
    换台机器、换个目录、换个用户名，程序就找不到自己的后端。
    MIHOME_HOME 可覆盖（把数据放别的盘时用）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = os.environ.get("MIHOME_HOME") or _default_dir()
# 必须和后端 config.PORT 一致 —— launcher 不 import config（要保持能被
# 独立打包成窗口外壳），所以两边都从同一个环境变量取默认值。
PORT_WEB = int(os.environ.get("MIHOME_PORT", "8765"))
PORT_CAM = 1984
PORT_RTSP = 8554       # go2rtc 的 RTSP 输出，转码链路（camhls）靠它取流
TITLE = "米家 · 电脑版"

log = logx.get(__name__)

# ---------------------------------------------------------------- 启动分段打点
# 双击到窗口出现这一段，过去除了"体感慢"没有任何数字，出问题只能靠猜 ——
# 历史上那次 12 秒卡死（webview.start → create_window 卡在 IME 窗口）就是靠
# 翻 pywebview 自己的日志才定位到的。现在每过一个阶段写一行：
#
#     启动 start_app      + 376ms  累计    447ms
#
# 注意这记的是**进程内**耗时，第一行不可能早于解释器启动，所以真实体感还要
# 加上 exe 加载/解包那几百毫秒。排查时看相邻两行的差比看累计值更有用。
_BOOT_T0 = time.time()
_BOOT_LAST = [_BOOT_T0]


def _boot_mark(name):
    """记一段启动耗时。任何异常都不许冒出去（打点不能影响启动）。"""
    try:
        now = time.time()
        dt = (now - _BOOT_LAST[0]) * 1000.0
        total = (now - _BOOT_T0) * 1000.0
        _BOOT_LAST[0] = now
        log.info("启动 %-14s +%5.0fms  累计 %6.0fms", name, dt, total)
        return total
    except Exception:
        return 0.0


def _cam_did(base):
    """默认摄像头 did —— 从 cam.json 读，不再写死在源码里。

    取值顺序：cam.json 的 "default" → streams 里的第一条 → 空串。
    空串时 _cam_env 会退化成"用 cam_secret.json 里的第一台摄像头"，
    与改造前行为一致，只是不再假设世界上只有一台摄像头。
    """
    try:
        with open(os.path.join(base, "cam.json"), encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception:
        return ""
    did = str(cfg.get("default") or "").strip()
    if did:
        return did
    streams = cfg.get("streams") or {}
    return str(next(iter(streams))) if streams else ""


def _page_url(base):
    """界面地址带版本号：js 一变地址就变，WebView 缓存的旧页面彻底失效。

    （历史教训：后端重启了，但窗口里跑的还是 12 小时前缓存的旧 js，
      表现就是"新版变慢、按钮点了没反应"这种灵异问题）"""
    try:
        v = int(os.path.getmtime(os.path.join(base, "static", "app.js")))
    except Exception:
        v = 1
    return "http://127.0.0.1:8765/?v=%d" % v


# 界面地址不在这里算：版本号取的是 static/app.js 的 mtime，而 APP_DIR 在打包后
# 是 exe 所在目录（那里没有 static/），算出来恒等于 1，等于版本号机制失效。
# 改由 open_window(base) 在拿到真正的程序目录之后再算 —— 见 _page_url。

CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008

# 记录本次由自己拉起来的子进程，退出时收拾干净
_started = []

# ---- 托盘/挂起状态（open_window 里赋值，IPC 线程与托盘线程会读） ----
_WIN = None            # pywebview 的 Window 对象
_SUSPENDED = False     # True = 窗口已进托盘，前端定时器已被掐掉
_TRAY_HINTED = False   # 第一次进托盘的气泡提示只弹一次
_FORCE_QUIT = False    # True = 本次关闭是用户明确选了退出（托盘菜单/弹窗），别再拦
_CLOSE_ASKING = False  # 页面上的关闭询问弹窗正在展示（防重复触发）
_QUIT_DONE = False        # True = webview 主循环已退出，main() 的正常收尾已接管
_QUIT_WATCHDOG_SEC = 8.0  # destroy 后主循环这么久没退出 → 判定收尾卡死，兜底强退


# ---------------- 关闭行为记忆（X 按钮的"记住我的选择"） ----------------
def _close_pref_path(base=None):
    return os.path.join(base or (find_dir() or APP_DIR), "ui_close_pref.json")


def _load_close_pref():
    """读"记住的选择"。返回 None / "tray" / "exit"。坏了当没记住（宁可多问一次）。"""
    try:
        with open(_close_pref_path(), encoding="utf-8") as f:
            v = (json.load(f) or {}).get("close_action")
        return v if v in ("tray", "exit") else None
    except Exception:
        return None


def _save_close_pref(v):
    try:
        if v in ("tray", "exit"):
            tmp = _close_pref_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"close_action": v}, f)
            os.replace(tmp, _close_pref_path())     # 原子写，别留半截文件
        else:
            try:
                os.remove(_close_pref_path())
            except OSError:
                pass
        _sync_tray_extra()
    except Exception as e:
        log_err("save_close_pref", e)


def _sync_tray_extra():
    """把"已记住的关闭行为"反映到托盘右键菜单（点一下即可恢复询问）。"""
    try:
        pref = _load_close_pref()
        if pref == "tray":
            _tray.set_extra("已记住：关闭时最小化到托盘（点击恢复询问）", _reset_close_pref)
        elif pref == "exit":
            _tray.set_extra("已记住：关闭时直接退出（点击恢复询问）", _reset_close_pref)
        else:
            _tray.set_extra(None, None)
    except Exception:
        pass


def _reset_close_pref():
    """托盘菜单点击：清掉记住的选择，下次点 X 重新询问。"""
    _save_close_pref(None)
    try:
        _tray.notify("已恢复关闭询问", "下次点关闭按钮会重新询问")
    except Exception:
        pass


class _CloseApi:
    """pywebview js_api 桥：页面上的关闭询问弹窗把选择回传到这里。

    js_api 的方法在后台线程被调用 —— hide_to_tray / destroy 都走
    Invoke，线程安全没问题（与 ipc 线程的呼出路径同一套）。
    """

    def close_choice(self, action, remember=False):
        global _CLOSE_ASKING, _FORCE_QUIT
        _CLOSE_ASKING = False
        if remember and action in ("tray", "exit"):
            _save_close_pref(action)
        if action == "exit":
            _FORCE_QUIT = True
            # 不能在这里同步 win.destroy()：close_choice 跑在 WebView2 的
            # JS 桥回调线程，destroy 内部同步 Invoke 到 GUI 线程关窗并销毁
            # WebView2，而 WebView2 要等本次 JS 调用返回才肯释放 —— JS 等
            # Python 返回、Python 等 GUI 线程、GUI 线程等 WebView2，互相等
            # 成环，窗口直接「未响应」（页面弹窗点「退出程序」实测必现）。
            # 必须先让本方法返回，再从别的线程销毁窗口。
            threading.Timer(0.3, _tray_quit).start()
        elif action == "tray":
            hide_to_tray()


def trim_memory():
    """把本进程 + 整棵子进程树的工作集压到最小。返回省下的字节数。

    EmptyWorkingSet 把页面赶进「备用列表」：任务管理器里的内存立刻降，
    别的程序要内存时内核直接拿走；页面本身还在，自己访问时缺页调回 ——
    对"后台躺着、偶尔呼出"的用法正合适。

    只在进托盘 / 托盘菜单手动释放时调用。不能反复 trim：
    把热数据也赶出去，呼出后的第一次交互会变慢（app.py 启动时那次
    _trim_once 的注释里记过这个教训）。

    范围：本进程 + 全部子孙。WebView2 的 msedgewebview2.exe 是本进程
    的子进程树，后端（shim+真解释器）、go2rtc、ffmpeg 也都在里面 ——
    一次遍历全覆盖。哪个进程打不开（权限）就跳过，不影响其他。
    """
    try:
        import psutil
        root = psutil.Process(os.getpid())
        procs = [root] + root.children(recursive=True)
    except Exception:
        return 0
    try:
        psapi = ctypes.WinDLL("psapi")
        psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        psapi.EmptyWorkingSet.restype = wintypes.BOOL
        k32 = ctypes.WinDLL("kernel32")
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                    wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
    except Exception:
        return 0
    RIGHTS = 0x0400 | 0x0100      # PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA
    before = {}
    for p in procs:
        try:
            before[p.pid] = p.memory_info().rss
        except Exception:
            continue
        h = k32.OpenProcess(RIGHTS, False, p.pid)
        if h:
            try:
                psapi.EmptyWorkingSet(h)
            except Exception:
                pass
            try:
                k32.CloseHandle(h)
            except Exception:
                pass
    saved = 0
    time.sleep(0.25)              # 等内核把页面挪进备用列表
    for pid, was in before.items():
        try:
            now = psutil.Process(pid).memory_info().rss
            saved += max(0, was - now)
        except Exception:
            continue
    return saved


def log_err(where, exc=None):
    """记一条错误。统一走 logx（logs/mihome.log，按天轮转）。

    改造前这里是自己 open("launcher.log","a") 手写的，和后端的 print、
    start.log 三份并存 —— 排查一个问题要同时翻三个文件，还不知道哪条是
    哪条。现在只有一个出口。

    exc 既可以是异常对象，也可以直接是一句说明文字。
    早先这里只处理异常对象：传字符串时会走进
    `traceback.format_exception(type(str), ...)`，那一步自己抛
    AttributeError（str 没有 __traceback__），又被外层 except 吞掉，
    于是日志里只剩下"[时间] 位置"两行、真正的信息一个字都没留下。
    """
    try:
        if exc is None:
            log.info(where)
        elif isinstance(exc, BaseException):
            log.error("%s：%s", where, exc, exc_info=True)
        else:
            log.error("%s：%s", where, exc)
    except Exception:
        # 日志本身不能反过来把程序搞挂
        pass


def _cam_env(base):
    """从 cam_secret.json 读出摄像头凭据，转成 go2rtc 认识的 ${MI_*} 变量。

    为什么要这么绕：go2rtc.yaml 里原先明文写着米家账号 token 和内网 IP，
    那个文件一旦被分享/上传/备份就等于把摄像头和账号一起交出去。现在
    配置文件里只有 ${MI_XIAOMI_TOKEN} 这类占位符，真正的秘密集中在这个
    **不进版本库**的 json 里（见 .gitignore）。

    返回 (env_dict, error)。error 非空时调用方要落日志 —— go2rtc 在环境变量
    缺失时不会报"配置错误"，只会认证失败，不主动说清楚用户根本猜不到。
    """
    path = os.path.join(base, "cam_secret.json")
    if not os.path.isfile(path):
        return {}, ("找不到 %s —— 摄像头不会出画面。\n"
                    "请从 cam_secret.json.example 复制一份并填入米家账号 token。" % path)
    # 明文和 DPAPI 加密两种格式都认（老文件不用迁移，下次保存时自动加密）
    cfg = creds.load_json(path)
    if cfg is None:
        return {}, "读取 %s 失败：不是合法 JSON（或换机器后解不开）" % path

    xi = cfg.get("xiaomi") or {}
    user = str(xi.get("user") or "").strip()
    token = str(xi.get("token") or "").strip()
    region = str(xi.get("region") or "cn").strip()
    if not user or not token:
        return {}, "cam_secret.json 里的 xiaomi.user / xiaomi.token 没填，摄像头不会出画面"

    cams = cfg.get("cameras") or {}
    did = _cam_did(base)
    cam = cams.get(did) or (next(iter(cams.values())) if cams else {})
    ip = str((cam or {}).get("ip") or "").strip()
    if not ip:
        return {}, ("cam_secret.json 里没有摄像头（%s）的 ip，摄像头不会出画面"
                    % (did or "未在 cam.json 里登记"))

    env = {
        "MI_XIAOMI_USER": user,
        "MI_XIAOMI_TOKEN": token,
        "MI_XIAOMI_REGION": region,
        "MI_CAMERA_IP": ip,
        "MI_CAMERA_DID": did,
        "MI_CAMERA_MODEL": str(cam.get("model") or "chuangmi.camera.029a02"),
        "MI_CAMERA_SUBTYPE": str(cam.get("subtype") or "hd"),
    }
    return env, ""


def msg(title, text, flags=0x10):
    """弹窗（带图标，无需控制台）。"""
    ctypes.windll.user32.MessageBoxW(None, text, title, flags)


def rotate_log(base, name, limit=2 * 1024 * 1024):
    """日志防爆：超过 limit 就清空（保留一个 .old 备份）。"""
    try:
        path = os.path.join(base, name)
        if os.path.isfile(path) and os.path.getsize(path) > limit:
            old = path + ".old"
            try:
                os.replace(path, old)
            except Exception:
                os.remove(path)
    except Exception:
        pass


#: 本机监听端口表的短缓存。见 port_open。
_LISTEN_CACHE = {"t": 0.0, "ports": None}


def _listen_ports(ttl=0.02):
    """本机处于 LISTEN 状态的 TCP 端口集合（IPv4 + IPv6），1ms 级。

    直接问内核（GetExtendedTcpTable），**不发起连接**。读不到时返回 None，
    调用方退回「真连一次」的老办法。

    为什么必须这样：这台机器对**没有监听**的本地端口不回 RST，而是静默
    丢包（Hyper-V / WSL / Docker 装的过滤规则会这样）。于是
    socket.create_connection 只能一路等到超时 —— 实测探测一个没开的端口
    要烧满 350ms。启动路径上这种探测要走 4~5 次（8765 两次、8554、1984），
    光「确认服务还没起来」就白等 1.4 秒，这是双击到出窗口慢的主因。
    查表则不论端口开没开都是 1ms 级。
    """
    now = time.time()
    if _LISTEN_CACHE["ports"] is not None and now - _LISTEN_CACHE["t"] < ttl:
        return _LISTEN_CACHE["ports"]
    ports = None
    try:
        iphlpapi = ctypes.windll.iphlpapi

        class _ROW(ctypes.Structure):
            _fields_ = [("dwState", wintypes.DWORD),
                        ("dwLocalAddr", wintypes.DWORD),
                        ("dwLocalPort", wintypes.DWORD),
                        ("dwRemoteAddr", wintypes.DWORD),
                        ("dwRemotePort", wintypes.DWORD),
                        ("dwOwningPid", wintypes.DWORD)]

        TCP_TABLE_OWNER_PID_LISTENER = 3
        got = set()
        for family in (socket.AF_INET, socket.AF_INET6):
            size = wintypes.DWORD(0)
            # 第一次调用只为拿缓冲区大小（必然返回 ERROR_INSUFFICIENT_BUFFER）
            iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False,
                                         family, TCP_TABLE_OWNER_PID_LISTENER, 0)
            if size.value <= 0:
                continue
            buf = ctypes.create_string_buffer(size.value)
            ret = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False,
                                               family, TCP_TABLE_OWNER_PID_LISTENER, 0)
            if ret != 0:
                continue
            n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
            rows = (_ROW * n).from_buffer(buf, ctypes.sizeof(wintypes.DWORD))
            for r in rows:
                got.add(socket.ntohs(r.dwLocalPort & 0xFFFF))
        if got:
            ports = got
    except Exception:
        ports = None
    _LISTEN_CACHE["t"] = now
    _LISTEN_CACHE["ports"] = ports
    return ports


def port_open(port, host="127.0.0.1", timeout=0.35):
    """端口上有没有人在听。

    本机地址优先查内核监听表（1ms，不受「丢包式端口」拖累）；
    查不到（非本机地址 / API 不可用）才退回真连一次。
    """
    if host in ("127.0.0.1", "localhost", "::1"):
        ports = _listen_ports()
        if ports is not None:
            return port in ports
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def find_dir():
    """找到含 app.py 的程序目录。

    候选顺序按"可信度"排，不按写死的字符串：
      1. APP_DIR（MIHOME_HOME 覆盖 / exe 所在目录 / 源码目录）
      2. exe 同级、同级下的 mihome-pc、上一级的 mihome-pc（绿色版常见布局）
      3. 本文件所在目录（打包后位于临时解包目录，只作最后兜底）
    每一步都要真的看到 app.py 才认，避免"目录存在但内容不对"。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        exe_dir = here
    up = os.path.dirname(exe_dir)
    up2 = os.path.dirname(up)
    # up 这一项不能省：打包脚本默认把 exe 输出到 <源码目录>/dist/，此时程序目录
    # 正是 exe 的上一级，而候选表里只有 "上一级/mihome-pc" 就找不到 app.py。
    # （写死绝对路径的年代这条不会发作，去掉硬编码后才暴露出来）
    # up2 这一项给 onedir 留的：onedir 产物是 <源码目录>/dist/米家/米家.exe，
    # 比 onefile 深一层，上一层是 dist\ 而不是源码目录 —— 没有 up2 就会报
    # "找不到程序目录（缺 app.py）"，而那行提示会让用户以为是文件被删了。
    cands = [APP_DIR, exe_dir, os.path.join(exe_dir, "mihome-pc"),
             up, os.path.join(up, "mihome-pc"),
             up2, os.path.join(up2, "mihome-pc"), here]
    seen = set()
    for d in cands:
        if not d or d in seen:
            continue
        seen.add(d)
        if os.path.isfile(os.path.join(d, "app.py")):
            return d
    return None


def load_autostart(base):
    """从源码目录动态加载 autostart 模块。

    为什么不直接 `import autostart`：
      exe 只打包了 launcher.py 本身，autostart.py 留在源码目录里。
      直接 import 在打包版里会 ModuleNotFoundError。
    为什么不在打包时把它塞进去：
      塞进去就多了一份副本，源码改了 exe 里还是旧的 —— 正是我们要避免的
      那种"改了没生效"的灵异问题。运行时按路径加载，永远用最新的那份。
    """
    try:
        import importlib.util
        path = os.path.join(base, "autostart.py")
        if not os.path.isfile(path):
            return None
        spec = importlib.util.spec_from_file_location("_mihome_autostart", path)
        if not spec or not spec.loader:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        log_err("load_autostart", e)
        return None


# ---------------- 单实例：命名管道 IPC ----------------
"""为什么把"找窗口 + PostMessage"换成命名管道（旧方案在本项目翻车三次）：

  1. `FindWindowW` 只返回**任意一个**同名窗口。桌面上有第二个同名窗口时
     会认错人 —— 实测拿到过完全无关的 pid，差点把人家的窗口关掉。
  2. 窗口过程已经死了（白壳：webview 崩了但窗口没销毁）时，消息发进去也
     没人处理，`PostMessage` 照样返回成功，调用方完全看不出来。
  3. 单向消息没有返回值。发完只能靠"等 1 秒看窗口还在不在"猜，猜错的
     后果是叠出第二个实例，用户看到两个窗口互相抢。

  命名管道把这三件事一次解决：管道名按程序目录做 hash，只有本程序的实例
  会在这个名字上听（不会认错人）；对端必须是活进程才能应答（白壳自然暴露，
  因为它的后端已经不通了）；一收一回带回真实结果（shown / starting / dead），
  调用方据此决定"退出"还是"接管重启"。

  额外的好处：Windows 内核对命名管道做引用计数，最后一个句柄关闭时管道对象
  自动消失。所以不存在 QLockFile 那种"进程被强杀留下 stale lock、下次启动
  要判 pid 才知道能不能拿"的问题 —— 没有锁文件，也就没有锁文件残留。
"""
try:
    from multiprocessing.connection import Client as _IpcClient
    from multiprocessing.connection import Listener as _IpcListener
except ImportError:                      # 极简环境（裁掉了 multiprocessing）
    _IpcClient = _IpcListener = None

_IPC_KEY = b"mihome-pc-ipc-v1"
_IPC = None          # Listener 要留引用，否则被 GC 掉就没人听了
_IPC_BASE = None     # 关闭时要用它算出管道名去唤醒服务线程
_IPC_STOP = threading.Event()


def _ipc_addr(base):
    """管道名按程序目录 hash。

    不能写死一个全局名字：同一台机器上可能同时存在源码调试版、打包版、
    复制出去的备份版，它们各自是**独立的程序**，不该互相当成"已经在跑"。
    按目录区分，各自单实例。
    """
    norm = os.path.normcase(os.path.abspath(base)).encode("utf-8")
    tag = hashlib.md5(norm).hexdigest()[:16]
    return r"\\.\pipe\mihome-pc-" + tag


def ipc_call(base, cmd, timeout=6.0):
    """给已经在跑的实例发一条指令。返回 (reply, error)。

    reply is None and error is None  → 没有实例在听（全新启动）
    reply 形如 {"ok": True, "state": "shown"|"starting"} 或 {"ok": False, "state": "dead"}
    error 非空 → 管道本身出问题（权限、超时），调用方应回退到窗口探测
    """
    if _IpcClient is None:
        return None, "no-multiprocessing"
    try:
        conn = _IpcClient(_ipc_addr(base), family="AF_PIPE", authkey=_IPC_KEY)
    except FileNotFoundError:
        return None, None            # 没有实例在听：正常情况，不是错误
    except OSError as e:
        return None, "connect:%s" % e
    except Exception as e:           # AuthenticationError 等
        return None, "auth:%s" % e
    try:
        conn.send({"cmd": cmd, "pid": os.getpid()})
        if not conn.poll(timeout):
            return None, "timeout"
        return conn.recv(), None
    except (EOFError, OSError) as e:
        return None, "io:%s" % e
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _front_window(dirs):
    """把本程序自己的窗口提到最前。返回 hwnd 或 None。

    只认 `_is_our_process` 验过归属的窗口 —— 光看标题会认错人。
    """
    _trace("front enter")
    wins = _our_windows(dirs)
    _trace("front wins=%d" % len(wins))
    if not wins:
        return None
    u32 = ctypes.windll.user32
    u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.BringWindowToTop.argtypes = [wintypes.HWND]
    u32.IsIconic.argtypes = [wintypes.HWND]
    for hwnd, _pid in wins:
        try:
            if u32.IsIconic(hwnd):
                _trace("front restore-send")
                u32.ShowWindow(hwnd, 9)      # SW_RESTORE：最小化的要先还原
                _trace("front restore-done")
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
            _trace("front ok")
            return hwnd
        except Exception:
            continue
    return None


def _ipc_serve_loop(base, listener):
    """在已有实例里应答后续启动的实例。daemon 线程，窗口关掉就随之结束。"""
    dirs = _our_dirs(base)
    while not _IPC_STOP.is_set():
        try:
            conn = listener.accept()
        except (OSError, EOFError):
            return                        # Listener 被关了，正常退出
        except IndexError:
            # ipc_stop() 已经把 _handle_queue 清空了。不单独接住的话，
            # 这里会变成 IndexError → continue → IndexError 的死循环，
            # 一秒钟烧掉整颗核，日志还被刷爆。
            return
        except Exception as e:
            log_err("ipc_accept", e)
            time.sleep(0.2)               # 真异常时也别忙等
            continue
        try:
            _trace("ipc accept")
            if not conn.poll(3.0):
                continue                  # 连上不说话，多半是探测工具，别理
            req = conn.recv()
            cmd = (req or {}).get("cmd") if isinstance(req, dict) else None
            _trace("ipc recv: %s" % cmd)
            if cmd == "ping":
                conn.send({"ok": True, "state": "alive",
                           "backend": port_open(PORT_WEB, timeout=1.2)})
            elif cmd == "show":
                # 顺序很重要：**先看后端，再看窗口**。
                # 废实例的典型特征恰恰是"窗口还挂着、后端已经死了"——
                # 如果先看窗口，就会把那个死界面叫到前面，用户看到的
                # 就是"双击没反应"（窗口在那儿，点什么都不动）。
                # 后端不通时窗口在不在都改变不了结论。
                #
                # 探测超时给 1.2 秒（别用默认的 0.35）：我这边能应答管道，
                # 就已经证明 launcher 进程是活的，剩下要判断的只是"后端忙不忙"。
                # 后端正在处理一堆轮询时，0.35 秒的 TCP 握手可能来不及，
                # 那样就会被误判成 dead —— 然后新实例把**好好的**实例杀掉接管，
                # 用户看到窗口自己闪一下就没了。宁可能多等一秒，也不能误杀。
                if not port_open(PORT_WEB, timeout=1.2):
                    conn.send({"ok": False, "state": "dead"})
                else:
                    # 窗口可能在托盘里（隐藏态）：show_main 能把它呼出来，
                    # 找不到再走老路（枚举可见窗口）。两步都不能砍——
                    # 窗口还没建出来时 show_main 拿不到东西，全靠老路
                    # 报 "starting" 让对方等。
                    _trace("ipc show -> show_main")
                    _stack_watchdog(12.0)
                    hwnd = show_main()
                    _trace("ipc show <- show_main")
                    if not hwnd:
                        hwnd = _front_window(dirs)
                    if hwnd:
                        _trace("ipc show send")
                        conn.send({"ok": True, "state": "shown",
                                   "hwnd": int(hwnd)})
                        _trace("ipc show sent")
                    else:
                        # 后端在跑但窗口还没建出来 —— 我正处在启动过程中。
                        # 告诉对方"等等我"，别叠一个实例上来。
                        conn.send({"ok": True, "state": "starting"})
            elif cmd == "__stop":
                # 关闭信号：应答完这一条就退出循环。
                conn.send({"ok": True, "state": "bye"})
                _IPC_STOP.set()
                return
            else:
                conn.send({"ok": False, "state": "unknown-cmd"})
        except Exception as e:
            log_err("ipc_serve", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def ipc_serve(base, attempts=4, gap=0.5):
    """开始在本实例上监听。返回 True = 已经在听。

    刚杀完僵尸实例时，旧进程的管道可能还没被内核回收，第一次建会失败
    （Listener 带 FILE_FLAG_FIRST_PIPE_INSTANCE，同名管道存在就报错）。
    所以重试几次 —— 这个重试本身也是单实例的第二道保险：真有两个实例
    同时启动，只有一个能建起管道。
    """
    global _IPC, _IPC_BASE
    if _IpcListener is None:
        return False
    _IPC_STOP.clear()
    _IPC_BASE = base
    addr = _ipc_addr(base)
    for i in range(attempts):
        try:
            _IPC = _IpcListener(addr, family="AF_PIPE", authkey=_IPC_KEY)
            break
        except OSError as e:
            if i == attempts - 1:
                log_err("ipc_serve", "命名管道建立失败（%s）：%s" % (addr, e))
                return False
            time.sleep(gap)
    t = threading.Thread(target=_ipc_serve_loop, args=(base, _IPC),
                         daemon=True, name="ipc")
    t.start()
    return True


def ipc_stop():
    """关掉监听。

    不能只调 `listener.close()`：Windows 上服务线程正卡在 ConnectNamedPipe 的
    overlapped 等待里，句柄被关掉也不会立刻把它唤醒 —— 结果是"以为停了，
    其实还在接客"（实测：close 之后再用 ipc_call 照样能拿到 shown）。
    所以先立停止标志，再自己连一次发 __stop 把它叫醒，最后才关句柄。
    """
    global _IPC, _IPC_BASE
    _IPC_STOP.set()
    lst, base = _IPC, _IPC_BASE
    _IPC = None
    _IPC_BASE = None
    if lst is None:
        return
    if base:
        try:
            ipc_call(base, "__stop", timeout=1.0)
        except Exception:
            pass
    time.sleep(0.05)          # 让服务线程真的从 accept 里退出来再关
    try:
        lst.close()
    except Exception:
        pass


# ---------------- 单实例 ----------------
MUTEX_NAME = "Local\\MiHomePCDesktop"
_MUTEX = None


def _mutex_acquire(wait=0.0):
    """抢单实例互斥量。True = 本进程拿到了（此后一直持有到进程结束）。

    为什么必须是"抢"而不是"探"：老实现只 CreateMutexW 然后看
    ERROR_ALREADY_EXISTS，拿到结论就把句柄留在 _MUTEX 里不管了 —— 判定
    和启动之间没有任何保护。二次双击撞上"对方正在启动（后端起来了、窗口
    还没建）"时，`already_running() and focus_existing()` 是 True and False
    = False，于是**两个进程一起往下走完整启动流程**，抢 8765/8554，
    谁先谁后看运气。用户看到的是随机白屏 / 摄像头转不出来。

    现在把互斥量当闸门：拿到才许启动，拿不到就等（对方在启动）或叫窗口
    （对方已经在跑）。等不到就交给调用方决定，绝不再并行启动一份。

    注意：判定为"已被占用"之后**必须 CloseHandle**。句柄留着不放，等于把
    对方的签名一直续着 —— 它退出了，互斥量还在，谁都起不来。
    """
    global _MUTEX
    if _MUTEX:
        return True
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = wintypes.BOOL
        deadline = time.time() + max(0.0, wait)
        while True:
            h = k32.CreateMutexW(None, False, MUTEX_NAME)
            exists = ctypes.windll.kernel32.GetLastError() == 183  # ALREADY_EXISTS
            if h and not exists:
                _MUTEX = h
                return True
            if h:
                k32.CloseHandle(ctypes.c_void_p(h))
            if time.time() >= deadline:
                return False
            time.sleep(0.1)          # 对方正在启动 / 正在退出，给它一点时间
    except Exception:
        # 互斥量这套东西坏了也不能不让用户开程序：当成"没人在跑"
        log_err("mutex", "互斥量不可用，跳过单实例保护")
        return True


def already_running():
    """是否已有实例开着我（探测语义，兼容老调用点）。

    现在就是"抢不到互斥量"，抢到的那次也会一直持有 —— 调用它的路径本来
    就打算继续启动，正好。
    """
    return not _mutex_acquire()


# ---------------- 子进程回收：Job Object ----------------
# 为什么需要：launcher 是被父进程主动拉起的窗口外壳。用户从任务管理器
# 结束 / 崩溃 / 被 停止.bat 杀掉时，pythonw app.py 和 go2rtc 不会跟着死
# （它们本来就是 DETACHED 的）。下一次启动 start_app 看到 8765 在听，
# 就认为"后端已经在跑"，直接开窗口 —— 而那个后端其实是上一轮遗留的孤儿，
# 它的 config 可能已经过期，用户看到的是白壳或错乱状态。
#
# Job Object 就是干这个的：KILL_ON_JOB_CLOSE 让句柄一关（= launcher 进程
# 结束，无论怎么结束）整个 Job 里的进程一起被内核干掉。子进程的子孙
# （app.py 拉起的充电器桥、go2rtc 拉起的 ffmpeg）默认也在 Job 里。
_JOB = None

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _job_init():
    """建 Job 并设 KILL_ON_JOB_CLOSE。失败返回 None（不致命）。"""
    global _JOB
    if _JOB is not None:
        return _JOB
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                ctypes.c_void_p(job), _JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            return None
        _JOB = job
        return job
    except Exception:
        return None


def _job_add(p):
    """把刚拉起的子进程放进 Job。失败只记一条，不影响启动。"""
    try:
        job = _job_init()
        if not job:
            return
        k32 = ctypes.windll.kernel32
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        if not k32.AssignProcessToJobObject(
                ctypes.c_void_p(job), ctypes.c_void_p(int(p._handle))):
            # 常见原因：本进程自己已经在一个不允许 breakaway 的 Job 里
            log_err("job", "子进程 %s 没能加入 Job（父进程已被别的 Job 收编？）"
                    % p.pid)
    except Exception:
        pass


def _takeover_dead_instance(base):
    """接管"窗口还在、后端已死"的废实例。返回 True = 已经清干净。

    为什么要专门做这件事：这种废壳的 launcher 进程还活着（所以占着单实例
    互斥量），但它的后端已经没了。只发 WM_CLOSE 是不够的 —— ✕ 会走
    on_closing 的"关闭还是最小化到托盘"询问，那个询问在页面上画弹窗，
    进程于是就卡在那里，互斥量一直不放。实测（tests/e2e/test_launcher_e2e
    第 3 步）：新实例等满 11 秒拿不到互斥量，最后放弃启动，用户看到的是
    "双击没反应" —— 比原来的双启动还糟。

    所以分两步：先礼貌关（正常情况 100ms 内自己就退了），2 秒还不走就
    强杀整棵进程树。判定"废"的依据是后端端口不通，不是猜的。
    """
    dirs = _our_dirs(base)
    if not _our_windows(dirs):
        return True
    if port_open(PORT_WEB):
        # 后端活着 —— 它不是废壳，不许动
        return False
    u32 = ctypes.windll.user32
    u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
    for hwnd, _pid in _our_windows(dirs):
        try:
            u32.PostMessageW(hwnd, 0x0010, 0, 0)     # WM_CLOSE
        except Exception:
            pass
    for _ in range(20):
        time.sleep(0.1)
        if not _our_windows(dirs):
            return True
    killed = 0
    for _hwnd, pid in _our_windows(dirs):
        try:
            _kill_tree(pid)
            killed += 1
        except Exception:
            pass
    if killed:
        log_err("single_instance", "废实例卡在关闭询问上，已强杀 %d 个进程" % killed)
    return True


def focus_existing():
    """把已经在跑的窗口叫到前面来。返回 True = 确实找到了**可用**的窗口。

    这里有个很容易踩的坑（真实发生过）：
      只看"窗口在不在"是不够的。后端进程可能已经崩了/被任务管理器杀了，
      但窗口还挂在桌面上。这时候再双击图标，如果只是把那个空窗口叫到
      前面，用户看到的就是"点了没反应"—— 窗口在那儿，界面却是死的，
      而且再也起不来了（单实例逻辑认为已经有人在跑）。
      所以必须同时确认后端活着，否则这个窗口就是废壳，得处理掉。

    另外：只认**属于本程序**的窗口。桌面上恰好有别的程序用了同一个
    窗口标题时，不能把人家的窗口叫到前面、更不能把它关掉。
    """
    base = find_dir() or APP_DIR
    dirs = _our_dirs(base)
    wins = _our_windows(dirs)
    if not wins:
        return False

    u32 = ctypes.windll.user32
    # 64 位下 HWND 必须显式声明，否则会被按 c_int 截断成别的窗口
    u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]

    if port_open(PORT_WEB):
        for hwnd, _pid in wins:
            try:
                u32.ShowWindow(hwnd, 9)          # SW_RESTORE
                u32.SetForegroundWindow(hwnd)
                break
            except Exception:
                continue
        return True

    # 窗口在、后端没了 —— 废窗口。关掉它，让这次启动走完整流程。
    log_err("focus_existing", "窗口还在但后端 %d 没响应，判定为废窗口，关闭后重启" % PORT_WEB)
    for hwnd, _pid in wins:
        try:
            u32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
        except Exception:
            pass
    time.sleep(1.0)
    return False


# ---------------- 服务 ----------------
def start_cam(base):
    if port_open(PORT_RTSP):
        # RTSP 8554 在听就说明转流服务已经活着。
        # （go2rtc 的 HTTP API 已在 go2rtc.yaml 里关掉：它没有鉴权、不校验
        #   Host，会把账号 token 明文吐给任何能连上 1984 的人。所以这里不能
        #   再拿 1984 判断死活了，否则每次启动都会以为它没起来、反复拉起新实例。）
        return
    if port_open(PORT_CAM):
        # 8554 没起来但 1984 有东西在听：旧配置的残留实例（启动时 rtsp 端口被占，
        # go2rtc 会把 rtsp 模块整个禁用，日志是 "exec: rtsp module disabled"），
        # 清掉重来，否则摄像头永远转不出来。
        # 同样按路径过滤，别全局按镜像名杀。
        _kill_by_path("go2rtc-tk.exe", _our_dirs(base))
        time.sleep(1)
    exe = os.path.join(base, "go2rtc", "go2rtc-tk.exe")
    if not os.path.isfile(exe):
        return
    env = dict(os.environ)
    env["NO_PROXY"] = "*"
    env["GO2RTC_CS2_CLOUD_DIDS"] = _cam_did(base)
    env["PATH"] = os.path.join(base, "go2rtc") + os.pathsep + env.get("PATH", "")
    # 凭据不在 go2rtc.yaml 里，从这里注入（go2rtc 自己做 ${MI_*} 替换）
    cam_env, cam_err = _cam_env(base)
    env.update(cam_env)
    if cam_err:
        # 缺凭据时 go2rtc 不会报"配置错误"，只会认证失败 —— 先把话说清楚
        log_err("start_cam", cam_err)
    try:
        # 这里只给 CREATE_NO_WINDOW，不能加 DETACHED_PROCESS：
        # DETACHED 让 go2rtc 完全没有控制台，它再用默认参数拉起 ffmpeg 时，
        # Windows 会给这个控制台程序新建一个（可见的）黑窗口 —— 就是点开摄像头
        # 时弹出的那个 ffmpeg.exe 窗口。给它一个隐藏的控制台，ffmpeg 继承下来，
        # 窗口就不再出现了。
        #
        # 同时把 go2rtc 的日志落盘（go2rtc.log），否则转码断流等问题无从排查。
        log_path = os.path.join(base, "go2rtc", "go2rtc.log")
        log = open(log_path, "ab", buffering=0)
        p = subprocess.Popen([exe, "-c", "go2rtc.yaml"],
                             cwd=os.path.join(base, "go2rtc"),
                             env=env, creationflags=CREATE_NO_WINDOW,
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             close_fds=True)
        _started.append(p)
        _job_add(p)      # launcher 被杀时跟着死，别留孤儿占着 8554
    except Exception:
        pass


def start_app(base, attempts=2):
    """拉起后端，返回 (成功?, 错误信息)

    为什么要重试：刚打包出来的 exe 第一次运行时，杀毒软件（卡巴斯基 /
    Defender）会实时扫一遍新文件，可能把后端拖慢几十秒 —— 表现就是
    "双击图标没反应"。重试一次，比让用户手动再点一下友好得多。
    """
    last_err = ""
    for attempt in range(1, attempts + 1):
        if port_open(PORT_WEB):
            return True, ""
        py = os.path.join(base, ".venv", "Scripts", "pythonw.exe")
        if not os.path.isfile(py):
            py = os.path.join(base, ".venv", "Scripts", "python.exe")
        if not os.path.isfile(py):
            return False, ("找不到 Python 运行环境：\n" + os.path.join(base, ".venv", "Scripts") +
                           "\n\n请先双击项目里的「启动米家.bat」完成初始化。")

        log = open(os.path.join(base, "start.log"), "ab", buffering=0)
        try:
            p = subprocess.Popen([py, os.path.join(base, "app.py")],
                                 cwd=base, stdout=log, stderr=log,
                                 creationflags=DETACHED | CREATE_NO_WINDOW, close_fds=True)
        except Exception as e:
            log_err("start_app", e)
            return False, "后端启动失败：\n%s" % e
        _started.append(p)
        _job_add(p)      # 同上：launcher 一死，后端不许留在后台占着 8765

        # 等后端开端口。原来固定 sleep(0.4) 一轮 —— 后端实测 272ms 就
        # 起来了，但要等到下一轮 400ms 才发现，白等 100~400ms。用户对
        # "双击到出窗口"最敏感，这段是纯浪费。改成前密后疏：头 10 次
        # 每 40ms 探一次（后端通常在这段内就绪），之后退到 200ms，
        # 总等待上限不变（仍是 30 秒）。
        waited = 0.0
        for i in range(150):
            if port_open(PORT_WEB):
                return True, ""
            if p.poll() is not None:
                # 后端进程自己退出了，别在这儿干等满 30 秒
                last_err = ("后端进程启动后立刻退出（退出码 %s），详情见：\n%s"
                            % (p.returncode, os.path.join(base, "start.log")))
                break
            gap = 0.04 if i < 10 else 0.2
            time.sleep(gap)
            waited += gap
            if waited >= 30.0:
                last_err = "后端启动超时，详情见：\n" + os.path.join(base, "start.log")
                break

        if attempt < attempts:
            log_err("start_app", "第 %d 次启动失败，重试一次：%s" % (attempt, last_err))
            time.sleep(2)

    return False, last_err


def _kill_by_path(image_name, dirs):
    """只杀「可执行文件路径落在指定目录内」的同名进程。

    替代 taskkill /IM —— 后者按镜像名全局匹配，会误伤用户自己装的同名程序。
    """
    try:
        import psutil
    except ImportError:
        return 0
    n = 0
    try:
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = (p.info["name"] or "").lower()
                if name != image_name.lower():
                    continue
                if not _under_dirs(p.info["exe"], dirs):
                    continue
                p.kill()
                n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n


def _kill_tree(pid):
    """杀掉一个进程**及其所有子孙进程**，返回杀掉的进程数。

    为什么必须杀整棵树：
      本项目的 .venv/Scripts/python.exe 是个 **shim**（引导器，真正的
      解释器是 WorkBuddy 自带的 vendor python）。它启动真正的解释器作为
      **子进程**，两者的命令行完全一样。
      只 terminate() 掉 shim 的话，真正的后端会变成孤儿继续占着 8765 端口 ——
      表现就是"窗口关了但程序还在后台跑"，下次双击又起不来。
    """
    kids = []
    try:
        import psutil
        p = psutil.Process(pid)
        kids = p.children(recursive=True)
        for c in kids:
            try:
                c.kill()
            except Exception:
                pass
        try:
            p.kill()
        except Exception:
            pass
        return 1 + len(kids)
    except Exception:
        pass
    # psutil 不可用（打包版缺依赖）时退化成 taskkill /T，它也是杀整棵树
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10)
        return 1
    except Exception:
        return 0


def stop_children():
    for p in _started:
        try:
            _kill_tree(p.pid)
        except Exception:
            pass
    # 顺带清掉可能残留的 ffmpeg 转码进程（go2rtc 退出后可能留下孤儿 ffmpeg）。
    #
    # 注意：这里**不能**用 taskkill /IM ffmpeg.exe —— 那是按镜像名全局杀，
    # 用户自己装的 ffmpeg 也会被一起干掉（曾经真出过这种事故）。
    # 改成按**完整路径**过滤：只杀本程序目录下的 ffmpeg。
    _kill_by_path("ffmpeg.exe", _our_dirs(find_dir() or APP_DIR))


# ---------------- 窗口 ----------------
IPC_JS = """
document.addEventListener('contextmenu', e => e.preventDefault(), true);
document.addEventListener('keydown', e => {
  const k = (e.key || '').toLowerCase();
  if (k === 'f5' || ((e.ctrlKey || e.metaKey) && k === 'r')) { e.preventDefault(); return; }
  if (k === 'f12' || ((e.ctrlKey || e.metaKey) && e.shiftKey && (k === 'i' || k === 'j'))) {
    e.preventDefault();
  }
}, true);
"""


def _static_sig(base):
    """前端资源指纹：文件名单 + 大小 + 修改时间。"""
    sdir = os.path.join(base, "static")
    h = hashlib.sha1()
    try:
        for fn in sorted(os.listdir(sdir)):
            p = os.path.join(sdir, fn)
            if not os.path.isfile(p):
                continue
            st = os.stat(p)
            h.update(("%s|%d|%d\n" % (fn, st.st_size, int(st.st_mtime))).encode())
    except OSError:
        return ""
    return h.hexdigest()


def clear_webview_cache(base):
    """前端资源变了才清 WebView2 磁盘缓存（只清 Cache，保留 localStorage）。

    历史版本的后端给静态文件带 12 小时缓存，窗口里可能一直跑旧 js/css，
    所以启动前一把删干净。但代价一直没被算过：把 Cache / Code Cache 一起
    删掉等于丢掉 V8 编译好的字节码，所有 js 下次都要重新解析 —— 实测冷
    首屏 198ms、热首屏 48ms，差的主要就是这块。

    现在页面里的资源都带 ?v=<mtime> 版本号（见 routes/system.py），旧资源
    本来就会被当新 URL 重新拉，不需要靠"每次清缓存"来兜底。所以改成按指纹
    清：前端一改就清、没改就留着。失败无所谓（还有版本号兜底）。
    """
    sig = _static_sig(base)
    mark = os.path.join(base, "runtime", "webview_sig.txt")
    if sig:
        try:
            if os.path.isfile(mark):
                with open(mark, encoding="utf-8") as f:
                    if f.read().strip() == sig:
                        return                  # 前端没动过，缓存留着，启动更快
        except Exception:
            pass

    try:
        import shutil
        root = os.path.join(os.environ.get("APPDATA", ""), "pywebview", "EBWebView")
        if os.path.isdir(root):
            for sub in ("Cache", "Cache_Data", "Code Cache", "GPUCache"):
                p = os.path.join(root, "Default", sub)
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                except BaseException:
                    pass
    except BaseException:
        pass

    if sig:
        try:
            os.makedirs(os.path.dirname(mark), exist_ok=True)
            with open(mark, "w", encoding="utf-8") as f:
                f.write(sig)
        except Exception:
            pass


def _eval_suspend(on):
    """通知前端挂起/恢复。页面没加载完时调用会失败 —— 挂起标志
    照样立住，页面加载完后第一个定时器滴答就会被 suspend.js 拦下。"""
    global _SUSPENDED
    _SUSPENDED = bool(on)
    win = _WIN
    if win is None:
        return
    try:
        win.evaluate_js(
            "window.MiHomeSuspend && MiHomeSuspend(%s)" % ("true" if on else "false"))
    except Exception:
        pass


# EnumWindows 正在检查的窗口（排查"枚举卡死在哪个窗口"用）
_ENUM_CUR = [None, None]


def _stack_watchdog(seconds=12.0):
    """起一个后台线程：seconds 秒后把全部线程的调用栈转储进日志。

    只在 ipc show 这种"应该秒回却可能卡死"的路径上用。转储是只读的，
    不会打断任何线程 —— 卡住了能看见卡在哪一行，没卡住就白等一次。
    """
    def _dump():
        time.sleep(seconds)
        try:
            import sys as _sys
            frames = _sys._current_frames()
            names = {t.ident: t.name for t in threading.enumerate()}
            parts = []
            for tid, fr in frames.items():
                parts.append("--- 线程 %s (id %s)" %
                             (names.get(tid, "?"), tid))
                parts.extend("    " + l.strip()
                             for l in traceback.format_stack(fr)[-6:])
            h = _ENUM_CUR[0]
            if h:
                cls = ctypes.create_unicode_buffer(256)
                try:
                    ctypes.windll.user32.GetClassNameW(h, cls, 256)
                except Exception:
                    cls.value = "?"
                parts.append("--- 卡住的窗口: hwnd=%s class=%s 线程=%s pid=%s"
                             % (h, cls.value, _ENUM_CUR[1],
                                u32pid(h)))
            log_err("stack_watchdog(%ds 无进展)" % seconds,
                    "\n".join(parts))
        except Exception:
            pass
    threading.Thread(target=_dump, daemon=True).start()


def u32pid(hwnd):
    try:
        pid = wintypes.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return "?"


def _slow_log(where, t0, floor=0.8):
    """只记慢的：某一步超过 floor 秒才落一条日志。

    呼出窗口这类交互路径出过"应答超过 8 秒"的慢病，但平常又快 ——
    每次都记日志会被刷爆，只有慢的时候才有排查价值。
    MIHOME_SLOW_FLOOR 环境变量可临时调低（端到端测试用来全量打点）。
    """
    try:
        floor = float(os.environ.get("MIHOME_SLOW_FLOOR", floor))
    except Exception:
        pass
    dt = time.time() - t0
    if dt >= floor:
        log_err(where, "耗时 %.2f 秒（>%.2f）" % (dt, floor))
    return dt


def _trace(where):
    """排查用的无条件打点。环境变量 MIHOME_TRACE=1 时才生效。"""
    if os.environ.get("MIHOME_TRACE"):
        log_err("trace:" + where, "")


def show_main():
    """把主窗口从托盘呼出来。返回 hwnd（拿不到给 None）。

    挂起态的窗口是"隐藏 + 最小化"的组合。用 Win32 的 SW_RESTORE 一步
    处理两种态；pywebview 的 restore()+show() 实测对隐藏过的窗口不生效
    （呼出来还是最小化，IsIconic=True），不要改回去。
    """
    _trace("show_main enter")
    win = _WIN
    if win is None:
        return None
    hwnd = None
    try:
        # pythonnet 的 IntPtr 不吃 int() —— 必须 ToInt64()（实测 int() 抛
        # TypeError，show_main 整个返回 None，白白掉进 EnumWindows 老路）
        hwnd = int(win.native.Handle.ToInt64())
    except Exception:
        pass
    if hwnd:
        u32 = ctypes.windll.user32
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.ShowWindow(hwnd, 9)          # SW_RESTORE：显示 + 还原二合一
        u32.SetForegroundWindow(hwnd)
    else:
        # 拿不到原生句柄就退回高层接口（弱化但聊胜于无）
        try:
            win.restore()
        except Exception:
            pass
        try:
            win.show()
        except Exception:
            pass
    if _SUSPENDED:
        _eval_suspend(False)
    _trace("show_main hwnd=%r" % (hwnd,))
    return hwnd


def hide_to_tray():
    """最小化 → 进托盘：挂起前端 + 藏窗 + 修剪内存。可重入（挂起态直接跳过）。"""
    global _TRAY_HINTED
    if _SUSPENDED or _WIN is None:
        return
    t0 = time.time()
    _eval_suspend(True)          # 先停摄像头/定时器，trim 时量到的就是省完的值
    _slow_log("hide.suspend_js", t0)
    try:
        _WIN.hide()
    except Exception:
        pass
    _slow_log("hide.hide", t0)
    saved = trim_memory()
    _slow_log("hide.trim", t0, floor=2.0)
    if not _TRAY_HINTED:
        _TRAY_HINTED = True
        try:
            _tray.notify("米家仍在后台运行",
                         "点托盘图标随时打开。" +
                         ("已释放 %d MB 内存" % (saved // 1048576) if saved > 33554432
                          else "呼出会很快，不必担心"))
        except Exception:
            pass


def _tray_show():
    """托盘左键/菜单「显示主窗口」。出错必须落盘 —— 托盘链路的异常
    曾经被 except: pass 吞掉，用户看到的就是"点了没反应"。"""
    try:
        show_main()
    except Exception as e:
        log_err("tray.show", e)


def _tray_release():
    """托盘菜单「释放内存」：立刻 trim 并把结果弹出来。"""
    saved = trim_memory()
    try:
        _tray.notify("内存已释放", "省下 %d MB" % (saved // 1048576))
    except Exception:
        pass


def _tray_quit():
    """托盘菜单「退出」与页面弹窗「退出程序」的共同入口。

    与点窗口 ✕ 走同一条路：destroy → webview 主循环退出 → main() 里统一
    stop_children / ipc_stop / tray.stop。

    destroy 内部是同步 Invoke：调用线程会一直等到 GUI 线程把窗关完，期间
    自己完全卡住。所以 destroy 放独立线程跑，并配 _quit_watchdog 看门狗 ——
    窗口没了、主循环却迟迟不退（GUI 线程被别的长任务拖住）时，由看门狗
    代为清理并强制退出，保证进程一定退得掉、子进程不残留。
    """
    global _FORCE_QUIT
    _FORCE_QUIT = True          # 这是用户明确要退出，closing 拦截器必须放行
    threading.Thread(target=_quit_watchdog, daemon=True).start()
    win = _WIN
    if win is not None:
        threading.Thread(target=_do_destroy, args=(win,), daemon=True).start()


def _do_destroy(win):
    try:
        win.destroy()
    except Exception:
        pass


def _quit_watchdog():
    """兜底：destroy 后若主循环 _QUIT_WATCHDOG_SEC 秒内没退出，
    说明收尾卡死，主动清理（杀子进程 / 撤托盘 / 关管道）后强制退出。"""
    time.sleep(_QUIT_WATCHDOG_SEC)
    if _QUIT_DONE:
        return
    log_err("quit_watchdog",
            "窗口已销毁但主循环 %ss 未退出，按收尾卡死处理，执行兜底清理"
            % _QUIT_WATCHDOG_SEC)
    for fn in (stop_children, ipc_stop, lambda: _tray.stop()):
        try:
            fn()
        except Exception:
            pass
    os._exit(0)


_GUI_PRELOAD = {"th": None}


def _preload_gui_stack(wait=0.0):
    """在后台线程里把 GUI 那套栈 import 进来（webview + pythonnet/CLR）。

    实测 import webview 87ms、import pythonnet 165ms —— 都是纯导入，跟后端
    启动没有任何先后依赖，却一直串在"后端就绪之后"才做，等于白等 250ms。
    挪成后台预热、跟后端启动并行，主线程走到 open_window 时已经是缓存命中。

    只 import，不建窗口、不碰 COM —— 失败也不影响主流程：open_window 里
    会再 import 一次，真缺依赖照旧在那里报错。
    """
    def _work():
        for mod in ("webview", "clr"):
            try:
                __import__(mod)
            except Exception:
                pass

    th = _GUI_PRELOAD["th"]
    if th is None:
        th = threading.Thread(target=_work, name="gui-preload", daemon=True)
        _GUI_PRELOAD["th"] = th
        th.start()
    if wait:
        th.join(wait)


def open_window(base):
    # 后端启动期间已在后台预热过；这里只需等它收尾（通常早就完了）
    _preload_gui_stack(wait=5.0)
    import webview
    _boot_mark("import webview")

    global _WIN
    clear_webview_cache(base)

    win = webview.create_window(
        TITLE, _page_url(base),
        width=1520, height=960,
        min_size=(1120, 720),
        background_color="#EFF2F7",
        text_select=False,
        js_api=_CloseApi(),          # 页面的关闭询问弹窗回传选择用
    )
    _WIN = win
    _boot_mark("create_window")

    def on_loaded():
        # 页面 DOM 就绪 = 用户能看见东西了。真正的"窗口已出现在屏幕上"
        # 比这还早一点点（webview.start 内部建窗体），差值就是渲染耗时。
        _boot_mark("页面 loaded")
        try:
            win.evaluate_js(IPC_JS)
            # 启动后才挂托盘的话（理论不会），加载完把真实状态同步给前端
            if _SUSPENDED:
                _eval_suspend(True)
        except Exception:
            pass

    def on_closing():
        """点 ✕（WM_CLOSE → FormClosing）。返回 False 取消本次关闭。

        三档行为：记住的选择直接执行；没记住就把询问弹窗画到页面上
        （复用页面样式，比原生 MessageBox 好看，还能带"记住我的选择"）。
        页面没就绪（刚启动 / 白壳）时退回原生三键弹窗兜底 —— 绝不把
        用户困在一个关不掉的窗口里。

        铁律：本函数跑在 GUI 线程（closing 事件 should_lock 同步触发），
        这里**绝不同步**调 evaluate_js / hide_to_tray：evaluate_js 内部
        Python 信号量等待不泵 Windows 消息，而脚本完成回调排在 GUI
        消息队列里等 GUI 取 —— GUI 卡住取不到，信号量永不释放，
        窗口直接「未响应」（实测必现：点 ✕ 弹窗能显示但之后全卡死，
        因为弹窗是浏览器进程画的，GUI 线程其实已经死在等待里）。
        所以这里只做决策 + 置标志，动作全部丢给后台线程。

        已知边界：Windows 关机/注销也会触发 FormClosing，pywebview 的
        closing 事件不透传 CloseReason，没法精确区分。此时若页面已死会
        落到原生弹窗，系统会以"应用阻止关机"提示用户，属可接受的降级。
        """
        global _CLOSE_ASKING
        if _FORCE_QUIT:
            return True
        pref = _load_close_pref()
        if pref == "tray":
            threading.Thread(target=hide_to_tray, daemon=True).start()
            return False
        if pref == "exit":
            return True
        if _CLOSE_ASKING:
            return False          # 弹窗已经在页面上，别再触发一轮
        _CLOSE_ASKING = True
        threading.Thread(target=_ask_close, args=(win,), daemon=True).start()
        return False

    def _ask_close(win):
        """后台线程里把关闭询问弹窗画到页面上（GUI 线程里做会死锁，
        见 on_closing 注释）。页面没就绪时退回原生三键弹窗。"""
        global _CLOSE_ASKING
        try:
            shown = bool(win.evaluate_js(
                "window.MiHomeCloseAsk ? MiHomeCloseAsk() : false"))
        except Exception:
            shown = False
        if shown:
            return
        _CLOSE_ASKING = False
        r = ctypes.windll.user32.MessageBoxW(
            0, "要退出程序，还是最小化到系统托盘继续运行？\n\n"
               "【是】最小化到托盘　【否】退出程序　【取消】留在当前界面",
            TITLE, 0x24)          # MB_YESNOCANCEL | MB_ICONQUESTION
        if r == 6:                # IDYES
            hide_to_tray()
        elif r == 7:              # IDNO
            _tray_quit()

    win.events.loaded += on_loaded
    win.events.minimized += hide_to_tray
    win.events.closing += on_closing

    # 托盘从启动就在，不等第一次最小化 —— 否则"最小化前托盘不存在"
    # 的间隙里用户找不到任何入口。on_error 把托盘链路的静默异常落盘：
    # "点了没反应"这种故障，日志里一个字都没有是查不动的。
    _tray.start(os.path.join(base, "mihome.ico"), TITLE,
                on_show=_tray_show,
                on_release=_tray_release,
                on_quit=_tray_quit,
                on_error=lambda w, e: log_err(w, e),
                hint="点图标显示窗口，右键有菜单")
    _sync_tray_extra()
    _boot_mark("托盘就绪")

    icon = os.path.join(base, "mihome.ico")   # Windows 后端只认 .ico
    kwargs = dict(private_mode=False, debug=False)   # private_mode=False：localStorage 要留着
    if os.path.isfile(icon):
        kwargs["icon"] = icon
    try:
        webview.start(gui="edgechromium", **kwargs)
    except Exception:
        webview.start(**kwargs)


def _enum_title_windows(title):
    """枚举所有**标题完全等于** title 的顶层窗口，返回 [(hwnd, pid)]。

    为什么不用 FindWindowW：它只返回"任意一个"同名窗口。桌面上只要有
    第二个同名窗口（旧的残留、别的程序抄了这个标题），就会认错人 ——
    实测就踩到过：拿到的 pid 根本不是要找的进程。

    两个必须注意的 Win32 细节（都踩过）：
      1. 回调必须包成 WINFUNCTYPE 实例，普通 Python 函数传进去直接抛
         TypeError；之前异常被 except 吞掉，函数静默返回空列表，
         看起来"就是找不到窗口"，实际是调用本身失败了
      2. argtypes / restype 必须显式声明，否则 64 位 HWND 会按 c_int
         传，句柄被截断成另一个（不存在的）窗口
    """
    out = []
    try:
        u32 = ctypes.windll.user32
        u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u32.GetWindowTextLengthW.restype = ctypes.c_int
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                       ctypes.c_int]
        u32.GetWindowTextW.restype = ctypes.c_int
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                 ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                      wintypes.LPARAM)
        u32.EnumWindows.argtypes = [ENUMPROC, wintypes.LPARAM]
        u32.EnumWindows.restype = wintypes.BOOL

        def _cb(hwnd, _lparam):
            try:
                _ENUM_CUR[0] = hwnd
                _ENUM_CUR[1] = u32.GetWindowThreadProcessId(hwnd, None)
                n = u32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u32.GetWindowTextW(hwnd, buf, n + 1)
                    if buf.value == title:
                        pid = wintypes.DWORD(0)
                        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if pid.value:
                            out.append((hwnd, pid.value))
            except Exception:
                pass
            return True

        cb = ENUMPROC(_cb)     # 必须保持引用，否则被 GC 掉会随机崩
        u32.EnumWindows(cb, 0)
    except Exception as e:
        log_err("enum_title_windows", e)
    return out


# 本程序自己的关键可执行文件（只看 basename）。
#
# 为什么不能写成"本程序目录下的任何文件"：项目里的 tests/ 目录、工具脚本
# 全都在这个目录下，按目录判身份会把**测试脚本、用户的临时脚本**统统
# 当成"自己人"清理掉 —— 实际踩过：launcher 启动时把正在跑测试的进程
# 连同它的父进程一起杀了，表现为"程序自己把会话关掉了"。
#
# 只认这几个关键文件，误伤面才真正收敛。
_OWN_FILES = ("launcher.py", "app.py", "米家.exe")
_OWN_LOWER = tuple(n.lower() for n in _OWN_FILES)


def _mentions_own_file(cmdline):
    """命令行里有没有直接点名本程序的关键文件（快速筛，不看路径）。"""
    if not cmdline:
        return False
    for raw in cmdline:
        if not raw:
            continue
        if os.path.basename(str(raw)).lower() in _OWN_LOWER:
            return True
    return False


def _is_own_cmdline(cmdline, dirs):
    """命令行是否点名了本程序的关键文件，**且路径落在本程序目录内**。

    路径这一层限制是必须的：光看文件名的话，别处的一个 launcher.py
    也会被认成自己人。
    """
    if not cmdline:
        return False
    for raw in cmdline:
        if not raw:
            continue
        name = os.path.basename(str(raw))
        if name.lower() not in _OWN_LOWER:
            continue
        try:
            full = os.path.normcase(os.path.abspath(str(raw)))
        except Exception:
            continue
        for d in dirs:
            try:
                if os.path.commonpath([full, d]) == d:
                    return True
            except Exception:
                continue
    return False


def _is_our_process(pid, dirs):
    """这个 pid 是不是"自己人"。

    判定依据：命令行里直接点名了本程序的关键文件（launcher.py / app.py /
    米家.exe），并且该文件就在本程序目录里。

    为什么不能只看"可执行文件路径"：本项目的 venv 里 python.exe 实际指向的是
    **别处**（WorkBuddy 自带的 vendor python）的可执行文件，路径压根不在
    本程序目录里 —— 实测踩过，靠 exe 路径判定会把自家进程全判成"别人"。

    为什么不能只看"命令行里有本目录下的文件"：那会把 tests/ 里的脚本、
    用户的临时脚本统统算成自己人，然后被 kill_zombies 清掉。
    """
    try:
        import psutil
        p = psutil.Process(pid)
        try:
            cmd = p.cmdline() or []
        except Exception:
            cmd = []
        if _is_own_cmdline(cmd, dirs):
            return True
    except Exception:
        pass
    return False


def _our_windows(dirs):
    """属于本程序的同名窗口：[(hwnd, pid)]"""
    res = []
    for hwnd, pid in _enum_title_windows(TITLE):
        if _is_our_process(pid, dirs):
            res.append((hwnd, pid))
    return res


def _window_pid(dirs=None):
    """拿到本程序窗口所属的进程 pid。找不到返回 None。

    用窗口本身定位，而不是靠命令行 —— 命令行会变（打包/源码、
    复制出去改名），但窗口标题是稳定的。找不到窗口时用进程名兜底。
    """
    if dirs is None:
        dirs = _our_dirs(find_dir() or APP_DIR)
    for _hwnd, pid in _our_windows(dirs):
        return pid
    return None


def _our_dirs(base):
    """本程序可能待在这些目录里 —— 只有这些目录下的进程才算"自己人"。"""
    dirs = set()
    for d in (base, APP_DIR, os.path.dirname(os.path.abspath(__file__))):
        if d:
            dirs.add(os.path.normcase(os.path.abspath(d)))
    if getattr(sys, "frozen", False):
        dirs.add(os.path.normcase(os.path.dirname(sys.executable)))
    return dirs


def _is_our_launcher(cmdline, dirs):
    """判断一条命令行是不是"本程序的启动器"。必须足够严格，宁可漏杀不可错杀。

    曾经的写法是 `"launcher.py" in cmdline` —— 子串匹配，危险得很：
      · 别的项目里同名的 launcher.py 会被当成僵尸杀掉
      · 任何命令行里带这四个字的进程（编辑器、脚本、甚至
        my_launcher.py.bak）统统中招
    现在改成三重约束：
      1. 参数里出现**精确**等于 launcher.py / 米家.exe 的文件名
      2. 它的绝对路径必须落在本程序目录（或 exe 所在目录）下
      3. 绝不能是本进程或本进程的祖先
    """
    if not cmdline:
        return False
    names = ("launcher.py", "米家.exe")
    for raw in cmdline:
        if not raw:
            continue
        name = os.path.basename(raw)
        if name.lower() not in names:
            continue
        try:
            full = os.path.normcase(os.path.abspath(raw))
        except Exception:
            continue
        for d in dirs:
            try:
                if os.path.commonpath([full, d]) == d:
                    return True
            except Exception:
                continue
    return False


def _ancestor_pids():
    """本进程及其所有祖先的 pid —— 这些绝不能动。"""
    out = set()
    try:
        import psutil
        me = psutil.Process(os.getpid())
        out.add(me.pid)
        for a in me.parents():
            out.add(a.pid)
    except Exception:
        out.add(os.getpid())
    return out


def kill_zombies(dry_run=False):
    """清掉只剩窗口、后端已死的僵尸实例。

    场景：后端被任务管理器杀了，但窗口进程还活着。这时候双击图标，
    老实例占着窗口标题和互斥量，新实例要么被当成"重复启动"退出，
    要么和它抢同一个标题 —— 用户看到的就是一个点不动的空窗口。

    识别方式（按可靠度排）：
      1. 窗口标题是 TITLE 的那个进程 —— 最准，标题就是本程序的身份证
      2. 命令行里的启动器 —— 兜底，但必须严格匹配（见 _is_our_launcher）

    两种都要求「后端没在跑」才动手，绝不误杀正常实例。
    dry_run=True 时只查不杀，返回 (命中列表, 实际清理数)。
    """
    try:
        import psutil
    except ImportError:
        return 0

    # 后端还活着就不是僵尸，一个都别动
    if port_open(PORT_WEB):
        return 0

    # 快速排除：僵尸的第一特征是「窗口还在」。连一个带本程序标题的窗口都没有，
    # 就不可能存在僵尸，没必要往下走。
    # 这一步很关键：下面那轮 psutil.process_iter(["pid","cmdline"]) 要逐个进程
    # 打开句柄读命令行，实测 330ms（235 个进程），而它服务的正是"万一标题认不出
    # 进程、再按命令行兜一次"的罕见场景。EnumWindows 只要 1ms。
    if not _enum_title_windows(TITLE):
        return 0

    base = find_dir() or APP_DIR
    dirs = _our_dirs(base)
    protect = _ancestor_pids()

    targets = set()

    # 窗口属于本程序的那些（可能不止一个：残留窗口 + 新窗口）
    for _hwnd, pid in _our_windows(dirs):
        if pid not in protect:
            targets.add(pid)

    try:
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                pid = p.info["pid"]
                if pid in protect:
                    continue
                cmd = p.info["cmdline"] or []
                if not cmd:
                    continue
                # 快速筛：命令行里没点名关键文件的一律不看
                if not _mentions_own_file(cmd):
                    continue
                if _is_our_process(pid, dirs):
                    targets.add(pid)
            except Exception:
                continue
    except Exception as e:
        log_err("kill_zombies", e)

    # 别把自己杀了
    targets.discard(os.getpid())
    targets -= protect

    if not targets:
        return 0

    if dry_run:
        names = []
        for pid in targets:
            try:
                names.append((pid, psutil.Process(pid).name()))
            except Exception:
                names.append((pid, "?"))
        return names

    killed = 0
    for pid in targets:
        # targets 里的进程已经确认是"本程序的"，它们拉起来的子孙自然也是
        # 本程序的组件（后端 / go2rtc / ffmpeg / shim 出来的真解释器），
        # 所以这里整棵树一起杀 —— 只杀父进程的话，shim 出来的真解释器会
        # 变成孤儿继续占着端口。
        try:
            killed += _kill_tree(pid)
        except Exception:
            continue

    if killed:
        log_err("kill_zombies", "清理了 %d 个僵尸实例（窗口在、后端已死）" % killed)
        time.sleep(1.0)
    return killed


def _under_dirs(path, dirs):
    """文件是否位于给定目录之一之下。"""
    if not path:
        return False
    try:
        full = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    for d in dirs:
        try:
            if os.path.commonpath([full, d]) == d:
                return True
        except Exception:
            continue
    return False


def main():
    global _QUIT_DONE
    base = find_dir()
    if not base:
        msg(TITLE, "找不到程序目录（缺 app.py），请把本程序放回：\n" + APP_DIR)
        return 1

    # 日志收拢：launcher 是 frozen 的（BASE=exe 目录），后端 app.py 是以源码
    # 身份跑的（BASE=源码目录）。不统一就会有两份同名 mihome.log，排查时
    # 时间线对不上；而且 onedir 升级整目录替换会把 launcher 那份覆盖掉。
    # 用 APP_DIR 而不是 config.BASE 比较：launcher 刻意不 import config
    # （它要能独立打包成窗口外壳），APP_DIR 就是它自己认定的根。
    if os.path.abspath(APP_DIR) != os.path.abspath(base):
        logx.redirect(os.path.join(base, "logs"))
    _boot_mark("find_dir")

    # ① 命名管道优先：只有本程序的活实例会在这个名字上应答。
    #    它带回真实结果，不用再"猜窗口是不是还活着"。
    reply, ipc_err = ipc_call(base, "show")
    if reply is not None and reply.get("ok"):
        if reply.get("state") == "starting":
            # 对方正在启动中（后端起来了，窗口还没建）。给它时间，
            # 别在这时候叠第二个实例上去 —— 用户双击两次就是这么炸的。
            for _ in range(4):
                time.sleep(1.5)
                again, _e = ipc_call(base, "show", timeout=3.0)
                if again and again.get("state") == "shown":
                    return 0
        return 0
    if reply is not None and reply.get("state") == "dead":
        # 有实例在听，但它的后端已经不通了：废实例。
        # 接管它（礼貌关 → 关不掉就强杀），否则它攥着互斥量，下面抢不到。
        log_err("single_instance", "已有实例在听但后端不通，按废实例处理，重新启动")
        _takeover_dead_instance(base)
    elif reply is None and ipc_err is None:
        # 没有实例在听。可能是全新启动，也可能是**老版本**实例占着
        # （老版本没有管道），或者对方正处在"已拉起、还没建管道"的启动中。
        pass
    else:
        # 管道本身出了问题（权限、超时）。不能因为 IPC 故障就不让用户启动，
        # 记一条，下面交给互斥量判断。
        log_err("single_instance", "管道异常，按互斥量判断：%s" % ipc_err)

    # ② 互斥量才是最终闸门：抢到才许启动。
    #    以前这里是 `already_running() and focus_existing()` —— 只要"没窗口"
    #    就返回 False，于是"对方正在启动"这种情况下判定为"没人在跑"，两个
    #    进程并行启动、抢同一个端口。现在改成：抢不到就等（最多 8 秒，够
    #    对方把窗口建出来或退出），等不到再考虑叫窗口。
    if not _mutex_acquire(wait=8.0):
        if focus_existing():
            _boot_mark("单实例判定(已有窗口)")
            return 0
        if port_open(PORT_WEB):
            # 后端在听 = 对方真的在启动/在跑，只是窗口还没出来。
            # 安静退出比叠第二个实例友好（用户马上就会看到那个窗口）。
            log_err("single_instance", "另一实例在运行（后端已就绪），本次不再启动")
            return 0
        # 既没有窗口也没有后端，互斥量却还被占着 —— 这是具认不出来的尸体
        # （进程卡死、被调试器挂住、句柄泄漏）。宁可冒一次重复启动的风险，
        # 也不能让用户从此彻底打不开程序（那才是真正的病）。
        _takeover_dead_instance(base)
        if not _mutex_acquire(wait=5.0):
            log_err("single_instance",
                    "互斥量被占但既无窗口也无后端，判定为死锁残留，强行继续启动")

    # 单实例判定已过，确定要真正启动了：让 GUI 栈（webview + CLR，约 250ms 的
    # 纯导入）在后台跟后端启动并行，别等后端就绪了才开始 import。
    _boot_mark("单实例判定")
    _preload_gui_stack()

    # 上一轮可能留下"窗口还在、后端已死"的僵尸实例，占着标题和互斥量。
    # 启动前先扫干净，否则新实例会被它挡在门外。
    kill_zombies()
    _boot_mark("kill_zombies")

    # 启动前先轮转过大的日志，避免长时间运行后磁盘被日志占满
    rotate_log(base, "start.log")
    rotate_log(os.path.join(base, "go2rtc"), "go2rtc.log")
    rotate_log(os.path.join(base, "charger"), "charger.log")
    _boot_mark("rotate_log")

    start_cam(base)
    _boot_mark("start_cam")

    ok, err = start_app(base)
    if not ok:
        # 打包版没有控制台，失败原因只能落盘 —— 不写日志的话，
        # 用户只看到"双击没反应"，排查完全无从下手
        log_err("start_app", err)
        msg(TITLE + " 启动失败", err)
        stop_children()
        # 启动失败要记账：连续失败 3 次就自动关掉开机自启，
        # 别让开机自启每次开机都拉起一个必然失败的程序。
        try:
            autostart = load_autostart(base)
            if autostart:
                autostart.note_start_failed(err)
                if autostart.fail_count() >= autostart.MAX_FAIL:
                    msg(TITLE + " 已关闭开机自启",
                        "连续 %d 次启动失败，已自动关闭开机自启。\n\n最后原因：\n%s\n\n"
                        "修好之后可以在侧栏重新打开。"
                        % (autostart.fail_count(), err))
        except Exception:
            pass
        return 1

    _boot_mark("start_app")

    # 开始接客：必须在 open_window 之前 —— open_window 会一直阻塞到
    # 窗口关闭（pywebview.start() 跑主循环），之后再建管道就永远轮不到。
    # 此时后端已经就绪，窗口可能还没建好，所以这段时间来的请求会收到
    # "starting"，对方会等而不是抢着起第二个实例。
    ipc_serve(base)
    _boot_mark("ipc_serve")

    try:
        open_window(base)
    except Exception as e:
        log_err("open_window", e)
        try:
            _tray.stop()
        except Exception:
            pass
        msg(TITLE + " 启动失败",
            "界面窗口打不开：\n%s\n\n可能是缺少 WebView2 运行时。" % e)
        stop_children()
        return 1

    _QUIT_DONE = True    # webview 主循环已退出，看门狗可以收工了
    _tray.stop()         # 撤托盘图标（窗口 ✕ 关闭时图标不会自己消失）
    stop_children()      # 关窗口 = 退出程序，后台服务一并收掉
    ipc_stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:
        log_err("main", e)
        raise
