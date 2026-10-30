// app.js — 主逻辑: 配置 / 连接 / 端口推送 / 全量控制 / 实时图表
import { CuktechBLE, UUID_FE95, SIID_CHARGER, PIID, PORT_NAMES, PORT_PIIDS } from './ble.js';
import { hexToBytes } from './crypto.js';

const $ = (id) => document.getElementById(id);
const PORTS = ['c1', 'c2', 'c3', 'a'];

// ---------- 常量（对齐原项目 state.py / app.js） ----------
const PORT_BIT = { c1: 0, c2: 1, c3: 2, a: 3 };
// 各端口满载功率（W）—— 负载条基准
const PORT_MAX_W = { c1: 120, c2: 120, c3: 44, a: 33 };
// PIID 21 协议开关位（state.py: PROTOCOL_SWITCH_BITS）
const PROTO_BITS = {
  c1: { pd: 0, pps: 1, ufcs: 2 },
  c2: { pd: 8, pps: 9, ufcs: 10 },
  c3: { ufcs: 16, scp: 17 },
  a:  { ufcs: 24, scp: 25 },
};
// 倒计时 PIID（app.js: COUNTDOWN_PIIDS）
const COUNTDOWN_PIIDS = { c1: 9, c2: 10, c3: 11, a: 12 };
const QUICK_MINUTES = [15, 30, 60, 90, 120, 240];

const SCENES = {
  1: { label: 'AI模式',   desc: '自动识别设备，智能匹配最优充电功率' },
  2: { label: '数码生态', desc: '多口同时充电，均衡分配功率' },
  3: { label: '单口模式', desc: '单口最大功率输出，优先 C1 口' },
  4: { label: '均衡模式', desc: '多个端口均衡分配充电功率' },
};

// ---------- 状态 ----------
let ble = null;
let connected = false;
let authenticated = false;
// 最近一次失败的原因码（no-adapter / device-away / unknown），曲线空状态要用
let lastReason = '';
let cfg = { mac: '', token: '', bleKey: '' };

const portState = {
  c1: { v: 0, i: 0, p: 0, proto: 'idle', active: false },
  c2: { v: 0, i: 0, p: 0, proto: 'idle', active: false },
  c3: { v: 0, i: 0, p: 0, proto: 'idle', active: false },
  a:  { v: 0, i: 0, p: 0, proto: 'idle', active: false },
};

// 原始设置值缓存（PIID → value）
const settings = {};
let protoExtend = 0;   // PIID 21 当前原始值
let portMask = 0x0F;   // PIID 16 当前原始值

// 图表 / 统计
const CHART_SECONDS = 60;
const chartHistory = { c1: [], c2: [], c3: [], a: [], total: [] };
let peakPower = 0;
let energyWh = 0;
let lastTick = Date.now();

// 桥接周期
const FULL_W = 250;        // 满载参考功率（堆叠条基准）
let lastDataAt = 0;        // 最后一次收到有效数据的时刻
let pollTimer = 0;         // 桥接轮询句柄

// ---------- 工具 ----------
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function log(level, msg) {
  const el = $('log');
  if (!el) return;
  const now = new Date();
  const ts = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
  const div = document.createElement('div');
  div.className = `lg ${level}`;
  div.innerHTML = `<span class="t">${ts}</span>${escapeHtml(msg)}`;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  while (el.children.length > 400) el.removeChild(el.firstChild);
}

function setConnState(state, text) {
  const dot = $('connDot');
  if (dot) dot.className = 'dot' + (state === 'ok' ? ' ok' : state === 'warn' ? ' warn' : '');
  const t = $('connText');
  if (t) t.textContent = text;
  const pill = $('statusPill');
  if (pill) pill.classList.toggle('live', state === 'ok');
  const sl = $('scanline');
  if (sl) sl.classList.toggle('on', state === 'warn');
}

// 把「为什么连不上」说清楚。只有一句"设备断开，30 秒后重连"是不够的：
// 本机没有蓝牙适配器时那个重连永远成功不了，用户会一直干等。
const HINT_TITLE = {
  'no-adapter': '本机没有检测到蓝牙适配器',
  'device-away': '找不到充电器',
  'unknown': '连接已中断',
};
function showHint(s2) {
  const bar = $('hintBar');
  if (!bar) return;
  const reason = (s2 && s2.reason) || '';
  const hint = (s2 && s2.hint) || '';
  lastReason = reason;
  if (!reason || !hint) { bar.hidden = true; return; }
  // 标题优先用服务端给的（只有一份文案，不会前后端各改一半）
  $('hintTitle').textContent = (s2.reason_title || HINT_TITLE[reason] || HINT_TITLE.unknown);
  $('hintText').textContent = hint;
  // 红色只给「等也没用」的硬故障，其它用黄色
  bar.classList.toggle('bad', reason === 'no-adapter');
  bar.hidden = false;
}

// 数据新鲜度：让用户一眼确认「现在这一秒的数字是真的」
function updateFreshness() {
  const el = $('freshText');
  if (!el) return;
  if (!lastDataAt) { el.textContent = '—'; return; }
  const ago = Math.round((Date.now() - lastDataAt) / 1000);
  if (ago <= 2) el.textContent = '刚刚更新';
  else if (ago < 60) el.textContent = `${ago} 秒前`;
  else el.textContent = `${Math.floor(ago / 60)} 分钟前`;
}

// 首屏骨架 → 真实数据
function endBoot() {
  if (document.body.classList.contains('booting')) {
    document.body.classList.remove('booting');
  }
}

