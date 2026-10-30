/* ==========================================================
   米家 · 电脑版 — 前端逻辑
   交互原则：
   · 卡片上直接给出"现在是什么状态"，不用点进去才知道
   · 详情页分三层：电源 → 主要调节 → 更多设置，默认只展开前两层
   · 所有操作先改界面再发请求，失败回滚；面板数据本地缓存，二次打开秒开
   ========================================================== */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  devices: [],
  rooms: [],
  room: "__all__",
  view: "devices",     // devices | charger
  query: "",
  live: {},            // did -> {on,temp,hum,batt,bright,level,mode,mode_t,temp_set,pos}
  chargerW: 0,         // 充电器实时总功率（后端 /api/live 的 charger_w 给的）
                       // 它不在米家设备表里，要单独并进「当前总功率」
  cards: {},           // did -> 元素（大卡或小卡）
  entered: false,      // 首屏入场动画是否已经播过（播过就不再错峰淡入）
  roomEls: {},
  panels: {},          // did -> 面板数据缓存
  touched: {},         // did -> 最近一次操作时间（用于"刚操作过，先别重绘"）
  gen: {},             // did -> 版本号。轮询发出前拍快照，回来时比对，
                        //        凡是期间被操作过的设备，旧结果一律丢弃。
                        //        不依赖时间窗口：慢网络下同样正确。
  currentDid: null,
  currentDev: null,
  cam: null,
  camWasPlaying: false, // 进托盘挂起前画面在播 → 恢复时自动续播
  camTimer: null,
  timer: null,
  quickTimer: null,    // 开关状态的高频轮询（只读 on）
  lastLive: 0,
  liveReady: false,   // 第一次拿到实时数据之前不显示"关"，避免误报
  fromCache: false,    // 当前设备列表是不是来自缓存
  loading: false,
};

/* ---------------- 图标 ---------------- */
// 注意 stroke="currentColor" 不能省：SVG 的 stroke 默认是 none，
// 只写 fill="none" 等于"既不填充也不描边"，图标画出来是空的。
// 之前漏了这个属性，云台四个方向键、设备类型图标全都是隐形按钮 ——
// 视觉上只剩一块底色，用户根本看不出能点。
const S = (d) => `<svg viewBox="0 0 24 24" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" fill="none" stroke="currentColor">${d}</svg>`;
const ICONS = {
  light: S('<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.5.4.8 1 .8 1.6h5.4c0-.6.3-1.2.8-1.6A6 6 0 0 0 12 3Z"/>'),
  plug: S('<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-6 6 6 6 0 0 1-6-6V8ZM12 17v4"/>'),
  camera: S('<path d="M3 8.5A2.5 2.5 0 0 1 5.5 6h6A2.5 2.5 0 0 1 14 8.5v7A2.5 2.5 0 0 1 11.5 18h-6A2.5 2.5 0 0 1 3 15.5v-7ZM14 10.5l6-3v9l-6-3"/>'),
  vacuum: S('<circle cx="12" cy="13" r="8"/><path d="M12 5V3M9.5 13h5"/>'),
  ac: S('<rect x="3" y="5" width="18" height="7" rx="2"/><path d="M6.5 8.5h11M7 16c0 1.5 1 2.5 2.5 2.5M12 16v2.5M17 16c0 1.5-1 2.5-2.5 2.5"/>'),
  curtain: S('<path d="M3 4h18M6 4v16M18 4v16M6 4c0 6 1.5 10 4 16M18 4c0 6-1.5 10-4 16"/>'),
  fan: S('<circle cx="12" cy="12" r="2.1"/><path d="M12 9.9c0-3.2 1.1-5.9 3.1-5.9 1.6 0 2.6 1.5 2.6 3.1 0 1.7-1.2 2.8-3 3.2M9.9 12c-3.2 0-5.9-1.1-5.9-3.1 0-1.6 1.5-2.6 3.1-2.6 1.7 0 2.8 1.2 3.2 3M12 14.1c0 3.2-1.1 5.9-3.1 5.9-1.6 0-2.6-1.5-2.6-3.1 0-1.7 1.2-2.8 3-3.2M14.1 12c3.2 0 5.9 1.1 5.9 3.1 0 1.6-1.5 2.6-3.1 2.6-1.7 0-2.8-1.2-3.2-3"/>'),
  climate: S('<circle cx="12" cy="12" r="2.2"/><path d="M12 4v3M12 17v3M4 12h3M17 12h3M6.3 6.3l2.1 2.1M15.6 15.6l2.1 2.1M17.7 6.3l-2.1 2.1M8.4 15.6l-2.1 2.1"/>'),
  lock: S('<rect x="5" y="10" width="14" height="10" rx="2.5"/><path d="M8 10V7.5a4 4 0 0 1 8 0V10"/>'),
  sensor: S('<path d="M12 14.5V5M8.5 8.5a5.5 5.5 0 1 0 7 0"/>'),
  media: S('<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 20h8"/>'),
  appliance: S('<rect x="5" y="3" width="14" height="18" rx="2.5"/><path d="M9 8h6M9 12h6"/>'),
  other: S('<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M9 12h6"/>'),
  ir: S('<rect x="4" y="3" width="16" height="18" rx="4"/><circle cx="12" cy="8" r="2"/><path d="M8.5 14h7M8.5 17h4"/>'),
  phone: S('<rect x="6.5" y="2.5" width="11" height="19" rx="2.8"/><path d="M10.5 18.5h3"/>'),
  wearable: S('<rect x="7" y="6.5" width="10" height="11" rx="3"/><path d="M9 6.5 9.4 3h5.2L15 6.5M9 17.5 9.4 21h5.2l.4-3.5"/>'),
  scale: S('<rect x="3" y="5" width="18" height="14" rx="3.5"/><path d="M8 12h8M12 9v3"/>'),
  charger: S('<path d="M13 2 5 13.5h5.2L10 22l8-11.5h-5.2z"/>'),
  home: S('<path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z"/>'),
  layers: S('<path d="M12 3 3 8l9 5 9-5-9-5ZM3 14l9 5 9-5"/>'),
  moon: S('<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"/>'),
  sun: S('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
  power: S('<path d="M12 3v9"/><path d="M6.5 6.5a7.5 7.5 0 1 0 11 0"/>'),
  chevron: S('<path d="M9 6l6 6-6 6"/>'),
  plus: S('<path d="M12 5v14M5 12h14"/>'),
  minus: S('<path d="M5 12h14"/>'),
  info: S('<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>'),
  bulb: S('<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.5.4.8 1 .8 1.6h5.4c0-.6.3-1.2.8-1.6A6 6 0 0 0 12 3Z"/>'),
  wind: S('<path d="M3 8h11a3 3 0 1 0-3-3M3 16h8a3 3 0 1 1-3 3M3 12h18"/>'),
  swing: S('<path d="M4 18c4 0 5-12 9-12s4 6 7 6"/><circle cx="4" cy="18" r="1.4"/>'),
  alert: S('<path d="M12 4 3 20h18z"/><path d="M12 10v4M12 17h.01"/>'),
  bell: S('<path d="M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6M10 19a2 2 0 0 0 4 0"/>'),
};
const iconOf = (k) => ICONS[k] || ICONS.other;

const KIND_COLOR = {
  light: "#EE9A12", plug: "#4C8DFF", camera: "#7C6BFF", fan: "#00A99B",
  climate: "#00B3A4", ac: "#2F9BFF", curtain: "#8B5CF6", media: "#FF5C7A",
  sensor: "#22C55E", appliance: "#6B7C93", lock: "#F59E0B", vacuum: "#0EA5E9",
  ir: "#FF6A00", phone: "#6366F1", wearable: "#EC4899", scale: "#14B8A6",
  charger: "#F59E0B", other: "#94A3B8",
};
const KIND_NAME = {
  light: "灯光", plug: "插座", camera: "摄像头", fan: "风扇", climate: "环境电器",
  ac: "空调", curtain: "窗帘", media: "影音", sensor: "传感器",
  appliance: "家电", lock: "门锁", vacuum: "扫地机", ir: "红外遥控",
  phone: "手机", wearable: "穿戴设备", scale: "体重秤", charger: "充电设备",
  other: "其他设备",
};

/* ---------------- 离线提示：后端进程没了自动提示并重连 ---------------- */
const NET = { down: false, timer: null };

function netDown() {
  if (NET.down) return;
  NET.down = true;
  let bar = $("#net-bar");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "net-bar";
    bar.innerHTML =
      `<i class="dot"></i><span>本地服务没在运行，正在重试…</span>` +
      `<button type="button">立即刷新</button>`;
    bar.querySelector("button").onclick = () => location.reload();
    document.body.appendChild(bar);
  }
  requestAnimationFrame(() => bar.classList.add("show"));

  clearInterval(NET.timer);
  NET.timer = setInterval(async () => {
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      if (r.ok) netUp(true);
    } catch (_) { /* 还没起来，继续等 */ }
  }, 2500);
}

function netUp(reconnected) {
  if (!NET.down) return;
  NET.down = false;
  clearInterval(NET.timer);
  NET.timer = null;
  const bar = $("#net-bar");
  if (bar) {
    bar.classList.remove("show");
    setTimeout(() => bar.remove(), 400);
  }
  if (!reconnected) return;
  if (!state.devices || !state.devices.length) return location.reload();
  toast("已重新连接");
  loadDevices();
  refreshLive();
}

/* ---------------- API ---------------- */
/* 兜底：任何没 catch 的异步异常都记到控制台。
   不弹 toast —— 用户没做错什么，弹出来只会更困惑；但排查时能看见，
   总比"界面忽然没反应、控制台也什么都没有"强。 */
window.addEventListener("unhandledrejection", (e) => {
  console.error("[mihome] 未处理的异步异常:", e && e.reason);
});
window.addEventListener("error", (e) => {
  if (e && e.message) console.error("[mihome] 脚本异常:", e.message, e.filename, e.lineno);
});
/* 每个请求都带超时。后端最慢的接口是 /api/live（云端 25 秒），留 45 秒余量。
   没有超时的话，一次"连上了但永远不回包"的请求会把界面吊死：同步指示一直
   转、按钮一直是 sending/waiting 态，用户看到的就是"卡住"。超时后走正常
   失败路径（提示 + 回滚），至少还能继续点。 */
const API_TIMEOUT = 45000;

async function api(path, opts) {
  const opt = Object.assign({}, opts);
  let timer = null;
  if (!opt.signal && typeof AbortController === "function") {
    const ac = new AbortController();
    opt.signal = ac.signal;
    timer = setTimeout(() => ac.abort(), API_TIMEOUT);
  }
  let r;
  try {
    r = await fetch(path, opt);
  } catch (e) {
    // 超时不是"后端没了"，别弹重连条：网络抖动时把界面拉去重连更烦人
    if (e && (e.name === "AbortError" || e.code === 20)) {
      return { ok: false, timeout: true, error: "请求超时" };
    }
    netDown();
    return { ok: false, net: true, error: "连接不到本地服务" };
  } finally {
    if (timer) clearTimeout(timer);
  }
  netUp();
  const txt = await r.text();
  if (!txt) return { ok: false, error: "空响应" };
  try {
    return JSON.parse(txt);
  } catch (e) {
    return { ok: false, error: "响应格式异常" };
  }
}
const post = (p, body) => api(p, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});
const get = (p) => api(p, { method: "GET" });

/* ---------------- Toast ---------------- */
let toastTimer = null;
function toast(msg, isErr) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2000);
}

/* ---------------- 同步状态 ---------------- */
let inflight = 0;
function syncStart() {
  inflight++;
  const b = $("#sync-badge");
  if (!b) return;
  b.classList.add("syncing");
  $("#sync-text").textContent = "同步中";
}
function syncEnd() {
  inflight = Math.max(0, inflight - 1);
  if (inflight) return;
  const b = $("#sync-badge");
  if (!b) return;
  b.classList.remove("syncing");
  $("#sync-text").textContent = "已同步";
}

/* ---------------- 主题 ---------------- */
function paintThemeBtn() {
  const dark = document.documentElement.dataset.theme === "dark";
  const btn = $("#btn-theme");
  if (!btn) return;
  $("#theme-ico").innerHTML = iconOf(dark ? "sun" : "moon");
  $("#theme-label").textContent = dark ? "浅色模式" : "深色模式";
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("mi-theme", next); } catch (e) {}
  paintThemeBtn();
}

/* ---------------- 数值格式化 ---------------- */
const fmt = (v) => {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  return Math.abs(n - Math.round(n)) < 0.05 ? String(Math.round(n)) : n.toFixed(1);
};

function climateOf(list) {
  const rows = list.map(d => state.live[d.did]).filter(v => v && v.temp != null);
  if (!rows.length) return null;
  const avg = (k) => {
    const vs = rows.map(r => r[k]).filter(v => v != null);
    return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
  };
  return { temp: avg("temp"), hum: avg("hum"), n: rows.length };
}

