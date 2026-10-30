#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CUKTECH 充电器 · 本地桥接服务
==============================

用系统蓝牙（bleak）在后台直连充电器，完成 MiOT 认证后持续轮询端口数据，
通过本地 HTTP 接口暴露给网页。

这样网页完全不需要 Web Bluetooth 授权，也不需要点任何按钮：
启动本服务 → 后台自动连接 → 网页打开即有数据。

接口:
    GET  /api/state    当前状态 JSON（端口数据 / 设置项 / 连接状态）
    GET  /api/health   存活探针
    POST /api/set      body: {"piid": 16, "value": 15}   写入设备属性
    其他路径           静态文件（网页本体）

BLE 协议实现来自 kairui1108/cuktech-ble-ha (MIT License)。
"""

import argparse
import asyncio
import json
import os
import queue
import sys
import threading
import time
import traceback
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SIID = 2                     # 充电器服务 ID
PORT_PIIDS = (1, 2, 3, 4)    # C1 / C2 / C3 / A
SETTING_PIIDS = (5, 6, 9, 10, 11, 12, 13, 15, 16, 19, 20, 21)

# ---- 自适应轮询 ----
# 有人在看页面时才按 1s 刷；没人看就退到保活频率，空闲时几乎不耗 CPU / 蓝牙。
POLL_INTERVAL  = 1.0         # 页面活跃
IDLE_INTERVAL  = 5.0         # 12s 无请求
SLEEP_INTERVAL = 15.0        # 2min 无请求（深睡保活）
CLIENT_WINDOW  = 12.0        # 多久内有请求算「有人在看」
ACTIVE_WINDOW  = 120.0


# ---------------------------------------------------------------- 失败分类
# 「充电器断开」和「这台电脑没有蓝牙」是两件完全不同的事，必须在界面上分开。
# 实测踩过：USB 蓝牙适配器掉了（设备管理器里变成一个
# "未知 USB 设备(设备描述符请求失败)"），界面却只说"设备断开，30 秒后重连"——
# 那个重连永远不会成功，用户会一直等，还以为充电器坏了。
_NO_ADAPTER = (
    'no bluetooth adapter', 'no bluetooth adaptor',
    'is bluetooth turned on', 'failed to start scanner',
    'bluetooth is not available', 'bluetooth turned off',
    'no bluetooth hardware', 'adapter not found',
)
_DEVICE_GONE = (
    'device not found', 'was not found', 'no matching device',
    'never got a reply', 'not in range', 'out of range',
    'connect timeout', 'timed out', 'timeout',
)

# 每种原因配一句"用户接下来该做什么"。没有这句，报错等于没报。
REASONS = {
    'no-adapter': {
        'title': '本机没有可用的蓝牙适配器',
        'hint': '系统里找不到蓝牙适配器。打开设备管理器 → 蓝牙：若出现'
                '「未知 USB 设备(设备描述符请求失败)」，重启一次电脑'
                '（关机再开机更彻底）通常能恢复',
        'cap': 60,      # 没有适配器时快重连毫无意义，退到 60 秒复查一次
    },
    'device-away': {
        'title': '找不到充电器',
        'hint': '确认充电器已通电、在 3 米内，且没有被手机米家 App 占用蓝牙',
        'cap': 30,
    },
    'unknown': {
        'title': '连接中断',
        'hint': '可以试试重新插拔充电器电源，或重启蓝牙',
        'cap': 30,
    },
}


# 「没有适配器」还分两种，处理方式完全不同：
#   1. 用户自己关了蓝牙 / 飞行模式     → 打开开关即可
#   2. 适配器被弹出、USB 枚举失败      → 开关和重启都没用，必须彻底断电
# 第 2 种在 Windows 里长得很有特征：设备描述符读不出来，系统把它挂成
# VID_0000&PID_0002（错误码 43），PnP 里连蓝牙类目都没了。
# 2026-10-25 实测：第三方「外设电量.exe」对蓝牙适配器发了一次 eject，
# 就变成这样，删设备/重扫/重置 USB 根集线器全失败，只有断电才回得来。
_BROKEN_USB_KEY = r'SYSTEM\CurrentControlSet\Enum\USB\VID_0000&PID_0002'

_BROKEN_TITLE = '蓝牙适配器枚举失败（被系统弹出）'
_BROKEN_HINT = ('适配器在 USB 层读不出设备描述符，软件重启无效。'
                '请彻底断电：关机 → 拔掉电源线或关插排开关 → 等 15 秒 → 开机。'
                '普通「重启」不行，待机供电还在。')


def usb_enum_broken():
    """有没有 USB 设备枚举失败（描述符请求失败，错误码 43）。

    只读注册表，微秒级，不会拖慢重连。读不到一律当 False。
    """
    try:
        import winreg
    except Exception:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BROKEN_USB_KEY) as k:
            winreg.EnumKey(k, 0)
            return True
    except OSError:
        return False


def reason_text(reason):
    """原因码 → (标题, 该怎么做)。no-adapter 会再细分是不是枚举失败。"""
    if reason == 'no-adapter' and usb_enum_broken():
        return _BROKEN_TITLE, _BROKEN_HINT
    info = REASONS.get(reason or 'unknown') or REASONS['unknown']
    return info['title'], info['hint']


def classify_error(msg):
    """把一长串底层报错压成一个原因码。顺序有讲究：先判「没有适配器」。"""
    low = (msg or '').lower()
    if any(k in low for k in _NO_ADAPTER):
        return 'no-adapter'
    if any(k in low for k in _DEVICE_GONE):
        return 'device-away'
    return 'unknown'


# ------------------------------------------------------------------ 配置
def load_config(path):
    """从 device-config.json 读取 mac / token，环境变量优先。"""
    cfg = {}
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                cfg = json.load(f)
        except Exception as e:
            print('[bridge] 配置文件读取失败: %s' % e, file=sys.stderr)

    name = str(cfg.get('name') or '').strip()
    mac = os.environ.get('CUKTECH_DEVICE_MAC') or cfg.get('mac') or ''
    token_hex = os.environ.get('CUKTECH_DEVICE_TOKEN') or cfg.get('token') or ''
    mac = str(mac).strip().upper()
    token_hex = str(token_hex).strip()

    if not mac or not token_hex:
        raise SystemExit('[bridge] 缺少设备参数：请在 device-config.json 填写 mac 与 token')

    try:
        token = bytes.fromhex(token_hex)
    except ValueError:
        raise SystemExit('[bridge] token 不是合法 hex: %r' % token_hex)

    if len(token) != 12:
        raise SystemExit('[bridge] token 应为 12 字节，当前 %d 字节' % len(token))

    return mac, token, name


# ------------------------------------------------------------------ 桥接核心
class Bridge:
    def __init__(self, mac, token, name=''):
        self.mac = mac
        self.token = token
        self.name = name
        self._lock = threading.Lock()
        self._cmd = queue.Queue()
        self._ctl = None
        self._stop = False
        self._last_client = 0.0     # 最近一次网页请求时间
        self._wake = False          # 有客户端刚上线，打断休眠
        self._state = {
            'status': 'starting',
            'detail': '正在启动 ...',
            # 原因码要能跨重连存活：detail 每轮都会被重写，只留它的话
            # 界面永远只会看到"设备断开"，看不到真正的失败原因
            'reason': '',
            'reason_title': '',
            'hint': '',
            'last_error': '',      # 底层原文，排查全靠它
            'device': {'mac': mac, 'name': name, 'model': '', 'fw': '', 'protoVer': ''},
            'ports': {},
            'settings': {},
            'frames': 0,
            'updated': 0,
        }

    # ---------------- 客户端活跃度 ----------------
    def touch(self):
        """网页拉状态时调用；从深睡恢复时立刻打断当前等待。"""
        now = time.time()
        if now - self._last_client > CLIENT_WINDOW:
            self._wake = True
        self._last_client = now

    def _poll_interval(self):
        if not self._last_client:
            return SLEEP_INTERVAL
        gap = time.time() - self._last_client
        if gap < CLIENT_WINDOW:
            return POLL_INTERVAL
        if gap < ACTIVE_WINDOW:
            return IDLE_INTERVAL
        return SLEEP_INTERVAL

    # ---------------- 状态快照（供 HTTP 线程读） ----------------
    def snapshot(self):
        with self._lock:
            s = self._state
            return {
                'status': s['status'],
                'detail': s['detail'],
                'reason': s['reason'],
                'reason_title': s['reason_title'],
                'hint': s['hint'],
                'last_error': s['last_error'],
                'device': dict(s['device']),
                'ports': dict(s['ports']),
                'settings': dict(s['settings']),
                'frames': s['frames'],
                'updated': s['updated'],
            }

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)
            self._state['updated'] = time.time()

    def _set_port(self, piid, data):
        with self._lock:
            self._state['ports'][str(piid)] = data
            self._state['frames'] = self._state.get('frames', 0) + 1
            self._state['updated'] = time.time()

    def _set_setting(self, piid, value):
        with self._lock:
            self._state['settings'][str(piid)] = value
            self._state['updated'] = time.time()

    # ---------------- 控制命令入队（HTTP 线程写） ----------------
    def send(self, piid, value):
        if self._state['status'] != 'ok':
            raise RuntimeError('设备未连接（当前状态: %s）' % self._state['status'])
        self._cmd.put((int(piid), value))

    # ---------------- 后台线程 ----------------
    def start(self):
        threading.Thread(target=self._thread_main, name='ble-bridge', daemon=True).start()

    def stop(self):
        self._stop = True

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception:
            traceback.print_exc()

    def _log_throttled(self, err, reason):
        """同一条错误别每 30 秒刷一行。

        以前没有节流，一个"没有蓝牙适配器"的状态能在一晚上把 charger.log
        刷到几千行，真正有新信息的那一行被淹掉，反而更难排查。
        消息变了立刻打；没变就 5 分钟一条。
        """
        now = time.time()
        sig = (reason, err)
        if sig != getattr(self, '_log_sig', None) or \
                now - getattr(self, '_log_t', 0.0) > 300:
            print('[bridge] 会话中断(%s): %s' % (reason or '-', err), file=sys.stderr)
            self._log_sig, self._log_t = sig, now

    async def _run(self):
        backoff = 2
        while not self._stop:
            err, reason = '', ''
            try:
                await self._connect_and_auth()
                backoff = 2
                reason = ''
                await self._session_loop()
            except Exception as e:
                err = str(e) or type(e).__name__
                reason = classify_error(err)
                self._log_throttled(err, reason)
                # 把原因留住。下面会写 status=reconnecting 并把 detail 换成
                # "N 秒后重连"，如果只存在 detail 里，真实原因当场就被盖掉了。
                r_title, r_hint = reason_text(reason)
                self._set(reason=reason, last_error=err,
                          reason_title=r_title, hint=r_hint)
            finally:
                await self._close()

            if self._stop:
                break
            info = REASONS[reason or 'unknown']
            if reason == 'no-adapter':
                # 说"重连"会误导：没有适配器时重连不可能成功，这里是"复查"
                detail = '本机没有蓝牙适配器 · %d 秒后复查' % backoff
            else:
                detail = '设备断开，%d 秒后重连 …' % backoff
            r_title, r_hint = reason_text(reason) if reason else ('', '')
            self._set(status='reconnecting', detail=detail,
                      reason=reason, reason_title=r_title, hint=r_hint)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, info['cap'])

    async def _close(self):
        ctl, self._ctl = self._ctl, None
        if ctl is None:
            return
        try:
            await ctl.stop_all_notifications()
        except Exception:
            pass
        try:
            await ctl.disconnect()
        except Exception:
            pass

    async def _connect_and_auth(self):
        from cuk_ble.controller import CuktechBLEController

        self._set(status='connecting', detail='正在连接 %s ...' % self.mac)
        ctl = CuktechBLEController(mac=self.mac, token=self.token)
        await ctl.connect()
        await ctl.read_device_info()

        self._set(status='connecting', detail='正在认证 ...')
        ok = await ctl.authenticate()
        if not ok:
            raise RuntimeError('认证失败 — Token 可能不正确')

        self._ctl = ctl
        dev = {
            'mac': self.mac,
            'name': self.name,
            'model': ctl.device_model or '',
            'fw': ctl.firmware_version or '',
            'protoVer': '',
        }
        self._set(status='ok', detail='已认证 · 实时监控中', device=dev,
                  reason='', reason_title='', hint='', last_error='')
        print('[bridge] 已连接并认证: %s' % (dev['model'] or self.mac))

    async def _session_loop(self):
        ctl = self._ctl
        await self._read_settings()

        while not self._stop:
            t0 = time.time()
            await self._drain_commands()

            for piid in PORT_PIIDS:
                try:
                    await self._poll_port(piid)
                except Exception as e:
                    msg = str(e) or type(e).__name__
                    print('[bridge] 读取 PIID %d 失败: %s' % (piid, msg), file=sys.stderr)
                await asyncio.sleep(0.04)

            await self._drain_commands()

            if not ctl.client or not ctl.client.is_connected:
                raise RuntimeError('蓝牙链路已断开')

            await self._idle_wait(max(0.2, self._poll_interval() - (time.time() - t0)))

    async def _idle_wait(self, seconds):
        """自适应等待：没人看页面时长时间休眠，网页一上来立刻恢复。

        seconds <= 2 时直接睡（活跃态零额外唤醒）；
        否则按 0.5s 分片，等待中途被 touch() 打断。
        """
        if seconds <= 2:
            await asyncio.sleep(seconds)
            return
        deadline = time.time() + seconds
        while time.time() < deadline and not self._stop:
            if self._wake:
                self._wake = False
                return
            await asyncio.sleep(0.5)

    async def _read_settings(self):
        for piid in SETTING_PIIDS:
            try:
                r = await self._ctl.send_miot_command(SIID, piid)
                if r and r.get('value') is not None:
                    self._set_setting(piid, r['value'])
            except Exception as e:
                print('[bridge] 读取设置 PIID %d 失败: %s' % (piid, e), file=sys.stderr)
            await asyncio.sleep(0.05)

    async def _poll_port(self, piid):
        from cuk_ble.state_protocol_v2 import decode_port

        r = await self._ctl.send_miot_command(SIID, piid)
        if not r:
            return
        raw = r.get('raw')
        if raw is None:
            val = r.get('value')
            if isinstance(val, int) and val:
                raw = val.to_bytes(4, 'little')
            else:
                return
        data = decode_port(piid, bytes(raw))
        if data:
            self._set_port(piid, data)

    async def _drain_commands(self):
        while True:
            try:
                piid, value = self._cmd.get_nowait()
            except queue.Empty:
                return
            try:
                await self._ctl.send_miot_command(SIID, piid, value)
                self._set_setting(piid, value)
                print('[bridge] 已设置 PIID %d = %s' % (piid, value))
            except Exception as e:
                print('[bridge] 设置 PIID %d 失败: %s' % (piid, e), file=sys.stderr)


# ------------------------------------------------------------------ HTTP
class Handler(SimpleHTTPRequestHandler):
    # HTTP/1.1 长连接：页面每秒一次轮询走同一条 TCP，不再反复握手/堆积 TIME_WAIT
    protocol_version = 'HTTP/1.1'
    bridge = None

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/state':
            self.bridge.touch()
            self._json(self.bridge.snapshot())
            return
        if path == '/api/health':
            self._json({'ok': True})
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path != '/api/set':
            self.send_error(404)
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            payload = json.loads(self.rfile.read(n) or b'{}')
            self.bridge.send(payload['piid'], payload.get('value'))
            self._json({'ok': True})
        except Exception as e:
            self._json({'ok': False, 'error': str(e) or type(e).__name__}, 400)

    def end_headers(self):
        # 静态资源用 no-cache：文件没变由 Last-Modified 走 304，不再整包重传
        if not self.path.startswith('/api/'):
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


def has_ble_deps():
    """bleak + cryptography 是否可用（缺任一个都只能做静态服务）。"""
    try:
        import bleak  # noqa: F401
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


# ------------------------------------------------------------------ 入口
def main():
    # 输出重定向到 bridge.log 时也保持实时写入，启动失败才能看到真实原因
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    ap = argparse.ArgumentParser(description='CUKTECH charger local bridge')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--config', default=os.path.join(HERE, 'device-config.json'))
    ap.add_argument('--serve-only', action='store_true', help='只做静态服务，不连蓝牙')
    args = ap.parse_args()

    if not args.serve_only and not has_ble_deps():
        print('[bridge] 缺少 bleak / cryptography，降级为仅静态服务', file=sys.stderr)
        args.serve_only = True

    bridge = None
    if args.serve_only:
        bridge = Bridge('', b'')
        bridge._set(status='disabled', detail='仅静态服务')
    else:
        try:
            mac, token, dev_name = load_config(args.config)
        except SystemExit as e:
            print(e, file=sys.stderr)
            print('[bridge] 将以「仅静态服务」模式启动（网页可回退到浏览器蓝牙模式）',
                  file=sys.stderr)
            bridge = Bridge('', b'')
            bridge._set(status='disabled', detail='缺少设备配置')
        else:
            bridge = Bridge(mac, token, dev_name)
            bridge.start()

    Handler.bridge = bridge
    httpd = ThreadingHTTPServer((args.host, args.port),
                                partial(Handler, directory=HERE))
    httpd.daemon_threads = True          # 退出时不被长连接线程拖住
    print('[bridge] http://%s:%d/  (Ctrl+C 停止)' % (args.host, args.port))
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print('\n[bridge] 停止中 ...')
    finally:
        bridge.stop()
        httpd.server_close()


if __name__ == '__main__':
    main()
