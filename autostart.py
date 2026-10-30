# -*- coding: utf-8 -*-
"""开机自启（Windows 注册表 Run 项）

为什么不用「启动文件夹」快捷方式：
  - 启动文件夹里用户能看见、能误删，误删后不自启却没人知道；
  - 注册表 Run 项可以精确指向 exe，不会因为移动了快捷方式就失效；
  - 还能用「自启失败三次就禁用」的看门狗，避免无限重启把电脑拖垮
    （详见下面的 _bump_fail_count）。

两条硬规矩：
  1. 只写 HKCU（当前用户），不碰 HKLM —— 免管理员权限，也不污染系统级配置。
  2. 写之前一定先校验目标文件真实存在。指向不存在的文件时 Windows
     开机后会静默失败，用户只看到"程序没起来"，排查起来很费劲。
"""
import os
import sys

try:
    import winreg
except ImportError:          # 非 Windows：整个模块不可用
    winreg = None

REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MiHomePCDesktop"
FAIL_LOG = "autostart_fail.log"

# 看门狗：连续失败 3 次就关掉自启，别让电脑开机就反复拉起一个起不来的程序
MAX_FAIL = 3


def _run_key():
    if winreg is None:
        return None
    try:
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_ALL_ACCESS)
    except OSError:
        try:
            return winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_ALL_ACCESS)
        except OSError:
            return None


def _target():
    """自启时要启动的东西。

    打包版 → exe 自己（frozen 时 sys.executable 就是那个 exe）；
    源码版 → 启动米家.bat（它会拉起 launcher，行为和桌面图标一致）。
    """
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if os.path.isfile(exe):
            return exe
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    bat = os.path.join(here, "启动米家.bat")
    if os.path.isfile(bat):
        return bat
    return None


def _read_value():
    k = _run_key()
    if not k:
        return None
    try:
        with k:
            v, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return v
    except OSError:
        return None


def _parse_path(v):
    """从注册表值里取出可执行文件路径。

    坑：路径含空格时不能用 split(" ")[0] —— 值是
    '"C:\\Program Files\\米家.exe"'，按空格切会得到 '"C:\\Program'，
    后面 isfile 判 False，自启就莫名其妙"显示是关的"。
    正确做法：先看是不是带引号，带引号就取到**下一个**引号为止；
    不带引号时整串先试一次（路径含空格但没加引号的情况），
    整串不是文件才按第一个空格切（兼容 "路径 参数"）。
    """
    if not v:
        return ""
    v = v.strip()
    if v.startswith('"'):
        end = v.find('"', 1)
        return v[1:end] if end > 1 else v.strip('"')
    if os.path.isfile(v):
        return v
    return v.split(" ", 1)[0]


def is_enabled():
    """当前是不是开着自启。

    注意：要同时满足「注册表里有」和「指的文件还在」——
    注册表项还在但 exe 被删了，等于没开。
    """
    v = _read_value()
    if not v:
        return False
    return os.path.isfile(_parse_path(v))


def enable():
    """打开自启。返回 (成功?, 错误信息)。"""
    if winreg is None:
        return False, "只有 Windows 支持开机自启"
    target = _target()
    if not target:
        return False, "找不到可自启的程序文件"
    # 存在性校验必须放在**写入点**，不能只靠 _target() 内部那道。
    # 注册表项一旦指向不存在的文件，开机后静默失败，用户只看到"程序没起来"，
    # 排查极其费劲。写之前在这里再挡一次，任何路径变化都拦得住。
    if not os.path.isfile(target):
        return False, "要自启的文件不存在：%s" % target
    cmd = '"%s"' % target
    k = _run_key()
    if not k:
        return False, "注册表项打不开"
    try:
        with k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as e:
        return False, "写注册表失败：%s" % e
    _clear_fail()
    return True, ""


def disable():
    """关掉自启。返回 (成功?, 错误信息)。"""
    if winreg is None:
        return False, "只有 Windows 支持开机自启"
    k = _run_key()
    if not k:
        return False, "注册表项打不开"
    try:
        with k:
            winreg.DeleteValue(k, VALUE_NAME)
    except FileNotFoundError:
        pass
    except OSError as e:
        return False, "删注册表失败：%s" % e
    return True, ""


def set_enabled(on):
    return enable() if on else disable()


# ---------------- 看门狗 ----------------
def _fail_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), FAIL_LOG)


def _clear_fail():
    try:
        p = _fail_path()
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass


def note_start_failed(reason=""):
    """启动失败时记一笔。连续 MAX_FAIL 次就自动关掉自启。

    场景：exe 被杀毒软件删了、目录被移动了、Python 环境坏了 —— 这时候
    开机自启会每次开机都尝试拉起一个必然失败的程序，白白拖慢开机。
    """
    path = _fail_path()
    try:
        n = 0
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                n = int((f.read() or "0").strip() or 0)
        n += 1
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(n))
    except Exception:
        return
    if n >= MAX_FAIL and is_enabled():
        disable()
        try:
            with open(path + ".disabled", "w", encoding="utf-8") as f:
                f.write("连续 %d 次启动失败，已自动关闭开机自启。\n最后原因：%s\n"
                        % (n, reason))
        except Exception:
            pass


def fail_count():
    try:
        p = _fail_path()
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return int((f.read() or "0").strip() or 0)
    except Exception:
        pass
    return 0


def status():
    """给前端用的完整状态。"""
    on = is_enabled()
    return {
        "enabled": on,
        "target": _read_value() or "",
        "fail_count": fail_count(),
        "supported": winreg is not None,
    }