/* ---------------- 云端状态圆点 ----------------
   数据来自 /api/live 回包里的 conn（后端 netguard 的健康账本）。
   为什么要它：断网、米家云抖动、登录失效，三种原因在界面上长得一模一样，
   都是"设备不刷新"。有了这个点，用户不用猜，也知道该等还是该重新登录。 */
function paintHealth(conn, stale) {
  const box = $("#side-health");
  if (!box) return;
  const lab = $("#health-label");
  let lvl = "ok", txt = "云端已连接";
  if (conn === "down") { lvl = "bad"; txt = "连不上云端"; }
  else if (conn === "degraded") { lvl = "warn"; txt = "网络不稳"; }
  else if (stale) { lvl = "warn"; txt = "显示的是旧数据"; }
  else if (!conn) { lvl = "idle"; txt = "未连接"; }
  if (box.dataset.lvl !== lvl) box.dataset.lvl = lvl;
  if (lab.textContent !== txt) lab.textContent = txt;
  box.title = txt + (stale ? "（这一屏是缓存的上一轮数据）" : "");
}

/* ---------------- 登录 ---------------- */
/* 恢复中的界面：有凭证时全程自动，绝不弹表单打扰用户 */
function showRestoring(on, txt) {
  const box = $("#restoring");
  if (!box) return;
  box.classList.toggle("hidden", !on);
  $("#login-form").classList.toggle("hidden", !!on);
  if (on) stopQrPoll();
  const sub = $("#login-sub");
  if (sub) sub.textContent = on ? "已保存登录状态，正在自动进入" : "扫码登录，安全便捷";
  if (txt) $("#restoring-txt").textContent = txt;
}

/* 三个"能看见"的指标：首屏 ready、最近一次出画面、自愈次数 */
function paintPerf(m) {
  const el = $("#perf-line");
  if (!el || !m) return;
  const ready = m.ready_at ? m.ready_at.toFixed(1) + "s" : "—";
  const cam = m.cam_first_ms != null ? (m.cam_first_ms / 1000).toFixed(1) + "s" : "—";
  el.textContent = "首屏 " + ready + " · 画面 " + cam;
  el.title = "首屏=进程启动到可交互；画面=最近一次点开摄像头到能播；"
    + "登录自愈 " + (m.heals || 0) + " 次；累计出画面 "
    + (m.cam_ok || 0) + " / 失败 " + (m.cam_fail || 0);
}

/* 启动遮罩：淡出后直接移除，别留一个全屏元素压着渲染 */
function hideSplash() {
  const s = $("#splash");
  if (!s) return;
  s.classList.add("hide");
  setTimeout(() => { if (s.parentNode) s.parentNode.removeChild(s); }, 380);
}

/* ---------------- 扫码登录 ---------------- */
let qrTimer = null;
let qrStarting = false;

function stopQrPoll() {
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
}

function showQrPanel(on) {
  const qr = $("#qr-panel"), pwd = $("#pwd-panel");
  if (qr) qr.classList.toggle("hidden", !on);
  if (pwd) pwd.classList.toggle("hidden", on);
  const sub = $("#login-sub");
  if (sub) sub.textContent = on ? "扫码登录，安全便捷" : "用小米账号登录，直连小米云";
}

function qrShowError(text) {
  stopQrPoll();
  const box = $("#qr-box");
  if (box) { box.classList.remove("loading"); box.classList.add("expired"); }
  const tip = $("#qr-loading span");
  if (tip) tip.textContent = text || "二维码已失效";
  const acts = $("#qr-actions");
  if (acts) acts.classList.remove("hidden");
  setMsg(text || "二维码已失效");
}

async function startQr() {
  const form = $("#login-form");
  if (!form || form.classList.contains("hidden") || qrStarting) return;
  qrStarting = true;
  stopQrPoll();
  showQrPanel(true);
  const box = $("#qr-box"), img = $("#qr-img");
  if (box) { box.classList.remove("expired"); box.classList.add("loading"); }
  const tip = $("#qr-loading span");
  if (tip) tip.textContent = "正在获取二维码…";
  const acts = $("#qr-actions");
  if (acts) acts.classList.add("hidden");
  setMsg("正在获取登录二维码…", true);

  const r = await post("/api/qr/start");
  qrStarting = false;
  if (!r.ok || r.state === "error") {
    const why = r.error === "响应格式异常"
      ? "后台还是旧版本，请重启米家电脑版后再扫码；也可以先用账号密码"
      : (r.error || "获取二维码失败");
    qrShowError(why);
    return;
  }
  if (r.state === "success") { watchLogin(); return; }
  if (!r.qr) { qrShowError(r.error || "没有拿到二维码，请刷新重试"); return; }

  img.onload = () => {
    if (box) box.classList.remove("loading");
    setMsg("等待手机扫码…", true);
  };
  img.onerror = () => qrShowError("二维码图片加载失败，请刷新重试");
  img.src = r.qr;
  // 图片走的是小米 CDN，偶尔 onload 不触发；complete + 尺寸兜底一次
  setTimeout(() => {
    if (box && box.classList.contains("loading") && img.complete && img.naturalWidth) {
      box.classList.remove("loading");
      setMsg("等待手机扫码…", true);
    }
  }, 900);
  qrTimer = setInterval(pollQr, 1500);
  pollQr();
}

async function pollQr() {
  const st = await api("/api/qr/status");
  if (st.net) return;
  if (st.state === "waiting" || st.state === "starting") {
    setMsg("等待手机扫码…", true);
    return;
  }
  if (st.state === "scanned") {
    const box = $("#qr-box");
    if (box) box.classList.add("loading");
    const tip = $("#qr-loading span");
    if (tip) tip.textContent = "已扫码，正在登录…";
    setMsg("已扫码，正在登录…", true);
    return;
  }
  if (st.state === "success") {
    stopQrPoll();
    watchLogin();
    return;
  }
  if (st.state === "error") {
    qrShowError(st.error || "二维码已失效，请刷新");
    return;
  }
  if (st.state === "idle") stopQrPoll();
}

function wireQr() {
  const sw = $("#qr-switch"), back = $("#pwd-switch"), retry = $("#qr-retry");
  if (sw) sw.onclick = () => {
    stopQrPoll();
    post("/api/qr/cancel");
    showQrPanel(false);
    setMsg("");
    const u = $("#in-user");
    if (u) u.focus();
  };
  if (back) back.onclick = () => { startQr(); };
  if (retry) retry.onclick = () => { startQr(); };
}

async function boot() {
  paintThemeBtn();
  const st = await api("/api/status");
  paintHealth(st.health && st.health.state);
  paintPerf(st.metrics);
  hideSplash();

  if (st.net) {
    // 后端还在启动路上，继续等，不要弹登录框
    showRestoring(true, "正在启动本地服务…");
    wireLogin();
    wireOtp();
    watchLogin(true);
    return;
  }
  if (st.status === "ready") return enterApp();

  // 有缓存设备但还没恢复完：先进主界面把上次的设备显示出来，
  // 后台继续恢复，好过对着空白页等 20-30 秒。
  if (st.has_cached_devices) {
    enterApp();
    wireLogin();
    wireOtp();
    watchLogin(true);
    return;
  }

  if (st.has_token) {
    // 有凭证：后台会自动恢复，前端只需安静等待 + 显示进度
    showRestoring(true, "正在用已保存的登录状态进入…");
    wireLogin();
    wireOtp();
    watchLogin(true);
    return;
  }

  // 没有凭证 / 明确恢复失败：默认扫码；密码保留为备选
  showRestoring(false);
  wireLogin();
  wireOtp();
  showQrPanel(true);
  if (st.restore_failed) setMsg("上次的登录状态已失效，请扫码或使用账号密码");
  else if (st.error) setMsg(st.error);
  startQr();
}

function wireLogin() {
  $("#btn-login").onclick = async () => {
    const u = $("#in-user").value.trim();
    const p = $("#in-pass").value;
    if (!u || !p) return setMsg("请填写账号和密码");
    setMsg("正在登录…", true);
    $("#btn-login").disabled = true;
    const r = await post("/api/login", { username: u, password: p });
    if (!r.ok) { setMsg(r.error || "登录失败"); $("#btn-login").disabled = false; return; }
    watchLogin();
  };
  $("#btn-restore").onclick = async () => {
    showRestoring(true, "正在用已保存的登录状态进入…");
    const r = await post("/api/restore");
    if (r.ok) return enterApp();
    // 后台已经在跑，或者这次没成功，都交给轮询去决定下一步
    watchLogin(true);
  };
  $("#in-pass").onkeydown = (e) => { if (e.key === "Enter") $("#btn-login").click(); };
  wireQr();
  wireOtp();
  wireDrawer();
}

let loginTimer = null;
function watchLogin(restore) {
  let tries = 0;
  clearInterval(loginTimer);
  loginTimer = setInterval(async () => {
    tries++;
    const st = await api("/api/status");

    if (st.net) {                       // 后端短暂不可达，继续等，不当成失败
      if (restore) showRestoring(true, "正在启动本地服务…");
      return;
    }
    if (st.status === "ready") {
      clearInterval(loginTimer);
      stopQrPoll();
      paintHealth(st.health && st.health.state);
      $("#otp-modal").classList.add("hidden");
      if (state.fromCache) {
        // 之前显示的是缓存数据，现在真实数据到了，换成真的
        hideCacheHint();
        await loadDevices();
        return;
      }
      enterApp();
      return;
    }
    if (st.status === "need_otp") {
      $("#otp-modal").classList.remove("hidden");
      $("#otp-tip").textContent = st.otp_method === "Email"
        ? "验证码已发送到你的邮箱，请查收" : "验证码已发送到你的手机，请查收短信";
      setTimeout(() => $("#otp-input").focus(), 100);
      return;
    }
    if (st.status === "error") {
      clearInterval(loginTimer);
      $("#otp-modal").classList.add("hidden");
      showRestoring(false);
      $("#btn-login").disabled = false;
      setMsg(st.error || "登录失败");
      return;
    }

    // 后端已经明确空闲且没有凭证：不能再停在"正在恢复"，
    // 否则首屏先连上后端、随后才启动完成时会永远转圈。
    if (restore && st.status === "idle" && !st.has_token) {
      clearInterval(loginTimer);
      showRestoring(false);
      showQrPanel(true);
      setMsg("请扫码登录");
      startQr();
      return;
    }

    if (restore) {
      // 关键：后端还在恢复就继续等（拉 13 台设备冷启动要 20-30 秒），
      // 只有后端明确说"失败了"才切回表单，绝不按秒数猜。
      if (st.restore_failed) {
        clearInterval(loginTimer);
        showRestoring(false);
        showQrPanel(true);
        $("#btn-restore").classList.remove("hidden");
        $("#btn-login").disabled = false;
        const why = st.error ? String(st.error).slice(0, 80) : "登录凭证可能已过期";
        setMsg("自动登录没成功：" + why + "。请扫码或使用账号密码");
        startQr();
        return;
      }
      const n = st.restore_tries || 1;
      showRestoring(true,
        tries <= 3 ? "正在用已保存的登录状态进入…"
                   : `正在重新连接小米云…（第 ${n} 次尝试，已等 ${tries} 秒）`);
      return;
    }

    setMsg("正在登录…", true);
    if (tries >= 90) {
      clearInterval(loginTimer);
      $("#btn-login").disabled = false;
      setMsg("登录超时，请重试");
    }
  }, 1000);
}

function wireOtp() {
  $("#otp-submit").onclick = async () => {
    const code = $("#otp-input").value.trim();
    if (!code) return;
    $("#otp-msg").textContent = "提交中…";
    const r = await post("/api/otp", { code });
    $("#otp-msg").textContent = r.ok ? "已提交，等待验证…" : (r.error || "提交失败");
    $("#otp-input").value = "";
  };
  $("#otp-input").onkeydown = (e) => { if (e.key === "Enter") $("#otp-submit").click(); };
}

function setMsg(t, ok) {
  const el = $("#login-msg");
  el.textContent = t;
  el.className = "msg" + (ok ? " ok" : "");
}

/* ---------------- 主界面 ---------------- */
async function enterApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  stopQrPoll();
  $("#otp-modal").classList.add("hidden");
  wireApp();
  wireDrawer();
  paintThemeBtn();
  await loadDevices();
  clearInterval(state.timer);
  clearInterval(state.quickTimer);
  // 缓存态下还没登录，轮询实时数据只会一路 401；
  // 等真实数据到了再由 watchLogin 那边启动。
  //
  // 两档节拍。以前是"一刀切 20 秒"，两头不讨好：开关状态跟不上
  // （在 App 里按了墙上的开关，屏幕上要等 20 秒），而温湿度这种五分钟
  // 都不变的读数每 20 秒白读一次，回来还要整页重绘。
  //   4 秒  —— 只读开关。整轮十几条参数一个请求，几十毫秒回来。
  //   45 秒 —— 全量读数：温度/湿度/电量/功率/档位/模式。
  if (!state.fromCache) {
    state.quickTimer = setInterval(() => {
      if (!document.hidden) refreshQuick();
    }, 4000);
    state.timer = setInterval(() => {
      if (!document.hidden) refreshLive();
    }, 45000);
  }
  // 切回来立刻补一次，别让用户对着旧状态。
  // 只挂一次：enterApp 可能被调用多次（boot / watchLogin / 重新登录），
  // 每调一次就 addEventListener 一次的话，切回窗口会连着打好几轮请求。
  if (!enterApp.visHooked) {
    enterApp.visHooked = true;
    document.addEventListener("visibilitychange", () => {
      if (document.hidden || state.fromCache) return;
      refreshQuick();
      refreshLive();
    });
  }
  openFromHash();
}

