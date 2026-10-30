"""本机硬件状态采集（CPU / GPU / 内存 / 磁盘 / 网络 / 温度 / 风扇）

为什么单独一个模块：
  米家那套是"云 + 蓝牙"的异步 I/O，本机状态是"每秒一次的系统调用"，
  两者节奏完全不同，塞在 app.py 里会把路由文件撑爆，也不方便单独测。

数据来源（都是只读、只在本机，不出网）：
  · psutil         CPU 占用 / 每核占用 / 频率 / 内存 / 磁盘 / 网卡吞吐 / 进程
  · nvidia-smi     NVIDIA 显卡利用率、温度、风扇百分比、显存、功耗、核心频率
  · LibreHardwareMonitor（可选）
                   装了并开着的话，能补上 CPU 封装温度和主板风扇转速；
                   没装就退化成"没有这一项"，绝不编数字。
  · ACPI 温区（BrimDeck 同款，零依赖兜底）
                   读 Windows 性能计数器 \\Thermal Zone Information(*)\\Temperature，
                   任何用户都能读、不需要管理员、不需要装任何东西。代价是
                   台式机主板的这个温区多半不跟 CPU 核心走（本机实测恒定
                   27.9℃），所以排在 Core Temp / LHM 之后只当兜底，前端会
                   标注来源（acpi = 主板温区）据此弱化显示。

采样在后台线程里跑，路由只读快照 —— 页面刷多快都不会把系统调用打爆。
"""

import collections
import os
import shutil
import subprocess
import threading
import time

try:
    import ctypes
    from ctypes import wintypes
except Exception:                                     # pragma: no cover
    ctypes = wintypes = None

try:
    import psutil
except Exception:                                     # pragma: no cover
    psutil = None

IS_WIN = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

_GUARD = threading.Lock()
_STARTED = [False]
_INTERVAL = 1.0            # 主循环节拍（秒）
_HIST_LEN = 120            # 曲线保留 120 个点 ≈ 2 分钟

# 每路曲线的环形缓冲：CPU 占用 / GPU 占用 / GPU 温度
_HIST = {
    "cpu": collections.deque(maxlen=_HIST_LEN),
    "gpu": collections.deque(maxlen=_HIST_LEN),
    "mem": collections.deque(maxlen=_HIST_LEN),
    "temp": collections.deque(maxlen=_HIST_LEN),
}

_STATE = {
    "ok": False,
    "ts": 0,
    "error": "",
    "cpu": {}, "gpu": None, "mem": {}, "disk": {"parts": []},
    "net": {}, "procs": {"n": 0, "top": []}, "host": {},
    "sources": {"temp": "none", "fan": "none"},
    "hist": {"cpu": [], "gpu": [], "mem": [], "temp": []},
}

_PREV = {"net": None, "disk": None, "at": 0.0}

# 慢速项各自的时间戳，避免每秒都跑一遍
_SLOW = {"gpu": 0.0, "lhm": 0.0, "ct": 0.0, "procs": 0.0, "parts": 0.0,
         "host": 0.0, "tz": 0.0}
_CACHE = {"gpu": None, "lhm": None, "ct": None, "procs": [], "parts": [],
          "host": {}, "tz": None}

_CPU_NAME = [""]
_MAX_FREQ = [0]


# ----------------------------------------------------------------- 静态信息
def _cpu_static():
    if _CPU_NAME[0]:
        return
    if not psutil:
        return
    try:
        _CPU_NAME[0] = _wmi_one("Win32_Processor", "Name") or "CPU"
        _MAX_FREQ[0] = int(_wmi_one("Win32_Processor", "MaxClockSpeed") or 0)
    except Exception:
        _CPU_NAME[0] = "CPU"
        _MAX_FREQ[0] = 0


def _wmi_one(cls, field):
    """单个字段的 WMI 查询（只在启动时用几次，慢一点无所谓）"""
    if not IS_WIN:
        return None
    try:
        import win32com.client
        c = win32com.client.GetObject("winmgmts:")
        for r in c.ExecQuery("SELECT %s FROM %s" % (field, cls)):
            return getattr(r, field, None)
    except Exception:
        pass
    return None


# ----------------------------------------------------------------- GPU
_NV_QUERY = ("name,temperature.gpu,fan.speed,utilization.gpu,"
             "memory.used,memory.total,power.draw,clocks.sm")


if IS_WIN:
    class _NVML_UTIL(ctypes.Structure):
        """nvmlUtilization_t"""
        _fields_ = [("gpu", wintypes.UINT), ("mem", wintypes.UINT)]

    class _NVML_MEM(ctypes.Structure):
        """nvmlMemory_t（单位字节）"""
        _fields_ = [("total", ctypes.c_ulonglong),
                    ("free", ctypes.c_ulonglong),
                    ("used", ctypes.c_ulonglong)]
else:                                                    # pragma: no cover
    _NVML_UTIL = _NVML_MEM = None

# nvidia-smi 每次查询都要起一个子进程（实测 53ms）。机器上装了 NVIDIA 卡
# 就有 nvml.dll —— nvidia-smi 自己也是调它。常驻在进程里读一次只要 0.12ms，
# 快 440 倍，也不用在任务管理器里一闪一闪地创建进程。
# 没有 nvml.dll（A 卡 / 没装驱动）时退回 nvidia-smi 那条老路。
_NVML = {"ok": None, "dll": None, "dev": None}


def _nvml_sigs(dll, pairs):
    for name, args in pairs:
        try:
            f = getattr(dll, name)
        except AttributeError:
            return False
        f.argtypes = args
        f.restype = ctypes.c_int
    return True


