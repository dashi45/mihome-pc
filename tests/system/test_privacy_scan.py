# -*- coding: utf-8 -*-
"""构建前隐私自检：确认要分发的东西里没有凭据、令牌、日志、个人数据。

为什么需要（参考项目也做了这件事，理由同样适用于我）：
  打包一次 `dist/`，如果不小心把 cam_secret.json（摄像头账号密码）、
  token_store、evt.db（设备使用记录）、devices_cache.json（家庭房间名）
  一起打进去，程序一发给别人，这些东西就跟着走了 —— 而且**看不出来**，
  因为程序照常运行，没有任何报错。

跑法：
  .venv/Scripts/python.exe -u tests/privacy_scan.py            # 扫 dist 和 exe 目录
  .venv/Scripts/python.exe -u tests/privacy_scan.py --all      # 连项目根目录一起扫

退出码 1 = 发现不该分发的东西。
"""
import fnmatch
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 绝不能进分发物的文件名（按 basename 匹配，大小写不敏感）
FORBIDDEN_FILES = [
    "cam_secret.json",       # 摄像头账号密码
    "token_store.json",      # 米家登录令牌
    "mi.token", "token.json", "miot_token*",
    "evt.db", "*.db", "*.sqlite", "*.sqlite3",     # 设备使用记录
    "devices_cache.json",    # 家庭/房间/设备名
    "spec_cache.json",
    "cookies.txt", "*.pem", "*.key", "id_rsa*",
    ".env", "*.log", "*.bak",
    "schedules.json", "autostart.json",
]

# 允许存在的日志/缓存文件名（明确豁免，避免误报）
ALLOW_SUFFIX = (".py", ".pyc", ".js", ".css", ".html", ".md", ".txt",
                ".ico", ".png", ".jpg", ".svg", ".bat", ".json.example")

# 运行期目录：程序自己写的日志、缓存都在这里，不属于"打包进去的东西"。
# 打包版 exe 的日志落在 <exe目录>/logs/（绿色版就地留痕，用户找日志不用翻
# AppData）；如果把它算成违规，每跑一次程序测试就红一次，红线就没人看了。
# 但打包成 zip 分发时**必须**手动排除 —— 所以下面会明确提示，不是静默放过。
RUNTIME_DIRS = {"logs", "runtime", "__pycache__", ".pytest_cache"}


def hit(name):
    low = name.lower()
    for pat in FORBIDDEN_FILES:
        if fnmatch.fnmatch(low, pat.lower()):
            return pat
    return None


def scan(root, label):
    bad = []
    if not os.path.isdir(root):
        return bad
    skipped = []
    for dirpath, dirnames, filenames in os.walk(root):
        drop = [d for d in dirnames if d.lower() in RUNTIME_DIRS]
        for d in drop:
            skipped.append(os.path.relpath(os.path.join(dirpath, d), BASE))
        dirnames[:] = [d for d in dirnames if d.lower() not in RUNTIME_DIRS]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            pat = hit(fn)
            if pat:
                # 项目根目录里的运行期文件是正常的（程序自己在用）；
                # 只在"分发物"目录里出现才算问题。
                bad.append((os.path.relpath(p, BASE), pat,
                            os.path.getsize(p)))
    for s in skipped:
        print("  · 跳过运行期目录 %s（分发 zip 前记得排除）" % s)
    return bad


def main():
    targets = [
        (os.path.join(BASE, "dist"), "dist/（打包产物目录）"),
    ]
    if "--all" in sys.argv:
        targets.append((BASE, "项目根目录"))

    desktop_exe = os.path.join(os.path.expanduser("~"), "Desktop", "米家.exe")
    if os.path.isfile(desktop_exe):
        # 桌面 exe 是单文件，只可能夹带"打包进去"的东西，这里只能提示
        print("  · 桌面 exe: %s（%.1f MB）"
              % (desktop_exe, os.path.getsize(desktop_exe) / 1048576))

    total = 0
    for root, label in targets:
        if not os.path.isdir(root):
            print("  · 跳过 %s（目录不存在）" % label)
            continue
        bad = scan(root, label)
        if bad:
            print("\n❌ %s 里发现 %d 个不该分发的文件：" % (label, len(bad)))
            for rel, pat, size in bad:
                print("    %-52s 命中规则 %-18s %.1f KB"
                      % (rel, pat, size / 1024))
            total += len(bad)
        else:
            print("  ✅ %s 干净" % label)

    # 顺手确认关键代码文件都在（避免"扫了个空目录说干净"）
    for need in ("launcher.py", "app.py", "camhls.py"):
        p = os.path.join(BASE, need)
        if not os.path.isfile(p):
            print("  ⚠ 关键文件缺失：%s（当前目录可能不对）" % need)

    print()
    if total:
        print("❌ 自检失败：有 %d 个敏感文件会跟着分发包出去" % total)
        return 1
    print("✅ 自检通过：没有敏感文件混入分发物")
    return 0


if __name__ == "__main__":
    sys.exit(main())
