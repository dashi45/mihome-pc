# -*- coding: utf-8 -*-
"""进程内的"云直连"策略：不动环境变量。

## 原来的做法有什么问题

app.py 开头干了三件事：

    for k in ("http_proxy", ..., "NO_PROXY"): os.environ.pop(k)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"

目的正当（小米服务在国内，走代理隧道会把登录卡死），但手段是**改全局环境**：

  1. 子进程继承。go2rtc、ffmpeg、充电器蓝牙桥都是 `Popen` 起来的，它们
     继承 `NO_PROXY=*`，各自库里的代理逻辑跟着失效 —— 用户机器上本来配好的
     公司代理/VPN 分流被打乱，而且完全看不出是这里干的。
  2. 影响面过大。进程内任何用 urllib 的代码都被强制直连，包括本该走系统
     代理的（天气、外网素材）。

## 现在的做法

只在**云调用这条链路**上直连：miservice 的 `biohttp` 每次请求都会
`urllib.request.build_opener(...)`，我们把这个工厂包一层，强制追加
`ProxyHandler({})`（空代理表 = 直连）。作用范围就是这个进程里的 urllib，
环境变量一个都不碰，子进程不受影响。

天气模块继续用默认 opener，该走系统代理就走系统代理 —— 那是它该有的行为。

要关掉：`MIHOME_CLOUD_DIRECT=0`（网络环境特殊时排障用）。
"""
import os
import urllib.request

import logx

log = logx.get(__name__)

_installed = False
_orig_build_opener = None


def enabled():
    return (os.environ.get("MIHOME_CLOUD_DIRECT", "1").strip().lower()
            not in ("0", "false", "off", "no"))


def direct_opener(*handlers):
    """要直连的地方直接用它，比 install() 更明确。"""
    hs = tuple(h for h in handlers
               if not isinstance(h, urllib.request.ProxyHandler))
    return urllib.request.build_opener(*(hs + (urllib.request.ProxyHandler({}),)))


def install():
    """让之后所有 build_opener 出来的人都直连（幂等）。"""
    global _installed, _orig_build_opener
    if _installed or not enabled():
        return False
    _orig_build_opener = urllib.request.build_opener

    def _patched(*handlers):
        hs = tuple(h for h in handlers
                   if not isinstance(h, urllib.request.ProxyHandler))
        return _orig_build_opener(*(hs + (urllib.request.ProxyHandler({}),)))

    urllib.request.build_opener = _patched
    _installed = True
    log.debug("云调用已切换为进程内直连（未修改环境变量）")
    return True


def uninstall():
    global _installed
    if _installed and _orig_build_opener is not None:
        urllib.request.build_opener = _orig_build_opener
        _installed = False