def _nvml_open():
    """初始化 NVML，成功返回 True。失败一次就记下来不再试。"""
    if not IS_WIN or ctypes is None:
        _NVML["ok"] = False
        return False
    paths = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "nvml.dll"),
             r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"]
    dll = None
    for p in paths:
        if os.path.exists(p):
            try:
                dll = ctypes.CDLL(p)
                break
            except Exception:
                continue
    if dll is None:
        _NVML["ok"] = False
        return False

    ok = _nvml_sigs(dll, [
        ("nvmlInit_v2", []),
        ("nvmlDeviceGetCount_v2", [ctypes.POINTER(wintypes.UINT)]),
        ("nvmlDeviceGetHandleByIndex_v2",
         [wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)]),
        ("nvmlDeviceGetUtilizationRates",
         [ctypes.c_void_p, ctypes.POINTER(_NVML_UTIL)]),
        ("nvmlDeviceGetTemperature",
         [ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(wintypes.UINT)]),
        ("nvmlDeviceGetMemoryInfo",
         [ctypes.c_void_p, ctypes.POINTER(_NVML_MEM)]),
        ("nvmlDeviceGetPowerUsage",
         [ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)]),
        ("nvmlDeviceGetClockInfo",
         [ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(wintypes.UINT)]),
        ("nvmlDeviceGetFanSpeed",
         [ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)]),
        ("nvmlDeviceGetName", [ctypes.c_void_p, ctypes.c_char_p, wintypes.UINT]),
    ])
    if not ok or dll.nvmlInit_v2() != 0:
        _NVML["ok"] = False
        return False
    cnt = wintypes.UINT()
    if dll.nvmlDeviceGetCount_v2(ctypes.byref(cnt)) != 0 or cnt.value < 1:
        _NVML["ok"] = False
        return False
    dev = ctypes.c_void_p()
    if dll.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev)) != 0:
        _NVML["ok"] = False
        return False
    _NVML.update(dll=dll, dev=dev, ok=True)
    return True


def _nvml_read():
    """读一次 GPU 状态，字段和 _nvidia() 完全一致。不可用返回 None。"""
    if _NVML["ok"] is None and not _nvml_open():
        return None
    if not _NVML["ok"]:
        return None
    dll, dev = _NVML["dll"], _NVML["dev"]
    try:
        u, t, m = _NVML_UTIL(), wintypes.UINT(), _NVML_MEM()
        pw, clk, fan = wintypes.UINT(), wintypes.UINT(), wintypes.UINT()
        nm = ctypes.create_string_buffer(96)
        if dll.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u)) != 0:
            return None
        dll.nvmlDeviceGetTemperature(dev, 0, ctypes.byref(t))
        dll.nvmlDeviceGetMemoryInfo(dev, ctypes.byref(m))
        dll.nvmlDeviceGetPowerUsage(dev, ctypes.byref(pw))
        dll.nvmlDeviceGetClockInfo(dev, 1, ctypes.byref(clk))     # 1 = SM 时钟
        dll.nvmlDeviceGetFanSpeed(dev, ctypes.byref(fan))
        dll.nvmlDeviceGetName(dev, nm, 96)
        return {
            "name": nm.value.decode("utf-8", "replace") or "NVIDIA GPU",
            "temp": float(t.value) if t.value else None,
            "fan": float(fan.value),                  # 百分比
            "util": float(u.gpu),
            "mem_used": float(m.used // 1048576),     # MB
            "mem_total": float(m.total // 1048576),   # MB
            "power": round(pw.value / 1000.0, 2),     # mW → W
            "clock": float(clk.value),                # MHz
        }
    except Exception:
        _NVML["ok"] = False
        return None


def _nvidia():
    got = _nvml_read()
    if got:
        return got
    exe = shutil.which("nvidia-smi")
    if not exe and IS_WIN:
        cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "nvidia-smi.exe")
        exe = cand if os.path.exists(cand) else None
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=" + _NV_QUERY, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=_CREATE_NO_WINDOW,
        )
        line = (out.stdout or "").strip().splitlines()
        if not line:
            return None
        f = [x.strip() for x in line[0].split(",")]

        def num(v):
            try:
                return float(v)
            except Exception:
                return None

        return {
            "name": f[0],
            "temp": num(f[1]),
            "fan": num(f[2]),          # 百分比
            "util": num(f[3]),
            "mem_used": num(f[4]),     # MB
            "mem_total": num(f[5]),    # MB
            "power": num(f[6]),        # W
            "clock": num(f[7]),        # MHz
        }
    except Exception:
        return None


# ------------------------------------------------- Core Temp 共享内存（可选）
# Core Temp 从官网或各种绿色工具包都能拿到。Windows 用户态根本读不到
# CPU 的 DTS 温度 —— 必须有个带内核驱动的程序去读，这活只能交给 Core Temp /
# HWiNFO / LHM 这类工具。Core Temp 的共享内存是它官方 SDK 里公开的结构体，
# 我们把同一块内存映射进来读，就拿到了每核温度、功耗和 TjMax。
# 它没在跑时 OpenFileMapping 会失败，返回 None，不报错、不占 CPU。
_CT_NAMES = ("CoreTempMappingObjectEx", "CoreTempMappingObject")


