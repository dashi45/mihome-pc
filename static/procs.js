/* 正在运行 —— 像手机后台那样看程序
   ----------------------------------------------------------
   三条来自"手机怎么做的"的经验：
     · 按**应用**聚合，不按进程。浏览器开 24 个子进程是它的实现细节，
       用户想知道的是"Edge 一共吃了多少"，不是看 24 行 msedge.exe。
     · 点进去才看明细。聚合行给总量，展开给每个 pid，结束按钮在明细里。
     · 系统进程折叠起来。它们必须一直跑，列在面前只会让列表变长、
       让真正想关的东西沉到底下。

   数据每 3 秒拉一次，而且**只在这个面板打开时**拉 —— 面板关掉，
   后端连扫描都停（procman 里看 _USED 的时间戳）。
*/
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const S = {
    open: false,
    timer: null,
    busy: false,
    group: "all",          // all / app / bg / sys
    sort: "cpu",
    q: "",
    data: null,            // group=all 的结果（应用 + 后台）
    sysData: null,         // group=sys 的结果
    expanded: new Set(),   // 展开的聚合 key
    confirm: "",           // 正在确认结束的 key
    note: "",              // 底部临时提示
    noteAt: 0,
  };

  /* 图标底色：按名字取色，同一个程序每次都是同一个颜色。
     不用真图标（提取 exe 图标要读 PE 资源、转 PNG，代价不划算），
     首字母 + 稳定的颜色已经足够在列表里认出"那个紫色的"。 */
  const PALETTE = ["#6366F1", "#0EA5E9", "#10B981", "#F59E0B", "#EF4444",
                   "#8B5CF6", "#EC4899", "#14B8A6", "#F97316", "#3B82F6",
                   "#84CC16", "#06B6D4"];

  function hash(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h;
  }
  function colorOf(name) { return PALETTE[hash(name) % PALETTE.length]; }
  function letterOf(name) {
    const s = String(name).replace(/\.exe$/i, "").trim();
    return (s[0] || "?").toUpperCase();
  }
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  const IC_LOCK = `<svg viewBox="0 0 24 24" width="11" height="11" fill="none"
    stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
    <rect x="5" y="10.5" width="14" height="9" rx="2"/><path d="M8.2 10.5V7.8a3.8 3.8 0 0 1 7.6 0v2.7"/>
  </svg>`;

  function fmtMB(mb) {
    if (!mb) return "—";
    if (mb >= 1024) return (mb / 1024).toFixed(1) + " G";
    return Math.round(mb) + " M";
  }
  function fmtIO(b) {
    if (!b) return "";
    if (b >= 1048576) return (b / 1048576).toFixed(1) + "M/s";
    if (b >= 1024) return Math.round(b / 1024) + "K/s";
    return b + "B/s";
  }
  function heat(cpu) {
    if (cpu >= 25) return " hot";
    if (cpu >= 5) return " warm";
    return "";
  }

  /* ---------------- 聚合：把同名进程合成一个"应用" ---------------- */
  function regroup(items) {
    const m = new Map();
    for (const it of items) {
      const k = String(it.name || "?").toLowerCase();
      let g = m.get(k);
      if (!g) {
        g = { key: k, name: it.name, n: 0, cpu: 0, mem: 0, app: 0, bg: 0,
              title: "", guards: new Set(), kids: [], me: false };
        m.set(k, g);
      }
      g.n++;
      g.cpu += it.cpu || 0;
      g.mem += it.mem || 0;
      if (it.kind === "app") { g.app++; if (!g.title && it.title) g.title = it.title; }
      else g.bg++;
      if (it.guard) g.guards.add(it.guard);
      if (it.me) g.me = true;
      g.kids.push(it);
    }
    const out = [...m.values()];
    for (const g of out) {
      g.cpu = Math.round(g.cpu * 10) / 10;
      g.kind = g.app ? "app" : "bg";
      // 只要有子进程可杀，就能从聚合行发起结束（受保护的会被跳过）
      g.locked = g.kids.every((x) => x.guard || x.me);
      g.why = g.kids.find((x) => x.guard) ? g.kids.find((x) => x.guard).guard : "";
      g.kids.sort((a, b) => (b.cpu - a.cpu) || (b.mem - a.mem));
    }
    return out;
  }

  function sortList(list) {
    const s = S.sort;
    list.sort((a, b) => {
      if (s === "mem") return (b.mem - a.mem) || (b.cpu - a.cpu);
      if (s === "name") return a.name.localeCompare(b.name, "zh");
      return (b.cpu - a.cpu) || (b.mem - a.mem);
    });
    return list;
  }

  function current() {
    const src = S.group === "sys" ? S.sysData : S.data;
    if (!src) return null;
    let items = src.items || [];
    if (S.q) {
      const q = S.q.toLowerCase();
      items = items.filter((x) =>
        (x.name || "").toLowerCase().includes(q) ||
        (x.title || "").toLowerCase().includes(q));
    }
    if (S.group === "app") items = items.filter((x) => x.kind === "app");
    if (S.group === "bg") items = items.filter((x) => x.kind === "bg");
    return sortList(regroup(items));
  }

  /* ---------------- 行 ---------------- */
  function rowHTML(g) {
    const open = S.expanded.has(g.key);
    const conf = S.confirm === g.key;

    if (conf) {
      return `<div class="prx-row is-confirm" data-key="${esc(g.key)}">
        <span class="prx-ic" style="--c:${colorOf(g.name)}">${esc(letterOf(g.name))}</span>
        <div class="prx-main">
          <div class="prx-name">结束 ${esc(g.name.replace(/\.exe$/i, ""))}？</div>
          <div class="prx-sub">${g.n > 1 ? `${g.n} 个进程，` : ""}释放约 ${fmtMB(g.mem)} 内存${
            g.app ? "，窗口里没保存的内容会丢" : ""}</div>
        </div>
        <div class="prx-act">
          <button class="prx-ok" data-kill="${esc(g.key)}" type="button">结束</button>
          <button class="prx-no" data-cancel="1" type="button">取消</button>
        </div>
      </div>`;
    }

    const sub = g.title
      ? `<span class="prx-t" title="${esc(g.title)}">${esc(g.title)}</span>`
      : (g.n > 1 ? `${g.n} 个进程` : (g.kind === "app" ? "前台" : "后台"));
    const kids = open ? `<div class="prx-kids">${g.kids.map(kidHTML).join("")}</div>` : "";
    const lockWhy = g.locked ? esc(g.why || "系统进程，不能结束") : "";
    const btn = g.locked
      ? `<span class="prx-lock" title="${lockWhy}">${IC_LOCK}</span>`
      : `<button class="prx-x" data-conf="${esc(g.key)}" type="button" title="结束${
          g.n > 1 ? "这 " + g.n + " 个进程" : ""}">结束</button>`;

    return `<div class="prx-row${open ? " is-open" : ""}" data-key="${esc(g.key)}">
      <span class="prx-ic" style="--c:${colorOf(g.name)}">${esc(letterOf(g.name))}</span>
      <div class="prx-main">
        <div class="prx-name">${esc(g.name.replace(/\.exe$/i, ""))}${
          g.n > 1 ? `<em class="prx-n">×${g.n}</em>` : ""}${
          g.me ? `<em class="prx-me">本页</em>` : ""}</div>
        <div class="prx-sub">${sub}</div>
      </div>
      <div class="prx-stats">
        <span class="prx-cpu${heat(g.cpu)}">${g.cpu >= 0.05 ? g.cpu.toFixed(1) + "%" : "—"}</span>
        <span class="prx-mem">${fmtMB(g.mem)}</span>
      </div>
      ${btn}
      <i class="prx-meter" style="width:${Math.min(100, g.cpu)}%"></i>
    </div>${kids}`;
  }

  function kidHTML(k) {
    const io = fmtIO((k.io_r || 0) + (k.io_w || 0));
    const lock = k.guard || k.me;
    return `<div class="prx-kid">
      <span class="prx-kid-pid">${k.pid}</span>
      <span class="prx-kid-t">${k.title ? esc(k.title) : (k.kind === "app" ? "前台窗口" : "后台")}</span>
      <span class="prx-kid-cpu${heat(k.cpu)}">${k.cpu >= 0.05 ? k.cpu.toFixed(1) + "%" : "—"}</span>
      <span class="prx-kid-mem">${fmtMB(k.mem)}</span>
      <span class="prx-kid-io">${io}</span>
      ${lock
        ? `<span class="prx-kid-lock" title="${esc(k.guard || "本页面自己的进程")}">${IC_LOCK}</span>`
        : `<button class="prx-kid-x" data-pid="${k.pid}" type="button">结束</button>`}
    </div>`;
  }

  /* ---------------- 绘制 ---------------- */
  function paint() {
    const box = $("prx-list");
    if (!box) return;
    const d = S.data;
    const sum = (d && d.sum) || {};
    const sys = (S.sysData && S.sysData.sum) || null;

    // 顶部指标
    const cnt = $("prx-count");
    if (cnt) cnt.textContent = sum.total ? `${sum.total} 个进程` : "";
    const sub = $("prx-sub");
    if (sub) {
      const used = (sum.mem || 0) / 1024;
      sub.innerHTML = sum.total
        ? `CPU <b>${(sum.cpu || 0).toFixed(1)}%</b> · 内存 <b>${used.toFixed(1)} G</b>` +
          ` · 系统进程 <b>${sum.sys || 0}</b> 个已折叠`
        : "正在扫描…";
    }
    const ft = $("prx-ft");
    if (ft) {
      const fresh = S.note && Date.now() - S.noteAt < 6000;
      ft.className = "prx-ft" + (fresh ? " is-note" : "");
      ft.innerHTML = fresh
        ? esc(S.note)
        : `只列你自己的程序；系统关键进程已折叠并锁定。` +
          `<span class="prx-cost">扫描 ${(d && d.cost) || "?"} ms</span>`;
    }

    // 分组标签上的数字
    const tabs = $("prx-tabs");
    if (tabs) {
      const map = { all: (sum.app || 0) + (sum.bg || 0), app: sum.app, bg: sum.bg, sys: sum.sys };
      tabs.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("on", b.dataset.g === S.group);
        const i = b.querySelector("i");
        if (i) i.textContent = map[b.dataset.g] == null ? "" : map[b.dataset.g];
      });
    }

    const list = current();
    if (!list) { box.innerHTML = `<div class="prx-empty">正在扫描进程…</div>`; return; }
    if (!list.length) {
      box.innerHTML = `<div class="prx-empty">${S.q ? "没有匹配的程序" : "这里什么都没有"}</div>`;
      return;
    }
    box.innerHTML = list.map(rowHTML).join("");
  }

  /* ---------------- 取数 ---------------- */
  function pull(alsoSys) {
    if (S.busy) return;
    S.busy = true;
    const jobs = [
      fetch("/api/pc/procs?group=all&sort=cpu", { cache: "no-store" })
        .then((r) => r.json())
        .then((j) => { if (j && j.items) S.data = j; })
        .catch(() => {}),
    ];
    if (alsoSys || S.group === "sys") {
      jobs.push(fetch("/api/pc/procs?group=sys&sort=cpu", { cache: "no-store" })
        .then((r) => r.json())
        .then((j) => { if (j && j.items) S.sysData = j; })
        .catch(() => {}));
    }
    Promise.all(jobs).then(() => {
      S.busy = false;
      paint();
    });
  }

  function loop() {
    clearInterval(S.timer);
    S.timer = setInterval(() => { if (S.open) pull(false); }, 3000);
  }

  /* ---------------- 结束 ---------------- */
  function kill(group) {
    const targets = S.group === "sys"
      ? group.kids.filter((k) => !k.guard && !k.me)
      : group.kids.filter((k) => !k.guard && !k.me);
    if (!targets.length) { S.note = "这些进程受保护，不能结束"; S.noteAt = Date.now(); paint(); return; }

    const one = (pid, force) => fetch("/api/pc/procs/kill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pid: pid, force: !!force }),
    }).then((r) => r.json()).catch((e) => ({ ok: false, msg: String(e) }));

    // 逐个来：一次并发几十个请求既没必要，也会让失败原因分不清
    let i = 0, okn = 0, push = 0, fail = "";
    const step = () => {
      if (i >= targets.length) {
        S.note = `已结束 ${okn} 个` +
          (push ? `，${push} 个需要强制` : "") +
          (fail ? `，${fail}` : "");
        S.noteAt = Date.now();
        S.confirm = "";
        S.expanded.delete(group.key);
        pull(false);
        return;
      }
      const t = targets[i++];
      one(t.pid, false).then((r) => {
        if (r && r.ok) okn++;
        else if (r && r.need_force) push++;
        else if (r && !r.ok && !fail) fail = r.msg || "有进程结束失败";
        step();
      });
    };
    step();
  }

  /* ---------------- 事件接线 ---------------- */
  function bind() {
    const mask = $("prx-mask");
    if (!mask) return;

    mask.addEventListener("click", (ev) => {
      if (ev.target === mask) close();
    });
    $("prx-close").addEventListener("click", close);

    const q = $("prx-q");
    if (q) {
      q.addEventListener("input", () => { S.q = q.value.trim(); paint(); });
    }

    const sort = $("prx-sort");
    if (sort) {
      sort.addEventListener("click", (ev) => {
        const b = ev.target.closest("button[data-sort]");
        if (!b) return;
        S.sort = b.dataset.sort;
        sort.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
        paint();
      });
    }

    const tabs = $("prx-tabs");
    if (tabs) {
      tabs.addEventListener("click", (ev) => {
        const b = ev.target.closest("button[data-g]");
        if (!b) return;
        S.group = b.dataset.g;
        S.confirm = "";
        if (S.group === "sys" && !S.sysData) pull(true);
        else paint();
      });
    }

    const list = $("prx-list");
    list.addEventListener("click", (ev) => {
      const conf = ev.target.closest("[data-conf]");
      if (conf) {
        S.confirm = conf.dataset.conf;
        paint();
        return;
      }
      const cancel = ev.target.closest("[data-cancel]");
      if (cancel) { S.confirm = ""; paint(); return; }

      const ok = ev.target.closest("[data-kill]");
      if (ok) {
        const g = (current() || []).find((x) => x.key === ok.dataset.kill);
        if (g) kill(g);
        return;
      }

      const kid = ev.target.closest("[data-pid]");
      if (kid) {
        ev.stopPropagation();
        const pid = +kid.dataset.pid;
        kid.disabled = true;
        kid.textContent = "…";
        fetch("/api/pc/procs/kill", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pid: pid }),
        }).then((r) => r.json()).then((j) => {
          S.note = (j && j.msg) || "已发送结束请求";
          S.noteAt = Date.now();
          pull(false);
        }).catch(() => { kid.disabled = false; kid.textContent = "结束"; });
        return;
      }

      // 点行主体 = 展开/收起
      const row = ev.target.closest(".prx-row");
      if (row && row.dataset.key && !ev.target.closest("button")) {
        const k = row.dataset.key;
        if (S.expanded.has(k)) S.expanded.delete(k);
        else S.expanded.add(k);
        paint();
      }
    });

    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && S.open) close();
    });
  }

  function open() {
    S.open = true;
    const mask = $("prx-mask");
    if (!mask) return;
    mask.classList.remove("hidden");
    document.body.classList.add("prx-lock");
    paint();
    pull(false);
    loop();
  }

  function close() {
    S.open = false;
    S.confirm = "";
    clearInterval(S.timer);
    S.timer = null;
    const mask = $("prx-mask");
    if (mask) mask.classList.add("hidden");
    document.body.classList.remove("prx-lock");
  }

  function init() {
    bind();
    window.Procs = { open: open, close: close };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
