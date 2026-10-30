// ble.js — CUKTECH 充电器 BLE 协议层 (Web Bluetooth)
// 完整对齐 cuktech-ble-ha/ble-server/src/cuktech_ble/controller.py 的认证 + 命令流程

import {
  randomBytes, hexToBytes, bytesToHex, u32le, u16le, concat,
  hkdfSha256, hmacSha256, deriveSessionKeys,
  aesCcmEncrypt, aesCcmDecrypt,
} from './crypto.js';

// ---------- 协议常量 (对齐 protocol.py) ----------
export const UUID_FE95        = '0000fe95-0000-1000-8000-00805f9b34fb';
export const CHAR_DEVICE_INFO = '0000001c-0000-1000-8000-00805f9b34fb';
export const CHAR_AUTH_CTRL   = '00000010-0000-1000-8000-00805f9b34fb';
export const CHAR_AUTH_DATA   = '00000019-0000-1000-8000-00805f9b34fb';
export const CHAR_CMD_SEND    = '0000001a-0000-1000-8000-00805f9b34fb';
export const CHAR_CMD_RECV    = '0000001b-0000-1000-8000-00805f9b34fb';
export const CHAR_FW_VERSION  = '00000004-0000-1000-8000-00805f9b34fb';

export const SIID_CHARGER = 2;

// MiOT PIID 定义
export const PIID = {
  C1: 1, C2: 2, C3: 3, A: 4,
  SCENE: 5, SCREEN: 6, PROTO_CTL: 7, TIMER_SET: 8,
  TIMER_C1: 9, TIMER_C2: 10, TIMER_C3: 11, TIMER_A: 12,
  LANG: 13, ENTER_UI: 14, USB_A_LOW: 15, PORT_CTL: 16,
  HW_PROTO_C1C2: 17, HW_PROTO_C3A: 18,
  IDLE_SCREEN: 19, SCREEN_LOCK: 20, PROTO_EXTEND: 21,
};

// 协议号 → 名称 (米家编制)
const MIJIA_PROTOCOLS = {
  0: 'idle', 1: '5V', 2: '5V', 3: 'QC', 4: 'AFC',
  5: 'FCP', 6: 'SCP', 7: 'PD', 8: 'PPS', 9: 'PPS', 10: 'UFCS',
};

// 端口 PIID → 名称
export const PORT_NAMES = { 1: 'c1', 2: 'c2', 3: 'c3', 4: 'a' };
export const PORT_PIIDS = [1, 2, 3, 4];

// 电压兜底协议号
function fallbackProtoByVoltage(piid, voltage) {
  if (piid === 4) return voltage > 5.5 ? 3 : 1;
  if (piid === 3) {
    if (voltage >= 15.0) return 7;
    return voltage >= 8.5 ? 3 : 1;
  }
  if (voltage <= 5.5) return 1;
  const PD_FIXED = [5.0, 9.0, 12.0, 15.0, 20.0];
  if (PD_FIXED.some(v => Math.abs(voltage - v) < 0.25)) return 7;
  return 8;
}