def _coretemp():
    if not IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes as wt
    except Exception:
        return None

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = wt.HANDLE
    k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
    k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wt.HANDLE]

    for name in _CT_NAMES:
        h = k32.OpenFileMappingW(0x0004, False, name)      # FILE_MAP_READ
        if not h:
            continue
        ptr = k32.MapViewOfFile(h, 0x0004, 0, 0, 4000)
        if not ptr:
            k32.CloseHandle(h)
            continue
        try:
            # 结构体（CoreTempSharedDataEx）：uiLoad[256] uiTjMax[128] uiCoreCnt
            # uiCPUCnt fTemp[256] fVID fCPUSpeed fFSBSpeed fMultiplier sCPUName[100]
            # ucFahrenheit ucDeltaToTjMax ucTDPSupported ucPowerSupported
            # uiStructVersion fPower fTDP ...
            core_cnt = ctypes.c_uint.from_address(ptr + 1536).value
            cpu_cnt = ctypes.c_uint.from_address(ptr + 1540).value
            if not (0 < core_cnt <= 256):
                core_cnt = min(max(cpu_cnt, 1), 64)
            temps = []
            for i in range(core_cnt):
                t = ctypes.c_float.from_address(ptr + 1544 + i * 4).value
                if -40 < t < 150:
                    temps.append(round(t, 1))
            name_raw = ctypes.string_at(ptr + 2584, 100)
            power = ctypes.c_float.from_address(ptr + 2692).value
            fahrenheit = ctypes.c_ubyte.from_address(ptr + 2684).value
            # 2686=ucTDPSupported 2687=ucPowerSupported，功耗看后者
            power_ok = ctypes.c_ubyte.from_address(ptr + 2687).value
            if fahrenheit:
                temps = [round((t - 32) * 5 / 9, 1) for t in temps]
            if not temps:
                continue
            return {
                "name": name_raw.split(b"\x00")[0].decode("utf-8", "ignore").strip(),
                "cores": temps,
                "pkg": max(temps),
                "power": round(power, 1) if (power_ok and 0 < power < 400) else None,
            }
        finally:
            k32.UnmapViewOfFile(ptr)
            k32.CloseHandle(h)
    return None


# 认一下机器上有没有现成的传感器程序（只用来给前端一句提示，不启动它们）
_TOOLS = [(
    "Core Temp",
    [
        r"C:\Program Files\Core Temp\Core Temp.exe",
        r"C:\Program Files (x86)\Core Temp\Core Temp.exe",
        # 绿色版/工具包解压目录里的 Core Temp 也认：把完整 exe 路径加进来即可
    ],
), (
    "HWiNFO",
    [
        r"C:\Program Files\HWiNFO64\HWiNFO64.exe",
        r"C:\Program Files (x86)\HWiNFO64\HWiNFO64.exe",
    ],
), (
    "LibreHardwareMonitor",
    [
        r"C:\Program Files\LibreHardwareMonitor\LibreHardwareMonitor.exe",
        r"C:\Program Files\OpenHardwareMonitor\OpenHardwareMonitor.exe",
    ],
)]
_FOUND = {"at": 0.0, "list": []}


def _probe_tools():
    if time.time() - _FOUND["at"] < 300:
        return _FOUND["list"]
    _FOUND["at"] = time.time()
    found = []
    for label, paths in _TOOLS:
        for p in paths:
            if os.path.exists(p):
                found.append({"name": label, "path": p})
                break
    _FOUND["list"] = found
    return found


# ------------------------------------------------- LibreHardwareMonitor（可选）
# 连续探不到就退避：用户没装 LHM 是最常见的情况，不该每 15 秒都去敲一次门。
# 退避到 5 分钟，装好并开了 Web Server 之后最多等 5 分钟就能自己恢复，
# 不用重启程序。
_LHM_MISS = [0]
_LHM_NEXT = [0.0]
_LHM_PORT = 8085


def _miss():
    """记一次探测失败并安排下次重试的时间。"""
    _LHM_MISS[0] += 1
    # 头两次算偶发（LHM 可能正在启动），三次以上按"这台机器没装"处理
    _LHM_NEXT[0] = time.time() + (300.0 if _LHM_MISS[0] >= 3 else 20.0)


def _lhm_http():
    """LHM 的 Web Server（http://127.0.0.1:8085/data.json）。

    为什么加这条：LHM 从 0.9 之后**不再暴露 WMI 命名空间**（实测
    `root/LibreHardwareMonitor` 直接报"无效命名空间"），所以原来那套
    win32com 查询在新版上永远拿不到 CPU 温度。它自带一个只读的 Web 接口，
    吐一棵传感器树，比 WMI 稳、也不受版本影响。

    为什么先探端口再发请求：原来直接 urlopen + 2.5 秒超时，机器上没开
    Web Server 时（最常见的情况）会**实打实等满 2.08 秒**才失败 —— 每
    15 秒白等一次，启动时那 3 秒也主要是它造成的。先对本机端口做一次
    0.12 秒的 connect，没在监听就立刻返回，代价是一毫秒。
    """
    if not IS_WIN:
        return None
    now = time.time()
    if now < _LHM_NEXT[0]:
        return None
    try:
        import socket
        s = socket.socket()
        s.settimeout(0.12)
        alive = s.connect_ex(("127.0.0.1", _LHM_PORT)) == 0
        s.close()
    except Exception:
        alive = False
    if not alive:
        _miss()
        return None
    try:
        import json
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/data.json" % _LHM_PORT,
                                    timeout=2.5) as r:
            tree = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        _miss()
        return None

    def num(s):
        try:
            return float(str(s).split()[0])
        except Exception:
            return None

    cands, fans = [], []

    def walk(node):
        try:
            txt = node.get("Text") or ""
            typ = node.get("Type") or ""
            sid = node.get("SensorId") or ""
            v = num(node.get("Value"))
            if typ == "Temperature" and v is not None and "cpu" in sid.lower():
                cands.append((0 if "Package" in txt else 1, v))
            elif typ == "Fan" and v is not None:
                fans.append({"name": txt, "rpm": v})
            for ch in node.get("Children") or []:
                walk(ch)
        except Exception:
            pass

    walk(tree)
    cpu_t = min(cands)[1] if cands else None      # 优先 "CPU Package"
    _LHM_MISS[0] = 0                               # 答上了，退避清零
    _LHM_NEXT[0] = 0.0
    if cpu_t is None and not fans:
        return None
    return cpu_t, fans