// 认证后启用全部控件
function setControlsEnabled(on) {
  document.querySelectorAll('.sw, .psw, .scene').forEach(el => el.classList.toggle('disabled', !on));
  document.querySelectorAll('.cd-input').forEach(el => { el.disabled = !on; });
  document.querySelectorAll('.cd-btn').forEach(el => { el.classList.toggle('disabled', !on); });
  ['selScreen', 'selLang'].forEach(id => { const e = $(id); if (e) e.disabled = !on; });
  ['swIdle', 'swLock'].forEach(id => { const e = $(id); if (e) e.classList.toggle('disabled', !on); });
  $('btnRefresh').disabled = !on;
  $('btnConnect').disabled = on;
  $('btnScanAll').disabled = on;
  $('btnDisconnect').disabled = !connected;
}

// ---------- 配置 ----------
const STORAGE_KEY = 'cuktech-web-cfg';

function saveCfg(silent) {
  cfg.mac = $('cfgMac').value.trim();
  cfg.token = $('cfgToken').value.trim();
  cfg.bleKey = $('cfgBleKey').value.trim();
  if (cfg.mac && !/^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$/.test(cfg.mac)) {
    log('e', 'MAC 格式错误，应为 AA:BB:CC:DD:EE:FF');
    return false;
  }
  if (cfg.token && !/^[0-9a-fA-F]{24}$/.test(cfg.token)) {
    log('e', 'Token 应为 24 位 hex（12 字节）');
    return false;
  }
  localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
  if (!silent) log('o', '配置已保存到本地');
  return true;
}

async function loadCfg() {
  const s = localStorage.getItem(STORAGE_KEY);
  if (s) {
    try {
      const local = JSON.parse(s);
      if (local && local.mac && local.token) {
        cfg = local;
        $('cfgMac').value = cfg.mac || '';
        $('cfgToken').value = cfg.token || '';
        $('cfgBleKey').value = cfg.bleKey || '';
        log('i', '已从本地读取配置');
        return true;
      }
    } catch (e) { /* ignore */ }
  }
  try {
    const res = await fetch('device-config.json', { cache: 'no-store' });
    if (res.ok) {
      const d = await res.json();
      if (d.mac || d.token) {
        cfg = { mac: d.mac || '', token: d.token || '', bleKey: d.bleKey || '' };
        $('cfgMac').value = cfg.mac;
        $('cfgToken').value = cfg.token;
        $('cfgBleKey').value = cfg.bleKey;
        localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
        log('i', '已载入 device-config.json');
        if (d.name) log('i', `设备: ${d.name}`);
        return true;
      }
    }
  } catch (e) { /* 无该文件 */ }
  log('w', '本地无配置，请填写 MAC 与 Token');
  return false;
}

function clearCfg() {
  cfg = { mac: '', token: '', bleKey: '' };
  localStorage.removeItem(STORAGE_KEY);
  $('cfgMac').value = '';
  $('cfgToken').value = '';
  $('cfgBleKey').value = '';
  log('i', '配置已清空');
}

// ---------- 端口 UI ----------
function loadPct(power, port) {
  if (!(power > 0)) return 0;
  const max = PORT_MAX_W[port] || 100;
  return Math.min(99, Math.max(2, Math.round((power / max) * 100)));
}

function updatePortUI(piid, data) {
  const name = PORT_NAMES[piid];
  if (!name) return;
  const st = portState[name];
  st.v = data.voltage || 0;
  st.i = data.current || 0;
  st.p = data.power || 0;
  st.proto = data.protocol || 'idle';
  st.active = !!data.active;

  const card = document.querySelector(`.port[data-port="${name}"]`);
  if (card) {
    card.classList.toggle('active', st.active);
    card.classList.toggle('off', !st.active && !((portMask >> PORT_BIT[name]) & 1));
  }
  $('p-' + name).textContent = st.p.toFixed(1);
  $('v-' + name).textContent = st.v.toFixed(1);
  $('i-' + name).textContent = st.i.toFixed(1);
  const pr = $('proto-' + name);
  pr.textContent = st.proto;
  pr.classList.toggle('live', st.active);
  $('bar-' + name).style.width = loadPct(st.p, name) + '%';

  updateTotals();
}

function totalPower() {
  return PORTS.reduce((s, p) => s + (portState[p].p || 0), 0);
}

function updateTotals() {
  const total = totalPower();
  if (total > peakPower) peakPower = total;
  const tp = $('totalPower');
  if (tp) tp.textContent = total.toFixed(1);
  const ac = $('activeCount');
  if (ac) ac.textContent = PORTS.filter(p => portState[p].active).length;
  const pk = $('peakPower');
  if (pk) pk.textContent = peakPower.toFixed(1);
  const en = $('energyWh');
  if (en) en.textContent = energyWh.toFixed(2);

  // 堆叠条：宽度按「相对满载」的比例分配，一眼看出谁在吃电
  const bar = $('stackbar');
  if (bar) {
    for (const p of PORTS) {
      const seg = bar.querySelector(`i[data-port="${p}"]`);
      if (seg) seg.style.flexGrow = String(portState[p].p || 0);
    }
    const idle = bar.querySelector('em');
    if (idle) idle.style.flexGrow = String(Math.max(0, FULL_W - total));
  }
}