// ---------- Notify Queue (Promise-based) ----------
class NotifyQueue {
  constructor() { this.buf = []; this.waiter = null; }
  push(data) {
    if (this.waiter) {
      const w = this.waiter; this.waiter = null;
      clearTimeout(w.timer);
      w.resolve(data);
    } else {
      this.buf.push(data);
      if (this.buf.length > 512) this.buf.splice(0, this.buf.length - 256);
    }
  }
  async pop(timeoutMs) {
    if (this.buf.length) return this.buf.shift();
    return new Promise((resolve) => {
      const w = { resolve, timer: null };
      w.timer = setTimeout(() => {
        if (this.waiter === w) this.waiter = null;
        resolve(null);
      }, timeoutMs);
      this.waiter = w;
    });
  }
  drain() { this.buf = []; }       // 清空已积压数据, 不影响当前等待
  clear() {
    this.buf = [];
    if (this.waiter) { clearTimeout(this.waiter.timer); this.waiter = null; }
  }
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// 判定两个 Uint8Array 内容相等
function bytesEqual(a, b) {
  if (!a || !b) return false;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

// ============================================================
// CuktechBLE 主控制器
// ============================================================
export class CuktechBLE {
  constructor({ mac, token, log }) {
    this.mac = mac;
    this.token = token;  // Uint8Array 12B
    this.log = log || (() => {});
    this.device = null;
    this.server = null;
    this.service = null;
    this.chars = {};
    this.queues = {
      dev_info: new NotifyQueue(),
      auth_ctrl: new NotifyQueue(),
      auth_data: new NotifyQueue(),
      cmd_send: new NotifyQueue(),
      cmd_recv: new NotifyQueue(),
    };
    this.authenticated = false;
    this._sendIt = 0;
    this._devItHi = 0;
    this._lastDevItLo = 0;
    this._miotSeq = 1;
    this._keys = null;
    this._mtu = 23;
    this.deviceModel = '';
    this.firmwareVersion = '';
    this.frameCount = 0;
    // 端口推送回调: async function(piid, pt) — 解码后的明文 MiOT 帧
    this.onPortPush = null;
    // 硬件协议号缓存 (PIID 17/18)
    this.hwProto = { c1: 0, c2: 0, c3: 0, a: 0 };
    // 设备断连回调
    this.onDisconnect = null;
    this._disconnecting = false;
  }

  // ---------- Web Bluetooth 连接 ----------
  // device 由 app.js 在用户手势里 requestDevice 后传入
  async connect(device) {
    this.log('i', `连接 ${device.name || device.id} ...`);
    this.device = device;
    device.addEventListener('gattserverdisconnected', () => {
      if (this._disconnecting) return;
      this.log('w', 'BLE 链路断开');
      this.authenticated = false;
      if (this.onDisconnect) this.onDisconnect();
    });

    this.server = await device.gatt.connect();
    this.log('i', 'GATT 已连接, 获取服务 0xFE95 ...');
    this.service = await this.server.getPrimaryService(UUID_FE95);

    // 获取所有特征
    const charUUIDs = [
      [CHAR_DEVICE_INFO, 'dev_info'],
      [CHAR_AUTH_CTRL, 'auth_ctrl'],
      [CHAR_AUTH_DATA, 'auth_data'],
      [CHAR_CMD_SEND, 'cmd_send'],
      [CHAR_CMD_RECV, 'cmd_recv'],
      [CHAR_FW_VERSION, 'fw'],
    ];
    for (const [uuid, name] of charUUIDs) {
      try {
        this.chars[name] = await this.service.getCharacteristic(uuid);
      } catch (e) {
        this.log('w', `特征 ${name} 获取失败: ${e.message}`);
      }
    }

    // 订阅通知通道 (对齐 Python connect() 的顺序)
    // 第一批: cmd_recv, cmd_send, dev_info
    // 第二批: auth_data
    // (auth_ctrl 延迟到 Phase B CMD_LOGIN 之前订阅, 对齐米家时序)
    for (const [charName, queueName] of [
      ['cmd_recv', 'cmd_recv'],
      ['cmd_send', 'cmd_send'],
      ['dev_info', 'dev_info'],
    ]) {
      await this._subscribe(charName, queueName);
    }
    await this._subscribe('auth_data', 'auth_data');
    this.log('i', '通知通道已订阅 (auth_ctrl 延迟)');
    return true;
  }

  async _subscribe(charName, queueName) {
    const ch = this.chars[charName];
    if (!ch) return;
    const q = this.queues[queueName];
    ch.addEventListener('characteristicvaluechanged', (evt) => {
      const dv = evt.target.value;
      const data = new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength);
      this.frameCount++;
      q.push(data);
    });
    try {
      await ch.startNotifications();
    } catch (e) {
      this.log('w', `订阅 ${charName} 失败: ${e.message}`);
    }
  }

  async _write(charName, data) {
    const ch = this.chars[charName];
    if (!ch) throw new Error(`特征 ${charName} 不存在`);
    // 优先 writeValueWithoutResponse (MiOT 协议要求 No-Response 写入)
    if (ch.properties.writeWithoutResponse) {
      await ch.writeValueWithoutResponse(data);
    } else if (ch.properties.write) {
      await ch.writeValue(data);
    } else {
      throw new Error(`特征 ${charName} 不可写`);
    }
  }

  async waitNotify(name, timeout = 3000) {
    return await this.queues[name].pop(timeout);
  }

  // ---------- 设备信息 (无需认证) ----------
  async readDeviceInfo() {
    this.log('i', '读取设备信息 ...');
    const chInfo = this.chars.dev_info;
    const chFw = this.chars.fw;

    if (chInfo) {
      // 协议版本
      await this._write('dev_info', new Uint8Array([0x00]));
      const v = await this.waitNotify('dev_info', 3000);
      if (v && v.length > 2) {
        this.protoVer = `v${v[1]}.${v[2]}`;
        this.log('i', `协议版本: ${this.protoVer}`);
      }
      // 芯片信息
      await this._write('dev_info', new Uint8Array([0x03]));
      const c = await this.waitNotify('dev_info', 3000);
      if (c && c.length > 2) {
        const len = c[1];
        const chip = new TextDecoder('ascii').decode(c.slice(2, 2 + len));
        this.deviceModel = `njcuk.fitting.ad1204_${chip}`;
        this.log('i', `芯片: ${chip} → ${this.deviceModel}`);
      }
    }
    if (chFw) {
      try {
        const dv = await chFw.readValue();
        const arr = new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength);
        let end = arr.length;
        while (end > 0 && arr[end - 1] === 0) end--;
        this.firmwareVersion = new TextDecoder('ascii').decode(arr.slice(0, end));
        this.log('i', `固件: ${this.firmwareVersion}`);
      } catch (e) {
        this.log('w', `读固件失败: ${e.message}`);
      }
    }
  }