def _lhm():
    """装了 LibreHardwareMonitor / OpenHardwareMonitor 就能拿到 CPU 封装温度和主板风扇。

    返回 (cpu_temp, [风扇 dict]) ；拿不到就 None。这个函数每 15 秒才试一次。
    先试 Web Server（新版 LHM 只有这个），再退回 WMI（老版 LHM / OHM）。
    """
    got = _lhm_http()
    if got:
        return got
    if not IS_WIN:
        return None
    try:
        import win32com.client
    except Exception:
        return None
    for ns in ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor"):
        try:
            c = win32com.client.GetObject(
                "winmgmts:{impersonationLevel=impersonate}!//./" + ns)
            rows = c.ExecQuery("SELECT Name, SensorType, Value FROM Sensor")
        except Exception:
            continue
        cpu_t = None
        fans = []
        try:
            for r in rows:
                t = getattr(r, "SensorType", "")
                nm = getattr(r, "Name", "") or ""
                v = getattr(r, "Value", None)
                if v is None:
                    continue
                if t == "Temperature" and cpu_t is None and (
                        "Package" in nm or "CPU" in nm.upper()):
                    cpu_t = float(v)
                elif t == "Fan" or t == "Control":
                    if t == "Fan":
                        fans.append({"name": nm, "rpm": float(v)})
        except Exception:
            pass
        if cpu_t is not None or fans:
            return cpu_t, fans
    return None


# ------------------------------------------------- ACPI 温区（BrimDeck 同款兜底）
# BrimDeck 的 CPU 温度就是它：Windows 性能计数器
#   \Thermal Zone Information(*)\Temperature
#   \Thermal Zone Information(*)\High Precision Temperature
# 三个卖点：
#   · 任何用户都能读 —— 不需要管理员，不弹 UAC，不装任何东西；
#   · 单次读取微秒级，比 LHM 的 Web 接口便宜三个数量级；
#   · 计数器名字走 PdhAddEnglishCounterW 解析，中文系统上照样能用英文名。
# 单位从本机实测反推（T=301 与 HP=3010 同读数）：
#   Temperature 是开尔文（K），High Precision 是 0.1 开尔文 —— 减 273.15 即 ℃。
# 短板也明说：这是 ACPI 温区（主板/机箱传感器），台式机上常不随 CPU 核心走，
# 所以只当兜底，temp_src 会标成 "acpi" 让前端弱化。
if IS_WIN:
    class _PDH_CV_D(ctypes.Union):
        """PDH_FMT_COUNTERVALUE 的值联合体"""
        _fields_ = [("doubleValue", ctypes.c_double),
                    ("longValue", wintypes.LONG),
                    ("largeValue", ctypes.c_longlong)]

    class _PDH_CV(ctypes.Structure):
        """PDH_FMT_COUNTERVALUE：DWORD CStatus + 值联合体（按 C 自然对齐）"""
        _fields_ = [("CStatus", wintypes.DWORD), ("u", _PDH_CV_D)]

    class _PDH_ITEM_W(ctypes.Structure):
        """PDH_FMT_COUNTERVALUE_ITEM_W"""
        _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _PDH_CV)]
else:                                                    # pragma: no cover
    _PDH_CV_D = _PDH_CV = _PDH_ITEM_W = None

_TZ_FMT_DOUBLE = 0x00000200          # PDH_FMT_DOUBLE
_TZ_RETRY = 60.0                     # 探测失败后的退避（这类机器压根没有温区）
_TZ = {"q": None, "hp": None, "tk": None, "fail": 0.0, "pdh": None}


def _tz_open():
    """建一次 PDH 查询，把两个温区计数器都挂上。失败返回 False（退避后重试）。"""
    try:
        pdh = ctypes.WinDLL("pdh", use_last_error=True)
        pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID,
                                      ctypes.POINTER(wintypes.HANDLE)]
        pdh.PdhOpenQueryW.restype = wintypes.LONG
        pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR,
                                              wintypes.LPVOID,
                                              ctypes.POINTER(wintypes.HANDLE)]
        pdh.PdhAddEnglishCounterW.restype = wintypes.LONG
        pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        pdh.PdhCollectQueryData.restype = wintypes.LONG
        # 注意实参顺序（实测反推，与老文档不同）：缓冲区指针在**最后**，
        # 前面是 (句柄, 格式, &所需字节数, &实例数)。给错顺序报 0xC0000BBD。
        # 缓冲区参数用 c_void_p 收：调用方传的是 create_string_buffer 的字节数组
        pdh.PdhGetFormattedCounterArrayW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p]
        pdh.PdhGetFormattedCounterArrayW.restype = wintypes.LONG
    except Exception:
        return False
    q = wintypes.HANDLE()
    if pdh.PdhOpenQueryW(None, None, ctypes.byref(q)) != 0:
        return False
    hp, tk = wintypes.HANDLE(), wintypes.HANDLE()
    ok = True
    # 带通配符的路径一次挂上全部温区实例，取值用 PdhGetFormattedCounterArrayW
    if pdh.PdhAddEnglishCounterW(
            q, r"\Thermal Zone Information(*)\High Precision Temperature",
            None, ctypes.byref(hp)) != 0:
        ok = False
    if pdh.PdhAddEnglishCounterW(
            q, r"\Thermal Zone Information(*)\Temperature",
            None, ctypes.byref(tk)) != 0:
        if not ok:                       # 两个都挂不上，查询作废
            pdh_close = getattr(pdh, "PdhCloseQuery", None)
            if pdh_close:
                pdh_close(q)
            return False
        tk.value = None
    _TZ.update(pdh=pdh, q=q, hp=hp.value or None, tk=tk.value or None)
    return True


