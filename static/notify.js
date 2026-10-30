/* ==========================================================================
   推送到手机 —— 配置面板
   --------------------------------------------------------------------------
   这里只做"填地址 + 点测试"这一件事。通道协议的差异全部在后端 notify.py
   里，前端不该知道 Bark 和企微的 JSON 长得不一样。

   一个必须注意的点：手机推送的地址本身就是密钥，所以 GET /api/notify
   只回脱敏串（api.day.app/xxxx…ab12）。用户没改的通道提交 url="" + keep=原下标，
   由后端把旧地址接回去 —— 否则一保存就把密钥写成 "xxxx…ab12" 了。
   ========================================================================== */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const TYPES = [
    ["wecom", "企业微信群机器人"],
    ["bark", "Bark（iPhone）"],
    ["ntfy", "ntfy"],
    ["feishu", "飞书机器人"],
    ["dingtalk", "钉钉机器人"],
    ["webhook", "通用 webhook"],
  ];

  const S = {
    cfg: null,
    rows: [],      // {keep, type, url, name, on, hint}
    busy: false,
    open: false,
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  /* ---------------- 面板骨架 ---------------- */
  function ensure() {
    let el = $("nt-panel");
    if (el) return el;
    el = document.createElement("div");
    el.id = "nt-panel";
    el.className = "evt-mask";
    el.innerHTML = `<div class="evt-sheet nt-sheet" role="dialog" aria-label="推送到手机">
      <div class="evt-sheet-hd">
        <h4>推送到手机</h4>
        <span class="nt-hd-sub">守住级别、合并同类、夜里安静</span>
        <button class="evt-x" id="nt-x" type="button" aria-label="关闭">
          <svg viewBox="0 0 22 22" width="15" height="15" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round"><path d="M5 5l12 12M17 5L5 17"/></svg>
        </button>
      </div>
      <div class="evt-sheet-bd nt-bd">
        <div class="nt-sec">
          <div class="nt-sec-t">通道</div>
          <div id="nt-rows"></div>
          <button class="nt-add" id="nt-add" type="button">＋ 添加一个通道</button>
        </div>
        <div class="nt-sec">
          <div class="nt-sec-t">规则</div>
          <div class="nt-grid">
            <label>推送级别不低于
              <select id="nt-min">
                <option value="good">正常（连"开灯"也推）</option>
                <option value="warn">留意（告警才推，推荐）</option>
                <option value="bad">警告（只推严重问题）</option>
              </select>
            </label>
            <label>免打扰时段
              <input id="nt-quiet" type="text" placeholder="23:30-07:30" maxlength="20">
            </label>
            <label>此时段仍推
              <select id="nt-quiet-lv">
                <option value="bad">仅警告</option>
                <option value="warn">留意以上</option>
                <option value="good">全部</option>
              </select>
            </label>
          </div>
          <p class="nt-note">同类事件 3 分钟内合并成一条，不会连环震。</p>
        </div>
      </div>
      <div class="evt-sheet-ft nt-ft">
        <span id="nt-msg"></span>
        <div class="nt-btns">
          <button class="btn-ghost nt-btn" id="nt-test" type="button">发一条测试</button>
          <button class="nt-btn nt-primary" id="nt-save" type="button">保存</button>
        </div>
      </div>
    </div>`;
    document.body.appendChild(el);

    el.addEventListener("click", (ev) => { if (ev.target === el) close(); });
    $("nt-x").addEventListener("click", close);
    $("nt-add").addEventListener("click", () => {
      S.rows.push({ keep: -1, type: "wecom", url: "", name: "", on: true, hint: "" });
      paintRows();
      const list = $("nt-rows").querySelectorAll(".nt-row");
      const last = list[list.length - 1];
      if (last) last.querySelector("input").focus();
    });
    $("nt-save").addEventListener("click", save);
    $("nt-test").addEventListener("click", test);
    return el;
  }

  /* ---------------- 行渲染 ---------------- */
  function paintRows() {
    const box = $("nt-rows");
    if (!box) return;
    if (!S.rows.length) {
      box.innerHTML = `<div class="nt-empty">
        还没有通道。加一个之后，达到级别的事件会直接发到你手机上 ——
        这样你不在电脑前也能知道家里出了什么事。</div>`;
      return;
    }
    box.innerHTML = S.rows.map((r, i) => `
      <div class="nt-row" data-i="${i}">
        <select class="nt-type">${TYPES.map(([v, n]) =>
          `<option value="${v}"${r.type === v ? " selected" : ""}>${n}</option>`).join("")}
        </select>
        <input class="nt-url" type="text" spellcheck="false"
               placeholder="${r.hint ? "已配置 " + esc(r.hint) + " · 留空不改" : "粘贴 webhook / 推送地址"}"
               value="${esc(r.url)}">
        <input class="nt-name" type="text" maxlength="30" placeholder="备注"
               value="${esc(r.name)}">
        <label class="nt-on" title="启用">
          <input type="checkbox"${r.on ? " checked" : ""}><i></i>
        </label>
        <button class="nt-del" type="button" title="删除">
          <svg viewBox="0 0 22 22" width="13" height="13" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round"><path d="M5 5l12 12M17 5L5 17"/></svg>
        </button>
      </div>`).join("");

    box.querySelectorAll(".nt-row").forEach((row) => {
      const i = +row.dataset.i;
      row.querySelector(".nt-type").addEventListener("change", (e) => {
        S.rows[i].type = e.target.value;
      });
      row.querySelector(".nt-url").addEventListener("input", (e) => {
        S.rows[i].url = e.target.value;
      });
      row.querySelector(".nt-name").addEventListener("input", (e) => {
        S.rows[i].name = e.target.value;
      });
      row.querySelector(".nt-on input").addEventListener("change", (e) => {
        S.rows[i].on = e.target.checked;
      });
      row.querySelector(".nt-del").addEventListener("click", () => {
        S.rows.splice(i, 1);
        paintRows();
      });
    });
  }

  function paintForm() {
    const c = S.cfg || {};
    const min = $("nt-min"), q = $("nt-quiet"), ql = $("nt-quiet-lv");
    if (min) min.value = c.min_level || "warn";
    if (q) q.value = c.quiet || "";
    if (ql) ql.value = c.quiet_level || "bad";
  }

  function paint() {
    paintForm();
    paintRows();
  }

  /* ---------------- 读写 ---------------- */
  function load(then) {
    fetch("/api/notify", { cache: "no-store" })
      .then((r) => r.json())
      .then((j) => {
        if (!j || !j.ok) return;
        S.cfg = j.config;
        S.rows = (j.config.channels || []).map((c) => ({
          keep: c.i, type: c.type, url: "", name: c.name, on: c.on, hint: c.hint,
        }));
        paint();
        if (then) then();
      })
      .catch(() => {
        const m = $("nt-msg");
        if (m) m.textContent = "读取配置失败";
      });
  }

  function collect() {
    return S.rows.map((r) => ({
      type: r.type,
      url: (r.url || "").trim(),
      name: r.name,
      on: !!r.on,
      keep: r.keep,
    }));
  }

  function save() {
    if (S.busy) return;
    const m = $("nt-msg");
    S.busy = true;
    if (m) m.textContent = "保存中…";
    fetch("/api/notify/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        channels: collect(),
        min_level: $("nt-min").value,
        quiet: $("nt-quiet").value.trim(),
        quiet_level: $("nt-quiet-lv").value,
      }),
    })
      .then((r) => r.json())
      .then((j) => {
        if (!j || !j.ok) {
          if (m) m.textContent = (j && j.error) || "保存失败";
          return;
        }
        S.cfg = j.config;
        const n = (j.config.channels || []).length;
        // 保存后不重绘行：input 里的原始地址要留着，否则再存一次就成了脱敏串
        S.rows = (j.config.channels || []).map((c) => ({
          keep: c.i, type: c.type, url: "", name: c.name, on: c.on, hint: c.hint,
        }));
        if (m) m.textContent = n ? `已保存 ${n} 个通道` : "已清空所有通道";
      })
      .catch((e) => { if (m) m.textContent = "保存失败：" + (e.message || ""); })
      .finally(() => { S.busy = false; });
  }

  function test() {
    if (S.busy) return;
    const m = $("nt-msg");
    S.busy = true;
    if (m) m.textContent = "发送中…";
    fetch("/api/notify/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then((r) => r.json())
      .then((j) => {
        if (!m) return;
        if (j && j.ok) m.textContent = "已发到：" + (j.sent || []).join("、");
        else if (j && j.failed && j.failed.length) m.textContent = "失败 · " + j.failed[0];
        else if (j && j.n_channels === 0) m.textContent = "还没有配置通道";
        else m.textContent = "发送失败";
      })
      .catch((e) => { if (m) m.textContent = "发送失败：" + (e.message || ""); })
      .finally(() => { S.busy = false; });
  }

  /* ---------------- 开关 ---------------- */
  function open() {
    S.open = true;
    ensure().classList.add("on");
    document.body.style.overflow = "hidden";
    const m = $("nt-msg");
    if (m) m.textContent = "";
    load();
  }
  function close() {
    S.open = false;
    const el = $("nt-panel");
    if (el) el.classList.remove("on");
    document.body.style.overflow = "";
  }

  function init() {
    const bell = $("evt-bell");
    if (!bell) return;
    bell.addEventListener("click", open);
    // 有通道就把铃铛点亮：否则"推送到底开没开"要每次点进去看
    fetch("/api/notify", { cache: "no-store" })
      .then((r) => r.json())
      .then((j) => {
        if (j && j.ok && (j.config.channels || []).some((c) => c.on)) {
          bell.classList.add("configured");
        }
      })
      .catch(() => {});
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && S.open) close();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