  // ---------- 认证流程 ----------
  async authenticate() {
    this.log('i', '[认证] 开始 MiOT BLE 认证 ...');
    const ok = await this._tryAuthenticate();
    if (ok) {
      this.log('o', '认证成功! 命令通道就绪');
      this.authenticated = true;
    } else {
      this.log('e', '认证失败');
    }
    return ok;
  }

  async _tryAuthenticate() {
    // ---- Phase A: 设备初始化 (0xa4) ----
    this.queues.auth_data.clear();
    this.log('i', '[认证 1/5] 设备初始化 (0xa4) ...');
    await this._write('auth_ctrl', new Uint8Array([0xa4]));

    let initResp = await this.waitNotify('auth_data', 3000);
    if (!initResp) { this.log('w', '未收到初始化响应'); return false; }

    // 回传 byte[2]+1
    const ack = initResp.slice();
    if (ack.length >= 3) ack[2] = (ack[2] + 1) & 0xff;
    await this._write('auth_data', ack);

    // 接收密钥交换数据
    let keyData = await this.waitNotify('auth_data', 5000);

    // 异常处理: 设备状态不同步 (短帧 byte[2]=0x04)
    const isDesync = keyData && keyData.length < 20 && keyData.length >= 3 && keyData[2] === 0x04;
    if (isDesync) {
      this.log('w', `[!] 设备状态不同步, 等待恢复 ...`);
      const deadline = Date.now() + 12000;
      for (let i = 0; i < 3; i++) {
        const remain = deadline - Date.now();
        if (remain <= 0) break;
        const extra = await this.waitNotify('auth_data', Math.min(remain, 5000));
        if (extra && extra.length >= 20 && extra[2] === 0x04) {
          keyData = extra;
          break;
        }
      }
      if (!keyData || keyData.length < 20) {
        this.log('w', '设备未恢复, 放弃认证');
        return false;
      }
    } else if (!keyData) {
      this.log('w', '未收到密钥交换数据');
      return false;
    }

    // 回传占位 (与 BLE 日志一致: 0xf2 填充)
    const padLen = Math.max(0, keyData.length - 4);
    const placeholder = concat(new Uint8Array([0x00, 0x00, 0x05, 0x01]),
                                new Uint8Array(padLen).fill(0xf2));
    await this._write('auth_data', placeholder);

    // 两次 drain (消耗残留通知)
    for (const wait of [300, 300]) {
      await sleep(wait);
      this.queues.auth_data.drain();
    }

    // ---- Phase B: CMD_LOGIN ----
    try {
      await this._subscribe('auth_ctrl', 'auth_ctrl');
    } catch (e) { /* 可能已订阅 */ }

    this.log('i', '[认证 2/5] 发送 CMD_LOGIN (0x24) ...');
    await sleep(50);
    await this._write('auth_ctrl', new Uint8Array([0x24, 0x00, 0x00, 0x00]));

    // ---- 发送我方随机密钥 ----
    this.log('i', '[认证 3/5] 发送随机密钥 (16B) ...');
    const randKey = randomBytes(16);
    await this._write('auth_data', new Uint8Array([0x00, 0x00, 0x00, 0x0b, 0x01, 0x00]));

    // 等待 RCV_RDY (重试 5 次, 跳过残留 key exchange 数据)
    let got = null;
    for (let r = 0; r < 5; r++) {
      const d = await this.waitNotify('auth_data', 3000);
      if (bytesEqual(d, new Uint8Array([0x00, 0x00, 0x01, 0x01]))) { got = d; break; }
      if (d) this.log('i', `  Phase B: 跳过 ${d.length}B (期待 RCV_RDY)`);
    }
    if (!got) { this.log('w', '未收到 RCV_RDY'); return false; }

    // 发送随机密钥 (帧头 0100 + 16B)
    await this._write('auth_data', concat(new Uint8Array([0x01, 0x00]), randKey));

    // 等待 RCV_OK
    const ok1 = await this.waitNotify('auth_data', 3000);
    if (!bytesEqual(ok1, new Uint8Array([0x00, 0x00, 0x01, 0x00]))) {
      this.log('w', `未收到 RCV_OK, got=${ok1 ? bytesToHex(ok1) : 'null'}`);
      return false;
    }

    // ---- 接收设备随机密钥 ----
    this.log('i', '[认证 4/5] 接收设备响应 ...');
    let devRandom = await this._recvAuthResponse('auth_data');
    if (!devRandom || devRandom.length < 16) {
      this.log('w', `设备密钥无效: ${devRandom ? devRandom.length : 0}B`);
      return false;
    }
    devRandom = devRandom.slice(0, 16);

    // 接收设备 HMAC (32B)
    let devHmac = await this._recvAuthResponse('auth_data');
    if (!devHmac || devHmac.length < 32) {
      this.log('w', `设备 HMAC 无效: ${devHmac ? devHmac.length : 0}B`);
      return false;
    }
    devHmac = devHmac.slice(0, 32);

    // ---- 派生会话密钥 + 验证 ----
    this._keys = await deriveSessionKeys(this.token, randKey, devRandom);
    this.log('i', '  会话密钥已派生');

    const expectedDevHmac = await hmacSha256(this._keys.dev_key, concat(devRandom, randKey));
    if (!bytesEqual(expectedDevHmac, devHmac)) {
      this.log('e', '设备 HMAC 验证失败! (token 可能不对)');
      return false;
    }
    this.log('i', '  设备 HMAC 验证通过');

    // 计算我方 HMAC
    const ourHmac = await hmacSha256(this._keys.app_key, concat(randKey, devRandom));

    this.log('i', '[认证 5/5] 发送认证确认 ...');
    await this._write('auth_data', new Uint8Array([0x00, 0x00, 0x00, 0x0a, 0x01, 0x00]));

    // 等待 RCV_RDY
    const rdy = await this.waitNotify('auth_data', 3000);
    if (!bytesEqual(rdy, new Uint8Array([0x00, 0x00, 0x01, 0x01]))) {
      this.log('w', `未收到 RCV_RDY, got=${rdy ? bytesToHex(rdy) : 'null'}`);
      return false;
    }

    // 发送 HMAC (帧号 0100 + 32B)
    await this._write('auth_data', concat(new Uint8Array([0x01, 0x00]), ourHmac));

    // 等待 RCV_OK
    const ok2 = await this.waitNotify('auth_data', 3000);
    if (ok2) this.log('i', `  Phase5 ACK: ${bytesToHex(ok2)}`);

    // ---- 第二轮 challenge/response ----
    await this._phase6ChallengeResponse();

    // ---- 等待认证结果 ----
    const result = await this.waitNotify('auth_ctrl', 5000);
    if (!result || result.length === 0) {
      this.log('w', '未收到认证结果');
      return false;
    }
    const frm = result[0];
    if (frm === 0x21 || frm === 0x11) {
      this.log('o', `认证成功 (0x${frm.toString(16)})`);
      await this._setupCmdChannel();
      return true;
    } else if (frm === 0x23) {
      this.log('e', '登录失败 (0x23)');
    } else if (frm === 0x12) {
      this.log('e', '激活失败 (0x12)');
    } else {
      this.log('w', `未知结果: 0x${frm.toString(16)}`);
    }
    return false;
  }