function wireApp() {
  if (wireApp.done) return;
  wireApp.done = true;

  $("#btn-theme").onclick = toggleTheme;

  // 侧栏「房间温度」「充电器」入口
  const navCharger = $("#nav-charger");
  if (navCharger) navCharger.onclick = openCharger;
  setInterval(() => { if (!document.hidden) refreshChargerBadge(); }, 15000);
  refreshChargerBadge();

  $("#btn-refresh").onclick = async () => {
    const btn = $("#btn-refresh");
    btn.style.opacity = ".5";
    syncStart();
    await post("/api/refresh");
    await loadDevices();
    syncEnd();
    btn.style.opacity = "";
    toast("设备列表已更新");
  };
  $("#btn-logout").onclick = async () => {
    if (!confirm("确定退出登录？本机保存的登录凭证会被删除。")) return;
    await post("/api/logout");
    location.reload();
  };

  // 开机自启开关
  const btnAuto = $("#btn-autostart");
  const autoLabel = $("#autostart-label");
  let autoOn = false;
  const paintAuto = () => {
    if (!autoLabel) return;
    autoLabel.textContent = autoOn ? "开机自启 · 开" : "开机自启 · 关";
    if (btnAuto) btnAuto.style.color = autoOn ? "var(--accent)" : "";
    if (btnAuto) btnAuto.title = autoOn
      ? "已设置开机自动启动，点一下关闭"
      : "点一下设置为开机自动启动";
  };
  const loadAuto = async () => {
    try {
      const r = await get("/api/autostart");
      if (r && r.ok) { autoOn = !!r.enabled; paintAuto(); }
    } catch (e) { /* 接口没起来就当不支持，按钮保持默认态 */ }
  };
  if (btnAuto) {
    btnAuto.onclick = async () => {
      btnAuto.style.opacity = ".5";
      const want = !autoOn;
      try {
        const r = await post("/api/autostart", { enabled: want });
        if (r && r.ok) {
          autoOn = !!r.enabled;
          toast(autoOn ? "已设为开机自动启动" : "已关闭开机自启");
        } else {
          toast((r && r.error) || "设置失败");
        }
      } catch (e) {
        toast("设置失败：" + (e && e.message ? e.message : "未知错误"));
      }
      paintAuto();
      btnAuto.style.opacity = "";
    };
    loadAuto();
  }
  $("#q").oninput = (e) => {
    state.query = e.target.value.trim().toLowerCase();
    renderGrid();
  };

  $("#search-box").onclick = () => $("#q").focus();
  $("#scroller").addEventListener("scroll", () => {
    $("#topbar").classList.toggle("scrolled", $("#scroller").scrollTop > 6);
  }, { passive: true });

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); $("#q").focus(); }
    if (e.key === "Escape") {
      if (!$("#device-modal").classList.contains("hidden")) return closeDevice();
      if (typing && $("#q").value) { $("#q").value = ""; state.query = ""; renderGrid(); }
    }
  });
}

function wireDrawer() {
  if (wireDrawer.done) return;
  wireDrawer.done = true;
  $("#dm-close").onclick = closeDevice;
  $("#device-modal").onclick = (e) => { if (e.target.id === "device-modal") closeDevice(); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDevice(); });
}

async function loadDevices() {
  if (!state.devices.length) skeletonGrid();
  const r = await api("/api/devices");
  if (!r.ok) {
    $("#device-grid").innerHTML = "";
    $("#empty-tip").classList.remove("hidden");
    $("#empty-tip").querySelector("p").textContent = r.error || "设备列表加载失败";
    toast("设备列表加载失败", true);
    return;
  }
  // from_cache：这次拿到的是上次缓存的设备列表。
  // 里面的在线状态是旧的，统一标成 stale，卡片上显示"待确认"，
  // 等真实数据来了再覆盖——否则会看到一个明显错误的状态。
  const wasCache = !!r.from_cache;
  state.devices = (r.devices || []).map((d) =>
    wasCache ? Object.assign({}, d, { stale: true }) : d);
  state.fromCache = wasCache;
  buildRooms();
  renderGrid();
  if (wasCache) showCacheHint(); else hideCacheHint();
  // 缓存状态下还没登录，发 /api/live 会 401，白刷一串错误
  if (wasCache) return;
  await refreshLive(true);
}

/* 缓存数据的提示条：说清楚"这是上次的数据，正在刷新"，
   而不是让用户以为程序读错了。
   提示条本体在 index.html 里是静态的，这里只切显隐。 */
function showCacheHint() {
  const el = $("#cache-hint");
  if (el) el.classList.remove("hidden");
}

function hideCacheHint() {
  const el = $("#cache-hint");
  if (el) el.classList.add("hidden");
}

function skeletonGrid() {
  const g = $("#device-grid");
  g.innerHTML = `<div class="sect"><div class="grid">${
    Array.from({ length: 6 }, () => '<div class="sk-card"></div>').join("")
  }</div></div>`;
}

function buildRooms() {
  const map = new Map();
  for (const d of state.devices) {
    const k = d.room || "未分组";
    map.set(k, (map.get(k) || 0) + 1);
  }
  state.rooms = [...map.entries()].map(([name, n]) => ({ name, n }));

  const nav = $("#room-nav");
  nav.innerHTML = "";
  state.roomEls = {};
  const items = [{ name: "__all__", label: "全部设备", n: state.devices.length, icon: "layers" }]
    .concat(state.rooms.map(r => ({ ...r, label: r.name, icon: "home" })));

  for (const it of items) {
    const div = document.createElement("div");
    div.className = "room-item" + (state.room === it.name ? " active" : "");
    div.dataset.room = it.name;
    div.innerHTML = `<span class="ri">${iconOf(it.icon)}</span>
      <span class="nm">${esc(it.label)}</span>
      <span class="rt"></span><span class="cnt">${it.n}</span>`;
    const svg = div.querySelector(".ri svg");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.9");
    div.onclick = () => {
      setView("devices");
      state.room = it.name;
      $$("#room-nav .room-item").forEach(x => x.classList.remove("active"));
      div.classList.add("active");
      $("#scroller").scrollTop = 0;
      renderGrid();
      if (window.Climate) Climate.followRoom(it.name);
    };
    nav.appendChild(div);
    state.roomEls[it.name] = div.querySelector(".rt");
  }
  paintRoomTemps();
}

/* ---------------- 充电器视图 ---------------- */
/* 首次点进去才加载 iframe —— 蓝牙桥接服务是按需拉起的，
   不点就不会占内存、也不会抢充电器的蓝牙连接。 */
let chargerBooted = false;

/* 三个视图共用同一个窗口：设备 / 房间温度 / 充电器。
   充电器是 iframe（首次点进去才加载，蓝牙桥接按需拉起）；
   房间温度是纯前端绘制的动态示意图。 */
function setView(v) {
  state.view = v;
  $("#scroller").classList.toggle("hidden", v !== "devices");
  $("#charger-view").classList.toggle("hidden", v !== "charger");

  $$(".room-item").forEach(x => x.classList.remove("active"));
  if (v === "charger") {
    const nav = $("#nav-charger");
    if (nav) nav.classList.add("active");
  } else {
    $$("#room-nav .room-item").forEach(x =>
      x.classList.toggle("active", x.dataset.room === state.room));
    if (window.Climate) Climate.show();
  }
}

function openCharger() {
  setView("charger");
  if (!chargerBooted) {
    chargerBooted = true;
    try { localStorage.setItem("charger-booted", "1"); } catch (e) {}
    $("#charger-frame").src = "/charger/";
  } else {
    // 切回来时让页面重新拉一次状态，别停在旧数据上
    try { $("#charger-frame").contentWindow.postMessage("refresh", "*"); } catch (e) {}
  }
}

function closeCharger() {
  if (state.view !== "charger") return;
  setView("devices");
}

/* 充电器功率：后端 /api/live 已经算好了（charger_w），这里只负责落地。
   桥接没在跑时后端返回 null，按 0 处理 —— 不显示那一行，也不显示 0W。 */
function setChargerW(v) {
  const n = Number(v);
  const next = isFinite(n) && n > 0 ? n : 0;
  if (state.chargerW === next) return;
  state.chargerW = next;
  // 总功率卡立刻重画，别等 energy.js 那个 4 秒定时器
  if (window.Energy && window.Energy.paintPower) window.Energy.paintPower();
  refreshChargerBadge();
}

/* 侧栏那个小徽章。数据统一来自 charger_w，不再自己发请求 ——
   原来这里每 15 秒单独打一次 /charger/api/state，和 /api/live 里的
   查询重复，还会把桥接的 idle 超时一直顶着不让它歇。 */
function refreshChargerBadge() {
  const el = $("#charger-w");
  if (!el) return;
  el.textContent = state.chargerW > 0 ? Math.round(state.chargerW) + "W" : "";
}

function paintRoomTemps() {
  for (const [room, el] of Object.entries(state.roomEls)) {
    if (!el) continue;
    const list = room === "__all__"
      ? state.devices
      : state.devices.filter(d => (d.room || "未分组") === room);
    const c = climateOf(list);
    el.textContent = c && c.temp != null ? fmt(c.temp) + "°" : "";
  }
}

function visibleDevices() {
  let list = state.room === "__all__"
    ? state.devices
    : state.devices.filter(d => (d.room || "未分组") === state.room);
  if (state.query) {
    list = list.filter(d =>
      d.name.toLowerCase().includes(state.query) ||
      (d.model || "").toLowerCase().includes(state.query) ||
      (d.room || "").toLowerCase().includes(state.query));
  }
  return list;
}

/* 大卡 = 能开关的 + 红外遥控（真实家电）；小卡 = 只能看不能控的设备 */
const isMini = (d) => !d.switchable && !d.ir;

/* ---------- 设备卡片自定义顺序（交互在 cardsort.js） ----------
   按"分组"存，不存全局序列：拖动只发生在同一个网格里（「可控制」一组、
   每个房间一组），跨组移动没有意义 —— 分组是按在线状态和房间算出来的，
   不是用户摆出来的。存成 {scope: [did, ...]} 也就不会有"搜索时拖一下，
   把没显示的设备顺序全都冲掉"这种事。 */
const DEV_ORDER_KEY = "mi-dev-order-v2";

function devOrderMap() {
  try {
    const v = JSON.parse(localStorage.getItem(DEV_ORDER_KEY) || "null");
    return (v && typeof v === "object" && !Array.isArray(v)) ? v : {};
  } catch (e) { return {}; }
}

function devOrderRead(scope) {
  const a = devOrderMap()[scope];
  return Array.isArray(a) ? a : null;
}

function devOrderSave(scope, ids) {
  const m = devOrderMap();
  m[scope] = ids;
  try { localStorage.setItem(DEV_ORDER_KEY, JSON.stringify(m)); } catch (e) {}
}

function devOrderClear(scope) {
  const m = devOrderMap();
  delete m[scope];
  try { localStorage.setItem(DEV_ORDER_KEY, JSON.stringify(m)); } catch (e) {}
}

function applyDevOrder(list, scope) {
  const saved = devOrderRead(scope);
  if (!saved || !saved.length) return list;
  const pos = new Map();
  saved.forEach((id, i) => pos.set(String(id), i));
  const known = [], rest = [];
  for (const d of list) (pos.has(String(d.did)) ? known : rest).push(d);
  known.sort((a, b) => pos.get(String(a.did)) - pos.get(String(b.did)));
  // 没排过的（新加的设备）保持原来的排序，接在后面 —— 不会因为拖过一次
  // 就把新设备塞到最前面，也不会把它们挤出视野。
  return known.concat(rest);
}

window.devOrder = { read: devOrderRead, save: devOrderSave,
                    clear: devOrderClear, apply: applyDevOrder };

