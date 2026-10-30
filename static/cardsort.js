/* ==========================================================
   设备卡片拖动排序
   ----------------------------------------------------------
   dashdrag.js 管的是左栏那几张统计卡；这里是设备网格里的大卡
   （#device-grid 下的 .device-card）。两者互不干扰。

   为什么用"手柄"而不是整张卡拖：卡片本身要响应点击（打开详情），
   整张卡可拖会把"点一下"变成拖拽，手感会坏。手柄是右上角那六点，
   悬停才现形，平时不抢视觉。

   为什么只允许同组内拖：网格是按「可控制 / 各房间」算出来的分组，
   把卡片拖到别的组里没有语义（切房间靠左栏）。所以顺序按 scope
   分开存，见 app.js 里 DEV_ORDER_KEY 的注释。

   顺序由 app.js 在 renderGrid 里应用（applyDevOrder），这里只管
   交互 + 落盘，删掉这个文件不影响渲染，只是不能拖。
   ========================================================== */
(function () {
  "use strict";

  const root = document.getElementById("device-grid");
  if (!root) return;

  let dragged = null;

  const scopeOf = (card) => {
    const g = card && card.closest(".grid");
    return g ? (g.dataset.scope || "") : "";
  };
  const gridOf = (card) => card && card.parentElement;

  function cardsIn(grid) {
    return Array.from(grid.querySelectorAll(":scope > .device-card"));
  }

  function saveGrid(grid) {
    if (!window.devOrder) return;
    const ids = cardsIn(grid).map((el) => el.dataset.did).filter(Boolean);
    window.devOrder.save(grid.dataset.scope || "", ids);
  }

  /* ---------- 手柄 ---------- */
  function addHandle(card) {
    if (card.querySelector(".dc-drag")) return;
    const grid = gridOf(card);
    if (!grid || grid.dataset.nosort) return;   // 搜索结果有序无义，不给拖
    const tools = card.querySelector(".dc-tools");
    if (!tools) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "dc-drag";
    btn.title = "拖动排序（Alt+↑/↓ 也行，双击恢复默认）";
    btn.setAttribute("aria-label", "拖动排序");
    btn.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="9" cy="6" r="1.7"/><circle cx="15" cy="6" r="1.7"/>
      <circle cx="9" cy="12" r="1.7"/><circle cx="15" cy="12" r="1.7"/>
      <circle cx="9" cy="18" r="1.7"/><circle cx="15" cy="18" r="1.7"/>
    </svg>`;
    // 手柄在卡片里，点它会冒泡到卡片的 onclick（打开详情）—— 必须掐掉
    btn.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      card.draggable = true;
    });
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
    });
    btn.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      const sc = scopeOf(card);
      if (window.devOrder) window.devOrder.clear(sc);
      if (typeof renderGrid === "function") renderGrid();
      if (typeof toast === "function") toast("已恢复该组默认顺序");
    });
    btn.addEventListener("keydown", (e) => {
      if (!e.altKey) return;
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      moveByKeyboard(card, e.key === "ArrowUp" ? -1 : 1);
      btn.focus();
    });
    tools.insertBefore(btn, tools.firstChild);
  }

  function addAllHandles() {
    root.querySelectorAll(".grid > .device-card").forEach(addHandle);
  }

  /* ---------- 键盘移动 ---------- */
  function moveByKeyboard(card, dir) {
    const grid = gridOf(card);
    if (!grid) return;
    const list = cardsIn(grid);
    const i = list.indexOf(card);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= list.length) return;
    if (dir < 0) grid.insertBefore(card, list[j]);
    else grid.insertBefore(card, list[j].nextSibling);
    saveGrid(grid);
    card.classList.add("dropping");
    setTimeout(() => card.classList.remove("dropping"), 220);
  }

  /* ---------- 拖拽 ---------- */
  function clearMarks() {
    root.querySelectorAll(".drop-before, .drop-after")
      .forEach((el) => el.classList.remove("drop-before", "drop-after"));
  }

  /* 网格是换行的，单看 X 或单看 Y 都会判错：
     同一行里左右移动要看 X，跨行往下拖要看 Y。垂直方向偏得够多就按 Y 算。 */
  function isAfter(target, ev) {
    const r = target.getBoundingClientRect();
    const dy = ev.clientY - (r.top + r.height / 2);
    const dx = ev.clientX - (r.left + r.width / 2);
    return Math.abs(dy) > r.height * 0.35 ? dy > 0 : dx > 0;
  }

  root.addEventListener("dragstart", (ev) => {
    const card = ev.target.closest && ev.target.closest(".device-card");
    if (!card || !card.draggable) { ev.preventDefault(); return; }
    dragged = card;
    card.classList.add("dragging");
    try {
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", card.dataset.did || "");
    } catch (e) {}
  });

  root.addEventListener("dragover", (ev) => {
    if (!dragged) return;
    const target = ev.target.closest && ev.target.closest(".device-card");
    // 跨组不让放：分组是算出来的，不是摆出来的
    if (!target || target === dragged || target.parentElement !== dragged.parentElement) {
      clearMarks();
      return;
    }
    ev.preventDefault();
    try { ev.dataTransfer.dropEffect = "move"; } catch (e) {}
    clearMarks();
    target.classList.add(isAfter(target, ev) ? "drop-after" : "drop-before");
  });

  root.addEventListener("drop", (ev) => {
    if (!dragged) return;
    const target = ev.target.closest && ev.target.closest(".device-card");
    if (target && target !== dragged && target.parentElement === dragged.parentElement) {
      ev.preventDefault();
      const after = isAfter(target, ev);
      dragged.parentElement.insertBefore(dragged, after ? target.nextSibling : target);
      saveGrid(dragged.parentElement);
      dragged.classList.add("dropping");
      setTimeout(() => dragged && dragged.classList.remove("dropping"), 220);
    }
    clearMarks();
  });

  root.addEventListener("dragend", () => {
    if (dragged) dragged.classList.remove("dragging");
    dragged = null;
    clearMarks();
    root.querySelectorAll(".device-card").forEach((el) => { el.draggable = false; });
  });

  document.addEventListener("pointerup", () => {
    // 松手后立刻取消可拖，否则下一次在卡片上按下鼠标也可能被当成拖拽起点
    setTimeout(() => {
      root.querySelectorAll(".device-card").forEach((el) => { el.draggable = false; });
    }, 0);
  });

  /* ---------- 网格重建后补手柄 ----------
     renderGrid 每次都是 innerHTML = "" 重建，手柄跟着没了。
     用 MutationObserver 而不是在 renderGrid 里回调：删掉本文件时
     app.js 一行都不用改。 */
  let pending = false;
  const mo = new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      addAllHandles();
    });
  });
  mo.observe(root, { childList: true, subtree: true });

  addAllHandles();
})();