// ---------- 图表 ----------
const canvas = $('chart');
const ctx = canvas.getContext('2d');
const PORT_COLORS = { c1: '#34d399', c2: '#4c8dff', c3: '#a78bfa', a: '#fbbf24', total: '#fbbf24' };

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  if (!r.width) return;
  canvas.width = Math.round(r.width * dpr);
  canvas.height = Math.round(r.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  scheduleDraw();
}
window.addEventListener('resize', resizeCanvas);

// 按需重绘: 合并同一帧的多次请求, 避免 60fps 空转
let _chartRaf = 0;
function scheduleDraw() {
  if (_chartRaf) return;
  _chartRaf = requestAnimationFrame(() => { _chartRaf = 0; drawChart(); });
}

function tickChart() {
  updateFreshness();
  if (ble) {
    const fc = $('frameCount');
    if (fc) fc.textContent = ble.frameCount || 0;
  }
  // 页面不可见时不做任何绘制，回来后补齐
  if (document.hidden) return;

  const now = Date.now();
  const dt = Math.min(5, (now - lastTick) / 1000);
  lastTick = now;

  const total = totalPower();
  energyWh += total * dt / 3600;   // W·s → Wh

  for (const p of [...PORTS, 'total']) {
    const v = p === 'total' ? total : (portState[p].p || 0);
    chartHistory[p].push({ t: now, v });
    while (chartHistory[p].length > CHART_SECONDS + 5) chartHistory[p].shift();
  }
  updateTotals();
  scheduleDraw();
}
setInterval(tickChart, 1000);

// 窗口重新可见时，把曲线补上当前点并立即重绘
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { lastTick = Date.now(); scheduleDraw(); }
});

