/* ==========================================================
   左栏卡片拖动排序
   ----------------------------------------------------------
   只负责 #dash 里的 .dcard（功率 / 本机 / 环境 / 能源），
   设备网格不参与。拖动从卡片右上角的六点手柄开始，避免和
   卡片里的滑块、按钮、图表抢鼠标。

   顺序存在 localStorage（每台机器自己的习惯），用行内 order
   覆盖宽屏媒体查询里的默认顺序；窗口缩到 <1200 时清掉行内
   order，回到原来的栅格布局。Alt+↑/↓ 也能移动当前卡片。
   ========================================================== */
(function () {
  "use strict";

  const KEY = "mi-dash-order-v1";
  const DEFAULT_ORDER = ["power-card", "pc-card", "env-section", "energy-section"];
  const MQ = window.matchMedia("(min-width:1200px)");
  const dash = document.getElementById("dash");
  if (!dash) return;

  const cards = () => Array.from(dash.children).filter(
    (el) => el.classList && el.classList.contains("dcard") && el.id);
  let dragged = null;

  function readOrder() {
    try {
      const v = JSON.parse(localStorage.getItem(KEY) || "null");
      return Array.isArray(v) ? v : null;
    } catch (e) { return null; }
  }
  function saveOrder(ids) {
    try { localStorage.setItem(KEY, JSON.stringify(ids)); } catch (e) {}
  }

  function markCustom(on) {
    const btn = document.getElementById("dash-reset");
    if (btn) btn.classList.toggle("on", !!on);
  }

  function applySaved() {
    const list = cards();
    if (!MQ.matches) {
      list.forEach((el) => { el.style.order = ""; });
      return;
    }
    const saved = readOrder();
    const ids = list.map((el) => el.id);
    const base = (saved && saved.length) ? saved : DEFAULT_ORDER;
    const order = base.filter((id) => ids.indexOf(id) >= 0);
    ids.forEach((id) => { if (order.indexOf(id) < 0) order.push(id); });
    // 物理重排 DOM，让拖动/键盘的起点和屏幕上的顺序一致；
    // 再写行内 order，覆盖宽屏媒体查询里的默认顺序。
    order.forEach((id) => {
      const el = document.getElementById(id);
      if (el) dash.appendChild(el);
    });
    order.forEach((id, i) => {
      const el = document.getElementById(id);
      if (el) el.style.order = String(i + 1);
    });
    markCustom(!!(saved && saved.length));
  }

  function syncFromDom() {
    const list = cards();
    list.forEach((el, i) => { el.style.order = String(i + 1); });
    saveOrder(list.map((el) => el.id));
    markCustom(true);
  }

  function clearMarks() {
    cards().forEach((el) => el.classList.remove("drop-before", "drop-after"));
  }

  function reset() {
    try { localStorage.removeItem(KEY); } catch (e) {}
    DEFAULT_ORDER.forEach((id) => {
      const el = document.getElementById(id);
      if (el) dash.appendChild(el);
    });
    cards().forEach((el) => { el.style.order = ""; });
    markCustom(false);
    if (typeof toast === "function") toast("已恢复默认卡片顺序");
  }

  function moveByKeyboard(card, dir) {
    const list = cards();
    const i = list.indexOf(card);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= list.length) return;
    if (dir < 0) dash.insertBefore(card, list[j]);
    else dash.insertBefore(card, list[j].nextSibling);
    syncFromDom();
    card.classList.add("dragging");
    setTimeout(() => card.classList.remove("dragging"), 180);
    const handle = card.querySelector(".card-drag");
    if (handle) handle.focus();
  }

  function addHandle(card) {
    if (card.querySelector(".card-drag")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "card-drag";
    btn.title = "拖动排序（Alt+↑/↓ 也可以）";
    btn.setAttribute("aria-label", "拖动排序");
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="9" cy="6" r="1.7"/><circle cx="15" cy="6" r="1.7"/>
      <circle cx="9" cy="12" r="1.7"/><circle cx="15" cy="12" r="1.7"/>
      <circle cx="9" cy="18" r="1.7"/><circle cx="15" cy="18" r="1.7"/>
    </svg>`;

    const head = card.querySelector(".dc-head");
    if (head) head.appendChild(btn);
    else card.appendChild(btn);

    btn.addEventListener("pointerdown", () => { card.draggable = true; });
    btn.addEventListener("keydown", (ev) => {
      if (!ev.altKey) return;
      if (ev.key === "ArrowUp") { ev.preventDefault(); moveByKeyboard(card, -1); }
      else if (ev.key === "ArrowDown") { ev.preventDefault(); moveByKeyboard(card, 1); }
    });
  }

  dash.addEventListener("dragstart", (ev) => {
    const card = ev.target.closest && ev.target.closest(".dcard");
    if (!card || !card.draggable) { ev.preventDefault(); return; }
    dragged = card;
    card.classList.add("dragging");
    try {
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", card.id);
    } catch (e) {}
  });

  dash.addEventListener("dragover", (ev) => {
    if (!dragged) return;
    ev.preventDefault();
    try { ev.dataTransfer.dropEffect = "move"; } catch (e) {}
    const target = ev.target.closest && ev.target.closest(".dcard");
    clearMarks();
    if (target && target !== dragged) {
      const r = target.getBoundingClientRect();
      target.classList.add(ev.clientY > r.top + r.height / 2
        ? "drop-after" : "drop-before");
    }
  });

  dash.addEventListener("drop", (ev) => {
    if (!dragged) return;
    ev.preventDefault();
    const target = ev.target.closest && ev.target.closest(".dcard");
    if (target && target !== dragged) {
      const r = target.getBoundingClientRect();
      const after = ev.clientY > r.top + r.height / 2;
      dash.insertBefore(dragged, after ? target.nextSibling : target);
    } else if (!target) {
      dash.appendChild(dragged);
    }
    clearMarks();
    syncFromDom();
  });

  dash.addEventListener("dragend", () => {
    if (dragged) dragged.classList.remove("dragging");
    dragged = null;
    clearMarks();
    cards().forEach((el) => { el.draggable = false; });
  });

  document.addEventListener("pointerup", () => {
    setTimeout(() => cards().forEach((el) => { el.draggable = false; }), 0);
  });

  const resetBtn = document.getElementById("dash-reset");
  if (resetBtn) resetBtn.addEventListener("click", reset);

  function init() {
    cards().forEach(addHandle);
    applySaved();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
  if (MQ.addEventListener) MQ.addEventListener("change", applySaved);
  else if (MQ.addListener) MQ.addListener(applySaved);
})();