  async _phase6ChallengeResponse() {
    try {
      const deadline = Date.now() + 8000;
      let challengeData = null, responseData = null;
      while (Date.now() < deadline) {
        const remain = deadline - Date.now();
        const data = await this.waitNotify('auth_data', Math.min(remain, 3000));
        if (!data) continue;

        if (data.length >= 3 && data[2] === 0x0d) {
          // challenge
          challengeData = data;
          await this._write('auth_data', new Uint8Array([0x00, 0x00, 0x03, 0x00]));
        } else if (data.length >= 3 && data[2] === 0x0c) {
          // response
          responseData = data;
          await this._write('auth_data', new Uint8Array([0x00, 0x00, 0x03, 0x00]));
          await this._write('auth_data', new Uint8Array([0x00, 0x00, 0x00, 0x0a, 0x01, 0x00]));
        } else if (bytesEqual(data, new Uint8Array([0x00, 0x00, 0x01, 0x01]))) {
          break; // RCV_RDY
        } else if (bytesEqual(data, new Uint8Array([0x00, 0x00, 0x01, 0x00]))) {
          break; // RCV_OK
        }
      }

      if (responseData) {
        // 第二轮 auth response: header 0100 + opcode 0c + data[3:]
        const payload = concat(new Uint8Array([0x01, 0x00, 0x0c]), responseData.slice(3));
        await this._write('auth_data', payload);
        await this.waitNotify('auth_data', 3000);
      }
    } catch (e) {
      this.log('i', `  Phase6 异常 (继续): ${e.message}`);
    }
  }