function drawChart() {
  const r = canvas.getBoundingClientRect();
  const W = r.width, H = r.height;
  if (!W || !H) return;
  ctx.clearRect(0, 0, W, H);

  // 水平网格
  ctx.strokeStyle = 'rgba(255,255,255,.075)';
  ctx.lineWidth = 1;
  for (let i = 1; i <= 3; i++) {
    const y = (H / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }

  const now = Date.now();
  const windowMs = CHART_SECONDS * 1000;
  let maxV = 5;
  for (const pt of chartHistory.total) if (pt.v > maxV) maxV = pt.v;
  maxV = Math.ceil(maxV * 1.25);

  // 总功率面积
  const data = chartHistory.total;
  if (data.length >= 2) {
    const pts = [];
    for (const pt of data) {
      const x = W - ((now - pt.t) / windowMs) * W;
      if (x < -2) continue;
      pts.push([x, H - (pt.v / maxV) * (H - 6) - 2]);
    }
    if (pts.length >= 2) {
      const grad = ctx.createLinearGradient(0, 0, 0, H);
      grad.addColorStop(0, 'rgba(251,191,36,.34)');
      grad.addColorStop(1, 'rgba(251,191,36,0)');
      ctx.beginPath();
      ctx.moveTo(pts[0][0], H);
      for (const [x, y] of pts) ctx.lineTo(x, y);
      ctx.lineTo(pts[pts.length - 1][0], H);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.beginPath();
      pts.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
      ctx.strokeStyle = '#fbbf24';
      ctx.lineWidth = 1.8;
      ctx.lineJoin = 'round';
      ctx.stroke();
    }
  }

  // 各端口细线
  for (const p of PORTS) {
    const arr = chartHistory[p];
    if (arr.length < 2) continue;
    ctx.beginPath();
    let started = false;
    for (const pt of arr) {
      const x = W - ((now - pt.t) / windowMs) * W;
      if (x < -2) continue;
      const y = H - (pt.v / maxV) * (H - 6) - 2;
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = PORT_COLORS[p];
    ctx.globalAlpha = 0.55;
    ctx.lineWidth = 1.1;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // 峰值参考线：这一分钟里最高冲到哪里，比只看当前值有用
  // （参考图里那条"峰值 14.1"的虚线就是这个意思）
  let peakV = 0;
  for (const pt of chartHistory.total) if (pt.v > peakV) peakV = pt.v;
  if (peakV > 0.5) {
    const y = H - (peakV / maxV) * (H - 6) - 2;
    ctx.save();
    ctx.setLineDash([3, 4]);
    ctx.strokeStyle = 'rgba(251,191,36,.38)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.restore();
    ctx.fillStyle = 'rgba(251,191,36,.72)';
    ctx.font = '9px ui-monospace, monospace';
    ctx.textAlign = 'right';
    ctx.fillText(`峰值 ${peakV.toFixed(1)}W`, W - 4, y - 4);
  }

  // 当前值的小圆点：曲线右端有个"落点"，眼睛才有地方停
  if (data.length >= 2) {
    const last = data[data.length - 1];
    const ly = H - (last.v / maxV) * (H - 6) - 2;
    ctx.beginPath();
    ctx.arc(W - 1.5, ly, 2.4, 0, Math.PI * 2);
    ctx.fillStyle = '#fbbf24';
    ctx.fill();
  }

  // 刻度
  ctx.fillStyle = 'rgba(255,255,255,.28)';
  ctx.font = '9px ui-monospace, monospace';
  ctx.textAlign = 'right';
  ctx.fillText(`${maxV}W`, W - 2, 10);

  // 空状态：全端口无输出时给出明确语义，而不是一条贴底的空线
  if (totalPower() === 0) {
    ctx.fillStyle = 'rgba(255,255,255,.15)';
    ctx.font = '11px -apple-system, "Microsoft YaHei", sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(authenticated ? '待机 · 各端口无输出'
      : (lastReason === 'no-adapter' ? '未连接 · 本机没有蓝牙适配器' : '等待设备数据'),
      W / 2, H / 2 + 2);
    ctx.textAlign = 'right';
  }
}

// ---------- 连接流程 ----------
async function connect(scanAll) {
  const mac = $('cfgMac').value.trim();
  const tokenHex = $('cfgToken').value.trim();
  if (!mac) { log('e', '请先填写 MAC 地址'); return; }
  if (!tokenHex) { log('e', '请先填写 Token'); return; }
  let token;
  try { token = hexToBytes(tokenHex); } catch (e) { log('e', `Token 解析失败: ${e.message}`); return; }
  if (token.length !== 12) { log('e', `Token 应为 12 字节，当前 ${token.length}`); return; }

  if (!navigator.bluetooth) {
    log('e', '浏览器不支持 Web Bluetooth，请用 Chrome / Edge 并通过 localhost 或 HTTPS 打开');
    return;
  }

  setConnState('warn', '选择设备中...');
  log('i', scanAll ? '扫描全部蓝牙设备，请选择 njcuk.fitting.ad1204'
                   : '请在蓝牙选择器中选择 njcuk.fitting.ad1204');

  let device;
  try {
    const reqOpts = scanAll
      ? { acceptAllDevices: true, optionalServices: [UUID_FE95] }
      : {
          filters: [
            { namePrefix: 'njcuk' },
            { namePrefix: 'CUKTECH' },
            { namePrefix: 'Cuktech' },
          ],
          optionalServices: [UUID_FE95],
        };
    device = await navigator.bluetooth.requestDevice(reqOpts);
  } catch (e) {
    log('w', `设备选择取消或失败: ${(e && e.message) || e}`);
    log('i', '若列表为空，点「扫描全部设备」在完整列表里选 njcuk.fitting.ad1204');
    setConnState('', '未连接');
    return;
  }

  log('o', `已选设备: ${device.name || device.id}`);
  await connectToDevice(device);
}

// 拿到 device 之后的完整连接 + 认证流程 (自动重连也走这里)
async function connectToDevice(device) {
  const mac = $('cfgMac').value.trim();
  let token;
  try { token = hexToBytes($('cfgToken').value.trim()); } catch (e) { return; }
  if (!mac || token.length !== 12) return;

  setConnState('warn', '连接中...');

  ble = new CuktechBLE({ mac, token, log: (lvl, msg) => log(lvl, msg) });
  ble.onPortPush = handlePortPush;
  ble.onDisconnect = () => {
    connected = false; authenticated = false;
    setConnState('', '链路断开');
    setControlsEnabled(false);
    log('w', '设备已断开');
  };

  try {
    await ble.connect(device);
    connected = true;
    $('btnDisconnect').disabled = false;

    setConnState('warn', '读取设备信息...');
    await ble.readDeviceInfo();
    setDevInfo();

    setConnState('warn', '认证中...');
    const ok = await ble.authenticate();
    if (!ok) {
      setConnState('', '认证失败');
      log('e', '认证失败 — Token 可能不正确');
      await ble.disconnect();
      connected = false;
      return;
    }

    authenticated = true;
    setConnState('ok', '已认证 · 实时监控中');
    setControlsEnabled(true);
    log('o', '✅ 连接 + 认证成功，开始拉取设备状态 ...');

    await fetchInitialSettings();
    log('o', '初始状态就绪，等待实时推送 ...');
  } catch (e) {
    log('e', `连接失败: ${e.message}`);
    console.error(e);
    setConnState('', '连接失败');
    connected = false;
    setControlsEnabled(false);
  }
}

function setDevInfo() {
  if (!ble) return;
  const nm = ble.deviceName || ble.deviceModel || '—';
  const el = $('devModel');
  if (el) { el.textContent = nm; el.title = nm; }
  $('devFw').textContent = ble.firmwareVersion || '—';
  $('devProtoVer').textContent = ble.protoVer || '—';
}

// ---------- 推送处理 ----------
async function handlePortPush(pt) {
  if (!pt || pt.length < 8) return;
  const siid = pt[6];
  const piid = pt[7];
  if (siid !== SIID_CHARGER) return;

  if (PORT_PIIDS.includes(piid)) {
    const data = ble.decodePort(piid, pt);
    if (data) updatePortUI(piid, data);
    return;
  }
  if (piid === 17) {
    const val = readU32(pt);
    const c1 = (val >> 8) & 0xff, c2 = val & 0xff;
    if (c1) ble.hwProto.c1 = c1;
    if (c2) ble.hwProto.c2 = c2;
    return;
  }
  if (piid === 18) {
    const val = readU32(pt);
    const c3 = (val >> 8) & 0xff, a = val & 0xff;
    if (c3) ble.hwProto.c3 = c3;
    if (a) ble.hwProto.a = a;
    return;
  }
  if (piid === 16) {
    updatePortSwitchUI(readU8(pt));
    return;
  }
  if (piid === 21) {
    updateProtoUI(readU32(pt));
    return;
  }
  // 其余设置项推送：同步缓存与 UI
  if (piid === 5) { settings[5] = readU8(pt); updateSceneUI(settings[5]); return; }
  if (piid === 6) { settings[6] = readU8(pt); $('selScreen').value = String(settings[6]); return; }
  if (piid === 13) { settings[13] = readU8(pt); $('selLang').value = String(settings[13]); return; }
  if (piid === 15) { settings[15] = readU8(pt); $('ps-trickle').classList.toggle('on', !!settings[15]); return; }
  if (piid === 19) { settings[19] = readU8(pt); $('swIdle').classList.toggle('on', !!settings[19]); return; }
  if (piid === 20) { settings[20] = readU8(pt); $('swLock').classList.toggle('on', !!settings[20]); return; }
  if (COUNTDOWN_PIIDS && Object.values(COUNTDOWN_PIIDS).includes(piid)) {
    settings[piid] = readU16(pt);
    for (const [p, id] of Object.entries(COUNTDOWN_PIIDS)) {
      if (id === piid) updateCountdownUI(p, settings[piid]);
    }
    return;
  }
}

function readU8(pt) { return pt.length > 13 ? pt[13] : 0; }
function readU16(pt) { return pt.length >= 15 ? (pt[13] | (pt[14] << 8)) : readU8(pt); }
function readU32(pt) {
  return pt.length >= 17
    ? ((pt[13] | (pt[14] << 8) | (pt[15] << 16) | (pt[16] << 24)) >>> 0)
    : readU8(pt);
}

// ---------- 初始设置拉取 ----------
async function getPiid(piid) {
  const r = await ble.sendMiotCommand(SIID_CHARGER, piid);
  if (!r) return null;
  if (r.value !== null && r.value !== undefined) return r.value;
  if (r.raw) return r.raw;
  return null;
}

async function fetchInitialSettings() {
  const simplePiids = [5, 6, 13, 15, 19, 20];
  for (const piid of simplePiids) {
    try {
      const r = await ble.sendMiotCommand(SIID_CHARGER, piid);
      if (r && r.value !== null && r.value !== undefined) {
        settings[piid] = r.value;
        applySetting(piid, r.value);
      }
    } catch (e) { log('w', `读 PIID ${piid} 失败: ${e.message}`); }
  }

  // 端口开关掩码
  try {
    const r = await ble.sendMiotCommand(SIID_CHARGER, PIID.PORT_CTL);
    if (r && r.value !== null && r.value !== undefined) updatePortSwitchUI(r.value);
  } catch (e) { log('w', `读 PIID 16 失败: ${e.message}`); }

  // 协议扩展
  try {
    const r = await ble.sendMiotCommand(SIID_CHARGER, PIID.PROTO_EXTEND);
    if (r && r.value !== null && r.value !== undefined) updateProtoUI(r.value);
  } catch (e) { log('w', `读 PIID 21 失败: ${e.message}`); }

  // 倒计时
  for (const [p, piid] of Object.entries(COUNTDOWN_PIIDS)) {
    try {
      const r = await ble.sendMiotCommand(SIID_CHARGER, piid);
      if (r && r.value !== null && r.value !== undefined) {
        settings[piid] = r.value;
        updateCountdownUI(p, r.value);
      }
    } catch (e) { /* 该端口可能不支持 */ }
  }

  // 硬件协议 PDO
  try {
    const r = await ble.sendMiotCommand(SIID_CHARGER, PIID.HW_PROTO_C1C2);
    if (r && r.value) {
      const c1 = (r.value >> 8) & 0xff, c2 = r.value & 0xff;
      if (c1) ble.hwProto.c1 = c1;
      if (c2) ble.hwProto.c2 = c2;
    }
  } catch (e) { /* ignore */ }
  try {
    const r = await ble.sendMiotCommand(SIID_CHARGER, PIID.HW_PROTO_C3A);
    if (r && r.value) {
      const c3 = (r.value >> 8) & 0xff, a = r.value & 0xff;
      if (c3) ble.hwProto.c3 = c3;
      if (a) ble.hwProto.a = a;
    }
  } catch (e) { /* ignore */ }

  // 端口实时数据
  for (const piid of PORT_PIIDS) {
    try {
      const r = await ble.sendMiotCommand(SIID_CHARGER, piid);
      if (r && r.raw) {
        const data = ble.decodePort(piid, r.raw);
        if (data) updatePortUI(piid, data);
      }
    } catch (e) { /* ignore */ }
  }
}

function applySetting(piid, value) {
  switch (piid) {
    case 5: updateSceneUI(value); break;
    case 6: $('selScreen').value = String(value); break;
    case 13: $('selLang').value = String(value); break;
    case 15: $('ps-trickle').classList.toggle('on', !!value); break;
    case 19: $('swIdle').classList.toggle('on', !!value); break;
    case 20: $('swLock').classList.toggle('on', !!value); break;
  }
}

// ---------- 端口开关 (PIID 16) ----------
function updatePortSwitchUI(mask) {
  portMask = mask >>> 0;
  for (const p of PORTS) {
    const on = !!((portMask >> PORT_BIT[p]) & 1);
    const el = $('sw-' + p);
    if (!el) continue;
    el.classList.toggle('on', on);
    const st = $('sws-' + p);
    if (st) st.textContent = on ? '开' : '关';
  }
  for (const p of PORTS) {
    const card = document.querySelector(`.port[data-port="${p}"]`);
    if (card) card.classList.toggle('off', !((portMask >> PORT_BIT[p]) & 1) && !portState[p].active);
  }
}

async function onPortToggle(p) {
  if (!authenticated) return;
  const next = portMask ^ (1 << PORT_BIT[p]);
  const el = $('sw-' + p);
  el.classList.toggle('on');   // 乐观更新
  const st = $('sws-' + p);
  if (st) st.textContent = el.classList.contains('on') ? '开' : '关';
  try {
    await ble.sendMiotCommand(SIID_CHARGER, PIID.PORT_CTL, next);
    updatePortSwitchUI(next);
    log('o', `${p.toUpperCase()} 端口已${((next >> PORT_BIT[p]) & 1) ? '开启' : '关闭'}`);
  } catch (e) {
    log('e', `端口开关失败: ${e.message}`);
    updatePortSwitchUI(portMask);   // 回滚
  }
}

// ---------- 协议开关 (PIID 21) ----------
function updateProtoUI(value) {
  protoExtend = value >>> 0;
  for (const [port, bits] of Object.entries(PROTO_BITS)) {
    for (const [proto, bit] of Object.entries(bits)) {
      const el = $(`ps-${port}-${proto}`);
      if (el) el.classList.toggle('on', !!((protoExtend >> bit) & 1));
    }
  }
}

async function onProtoToggle(port, proto) {
  if (!authenticated) return;
  // 小电流走 PIID 15，不走 PIID 21
  if (proto === 'trickle') {
    const cur = settings[15] ? 1 : 0;
    await setPiid(15, cur ? 0 : 1, '小电流');
    return;
  }
  const bit = PROTO_BITS[port] && PROTO_BITS[port][proto];
  if (bit === undefined) return;
  const el = $(`ps-${port}-${proto}`);
  const on = !((protoExtend >> bit) & 1);

  let next = on ? (protoExtend | (1 << bit)) : (protoExtend & ~(1 << bit));
  // c1/c2 的 bit3 / bit11 是保留位，必须恒为 1
  next = (next | 0x08 | 0x0800) >>> 0;

  if (on) el.classList.add('on'); else el.classList.remove('on');
  try {
    await ble.sendMiotCommand(SIID_CHARGER, PIID.PROTO_EXTEND, next);
    updateProtoUI(next);
    log('o', `${port.toUpperCase()} ${proto.toUpperCase()} 已${on ? '开启' : '关闭'}（需重新插拔生效）`);
  } catch (e) {
    log('e', `协议开关失败: ${e.message}`);
    updateProtoUI(protoExtend);
  }
}

// ---------- 倒计时 (PIID 9-12) ----------
function updateCountdownUI(port, minutes) {
  const cur = $('cds-' + port);
  const btn = $('cdb-' + port);
  const m = Number(minutes) || 0;
  if (cur) {
    cur.textContent = m > 0 ? `${m} 分钟` : '未设置';
    cur.classList.toggle('on', m > 0);
  }
  if (btn) {
    btn.textContent = m > 0 ? '清除' : '设置';
    btn.className = 'cd-btn ' + (m > 0 ? 'clr' : 'set');
  }
}

async function setCountdown(port, minutes) {
  if (!authenticated) return;
  const piid = COUNTDOWN_PIIDS[port];
  const btn = $('cdb-' + port);
  const prev = settings[piid];
  if (btn) btn.disabled = true;
  settings[piid] = minutes;
  updateCountdownUI(port, minutes);    // 先显示，别等蓝牙往返
  try {
    const r = await ble.sendMiotCommand(SIID_CHARGER, piid, minutes);
    if (!r) throw new Error('设备无响应');
    log('o', minutes > 0
      ? `${port.toUpperCase()} 倒计时已设为 ${minutes} 分钟`
      : `${port.toUpperCase()} 倒计时已清除`);
  } catch (e) {
    settings[piid] = prev;
    updateCountdownUI(port, prev != null ? prev : 0);
    log('e', `倒计时设置失败: ${e.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---------- 通用设置写入 ----------
async function setPiid(piid, value, label) {
  if (!authenticated) return;
  // 同样乐观：下拉框 / 开关先动，别让人干等蓝牙往返
  const prev = settings[piid];
  settings[piid] = value;
  applySetting(piid, value);
  try {
    const r = await ble.sendMiotCommand(SIID_CHARGER, piid, value);
    if (!r) throw new Error('设备无响应');
    log('o', `${label} 已设为 ${value}`);
  } catch (e) {
    settings[piid] = prev;
    if (prev != null) applySetting(piid, prev);
    log('e', `${label} 设置失败: ${e.message}`);
  }
}

// ---------- 场景模式 (PIID 5) ----------
function updateSceneUI(value) {
  document.querySelectorAll('.scene').forEach(el => {
    el.classList.toggle('on', Number(el.dataset.scene) === Number(value));
  });
  const s = SCENES[value];
  $('sceneDesc').textContent = s ? s.desc : '—';
}

async function onSceneClick(value) {
  if (!authenticated) return;
  // 乐观更新：一次 BLE 往返要等 ACK + Result（最长 8 秒超时），
  // 等结果回来才高亮的话，点下去像没反应。先改界面，失败再回滚。
  // 端口开关 / 协议开关早就是这个模式，这里补齐。
  const prev = settings[5];
  settings[5] = value;
  updateSceneUI(value);
  try {
    const r = await ble.sendMiotCommand(SIID_CHARGER, PIID.SCENE, value);
    if (!r) throw new Error('设备无响应');
    log('o', `场景模式已切换为 ${SCENES[value].label}`);
  } catch (e) {
    settings[5] = prev;
    updateSceneUI(prev);
    log('e', `场景切换失败: ${e.message}`);
  }
}

// ---------- 其他操作 ----------
async function onRefresh() {
  if (!authenticated) return;
  log('i', '刷新设备状态 ...');
  await fetchInitialSettings();
  log('o', '状态已刷新');
}

async function onDisconnect() {
  if (!ble) return;
  try { await ble.disconnect(); } catch (e) { /* ignore */ }
  connected = false; authenticated = false;
  setConnState('', '未连接');
  setControlsEnabled(false);
  log('i', '已断开连接');
}

// ---------- 事件绑定 ----------
$('btnConnect').onclick = () => connect(false);
$('btnScanAll').onclick = () => connect(true);
$('btnDisconnect').onclick = onDisconnect;
$('btnRefresh').onclick = onRefresh;
$('btnSaveCfg').onclick = () => saveCfg(false);
$('btnClearCfg').onclick = clearCfg;
$('logClear').onclick = () => { $('log').innerHTML = ''; };

// 设置抽屉
$('btnSettings').onclick = openDrawer;
$('btnDrawerClose').onclick = closeDrawer;
$('drawerMask').onclick = closeDrawer;
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeDrawer();
});

document.querySelectorAll('.sw').forEach(el => {
  el.onclick = () => { if (!el.classList.contains('disabled')) onPortToggle(el.dataset.port); };
});

document.querySelectorAll('.psw').forEach(el => {
  el.onclick = () => {
    if (el.classList.contains('disabled')) return;
    onProtoToggle(el.dataset.port, el.dataset.proto);
  };
});

document.querySelectorAll('.scene').forEach(el => {
  el.onclick = () => { if (!el.classList.contains('disabled')) onSceneClick(Number(el.dataset.scene)); };
});

// 倒计时按钮 + 快捷项
PORTS.forEach(p => {
  const btn = $('cdb-' + p);
  btn.onclick = () => {
    if (btn.classList.contains('disabled')) return;
    const isClear = btn.classList.contains('clr');
    if (isClear) {
      setCountdown(p, 0);
    } else {
      const v = parseInt($('cd-' + p).value, 10);
      if (!v || v <= 0 || v > 1440) { log('w', '请输入 1-1440 之间的分钟数'); return; }
      setCountdown(p, v);
    }
  };
  const quick = document.querySelector(`.cd-quick[data-port="${p}"]`);
  if (quick) {
    quick.innerHTML = QUICK_MINUTES.map(m =>
      `<button class="cd-q" data-min="${m}">${m}分</button>`
    ).join('');
    quick.querySelectorAll('.cd-q').forEach(b => {
      b.onclick = () => {
        if (quick.closest('.cd-item').querySelector('.cd-btn').classList.contains('disabled')) return;
        setCountdown(p, Number(b.dataset.min));
      };
    });
  }
});

// 设备设置
$('selScreen').onchange = (e) => setPiid(6, Number(e.target.value), '息屏时间');
$('selLang').onchange = (e) => setPiid(13, Number(e.target.value), '语言');
$('swIdle').onclick = () => {
  if ($('swIdle').classList.contains('disabled')) return;
  setPiid(19, settings[19] ? 0 : 1, '空闲息屏');
};
$('swLock').onclick = () => {
  if ($('swLock').classList.contains('disabled')) return;
  setPiid(20, settings[20] ? 0 : 1, '屏幕方向锁');
};

// ---------- 自动重连 (免点击) ----------
// Chrome 会记住已授权的蓝牙设备; getDevices() 能把它们取回来并直接 gatt.connect(),
// 不需要再弹选择器。首次使用仍需手动点一次「连接并认证」授权。
const BT_NAME_HINTS = ['njcuk', 'cuktech', 'cuk'];
let autoTried = false;

async function autoReconnect() {
  if (autoTried || connected) return;
  autoTried = true;

  if (!navigator.bluetooth || typeof navigator.bluetooth.getDevices !== 'function') {
    log('i', '浏览器不支持自动重连，请手动点「连接并认证」');
    return;
  }

  let devices = [];
  try {
    devices = await navigator.bluetooth.getDevices();
  } catch (e) {
    log('w', `读取已授权设备失败: ${(e && e.message) || e}`);
    return;
  }

  if (!devices.length) {
    log('i', '尚未授权过蓝牙设备 — 首次请点「连接并认证」授权一次，之后就能自动连');
    return;
  }

  const target = devices.find(d =>
    BT_NAME_HINTS.some(h => (d.name || '').toLowerCase().includes(h))
  ) || devices[0];

  // 充电器可能休眠/刚上电，给几次重试机会
  for (let attempt = 1; attempt <= 3; attempt++) {
    if (authenticated) return;
    setConnState('warn', `自动连接 ${attempt}/3 ...`);
    log('i', `自动连接已授权设备「${target.name || target.id}」(${attempt}/3) ...`);
    await connectToDevice(target);
    if (authenticated) {
      log('o', '自动连接成功');
      return;
    }
    if (attempt < 3) {
      log('i', '未连上，6 秒后重试 ...');
      await new Promise(r => setTimeout(r, 6000));
    }
  }

  log('w', '自动连接未成功 — 确认充电器已上电，然后点「连接并认证」');
  setConnState('', '未连接');
}

// ---------- 本地桥接模式 (优先) ----------
// bridge.py 在后台用系统蓝牙直连设备, 网页只读 /api/state。
// 好处: 不需要浏览器蓝牙授权, 不需要点任何按钮, 打开即有数据。
let bridgeMode = false;

class BridgeAdapter {
  constructor(dev) {
    this._dev = dev || {};
    this.frameCount = 0;
    this.hwProto = {};
  }
  get deviceName() { return this._dev.name || ''; }
  get deviceModel() { return this._dev.model || ''; }
  get firmwareVersion() { return this._dev.fw || ''; }
  get protoVer() { return this._dev.protoVer || ''; }
  async sendMiotCommand(siid, piid, value) {
    const r = await fetch('api/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ piid, value }),
    });
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (e) { /* ignore */ }
      throw new Error(msg);
    }
    return { piid, value };
  }
  async disconnect() { /* 连接由本地服务持有, 网页不主动断开 */ }
}

function applyBridgeState(st) {
  const s = st.settings || {};
  // 先刷新缓存, 控制逻辑 (onPortToggle 等) 依赖它
  for (const [k, v] of Object.entries(s)) settings[Number(k)] = v;

  for (const [piid, d] of Object.entries(st.ports || {})) {
    updatePortUI(Number(piid), d);
  }

  const has = (k) => s[String(k)] !== undefined;
  if (has(16)) updatePortSwitchUI(s['16']);
  if (has(21)) updateProtoUI(s['21']);
  if (has(5)) updateSceneUI(s['5']);
  if (has(6)) $('selScreen').value = String(s['6']);
  if (has(13)) $('selLang').value = String(s['13']);
  if (has(15)) $('ps-trickle').classList.toggle('on', !!s['15']);
  if (has(19)) $('swIdle').classList.toggle('on', !!s['19']);
  if (has(20)) $('swLock').classList.toggle('on', !!s['20']);
  for (const [p, id] of Object.entries(COUNTDOWN_PIIDS)) {
    if (has(id)) updateCountdownUI(p, s[String(id)]);
  }
}

async function tryBridge() {
  let st;
  try {
    const r = await fetch('api/state', { cache: 'no-store' });
    if (!r.ok) return false;
    st = await r.json();
  } catch (e) {
    return false;
  }
  // disabled = 服务在跑但没配设备, 交给浏览器蓝牙模式
  if (!st || st.status === 'disabled') return false;

  bridgeMode = true;
  document.body.classList.add('bridge-mode');
  ble = new BridgeAdapter(st.device);
  setDevInfo();
  log('o', '本地服务已接管 · 系统蓝牙直连，无需任何授权');

  // 桥接模式下连接由本地服务持有，网页侧不提供连接动作
  const lockBtns = () => {
    for (const id of ['btnConnect', 'btnScanAll', 'btnDisconnect', 'btnRefresh']) {
      const el = $(id);
      if (el) { el.disabled = true; el.title = '由本地服务托管，无需手动操作'; }
    }
  };
  lockBtns();

  let lastSig = '';
  const sync = (s2) => {
    const ok = s2.status === 'ok';
    if (ok !== authenticated) {
      authenticated = ok;
      connected = ok;
      setControlsEnabled(ok);
      lockBtns();
    }

    if (ok) {
      setConnState('ok', '本地直连 · 实时监控中');
      showHint(null);
      lastDataAt = Date.now();
      endBoot();
    } else {
      setConnState('warn', s2.detail || s2.status);
      showHint(s2);
      // 只在「还没开始连」时保留骨架，出错/断开要显示真实 0 值
      if (s2.status !== 'connecting' && s2.status !== 'starting') endBoot();
    }

    if (s2.device && s2.device.model) {
      ble._dev = s2.device;
      setDevInfo();
    }
    ble.frameCount = s2.frames || 0;

    // 待机时数据常年不变 —— 跳过 DOM 写入，只保留一次 JSON 比较
    const sig = JSON.stringify([s2.ports, s2.settings]);
    if (sig === lastSig) return;
    lastSig = sig;
    applyBridgeState(s2);
  };

  sync(st);

  // 单一轮询循环：可见 1s / 隐藏 6s，无叠加重绘
  const poll = async () => {
    try {
      const r = await fetch('api/state', { cache: 'no-store' });
      if (r.ok) sync(await r.json());
    } catch (e) { /* 服务重启中, 跳过这一轮 */ }
    pollTimer = setTimeout(poll, document.hidden ? 6000 : 1000);
  };
  pollTimer = setTimeout(poll, 900);

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && bridgeMode) {
      clearTimeout(pollTimer);
      poll();
    }
  });

  return true;
}

// ---------- 设置抽屉 ----------
function openDrawer() {
  const d = $('drawer'), m = $('drawerMask');
  if (d) d.classList.add('show');
  if (m) m.classList.add('show');
}
function closeDrawer() {
  const d = $('drawer'), m = $('drawerMask');
  if (d) d.classList.remove('show');
  if (m) m.classList.remove('show');
}

// ---------- 初始化 ----------
window.addEventListener('DOMContentLoaded', async () => {
  await loadCfg();
  resizeCanvas();
  setTimeout(resizeCanvas, 100);

  setControlsEnabled(false);

  // 优先走本地桥接服务: 成功则完全不碰 Web Bluetooth
  const bridged = await tryBridge();
  if (bridged) {
    log('i', '页面已就绪 — 数据由本地服务直连提供');
    return;
  }

  endBoot();
  if (!navigator.bluetooth) {
    log('e', '当前浏览器不支持 Web Bluetooth API');
    log('i', '请用 Chrome / Edge / Opera，并通过 http://localhost 或 HTTPS 打开');
    setConnState('', '浏览器不支持蓝牙');
  } else {
    log('i', '页面已就绪 — 浏览器蓝牙模式');
    log('i', '提示: 设备广播名是 njcuk.fitting.ad1204');
    setConnState('warn', '正在自动连接…');
  }

  // 没有桥接服务 → 回退浏览器蓝牙, 自动重连已授权设备
  setTimeout(() => { autoReconnect(); }, 400);
});