def _tz_values(h, scale):
    """读一个（可能带通配符的）计数器句柄 → [℃]。没有有效值返回 None。"""
    if not h:
        return None
    pdh = _TZ["pdh"]
    need, cnt = wintypes.DWORD(0), wintypes.DWORD(0)
    # 第一趟只要大小：不拦返回码，看 need 是否被填上（有些版本首趟不给 0）
    pdh.PdhGetFormattedCounterArrayW(
        h, _TZ_FMT_DOUBLE, ctypes.byref(need), ctypes.byref(cnt), None)
    if need.value == 0:
        return None
    buf = ctypes.create_string_buffer(need.value)
    rc = pdh.PdhGetFormattedCounterArrayW(
        h, _TZ_FMT_DOUBLE, ctypes.byref(need), ctypes.byref(cnt), buf)
    if rc != 0 or cnt.value == 0:
        return None
    out = []
    items = ctypes.cast(buf, ctypes.POINTER(_PDH_ITEM_W))
    for i in range(cnt.value):
        it = items[i]
        if it.FmtValue.CStatus == 0:
            c = it.FmtValue.u.doubleValue * scale - 273.15
            if -10.0 < c < 150.0:        # 温度合理性（温区坏值直接丢）
                out.append(round(c, 1))
    return out or None


def _tz():
    """ACPI 温区兜底。返回 {"temp": ℃, "zones": n} 或 None。

    出错不抛异常：标一个退避时间，60 秒后才再试 —— 没有温区的机器
    （一部分主板/虚拟机）不值得每 5 秒敲一次门。
    """
    if not IS_WIN or ctypes is None:
        return None
    now = time.time()
    if now < _TZ["fail"]:
        return None
    try:
        if _TZ["q"] is None and not _tz_open():
            _TZ["fail"] = now + _TZ_RETRY
            return None
        pdh, q = _TZ["pdh"], _TZ["q"]
        if pdh.PdhCollectQueryData(q) != 0:
            raise OSError("PdhCollectQueryData")
        vals = _tz_values(_TZ["hp"], 0.1) or _tz_values(_TZ["tk"], 1.0)
        if not vals:
            return None
        # 多个温区取最热的那个 —— 它最可能是离 CPU 最近的传感器
        return {"temp": max(vals), "zones": len(vals)}
    except Exception:
        _TZ["fail"] = now + _TZ_RETRY
        return None


# ----------------------------------------------------------------- 主采样
def _boot_host():
    if not psutil:
        return {}
    try:
        boot = psutil.boot_time()
        return {"boot": boot, "uptime": max(0, int(time.time() - boot))}
    except Exception:
        return {}


def _partitions():
    if not psutil:
        return []
    sysdrive = (os.environ.get("SystemDrive") or "C:").upper()
    out = []
    try:
        for p in psutil.disk_partitions(all=False):
            if "cdrom" in (p.opts or "") or not p.fstype:
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            mnt = p.mountpoint.rstrip("\\") or p.mountpoint
            out.append({
                "mnt": mnt,
                "sys": mnt.upper() == sysdrive,
                "pct": round(u.percent, 1),
                "used": u.used, "free": u.free, "total": u.total,
            })
    except Exception:
        pass
    out.sort(key=lambda x: -x["total"])
    return out[:4]


# ------------------------------------------------------------ 进程全量快照
#
# 为什么不用 psutil 逐个问：
#   它取一个进程的 CPU 时间和内存是"一次 OpenProcess、一次系统调用"，
#   264 个进程跑一轮要 296ms（实测）。而 _top_procs 原来每 3 秒跑一次，
#   等于常年吃掉约 10% 的单核 —— 更要命的是这 296ms 全程握着 GIL，
#   Flask 的请求线程只能排队。实测 /api/status 本身几乎不干活，中位数
#   0.9ms，但 P99 被顶到 149ms。用户感受到的就是"偶尔顿一下"。
#
# 换成任务管理器用的那套：
#   NtQuerySystemInformation(SystemProcessInformation) 一次调用返回
#   全量链表，进程名、用户态/核心态时间、工作集全在里面。实测 1.4ms，
#   快了 100 倍（263 个进程逐一比对，数值零误差）。
#   附带好处：不走 OpenProcess，系统进程也能读到，不会因为拒绝访问丢行。
#
# 结构体只定义到 WorkingSetSize 为止 —— 后面的字段随 Windows 版本变过，
# 但这一段的布局从 Win7 到 Win11 没变。万一哪天真变了，_native_procs()
# 会返回 None，调用方自动退回 psutil 逐进程那条老路（有测试覆盖）。

_PROC_SNAPSHOT = {"ok": None, "buf": None, "size": 0}


