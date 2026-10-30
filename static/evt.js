/* ==========================================================================
   动态 —— 这个面板唯一"记得过去"的地方
   --------------------------------------------------------------------------
   别的卡片答的问题是"现在多少"，这张卡答的是"发生过什么"。
   数据来自 /api/evt（后端 evtlog.py 落盘在 evt.db）。

   几条自我约束：
     · 10 秒增量拉一次，只拿 id > cursor 的，不重拉整表；
     · 页面隐藏（切走标签页）就停，回来立刻补一次，别在后台空跑；
     · 只在真的追加了新条目时才播入场动画，重绘时不重播（否则整块在抖）。
   ========================================================================== */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const MAX_ROW = 8;          // 卡上直接露出的条数，多了进"全部"
  const POLL = 10000;

  const S = {
    list: [],        // 最新在前
    cursor: 0,
    total: null,
    timer: null,
    busy: false,
    fresh: new Set(), // 本次新到、需要播动画的 id
    panel: false,
    panelList: [],
    panelBefore: 0,
    panelKind: "",
    panelEnd: false,
  };

  /* ---------------- 图标 ---------------- */
  const P = {
    power: '<path d="M13 3L5.5 13.2h4.4L9.6 21 17 10.8h-4.4z"/>',
    energy: '<path d="M3 15.5l4-5 3.5 3 4-6.5 3.5 4.2"/><path d="M3 19.5h15"/>',
    temp: '<path d="M10 3.5a1.6 1.6 0 0 1 3.2 0v8.1a4.2 4.2 0 1 1-3.2 0z"/><path d="M11.6 7v6.4"/>',
    hum: '<path d="M11.6 3.4s5 6.2 5 9.2a5 5 0 0 1-10 0c0-3 5-9.2 5-9.2z"/>',
    batt: '<rect x="2.5" y="7" width="15" height="8" rx="2"/><path d="M20 10.4v2.2"/><path d="M5.2 9.6h4.4v3.2H5.2z"/>',
    mode: '<path d="M3 6.5h13M3 11.5h9M3 16.5h12"/>',
    pc: '<rect x="5.5" y="5.5" width="11" height="11" rx="2"/><path d="M8.5 2.8v2.7M13.5 2.8v2.7M8.5 16.5v2.7M13.5 16.5v2.7M2.8 8.5h2.7M2.8 13.5h2.7M16.5 8.5h2.7M16.5 13.5h2.7"/>',
    wx: '<path d="M6.8 16.5h9.4a3.2 3.2 0 0 0 .3-6.4 4.6 4.6 0 0 0-8.9-1.2A3.8 3.8 0 0 0 6.8 16.5z"/>',
    online: '<path d="M4 15.5a9 9 0 0 1 14 0"/><path d="M6.7 18a5.6 5.6 0 0 1 8.6 0"/><circle cx="11" cy="20.3" r="1.1"/>',
    offline: '<path d="M4 15.5a9 9 0 0 1 5-2.6"/><path d="M15.5 14.5a9 9 0 0 0-1-1"/><path d="M3.4 3.4l15.2 15.2"/>',
    cam: '<rect x="2.8" y="7" width="12" height="9" rx="2.4"/><path d="M14.8 10.6l3.4-2v6l-3.4-2z"/>',
    sys: '<circle cx="11" cy="11" r="3"/><path d="M11 3.2v2.4M11 16.4v2.4M3.2 11h2.4M16.4 11h2.4M5.6 5.6l1.7 1.7M14.7 14.7l1.7 1.7M16.4 5.6l-1.7 1.7M7.3 14.7l-1.7 1.7"/>',
  };
  function icon(kind, size) {
    const p = P[kind] || P.sys;
    return `<svg viewBox="0 0 22 22" width="${size || 15}" height="${size || 15}" fill="none"
      stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
      stroke-linejoin="round">${p}</svg>`;
  }
  const KIND_NAME = {
    power: "开关", energy: "用电", temp: "温度", hum: "湿度", batt: "电量",
    mode: "模式", pc: "本机", wx: "天气", online: "上线", offline: "离线",
    cam: "摄像头", sys: "系统",
  };

  /* ---------------- 时间 ---------------- */
  const pad = (n) => String(n).padStart(2, "0");
  function hm(ts) {
    const d = new Date(ts * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function ago(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 45) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400 && sameDay(ts, Date.now() / 1000)) return Math.floor(s / 3600) + " 小时前";
    if (s < 172800 && yest(ts)) return "昨天 " + hm(ts);
    const d = new Date(ts * 1000);
    return (d.getMonth() + 1) + "月" + d.getDate() + "日 " + hm(ts);
  }
  function sameDay(a, b) {
    const x = new Date(a * 1000), y = new Date(b * 1000);
    return x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth()
      && x.getDate() === y.getDate();
  }
  function yest(ts) {
    const d = new Date(ts * 1000);
    const n = new Date();
    const t = new Date(n.getFullYear(), n.getMonth(), n.getDate() - 1);
    return d.getFullYear() === t.getFullYear() && d.getMonth() === t.getMonth()
      && d.getDate() === t.getDate();
  }
  /* 卡上的时间列只有 56px，写不下"昨天 06:18"。
     按"多久之前"分三档：今天给时刻、昨天给"昨 HH:MM"、更早给日期。
     具体到分钟的时间点看"全部"弹层，那里按天分组、宽度不受限。 */
  function when(ts) {
    const now = Date.now() / 1000;
    if (sameDay(ts, now)) return hm(ts);
    if (yest(ts)) return "昨 " + hm(ts);
    const d = new Date(ts * 1000);
    return (d.getMonth() + 1) + "/" + d.getDate();
  }

  /* ---------------- 主卡渲染 ---------------- */
  function rowHTML(e) {
    const anim = S.fresh.has(e.id) ? " is-new" : "";
    return `<div class="evt${anim}" data-id="${e.id}" title="${esc(e.title)}${e.detail ? " · " + esc(e.detail) : ""}">
      <i class="evt-ic lv-${e.level}">${icon(e.kind)}</i>
      <span class="evt-t">${when(e.ts)}</span>
      <span class="evt-tx">${esc(e.title)}</span>
      ${e.detail ? `<span class="evt-d">${esc(e.detail)}</span>` : ""}
    </div>`;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function paintMini() {
    const el = $("evt-mini");
    if (!el) return;
    const e = S.list[0];
    const txt = $("evt-mini-text"), tm = $("evt-mini-time");
    if (txt) txt.textContent = e ? e.title : "暂无动态";
    if (tm) tm.textContent = e ? hm(e.ts) : "";
    el.classList.toggle("warn", !!(e && (e.level === "warn" || e.level === "bad")));
    el.title = e
      ? `${when(e.ts)} ${e.title}${e.detail ? " · " + e.detail : ""}`
      : "查看全部动态";
  }

  function paint() {
    const box = $("evt-list");
    const sub = $("evt-sub");
    // 动态卡已经收进顶栏：没有列表节点时只更新那一条 ticker
    if (!box) { paintMini(); return; }
    if (!S.list.length) {
      box.innerHTML = `<div class="evt-empty">
        <i>${icon("sys", 16)}</i>
        <span>还没有记录。开关灯、空调启停、设备上下线、CPU 过热、下雨、空气质量变化，
        都会自动出现在这里。</span></div>`;
      if (sub) sub.textContent = "0 条记录";
      paintMini();
      return;
    }
    // 矮屏只露 4 条（2 行 × 2 列），给下面的设备卡腾出首屏空间；
    // 窗口够高就还是 8 条。只影响"卡上露几条"，"全部"里一条不少。
    const maxRows = (window.innerHeight && window.innerHeight <= 1000) ? 4 : MAX_ROW;
    const show = S.list.slice(0, maxRows);
    box.innerHTML = show.map((e) => rowHTML(e)).join("");
    if (sub) {
      const now = Date.now() / 1000;
      // 两个数都只算"今天"：卡上混着昨天的条目，如果只按今天算事件数、
      // 却按全天算告警数，会出现"今天 2 件 · 4 件需留意"这种自相矛盾的副标题。
      const today = S.list.filter((e) => sameDay(e.ts, now));
      const warn = today.filter((e) => e.level === "warn" || e.level === "bad").length;
      const total = S.total != null ? S.total : S.list.length;
      sub.textContent = `今天 ${today.length} 件`
        + (warn ? ` · ${warn} 件需留意` : "")
        + (total > today.length ? `｜共记 ${total} 条` : "");
    }
    S.fresh.clear();
    paintMini();
  }

  /* ---------------- 拉数据 ---------------- */
  function fetchNew(silent) {
    if (S.busy) return;
    S.busy = true;
    const q = S.cursor ? `/api/evt?since=${S.cursor}&limit=80` : "/api/evt?limit=60";
    fetch(q, { cache: "no-store" })
      .then((r) => r.json())
      .then((j) => {
        if (!j || !j.ok) return;
        if (typeof j.total === "number") S.total = j.total;
        const got = j.events || [];
        if (!got.length) return;
        // 后端一律按"新的在前"返回，首屏和增量都是同一种顺序，
        // 这里只做去重 + 往前插，不能 reverse（reverse 过就会把最新的切到末尾）
        const have = new Set(S.list.map((e) => e.id));
        const add = got.filter((e) => !have.has(e.id));
        if (!add.length) { S.cursor = Math.max(S.cursor, j.cursor || 0); return; }
        add.forEach((e) => S.fresh.add(e.id));
        S.list = add.concat(S.list).slice(0, 120);
        S.cursor = Math.max(S.cursor, j.cursor || 0);
        paint();
        if (!silent && S.panel) refreshPanel(true);
      })
      .catch(() => { /* 网络抖动不打扰用户，下个周期再说 */ })
      .finally(() => { S.busy = false; });
  }

  /* ---------------- 全部（弹层） ---------------- */
  function ensurePanel() {
    let el = $("evt-panel");
    if (el) return el;
    el = document.createElement("div");
    el.id = "evt-panel";
    el.className = "evt-mask";
    el.innerHTML = `<div class="evt-sheet" role="dialog" aria-label="全部动态">
      <div class="evt-sheet-hd">
        <h4>全部动态</h4>
        <div class="evt-tabs" id="evt-tabs"></div>
        <button class="evt-x" id="evt-x" type="button" aria-label="关闭">
          <svg viewBox="0 0 22 22" width="15" height="15" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round"><path d="M5 5l12 12M17 5L5 17"/></svg>
        </button>
      </div>
      <div class="evt-sheet-bd" id="evt-sheet-bd"></div>
      <div class="evt-sheet-ft" id="evt-sheet-ft"></div>
    </div>`;
    document.body.appendChild(el);

    el.addEventListener("click", (ev) => {
      if (ev.target === el) closePanel();
    });
    el.querySelector("#evt-x").addEventListener("click", closePanel);
    // 滚到底自动翻更早的
    el.querySelector("#evt-sheet-bd").addEventListener("scroll", function () {
      if (S.panelEnd || S.busy) return;
      if (this.scrollTop + this.clientHeight > this.scrollHeight - 60) loadPanel();
    });
    return el;
  }

  const TABS = [
    ["", "全部"], ["power", "开关"], ["energy", "用电"], ["temp", "温度"],
    ["pc", "本机"], ["wx", "天气"], ["offline", "离线"],
  ];

  function paintTabs() {
    const box = $("evt-tabs");
    if (!box) return;
    box.innerHTML = TABS.map(([k, n]) =>
      `<button type="button" class="evt-tab${S.panelKind === k ? " on" : ""}" data-k="${k}">${n}</button>`
    ).join("");
    box.querySelectorAll(".evt-tab").forEach((b) => {
      b.addEventListener("click", () => {
        S.panelKind = b.dataset.k;
        S.panelBefore = 0;
        S.panelEnd = false;
        S.panelList = [];
        paintTabs();
        refreshPanel(false);
      });
    });
  }

  function refreshPanel(incremental) {
    if (!S.panel) return;
    if (!incremental) {
      S.panelList = S.list.filter((e) => !S.panelKind || e.kind === S.panelKind);
      // 首屏拿回来的条数就少于库里总数，说明后面还有更早的；
      // 否则（比如库里一共 17 条、一次全给了）就不该继续提示"向下滚动"。
      S.panelEnd = !(S.total > S.panelList.length);
      paintSheet();
      return;
    }
    // 有新事件时，如果当前筛选能容纳，就补进弹层顶部
    const add = S.list.filter((e) =>
      (!S.panelKind || e.kind === S.panelKind)
      && !S.panelList.some((x) => x.id === e.id));
    if (add.length) {
      S.panelList = add.concat(S.panelList);
      paintSheet();
    }
  }

  function loadPanel() {
    if (S.busy || S.panelEnd) return;
    const last = S.panelList[S.panelList.length - 1];
    const before = last ? last.id : S.panelBefore;
    if (!before) { S.panelEnd = true; paintSheet(); return; }
    S.busy = true;
    const kind = S.panelKind ? `&kind=${S.panelKind}` : "";
    fetch(`/api/evt?before=${before}&limit=80${kind}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((j) => {
        if (!j || !j.ok) return;
        const got = j.events || [];
        if (!got.length) { S.panelEnd = true; return; }
        const have = new Set(S.panelList.map((e) => e.id));
        S.panelList = S.panelList.concat(got.filter((e) => !have.has(e.id)));
        if (!j.more) S.panelEnd = true;
      })
      .catch(() => {})
      .finally(() => { S.busy = false; paintSheet(); });
  }

  function paintSheet() {
    const bd = $("evt-sheet-bd");
    const ft = $("evt-sheet-ft");
    if (!bd) return;
    if (!S.panelList.length) {
      bd.innerHTML = `<div class="evt-empty pg"><i>${icon("sys", 18)}</i>
        <span>这段时间还没有记录</span></div>`;
      if (ft) ft.textContent = "";
      return;
    }
    // 按天分组
    let html = "";
    let day = "";
    for (const e of S.panelList) {
      const d = e.day || "";
      if (d !== day) {
        day = d;
        html += `<div class="evt-day">${dayLabel(d)}</div>`;
      }
      html += `<div class="evt evt-lg" data-id="${e.id}">
        <i class="evt-ic lv-${e.level}">${icon(e.kind, 16)}</i>
        <span class="evt-t">${hm(e.ts)}</span>
        <span class="evt-tx">${esc(e.title)}</span>
        ${e.detail ? `<span class="evt-d">${esc(e.detail)}</span>` : ""}
        <span class="evt-k">${KIND_NAME[e.kind] || e.kind}</span>
      </div>`;
    }
    bd.innerHTML = html;
    if (ft) {
      ft.textContent = S.panelEnd
        ? `—— 到底了，共 ${S.panelList.length} 条 ——`
        : "向下滚动加载更早";
    }
  }

  function dayLabel(d) {
    if (!d) return "";
    const t = new Date(d + "T00:00:00");
    const n = new Date();
    const nd = new Date(n.getFullYear(), n.getMonth(), n.getDate());
    const diff = Math.round((nd - t) / 86400000);
    if (diff === 0) return "今天";
    if (diff === 1) return "昨天";
    if (diff === 2) return "前天";
    return `${t.getMonth() + 1} 月 ${t.getDate()} 日 · 周${"日一二三四五六"[t.getDay()]}`;
  }

  function openPanel() {
    S.panel = true;
    ensurePanel().classList.add("on");
    document.body.style.overflow = "hidden";
    paintTabs();
    S.panelList = [];
    S.panelEnd = false;
    refreshPanel(false);
    if (!S.panelList.length) loadPanel();
  }
  function closePanel() {
    S.panel = false;
    const el = $("evt-panel");
    if (el) el.classList.remove("on");
    document.body.style.overflow = "";
  }

  /* ---------------- 启动 ---------------- */
  function tick() {
    if (document.hidden) return;
    fetchNew(true);
    // 相对时间会自己变旧（"3 分钟前"过一会儿就不对了），重画一遍
    paint();
  }

  function init() {
    if (!$("evt-list") && !$("evt-mini")) return;
    paint();
    fetchNew(true);
    S.timer = setInterval(tick, POLL);

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) fetchNew(true);
    });
    window.addEventListener("focus", () => fetchNew(true));

    const mini = $("evt-mini");
    if (mini) mini.addEventListener("click", openPanel);
    const more = $("evt-more");
    if (more) more.addEventListener("click", openPanel);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && S.panel) closePanel();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