function renderGrid() {
  const container = $("#device-grid");
  const list = visibleDevices();

  // 已经进过场就不要再让新卡片错峰淡入了：切房间、搜索、刷新重建网格时
  // 那 18ms×N 的延迟，体感就是"点了之后半天没反应"。
  if (state.entered) document.body.classList.remove("booting");

  $("#room-title").textContent = state.room === "__all__" ? "全部设备" : state.room;
  const online = list.filter(d => d.online).length;
  const stale = state.fromCache;
  $("#room-sub").textContent = stale
    ? `${list.length} 台设备 · 状态待刷新`
    : `${list.length} 台设备 · ${online} 台在线`;

  container.innerHTML = "";
  state.cards = {};
  $("#empty-tip").classList.toggle("hidden", list.length > 0);
  $("#empty-tip").querySelector("p").textContent = state.query ? "没有匹配的设备" : "这里还没有设备";

  let groups;
  if (state.room === "__all__" && !state.query) {
    // 置顶只放「在线 且 能下发指令」的：开关类（switchable）或带按键的红外遥控器。
    // 传感器、离线设备顶上去没有意义 —— 它们不能点，只会把真正要按的东西挤下去。
    const canCtl = (d) => d.online && (d.switchable || (d.ir && (d.ir_keys || 0) > 0));
    const pinned = state.devices.filter(canCtl);
    const pinnedIds = new Set(pinned.map((d) => String(d.did)));
    groups = [];
    if (pinned.length) {
      groups.push({ name: "可控制", key: "__pinned__", list: pinned, pinned: true });
    }
    groups = groups.concat(state.rooms
      .map(r => ({ name: r.name, key: "r:" + r.name,
                   list: state.devices.filter(d => (d.room || "未分组") === r.name
                                                   && !pinnedIds.has(String(d.did))) }))
      .filter(g => g.list.length));
  } else {
    groups = [{ name: null,
                key: state.query ? "__search__" : (state.room || "__all__"),
                list }];
  }

  // 用户拖过顺序的分组，按存下来的 did 序列重排（没拖过的原样不动）
  groups = groups.map(g => Object.assign({}, g, { list: applyDevOrder(g.list, g.key) }));

  let idx = 0;
  for (const g of groups) {
    const sect = document.createElement("div");
    sect.className = "sect";

    if (g.name) {
      const c = climateOf(g.list);
      const clim = c && c.temp != null
        ? `<span class="sect-climate">
             <span class="t"><i></i>${fmt(c.temp)}°C</span>
             ${c.hum != null ? `<span class="h"><i></i>${fmt(c.hum)}%</span>` : ""}
           </span>` : "";
      sect.innerHTML = `<div class="sect-head">
          <h4>${esc(g.name)}</h4>${clim}
          <span class="sect-count">${g.list.length} 台 · ${g.list.filter(d => d.online).length} 在线</span>
        </div>`;
    }

    const main = g.list.filter(d => !isMini(d));
    const mini = g.list.filter(d => isMini(d));

    if (main.length) {
      const grid = document.createElement("div");
      grid.className = "grid";
      // 拖动顺序按这个 scope 存（cardsort.js 读它）；搜索结果不给拖 ——
      // 那个顺序是筛出来的，不是用户摆的，存下来会污染真实顺序。
      grid.dataset.scope = g.key || "";
      if (state.query) grid.dataset.nosort = "1";
      for (const d of main) {
        const el = buildCard(d);
        el.style.setProperty("--i", idx++);
        grid.appendChild(el);
      }
      sect.appendChild(grid);
    }
    if (mini.length) {
      if (main.length) {
        const l = document.createElement("div");
        l.className = "sect-sub";
        l.textContent = "其他设备";
        sect.appendChild(l);
      }
      const mg = document.createElement("div");
      mg.className = "mini-grid";
      for (const d of mini) {
        const el = buildMini(d);
        el.style.setProperty("--i", idx++);
        mg.appendChild(el);
      }
      sect.appendChild(mg);
    }
    container.appendChild(sect);
  }

  paintTopClimate(list);

  // 首屏这一轮才播入场动画；算好最后一张卡的延迟，播完摘掉 .booting。
  if (!state.entered) {
    state.entered = true;
    const wait = 320 + Math.min(idx, 40) * 18;
    setTimeout(() => document.body.classList.remove("booting"), wait);
  }
}

function paintTopClimate(list) {
  const c = climateOf(list);
  const box = $("#climate");
  if (!box) return;
  if (!c || c.temp == null) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  $("#cl-temp").textContent = fmt(c.temp);
  $("#cl-hum").textContent = c.hum != null ? fmt(c.hum) : "—";
}

/* ---------- 状态摘要：卡片上直接看出设备现在在做什么 ---------- */
function liveOf(did) { return state.live[did] || {}; }

function summaryOf(dev) {
  const lv = liveOf(dev.did);
  // 缓存态：在线状态是上次的旧数据，不确定，如实说"待确认"
  if (dev.stale) return { txt: "待确认" };
  if (!dev.online) return { txt: "离线" };
  if (dev.ir) return { txt: dev.ir_keys ? `${dev.ir_keys} 个按键` : "未添加按键" };
  if (!state.liveReady) return { txt: "读取状态…" };

  let s;
  if (dev.switchable && !lv.on) s = { txt: "已关闭" };
  else if (dev.kind === "ac" && lv.temp_set != null) {
    s = { big: fmt(lv.temp_set), unit: "°", txt: lv.mode_t || "制冷" };
  } else if ((dev.kind === "fan" || dev.kind === "climate") && lv.level != null) {
    s = { big: String(lv.level), unit: "档", txt: lv.mode_t || "送风" };
  } else if (dev.kind === "light" && lv.bright != null) {
    s = { big: String(lv.bright), unit: "%", txt: "亮度" };
  } else if (dev.kind === "curtain" && lv.pos != null) {
    s = { big: String(lv.pos), unit: "%", txt: "开合" };
  } else if (dev.kind === "camera") s = { txt: lv.on ? "监控中" : "已关闭" };
  else if (dev.kind === "plug") s = { txt: lv.on ? "通电中" : "已断电" };
  else s = { txt: lv.on ? "运行中" : "已关闭" };

  // 插座 / 空调这类能报功率的，状态后面直接带上瓦数
  if (lv.watt != null && lv.watt > 0) {
    const w = Math.round(lv.watt);
    s.txt = s.txt ? `${s.txt} · ${w}W` : `${w}W`;
  }
  return s;
}

function statusHtml(dev) {
  const s = summaryOf(dev);
  let out = `<i class="dot"></i>`;
  if (s.big) out += `<span class="val">${esc(s.big)}<small>${esc(s.unit || "")}</small></span>`;
  if (s.txt) out += `<span class="txt">${esc(s.txt)}</span>`;
  return out;
}

function buildCard(dev) {
  const lv = liveOf(dev.did);
  const on = dev.switchable && !!lv.on;
  const card = document.createElement("div");
  card.className = "device-card"
    + (dev.stale ? " stale" : (dev.online ? "" : " offline"))
    + (on ? " on" : "")
    + (cardIsOff(dev) ? " is-off" : "");
  card.dataset.kind = dev.kind || "other";
  card.dataset.did = dev.did;

  const right = dev.switchable
    ? `<button class="mini-switch ${on ? "on" : ""}${state.liveReady ? "" : " unknown"}"
         data-switch aria-label="开关"${state.liveReady ? "" : " disabled"}></button>`
    : (dev.ir ? `<span class="dc-tag">红外</span>` : "");

  // 红外遥控没有开关，把自定义按键直接摆在卡片上，点一下就发码
  const keys = (dev.ir && dev.ir_key_list) || [];
  const keysHtml = keys.length
    ? `<div class="dc-keys">${keys.map(k =>
        `<button class="dc-key" type="button" data-irkey="${k.id}"
           title="发送红外「${esc(k.name)}」">${esc(k.name)}</button>`).join("")}</div>`
    : "";

  // 风扇/取暖器的档位与模式：跟红外按键摆在一起，按一下就下发，不用进详情页。
  // 离线或状态待确认时不摆 —— 点了也不会成功的按钮只会让人以为程序坏了。
  const quick = (dev.online && !dev.stale) ? (dev.quick || []) : [];
  // 摇头（摆风）是个开关，不是档位：单独做成胶囊挂在最后一行右侧，
  // 形状和档位按钮不同，才不会看成"第 5 档"。离线时同样不摆。
  const swing = (dev.online && !dev.stale) ? dev.swing : null;
  const swingHtml = swing
    ? `<button class="dc-swing${lv.swing ? " on" : ""}" type="button" data-swing
         aria-pressed="${lv.swing ? "true" : "false"}"
         title="摇头（${swing.name === "vertical-swing" ? "上下" : "左右"}摆风）"
         >${swingGlyph(swing.name === "vertical-swing")}<span>摇头</span></button>`
    : "";
  // 档位一行、模式一行：6 个按钮挤在一行会被卡片宽度挤成随机换行（第二排还
  // 被上一个按钮的间距顶歪），分行之后不管卡片多宽都是同一个样子。
  const quickRow = (grp, tail) => {
    const items = quick.filter(q => q.grp === grp);
    if (!items.length) return "";
    return `<div class="dc-keys dc-quick">${items.map(q => {
      const i = quick.indexOf(q);
      const on = qIsOn(lv, q);
      return `<button class="dc-key${on ? " on" : ""}" type="button"
        data-quick="${i}" data-grp="${q.grp}" aria-pressed="${on ? "true" : "false"}"
        title="${grp === "mode" ? "切到" : "调到"}「${esc(q.label)}」">${esc(q.label)}</button>`;
    }).join("")}${tail || ""}</div>`;
  };
  const hasMode = quick.some(q => q.grp === "mode");
  const hasLvl = quick.some(q => q.grp === "lvl");
  let quickHtml = quickRow("lvl", hasMode ? "" : swingHtml) + quickRow("mode", swingHtml);
  // 只有摇头、没有档位/模式的设备（比如某些取暖器），自己占一行
  if (!hasLvl && !hasMode && swingHtml) {
    quickHtml = `<div class="dc-keys dc-quick">${swingHtml}</div>`;
  }

  card.innerHTML = `
    <div class="dc-top">
      <div class="dc-icon">${iconOf(dev.kind)}</div>
      <div class="dc-tools">${right}</div>
    </div>
    <div class="dc-name">${esc(dev.name)}</div>
    <div class="dc-status">${statusHtml(dev)}</div>
    ${quickHtml}${keysHtml}`;

  card.onclick = () => openDevice(dev);

  const sw = card.querySelector("[data-switch]");
  if (sw) sw.onclick = (e) => { e.stopPropagation(); togglePower(dev, sw); };

  const sg = card.querySelector("[data-swing]");
  if (sg) sg.onclick = (e) => { e.stopPropagation(); toggleSwing(dev, sg); };

  card.querySelectorAll("[data-irkey]").forEach(b => {
    b.onclick = (e) => { e.stopPropagation(); pressIrCardKey(dev, b); };
  });

  card.querySelectorAll("[data-quick]").forEach(b => {
    b.onclick = (e) => {
      e.stopPropagation();
      setQuick(dev, quick[Number(b.dataset.quick)], b);
    };
  });

  state.cards[dev.did] = card;
  return card;
}

/* 摇头图标：双向箭头。12px 下"风扇 + 弧线"那种画法会糊成一坨，
   箭头是唯一还认得出的形状；上下摆风的设备把箭头转 90°。 */
function swingGlyph(vertical) {
  const d = vertical
    ? '<path d="M12 3.5v17"/><path d="M8.9 6.6 12 3.5l3.1 3.1"/><path d="M8.9 17.4 12 20.5l3.1-3.1"/>'
    : '<path d="M3.5 12h17"/><path d="M6.6 8.9 3.5 12l3.1 3.1"/><path d="M17.4 8.9 20.5 12l-3.1 3.1"/>';
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
}

/* 设备"明确关着"：卡片上的一切发光（状态点、档位选中、摇头）都要熄掉。
   注意三态：开关状态还没读到时（liveReady=false）不能算关 ——
   否则首屏那一下所有卡片都会先暗一遍再亮，像闪了一下。 */
function cardIsOff(dev) {
  if (!dev.switchable || !state.liveReady) return false;
  return liveOf(dev.did).on === false;
}

/* ---------- 卡片上的档位 / 模式按钮 ---------- */
/* 判断某个快捷按钮是不是"当前选中"：档位看 live.level，模式看 live.mode。
   两者都是数字枚举，但云端偶尔给字符串，所以统一走 Number 比较。 */
function qIsOn(lv, q) {
  const cur = q.grp === "mode" ? lv.mode : lv.level;
  return cur != null && cur !== "" && Number(cur) === Number(q.value);
}

/* 按一下档位/模式：乐观更新 → 下发 → 失败回滚由轮询自然纠正 */
async function setQuick(dev, q, btn) {
  if (!q || btn.classList.contains("sending")) return;
  if (qIsOn(liveOf(dev.did), q)) return;    // 已经是这个档，别白打一次云
  btn.classList.add("sending");
  // 先标"刚操作过"：否则这一秒内的轮询会把界面盖回旧档位，看起来像没点上
  markLocal(dev.did);
  syncStart();
  const r = await post("/api/setprop",
    { did: dev.did, siid: q.siid, piid: q.piid, value: q.value });
  syncEnd();
  btn.classList.remove("sending");
  if (!r.ok) { toast("控制失败，设备可能离线", true); return; }
  if (q.grp === "mode") {
    setLive(dev.did, "mode", q.value);
    setLive(dev.did, "mode_t", q.label);
  } else {
    setLive(dev.did, "level", q.value);
  }
  markLocal(dev.did);
  paintQuick(dev);
  updateCard(dev);
}