def _native_procs():
    """一次系统调用拿全部进程：[{pid, name, cpu_sum(秒), ws(字节)}]

    不支持（非 Windows / 结构对不上 / 被拦）时返回 None，不要抛。
    返回的 cpu_sum 是**累计** CPU 时间，占用率由调用方用两次差值算。
    """
    if not IS_WIN:
        return None
    st = _PROC_SNAPSHOT
    if st["ok"] is False:          # 已知不可用，别再试了
        return None

    try:
        ntdll = ctypes.WinDLL("ntdll")
        fn = ntdll.NtQuerySystemInformation
        fn.argtypes = [wintypes.ULONG, ctypes.c_void_p,
                       wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        fn.restype = ctypes.c_long
    except Exception:
        st["ok"] = False
        return None

    size = st["size"] or (1 << 20)
    buf = st["buf"]
    need = wintypes.ULONG(0)
    for _ in range(6):
        if buf is None or len(buf) < size:
            buf = ctypes.create_string_buffer(size)
        status = fn(5, buf, size, ctypes.byref(need))   # 5 = SystemProcessInformation
        if status == 0:
            break
        if status == 0xC0000004:                        # STATUS_INFO_LENGTH_MISMATCH
            size = max(size * 2, need.value + 65536)
            continue
        st["ok"] = False
        return None
    else:
        st["ok"] = False
        return None

    # 缓冲区留着复用：每次新建 1MB 只会给 GC 添堵
    st["buf"], st["size"], st["ok"] = buf, size, True

    out = []
    base = ctypes.addressof(buf)
    off = 0
    try:
        while True:
            spi = _SPI.from_address(base + off)
            name = ""
            if spi.ImageName.Length and spi.ImageName.Buffer:
                name = ctypes.wstring_at(spi.ImageName.Buffer,
                                         spi.ImageName.Length // 2)
            out.append({
                "pid": spi.UniqueProcessId or 0,
                "name": name,
                # 100ns 计时单位 → 秒
                "cpu_sum": (spi.UserTime + spi.KernelTime) / 1e7,
                "ws": spi.WorkingSetSize,
            })
            if not spi.NextEntryOffset:
                break
            off += spi.NextEntryOffset
    except Exception:
        st["ok"] = False
        return None
    return out


# 进程快照用到的两个结构体。放在模块级只定义一次。
if IS_WIN:
    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT),
                    ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_void_p)]

    class _SPI(ctypes.Structure):
        """SYSTEM_PROCESS_INFORMATION 的前半段（到 WorkingSetSize 为止）"""
        _fields_ = [
            ("NextEntryOffset", wintypes.ULONG),
            ("NumberOfThreads", wintypes.ULONG),
            ("WorkingSetPrivateSize", ctypes.c_longlong),
            ("HardFaultCount", wintypes.ULONG),
            ("NumberOfThreadsHighWatermark", wintypes.ULONG),
            ("CycleTime", ctypes.c_ulonglong),
            ("CreateTime", ctypes.c_longlong),
            ("UserTime", ctypes.c_longlong),
            ("KernelTime", ctypes.c_longlong),
            ("ImageName", _UNICODE_STRING),
            ("BasePriority", ctypes.c_long),
            ("UniqueProcessId", ctypes.c_void_p),
            ("InheritedFromUniqueProcessId", ctypes.c_void_p),
            ("HandleCount", wintypes.ULONG),
            ("SessionId", wintypes.ULONG),
            ("UniqueProcessKey", ctypes.c_void_p),
            ("PeakVirtualSize", ctypes.c_size_t),
            ("VirtualSize", ctypes.c_size_t),
            ("PageFaultCount", wintypes.ULONG),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
        ]
else:                                                    # pragma: no cover
    _UNICODE_STRING = _SPI = None


_PROCS = {}
_PROC_PREV = {}          # pid -> (累计 CPU 秒, 采样时刻)，算占用率用
_SYS_MEM_TOTAL = [0]


def _top_procs_fallback():
    """psutil 逐进程的老路子，只给 _native_procs() 不可用时兜底。"""
    n = psutil.cpu_count() or 1
    rows = []
    alive = set()
    for p in psutil.process_iter():
        try:
            alive.add(p.pid)
            obj = _PROCS.get(p.pid)
            if obj is None:
                obj = _PROCS[p.pid] = p
                obj.cpu_percent(None)          # 先踩一脚建立基线
            cpu = obj.cpu_percent(None)
            nm = p.name() or "?"
            if nm in ("System Idle Process", "Idle") or nm.startswith("系统空闲"):
                continue
            rows.append({
                "pid": p.pid,
                "name": p.name() or "?",
                "cpu": round((cpu or 0) / n, 1),
                "mem": round(p.memory_percent(), 1),
            })
        except Exception:
            continue
    for pid in list(_PROCS):
        if pid not in alive:
            _PROCS.pop(pid, None)
    return rows


def _rows_native():
    """从原生快照算占用率。拿不到快照就返回 None（交给 fallback）。"""
    snap = _native_procs()
    if not snap:
        return None

    if psutil:
        n = psutil.cpu_count() or 1
    else:
        n = os.cpu_count() or 1
    now = time.time()
    if not _SYS_MEM_TOTAL[0]:
        try:
            _SYS_MEM_TOTAL[0] = psutil.virtual_memory().total if psutil else 0
        except Exception:
            _SYS_MEM_TOTAL[0] = 0
    total_mem = _SYS_MEM_TOTAL[0] or 1

    prev = _PROC_PREV
    rows = []
    cur = {}
    for r in snap:
        pid = r["pid"]
        nm = r["name"] or "?"
        # 0 号是 System Idle Process；它吃掉的是"空闲"时间，
        # 算进"谁在占 CPU"只会让榜首都变成 idle，没有意义
        if not pid or nm in ("System Idle Process", "Idle") \
                or nm.startswith("系统空闲"):
            continue
        cur[pid] = (r["cpu_sum"], now)
        old = prev.get(pid)
        if old:
            # 两次采样之间累计时间涨了多少秒 / 真实经过多少秒 / 几个核
            span = now - old[1]
            if span > 0.05:
                cpu = (r["cpu_sum"] - old[0]) / span / n * 100
            else:
                cpu = 0.0
        else:
            cpu = 0.0          # 新进程这一拍没有基线，下一拍才有
        if cpu < 0:
            cpu = 0.0
        rows.append({
            "pid": pid,
            "name": nm,
            "cpu": round(min(cpu, 100.0 * n), 1),
            "mem": round(r["ws"] / total_mem * 100, 1),
        })
    _PROC_PREV.clear()
    _PROC_PREV.update(cur)
    return rows


def _merge_rows(rows):
    """同名进程合并：浏览器/编辑器动辄开十几个子进程，一个个列出来只是噪音，
    合成一行反而更接近"这个东西一共吃了多少" """
    agg = {}
    for r in rows:
        key = r["name"].lower()
        if key in agg:
            agg[key]["cpu"] = round(agg[key]["cpu"] + r["cpu"], 1)
            agg[key]["mem"] = round(agg[key]["mem"] + r["mem"], 1)
            agg[key]["n"] += 1
        else:
            agg[key] = {"pid": r["pid"], "name": r["name"], "cpu": r["cpu"],
                        "mem": r["mem"], "n": 1}
    merged = sorted(agg.values(), key=lambda x: (-x["cpu"], -x["mem"]))
    return merged[:4]