  // 接收认证响应 (内联或多帧格式)
  async _recvAuthResponse(channel) {
    const data = await this.waitNotify(channel, 3000);
    if (!data || data.length < 4) return null;

    if (data[2] === 0x02) {
      // 内联: 0000 02 XX [data]
      const payload = data.slice(4);
      await this._write('auth_data', new Uint8Array([0x00, 0x00, 0x03, 0x00]));
      return payload;
    } else if (data[2] === 0x00 && data.length >= 6) {
      // 多帧头: 0000 00 XX count_lo count_hi
      const frameCount = Math.min(data[4] + 0x100 * data[5], 100);
      await this._write('auth_data', new Uint8Array([0x00, 0x00, 0x01, 0x01]));
      let received = new Uint8Array(0);
      for (let i = 0; i < frameCount; i++) {
        const frame = await this.waitNotify(channel, 3000);
        if (!frame) break;
        received = concat(received, frame.slice(2));
      }
      await this._write('auth_data', new Uint8Array([0x00, 0x00, 0x01, 0x00]));
      return received;
    }
    this.log('w', `未知 auth response 格式: data[2]=0x${data[2].toString(16)}, len=${data.length}`);
    return null;
  }

  // ---------- 命令通道初始化 ----------
  async _setupCmdChannel() {
    this._sendIt = 0;
    this._devItHi = 0;
    this._lastDevItLo = 0;
    this._miotSeq = 1;
    await sleep(500);
    await this._drainPendingPushes();
    await this._drainDevicePush();
  }

