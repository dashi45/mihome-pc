# -*- coding: utf-8 -*-
"""米家 PC 客户端 —— 应用装配入口。

这个文件只做四件事，不再承载任何业务：

  1. 进程级初始化（云直连策略、日志）
  2. 创建 Flask 实例、挂守卫钩子与响应头
  3. 注册 routes/ 下的全部蓝图
  4. ``__main__``：唤醒后台线程、拉起 waitress

业务在 ``services/``，HTTP 在 ``routes/``，路径在 ``config.py``。
拆分前这里是 3055 行 / 125 个函数 / 49 个路由挤在一个文件里，改一个设备接口
要翻过天气和进程管理才能找到地方。现在"改设备逻辑不会碰到天气代码"。
"""
import os
import sys

# 云调用直连：只作用于本进程 urllib，不改环境变量（子进程继承不到污染）。
# 必须在 import miservice / services 之前装好。
import netproxy

netproxy.install()

import atexit
import subprocess
import threading
import time
import types

from flask import Flask

import camhls
import config
import evtlog
import logx
import notify
import pcsys
import procman
import routes
import scheduler
import services.charger
import services.cloud
import services.devices
import services.energy
import services.guard as guard
import services.live
import services.state
import services.tokens
import services.workers
import weather
from services.workers import (_auto_restore, _evt_worker, _load_cached_devices,
                              _sched_worker)

log = logx.get(__name__)


# ---------------------------------------------------------------- 应用装配

def create_app():
    """创建 Flask 实例并把三条横切逻辑挂上。

    顺序有讲究：守卫（before_request）必须在注册蓝图之前装好 —— Flask 的
    请求钩子是"谁先注册谁先跑"，蓝图注册之后再挂也能生效，但装在这里能保证
    任何新加的蓝图都不会因为忘了加守卫而裸奔。
    """
    app = Flask(__name__, static_folder=config.STATIC_DIR,
                static_url_path="/static")
    # 本地应用绝不缓存：WebView2 会把带 max-age 的 js/css 缓存 12 小时，
    # 重启后端后界面仍跑旧代码（表现就是"新版变慢、按钮没反应"这类灵异 bug）
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    app.before_request(guard._local_only_guard)
    app.after_request(guard._no_static_cache)
    app.register_error_handler(Exception, guard._unhandled)

    names = routes.register_all(app)
    log.debug("已注册 %d 个蓝图：%s", len(names), ", ".join(names))
    return app


app = create_app()


# ---------------------------------------------------------------- 测试兼容层

#: 拆分前测试脚本用 `import app as A` 直接访问模块属性（A.S / A._OPENER = ...）。
#: 这些名字现在散在 services/ 下，这里做一层转发，让 40 多个测试脚本不用改一行。
_COMPAT_SOURCES = (
    config,
    services.tokens,
    services.state,
    services.cloud,
    services.devices,
    services.live,
    services.energy,
    services.charger,
    services.workers,
    services.guard,
)


#: 拆分前后名字对不上的几个（测试脚本还在用旧名字读）
_LEGACY_ALIASES = {
    "_PORT": (config, "PORT"),
    "_LOCAL_HOSTS": (config, "LOCAL_HOSTS"),
    "_RTSP_BASE": (config, "RTSP_BASE"),
}


class _CompatModule(types.ModuleType):
    """把"读/写 app.X"转发到真正的归属模块。

    · 读：本地没有就去各服务模块找 —— 返回的是**同一个对象**，
      所以 `A.S["status"] = ...` 改的是 services.state.S。
    · 写：先看哪个服务模块本来就有这个名字，写回那里。这样
      `A._OPENER = fake` 能让 services/charger.py 真的用上 fake。
      只写一份到本地会造成"两边不一致"的假象（读到的和用的不是同一个），
      所以命中归属模块时**不留本地副本**。

    这是过渡设施，不是架构。等测试脚本改成直接 import 各自的模块之后，
    这段可以整块删掉。
    """

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        # 少数名字拆分时改了大小写/前缀（_PORT → config.PORT），单独对一下
        alias = _LEGACY_ALIASES.get(name)
        if alias is not None:
            mod, attr = alias
            return getattr(mod, attr)
        for mod in _COMPAT_SOURCES:
            try:
                return getattr(mod, name)
            except AttributeError:
                continue
        raise AttributeError("module %r has no attribute %r" % (__name__, name))

    def __setattr__(self, name, value):
        for mod in _COMPAT_SOURCES:
            if hasattr(mod, name):
                setattr(mod, name, value)
                return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _CompatModule


# ---------------------------------------------------------------- 启动细节