def _top_procs():
    """占用前 4 名。优先走原生全量快照（1.4ms），失败才退回 psutil（296ms）。"""
    if not psutil and not IS_WIN:
        return []
    cold = not _PROC_PREV              # 还没有上一拍，算不出占用率
    rows = _rows_native()
    if rows is None:
        rows = _top_procs_fallback()
    elif cold:
        # 头一拍全是 0（差值需要前一次样本）。把节流拨回去，让下一拍
        # （1 秒后）立刻再采一次 —— 否则用户要等两个 5 秒才看到真数字。
        _SLOW["procs"] = time.time() - 4.0
    return _merge_rows(rows)


def _tick():
    now = time.time()
    dt = max(0.2, now - (_PREV["at"] or now))
    _PREV["at"] = now

    snap = {
        "ok": True, "ts": round(now, 2), "error": "",
        "sources": {"temp": "none", "fan": "none"},
    }
    _cpu_static()

    # ---- CPU ----
    pct = 0.0
    per = []
    if psutil:
        try:
            pct = psutil.cpu_percent(None)
            per = [round(x, 1) for x in psutil.cpu_percent(None, percpu=True)]
        except Exception:
            pass
        try:
            fr = psutil.cpu_freq()
            cur = int(fr.current) if fr else 0
        except Exception:
            cur = 0
        c = {"pct": round(pct, 1), "per": per, "freq": cur,
             "max_freq": _MAX_FREQ[0], "name": _CPU_NAME[0] or "CPU",
             "cores": psutil.cpu_count(logical=False) or 0,
             "threads": psutil.cpu_count() or 0,
             "temp": None, "temp_src": ""}
    else:
        c = {"pct": 0, "per": [], "freq": 0, "max_freq": 0, "name": "CPU",
             "cores": 0, "threads": 0, "temp": None, "temp_src": ""}

    # ---- GPU（2 秒一次）----
    # 有 nvml.dll 时一次只要 0.12ms，2 秒一轮的成本可以忽略；
    # 没装 NVIDIA 驱动时这里会退回 nvidia-smi 子进程（53ms），
    # _nvml_open() 失败后不再重试，所以最坏情况也不会每拍都起进程。
    if now - _SLOW["gpu"] > 2.0:
        _SLOW["gpu"] = now
        _CACHE["gpu"] = _nvidia()
    snap["gpu"] = _CACHE["gpu"]

    # ---- 温度 / 风扇（15 秒一次，只在有 LHM 时才有 CPU 温度）----
    if now - _SLOW["lhm"] > 15.0:
        _SLOW["lhm"] = now
        _CACHE["lhm"] = _lhm()
    lhm = _CACHE["lhm"]
    # ---- Core Temp 共享内存（它刚启动/刚退出时状态会跳，所以 5 秒探一次）----
    if now - _SLOW["ct"] > 5.0:
        _SLOW["ct"] = now
        _CACHE["ct"] = _coretemp()
    ct = _CACHE["ct"]
    if ct and ct.get("cores"):
        c["per_temp"] = ct["cores"]
        c["temp"] = ct["pkg"]
        c["temp_src"] = "coretemp"
        c["power"] = ct.get("power")
        snap["sources"]["temp"] = "coretemp"
    if lhm:
        if lhm[0] is not None:
            c["temp"] = round(lhm[0], 1)
            c["temp_src"] = "lhm"
            snap["sources"]["temp"] = "lhm"
        if lhm[1]:
            snap["fans"] = lhm[1]
            snap["sources"]["fan"] = "lhm"
    # ---- ACPI 温区兜底（BrimDeck 同款，免管理员零依赖）----
    # 只有前面两路都没给出 CPU 温度才轮到它：真温度永远优先。
    if snap["sources"]["temp"] == "none":
        if now - _SLOW["tz"] > 5.0:
            _SLOW["tz"] = now
            _CACHE["tz"] = _tz()
        tz = _CACHE["tz"]
        if tz and tz.get("temp") is not None:
            c["temp"] = tz["temp"]
            c["temp_src"] = "acpi"
            snap["sources"]["temp"] = "acpi"
            snap["temp_note"] = ("主板 ACPI 温区（%d 个），"
                                 "台式机上未必随 CPU 核心变化"
                                 % tz.get("zones", 1))
    if snap["gpu"] and snap["gpu"].get("temp") is not None:
        if snap["sources"]["temp"] == "none":
            snap["sources"]["temp"] = "gpu"
    snap["cpu"] = c
    # 没有真 CPU 温度时，看机器上有没有能补上的现成工具，给前端一句人话提示
    # （acpi 兜底时也带上：解释气泡要用它告诉用户怎么看到每核真温）
    if c["temp"] is None or c.get("temp_src") == "acpi":
        snap["temp_tools"] = _probe_tools()

    # ---- 内存 ----
    if psutil:
        try:
            m = psutil.virtual_memory()
            s = psutil.swap_memory()
            snap["mem"] = {"total": m.total, "used": m.used, "avail": m.available,
                           "pct": round(m.percent, 1), "swap": round(s.percent, 1)}
        except Exception:
            snap["mem"] = {}

    # ---- 磁盘吞吐（读写速率）----
    if psutil:
        try:
            io = psutil.disk_io_counters()
            if io and _PREV["disk"]:
                snap["disk_io"] = {
                    "read": round(max(0, io.read_bytes - _PREV["disk"].read_bytes) / dt, 0),
                    "write": round(max(0, io.write_bytes - _PREV["disk"].write_bytes) / dt, 0),
                }
            _PREV["disk"] = io
        except Exception:
            pass

    # ---- 网络吞吐 ----
    if psutil:
        try:
            ni = psutil.net_io_counters()
            if ni and _PREV["net"]:
                snap["net"] = {
                    "up": round(max(0, ni.bytes_sent - _PREV["net"].bytes_sent) / dt, 0),
                    "down": round(max(0, ni.bytes_recv - _PREV["net"].bytes_recv) / dt, 0),
                }
            _PREV["net"] = ni
        except Exception:
            pass

    # ---- 慢速：磁盘容量 30 秒、进程 5 秒、开机时长 60 秒 ----
    if now - _SLOW["parts"] > 30.0:
        _SLOW["parts"] = now
        _CACHE["parts"] = _partitions()
    snap["disk"] = {"parts": _CACHE["parts"]}
    snap["disk"].update(snap.pop("disk_io", {}))

    # 进程榜原来 3 秒一轮。换成原生快照后单次只要 1.7ms，但这是"谁在占
    # 资源"的概览，没人盯着 3 秒刷一次 —— 5 秒够用，也少一次抢 GIL 的
    # 机会（前端那个完整进程面板走 procman，是另一条按需路径）。
    if now - _SLOW["procs"] > 5.0:
        _SLOW["procs"] = now
        _CACHE["procs"] = _top_procs()
    snap["procs"] = {
        "n": len(psutil.pids()) if psutil else 0,
        "top": _CACHE["procs"],
    }

    if now - _SLOW["host"] > 60.0:
        _SLOW["host"] = now
        _CACHE["host"] = _boot_host()
    snap["host"] = dict(_CACHE["host"])

    # ---- 曲线缓冲 ----
    _HIST["cpu"].append(c["pct"])
    _HIST["gpu"].append(snap["gpu"]["util"] if snap["gpu"] and snap["gpu"].get("util") is not None else 0.0)
    _HIST["mem"].append(snap["mem"].get("pct") if snap.get("mem") else None)
    _HIST["temp"].append(snap["gpu"]["temp"] if snap["gpu"] and snap["gpu"].get("temp") is not None else None)

    with _GUARD:
        _STATE.clear()
        _STATE.update(snap)


