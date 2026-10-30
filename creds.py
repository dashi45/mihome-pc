# -*- coding: utf-8 -*-
"""凭据加密存储（Windows DPAPI）。

## 解决什么

`.mi.token`（米家登录态）和 `cam_secret.json`（摄像头 P2P 密钥）原来是明文 JSON。
`.gitignore` 只挡 git —— 一旦有人把整个项目目录拷给别人、或者备份到网盘，
凭据就跟着走了，而且拿到的人不需要任何技巧，双击就能看。

## 怎么做

用 Windows 自带的 DPAPI（`CryptProtectData`）加密。特点：

  · **不需要自己管密钥**。密钥由系统从"当前用户 + 当前机器"派生，存在
    登录凭据里，程序里没有可被拷走的密钥文件。
  · **换机器/换用户解不开**。这是加密的意义，但也意味着：把目录拷到另一台
    电脑上，登录态失效、需要重新登录一次。这是预期行为，不是 bug。
  · 纯 ctypes，不加依赖（项目里的 cryptography 留给他用）。

文件格式（第一行就是全部元信息，方便肉眼判断有没有加密）：

    MIHOME1:<base64(DPAPI blob)>

读取时同时接受三种：加密格式、旧明文 JSON、空文件。所以升级不需要迁移脚本 ——
旧文件照样能读，下次写入时自动变成加密格式。

## 逃生口

`MIHOME_CRED_PLAIN=1` 强制明文（调试/排障用）。DPAPI 调用失败时也退回明文，
宁可弱一点也不能让用户登不进去。

## 注意：miservice 自己会读写 token 文件

`MiTokenStore` 直接 `json.load(open(path))`，喂给它的必须是**能用标准 json
解析的内容**。所以不能只在文件上做手脚 —— 要把 `SecureTokenStore` 传给
`MiAccount(token_store=...)`，让加解密发生在读写层。
"""
import base64
import ctypes
import json
import os
import sys
import tempfile
from ctypes import wintypes

import logx

log = logx.get(__name__)

PREFIX = "MIHOME1:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01

_available = None


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _api():
    """把 crypt32 的函数签名声明好（64 位下不声明会把指针截断）。"""
    c32 = ctypes.windll.crypt32
    k32 = ctypes.windll.kernel32
    c32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    c32.CryptProtectData.restype = wintypes.BOOL
    c32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_Blob)]
    c32.CryptUnprotectData.restype = wintypes.BOOL
    k32.LocalFree.argtypes = [wintypes.HLOCAL]
    k32.LocalFree.restype = wintypes.HLOCAL
    return c32, k32


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def available():
    """DPAPI 能不能用（非 Windows、被裁剪的系统上会不可用）。"""
    global _available
    if _available is None:
        try:
            _api()
            _available = True
        except Exception:
            _available = False
    return _available


def protect(raw: bytes) -> bytes:
    if not available():
        raise OSError("DPAPI 不可用")
    c32, k32 = _api()
    bin_blob, _keep = _blob(raw)
    bout = _Blob()
    ok = c32.CryptProtectData(ctypes.byref(bin_blob), "mihome-pc", None,
                              None, None, _CRYPTPROTECT_UI_FORBIDDEN,
                              ctypes.byref(bout))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(bout.pbData, bout.cbData)
    finally:
        k32.LocalFree(bout.pbData)


def unprotect(blob: bytes) -> bytes:
    if not available():
        raise OSError("DPAPI 不可用")
    c32, k32 = _api()
    bin_blob, _keep = _blob(blob)
    bout = _Blob()
    ok = c32.CryptUnprotectData(ctypes.byref(bin_blob), None, None,
                                None, None, _CRYPTPROTECT_UI_FORBIDDEN,
                                ctypes.byref(bout))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(bout.pbData, bout.cbData)
    finally:
        k32.LocalFree(bout.pbData)


def plain_mode():
    return (os.environ.get("MIHOME_CRED_PLAIN", "").strip().lower()
            in ("1", "true", "yes", "on"))


# ---------------------------------------------------------------- 文本层

def encrypt_text(text: str) -> str:
    """明文 → 可落盘的字符串。任何失败都退回明文（不阻断登录）。"""
    if plain_mode() or not available():
        return text
    try:
        blob = protect(text.encode("utf-8"))
        return PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception as e:
        log.warning("凭据加密失败，退回明文：%s", e)
        return text


def decrypt_text(text: str):
    """落盘字符串 → 明文。加密的解开，明文的原样返回。

    解不开（换了机器/用户）返回 None，调用方按"没有凭据"处理 ——
    表现是要求重新登录，而不是崩一个看不懂的异常。
    """
    if not text:
        return None
    s = text.lstrip("\ufeff").strip()
    if not s.startswith(PREFIX):
        return s
    try:
        raw = unprotect(base64.b64decode(s[len(PREFIX):]))
        return raw.decode("utf-8")
    except Exception as e:
        log.warning("凭据解密失败（可能换了机器或用户，需要重新登录）：%s", e)
        return None


# ---------------------------------------------------------------- 文件层

def read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return decrypt_text(f.read())
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("读取凭据文件失败 %s：%s", path, e)
        return None


def load_json(path):
    """读 JSON 凭据文件；不存在/损坏/解不开都返回 None。"""
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def write_text(path, text, private=True):
    """原子写：临时文件 + replace。写入时按需加密。"""
    payload = encrypt_text(text)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cred-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
        if private:
            _harden(path)
        return True
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        log.error("写入凭据文件失败 %s：%s", path, e)
        return False


def dump_json(path, obj):
    return write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def _harden(path):
    """尽力把 ACL 收紧到当前用户（Windows 上用 icacls；失败不影响功能）。"""
    if sys.platform != "win32":
        return
    try:
        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        import subprocess
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", "%s:F" % user],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            creationflags=0x08000000)
    except Exception:
        pass


# ---------------------------------------------------------------- miservice 适配

def make_token_store(path):
    """给 `MiAccount(token_store=...)` 用的加密适配层。

    miservice 只依赖 `load_token` / `save_token` 两个方法，继承它自己的
    MiTokenStore 覆盖掉即可，其余行为（token 为 None 时删除文件）保持原样。
    """
    from miservice.miaccount import MiTokenStore

    class SecureTokenStore(MiTokenStore):
        async def load_token(self):
            import asyncio
            return await asyncio.get_running_loop().run_in_executor(
                None, load_json, path)

        async def save_token(self, token=None):
            import asyncio
            loop = asyncio.get_running_loop()

            def _do():
                if token:
                    dump_json(path, token)
                elif os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

            await loop.run_in_executor(None, _do)

    return SecureTokenStore(path)
