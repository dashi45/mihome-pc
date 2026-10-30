/* ==========================================================
   本机 —— CPU / 显卡 / 内存 / 磁盘 / 网络
   ----------------------------------------------------------
   数据来自 /api/pc（后端 pcsys.py 每秒采一次，这里 2 秒拉一次快照）。

   为什么不用指针表盘：网页上的圆表盘看着热闹，但读数要盯半天。
   这里用的是任务管理器"性能"页那套——一个大数字 + 一条走动的曲线，
   余光扫一眼就知道机器现在累不累。

   主指标可以点着换（CPU / 内存 / 显卡）：三台机器的"累不累"不是一回事，
   剪片子看显卡、跑模型看内存，同一块地方换个视角比并排三条曲线省地方。

   温度这件事得说清楚：
     · 显卡温度 / 风扇   —— nvidia-smi 直接给，最准
     · CPU 温度          —— Windows 用户态读不到 DTS，必须有个带内核驱动的
                            程序帮我们读。点"接入"会用 UAC 拉起 Core Temp，
                            之后这里自动出现每核读数；拿不到就显示"——"，
                            绝不拿 ACPI 温区那个常年 27.9℃ 的死数糊弄人。
   ========================================================== */
(function () {
  "use strict";

  const S = { data: null, busy: false, at: 0, err: "", timer: 0, metric: "cpu", joining: false };
  const $ = (id) => document.getElementById(id);

  const METRICS = {
    cpu: { name: "CPU", unit: "%", cap: "CPU 占用", key: "cpu", color: null },
    mem: { name: "内存", unit: "%", cap: "内存占用", key: "mem", color: "#0EA5E9" },
    gpu: { name: "显卡", unit: "%", cap: "显卡占用", key: "gpu", color: "#0E9F72" },
  };

  /* ---------- 小工具 ---------- */
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const num = (v, d) => (v == null || !isFinite(v) ? "—" : Number(v).toFixed(d == null ? 0 : d));

  function bytes(v) {
    if (v == null) return "—";
    const u = ["B", "K", "M", "G", "T"];
    let i = 0, x = Math.abs(v);
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
    return (x >= 100 || i === 0 ? Math.round(x) : x.toFixed(1)) + " " + u[i] + (i ? "B" : "");
  }
  // 速率压得更短：脚注一行要塞下 4 组数字，B/s 这种后缀能省就省
  function rate(v) {
    if (v == null) return "—";
    const u = ["", "K", "M", "G"];
    let i = 0, x = v;
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
    return (x >= 100 ? Math.round(x) : x.toFixed(1)) + u[i] + "B/s";
  }

  function dur(sec) {
    if (sec == null) return "—";
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    if (d) return d + " 天 " + h + " 小时";
    if (h) return h + " 小时 " + m + " 分";
    return m + " 分钟";
  }

  // "13th Gen Intel(R) Core(TM) i5-13600KF" → "i5-13600KF"
  // 卡片副标题就一行，型号才是人认的那部分，"13th Gen Intel(R) Core(TM)" 全是噪音。
  // 注意别把 "i5" 里的数字一起吃掉——所以先按型号规则整体匹配，不行再退回清洗。
  function shortCpu(name) {
    if (!name) return "";
    let m = name.match(/\b([iI][3579])[-\s]?(\d{4,5}[A-Za-z]{0,3})\b/);   // i5-13600KF
    if (m) return m[1] + "-" + m[2];
    m = name.match(/\b(Ryzen\s*)?([3579])\s?(\d{4}[A-Za-z]{0,3})\b/);      // Ryzen 7 5800X
    if (m) return "R" + m[2] + " " + m[3];
    m = name.match(/\b(Xeon|EPYC|Threadripper)\b[^,]*/i);                  // 服务器 U
    if (m) return m[0].trim();
    return name.replace(/\(R\)|\(TM\)|CPU|Processor/gi, " ").replace(/\s+/g, " ").trim();
  }

  const LOAD_COLORS = ["#6366F1", "#0EA5E9", "#F59E0B", "#EF4444"];
  function loadColor(pct) {
    if (pct == null) return LOAD_COLORS[0];
    if (pct >= 85) return LOAD_COLORS[3];
    if (pct >= 55) return LOAD_COLORS[2];
    if (pct >= 25) return LOAD_COLORS[1];
    return LOAD_COLORS[0];
  }
  function heatCls(pct, warm, hot) {
    if (pct == null) return "";
    return pct >= hot ? "hot" : pct >= warm ? "warm" : "";
  }

  /* ---------- 取数 ---------- */
  function load() {
    if (S.busy || document.hidden) return;
    if (!$("pc-card") || !$("pc-card").offsetParent) return;   // 充电器视图下不拉
    S.busy = true;
    fetch("/api/pc", { cache: "no-store" })
      .then((r) => {
        // 404 说明后台还是旧进程（本机采样是后加的接口），
        // 这时候说"读不到"比抛个 JSON 解析错误有用得多
        if (r.status === 404) throw new Error("后台还没重启，重启程序后本机数据才会出现");
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((j) => {
        if (j && j.ok) { S.data = j; S.at = Date.now(); S.err = ""; }
        else if (j) S.err = j.error || "读不到本机状态";
      })
      .catch((e) => { S.err = (e && e.message) || "网络异常"; })
      .finally(() => { S.busy = false; paint(); });
  }

  /* ---------- 主指标曲线 ---------- */
  function currentValue(d) {
    const m = S.metric;
    if (m === "mem") return (d.mem || {}).pct;
    if (m === "gpu") return d.gpu ? d.gpu.util : null;
    return (d.cpu || {}).pct;
  }

  function paintSpark(d) {
    const line = $("pc-spark-line"), area = $("pc-spark-area");
    if (!line) return;
    const key = METRICS[S.metric].key;
    const raw = ((d.hist || {})[key] || []).filter((v) => v != null && isFinite(v));
    const W = 260, H = 44, P = 4;
    if (raw.length < 2) {
      line.setAttribute("d", "");
      if (area) area.setAttribute("d", "");
      const mx0 = $("pc-max");
      if (mx0) mx0.textContent = "峰值 —";
      return;
    }
    // 纵轴留一点头：按 10 的整数取顶，曲线不至于一直贴着天花板
    const top = Math.max(20, Math.ceil(Math.max.apply(null, raw) / 10) * 10);
    const x = (i) => (i / (raw.length - 1)) * W;
    const y = (v) => H - P - (clamp(v, 0, 100) / top) * (H - P * 2);
    let dpath = "M" + x(0).toFixed(1) + "," + y(raw[0]).toFixed(1);
    for (let i = 1; i < raw.length; i++) {
      dpath += " L" + x(i).toFixed(1) + "," + y(raw[i]).toFixed(1);
    }
    line.setAttribute("d", dpath);
    if (area) area.setAttribute("d", dpath + " L" + W + "," + H + " L0," + H + " Z");
    const mx = $("pc-max");
    if (mx) {
      const t = "峰值 " + Math.round(Math.max.apply(null, raw)) + "%";
      if (mx.textContent !== t) mx.textContent = t;
    }
  }

  /* ---------- 每核占用 ---------- */
  function paintCores(cpu) {
    const box = $("pc-cores");
    if (!box) return;
    const per = cpu.per && cpu.per.length ? cpu.per : [];
    if (!per.length) { box.innerHTML = ""; return; }
    if (box.children.length !== per.length) {
      box.innerHTML = per.map(() => '<span class="pc-core"><i></i></span>').join("");
      box.title = per.length + " 个线程";
    }
    per.forEach((v, i) => {
      const el = box.children[i];
      if (!el) return;
      const bar = el.firstChild;
      // 写 transform 而不是 height：height 会触发布局，20~32 个条乘以
      // .4s 过渡的每一帧，就是每轮上百次重排。scaleY 走合成器，零布局。
      const k = clamp(v, 2, 100) / 100;
      const sv = k.toFixed(3);
      if (bar.__k !== sv) {
        bar.__k = sv;
        bar.style.transform = "scaleY(" + sv + ")";
      }
      const cls = "pc-core " + heatCls(v, 55, 85);
      if (el.className !== cls) el.className = cls;
    });
  }

  /* ---------- 温度 / 风扇 ---------- */
  const IC = {
    temp: '<svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 13V5a2 2 0 1 1 4 0v8a4 4 0 1 1-4 0z"/></svg>',
    fan: '<svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="2.2"/><path d="M12 9.8c0-3 1.2-5 3.4-5.4 1.6 2 1 4.3-1.2 5.6M14.2 12c3 0 5 1.2 5.4 3.4-2 1.6-4.3 1-5.6-1.2M9.8 12c0 3-1.2 5-3.4 5.4-1.6-2-1-4.3 1.2-5.6"/></svg>',
    chip: '<svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v2M14 3v2M10 19v2M14 19v2M3 10h2M3 14h2M19 10h2M19 14h2"/></svg>',
  };

  function tile(iconSvg, label, value, unit, cls, extra, tip) {
    return `<div class="pc-tile ${cls || ""}">
      <span class="k"${tip ? ` title="${tip}"` : ""}>${iconSvg}${label}</span>
      <span class="v ${value === "—" ? "miss" : ""}">${value}${
        unit && value !== "—" ? `<em>${unit}</em>` : ""}${extra || ""}</span>
    </div>`;
  }

  const vHtml = (value, unit) =>
    value + (unit && value !== "—" ? `<em>${unit}</em>` : "");

  /* 只改数值，不动结构。结构（形态）用签名判断，变了才重建。 */
  function setTile(row, value, unit, cls) {
    if (!row || row.children.length < 2) return;
    const html = vHtml(value, unit);
    const v = row.children[1];
    if (v.__h !== html) {
      v.__h = html;
      v.innerHTML = html;
    }
    const name = "v" + (value === "—" ? " miss" : "");
    if (v.className !== name) v.className = name;
    const c = "pc-tile " + (cls || "");
    if (row.className !== c) row.className = c;
  }

  function paintTiles(d) {
    const box = $("pc-tiles");
    if (!box) return;
    const cpu = d.cpu || {}, gpu = d.gpu, fans = d.fans || [];

    // ① CPU 温度：coretemp / lhm 是真核心温度；acpi 是主板 ACPI 温区
    //    （BrimDeck 同款方案：零依赖、免管理员，但台式机上常不随核心走，
    //    悬停说明里讲清楚，不冒充核心温度）
    const ct = cpu.temp;
    const src = cpu.temp_src || "";
    const hasTool = ((d.temp_tools || [])[0] || {}).name || "";
    const needCpu = ct == null;

    // ② 显卡温度
    const gt = gpu && gpu.temp != null ? gpu.temp : null;

    // ③ 风扇：主板风扇 RPM 优先（要有传感器程序），否则用显卡风扇百分比
    let fanVal = "—", fanUnit = "", fanLabel = "显卡风扇";
    const boardFan = fans.find((f) => /fan/i.test(f.name || "")) || fans[0];
    if (boardFan && boardFan.rpm != null) { fanVal = num(boardFan.rpm); fanUnit = "RPM"; fanLabel = "风扇"; }
    else if (gpu && gpu.fan != null) { fanVal = num(gpu.fan); fanUnit = "%"; }

    // 形态签名：只有"要不要显示接入按钮""风扇标签叫什么""温度来源换了"
    // 这类结构性差异才需要重建 DOM；温度从 62 变 63 只改一个文本节点。
    const sig = (needCpu ? "need" : src || "temp") + "|" + fanLabel + "|" +
                (needCpu && S.joining ? "j" : "") + "|" + (needCpu ? hasTool : "");
    if (box.__sig !== sig) {
      box.__sig = sig;
      // CPU 温度格回来了（2026-10-31）：BrimDeck 证明不装任何东西也能读 ——
      // Windows 性能计数器 \Thermal Zone Information(*)\Temperature 任何用户
      // 都能读，不需要管理员。代价是台式机上它多半是主板温区、不随核心走，
      // 所以来源是 acpi 时标签照旧但悬停说明弱化，不冒充核心温度。
      const cpuTile = needCpu ? "" : tile(IC.temp, "CPU 温度", num(ct), "°C",
        heatCls(ct, 70, 88), "",
        src === "acpi"
          ? "主板 ACPI 温区读数（零依赖、免管理员），台式机上未必随核心温度变化"
          : "CPU 封装温度");
      const gpuTile = gt == null
        ? tile(IC.temp, "显卡温度", "—", "", "")
        : tile(IC.temp, "显卡温度", num(gt), "°C", heatCls(gt, 72, 85));
      const fanTile = tile(IC.fan, fanLabel, fanVal, fanUnit, "");
      box.innerHTML = cpuTile + gpuTile + fanTile;
      const j = $("pc-join");
      if (j) j.onclick = join;
    }
    // 数值走局部更新（形态没变才有 row.children[1] 这一层）
    const rows = box.children;
    if (!needCpu) setTile(rows[0], num(ct), "°C", heatCls(ct, 70, 88));
    if (gt != null) setTile(rows[1], num(gt), "°C", heatCls(gt, 72, 85));
    else setTile(rows[1], "—", "", "");
    setTile(rows[2], fanVal, fanUnit, "");
  }

  /* ---------- 内存 / 磁盘 / 显存 ---------- */
  /* 行结构只建一次，之后只改数字和 transform。
     原来每 2 秒 innerHTML 重建 3 行 —— 销毁+新建+重新布局，
     而里面的数只变了几位。 */
  function makeBarRow() {
    const row = document.createElement("div");
    row.className = "pc-bar-row";
    row.innerHTML = '<span class="nm"></span>' +
                    '<span class="track"><i></i></span>' +
                    '<span class="val"></span>';
    return row;
  }

  function paintBars(d) {
    const box = $("pc-bars");
    if (!box) return;
    const rows = [];
    const mem = d.mem || {};
    if (mem.total) {
      rows.push({ label: "内存", pct: mem.pct, warn: 75, crit: 90,
        txt: `<b>${num(mem.pct, 1)}%</b> · ${bytes(mem.used)}/${bytes(mem.total)}` });
    }
    const parts = (d.disk && d.disk.parts) || [];
    // 系统盘排第一个（后端已经用 %SystemDrive% 标好了，不靠盘符猜）
    const sys = parts.find((p) => p.sys) || parts[0];
    if (sys) {
      rows.push({ label: sys.mnt.replace(":", "") + " 盘", pct: sys.pct, warn: 80, crit: 92,
        txt: `<b>${num(sys.pct, 0)}%</b> · 剩 ${bytes(sys.free)}` });
    }
    if (d.gpu && d.gpu.mem_total) {
      const gp = (d.gpu.mem_used / d.gpu.mem_total) * 100;
      rows.push({ label: "显存", pct: gp, warn: 80, crit: 92,
        txt: `<b>${num(gp, 0)}%</b> · ${num(d.gpu.mem_used / 1024, 1)}/${num(d.gpu.mem_total / 1024, 0)}G` });
    }
    while (box.children.length < rows.length) box.appendChild(makeBarRow());
    while (box.children.length > rows.length) box.removeChild(box.lastChild);
    rows.forEach((r, i) => {
      const row = box.children[i];
      const nm = row.children[0];
      const bar = row.children[1].firstChild;
      const val = row.children[2];
      if (nm.textContent !== r.label) nm.textContent = r.label;
      const sv = (clamp(r.pct || 0, 0, 100) / 100).toFixed(3);
      if (bar.__k !== sv) { bar.__k = sv; bar.style.transform = "scaleX(" + sv + ")"; }
      if (val.__h !== r.txt) { val.__h = r.txt; val.innerHTML = r.txt; }
      const cls = "pc-bar-row " + heatCls(r.pct, r.warn, r.crit);
      if (row.className !== cls) row.className = cls;
    });
  }

  /* ---------- 最吃 CPU 的进程 ---------- */
  function paintProcs(d) {
    const box = $("pc-procs");
    if (!box) return;
    const all = d.procs || {};
    const top = (all.top || []).filter((p) => p.cpu > 0.05).slice(0, 3);
    const rows = top.map((p) => `<div class="pc-proc" title="PID ${p.pid}${
      p.n > 1 ? "（" + p.n + " 个同名进程合计）" : ""}">
      <span class="nm">${String(p.name).replace(/\.exe$/i, "")}${
        p.n > 1 ? `<i class="pc-n">×${p.n}</i>` : ""}</span>
      <span class="c">${num(p.cpu, 1)}%</span>
      <span class="m">${num(p.mem, 1)}%</span>
    </div>`).join("");
    // 入口始终在：这是从"只看三个"走到"看全部"的唯一通道，
    // 不能因为当下 CPU 都是 0 就消失。
    const html = rows + `<button class="pc-all" id="pc-all" type="button">
      <span>全部${all.n ? " " + all.n + " 个" : ""}进程</span>
      <svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor"
           stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
        <path d="M9.5 6l6 6-6 6"/>
      </svg>
    </button>`;
    // 这段每次都是全量重建：没有变化就别碰 DOM。
    // 进程名的百分比大部分时候一模一样，跳过能省掉一次销毁+重建+布局。
    if (box.__h === html) return;
    box.__h = html;
    box.innerHTML = html;
  }

  /* ---------- 底部一行 ---------- */
  function paintFoot(d) {
    const box = $("pc-foot");
    if (!box) return;
    const cpu = d.cpu || {}, net = d.net || {}, dio = d.disk || {};
    const g1 = [];
    if (cpu.freq) g1.push(`<b>${(cpu.freq / 1000).toFixed(1)}</b> GHz`);
    if (d.procs && d.procs.n) g1.push(`<b>${d.procs.n}</b> 进程`);
    if (d.host && d.host.uptime) g1.push(`已开机 <b>${dur(d.host.uptime)}</b>`);
    const g2 = [];
    if (net.down != null) g2.push(`↓<b>${rate(net.down)}</b> ↑<b>${rate(net.up)}</b>`);
    const html =
      `<span class="pc-fl">${g1.join('<i class="pc-dot">·</i>')}</span>` +
      (g2.length ? `<span class="pc-fl">${g2.join('<i class="pc-dot">·</i>')}</span>` : "");
    if (box.__h === html) return;
    box.__h = html;
    box.innerHTML = html;
  }

  /* ---------- 主绘制 ---------- */
  /* 绘制分两步：paint() 只排一次队，paintNow() 才是真正写 DOM。
     同一次响应里被多处触发（load 回来、切换指标、resize）时，
     只会落一次，不会连着排队等布局。 */
  let rafId = 0;
  function paint() {
    if (rafId) return;
    rafId = requestAnimationFrame(() => {
      rafId = 0;
      paintNow();
    });
  }

  function paintNow() {
    const card = $("pc-card");
    if (!card) return;
    const d = S.data;
    const sub = $("pc-sub");
    if (!d) {
      if (sub && sub.textContent !== (S.err || "正在读取系统状态…")) {
        sub.textContent = S.err || "正在读取系统状态…";
      }
      return;
    }
    const cpu = d.cpu || {};
    const val = currentValue(d);
    const M = METRICS[S.metric];

    // 卡片的强调色跟着当前指标走（曲线、每核条、进度条都用它）。
    // --pc-c 被下面一大票元素 var() 引用，改一次就是一次整棵子树的样式
    // 重算 —— 只有真变了才写。
    const color = M.color || loadColor((cpu.pct));
    if (card.__c !== color) {
      card.__c = color;
      card.style.setProperty("--pc-c", color);
    }

    if (sub) {
      const c = shortCpu(cpu.name);
      const g = d.gpu && d.gpu.name ? d.gpu.name.replace(/^NVIDIA\s+/i, "").replace(/GeForce\s+/i, "") : "";
      const t = [c, g].filter(Boolean).join(" · ");
      if (sub.textContent !== t) sub.textContent = t;
      const tip = [(cpu.name || ""), (d.gpu ? d.gpu.name : "")].filter(Boolean).join("  ·  ");
      if (sub.title !== tip) sub.title = tip;
    }
    const elCap = $("pc-cap");
    if (elCap) {
      const sp = elCap.querySelector("span");
      if (sp && sp.textContent !== M.cap) sp.textContent = M.cap;
    }
    const elCpu = $("pc-cpu");
    if (elCpu) {
      const t = val == null ? "—" : String(Math.round(val));
      if (elCpu.textContent !== t) elCpu.textContent = t;
    }

    paintSpark(d);
    paintCores(cpu);
    paintTiles(d);
    paintBars(d);
    paintProcs(d);
    paintFoot(d);
  }

  /* ---------- 接入传感器（会弹一次 UAC） ---------- */
  function join() {
    if (S.joining) return;
    S.joining = true;
    const j = $("pc-join");
    if (j) { j.textContent = "接入中…"; j.className = "pc-join busy"; j.disabled = true; }
    const sub = $("pc-sub");
    if (sub) sub.textContent = "等待 UAC 授权…";
    fetch("/api/pc/sensor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then((r) => r.json())
      .then((j2) => {
        if (sub) sub.textContent = (j2 && j2.msg) || "";
        if (j2 && j2.ok) { S.at = 0; load(); }
      })
      .catch(() => { if (sub) sub.textContent = "启动失败"; })
      .finally(() => {
        S.joining = false;
        setTimeout(() => { if (S.data) paint(); }, 1200);
      });
  }

  /* ---------- 数据来源说明气泡 ---------- */
  let pop = null;
  function closePop() { if (pop) { pop.remove(); pop = null; } }

  function openPop(anchor) {
    closePop();
    pop = document.createElement("div");
    pop.className = "pc-pop";
    const tools = ((S.data || {}).temp_tools || []);
    const csrc = (((S.data || {}).cpu || {}).temp_src) || "";
    pop.innerHTML = `
      <h5>这些数字从哪来</h5>
      <p><b>CPU 占用 / 每核 / 频率 / 内存 / 磁盘 / 网络</b>：psutil 直接问 Windows，1 秒一次。</p>
      <p><b>显卡温度 / 风扇 / 功耗 / 显存</b>：nvidia-smi，2 秒一次。</p>
      <p><b>CPU 温度</b>：${
        csrc === "coretemp" || csrc === "lhm"
          ? `每核真实温度，来自 ${csrc === "coretemp" ? "Core Temp" : "LibreHardwareMonitor"}。`
          : csrc === "acpi"
            ? `主板 ACPI 温区（Windows 性能计数器，BrimDeck 同款）：零依赖、免管理员，但台式机上这个传感器多半不随核心温度走，仅供参考。`
            : `Windows 用户态读不到 CPU 的 DTS，必须由带内核驱动的程序代读。`
      }${tools.length
        ? `机器上有 <code>${tools[0].name}</code>，以管理员身份打开它就能看到每核真实温度。`
        : `装个 Core Temp / LibreHardwareMonitor 就能看到每核真实温度。`}</p>
      <p>点曲线上方的指标名可以切换 CPU / 内存 / 显卡。所有数据只在本机进程间传，不出网。</p>`;
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect();
    pop.style.left = Math.max(12, Math.min(window.innerWidth - 280, r.right - 268)) + "px";
    pop.style.top = (r.bottom + 8) + "px";
    setTimeout(() => document.addEventListener("click", outside, { once: true }), 0);
  }
  function outside(ev) { if (pop && !pop.contains(ev.target)) closePop(); }

  function cycle() {
    const keys = Object.keys(METRICS);
    S.metric = keys[(keys.indexOf(S.metric) + 1) % keys.length];
    try { localStorage.setItem("pc-metric", S.metric); } catch (e) { /* 隐私模式 */ }
    paint();
  }

  function wire() {
    const btn = $("pc-explain");
    if (btn) btn.onclick = (ev) => { ev.stopPropagation(); pop ? closePop() : openPop(btn); };
    const cap = $("pc-cap");
    if (cap) cap.onclick = cycle;
    // "全部进程"入口用事件委托：进程列表每 2 秒重建，直接绑会被冲掉
    const box = $("pc-procs");
    if (box) {
      box.addEventListener("click", (ev) => {
        if (ev.target.closest("#pc-all") && window.Procs) window.Procs.open();
      });
    }
    window.addEventListener("resize", closePop);
  }

  /* ---------- 启动 ---------- */
  function init() {
    try {
      const m = localStorage.getItem("pc-metric");
      if (m && METRICS[m]) S.metric = m;
    } catch (e) { /* ignore */ }
    wire();
    paint();
    load();
    clearInterval(S.timer);
    // 2 秒一档：既跟得上负载变化，又不会让浏览器一直占着 CPU。
    // 窗口被最小化/切到别的应用时 document.hidden 为 true，这时拉数据既
    // 没人看、又要白白重建一遍 DOM（每轮上千个节点），直接跳过；
    // 下面 visibilitychange 会在切回来时立刻补一次。
    S.timer = setInterval(() => { if (!document.hidden) load(); }, 2000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && Date.now() - S.at > 4000) load();
    });
  }

  window.PCStat = { init, paint, reload: load };

  document.addEventListener("DOMContentLoaded", init);
})();
