# -*- coding: utf-8 -*-
"""token 文件的读/写/备份/还原。

miservice 的 MiTokenStore 是**直接覆盖写**（先 open('w') 截断，再写），
进程被杀或断电就会留下半截 JSON。这里的 `_token_read` 因此校验内容而不只
判断文件存在性；备份走"临时文件 + 原子替换"，还原时反过来校验一遍。

凭据在磁盘上是加密的（见 creds.py），读写都经过它；明文旧文件照样能读。
"""

import config
import creds


def _token_read(path):
    """读并校验 token 文件，返回 dict 或 None。

    miservice 的 MiTokenStore 是**直接覆盖写**（先 open('w') 截断，再 f.write），
    进程被杀 / 断电就会留下一个半截 JSON。原来这里只判断"文件在不在"，
    半截文件在 → 既不还原备份也不报错，最后表现成"莫名其妙突然要重新登录"。
    所以校验内容，不只看存在性。
    """
    d = creds.load_json(path)      # 加密的解开，明文旧文件原样解析
    if isinstance(d, dict) and d.get("userId") and d.get("xiaomiio"):
        return d
    return None


def backup_token():
    """登录/恢复成功后留一份副本（只备份**校验通过**的内容）。

    miservice 在认证异常时会直接删掉主 token 文件，有副本就能在下次启动时
    自动还原，避免因为一次网络抖动就要求重新输密码。

    必须先校验再备份：原来是无条件 shutil.copyfile，主文件一旦是半截的，
    唯一的好副本会被坏内容当场覆盖 —— 备份等于没有。
    """
    d = _token_read(config.TOKEN_PATH)
    if not d:
        return False
    # creds.dump_json 内部就是"临时文件 + 原子替换"，且写入时按需加密
    return creds.dump_json(config.TOKEN_BAK, d)


def restore_token_from_backup():
    """主 token 缺失**或损坏**时从副本还原（原子写）。

    只在主文件不可用时动手；主文件好着就一个字节都不碰 ——
    登录态正常时无谓的重写反而会把它弄坏。
    """
    if _token_read(config.TOKEN_PATH):
        return False
    d = _token_read(config.TOKEN_BAK)
    if not d:
        return False
    return creds.dump_json(config.TOKEN_PATH, d)
