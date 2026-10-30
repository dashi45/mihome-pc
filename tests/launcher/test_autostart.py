# -*- coding: utf-8 -*-
"""开机自启模块回归测试

真实动注册表有风险（写坏了用户开机就乱跑），
所以测试全程在**替身注册表**上跑 —— 用一个假的 winreg，
模块通过 _run_key() 拿到的永远是假对象，真实 HKCU 一个字节都不碰。

真正动注册表的那一步（enable/disable）只在最后用「读回验证」的方式做，
且会在结束时无条件清理掉。
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

import autostart  # noqa: E402

failed = 0
total = 0


def check(name, cond, extra=""):
    global failed, total
    total += 1
    if cond:
        print("  ✓ %s" % name)
    else:
        failed += 1
        print("  ✗ %s %s" % (name, extra))


print("== 1. 假注册表：enable / disable / is_enabled ==")

# 造一个假的 winreg
STORE = {}


class FakeKey:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True
        return False

    def SetValueEx(self, name, typ, flags, value):
        STORE[name] = value

    def QueryValueEx(self, name):
        if name not in STORE:
            raise OSError("not found")
        return STORE[name], 1

    def DeleteValue(self, name):
        if name not in STORE:
            raise FileNotFoundError(name)
        del STORE[name]


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_ALL_ACCESS = 0xF003F
    REG_SZ = 1

    @staticmethod
    def OpenKey(root, path, reserved=0, access=0):
        return FakeKey()

    @staticmethod
    def CreateKey(root, path, reserved=0, access=0):
        return FakeKey()

    # 真实 winreg 是模块级函数，签名是 (key, name) —— 替身必须一致
    @staticmethod
    def QueryValueEx(key, name):
        return key.QueryValueEx(name)

    @staticmethod
    def SetValueEx(key, name, typ, flags, value):
        return key.SetValueEx(name, typ, flags, value)

    @staticmethod
    def DeleteValue(key, name):
        return key.DeleteValue(name)


orig_winreg = autostart.winreg
autostart.winreg = FakeWinreg

# _target() 依赖真实文件存在，先造一个临时 bat
tmpdir = tempfile.mkdtemp()
fake_bat = os.path.join(tmpdir, "启动米家.bat")
with open(fake_bat, "w", encoding="utf-8") as f:
    f.write("@echo off\n")

orig_target = autostart._target
autostart._target = lambda: fake_bat

STORE.clear()
check("初始是关的", autostart.is_enabled() is False)
check("status 里 supported=True", autostart.status()["supported"] is True)

ok, err = autostart.enable()
check("enable 成功", ok is True, err)
check("注册表写进去了", autostart.VALUE_NAME in STORE)
check("enable 后 is_enabled=True", autostart.is_enabled() is True)
check("值里带引号（路径含空格也不怕）",
      STORE.get(autostart.VALUE_NAME, "").startswith('"') and
      STORE.get(autostart.VALUE_NAME, "").endswith('"'),
      repr(STORE.get(autostart.VALUE_NAME)))

ok, err = autostart.disable()
check("disable 成功", ok is True, err)
check("disable 后 is_enabled=False", autostart.is_enabled() is False)

# 删不存在的项不该报错
ok, err = autostart.disable()
check("重复 disable 不报错", ok is True, err)

print()
print("== 2. 目标文件不存在 → 必须拒绝写入 ==")
autostart._target = lambda: os.path.join(tmpdir, "不存在的.exe")
ok, err = autostart.enable()
check("目标不存在时 enable 失败", ok is False)
check("给出了错误原因", bool(err), repr(err))
check("注册表里没有留下坏项", autostart.VALUE_NAME not in STORE)

print()
print("== 3. 路径含空格 → 不能解析错 ==")
# 真实的坑：'"C:\\Program Files\\米家.exe"' 用 split(" ")[0] 会切成 '"C:\\Program'
space_dir = os.path.join(tmpdir, "Program Files")
os.makedirs(space_dir, exist_ok=True)
space_exe = os.path.join(space_dir, "米家.exe")
with open(space_exe, "w", encoding="utf-8") as f:
    f.write("x")

STORE[autostart.VALUE_NAME] = '"%s"' % space_exe
check("含空格路径能正确解析",
      autostart._parse_path(STORE[autostart.VALUE_NAME]) == space_exe,
      repr(autostart._parse_path(STORE[autostart.VALUE_NAME])))
check("含空格路径时 is_enabled=True", autostart.is_enabled() is True)

STORE[autostart.VALUE_NAME] = '"%s" --arg' % space_exe
check("带参数的路径也能解析",
      autostart._parse_path(STORE[autostart.VALUE_NAME]) == space_exe,
      repr(autostart._parse_path(STORE[autostart.VALUE_NAME])))

STORE[autostart.VALUE_NAME] = space_exe          # 不带引号的裸路径
check("裸路径能解析",
      autostart._parse_path(STORE[autostart.VALUE_NAME]) == space_exe,
      repr(autostart._parse_path(STORE[autostart.VALUE_NAME])))

print()
print("== 3b. 注册表项在但文件没了 → 视为没开 ==")
STORE[autostart.VALUE_NAME] = '"%s"' % os.path.join(tmpdir, "已删除.exe")
check("文件不存在时 is_enabled=False", autostart.is_enabled() is False)

print()
print("== 4. 看门狗：连续失败自动关掉自启 ==")
autostart._target = lambda: fake_bat
STORE.clear()
autostart.enable()
check("先打开自启", autostart.is_enabled() is True)

# 把失败日志指到临时目录，别污染项目目录
tmp_fail = os.path.join(tmpdir, "autostart_fail.log")
orig_fail_path = autostart._fail_path
autostart._fail_path = lambda: tmp_fail

autostart.note_start_failed("第一次失败")
check("失败 1 次后自启还开着", autostart.is_enabled() is True)
check("失败计数 = 1", autostart.fail_count() == 1)

autostart.note_start_failed("第二次失败")
check("失败 2 次后自启还开着", autostart.is_enabled() is True)
check("失败计数 = 2", autostart.fail_count() == 2)

autostart.note_start_failed("第三次失败")
check("失败 3 次后自启被自动关闭", autostart.is_enabled() is False)
check("失败计数 = 3", autostart.fail_count() == 3)
check("写了说明文件", os.path.isfile(tmp_fail + ".disabled"))

# 成功启动一次应该清零
autostart._clear_fail()
check("成功后失败计数清零", autostart.fail_count() == 0)

print()
print("== 5. 非 Windows 平台降级 ==")
autostart.winreg = None
ok, err = autostart.enable()
check("无 winreg 时 enable 失败但不崩", ok is False)
check("提示是 Windows 专属", "Windows" in err, repr(err))
check("status.supported=False", autostart.status()["supported"] is False)

# 还原
autostart.winreg = orig_winreg
autostart._target = orig_target
autostart._fail_path = orig_fail_path

print()
print("== 6. 真实注册表读写（会无条件清理回去）==")
real_before = autostart.is_enabled()
try:
    ok, err = autostart.enable()
    check("真实 enable 成功", ok is True, err)
    check("真实 is_enabled=True", autostart.is_enabled() is True)
    ok, err = autostart.disable()
    check("真实 disable 成功", ok is True, err)
    check("真实 is_enabled=False", autostart.is_enabled() is False)
except Exception as e:
    check("真实注册表操作没抛异常", False, repr(e))
finally:
    # 恢复用户原来的设置，不能把人家开着的自启关掉
    try:
        if real_before:
            autostart.enable()
    except Exception:
        pass
    check("已恢复用户原本的自启设置", autostart.is_enabled() == real_before)

print()
if failed:
    print("❌ %d / %d 项未通过" % (failed, total))
    sys.exit(1)
print("✅ 全部 %d 项通过" % total)