  async _drainPendingPushes() {
    const q = this.queues.cmd_recv;
    if (!q.buf.length) return;
    // 复制并清空 buffer (避免 push 时无限循环)
    const items = q.buf.splice(0);
    for (const data of items) {
      if (data && data.length >= 4 && data[2] === 0x02) {
        await this._write('cmd_recv', new Uint8Array([0x00, 0x00, 0x03, 0x00]));
      } else if (data && data.length >= 6 && data[2] === 0x00) {
        const fc = Math.min(data[4] + 0x100 * data[5], 100);
        await this._write('cmd_recv', new Uint8Array([0x00, 0x00, 0x01, 0x01]));
        for (let i = 0; i < fc; i++) {
          const f = await this.waitNotify('cmd_recv', 2000);
          if (!f) break;
        }
        await this._write('cmd_recv', new Uint8Array([0x00, 0x00, 0x01, 0x00]));
      }
    }
  }

  async _drainDevicePush() {
    const start = Date.now();
    const maxSec = 6;
    const maxCount = 60;
    let count = 0;
    while (count < maxCount && Date.now() - start < maxSec * 1000) {
      const data = await this.waitNotify('cmd_recv', 800);
      if (!data) break;
      count++;
      if (data[2] === 0x00 && data.length >= 6) {
        const fc = Math.min(data[4] + 0x100 * data[5], 100);
        await this._write('cmd_recv', new Uint8Array([0x00, 0x00, 0x01, 0x01]));
        for (let i = 0; i < fc; i++) {
          const f = await this.waitNotify('cmd_recv', 3000);
          if (!f) break;
        }
        await this._write('cmd_recv', new Uint8Array([0x00, 0x00, 0x01, 0x00]));
      } else if (data[2] === 0x02) {
        // inline 帧 — 尝试解码并转发 (可能是初始端口推送)
        await this._tryDecodeInline(data);
      }
    }
  }

  // ---------- 加解密 ----------
  _encrypt(plaintext) {
    const it = this._sendIt;
    const itBytes = u32le(it);
    const nonce = concat(this._keys.app_iv, new Uint8Array(4), itBytes);
    const ct = aesCcmEncrypt(this._keys.app_key, nonce, plaintext, null, 4);
    this._sendIt++;
    return concat(u16le(it), ct);  // it 低 2 字节 + ciphertext+tag
  }

  _decrypt(data) {
    if (data.length < 6) return null;
    const itLo = (data[0] | (data[1] << 8)) >>> 0;
    if (itLo < this._lastDevItLo && (this._lastDevItLo - itLo) > 32768) {
      this._devItHi++;
    }
    this._lastDevItLo = itLo;
    const it = ((this._devItHi << 16) | itLo) >>> 0;
    const itBytes = u32le(it);
    const nonce = concat(this._keys.dev_iv, new Uint8Array(4), itBytes);
    const ct = data.slice(2);
    try {
      return aesCcmDecrypt(this._keys.dev_key, nonce, ct, null, 4);
    } catch (e) {
      this.log('w', `解密失败: ${e.message}`);
      return null;
    }
  }

  // ---------- 发送加密命令 ----------
  async _sendEncrypted(plaintext) {
    this.queues.cmd_send.drain();  // 清空过期响应

    const encrypted = this._encrypt(plaintext);

    // 写头部: 告知设备要发送 1 帧
    await this._write('cmd_send', new Uint8Array([0x00, 0x00, 0x00, 0x00, 0x01, 0x00]));

    // 等待 RCV_RDY
    const rdy = await this.waitNotify('cmd_send', 3000);
    if (!bytesEqual(rdy, new Uint8Array([0x00, 0x00, 0x01, 0x01]))) {
      this.log('w', `CMD_SEND 无 RCV_RDY: ${rdy ? bytesToHex(rdy) : 'null'}`);
      return false;
    }

    // 发送数据帧 (帧号 0100 + 加密数据)
    await this._write('cmd_send', concat(new Uint8Array([0x01, 0x00]), encrypted));

    // 等待 RCV_OK
    const ok = await this.waitNotify('cmd_send', 3000);
    if (!bytesEqual(ok, new Uint8Array([0x00, 0x00, 0x01, 0x00]))) {
      this.log('w', `CMD_SEND 无 RCV_OK: ${ok ? bytesToHex(ok) : 'null'}`);
      return false;
    }
    return true;
  }