def _relaunch_windowless():
    """如果自己是跑在控制台版 python.exe 下，换成 pythonw.exe 重启一次。

    为什么需要：python.exe 是控制台程序。哪怕用 CREATE_NO_WINDOW 拉起，
    只要父进程本身带控制台，子进程就会继承它 —— 屏幕上就是一个黑框。
    正常路径下 launcher 用 pythonw.exe 启动，不会有这问题；但有人手动
    `python app.py` 或用别的方式起，就会弹框。桌面程序不该在用户面前
    甩一个黑框，所以这里自愈一次。

    只做一次：用环境变量打标记，重启后的进程不会再触发。
    """
    if os.environ.get("MIHOME_WINDOWLESS"):
        return False
    if os.environ.get("MIHOME_NO_RELAUNCH"):
        return False

    exe = sys.executable or ""
    if not exe.lower().endswith("python.exe"):
        return False                      # 已经是 pythonw 或打包版，不用管
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if not os.path.isfile(pyw):
        return False

    env = dict(os.environ)
    env["MIHOME_WINDOWLESS"] = "1"
    try:
        fh = open(os.path.join(config.BASE, "start.log"), "ab", buffering=0)
        subprocess.Popen([pyw, os.path.abspath(__file__)],
                         cwd=config.BASE, env=env,
                         stdout=fh, stderr=fh,
                         stdin=subprocess.DEVNULL,
                         creationflags=config.NO_WIN, close_fds=True)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    # 先看要不要换成无窗口版再跑（换成功就直接退出，让新进程接管）
    if _relaunch_windowless():
        sys.exit(0)

    # 先读缓存（毫秒级），再起后台恢复线程
    _load_cached_devices()
    threading.Thread(target=_auto_restore, daemon=True).start()
    # 天气预热：首次定位（按 IP 猜城市）+ 取一次预报，
    # 这样界面一打开就有数据，而不是先转圈等外网
    weather.warm()
    # 本机硬件采样线程（1 秒一拍，路由只读快照）
    pcsys.start()
    # 进程列表线程：没人看的时候只是空转，不扫描
    procman.start()
    # 定时开关：后台每 15 秒判一次，到点就执行
    scheduler.load()
    threading.Thread(target=_sched_worker, daemon=True).start()
    # 事件补采线程（页面没人看的时候顶班记录变化）
    threading.Thread(target=_evt_worker, daemon=True).start()
    # 事件 -> 推送：用回调挂上去，evtlog 不认识 notify，想停就停
    evtlog.set_hook(notify.on_event)
    notify.start()
    port = config.PORT      # 守卫按这个端口校验 Origin/Host，两处必须是同一个值
    log.info("米家 PC 客户端已启动：http://127.0.0.1:%d", port)
    if sys.stdout is not None:      # windowed 运行时 stdout 是 None，别往空气里写
        print("=" * 56)
        print("  米家 PC 客户端")
        print(f"  打开浏览器访问: http://127.0.0.1:{port}")
        print("=" * 56)
    # 后端退出时把转码进程收干净，否则下次启动会占着摄像头的 P2P 连接
    atexit.register(camhls.stop_all)

    # 首屏之后把工作集交还系统。
    #
    # 任务管理器「内存」列看的是**工作集**（当前驻留的物理页）。启动阶段为了
    # 拉设备列表、解设备规范、导前端包，会留下一堆只用了一次的页。把这些页
    # 换出去，任务管理器里的数字会明显下降；下次用到时按需换回（代价是一次
    # 缺页，几毫秒）。
    #
    # 说清楚性质：这是**账面优化**，不是真的少占了内存 —— 提交内存不变，
    # 只是把暂时不用的物理页挪到页面文件。常驻类应用都这么做（Windows 自己
    # 也会在空闲时自动 trim），所以放在启动后做一次。
    #
    # 只做一次：反复 trim 会把热数据也赶出去，重新访问反而变慢。
    def _trim_once():
        time.sleep(90)      # 等首屏数据拉完、前端也加载完
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            # 必须显式声明 restype/argtypes：进程伪句柄是 -1，默认按 c_int
            # 处理会在 64 位下被截断成无效句柄，调用静默失败（GetLastError=6）
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetProcessWorkingSetSize.argtypes = [
                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
            handle = k32.GetCurrentProcess()
            k32.SetProcessWorkingSetSize(handle, ctypes.c_size_t(-1),
                                         ctypes.c_size_t(-1))
        except Exception:
            pass

    threading.Thread(target=_trim_once, name="trim-wset", daemon=True).start()

    try:
        from waitress import serve
        # 生产级 WSGI：线程池，慢请求（米家云端/取流）不会再卡住整个界面。
        #
        # 线程数从 16 降到 8：这是单用户本地应用，并发请求都来自同一个页面
        # （本机 2 秒一次、环境 5 秒一次、进程面板 3 秒一次），16 个线程里
        # 常驻闲着 12 个。waitress 的线程是启动时全部创建好的，每个都带
        # 独立栈和缓冲区；减一半线程，内存下来一点，调度开销也小。
        # 仍留 8 个：/api/cam 这种要握手 20 秒的慢请求最多会同时占 1~2 个，
        # 再留足余量给快请求。
        serve(app, host="127.0.0.1", port=port, threads=8, channel_timeout=120)
    except ImportError:
        # 兜底：老环境没有 waitress 时退回 Flask 开发服务器
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
