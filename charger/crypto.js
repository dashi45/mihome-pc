// crypto.js — HKDF-SHA256 / HMAC-SHA256 (Web Crypto) + AES-CCM (自实现, 基于 aes-js raw AES block)
// 对齐 cuktech-ble-ha/ble-server/src/cuktech_ble/controller.py 的加密流程

const aesjs = window.aesjs;

// ---------- 工具 ----------
export function randomBytes(n) {
  const b = new Uint8Array(n);
  crypto.getRandomValues(b);
  return b;
}

export function hexToBytes(hex) {
  hex = String(hex).replace(/\s+/g, '').toLowerCase();
  if (!/^[0-9a-f]*$/.test(hex)) throw new Error('hex contains non-hex chars');
  if (hex.length % 2) throw new Error('hex length must be even');
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

export function bytesToHex(b) {
  return Array.from(b).map(x => x.toString(16).padStart(2, '0')).join('');
}

// little-endian uint32 → 4 bytes
export function u32le(n) {
  return new Uint8Array([n & 0xff, (n >> 8) & 0xff, (n >> 16) & 0xff, (n >>> 24) & 0xff]);
}

export function u16le(n) {
  return new Uint8Array([n & 0xff, (n >> 8) & 0xff]);
}

// big-endian length field (L bytes), 用于 CCM B0 / A0 counter
function numToBytesBE(n, len) {
  const b = new Uint8Array(len);
  let v = n;
  for (let i = len - 1; i >= 0; i--) {
    b[i] = v & 0xff;
    v = Math.floor(v / 256);
  }
  return b;
}

// ---------- HKDF-SHA256 (Web Crypto 原生) ----------
export async function hkdfSha256(ikm, salt, info, length) {
  // 与 Python: HKDF(SHA256(), length, salt, info).derive(token) 完全一致 (RFC 5869)
  const baseKey = await crypto.subtle.importKey('raw', ikm, { name: 'HKDF' }, false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt, info },
    baseKey,
    length * 8
  );
  return new Uint8Array(bits);
}

// ---------- HMAC-SHA256 (Web Crypto 原生) ----------
export async function hmacSha256(key, data) {
  const k = await crypto.subtle.importKey(
    'raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
  );
  const sig = await crypto.subtle.sign('HMAC', k, data);
  return new Uint8Array(sig);
}

// constant-time compare
function ctEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

// 包装: aes-js 的 encrypt 返回 Array, 统一转成 Uint8Array 避免类型混乱
const enc = (aes, block) => new Uint8Array(aes['encrypt'](block));

// ---------- AES-CCM (RFC 3610) ----------
// key: 16B (AES-128), nonce: 7-13B (本项目固定 12B), tag 长度可自定义 (本项目 4B)
// 输出格式与 cryptography 库 AESCCM 一致: ciphertext || tag (tag 在末尾)
export function aesCcmEncrypt(key, nonce, plaintext, aad = null, tagLen = 4) {
  const aes = new aesjs.AES(key);
  const L = 15 - nonce.length;
  if (L < 2 || L > 8) throw new Error(`invalid nonce length ${nonce.length}`);

  const hasAad = aad && aad.length > 0;
  const flags0 = ((tagLen - 2) / 2 << 3) | (hasAad ? 0x40 : 0) | (L - 1);

  // ---- CBC-MAC ----
  const b0 = new Uint8Array(16);
  b0[0] = flags0;
  b0.set(nonce, 1);
  b0.set(numToBytesBE(plaintext.length, L), 1 + nonce.length);
  let t = enc(aes, b0);

  // AAD 块 (本项目无 AAD, 但保留以备)
  if (hasAad) {
    const aadLen = aad.length;
    let aadHeader;
    if (aadLen < 0xff00) {
      aadHeader = new Uint8Array(2);
      aadHeader[0] = (aadLen >> 8) & 0xff;
      aadHeader[1] = aadLen & 0xff;
    } else {
      aadHeader = new Uint8Array(6);
      aadHeader[0] = 0xff; aadHeader[1] = 0xfe;
      aadHeader[2] = (aadLen >> 24) & 0xff; aadHeader[3] = (aadLen >> 16) & 0xff;
      aadHeader[4] = (aadLen >> 8) & 0xff; aadHeader[5] = aadLen & 0xff;
    }
    const aadPadded = new Uint8Array(Math.ceil((aadHeader.length + aadLen) / 16) * 16);
    aadPadded.set(aadHeader);
    aadPadded.set(aad, aadHeader.length);
    for (let off = 0; off < aadPadded.length; off += 16) {
      for (let i = 0; i < 16; i++) t[i] ^= aadPadded[off + i];
      t = enc(aes, t);
    }
  }

  // plaintext 块 (最后一块补零)
  const ptPadded = new Uint8Array(Math.ceil(plaintext.length / 16) * 16 || 16);
  ptPadded.set(plaintext);
  for (let off = 0; off < ptPadded.length; off += 16) {
    for (let i = 0; i < 16; i++) t[i] ^= ptPadded[off + i];
    t = enc(aes, t);
  }

  // ---- CTR ----
  const a0 = new Uint8Array(16);
  a0[0] = L - 1;
  a0.set(nonce, 1);
  // counter 初始 0 → S0 用于 tag
  const s0 = enc(aes, a0);

  const ciphertext = new Uint8Array(plaintext.length);
  let ctr = 1;
  for (let off = 0; off < plaintext.length; off += 16) {
    const ctrBlock = a0.slice();
    ctrBlock.set(numToBytesBE(ctr, L), 1 + nonce.length);
    const s = enc(aes, ctrBlock);
    const n = Math.min(16, plaintext.length - off);
    for (let i = 0; i < n; i++) ciphertext[off + i] = plaintext[off + i] ^ s[i];
    ctr++;
  }

  // ---- 最终 tag = T XOR S0[:tagLen] ----
  const tag = new Uint8Array(tagLen);
  for (let i = 0; i < tagLen; i++) tag[i] = t[i] ^ s0[i];

  const out = new Uint8Array(ciphertext.length + tagLen);
  out.set(ciphertext);
  out.set(tag, ciphertext.length);
  return out;
}

