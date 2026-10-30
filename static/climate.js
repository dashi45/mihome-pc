/* ==========================================================
   环境 · 温度曲线 + 温湿度读数
   ----------------------------------------------------------
   曲线只画温度（单 Y 轴）。湿度仍是三元组里的一项：数字行、增减量
   和下面那条 6px 能量条都要用，所以 push(room,temp,hum) 的数据形状
   保持不变，只是不再进折线图。
   数据来源两份，合成一条时间轴：
   1) 米家云端历史（/api/climate/history）—— 和米家 App 里那条曲线同源，
      一打开就有整天数据；
   2) 本机采样（localStorage，20 秒一个点）—— 只补在云端最新点之后，
      让曲线右端始终更新到“现在”。
   ========================================================== */
(function () {
  "use strict";

  const LKEY = "mi-cv-hist-v1";       // 本机采样（沿用旧键，历史不丢）
  const RKEY = "mi-cv-room-v2";       // 上次看的房间
  const SKEY = "mi-cv-span-v3";       // 时间范围
  const KEEP = 26 * 60 * 60 * 1000;   // 本机最多留 26 小时
  const BUCKET = 5 * 60 * 1000;       // 云端点按 5 分钟合桶

  const C_T = "#FF6A00";              // 温度（唯一的曲线颜色）

  const S = {
    room: "__all__",
    span: 86400000,
    cloud: {},        // did -> {temp: [[ts,v]], hum: [[ts,v]]}
    cloudAt: 0,
    busy: false,
    tried: 0,
    local: {},        // room -> [[ts,temp,hum]]
    hist: {},         // room -> [[ts,temp,hum]]
    ext: [],          // 外部传感器（/api/ext/sensors）
    extCloud: {},     // did -> {temp, hum}
    extLive: {},      // did -> {temp, hum}
    shown: null,
    err: "",
  };

  const $ = id => document.getElementById(id);
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const isAll = () => S.room === "__all__";
  const fmt1 = v => {
    const n = Math.round(v * 10) / 10;
    return Math.abs(n - Math.round(n)) < 0.05 ? String(Math.round(n)) : n.toFixed(1);
  };

  /* ---------------- 设备 / 当前值 ---------------- */
  /* 外部传感器在环境卡里当"虚拟设备"用：did 统一是 ext:<id>，
     这样合桶、按房间聚合、算平均都不用再写一套分支。 */
  function extDevices() {
    return (S.ext || []).map(s => ({
      did: s.did, room: s.room || "外部设备", name: s.name, ext: true,
    }));
  }

  function devicesOf(room) {
    const all = ((typeof state !== "undefined" && state.devices) || []).concat(extDevices());
    return room === "__all__"
      ? all
      : all.filter(d => (d.room || "未分组") === room);
  }

  function liveOf(did) {
    const live = (typeof state !== "undefined" && state.live) || {};
    return live[did] || S.extLive[did] || {};
  }

  function cloudOf(did) {
    return S.cloud[did] || S.extCloud[did] || null;
  }

  function curOf(room) {
    let tSum = 0, tN = 0, hSum = 0, hN = 0, n = 0;
    for (const d of devicesOf(room)) {
      const lv = liveOf(d.did);
      if (lv.temp != null && isFinite(lv.temp)) { tSum += lv.temp; tN++; }
      if (lv.hum != null && isFinite(lv.hum)) { hSum += lv.hum; hN++; }
      if (lv.temp != null || lv.hum != null) n++;
    }
    return {
      temp: tN ? tSum / tN : null,
      hum: hN ? hSum / hN : null,
      n,
    };
  }

  /* ---------------- 本机采样 ---------------- */
  function loadLocal() {
    try {
      const raw = localStorage.getItem(LKEY);
      S.local = raw ? (JSON.parse(raw) || {}) : {};
    } catch (e) { S.local = {}; }
    try {
      const r = localStorage.getItem(RKEY);
      if (r) S.room = r;
      const sp = Number(localStorage.getItem(SKEY));
      if (sp === 3600000 || sp === 21600000 || sp === 86400000) S.span = sp;
    } catch (e) {}
  }

  function saveLocal() {
    try { localStorage.setItem(LKEY, JSON.stringify(S.local)); } catch (e) {}
  }

  function push(room, temp, hum) {
    if (temp == null && hum == null) return;
    const now = Date.now();
    const arr = S.local[room] || (S.local[room] = []);
    const last = arr[arr.length - 1];
    if (last && now - last[0] < 12000 &&
        Math.abs((last[1] == null ? temp : last[1]) - temp) < 0.05 &&
        Math.abs((last[2] == null ? hum : last[2]) - hum) < 0.5) {
      last[0] = now;
      return;
    }
    arr.push([now,
      temp == null ? null : Math.round(temp * 10) / 10,
      hum == null ? null : Math.round(hum)]);
    while (arr.length && now - arr[0][0] > KEEP) arr.shift();
    if (arr.length > 1200) {
      const out = [];
      for (let i = 0; i < arr.length; i += 2) {
        const a = arr[i], b = arr[i + 1] || a;
        out.push([b[0],
          a[1] != null && b[1] != null ? Math.round((a[1] + b[1]) / 2 * 10) / 10
            : (a[1] != null ? a[1] : b[1]),
          a[2] != null && b[2] != null ? Math.round((a[2] + b[2]) / 2)
            : (a[2] != null ? a[2] : b[2])]);
      }
      S.local[room] = out;
    }
  }

  function sampleAll() {
    for (const r of ((typeof state !== "undefined" && state.rooms) || [])) {
      const c = curOf(r.name);
      if (c.temp != null || c.hum != null) push(r.name, c.temp, c.hum);
    }
    const a = curOf("__all__");
    if (a.temp != null || a.hum != null) push("__all__", a.temp, a.hum);
    saveLocal();
  }

  /* ---------------- 云端历史 ---------------- */
  function bucketAvg(pts) {
    const m = new Map();
    for (const p of pts) {
      const k = Math.round(p[0] / BUCKET) * BUCKET;
      const e = m.get(k);
      if (e) { e[1] += p[1]; e[2] += 1; } else m.set(k, [k, p[1], 1]);
    }
    return Array.from(m.values())
      .sort((a, b) => a[0] - b[0])
      .map(e => [e[0], e[1] / e[2]]);
  }

  function loadCloud(done) {
    if (S.busy) return;
    S.busy = true;
    S.tried = Date.now();
    fetch("/api/climate/history?hours=24")
      .then(r => r.json())
      .then(j => {
        if (j && j.ok && j.devices) {
          S.cloud = j.devices;
          S.cloudAt = Date.now();
          S.err = "";
        } else {
          S.err = (j && j.error) || "云端历史读取失败";
        }
      })
      .catch(e => { S.err = "云端历史读取失败：" + (e.message || e); })
      .finally(() => {
        S.busy = false;
        build();
        if (done) done();
      });
  }

  function maybeLoadCloud() {
    if (S.busy || S.cloudAt) return;
    if (Date.now() - S.tried < 8000) return;
    loadCloud(null);
  }

  /* ---------------- 外部传感器 ---------------- */
  /* 这些设备不在米家云端，历史由后端（extsensors.py）自己攒，26 小时。
     取回来直接塞进 extCloud，之后和云端数据走同一条合成路径。 */
  let extAt = 0, extBusy = false;
  function loadExt(done) {
    if (extBusy) return;
    extBusy = true;
    fetch("/api/ext/sensors")
      .then(r => r.json())
      .then(j => {
        if (!j || !j.ok) return;
        const list = j.sensors || [];
        S.ext = list;
        S.extCloud = {}; S.extLive = {};
        for (const s of list) {
          S.extCloud[s.did] = { temp: s.hist.temp || [], hum: s.hist.hum || [] };
          S.extLive[s.did] = { temp: s.temp, hum: s.hum };
        }
        extAt = Date.now();
      })
      .catch(() => {})
      .finally(() => { extBusy = false; if (done) done(); });
  }

  /* ---------------- 合成时间轴 ---------------- */
  function build() {
    const rooms = ["__all__"];
    for (const r of ((typeof state !== "undefined" && state.rooms) || [])) {
      rooms.push(r.name);
    }
    const hist = {};
    for (const room of rooms) {
      const tAll = [], hAll = [];
      let newest = 0;
      for (const d of devicesOf(room)) {
        const c = cloudOf(d.did);
        if (!c) continue;
        for (const p of (c.temp || [])) { tAll.push(p); if (p[0] > newest) newest = p[0]; }
        for (const p of (c.hum || [])) { hAll.push(p); if (p[0] > newest) newest = p[0]; }
      }
      const map = new Map();
      for (const pair of bucketAvg(tAll)) {
        map.set(pair[0], [pair[0], Math.round(pair[1] * 10) / 10, null]);
      }
      for (const pair of bucketAvg(hAll)) {
        const e = map.get(pair[0]);
        if (e) e[2] = Math.round(pair[1]);
        else map.set(pair[0], [pair[0], null, Math.round(pair[1])]);
      }
      for (const p of (S.local[room] || [])) {
        if (p[0] < newest - 60000) continue;
        map.set(p[0], [p[0], p[1], p[2]]);
      }
      hist[room] = Array.from(map.values()).sort((a, b) => a[0] - b[0]);
    }
    S.hist = hist;
  }

  /* ---------------- 数字 ---------------- */
  let anim = 0;
  function rollTo(el, to) {
    const from = S.shown;
    S.shown = to;
    cancelAnimationFrame(anim);
    if (el == null || from == null || Math.abs(to - from) < 0.05) {
      if (el) el.textContent = fmt1(to);
      return;
    }
    const t0 = performance.now();
    const step = now => {
      const k = clamp((now - t0) / 500, 0, 1);
      el.textContent = fmt1(from + (to - from) * (1 - Math.pow(1 - k, 3)));
      if (k < 1) anim = requestAnimationFrame(step);
    };
    anim = requestAnimationFrame(step);
  }

  function setDelta(el, now, first, eps, unit) {
    if (!el) return;
    if (now == null || first == null) {
      el.textContent = ""; el.className = ""; return;
    }
    const d = now - first;
    if (Math.abs(d) < eps) { el.textContent = "持平"; el.className = ""; return; }
    el.textContent = (d > 0 ? "↑" : "↓") +
      (unit === "%" ? Math.round(Math.abs(d)) : fmt1(Math.abs(d))) + unit;
    el.className = d > 0 ? "up" : "down";
  }

  function comfort(temp, hum) {
    if (temp == null) return { rank: "—", short: "没有温湿度数据" };
    let rank = "舒适", short = "温度湿度都合适";
    if (temp > 26 && temp <= 29) { rank = "偏热"; short = "有点热，可开空调"; }
    else if (temp > 29) { rank = "炎热"; short = "建议开空调"; }
    else if (temp < 18) { rank = "偏冷"; short = "注意保暖"; }
    else if (temp < 22) { rank = "微凉"; short = "略偏凉"; }
    if (hum != null) {
      if (hum > 72 && temp >= 24) { rank = "闷热"; short = "潮湿，建议除湿"; }
      else if (hum < 32) { rank = "干燥"; short = "空气干燥"; }
    }
    return { rank, short };
  }

  /* ---------------- 房间 chips ---------------- */
  let chipKey = "";
  function buildRooms() {
    const box = $("cv-rooms");
    if (!box) return;
    const names = [];
    for (const r of ((typeof state !== "undefined" && state.rooms) || [])) {
      if (names.indexOf(r.name) < 0) names.push(r.name);
    }
    // 外部传感器的房间可能压根不在米家里（比如自己起的"阳台"），也要给个 chip
    for (const s of (S.ext || [])) {
      const r = s.room || "外部设备";
      if (names.indexOf(r) < 0) names.push(r);
    }
    const items = [{ name: "__all__", label: "全屋" }];
    for (const n of names) items.push({ name: n, label: n });
    const key = items.map(i => i.name).join("|") + "#" + S.room;
    if (key === chipKey) return;
    chipKey = key;
    box.innerHTML = "";
    for (const it of items) {
      const b = document.createElement("button");
      b.className = "cv-chip" + (it.name === S.room ? " active" : "");
      b.textContent = it.label;
      b.onclick = () => setRoom(it.name);
      box.appendChild(b);
    }
  }

  function setRoom(name) {
    S.room = name;
    try { localStorage.setItem(RKEY, name); } catch (e) {}
    buildRooms();
    paint();
  }

  /* ---------------- 画图 ---------------- */
  /* 右边距只留时间刻度的半个字宽：右轴湿度已经去掉，不必再给刻度让位。 */
  const PAD = { l: 34, r: 18, t: 10, b: 20 };

  function timeLabel(ts) {
    const d = new Date(ts);
    return String(d.getHours()).padStart(2, "0") + ":" +
           String(d.getMinutes()).padStart(2, "0");
  }

  /* 平滑曲线：Catmull-Rom 转三次贝塞尔，控制点再夹回本段的 x/y 区间。

     原来那版是"每段水平出入"的贝塞尔（控制点取两点中点、切线一律水平），
     于是每个采样点都是一个平肩 + 一个硬拐角 —— 数据拐得越猛，看着越像折线，
     这就是"生硬"的来源。换成切线连续、且不允许冲出数据范围的样条后，
     曲线才会像米家那条一样顺着数据流过去，而不是拿直线段拼。 */
  function smoothPath(pts) {
    const n = pts.length;
    if (!n) return "";
    let d = `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
    if (n === 1) return d;
    if (n === 2) {
      return d + ` L${pts[1][0].toFixed(1)} ${pts[1][1].toFixed(1)}`;
    }
    for (let i = 0; i < n - 1; i++) {
      const p0 = pts[i - 1] || pts[i], p1 = pts[i];
      const p2 = pts[i + 1], p3 = pts[i + 2] || p2;
      // 1/6 是 Catmull-Rom 的标准系数：切线由前后两点决定，所以连着走不打架
      let c1x = p1[0] + (p2[0] - p0[0]) / 6;
      let c1y = p1[1] + (p2[1] - p0[1]) / 6;
      let c2x = p2[0] - (p3[0] - p1[0]) / 6;
      let c2y = p2[1] - (p3[1] - p1[1]) / 6;
      // 夹住控制点：只能落在这一段的 x/y 区间内。
      // 不夹的话，急升急降处曲线会冲过数据极值，看着像"甩出去"了一下。
      c1x = clamp(c1x, p1[0], p2[0]);
      c2x = clamp(c2x, p1[0], p2[0]);
      const lo = Math.min(p1[1], p2[1]), hi = Math.max(p1[1], p2[1]);
      c1y = clamp(c1y, lo, hi);
      c2y = clamp(c2y, lo, hi);
      d += ` C${c1x.toFixed(1)} ${c1y.toFixed(1)} ${c2x.toFixed(1)} ${c2y.toFixed(1)}` +
           ` ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
    }
    return d;
  }

  function pointsFor(room, idx, t0, now) {
    const all = (S.hist[room] || []).filter(p => p[idx] != null);
    const raw = [];
    for (const p of all) if (p[0] >= t0) raw.push([p[0], p[idx]]);
    for (let i = all.length - 1; i >= 0; i--) {
      if (all[i][0] < t0) { raw.unshift([t0, all[i][idx]]); break; }
    }
    if (raw.length && now - raw[raw.length - 1][0] > 2000) {
      raw.push([now, raw[raw.length - 1][1]]);
    }
    return raw;
  }

  /* 轻平滑：传感器上报有毛刺，三点加权让它顺一点。
     权重从 1:2:1 放到 1:4:1 —— 原来的太狠，会把真实的起伏（比如夜里那点
     小回温）一起抹平，看着反而假；现在只削掉单点毛刺，形状留给样条去圆。 */
  function soft(pts) {
    if (pts.length < 4) return pts;
    const out = pts.map(p => [p[0], p[1]]);
    for (let i = 1; i < pts.length - 1; i++) {
      out[i][1] = (pts[i - 1][1] + 4 * pts[i][1] + pts[i + 1][1]) / 6;
    }
    return out;
  }

  function axisOf(vals, minSpan, steps) {
    if (!vals.length) return null;
    let lo = Infinity, hi = -Infinity;
    for (const v of vals) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    const mid = (lo + hi) / 2;
    if (hi - lo < minSpan) { lo = mid - minSpan / 2; hi = mid + minSpan / 2; }
    // 目标 4~6 条网格线：以前按 span/3 选步长，8° 的温度跨度会直接跳到 5°
    // 一档，20~35 的轴把 24~32 的线压在下半截，上面空一大块。改成 span/5 后，
    // 8° 用 2° 一档，轴刚好收在 24~32，曲线能用满高度。
    const step = steps.find(c => c >= (hi - lo) / 5) || steps[steps.length - 1];
    return { lo: Math.floor(lo / step) * step, hi: Math.ceil(hi / step) * step, step };
  }

  function draw() {
    const wrap = $("cv-chart-wrap");
    const svg = $("cv-chart");
    const gGrid = $("cv-grid"), gArea = $("cv-area"), gLines = $("cv-lines"),
          gAxis = $("cv-axis"), gMark = $("cv-marker");
    const empty = $("cv-empty");
    if (!wrap || !svg || !gGrid) return;

    // 尺寸夹紧是"防复发"的第二道闸：即使以后有人把容器改回 flex:1，
    // viewBox 也不会跟着容器无限长（那会让曲线越来越扁，卡片越撑越高）。
    const W = Math.round(wrap.clientWidth);
    const H = Math.min(Math.round(wrap.clientHeight), 480);
    if (W < 60 || H < 40) return;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

    const PW = W - PAD.l - PAD.r;
    const PH = H - PAD.t - PAD.b;
    const now = Date.now();
    const span = S.span;
    const unit = span <= 3600000 ? 900000 : 3600000;
    const tEnd = Math.ceil(now / unit) * unit;
    const t0 = tEnd - span;
    const X = t => PAD.l + PW * clamp((t - t0) / span, 0, 1);

    const room = isAll() ? "__all__" : S.room;
    const tPts = pointsFor(room, 1, t0, now);
    /* 湿度仍是三元组里的 p[2]（数字行和能量条要用），但不参与画图。
       右轴只属于湿度，去掉序列就必须一起收掉，否则会留下一条没有线的刻度。 */
    if (empty) empty.classList.toggle("hidden", tPts.length >= 2);

    const at = axisOf(tPts.map(p => p[1]), 1.2, [0.2, 0.5, 1, 2, 5]);
    const yT = at ? (v => PAD.t + PH * (1 - clamp((v - at.lo) / (at.hi - at.lo), 0, 1))) : null;

    // 曲线右端就是"现在"：只留一个端点圆点（带一圈淡光晕），
    // 不再画竖虚线 + 时间胶囊 —— 参考版那种克制的收尾更耐看，
    // 右端贴边本身已经说明"最新值在这"。
    let grid = "", axis = "";
    if (at) {
      const n = Math.round((at.hi - at.lo) / at.step);
      for (let i = 0; i <= n; i++) {
        const v = at.lo + at.step * i, y = yT(v);
        grid += `<line x1="${PAD.l}" y1="${y.toFixed(1)}" x2="${(PAD.l + PW).toFixed(1)}"
          y2="${y.toFixed(1)}" stroke="var(--line)" stroke-width="1"
          stroke-dasharray="2 6" opacity=".55"/>`;
        axis += `<text x="${(PAD.l - 7).toFixed(1)}" y="${(y + 3.4).toFixed(1)}"
          text-anchor="end" font-size="9.5" fill="var(--faint)">${fmt1(v)}°</text>`;
      }
    }

    // 时间刻度对齐到整点（1 小时视图对齐到整刻），不会出现 05:48 这种怪值
    const stepMs = span >= 86400000 ? 6 * 3600000
      : span >= 21600000 ? 2 * 3600000 : 15 * 60000;
    const dt = new Date(t0);
    dt.setSeconds(0, 0);
    dt.setMinutes(stepMs >= 3600000 ? 0 : Math.ceil(dt.getMinutes() / 15) * 15);
    let tk = dt.getTime();
    if (tk < t0) tk += stepMs;
    for (; tk <= tEnd + 1; tk += stepMs) {
      const x = X(tk);
      if (x < PAD.l - 1 || x > PAD.l + PW + 1) continue;
      axis += `<text x="${x.toFixed(1)}" y="${(H - 3).toFixed(1)}"
        text-anchor="middle" font-size="9.5" fill="var(--faint)">${timeLabel(tk)}</text>`;
    }

    const baseY = PAD.t + PH;
    let lines = "", area = "", dots = "", mark = "";

    if (tPts.length >= 2) {
      const pts = soft(tPts).map(p => [X(p[0]), yT(p[1])]);
      const d = smoothPath(pts);
      const end = pts[pts.length - 1];
      area += `<path d="${d} L${end[0].toFixed(1)} ${baseY.toFixed(1)}
        L${pts[0][0].toFixed(1)} ${baseY.toFixed(1)} Z" fill="url(#cv-area-g)"/>`;
      lines += `<path d="${d}" fill="none" stroke="${C_T}" stroke-width="2.2"
        stroke-linecap="round" stroke-linejoin="round"/>`;
      // 端点：一圈很淡的光晕 + 一个空心圆，收住整条线
      dots += `<circle cx="${end[0].toFixed(1)}" cy="${end[1].toFixed(1)}" r="7.5"
        fill="${C_T}" opacity=".13"/>
      <circle cx="${end[0].toFixed(1)}" cy="${end[1].toFixed(1)}" r="3.3"
        fill="var(--card)" stroke="${C_T}" stroke-width="2.3"/>`;
    }

    mark += dots;

    gGrid.innerHTML = grid;
    gArea.innerHTML = area;
    gLines.innerHTML = lines;
    gAxis.innerHTML = axis;
    gMark.innerHTML = mark;

    const host = $("env-section");
    if (host) host.style.setProperty("--cv-c1", C_T);
  }

  /* ---------------- 主刷新 ---------------- */
  /* 当前时间窗内的温度最低/最高。和 draw() 用同一套"整点对齐"的时间轴，
     所以切 1 小时 / 6 小时 / 24 小时时，右边这个区间会跟着变。 */
  function tempRange(room, span) {
    const now = Date.now();
    const unit = span <= 3600000 ? 900000 : 3600000;
    const t0 = Math.ceil(now / unit) * unit - span;
    let lo = Infinity, hi = -Infinity;
    for (const p of (S.hist[room] || [])) {
      if (p[1] == null || p[0] < t0) continue;
      if (p[1] < lo) lo = p[1];
      if (p[1] > hi) hi = p[1];
    }
    return lo <= hi ? { lo, hi } : null;
  }

  let lastEvt = 0;
  function paint() {
    if (!$("env-section")) return;
    buildRooms();
    sampleAll();
    build();

    const spanTxt = S.span >= 86400000 ? "24 小时"
      : S.span >= 21600000 ? "6 小时" : "1 小时";
    const c = curOf(S.room);
    const tEl = $("cv-temp");
    if (tEl) {
      if (c.temp != null) rollTo(tEl, c.temp);
      else { cancelAnimationFrame(anim); tEl.textContent = "—"; S.shown = null; }
    }
    const hEl = $("cv-hum-val");
    if (hEl) hEl.textContent = c.hum != null ? String(Math.round(c.hum)) : "—";

    /* 湿度能量条：宽度就是湿度百分比，60% 以上转暖色提醒"该除湿了"。
       只改 style.width 和 class，不参与布局动画，5 秒一轮也不掉帧。 */
    const hBar = $("cv-hum-bar"), hFill = $("cv-hum-fill");
    const hv = c.hum == null ? 0 : clamp(c.hum, 0, 100);
    if (hFill) hFill.style.width = hv.toFixed(1) + "%";
    if (hBar) {
      hBar.classList.toggle("warm", hv >= 60);
      hBar.setAttribute("aria-valuenow", String(Math.round(hv)));
      hBar.title = c.hum == null ? "没有湿度数据"
        : `湿度 ${Math.round(c.hum)}%${hv >= 60 ? "（偏高，建议除湿）" : ""}`;
    }

    /* 右侧"本段区间"：填掉体感右边那块空白，也给曲线一个数字上的极值 */
    const range = tempRange(S.room, S.span);
    const rlo = $("cv-range-lo"), rhi = $("cv-range-hi");
    if (rlo) rlo.textContent = range ? fmt1(range.lo) : "—";
    if (rhi) rhi.textContent = range ? fmt1(range.hi) : "—";
    const rsub = $("cv-range-sub");
    if (rsub) rsub.textContent = `${spanTxt}最低 / 最高`;

    const pts = (S.hist[S.room] || []);
    const first = pts.length ? pts[0] : null;
    setDelta($("cv-temp-d"), c.temp, first ? first[1] : null, 0.15, "°");
    setDelta($("cv-hum-d"), c.hum, first ? first[2] : null, 1, "%");

    const cf = comfort(c.temp, c.hum);
    const rank = $("cv-comf-rank");
    if (rank) rank.textContent = cf.rank;
    const tip = $("cv-comf-tip");
    if (tip) tip.textContent = cf.short;

    const sub = $("cv-chart-sub");
    if (sub) {
      const src = S.cloudAt ? "云端" : "本机";
      // 只数带温度的点：曲线现在只画温度，别把"只有湿度"的点算成有曲线
      const has = (S.hist[S.room] || []).filter(p => p[1] != null).length;
      const extN = (S.ext || []).filter(s =>
        isAll() || (s.room || "外部设备") === S.room).length;
      const nTxt = extN ? `${c.n} 个传感器（含 ${extN} 个外部）` : `${c.n} 个传感器`;
      sub.textContent = has >= 2
        ? `${nTxt} · ${src} ${spanTxt}`
        : (S.err || "正在读取米家云端记录…");
    }

    const et = $("cv-empty-t"), es = $("cv-empty-s");
    if (et) et.textContent = S.err ? S.err : "正在读取米家云端温湿度记录…";
    if (es) es.textContent = S.err ? "稍后会自动重试" : "米家 App 里那条曲线就是这些记录";

    // 室外卡要拿室内读数算"开窗建议"，这里广播一次（节流，别让 5 秒轮询
    // 把天气卡也带着重绘）
    if (Date.now() - lastEvt > 2500) {
      lastEvt = Date.now();
      try {
        window.dispatchEvent(new CustomEvent("mi:climate", {
          detail: { temp: c.temp, hum: c.hum, rank: cf.rank, room: S.room, n: c.n },
        }));
      } catch (e) {}
    }

    draw();
  }

  /* ---------------- 交互 ---------------- */
  function wireRange() {
    const box = $("cv-range");
    if (!box) return;
    const sync = () => box.querySelectorAll("button").forEach(b =>
      b.classList.toggle("active", Number(b.dataset.span) === S.span));
    sync();
    box.onclick = ev => {
      const b = ev.target.closest("button");
      if (!b) return;
      S.span = Number(b.dataset.span);
      try { localStorage.setItem(SKEY, String(S.span)); } catch (e) {}
      sync();
      paint();
    };
  }

  function loop() {
    try { paint(); }
    catch (e) {
      console.warn("[climate]", e);
      const sub = $("cv-chart-sub");
      if (sub) sub.textContent = "环境模块出错：" + (e.message || e);
    }
  }

  window.Climate = {
    init() {
      try { loadLocal(); } catch (e) {}
      wireRange();
      buildRooms();
      loadExt(loop);
      loadCloud(loop);
      loop();
    },
    show() { loop(); },
    hide() {},
    update() { loop(); },
    /* 给室外卡用：当前房间的室内读数 + 体感等级 */
    indoor() {
      const c = curOf(S.room);
      return {
        temp: c.temp, hum: c.hum, n: c.n,
        rank: comfort(c.temp, c.hum).rank,
        room: isAll() ? "全屋" : S.room,
      };
    },
    /* 外部传感器增删/改名之后重取一次 */
    reloadExt() { loadExt(loop); },
    followRoom(name) {
      if (name !== S.room && (name === "__all__" ||
          ((typeof state !== "undefined" && state.rooms) || []).some(r => r.name === name))) {
        setRoom(name);
      }
    },
  };

  /* ==========================================================
     外部传感器接入面板（米家之外的设备）
     ----------------------------------------------------------
     后端在 extsensors.py：跟米家账号没关系，用一把本地密钥认设备，
     所以 ESP32 这种没有登录态的东西也能用。
     ========================================================== */
  const esc = s => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  function agoText(ms) {
    const d = Math.max(0, Date.now() - ms);
    if (d < 60000) return "刚刚";
    if (d < 3600000) return Math.floor(d / 60000) + " 分钟前";
    if (d < 86400000) return Math.floor(d / 3600000) + " 小时前";
    return Math.floor(d / 86400000) + " 天前";
  }

  function extCode(key) {
    const host = location.host || "127.0.0.1:8765";
    const origin = location.protocol + "//" + host;
    // JSON 拆成多行写：一行 90 个字在弹窗里必然要折行，
    // 折在 "room" 中间比整段换行难读得多
    return `curl -X POST ${origin}/api/ext/push \\
  -H "Content-Type: application/json" \\
  -d '{
    "key": "${key}",
    "id": "esp32-living",
    "name": "客厅传感器",
    "room": "客厅",
    "temp": 24.6,
    "hum": 52
  }'`;
  }

  function renderExtList(list) {
    const box = $("ext-list"), lead = $("ext-lead2");
    if (lead) {
      lead.textContent = list.length
        ? `已接入 ${list.length} 个外部设备，会并进环境卡的同一张图`
        : "还没有外部设备接入";
    }
    if (!box) return;
    box.innerHTML = list.map(s => {
      const when = s.at ? agoText(s.at * 1000) : "从未上报";
      const vals = [
        s.temp != null ? s.temp + "°C" : "",
        s.hum != null ? Math.round(s.hum) + "%" : "",
        s.co2 != null ? s.co2 + "ppm" : "",
        s.pm25 != null ? "PM2.5 " + s.pm25 : "",
      ].filter(Boolean).join(" · ");
      return `<div class="ext-row" data-id="${esc(s.id)}">
        <div class="ext-row-main">
          <b>${esc(s.name)}</b>
          <span>${esc(s.room)} · ${when}</span>
        </div>
        <div class="ext-row-val">${vals || "—"}</div>
        <div class="ext-row-act">
          <button type="button" data-act="edit">编辑</button>
          <button type="button" data-act="del">删除</button>
        </div>
      </div>`;
    }).join("");
  }

  function extRefresh() {
    return fetch("/api/ext/sensors").then(r => r.json()).then(j => {
      if (!j || !j.ok) return;
      const key = $("ext-key"), code = $("ext-code");
      if (key) key.textContent = j.key || "—";
      if (code) code.textContent = extCode(j.key || "");
      renderExtList(j.sensors || []);
    }).catch(() => {});
  }

  function wireExtPanel() {
    const open = $("ext-open"), mask = $("ext-modal");
    if (!open || !mask) return;

    open.onclick = () => {
      mask.classList.remove("hidden");
      extRefresh();
    };
    mask.onclick = ev => { if (ev.target === mask) mask.classList.add("hidden"); };
    document.addEventListener("keydown", ev => {
      if (ev.key === "Escape") mask.classList.add("hidden");
    });

    const copy = $("ext-key-copy");
    if (copy) copy.onclick = () => {
      const txt = ($("ext-key") || {}).textContent || "";
      const done = ok => {
        copy.textContent = ok ? "已复制" : "复制失败";
        setTimeout(() => { copy.textContent = "复制"; }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(() => done(true)).catch(() => done(false));
      } else {
        const ta = document.createElement("textarea");
        ta.value = txt; document.body.appendChild(ta); ta.select();
        let ok = false;
        try { ok = document.execCommand("copy"); } catch (e) {}
        document.body.removeChild(ta);
        done(ok);
      }
    };

    const reset = $("ext-key-reset");
    if (reset) reset.onclick = () => {
      if (!window.confirm("重置后旧密钥立刻失效，已经接入的设备需要改配置。继续？")) return;
      fetch("/api/ext/key", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
        .then(r => r.json()).then(() => extRefresh());
    };

    const list = $("ext-list");
    if (list) list.onclick = ev => {
      const b = ev.target.closest("button[data-act]");
      const row = ev.target.closest(".ext-row");
      if (!b || !row) return;
      const id = row.dataset.id;
      if (b.dataset.act === "del") {
        if (!window.confirm("删除这个外部设备？历史数据也一起清掉。")) return;
        fetch("/api/ext/remove", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        }).then(r => r.json()).then(() => { extRefresh(); loadExt(loop); });
      }
      if (b.dataset.act === "edit") {
        const name = ((row.querySelector("b") || {}).textContent || "").trim();
        const sub = ((row.querySelector(".ext-row-main span") || {}).textContent || "");
        const room = sub.split(" · ")[0] || "";
        row.innerHTML = `<div class="ext-edit">
          <input class="ext-in ext-in-name" value="${esc(name)}" maxlength="24" placeholder="名称">
          <input class="ext-in ext-in-room" value="${esc(room)}" maxlength="24" placeholder="房间">
          <button type="button" class="btn-ghost" data-act="save">保存</button>
          <button type="button" class="btn-ghost" data-act="cancel">取消</button>
        </div>`;
      }
      if (b.dataset.act === "save") {
        const name = (row.querySelector(".ext-in-name") || {}).value || "";
        const room = (row.querySelector(".ext-in-room") || {}).value || "";
        fetch("/api/ext/update", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id, name, room }),
        }).then(r => r.json()).then(() => { extRefresh(); loadExt(loop); });
      }
      if (b.dataset.act === "cancel") extRefresh();
    };
  }

  document.addEventListener("DOMContentLoaded", () => {
    window.Climate.init();
    wireExtPanel();

    // 曲线右端一直顶到“现在”
    setInterval(() => {
      const v = $("env-section");
      if (!v || document.hidden || !v.offsetParent) return;
      maybeLoadCloud();
      loop();
    }, 5000);

    // 云端约 20 分钟才多一个点，10 分钟同步一次就够
    setInterval(() => {
      if (document.hidden) return;
      if (S.cloudAt && Date.now() - S.cloudAt < 600000) return;
      loadCloud(loop);
    }, 60000);

    // 外部传感器可能是几十秒一推，45 秒拉一次就够跟上
    setInterval(() => {
      if (document.hidden || !S.ext) return;
      if (Date.now() - extAt < 45000) return;
      loadExt(loop);
    }, 30000);

    const wrap = $("cv-chart-wrap");
    if (wrap && window.ResizeObserver) {
      let raf = 0, lastW = -1, lastH = -1;
      new ResizeObserver((ents) => {
        // 尺寸真的变了才重画。没有这道判断，观察者和被观察者会互相触发
        // （draw 改 viewBox → 容器变高 → 观察者又回调），浏览器会打出
        // "ResizeObserver loop completed with undelivered notifications"。
        const r = ents[0].contentRect;
        if (Math.abs(r.width - lastW) < 1 && Math.abs(r.height - lastH) < 1) return;
        lastW = r.width; lastH = r.height;
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => { try { draw(); } catch (e) {} });
      }).observe(wrap);
    }
  });
})();