/* 把选中态刷到按钮上（轮询/别处改了档位，这里跟着变） */
function paintQuick(dev) {
  const el = state.cards[dev.did];
  const quick = dev.quick || [];
  if (!el || !quick.length) return;
  const lv = liveOf(dev.did);
  el.querySelectorAll("[data-quick]").forEach(b => {
    const q = quick[Number(b.dataset.quick)];
    if (!q) return;
    const on = qIsOn(lv, q);
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

/* 摇头开关：和档位一样走乐观更新，失败回滚。
   摆风是个独立的 bool 属性，不跟档位/模式联动 —— 关着风扇时也能单独拨，
   至于拨了会不会转是设备自己的事（有些型号关着也能记住下次开就摇头）。 */
async function toggleSwing(dev, btn) {
  if (!dev.swing || btn.classList.contains("sending")) return;
  const cur = !!liveOf(dev.did).swing;
  const next = !cur;
  setLive(dev.did, "swing", next);
  markLocal(dev.did);
  btn.classList.add("sending");
  paintSwing(dev);
  syncStart();
  const r = await post("/api/setprop",
    { did: dev.did, siid: dev.swing.siid, piid: dev.swing.piid, value: next });
  syncEnd();
  btn.classList.remove("sending");
  if (!r.ok) {
    setLive(dev.did, "swing", cur);
    paintSwing(dev);
    toast("摇头切换失败，设备可能离线", true);
    return;
  }
  markLocal(dev.did);
  paintSwing(dev);
}

function paintSwing(dev) {
  const el = state.cards[dev.did];
  if (!el) return;
  const b = el.querySelector("[data-swing]");
  if (!b) return;
  const on = !!liveOf(dev.did).swing;
  b.classList.toggle("on", on);
  b.setAttribute("aria-pressed", on ? "true" : "false");
}

/* 卡片上按红外键：一点就发，不用进详情页 */
/* 注意：不能叫 pressIrKey——详情页还有个同名函数，后者会把它覆盖掉 */
function pressIrCardKey(dev, btn) {
  if (btn.classList.contains("sending")) return;
  btn.classList.add("sending");
  fetch("/api/ir/press", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ did: dev.did, key_id: Number(btn.dataset.irkey) }),
  })
    .then(r => r.json())
    .then(j => {
      if (!j || !j.ok) throw new Error((j && j.error) || "发送失败");
      btn.classList.add("sent");
      setTimeout(() => btn.classList.remove("sent"), 700);
    })
    .catch(e => toast("红外发送失败：" + (e.message || "网络异常"), true))
    .finally(() => btn.classList.remove("sending"));
}

function buildMini(dev) {
  const lv = liveOf(dev.did);
  const el = document.createElement("div");
  el.className = "mini-card" + (dev.stale ? " stale" : (dev.online ? "" : " offline"));
  el.dataset.kind = dev.kind || "other";
  el.dataset.did = dev.did;
  el.innerHTML = `
    <div class="mc-icon">${iconOf(dev.kind)}</div>
    <div class="mc-text"><div class="mc-name">${esc(dev.name)}</div>
      <div class="mc-sub">${esc(miniSub(dev, lv))}</div></div>
    <div class="mc-val">${miniVal(dev, lv)}</div>`;
  el.onclick = () => openDevice(dev);
  state.cards[dev.did] = el;
  return el;
}

function miniSub(dev, lv) {
  if (dev.stale) return "待确认";
  if (!dev.online) return "离线";
  if (lv.temp != null && lv.hum != null) return `湿度 ${fmt(lv.hum)}%`;
  if (lv.temp != null) return "温度";
  if (lv.batt != null) return `电量 ${fmt(lv.batt)}%`;
  return KIND_NAME[dev.kind] || "设备";
}

function miniVal(dev, lv) {
  if (!dev.online) return `<span style="color:var(--faint);font-size:12px">—</span>`;
  if (lv.temp != null) return `${fmt(lv.temp)}<small>°C</small>`;
  if (lv.batt != null) return `${fmt(lv.batt)}<small>%</small>`;
  return "";
}

function updateCard(dev) {
  const el = state.cards[dev.did];
  if (!el) return;
  const lv = liveOf(dev.did);
  const on = dev.switchable && !!lv.on;
  el.classList.toggle("on", on);
  if (el.classList.contains("device-card")) {
    // 关掉之后：状态点、档位选中、摇头一律熄光，只留"上次是几档"的信息
    el.classList.toggle("is-off", cardIsOff(dev));
    const sw = el.querySelector("[data-switch]");
    if (sw) {
      sw.classList.toggle("on", on);
      if (state.liveReady && sw.classList.contains("unknown")) {
        sw.classList.remove("unknown");
        sw.disabled = false;
      }
    }
    const st = el.querySelector(".dc-status");
    if (st) st.innerHTML = statusHtml(dev);
    // 档位/模式的选中态也跟着实时值走：用 App 或小爱改了档，卡片这里要跟着亮
    paintQuick(dev);
    paintSwing(dev);
  } else {
    const sub = el.querySelector(".mc-sub");
    const val = el.querySelector(".mc-val");
    if (sub) sub.textContent = miniSub(dev, lv);
    if (val) val.innerHTML = miniVal(dev, lv);
  }
}

/* 卡片上的小开关：先改界面，再发请求，失败回滚 */
async function togglePower(dev, btn) {
  const cur = !!liveOf(dev.did).on;
  const next = !cur;
  setLive(dev.did, "on", next);
  // 关键：乐观更新时就标记"刚操作过"，防止并发轮询把旧状态盖回来
  // （否则点了开关，20 秒轮询里的旧值会把界面刷回"关闭"，几秒后才又同步）
  markLocal(dev.did);
  btn.classList.add("waiting");
  updateCard(dev);
  if (state.currentDid === dev.did) { paintPowerTile(dev); syncPanelState(dev); }

  syncStart();
  const r = await post("/api/setprop", {
    did: dev.did, siid: dev.on_siid, piid: dev.on_piid, value: next,
  });
  syncEnd();
  btn.classList.remove("waiting");
  if (!r.ok) {
    setLive(dev.did, "on", cur);
    delete state.touched[dev.did];
    updateCard(dev);
    if (state.currentDid === dev.did) { paintPowerTile(dev); syncPanelState(dev); }
    toast("控制失败，设备可能离线", true);
    return;
  }
  markLocal(dev.did);
  if (state.currentDid === dev.did) { paintPowerTile(dev); syncPanelState(dev); }
}

/* 版本号 +1：标记"这台设备的本地状态被用户改过"。
   轮询回来时凡是版本对不上的设备，它带来的旧值直接丢弃。 */
function bumpGen(did) {
  state.gen[did] = (state.gen[did] || 0) + 1;
}

/* 乐观更新：改本地值 + 记版本号 + 记时间戳 */
function markLocal(did) {
  bumpGen(did);
  state.touched[did] = Date.now();
}

function setLive(did, key, val) {
  state.live[did] = state.live[did] || {};
  state.live[did][key] = val;
}

/* 批量取实时数据（后端并发 + 2.5 秒缓存） */
/* 高频轮询：只刷开关状态（4 秒一轮）。

   为什么单独一条：开关是"别人可能随时改动"的状态（App / 小爱语音 / 墙上的
   实体按键），越跟手越好；而温度湿度电量这些读数变化慢、回包大、重绘也重。
   分成两档之后，开关跟得上，读数不白刷。

   注意这里**只合并 on**，不能把 state.live 整个换掉 —— 这一轮没带读数，
   整个换就等于每 4 秒把温度湿度清空一次。 */

/* 轮询防叠：弱网/云端慢的时候一轮能拖几十秒，4 秒的定时器会不停往上叠，
   最后十几个请求同时打米家云（触发限流 → 更慢 → 叠得更多），界面反而更卡。
   在途标记保证同一时刻只有一轮。force=true 的那次（手动刷新）例外，见下。 */
let _quickBusy = false;
async function refreshQuick() {
  if (_quickBusy) return;
  _quickBusy = true;
  try {
    await _refreshQuick();
  } finally {
    _quickBusy = false;
  }
}

async function _refreshQuick() {
  if (!state.liveReady) return;          // 首屏还没就绪，交给 refreshLive
  const snap = Object.assign({}, state.gen);
  const r = await post("/api/live", { quick: 1 });
  paintHealth(r.conn, r.stale);
  if (!r.ok || !r.live) return;
  setChargerW(r.charger_w);

  const dirty = new Set();
  for (const did in r.live) {
    const v = r.live[did];
    if (v.on === undefined) continue;
    // 请求在途期间被本地操作过：这份结果比用户的操作旧，不能盖回去
    if ((state.gen[did] || 0) !== (snap[did] || 0)) continue;
    const cur = state.live[did] || (state.live[did] = {});
    if (cur.on !== v.on) {
      cur.on = v.on;
      dirty.add(String(did));
    }
  }
  // 只重绘真正变了的那几张卡：全量 updateCard 是十几张一起跑，
  // 每 4 秒来一遍纯属浪费 —— 而"别的房间有人开了灯"通常只动一台。
  if (!dirty.size || state.cam) return;  // 摄像头在放视频时把 CPU 让给解码
  for (const d of state.devices) {
    if (dirty.has(String(d.did))) updateCard(d);
  }
}

/* 同上的防叠，全量那一档也加一层。force 的那次（刚登录/手动刷新）不排队，
   但也不会和已经在跑的那轮并行 —— 直接复用在途的那一轮结果。 */
let _liveBusy = false;
async function refreshLive(force) {
  if (_liveBusy) return;
  _liveBusy = true;
  try {
    await _refreshLive(force);
  } finally {
    _liveBusy = false;
  }
}

async function _refreshLive(force) {
  const now = Date.now();
  // 和 4 秒那档错开：全量读数 45 秒一次就够，30 秒内的重复请求直接忽略
  if (!force && now - state.lastLive < 30000) return;
  state.lastLive = now;

  // 发出请求**之前**拍一张全设备版本快照。
  // 这条请求反映的是"发出那一刻"的状态；期间被操作过的设备，
  // 回来的这份旧结果对它们就是过期的，必须丢弃。
  const snap = Object.assign({}, state.gen);

  const r = await post("/api/live");
  paintHealth(r.conn, r.stale);
  if (!r.ok || !r.live) return;
  setChargerW(r.charger_w);

  // 逐台比对版本号：只要这台设备在请求期间被本地操作过，
  // 就保留本地的乐观值，把轮询带来的旧值挡在外面。
  // 注意这里是 `in r.live`（对象的 did 键），不是 `in Object.keys(...)`：
  // 后者拿到的是数组下标 "0"/"1"，state.gen 永远查不到，整段保护会静默失效。
  for (const did in r.live) {
    if ((state.gen[did] || 0) === (snap[did] || 0)) continue;  // 没动过，正常采纳
    const cur = state.live[did];
    if (!cur) continue;
    // 只保留控制类字段；温湿度等实时读数仍用新数据（它们不会因操作而失效）
    for (const k of ["on", "mode", "mode_t", "bright", "level", "temp_set", "pos", "speed", "swing"]) {
      if (cur[k] !== undefined) r.live[did][k] = cur[k];
    }
  }
  state.live = r.live;
  // 首次拿到状态时直接切到位，别让一排开关从灰色慢慢变蓝
  const first = !state.liveReady;
  if (first) document.body.classList.add("nofx");
  state.liveReady = true;
  // 顶部三格方块里的"当前功率"跟着这一轮实时数据立刻刷新，
  // 不必等 energy.js 那个 4 秒定时器（首屏那几秒会看到 0 W）
  if (window.Energy && window.Energy.paintPower) window.Energy.paintPower();
  // 摄像头正在放视频时，跳过整页重绘，把 CPU 让给视频解码，避免卡顿
  if (!state.cam) {
    for (const d of state.devices) updateCard(d);
    paintRoomTemps();
    paintTopClimate(visibleDevices());
    updateSectClimates();
    if (window.Climate) Climate.update();
  }
  if (state.currentDid) {
    const dev = state.devices.find(d => d.did === state.currentDid);
    if (dev) { paintHero(dev); paintPowerTile(dev); }
  }
  if (first) requestAnimationFrame(() => document.body.classList.remove("nofx"));
}