def _loop():
    # WMI 在线程里用 COM，必须自己初始化
    if IS_WIN:
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
    if psutil:
        # 第一次 cpu_percent 没有参照物，返回 0.0，先踩一脚丢掉
        try:
            psutil.cpu_percent(None)
            psutil.cpu_percent(None, percpu=True)
        except Exception:
            pass
    while True:
        t0 = time.time()
        try:
            _tick()
        except Exception as e:          # 任何一项炸了都不该让曲线停住
            with _GUARD:
                _STATE["ok"] = bool(_STATE.get("cpu"))
                _STATE["error"] = str(e)
        time.sleep(max(0.15, _INTERVAL - (time.time() - t0)))


def start():
    if _STARTED[0]:
        return
    _STARTED[0] = True
    if not psutil:
        with _GUARD:
            _STATE["error"] = "缺少 psutil，无法读取本机状态"
        return
    th = threading.Thread(target=_loop, name="pcsys", daemon=True)
    th.start()


def snapshot():
    """给路由用的只读快照（深拷贝一层外壳即可，历史数组本来每次都是新的）"""
    with _GUARD:
        out = dict(_STATE)
        out["hist"] = {
            "cpu": [round(x, 1) for x in _HIST["cpu"]],
            "gpu": [round(x, 1) for x in _HIST["gpu"]],
            "mem": [round(x, 1) for x in _HIST["mem"]],
            "temp": list(_HIST["temp"]),
            "n": len(_HIST["cpu"]),
            "span": _INTERVAL,
        }
    return out


# ------------------------------------------------- 拉起传感器（需要一次 UAC）
# CPU 温度必须由带内核驱动的程序读，而这些驱动只允许管理员加载。
# 网页进程没权限，唯一干净的做法是：用户点一下"接入"，由系统弹一次 UAC，
# Core Temp 以管理员身份起来，之后共享内存里就有每核温度了。
def find_tool(prefer="Core Temp"):
    for label, paths in _TOOLS:
        if label != prefer:
            continue
        for p in paths:
            if os.path.exists(p):
                return {"name": label, "path": p}
    return None


def start_sensor():
    """返回 {ok, msg, name}。只在 Windows 上有效。"""
    if not IS_WIN:
        return {"ok": False, "msg": "只支持 Windows"}
    t = find_tool("Core Temp")
    if not t:
        return {"ok": False, "msg": "没找到 Core Temp，装一个即可"}
    if _coretemp():
        return {"ok": True, "msg": "已经在读了", "name": t["name"]}
    try:
        import ctypes
        from ctypes import wintypes as wt
        # ShellExecuteW + "runas"：会弹系统 UAC。用户点"是"才真正启动，
        # 点"否"这里会返回 <= 32 的错误码，我们如实告诉他没启动成功。
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteW.restype = wt.HINSTANCE
        shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR,
                                          wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
        rc = shell32.ShellExecuteW(None, "runas", t["path"], "-m", None, 1)
        if rc is None or rc <= 32:
            return {"ok": False, "msg": "UAC 被取消或启动失败（错误码 %s）" % rc,
                    "name": t["name"]}
        # 起来后驱动要几秒初始化，先等一拍再回报
        for _ in range(14):
            time.sleep(0.5)
            if _coretemp():
                _SLOW["ct"] = 0.0        # 让下一拍立刻重读
                return {"ok": True, "msg": "已接入", "name": t["name"]}
        return {"ok": False, "msg": "已启动，但还没读到温度（可能被驱动拦住）",
                "name": t["name"]}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def tool_hint():
    """没温度时给前端一句能落地的话"""
    got = [x for x in _probe_tools()]
    return got