export function aesCcmDecrypt(key, nonce, ctWithTag, aad = null, tagLen = 4) {
  if (ctWithTag.length < tagLen) throw new Error('ciphertext too short for tag');
  const aes = new aesjs.AES(key);
  const L = 15 - nonce.length;
  if (L < 2 || L > 8) throw new Error(`invalid nonce length ${nonce.length}`);

  const ciphertext = ctWithTag.subarray(0, ctWithTag.length - tagLen);
  const tag = ctWithTag.subarray(ctWithTag.length - tagLen);

  // ---- CTR 解密 ----
  const a0 = new Uint8Array(16);
  a0[0] = L - 1;
  a0.set(nonce, 1);
  const s0 = enc(aes, a0);

  const plaintext = new Uint8Array(ciphertext.length);
  let ctr = 1;
  for (let off = 0; off < ciphertext.length; off += 16) {
    const ctrBlock = a0.slice();
    ctrBlock.set(numToBytesBE(ctr, L), 1 + nonce.length);
    const s = enc(aes, ctrBlock);
    const n = Math.min(16, ciphertext.length - off);
    for (let i = 0; i < n; i++) plaintext[off + i] = ciphertext[off + i] ^ s[i];
    ctr++;
  }

  // ---- 重算 CBC-MAC 验证 tag ----
  const hasAad = aad && aad.length > 0;
  const flags0 = ((tagLen - 2) / 2 << 3) | (hasAad ? 0x40 : 0) | (L - 1);
  const b0 = new Uint8Array(16);
  b0[0] = flags0;
  b0.set(nonce, 1);
  b0.set(numToBytesBE(plaintext.length, L), 1 + nonce.length);
  let t = enc(aes, b0);

  if (hasAad) {
    const aadLen = aad.length;
    let aadHeader;
    if (aadLen < 0xff00) {
      aadHeader = new Uint8Array(2);
      aadHeader[0] = (aadLen >> 8) & 0xff; aadHeader[1] = aadLen & 0xff;
    } else {
      aadHeader = new Uint8Array(6);
      aadHeader[0] = 0xff; aadHeader[1] = 0xfe;
      aadHeader[2] = (aadLen >> 24) & 0xff; aadHeader[3] = (aadLen >> 16) & 0xff;
      aadHeader[4] = (aadLen >> 8) & 0xff; aadHeader[5] = aadLen & 0xff;
    }
    const aadPadded = new Uint8Array(Math.ceil((aadHeader.length + aadLen) / 16) * 16);
    aadPadded.set(aadHeader);
    aadPadded.set(aad, aadHeader.length);
    for (let off = 0; off < aadPadded.length; off += 16) {
      for (let i = 0; i < 16; i++) t[i] ^= aadPadded[off + i];
      t = enc(aes, t);
    }
  }

  const ptPadded = new Uint8Array(Math.ceil(plaintext.length / 16) * 16 || 16);
  ptPadded.set(plaintext);
  for (let off = 0; off < ptPadded.length; off += 16) {
    for (let i = 0; i < 16; i++) t[i] ^= ptPadded[off + i];
    t = enc(aes, t);
  }

  const expectedTag = new Uint8Array(tagLen);
  for (let i = 0; i < tagLen; i++) expectedTag[i] = t[i] ^ s0[i];

  if (!ctEqual(expectedTag, tag)) throw new Error('AES-CCM authentication failed');
  return plaintext;
}

// ---------- CUKTECH 会话密钥派生 ----------
// 对齐 controller.py: HKDF(SHA256, length=64, salt=rand_key+dev_random, info="mible-login-info").derive(token)
// 输出 4 个 key: dev_key[0:16], app_key[16:32], dev_iv[32:36], app_iv[36:40]
export async function deriveSessionKeys(token, randKey, devRandom) {
  const salt = concat(randKey, devRandom);
  const info = new TextEncoder().encode('mible-login-info');
  const derived = await hkdfSha256(token, salt, info, 64);
  return {
    dev_key: derived.slice(0, 16),
    app_key: derived.slice(16, 32),
    dev_iv: derived.slice(32, 36),
    app_iv: derived.slice(36, 40),
  };
}

// ---------- 拼接工具 ----------
export function concat(...arrs) {
  let total = 0;
  for (const a of arrs) total += a.length;
  const out = new Uint8Array(total);
  let off = 0;
  for (const a of arrs) { out.set(a, off); off += a.length; }
  return out;
}