function updateSectClimates() {
  $$("#device-grid .sect").forEach(sect => {
    const name = sect.querySelector(".sect-head h4");
    if (!name) return;
    const room = name.textContent;
    const list = state.devices.filter(d => (d.room || "未分组") === room);
    const c = climateOf(list);
    const box = sect.querySelector(".sect-climate");
    if (!c || c.temp == null) { if (box) box.remove(); return; }
    const html = `<span class="t"><i></i>${fmt(c.temp)}°C</span>` +
      (c.hum != null ? `<span class="h"><i></i>${fmt(c.hum)}%</span>` : "");
    if (box) box.innerHTML = html;
    else name.insertAdjacentHTML("afterend", `<span class="sect-climate">${html}</span>`);
  });
}

/* ================= 设备详情 ================= */
async function openDevice(dev) {
  stopCam();
  state.currentDid = dev.did;
  state.currentDev = dev;
  const mask = $("#device-modal");
  mask.classList.remove("hidden");
  paintHero(dev);

  const cached = state.panels[dev.did];
  if (cached) {
    renderPanel(dev, cached);
  } else {
    $("#dm-body").innerHTML = `
      <div class="sk-card" style="height:86px;margin-bottom:12px"></div>
      <div class="sk-card" style="height:96px;margin-bottom:12px"></div>
      <div class="sk-card" style="height:96px"></div>`;
  }

  const r = await api("/api/panel/" + encodeURIComponent(dev.did));
  if (state.currentDid !== dev.did) return;
  if (!r.ok) {
    if (!cached) $("#dm-body").innerHTML = `<div class="ctrl-note">${esc(r.error || "读取失败")}</div>`;
    return;
  }
  state.panels[dev.did] = r;
  // 刚操作过 / 正在放视频时不要重绘，免得把用户的操作或画面冲掉
  const fresh = Date.now() - (state.touched[dev.did] || 0) < 4000;
  if (cached && (fresh || state.cam)) return;
  renderPanel(dev, r);
}

function paintHero(dev) {
  const lv = liveOf(dev.did);
  const on = dev.switchable && !!lv.on;
  const kc = KIND_COLOR[dev.kind] || "#2F6BFF";
  const drawer = $(".drawer");
  drawer.style.setProperty("--kc", kc);
  const hi = $("#dm-icon");
  hi.className = "hero-icon" + (on ? " on" : "");
  hi.innerHTML = iconOf(dev.kind);
  $("#dm-name").textContent = dev.name;
  const parts = [dev.room || "未分组",
                 dev.stale ? "待确认" : (dev.online ? "在线" : "离线")];
  if (dev.ir) parts.push("红外遥控");
  else parts.push(KIND_NAME[dev.kind] || "设备");
  $("#dm-meta").textContent = parts.join(" · ");
}

function renderPanel(dev, data) {
  const body = $("#dm-body");
  body.innerHTML = "";
  state.touched[dev.did] = state.touched[dev.did] || 0;

  /* ---- 红外遥控 ---- */
  if (data.type === "ir") {
    renderIr(dev, data);
    return;
  }

  /* ---- 摄像头画面永远放最上面 ---- */
  if (data.camera) {
    const camBox = buildCamLive(dev);
    body.appendChild(camBox);
    // 打开抽屉直接开播，不用再点一下播放键
    const lv0 = liveOf(dev.did);
    if (dev.online && lv0.on !== false) {
      setTimeout(() => {
        if (state.currentDid === dev.did && camBox.isConnected && !state.cam) {
          startCam(dev, camBox._ui);
        }
      }, 400);
    }
  }

  const controls = (data.controls || []).filter(c => c.type !== "skip");
  // 只有卡片上真给了开关的设备，才把 on 抽出来做电源大按钮
  const onCtrl = controls.find(c => c.name === "on" && c.weight === 0);
  const useTile = !!(dev.switchable && onCtrl);
  const rest = controls.filter(c => c !== (useTile ? onCtrl : null));
  const main = rest.filter(c => c.type !== "read" && c.group !== "extra");
  const extra = rest.filter(c => c.type !== "read" && c.group === "extra");
  const readonly = rest.filter(c => c.type === "read");

  /* ---- 电源 ---- */
  let tile = null;
  if (useTile) {
    tile = buildPowerTile(dev, onCtrl);
    body.appendChild(tile);
    paintPowerTile(dev);
  }

  /* ---- 关掉时的提示 + 主调节区（关闭时置灰） ---- */
  const offHint = document.createElement("div");
  offHint.className = "off-hint hidden";
  offHint.innerHTML = `${ICONS.info}<span>设备已关闭，打开后即可调节</span>`;
  if (tile) body.appendChild(offHint);

  const sec = document.createElement("div");
  sec.className = "ctl-zone";
  body.appendChild(sec);

  if (main.length) {
    sec.insertAdjacentHTML("beforeend", '<div class="sect-label">调节</div>');
    let buf = [];
    const flush = () => {
      if (!buf.length) return;
      sec.appendChild(buildRowList(dev, buf));
      buf = [];
    };
    for (const c of main) {
      if (c.type === "bool") { buf.push(c); continue; }
      flush();
      sec.appendChild(buildControl(dev, c));
    }
    flush();
  }

  /* ---- 更多设置（默认收起） ---- */
  if (extra.length) {
    const fold = document.createElement("div");
    fold.className = "fold";
    fold.innerHTML = `<button class="fold-head">${ICONS.chevron}更多设置 · ${extra.length}</button>
      <div class="fold-body"></div>`;
    fold.querySelector(".fold-head").onclick = () => fold.classList.toggle("open");
    const fb = fold.querySelector(".fold-body");
    let buf = [];
    const flush = () => {
      if (!buf.length) return;
      fb.appendChild(buildRowList(dev, buf));
      buf = [];
    };
    for (const c of extra) {
      if (c.type === "bool") { buf.push(c); continue; }
      flush();
      fb.appendChild(buildControl(dev, c));
    }
    flush();
    body.appendChild(fold);
  }

  /* ---- 只读状态 ---- */
  if (readonly.length) {
    body.insertAdjacentHTML("beforeend", `<div class="sect-label">${data.camera ? "状态" : "实时状态"}</div>`);
    body.appendChild(buildReadGrid(readonly));
  }

  /* ---- 完全没有可控项 ---- */
  if (!main.length && !extra.length && !data.camera) {
    body.insertAdjacentHTML("beforeend",
      '<div class="ctrl-note">这台设备没有可下发的控制项，数据由它自己上报</div>');
  }

  /* ---- 设备信息 ---- */
  const fold = document.createElement("div");
  fold.className = "fold";
  fold.innerHTML = `<button class="fold-head">${ICONS.chevron}设备信息</button>
    <div class="fold-body"></div>`;
  fold.querySelector(".fold-head").onclick = () => fold.classList.toggle("open");
  fold.querySelector(".fold-body").appendChild(buildInfoGrid(dev));
  body.appendChild(fold);

  syncPanelState(dev);
}

/* 电源大按钮 */
function buildPowerTile(dev, ctrl) {
  const wrap = document.createElement("button");
  wrap.className = "power-tile";
  wrap.innerHTML = `
    <div class="pt-text">
      <span class="pt-title">—</span>
      <span class="pt-sub">—</span>
    </div>
    <span class="pt-switch"></span>`;
  wrap.onclick = async () => {
    const cur = !!(liveOf(dev.did).on);
    const next = !cur;
    wrap.classList.add("busy");
    setLive(dev.did, "on", next);
    // 乐观更新时立即标记，防止并发轮询把状态盖回旧值
    markLocal(dev.did);
    paintPowerTile(dev);
    updateCard(dev);
    syncStart();
    const r = await post("/api/setprop", {
      did: dev.did, siid: ctrl.siid, piid: ctrl.piid, value: next,
    });
    syncEnd();
    wrap.classList.remove("busy");
    if (!r.ok) {
      setLive(dev.did, "on", cur);
      delete state.touched[dev.did];
      paintPowerTile(dev);
      updateCard(dev);
      toast("控制失败，设备可能离线", true);
      return;
    }
    markLocal(dev.did);
    ctrl.value = next;
    paintPowerTile(dev);
  };

  // 时钟按钮：给这台设备的开关设定时。
  // 放在电源按钮右上角，平时不抢眼，指到才显形。
  const clock = document.createElement("button");
  clock.className = "pt-clock";
  clock.type = "button";
  clock.title = "定时开关";
  clock.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none"
    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 1.9"/></svg>`;
  clock.onclick = (e) => {
    e.stopPropagation();
    if (window.Sched) window.Sched.openPanel(dev, ctrl.siid, ctrl.piid);
  };

  const host = document.createElement("div");
  host.className = "pt-host";
  host.appendChild(wrap);
  host.appendChild(clock);
  return host;
}

function paintPowerTile(dev) {
  const tile = $(".power-tile");
  if (!tile) return;
  const lv = liveOf(dev.did);
  const on = !!lv.on;
  if (!state.liveReady) {
    const t0 = tile.querySelector(".pt-title");
    const s0 = tile.querySelector(".pt-sub");
    if (t0) t0.textContent = "读取中…";
    if (s0) s0.textContent = "正在同步设备状态";
    return;
  }
  tile.classList.toggle("on", on);
  const sw = tile.querySelector(".pt-switch");
  if (sw) sw.classList.toggle("on", on);
  const t = tile.querySelector(".pt-title");
  const s = tile.querySelector(".pt-sub");
  if (t) t.textContent = on ? "已开启" : "已关闭";
  if (s) {
    if (on) {
      const sum = summaryOf(dev);
      s.textContent = sum.big
        ? `${sum.big}${sum.unit || ""} · ${sum.txt || ""}`
        : (sum.txt || "运行中");
    } else {
      s.textContent = "点按开启设备";
    }
  }
  syncPanelState(dev);
}

/* 关掉时把调节区置灰，避免"点了没用"的错觉 */
function syncPanelState(dev) {
  if (!dev.switchable) return;
  const on = !!liveOf(dev.did).on;
  /* 只有「调节」区跟着电源走。
     「更多设置」（指示灯 / 童锁 / 报警 / 延时关机）是**设置项**，设备关着
     也该能改：原来把这些 fold 一起 .disabled 掉，pointer-events 被关掉，
     用户关了指示灯就再也点不回来，整块面板像卡死 —— 这个坑踩过一次。 */
  const zone = $("#dm-body .ctl-zone");
  if (zone) zone.classList.toggle("disabled", !on);
  const hint = $("#dm-body .off-hint");
  if (hint) hint.classList.toggle("hidden", on);
}

/* 一组开关行 */
function buildRowList(dev, items) {
  const box = document.createElement("div");
  box.className = "row-list";
  for (const c of items) {
    const row = document.createElement("div");
    row.className = "row-item";
    const ico = rowIcon(c);
    row.innerHTML = `<div class="rl">${ico}<span>${esc(c.label)}</span></div>
      <button class="switch ${c.value ? "on" : ""}"></button>`;
    row.onclick = async () => {
      const btn = row.querySelector(".switch");
      const next = !btn.classList.contains("on");
      btn.classList.toggle("on", next);
      const ok = await pushProp(dev, c, next, row);
      if (!ok) btn.classList.toggle("on", !next);
    };
    box.appendChild(row);
  }
  return box;
}

function rowIcon(c) {
  const n = c.name || "";
  if (n.includes("swing")) return ICONS.swing;
  if (n.includes("lock")) return ICONS.lock;
  if (n.includes("alarm")) return ICONS.bell;
  if (n.includes("indicator") || n.includes("light")) return ICONS.bulb;
  if (n.includes("screen")) return ICONS.media;
  return ICONS.wind;
}

function buildReadGrid(items) {
  const grid = document.createElement("div");
  grid.className = "read-grid";
  for (const c of items) {
    const v = c.value == null ? "—"
      : (typeof c.value === "boolean" ? (c.value ? "开" : "关") : fmt(c.value));
    grid.insertAdjacentHTML("beforeend",
      `<div class="read-cell"><div class="k">${esc(c.label)}</div>
        <div class="v">${esc(v)}${c.unit ? `<small>${esc(c.unit)}</small>` : ""}</div></div>`);
  }
  return grid;
}

/* 单个控件：温度步进器 / 滑块 / 分段 */
function buildControl(dev, c) {
  if (c.type === "range" && c.name === "target-temperature") return buildStepper(dev, c);
  if (c.type === "range") return buildSlider(dev, c);
  return buildSegmented(dev, c);
}