  // 构造 MiOT TLV 明文
  _buildMiotTlv(seq, siid, piid, value) {
    let opcode, typeId, valueBytes;
    if (value !== null && value !== undefined) {
      opcode = 0x00;  // SET
      if (value <= 0xff) {
        typeId = 1; valueBytes = new Uint8Array([value & 0xff]);
      } else {
        typeId = 5; valueBytes = u32le(value >>> 0);
      }
    } else {
      opcode = 0x02;  // GET
      typeId = 1; valueBytes = new Uint8Array([0x00]);
    }
    const byteLen = valueBytes.length;
    const tl = (typeId << 12) | byteLen;
    const totalLen = 11 + byteLen;
    return concat(
      new Uint8Array([
        totalLen & 0xff, 0x20, seq & 0xff, 0x00,
        opcode, 0x01, siid & 0xff,
        piid & 0xff, (piid >> 8) & 0xff,
        tl & 0xff, (tl >> 8) & 0xff,
      ]),
      valueBytes
    );
  }

  // 发送 MiOT 命令
  async sendMiotCommand(siid, piid, value = null) {
    if (!this.authenticated) { this.log('w', '未认证, 不能发命令'); return null; }

    await this._drainPendingPushes();

    const seq = this._miotSeq;
    this._miotSeq = (this._miotSeq + 1) & 0xff;

    const plaintext = this._buildMiotTlv(seq, siid, piid, value);
    if (!await this._sendEncrypted(plaintext)) return null;

    if (value !== null && value !== undefined) {
      return await this._recvSetResponse(siid, piid, 8000);
    } else {
      return await this._recvGetResponse(siid, piid, 8000);
    }
  }

  // SET 响应: 期望 ACK (b4=0x01) + Result (b4=0x04)
  async _recvSetResponse(siid, piid, timeout) {
    const deadline = Date.now() + timeout;
    let gotAck = false;
    while (true) {
      const remain = deadline - Date.now();
      if (remain <= 0) break;
      const data = await this.waitNotify('cmd_recv', Math.min(remain, 3000));
      if (!data || data.length < 4) {
        if (gotAck) break;
        continue;
      }
      const { pt } = await this._tryDecodeInline(data);
      if (!pt) continue;
      if (pt.length < 8) continue;
      const b4 = pt[4];
      const ptSiid = pt[6];
      const ptPiid = pt[7];
      if (b4 === 0x01 && ptSiid === (siid & 0xff) && ptPiid === (piid & 0xff)) {
        gotAck = true;
        continue;
      } else if (b4 === 0x04 && ptSiid === (siid & 0xff) && ptPiid === (piid & 0xff)) {
        let val = null;
        if (pt.length >= 14) {
          const vlen = pt[11];
          if (vlen >= 4 && pt.length >= 17) {
            val = (pt[13] | (pt[14] << 8) | (pt[15] << 16) | (pt[16] << 24)) >>> 0;
          } else if (pt.length > 13) {
            val = pt[13];
          }
        }
        return { piid, value: val, raw: pt };
      }
      // 推送帧 (b4=0x04, 不匹配本命令) → 转发
      if (b4 === 0x04) {
        if (this.onPortPush) try { await this.onPortPush(pt); } catch (e) { this.log('w', `onPortPush: ${e.message}`); }
        continue;
      }
    }
    if (gotAck) return { piid, value: null, ackOnly: true };
    return null;
  }

  // GET 响应: 期望 b4=0x03, 值在 pt[13] 或 pt[13:17]
  async _recvGetResponse(siid, piid, timeout) {
    const deadline = Date.now() + timeout;
    while (true) {
      const remain = deadline - Date.now();
      if (remain <= 0) break;
      const data = await this.waitNotify('cmd_recv', Math.min(remain, 3000));
      if (!data || data.length < 4) break;
      const { pt } = await this._tryDecodeInline(data);
      if (!pt) continue;
      if (pt.length < 8) continue;
      const b4 = pt[4];
      const ptSiid = pt[6];
      const ptPiid = pt[7];
      if (b4 === 0x03 && ptSiid === (siid & 0xff) && ptPiid === (piid & 0xff)) {
        // 错误码 pt[9]/pt[10]
        if (pt.length > 10 && (pt[9] || pt[10])) {
          this.log('w', `GET siid=${siid} piid=${piid} 设备错误 0x${pt[9].toString(16)}${pt[10].toString(16)}`);
          return null;
        }
        let val = null;
        if (pt.length >= 14) {
          const vlen = pt[11];
          if (vlen >= 4 && pt.length >= 17) {
            val = (pt[13] | (pt[14] << 8) | (pt[15] << 16) | (pt[16] << 24)) >>> 0;
          } else if (pt.length > 13) {
            val = pt[13];
          }
        }
        return { piid, value: val, raw: pt };
      }
      if (b4 === 0x04) {
        if (this.onPortPush) try { await this.onPortPush(pt); } catch (e) { this.log('w', `onPortPush: ${e.message}`); }
        continue;
      }
    }
    return null;
  }

