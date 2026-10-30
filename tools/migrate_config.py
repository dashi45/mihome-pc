# -*- coding: utf-8 -*-
"""把散在根目录的配置/缓存搬进 config/ 与 runtime/。

    python tools/migrate_config.py --dry-run     # 只说要搬什么
    python tools/migrate_config.py               # 真的搬

## 为什么要手动跑

config.py 的路径解析是"config/<name> 存在就用它，否则用根目录的"。
也就是说**不迁移也能正常跑**，迁移只是让磁盘更整齐。之所以不在启动时
自动搬：程序运行中移动别人正打开的文件（evt.db 有 -wal/-shm 跟着），
轻则日志报错，重则数据库损坏。所以这是一个显式、可回退的动作。

规则：源存在且**目标不存在**才搬。中途中断再跑一次也不会互相覆盖。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                    # noqa: E402


def main():
    dry = "--dry-run" in sys.argv
    print("程序目录:", config.BASE)
    print("配置目录:", config.CONFIG_DIR)
    print("缓存目录:", config.RUNTIME_DIR)
    print()

    todo = []
    for kind, (dst_dir, names) in config.LAYOUT.items():
        for name in names:
            src = os.path.join(config.BASE, name)
            dst = os.path.join(dst_dir, name)
            if os.path.exists(src) and not os.path.exists(dst):
                todo.append((kind, src, dst))

    if not todo:
        print("没有需要迁移的文件（可能已经迁过，或者文件都在 config/ 里）。")
        return 0

    for kind, src, dst in todo:
        print("  [%s] %s\n        -> %s" % (kind, src, dst))
    print("\n共 %d 个文件。" % len(todo))

    if dry:
        print("\n（--dry-run：没有移动任何文件）")
        return 0

    # 有程序在跑的时候别搬：Windows 上移动被占用的文件会失败，数据库还可能中间态
    if _app_running():
        print("\n✗ 检测到后端正在运行（端口 %d 在监听）。" % config.PORT)
        print("  先退出程序再迁移，否则可能搬到一个正在被写的 evt.db。")
        return 2

    report = config.migrate(dry_run=False)
    ok = sum(1 for _s, _d, r in report if r is True)
    bad = [(s, r) for s, _d, r in report if r not in (True, None)]
    print("\n✓ 已迁移 %d 个文件。" % ok)
    for s, why in bad:
        print("  ✗ %s：%s" % (s, why))
    return 0 if not bad else 1


def _app_running():
    import socket
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", config.PORT)) == 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
