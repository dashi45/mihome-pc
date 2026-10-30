/* ==========================================================
   定时开关 —— 前端
   ----------------------------------------------------------
   入口放在设备抽屉的电源按钮旁边（一个时钟图标）。
   为什么不是放在设置页里：定时开关这件事的意义全在"针对哪台设备、
   什么动作、什么时刻"，离开具体设备就没法配了。

   面板本身沿用现成的弹层样式（和"全部动态""推送设置"一样）。
   ========================================================== */

const Sched = (() => {
  const S = {
    rules: [],
    next: [],
    open: false,
    did: null,
    dev: null,
    siid: 0,
    piid: 0,
    action: "on",     // on | off
  };

  const $ = (id) => document.getElementById(id);

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (m) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[m]));
  }

  /* ---------------- 打开 / 关闭 ---------------- */
  function openPanel(dev, siid, piid) {
    S.dev = dev;
    S.did = dev.did;
    S.siid = siid;
    S.piid = piid;
    S.open = true;
    ensurePanel();
    const m = $("sched-mask");
    if (!m) return;
    const t = $("sched-dev");
    if (t) t.textContent = dev.name || "";
    m.classList.add("on");
    document.body.classList.add("sched-lock");
    load();
  }

  function close() {
    S.open = false;
    const m = $("sched-mask");
    if (m) m.classList.remove("on");
    document.body.classList.remove("sched-lock");
  }

  /* ---------------- 数据 ---------------- */
  async function load() {
    const r = await api("/api/sched");
    if (!r || !r.ok) return;
    S.rules = r.rules || [];
    S.next = r.next || [];
    paint();
  }

  /* ---------------- 渲染 ---------------- */
  function paint() {
    const body = $("sched-body");
    if (!body) return;
    const mine = S.rules.filter((r) => r.did === S.did);

    let html = `
      <div class="sd-new">
        <div class="sd-new-hd">为这台设备添加定时</div>
        <div class="sd-form">
          <label class="sd-f">
            <span>动作</span>
            <div class="sd-seg" id="sd-action">
              <button type="button" data-act="on" class="${S.action === "on" ? "on" : ""}">开启</button>
              <button type="button" data-act="off" class="${S.action === "off" ? "on" : ""}">关闭</button>
            </div>
          </label>
          <label class="sd-f">
            <span>时间</span>
            <input type="time" id="sd-time" value="07:00" step="60">
          </label>
          <button class="sd-add" id="sd-add" type="button">添加</button>
        </div>
        <div class="sd-tip">只在整点那一分钟执行一次。程序没开着的时段会跳过并记录，不会事后补开。</div>
      </div>`;

    if (mine.length) {
      html += `<div class="sd-list-hd">这台设备的定时</div><div class="sd-list">`;
      html += mine.map((r) => {
        const on = r.enabled !== false;
        const st = r.last_result === "ok" ? "今天已执行"
          : (r.last_result === "fail" ? "上次失败" : "");
        return `<div class="sd-row${on ? "" : " off"}">
          <span class="sd-time">${esc(r.time)}</span>
          <span class="sd-act">${r.value ? "开启" : "关闭"}</span>
          <span class="sd-st">${st}</span>
          <span class="sd-gap"></span>
          <button class="sd-toggle${on ? " on" : ""}" data-tog="${esc(r.id)}" type="button"
                  title="${on ? "停用" : "启用"}"></button>
          <button class="sd-del" data-del="${esc(r.id)}" type="button" title="删除">×</button>
        </div>`;
      }).join("");
      html += `</div>`;
    } else {
      html += `<div class="sd-empty">这台设备还没有定时规则</div>`;
    }

    const others = S.rules.filter((r) => r.did !== S.did);
    if (others.length) {
      html += `<div class="sd-list-hd">其他设备的定时</div><div class="sd-list">`;
      html += others.slice(0, 12).map((r) => {
        const on = r.enabled !== false;
        return `<div class="sd-row${on ? "" : " off"}">
          <span class="sd-time">${esc(r.time)}</span>
          <span class="sd-name">${esc(r.name || r.did)}</span>
          <span class="sd-act">${r.value ? "开启" : "关闭"}</span>
          <span class="sd-gap"></span>
          <button class="sd-toggle${on ? " on" : ""}" data-tog="${esc(r.id)}" type="button"></button>
          <button class="sd-del" data-del="${esc(r.id)}" type="button" title="删除">×</button>
        </div>`;
      }).join("");
      html += `</div>`;
    }

    body.innerHTML = html;
    wire();
  }

  /* ---------------- 交互 ---------------- */
  function wire() {
    const seg = $("sd-action");
    if (seg) {
      seg.querySelectorAll("button").forEach((b) => {
        b.onclick = () => {
          S.action = b.dataset.act;
          paint();
        };
      });
    }
    const add = $("sd-add");
    if (add) add.onclick = doAdd;
    const ti = $("sd-time");
    if (ti) ti.onchange = () => { /* 保留用户选择 */ };

    document.querySelectorAll("#sched-body .sd-toggle").forEach((b) => {
      b.onclick = async () => {
        const r = S.rules.find((x) => x.id === b.dataset.tog);
        if (!r) return;
        await post("/api/sched/save", {
          id: r.id, did: r.did, name: r.name, time: r.time,
          siid: r.siid, piid: r.piid, value: r.value,
          enabled: !(r.enabled !== false),
        });
        await load();
      };
    });
    document.querySelectorAll("#sched-body .sd-del").forEach((b) => {
      b.onclick = async () => {
        await post("/api/sched/delete", { id: b.dataset.del });
        await load();
      };
    });
  }

  async function doAdd() {
    const ti = $("sd-time");
    const time = ti ? ti.value : "";
    if (!/^\d{2}:\d{2}$/.test(time)) { toast("请填写正确的时间", true); return; }
    const r = await post("/api/sched/save", {
      did: S.did,
      name: S.dev ? S.dev.name : "",
      time,
      siid: S.siid,
      piid: S.piid,
      value: S.action === "on",
      enabled: true,
    });
    if (!r.ok) { toast(r.error || "添加失败", true); return; }
    toast("已添加定时");
    await load();
  }

  /* ---------------- 挂载 ---------------- */
  function ensurePanel() {
    if ($("sched-mask")) return;
    const el = document.createElement("div");
    el.id = "sched-mask";
    el.className = "evt-mask";
    el.innerHTML = `<div class="evt-sheet sd-sheet" role="dialog" aria-label="定时开关">
      <div class="evt-sheet-hd">
        <div>
          <h3>定时开关<em id="sched-dev"></em></h3>
          <p>到点自动执行。程序没运行的时段会跳过并记录，不会事后补开。</p>
        </div>
        <button class="sd-x" id="sched-close" type="button" aria-label="关闭">×</button>
      </div>
      <div class="evt-sheet-bd" id="sched-body"></div>
    </div>`;
    document.body.appendChild(el);
  }

  function init() {
    ensurePanel();
    const m = $("sched-mask");
    if (!m) return;
    m.addEventListener("click", (e) => { if (e.target === m) close(); });
    const x = $("sched-close");
    if (x) x.onclick = close;
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && S.open) close();
    });
  }

  return { init, openPanel, close, load };
})();

window.Sched = Sched;

// 页面就绪后挂载面板。
// 注意：init 定义在上面的 IIFE 内部，外面直接调 init() 会报
// "init is not defined"——那样整个模块都不会初始化，
// 时钟按钮点了没反应。必须走 Sched.init。
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", function () { Sched.init(); });
} else {
  Sched.init();
}