function buildSlider(dev, c) {
  const min = c.min != null ? c.min : 0;
  const max = c.max != null ? c.max : 100;
  const step = c.step || 1;
  const val = c.value != null ? c.value : min;
  const wrap = document.createElement("div");
  wrap.className = "ctrl";
  const unit = c.unit || "";
  wrap.innerHTML = `
    <div class="ctrl-head">
      <span class="ctrl-name">${esc(c.label)}</span>
      <span class="ctrl-val">${fmt(val)}<small>${esc(unit)}</small></span>
    </div>
    <div class="slider-wrap"><input type="range" min="${min}" max="${max}" step="${step}" value="${val}"></div>
    <div class="slider-ends"><span>${fmt(min)}${esc(unit)}</span><span>${fmt(max)}${esc(unit)}</span></div>`;
  const range = wrap.querySelector("input");
  const label = wrap.querySelector(".ctrl-val");
  const paint = () => {
    const pct = ((range.value - min) / (max - min || 1)) * 100;
    range.style.setProperty("--pct", pct + "%");
  };
  paint();
  range.oninput = () => { label.innerHTML = `${fmt(range.value)}<small>${esc(unit)}</small>`; paint(); };
  range.onchange = async () => {
    const v = Number(range.value);
    const ok = await pushProp(dev, c, v, wrap);
    if (!ok) {
      range.value = c.value != null ? c.value : min;
      label.innerHTML = `${fmt(range.value)}<small>${esc(unit)}</small>`;
      paint();
    }
  };
  return wrap;
}

function buildStepper(dev, c) {
  const min = c.min != null ? c.min : 16;
  const max = c.max != null ? c.max : 30;
  const step = c.step || 1;
  const val = c.value != null ? c.value : Math.round((min + max) / 2);
  const wrap = document.createElement("div");
  wrap.className = "ctrl";
  wrap.innerHTML = `
    <div class="ctrl-head"><span class="ctrl-name">${esc(c.label)}</span></div>
    <div class="stepper">
      <button class="step-btn minus" aria-label="降低">${ICONS.minus}</button>
      <div class="step-val"><b>${fmt(val)}</b><em>${esc(c.unit || "")}</em></div>
      <button class="step-btn plus" aria-label="升高">${ICONS.plus}</button>
    </div>`;
  const b = wrap.querySelector("b");
  const minus = wrap.querySelector(".minus");
  const plus = wrap.querySelector(".plus");
  let cur = Number(val);
  let timer = null;

  const paint = () => {
    cur = Math.min(max, Math.max(min, cur));
    b.textContent = fmt(cur);
    minus.disabled = cur <= min;
    plus.disabled = cur >= max;
  };
  const commit = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const ok = await pushProp(dev, c, cur, wrap);
      if (!ok) { cur = c.value != null ? c.value : val; paint(); }
    }, 320);
  };
  minus.onclick = () => { cur -= step; paint(); commit(); };
  plus.onclick = () => { cur += step; paint(); commit(); };
  paint();
  return wrap;
}

function buildSegmented(dev, c) {
  const opts = c.options || [];
  const wrap = document.createElement("div");
  wrap.className = "ctrl";
  const grow = opts.length <= 5 ? " grow" : "";
  wrap.innerHTML = `<div class="ctrl-head"><span class="ctrl-name">${esc(c.label)}</span></div>
    <div class="segmented${grow}"></div>`;
  const seg = wrap.querySelector(".segmented");
  const unit = c.unit || "";
  for (const o of opts) {
    const btn = document.createElement("button");
    let lb = o.label;
    if (unit && /^-?\d+(\.\d+)?$/.test(lb)) lb = lb + unit;
    btn.textContent = lb;
    btn.className = "seg" + (o.v === c.value ? " on" : "");
    btn.onclick = async () => {
      if (btn.classList.contains("on")) return;
      const prev = seg.querySelector(".seg.on");
      seg.querySelectorAll(".seg").forEach(x => x.classList.remove("on"));
      btn.classList.add("on");
      const ok = await pushProp(dev, c, o.v, wrap);
      if (!ok) {
        btn.classList.remove("on");
        if (prev) prev.classList.add("on");
      }
    };
    seg.appendChild(btn);
  }
  return wrap;
}

/* 下发一个属性：自动管 busy 态、同步指示、卡片联动 */
const LIVE_KEY = {
  on: "on", brightness: "bright", "fan-level": "level", "speed-level": "level",
  "target-temperature": "temp_set", position: "pos", percentage: "pos", mode: "mode",
  swing: "swing", "horizontal-swing": "swing", "vertical-swing": "swing",
};

/* 这个控件改了之后，要不要顺手改卡片上的实时状态？
   ------------------------------------------------------------
   必须按「服务 + 属性」判断，不能只看属性名 —— 这是踩过的坑：
   风扇的「指示灯」在 spec 里也叫 `indicator-light:on`，只按名字查
   LIVE_KEY 就会把 `live.on` 写成 false，于是整张卡片显示「已关闭」、
   详情页的电源块也变「已关闭」，接着 syncPanelState 把「调节」区
   整块置灰（pointer-events:none），用户点了没反应，看起来就是"卡死"。
   同类的还有童锁、报警、摄像头开关（camera-control:on）。
   规则：只有「调节」区（group=main）里、且是主开关（weight 0）的 on
   才代表设备电源；其余一律只更新控件自己的 value。 */
function liveKeyOf(dev, c) {
  if (!c || c.group === "extra") return null;      // 更多设置里的项不动卡片
  if (c.name === "on" && c.weight !== 0) return null;  // 非主开关的 on
  // 摆风：只有和卡片上那个「摇头」按钮指向同一个属性时才联动。
  // 同时有左右+上下摆风的设备，按「上下摆风」不该改到卡片的摇头状态。
  if (c.name === "horizontal-swing" || c.name === "vertical-swing") {
    const s = dev && dev.swing;
    return s && s.name === c.name ? "swing" : null;
  }
  return LIVE_KEY[c.name] || null;
}

async function pushProp(dev, c, value, el) {
  const wrap = el && (el.classList.contains("ctrl") || el.classList.contains("row-list")
    ? el : el.closest(".ctrl, .row-list"));
  if (wrap) wrap.classList.add("busy");
  // 先标记"刚操作"，防止并发轮询把控件状态盖回旧值
  markLocal(dev.did);
  syncStart();
  const r = await post("/api/setprop", { did: dev.did, siid: c.siid, piid: c.piid, value });
  syncEnd();
  if (wrap) wrap.classList.remove("busy");
  if (!r.ok) { delete state.touched[dev.did]; toast("控制失败，设备可能离线", true); return false; }

  c.value = value;
  markLocal(dev.did);

  const k = liveKeyOf(dev, c);
  if (k) {
    setLive(dev.did, k, value);
    if (c.name === "mode") {
      const o = (c.options || []).find(x => x.v === value);
      setLive(dev.did, "mode_t", o ? o.label : "");
    }
    updateCard(dev);
    paintPowerTile(dev);
    // 电源变了要把「调节」区的可用性一起跟上，否则详情页还停在旧状态
    if (k === "on") syncPanelState(dev);
  }
  return true;
}

/* ---- 红外 ---- */
function renderIr(dev, data) {
  const body = $("#dm-body");
  const keys = data.keys || [];
  const tip = document.createElement("div");
  tip.className = "ir-banner";
  tip.innerHTML = keys.length
    ? `这是 <b>自定义红外遥控</b>，本身没有状态，按下即由红外发射器发码。<br>共 ${keys.length} 个按键，按一下试试。`
    : `这是 <b>自定义红外遥控</b>，但这台设备还没有按键。<br>请先在米家 App 里添加按键，再回来刷新。`;
  body.appendChild(tip);

  if (keys.length) {
    const grid = document.createElement("div");
    grid.className = "ir-grid";
    for (const k of keys) {
      const btn = document.createElement("button");
      btn.className = "ir-btn";
      btn.innerHTML = `<span class="ico">${iconOf(dev.kind)}</span>
        <span class="lb">${esc(k.display_name || k.name || "按键")}</span>`;
      btn.onclick = () => pressIrKey(dev, k, btn);
      grid.appendChild(btn);
    }
    body.appendChild(grid);
    body.insertAdjacentHTML("beforeend",
      `<div class="ir-tip">按键列表来自米家 App 里这台遥控器的配置，想增删按键请到米家 App 编辑后回来刷新。</div>`);
  }

  const fold = document.createElement("div");
  fold.className = "fold open";
  fold.innerHTML = `<button class="fold-head">${ICONS.chevron}设备信息</button>
    <div class="fold-body"></div>`;
  fold.querySelector(".fold-head").onclick = () => fold.classList.toggle("open");
  fold.querySelector(".fold-body").appendChild(buildInfoGrid(dev));
  body.appendChild(fold);
}

async function pressIrKey(dev, key, btn) {
  btn.classList.add("firing");
  setTimeout(() => btn.classList.remove("firing"), 480);
  syncStart();
  const r = await post("/api/ir/press", { did: dev.did, key_id: key.id });
  syncEnd();
  if (!r.ok) toast("发送失败，红外发射器可能离线", true);
}

function buildInfoGrid(dev) {
  const grid = document.createElement("div");
  grid.className = "read-grid";
  const lv = liveOf(dev.did);
  const cells = [
    ["状态", dev.online ? "在线" : "离线"],
    ["房间", dev.room || "未分组"],
    ["类型", dev.ir ? "红外遥控" : (KIND_NAME[dev.kind] || "其他设备")],
    ["型号", dev.model || "—"],
  ];
  if (lv.temp != null) cells.push(["温度", fmt(lv.temp) + " °C"]);
  if (lv.hum != null) cells.push(["湿度", fmt(lv.hum) + " %"]);
  if (lv.batt != null) cells.push(["电量", fmt(lv.batt) + " %"]);
  for (const [k, v] of cells) {
    grid.insertAdjacentHTML("beforeend",
      `<div class="read-cell"><div class="k">${esc(k)}</div>
        <div class="v sm">${esc(v)}</div></div>`);
  }
  return grid;
}

/* ================= 摄像头 ================= */

/* 云台一动，画面立刻跳到直播边缘。
   不这么做的话：云台指令 350ms 就到位了，但 HLS 还落后 2~4 秒，
   用户按完看不到画面变化，会以为"没反应"然后一直按 —— 这才是
   "云台点了半天没反应"的真实体感，不是指令慢。 */
function jumpToLive() {
  const h = state.cam && state.cam.hls;
  if (!h || typeof h.liveSyncPosition !== "number") return;
  const v = h.media || (state.cam && state.cam.video);
  if (!v) return;
  if (Math.abs(v.currentTime - h.liveSyncPosition) > 0.4) {
    try { v.currentTime = h.liveSyncPosition; } catch (e) {}
  }
}
function buildCamLive(dev) {
  const box = document.createElement("div");
  box.className = "cam-box";
  box.innerHTML = `
    <div class="cam-stage">
      <video class="cam-video" playsinline muted preload="none"></video>
      <div class="cam-cover">
        <button class="cam-play" aria-label="播放">
          <svg viewBox="0 0 24 24" width="26" height="26" fill="currentColor" stroke="none">
            <path d="M8.5 5.6c0-1 1.1-1.6 1.9-1L19 11c.8.5.8 1.6 0 2.1l-8.6 6.4c-.8.5-1.9-.1-1.9-1z"/>
          </svg>
        </button>
        <p class="cam-hint">点击观看实时画面</p>
        <p class="cam-sub">首次连接约 10–20 秒（走小米 P2P 打洞）</p>
      </div>
      <div class="cam-loading hidden"><span class="spin"></span><em>正在连接摄像头…</em></div>
    </div>
    <div class="cam-bar">
      <span class="cam-state"><i></i>未连接</span>
      <div class="cam-acts">
        <button class="cam-btn cam-full hidden">全屏</button>
        <button class="cam-btn cam-stop hidden">停止</button>
      </div>
    </div>
    <div class="cam-ptz">
      <div class="ptz-pad">
        <button class="ptz-btn ptz-up" data-dir="up" aria-label="向上" title="向上">${ICONS.chevron}</button>
        <button class="ptz-btn ptz-left" data-dir="left" aria-label="向左" title="向左">${ICONS.chevron}</button>
        <span class="ptz-hub" aria-hidden="true"></span>
        <button class="ptz-btn ptz-right" data-dir="right" aria-label="向右" title="向右">${ICONS.chevron}</button>
        <button class="ptz-btn ptz-down" data-dir="down" aria-label="向下" title="向下">${ICONS.chevron}</button>
      </div>
      <span class="ptz-label">云台 · 点一下转一步，按住连续转</span>
    </div>
    <div class="cam-err hidden"><span class="cam-err-txt"></span>
      <button class="cam-retry" type="button">重试</button></div>`;

  const video = box.querySelector(".cam-video");
  const cover = box.querySelector(".cam-cover");
  const loading = box.querySelector(".cam-loading");
  const stateEl = box.querySelector(".cam-state");
  const btnFull = box.querySelector(".cam-full");
  const btnStop = box.querySelector(".cam-stop");
  const errBox = box.querySelector(".cam-err");
  const errTxt = box.querySelector(".cam-err-txt");

  const setState = (txt, cls) => {
    stateEl.className = "cam-state" + (cls ? " " + cls : "");
    stateEl.innerHTML = `<i></i>${esc(txt)}`;
  };

  const ui = { box, video, cover, loading, btnFull, btnStop, errBox, errTxt, setState };
  box.querySelector(".cam-play").onclick = () => startCam(dev, ui);
  box.querySelector(".cam-retry").onclick = () => startCam(dev, ui);
  box._ui = ui;
  btnStop.onclick = () => { stopCam(); setState("未连接"); cover.classList.remove("hidden"); };
  btnFull.onclick = () => {
    if (video.requestFullscreen) video.requestFullscreen();
    else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
  };
  // 云台：前端只发"画面方向"（up/down/left/right），倒挂/镜像的坐标换算全在后端，
  // 所以以后换安装方式、换型号都不用改这里。设备是步进式的（没有"停止"指令），
  // 按住就是每隔一小段补一步。
  box.querySelectorAll(".ptz-btn").forEach(btn => {
    const dir = btn.dataset.dir;
    let busy = false, holdDelay = null, holdTimer = null;

    const stopHold = () => {
      clearTimeout(holdDelay); clearInterval(holdTimer);
      holdDelay = holdTimer = null;
    };
    const send = () => {
      if (busy) return;                       // 上一条还没回来，别叠着发
      busy = true;
      btn.classList.add("pressed");           // 按下立刻反馈，不等云端往返
      fetch("/api/camera/motor/" + encodeURIComponent(dev.did), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dir }),
      }).then(r => r.json()).then(j => {
        busy = false;
        if (j && j.ok) {
          // 成功：留一小段高亮，让"这一下点到了"看得见
          setTimeout(() => btn.classList.remove("pressed"), 90);
          // 指令已到位，但画面还落后 1~4 秒 —— 拉到直播边缘，让用户
          // 按完马上就能看到新角度（连续按时每步都追一次）
          jumpToLive();
          return;
        }
        btn.classList.remove("pressed");
        if (j && j.limit) { stopHold(); toast(j.error || "云台已到限位", true); return; }
        toast((j && j.error) || "云台控制失败", true);
      }).catch(() => {
        busy = false;
        btn.classList.remove("pressed");
        toast("云台控制失败，网络异常", true);
      });
    };
    const startHold = () => {
      // 270ms 一次：单条指令云端往返约 350ms，再快也只是排队，
      // 但比原来的 450ms 明显更跟手（单飞模式下实际节奏 = max(270, RTT)）
      holdDelay = setTimeout(() => { holdTimer = setInterval(send, 270); }, 450);
    };

    btn.onmousedown = (e) => { e.preventDefault(); send(); startHold(); };
    btn.onmouseup = btn.onmouseleave = stopHold;
    btn.ontouchstart = (e) => { e.preventDefault(); send(); startHold(); };
    btn.ontouchend = btn.ontouchcancel = stopHold;
  });
  return box;
}