  // 解码 inline / multiframe 帧
  async _tryDecodeInline(data) {
    if (data[2] === 0x02 && data.length >= 4) {
      // inline: 0000 02 XX [encrypted]
      const enc = data.slice(4);
      await this._write('cmd_recv', new Uint8Array([0x00, 0x00, 0x03, 0x00]));
      const pt = this._decrypt(enc);
      return { pt: (pt && pt.length >= 8) ? pt : null, ackMulti: false };
    } else if (data[2] === 0x00 && data.length >= 6) {
      // multiframe
      const fc = Math.min(data[4] + 0x100 * data[5], 100);
      await this._write('cmd_recv', new Uint8Array([0x00, 0x00, 0x01, 0x01]));
      let payload = new Uint8Array(0);
      const deadline = Date.now() + 30000;
      for (let i = 0; i < fc; i++) {
        const remain = deadline - Date.now();
        if (remain <= 0) break;
        const frame = await this.waitNotify('cmd_recv', Math.min(remain, 3000));
        if (!frame) break;
        payload = concat(payload, frame.slice(2));
      }
      await this._write('cmd_recv', new Uint8Array([0x00, 0x00, 0x01, 0x00]));
      if (!payload.length) return { pt: null, ackMulti: true };
      const pt = this._decrypt(payload);
      return { pt: (pt && pt.length >= 8) ? pt : null, ackMulti: true };
    }
    return { pt: null, ackMulti: false };
  }

  // ---------- 端口数据解析 ----------
  // pt 是解密后的 MiOT 明文帧 (≥12B), 最后 4 字节 = [status_raw, code, current_raw, voltage_raw]
  decodePort(piid, pt) {
    if (!pt || pt.length < 12) return null;
    const b = pt.slice(pt.length - 4);
    const inUse = !!b[0];
    const statusRaw = b[0];
    const code = b[1];
    const currentRaw = b[2];
    const voltageRaw = b[3];
    const voltage = voltageRaw / 10.0;
    const current = currentRaw / 10.0;
    const power = Math.round(voltage * current * 10) / 10;
    const active = inUse || voltage > 0 || current > 0;

    let protoNum;
    const hw = this.hwProto[PORT_NAMES[piid]] || 0;
    if (hw > 0) {
      if (hw <= 10) protoNum = hw;
      else {
        const map = { 0x70: 3, 0x80: 7 };
        protoNum = map[hw] || fallbackProtoByVoltage(piid, voltage);
      }
    } else {
      protoNum = fallbackProtoByVoltage(piid, voltage);
    }
    const protocol = active ? (MIJIA_PROTOCOLS[protoNum] || `Unknown(0x${protoNum.toString(16)})`) : 'idle';

    return {
      voltage: Math.round(voltage * 10) / 10,
      current: Math.round(current * 10) / 10,
      power,
      active,
      statusRaw,
      code,
      protocol,
      protoNum: active ? protoNum : null,
    };
  }

  // ---------- 断开 ----------
  async disconnect() {
    this._disconnecting = true;
    try {
      // 停止通知
      for (const charName of ['dev_info', 'auth_ctrl', 'auth_data', 'cmd_send', 'cmd_recv']) {
        const ch = this.chars[charName];
        if (ch) { try { await ch.stopNotifications(); } catch (e) {} }
      }
      if (this.device && this.device.gatt.connected) {
        this.device.gatt.disconnect();
      }
    } finally {
      this._disconnecting = false;
      this.authenticated = false;
      this.log('i', '已断开');
    }
  }
}
