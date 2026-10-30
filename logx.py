# -*- coding: utf-8 -*-
"""统一日志：一个进程一个日志文件，按天轮转，按模块分 logger。

## 改造前的状况

`print()` 直接往 stdout 打（打包成 windowed exe 后 stdout 是 None，等于扔掉）、
`log_err()` 各自实现、launcher.log 和 start.log 各写一份，出问题时要同时
翻三个文件还不知道哪条是哪条。

## 现在的约定

    logger = logx.get(__name__)          # 模块开头一次
    logger.info("设备 %s 已就绪", did)    # 用 %s 占位，别用 f-string

    · 所有 logger 都挂在 "mihome" 这个父 logger 下，handler 只在父上装一份
      （每个模块各装一份 handler 的话，一条日志会被写 N 遍）
    · 文件：logs/mihome.log，每天零点轮转，留 7 天
    · 控制台：只有非 windowed 运行时才装（打包 exe 的 stdout 是 None）
    · 级别：MIHOME_LOG_LEVEL，默认 INFO；排查时设 DEBUG 重启即可

日志**不写凭据**：token、密码、摄像头密钥在调用处就不要拼进消息。
"""
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

import config

_ROOT_NAME = "mihome"
_CONFIGURED = False

_FMT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_DATEFMT = "%m-%d %H:%M:%S"


def _level():
    raw = os.environ.get("MIHOME_LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, raw, logging.INFO)


def setup():
    """安装 handler（幂等，重复调用只生效一次）。"""
    global _CONFIGURED
    root = logging.getLogger(_ROOT_NAME)
    if _CONFIGURED:
        return root

    root.setLevel(_level())
    root.propagate = False          # 别再冒泡到 root 让第三方 handler 重复输出
    fmt = logging.Formatter(_FMT, _DATEFMT)

    # 文件：按天轮转。单文件无限增长的历史教训是 evt.db-wal 长到 280MB
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        fh = TimedRotatingFileHandler(
            config.LOG_PATH, when="midnight", backupCount=7, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        # 日志装不上不能影响主功能（目录只读之类），退化成只有控制台
        pass

    # 控制台：打包成 windowed exe 时 sys.stdout 是 None，写了会抛异常
    if sys.stdout is not None:
        try:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            root.addHandler(sh)
        except Exception:
            pass

    _CONFIGURED = True
    return root


def get(name):
    """取一个模块 logger。name 传 __name__ 即可，会自动挂到 mihome 下。"""
    setup()
    short = name.split(".")[-1] if name.startswith("mihome") else name
    return logging.getLogger("%s.%s" % (_ROOT_NAME, short))


def redirect(log_dir):
    """把日志文件换到 log_dir（搬已有的 handler，不重装控制台）。

    只给 launcher 用：它 import 时就装了 handler（那时 BASE 还是 exe 目录），
    等 find_dir() 找到源码目录后才调这里，把日志收拢到 <源码>\\logs，
    跟后端写同一份。调用前的记录已经落在老路径上，无所谓 —— 那几条
    本来也不重要（都是启动中段的常规信息）。

    失败不影响主流程：换不了就继续写老路径，总比没有日志好。
    """
    root = logging.getLogger(_ROOT_NAME)
    setup()
    old_path = config.LOG_PATH
    old = [h for h in list(root.handlers) if isinstance(h, TimedRotatingFileHandler)]
    for h in old:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    target = config.set_log_dir(log_dir)
    try:
        os.makedirs(target, exist_ok=True)
        fh = TimedRotatingFileHandler(
            config.LOG_PATH, when="midnight", backupCount=7, encoding="utf-8")
        fh.setFormatter(logging.Formatter(_FMT, _DATEFMT))
        root.addHandler(fh)
        _drop_empty(old_path)
        return config.LOG_PATH
    except Exception:
        # 新目录写不了（只读盘 / 权限），把老 handler 装回去
        for h in old:
            try:
                root.addHandler(h)
            except Exception:
                pass
        return None


def _drop_empty(path):
    """挪走之后把老文件清掉（仅当它是 0 字节的空壳）。

    打包版 launcher 一 import 就会在 exe 目录（dist\\米家\\logs）建好 handler，
    等 find_dir 找到源码目录才重定向 —— 那一瞬会在发行目录留下一个 0 字节的
    mihome.log。用户看到"程序目录里还有个空日志"会以为写盘失败，所以顺手删掉。
    非空一律不动：那里面可能有上一版本崩溃时的现场。
    """
    try:
        if path and os.path.isfile(path) and os.path.getsize(path) == 0:
            os.remove(path)
            parent = os.path.dirname(path)
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
    except Exception:
        pass


def child(suffix):
    """给同一模块里的子用途取 logger（比如 "cam.hls"）。"""
    setup()
    return logging.getLogger("%s.%s" % (_ROOT_NAME, suffix))