/* 按需加载 hls.js —— 它 414KB，只在真要看摄像头画面时才用得上。
   加载一次之后 window.Hls 就常驻，后续切摄像头不再走网络。 */
let hlsLoading = null;
function ensureHls() {
  if (window.Hls) return Promise.resolve();
  if (hlsLoading) return hlsLoading;   // 连点两次不要拉两份
  hlsLoading = new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = "/static/hls.min.js";
    s.async = true;
    s.onload = () => res();
    s.onerror = () => { hlsLoading = null; rej(new Error("下载失败")); };
    document.head.appendChild(s);
  });
  return hlsLoading;
}

async function startCam(dev, ui) {
  stopCam();
  ui.errBox.classList.add("hidden");
  ui.cover.classList.add("hidden");
  ui.loading.classList.remove("hidden");
  ui.btnStop.classList.add("hidden");
  ui.btnFull.classList.add("hidden");
  ui.setState("正在连接…", "wait");
  const lt = ui.loading.querySelector("em");
  if (lt) lt.textContent = "正在连接摄像头…（首次 5–15 秒）";

  // 后端会一直等到"首屏可播"（最多 22 秒）才返回，所以这里不需要再猜时间
  const r = await api("/api/cam/" + encodeURIComponent(dev.did));
  if (!r.ok) {
    ui.loading.classList.add("hidden");
    ui.cover.classList.remove("hidden");
    ui.errTxt.textContent = r.error || "无法开启实时画面";
    ui.errBox.classList.remove("hidden");
    ui.setState("不可用", "bad");
    return;
  }
  if (state.currentDid && state.currentDid !== dev.did) return;   // 用户已经切走

  const video = ui.video;
  state.camTries = state.camTries || {};

  const ok = () => {
    state.camTries[dev.did] = 0;
    ui.loading.classList.add("hidden");
    ui.btnStop.classList.remove("hidden");
    ui.btnFull.classList.remove("hidden");
    ui.setState("播放中", "ok");
    clearTimeout(state.camTimer);
  };
  const fail = (msg) => {
    ui.loading.classList.add("hidden");
    ui.cover.classList.remove("hidden");
    ui.errTxt.textContent = msg;
    ui.errBox.classList.remove("hidden");
    ui.setState("连接失败", "bad");
    video.onerror = null;
    video.onplaying = null;
    stopCam();
  };

  const armTimeout = () => {
    clearTimeout(state.camTimer);
    state.camTimer = setTimeout(() => {
      if (video.readyState < 2) {
        fail("连接超时：摄像头可能休眠、被别的 App 占用，或不在同一网络。可点重试");
      }
    }, 30000);
  };

  // 硬失败（分片拉不到 / 解码失败 / 流中断）：整路重开，最多 3 次，再失败就明说
  const reconnect = async (why) => {
    const n = (state.camTries[dev.did] = (state.camTries[dev.did] || 0) + 1);
    if (n > 3) {
      fail("连续重连失败：" + (why || "视频流中断") +
           "。可点重试；仍不行请打开 /api/cam-diag 查看自检信息");
      return;
    }
    ui.setState(`连接中断，正在重连（${n}/3）…`, "wait");
    try { if (state.cam && state.cam.hls) state.cam.hls.destroy(); } catch (e) {}
    state.cam = null;
    await new Promise(res => setTimeout(res, 900 * n));
    if (state.currentDid !== dev.did) return;
    startCam(dev, ui);
  };

  const playNative = () => {
    state.cam = { hls: null, video };
    video.src = r.hls;
    video.onloadedmetadata = () => { video.play().catch(() => {}); };
    video.onplaying = () => ok();
    video.onerror = () => reconnect("浏览器无法解码该视频流");
    armTimeout();
  };

  // hls.min.js 有 414KB，而摄像头不开的时候它一行都用不上。
  // 以前它挂在 index.html 的 <head> 同步加载，等于每次开软件都先花几十毫秒
  // 解析一份用不到的库。现在等到真要播视频才拉，拉一次之后走缓存。
  try {
    await ensureHls();
  } catch (e) {
    return fail("视频解码库加载失败：" + ((e && e.message) || "网络错误"));
  }

  if (!window.Hls || !Hls.isSupported()) {
    if (video.canPlayType("application/vnd.apple.mpegurl")) return playNative();
    return fail("当前环境不支持 HLS 播放（需要 MSE），请用系统自带的「米家」窗口打开");
  }

  // 延迟预算：分片 1 秒一个，playlist 保留 5 片。
  // 起播点离直播边缘越远，画面越"慢"。原来 liveSyncDurationCount=2 意味着
  // 落后 2 片（≈2 秒），再加编码 0.5 秒、P2P 中继，端到端 4 秒开外 ——
  // 这是"云台点了半天没反应"的主因（指令 350ms 就到了，画面要 4 秒后才动）。
  // 收到 1.5 片（≈1.5 秒）：仍留了半秒抗抖动，但明显更跟手。
  const hls = new Hls({
    lowLatencyMode: false,
    liveSyncDurationCount: 1.5,
    liveMaxLatencyDurationCount: 3,
    maxBufferLength: 2,
    maxMaxBufferLength: 3,
    maxLiveSyncPlaybackRate: 1.5,
    backBufferLength: 0,
    enableWorker: true,
    manifestLoadingTimeOut: 20000,
    manifestLoadingMaxRetry: 4,
    manifestLoadingRetryDelay: 600,
    levelLoadingTimeOut: 20000,
    levelLoadingMaxRetry: 6,
    fragLoadingTimeOut: 20000,
    fragLoadingMaxRetry: 6,
    fragLoadingRetryDelay: 500,
  });
  state.cam = { hls, video };

  let soft = 0;
  video.onplaying = () => ok();
  hls.on(Hls.Events.MANIFEST_PARSED, () => { video.play().catch(() => {}); });

  hls.on(Hls.Events.ERROR, (_e, data) => {
    if (!data) return;
    const d = data.details || "";
    if (!data.fatal) {
      // 非致命的缓冲抖动：hls.js 自己会恢复，只更新一下提示
      if (d === "bufferStalledError" || d === "bufferNudgeOnStall") {
        ui.setState("缓冲中…", "wait");
      }
      return;
    }
    // 分片解析/缓冲类：先小退避重载，别急着重开整路（重开代价 5–10 秒）
    if (d === "fragParsingError" || d === "bufferStalledError" ||
        d === "bufferAppendError" || d === "bufferNudgeOnStall") {
      if (soft++ < 5) {
        ui.setState(`缓冲恢复 ${soft}/5…`, "wait");
        setTimeout(() => { try { hls.startLoad(); } catch (e) {} }, Math.min(400 * soft, 2000));
        return;
      }
    }
    reconnect(d || "视频流中断");
  });

  hls.loadSource(r.hls);
  hls.attachMedia(video);
  armTimeout();
}

function stopCam() {
  clearTimeout(state.camTimer);
  const c = state.cam;
  state.cam = null;
  if (!c) return;
  // 先解绑事件，避免已销毁的实例再触发 onerror/onplaying 回调
  try {
    c.video.onerror = null;
    c.video.onplaying = null;
    c.video.onloadedmetadata = null;
  } catch (e) {}
  try { if (c.hls) c.hls.destroy(); } catch (e) {}
  try {
    c.video.pause();
    c.video.removeAttribute("src");
    c.video.load();
  } catch (e) {}
}

function closeDevice() {
  stopCam();
  $("#device-modal").classList.add("hidden");
  state.currentDid = null;
  state.currentDev = null;
}

/* ---------------- 后台挂起（托盘） ----------------
 * 窗口进托盘后，定时器已经被 suspend.js 从源头掐掉；这里补两件
 * 只有模块作用域能做的事：
 *   1. 停摄像头 —— ffmpeg + go2rtc 那条转码链常驻要吃两三百 MB，
 *      后台留着纯属浪费；恢复时面板还开着就自动续播。
 *   2. 恢复时立刻补一轮数据，别让用户对着旧状态等 4 秒。 */
window.MiHome = {
  onSuspend() {
    if (!state.cam) return;
    state.camWasPlaying = true;
    stopCam();
  },
  onResume() {
    const wasPlaying = state.camWasPlaying;
    state.camWasPlaying = false;
    if (!state.fromCache && !document.hidden) {
      refreshQuick();
      refreshLive();
    }
    if (!wasPlaying) return;
    // 面板还开着且还是同一台设备才续播；用户挂起前点过 ✕ 就不打扰
    const mask = $("#device-modal");
    if (mask.classList.contains("hidden") || !state.currentDev) return;
    const box = $("#dm-body .cam-box");
    if (box && box._ui) startCam(state.currentDev, box._ui);
  },
};

function openFromHash() {
  const h = location.hash || "";
  if (h === "#charger") return openCharger();
  const m = /did=([^&]+)/.exec(location.hash || "");
  if (!m) return;
  const did = decodeURIComponent(m[1]);
  const dev = state.devices.find(d => d.did === did);
  if (dev) openDevice(dev);
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, m => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[m]));
}

/* ============ 关闭行为询问 ============
   点窗口 ✕ 时 launcher 的 closing 事件通过 evaluate_js 调 window.MiHomeCloseAsk。
   用户的选择经 pywebview.api.close_choice 回传 Python（tray / exit + 记住选择）。 */
window.MiHomeCloseAsk = function () {
  const mask = $("#close-modal");
  if (!mask) return false;          // 页面还没渲染出弹窗 → 让 Python 走原生兜底
  const cb = $("#close-remember");
  if (cb) cb.checked = false;
  mask.classList.remove("hidden");
  return true;
};

function _closeChoice(action) {
  const mask = $("#close-modal");
  if (mask) mask.classList.add("hidden");
  const cb = $("#close-remember");
  const remember = !!(cb && cb.checked);
  if (window.pywebview && window.pywebview.api && window.pywebview.api.close_choice) {
    window.pywebview.api.close_choice(action, remember);
  }
}

(function wireCloseAsk() {
  const mask = $("#close-modal");
  if (!mask) return;
  $("#close-to-tray").addEventListener("click", () => _closeChoice("tray"));
  $("#close-exit").addEventListener("click", () => _closeChoice("exit"));
  $("#close-cancel").addEventListener("click", () => mask.classList.add("hidden"));
  mask.addEventListener("click", (e) => { if (e.target === mask) mask.classList.add("hidden"); });
})();

boot();
