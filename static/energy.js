/* ==========================================================
   能源 · 日均 / 总量 / 每日用电 / 设备排行
   ----------------------------------------------------------
   版式照米家 App 的「能源」页：月份切换 + 周/月、两个大数、
   环比横幅、每日柱状图（含日均虚线）、用电排行。

   数据来自 /api/energy：
     · 插座 → 云端按天累计电量（准）
     · 空调伴侣 → 云端只有功率点，后端积分成电量（约 4% 误差）
   ========================================================== */
(function () {
  "use strict";

  const RELOAD = 5 * 60 * 1000;   // 云端数据 5 分钟同步一次
  const TARIFF_KEY = "mi-tariff-v1";
  const DEFAULT_TARIFF = {
    mode: "flat", price: 0.55, fixed: 0,
    tiers: [
      { upTo: 180, price: 0.52 },
      { upTo: 280, price: 0.57 },
      { upTo: null, price: 0.82 },
    ],
  };

  const E = {
    range: "month",
    offset: 0,
    data: null,
    /* 顶部三格方块里的"今日 / 本月"缓存。
       只从既有的 /api/energy 回包里摘，不额外发请求；卡片切到周/上月时
       仍然显示当前月的数字，不会被切走。 */
    month: null,          // {sum, avg, today, label, at}
    tariff: null,         // 电价设置，本机 localStorage
    busy: false,
    at: 0,
    polls: 0,
    error: null,
  };

  const $ = id => document.getElementById(id);

  function fmtKwh(v) {
    if (v == null || !isFinite(v)) return "—";
    if (v >= 100) return v.toFixed(1);
    return v.toFixed(2);
  }

  function fmtMoney(v) {
    if (v == null || !isFinite(v)) return "—";
    return Number(v).toFixed(2);
  }

  /* ---------------- 电价 / 账单 ----------------
     电价先存本机（localStorage），不新增后端请求；账单用当前 /api/energy
     已经返回的按天总量算，切换周/月时只影响卡片，账单按钮固定按自然月。
     峰谷电价需要小时级电量，现有数据只有按天，所以这一版只做单一/阶梯。 */
  function num(v, fallback, min, max) {
    const n = Number(v);
    if (!isFinite(n)) return fallback;
    return Math.min(max, Math.max(min, n));
  }

  function normalizeTariff(raw) {
    const d = JSON.parse(JSON.stringify(DEFAULT_TARIFF));
    if (!raw || typeof raw !== "object") return d;
    d.mode = raw.mode === "tier" ? "tier" : "flat";
    d.price = num(raw.price, d.price, 0, 20);
    d.fixed = num(raw.fixed, 0, 0, 1000);
    const tiers = Array.isArray(raw.tiers) ? raw.tiers : [];
    for (let i = 0; i < 3; i++) {
      const src = tiers[i] || {};
      d.tiers[i].price = num(src.price, d.tiers[i].price, 0, 20);
      if (i < 2) d.tiers[i].upTo = num(src.upTo, d.tiers[i].upTo, 0, 100000);
    }
    if (d.tiers[1].upTo <= d.tiers[0].upTo) {
      d.tiers[1].upTo = d.tiers[0].upTo + 100;
    }
    return d;
  }

  function loadTariff() {
    let raw = null;
    try { raw = JSON.parse(localStorage.getItem(TARIFF_KEY) || "null"); } catch (e) {}
    E.tariff = normalizeTariff(raw);
  }

  function saveTariff() {
    try { localStorage.setItem(TARIFF_KEY, JSON.stringify(E.tariff)); } catch (e) {}
  }

  /* 把一天的 kWh 按"当天开始前的累计量"跨档拆开计价 */
  function tierCost(kwh, start, tariff) {
    const k = Number(kwh) || 0;
    if (tariff.mode !== "tier") return k * Number(tariff.price || 0);
    let left = k, lower = 0, cost = 0;
    for (const t of tariff.tiers || []) {
      const upper = t.upTo == null ? Infinity : Number(t.upTo);
      if (start >= upper) { lower = upper; continue; }
      const from = Math.max(start, lower);
      const take = Math.min(left, Math.max(0, upper - from));
      if (take > 0) {
        cost += take * Number(t.price || 0);
        left -= take;
      }
      lower = upper;
      if (left <= 0) break;
    }
    return cost;
  }

  function tierLabel(lower, upper) {
    if (upper === Infinity) return `${lower} kWh 以上`;
    return lower > 0 ? `${lower}–${upper} kWh` : `0–${upper} kWh`;
  }

  function calcBill(d, tariff) {
    const total = d.total || [];
    let cum = 0, variable = 0;
    const dayCosts = [];
    for (const v of total) {
      const k = Number(v) || 0;
      const c = tierCost(k, cum, tariff);
      variable += c;
      cum += k;
      dayCosts.push(c);
    }
    const fixed = Number(tariff.fixed) || 0;
    const cost = variable + fixed;
    const elapsed = Number(d.daysCounted) || (d.dates || []).length || 0;
    const monthDays = (d.days || []).length || elapsed;
    const proj = elapsed > 0 && monthDays > elapsed
      ? (variable / elapsed * monthDays + fixed) : null;

    const tiers = [];
    if (tariff.mode === "tier") {
      let lower = 0;
      for (const t of tariff.tiers || []) {
        const upper = t.upTo == null ? Infinity : Number(t.upTo);
        const used = Math.max(0, Math.min(cum, upper) - lower);
        if (used > 0) {
          tiers.push({
            label: tierLabel(lower, upper), used,
            price: Number(t.price || 0), amount: used * Number(t.price || 0),
          });
        }
        lower = upper;
        if (cum <= upper) break;
      }
    }
    return { cost, variable, fixed, dayCosts, cum, elapsed, monthDays, proj, tiers };
  }

  function tariffText(t) {
    if (!t) return "";
    if (t.mode === "tier") {
      return `阶梯电价 · 最高档 ${Number(t.tiers[2].price).toFixed(2)} 元/kWh`;
    }
    return `${Number(t.price).toFixed(2)} 元/kWh`;
  }

  let billOpen = false;
  let billData = null;   // 账单固定看"本月用电"快照，不跟能源卡翻月走

  function paintCost() {
    // 电费不再单独占下方统计位；这里只负责让打开着的账单弹层跟着数据刷新
    if (!billOpen) return;
    if (E.month && E.month.data) billData = E.month.data;
    paintBill();
  }

  function paintBill() {
    const d = billData || E.data;
    if (!d) return;
    const t = E.tariff || DEFAULT_TARIFF;
    const b = calcBill(d, t);
    const cost = $("bill-cost"), month = $("bill-month"), sub = $("bill-sub");
    if (cost) cost.textContent = fmtMoney(b.cost);
    if (month) month.textContent = d.label || "";
    if (sub) sub.textContent = `${d.label || "本期"} · ${tariffText(t)}`;
    const proj = $("bill-proj");
    if (proj) {
      proj.textContent = b.proj != null ? `≈ ${fmtMoney(b.proj)} 元`
        : (b.elapsed >= b.monthDays && b.elapsed > 0 ? "已结束" : "—");
    }
    const avg = $("bill-avg");
    if (avg) avg.textContent = b.elapsed > 0 ? `${fmtMoney(b.cost / b.elapsed)} 元/天` : "—";
    const prev = $("bill-prev");
    if (prev) {
      prev.textContent = d.prevAvg == null ? "—"
        : `${Number(d.prevAvg).toFixed(2)} kWh/天`;
    }

    const box = $("bill-tier");
    if (!box) return;
    if (!b.cum) {
      box.innerHTML = `<div class="bill-tier-empty">这个周期还没有用电记录</div>`;
      return;
    }
    let rows = "";
    if (t.mode === "tier") {
      for (const x of b.tiers) {
        rows += `<div class="bill-tier-row"><span>${x.label}</span>
          <b>${fmtKwh(x.used)} kWh</b><em>${fmtMoney(x.amount)} 元</em></div>`;
      }
    } else {
      rows += `<div class="bill-tier-row">
        <span>${Number(t.price).toFixed(2)} 元/kWh</span>
        <b>${fmtKwh(b.cum)} kWh</b><em>${fmtMoney(b.variable)} 元</em></div>`;
    }
    if (b.fixed > 0) {
      rows += `<div class="bill-tier-row"><span>固定月租</span>
        <b>—</b><em>${fmtMoney(b.fixed)} 元</em></div>`;
    }
    box.innerHTML = rows;
  }

  function fillTariffForm() {
    const t = E.tariff || DEFAULT_TARIFF;
    const mode = $("tariff-mode");
    if (!mode) return;
    mode.value = t.mode;
    $("tariff-price").value = Number(t.price).toFixed(2);
    $("tariff-fixed").value = Number(t.fixed).toFixed(2);
    $("tier1-up").value = t.tiers[0].upTo;
    $("tier1-price").value = Number(t.tiers[0].price).toFixed(2);
    $("tier2-up").value = t.tiers[1].upTo;
    $("tier2-price").value = Number(t.tiers[1].price).toFixed(2);
    $("tier3-price").value = Number(t.tiers[2].price).toFixed(2);
    $("tariff-flat").classList.toggle("hidden", t.mode !== "flat");
    $("tariff-tier").classList.toggle("hidden", t.mode !== "tier");
  }

  function readTariffForm() {
    const mode = $("tariff-mode").value === "tier" ? "tier" : "flat";
    const price = Number($("tariff-price").value);
    const fixed = Number($("tariff-fixed").value) || 0;
    const up1 = Number($("tier1-up").value);
    const up2 = Number($("tier2-up").value);
    const p1 = Number($("tier1-price").value);
    const p2 = Number($("tier2-price").value);
    const p3 = Number($("tier3-price").value);
    if (!isFinite(price) || price < 0 || price > 20) {
      toast("电价应在 0–20 元/kWh 之间", true); return null;
    }
    if (!isFinite(fixed) || fixed < 0 || fixed > 1000) {
      toast("固定月租应在 0–1000 元之间", true); return null;
    }
    if (mode === "tier") {
      if (!isFinite(up1) || !isFinite(up2) || up1 <= 0 || up2 <= up1) {
        toast("阶梯上限要递增：第一档 < 第二档", true); return null;
      }
      for (const p of [p1, p2, p3]) {
        if (!isFinite(p) || p < 0 || p > 20) {
          toast("阶梯单价应在 0–20 元/kWh 之间", true); return null;
        }
      }
    }
    return normalizeTariff({
      mode, price, fixed,
      tiers: [
        { upTo: up1, price: p1 },
        { upTo: up2, price: p2 },
        { upTo: null, price: p3 },
      ],
    });
  }

  function saveTariffFromForm() {
    const t = readTariffForm();
    if (!t) return;
    E.tariff = t;
    saveTariff();
    paintPower();   // 本月方块里的电费立刻按新电价重算
    paintCost();
    paintBill();
    toast("电价已保存");
  }

  function openBill() {
    const d = E.month && E.month.data;
    if (!d) {
      toast("本月用电还在读取，稍等再点", true);
      return;
    }
    billData = d;
    billOpen = true;
    fillTariffForm();
    paintBill();
    $("en-bill-modal").classList.remove("hidden");
  }

  function closeBill() {
    billOpen = false;
    billData = null;
    $("en-bill-modal").classList.add("hidden");
  }

  function exportCsv() {
    const d = billData || E.data;
    if (!d) return;
    const b = calcBill(d, E.tariff || DEFAULT_TARIFF);
    const rows = [["日期", "用电(kWh)", "电费(元)"]];
    const dates = d.dates || [], total = d.total || [];
    for (let i = 0; i < dates.length; i++) {
      rows.push([dates[i], (Number(total[i]) || 0).toFixed(3),
        (b.dayCosts[i] || 0).toFixed(2)]);
    }
    rows.push(["合计", b.cum.toFixed(3), b.cost.toFixed(2)]);
    rows.push([]);
    rows.push(["设备", "用电(kWh)"]);
    for (const x of (d.devices || [])) {
      rows.push([x.name || "设备", Number(x.kwh || 0).toFixed(2)]);
    }
    const csv = "\ufeff" + rows.map((r) => r.map((v) =>
      `"${String(v == null ? "" : v).replace(/"/g, '""')}"`).join(",")).join("\r\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `米家电费-${(d.label || "本期").replace(/[^\d年月]/g, "")}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  function wireBill() {
    const open = $("pw-month-cost-btn"), modal = $("en-bill-modal"), close = $("bill-close");
    if (!open || !modal) return;
    open.addEventListener("click", openBill);
    if (close) close.addEventListener("click", closeBill);
    modal.addEventListener("click", (ev) => { if (ev.target === modal) closeBill(); });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && billOpen) closeBill();
    });
    const exp = $("bill-export");
    if (exp) exp.addEventListener("click", exportCsv);
    const tg = $("bill-tariff-toggle"), form = $("bill-tariff");
    if (tg && form) {
      tg.addEventListener("click", () => {
        form.classList.toggle("hidden");
        if (!form.classList.contains("hidden")) fillTariffForm();
      });
    }
    const mode = $("tariff-mode");
    if (mode) {
      mode.addEventListener("change", () => {
        $("tariff-flat").classList.toggle("hidden", mode.value !== "flat");
        $("tariff-tier").classList.toggle("hidden", mode.value !== "tier");
      });
    }
    const save = $("tariff-save");
    if (save) save.addEventListener("click", saveTariffFromForm);
  }


  /* ---------------- 取数 ---------------- */
  /* 从 /api/energy 的回包里摘出顶部方块要用的两个数：
       · 今日用电 = total 里今天那一格（周视图也包含今天，照样能更新）
       · 本月用电 = 只有 range=month & offset=0 的那次回包才是"本月"，
                    所以按快照存起来，切到周/上月时不跟着变。
     这是纯前端缓存，没有新增任何请求。 */
  function noteMonth(j) {
    const dates = j.dates || [], tot = j.total || [];
    const ti = j.today ? dates.indexOf(j.today) : -1;
    const todayKwh = ti >= 0 ? (tot[ti] || 0) : null;
    if (j.range === "month" && j.offset === 0) {
      E.month = {
        sum: j.sum, avg: j.avg, today: todayKwh,
        label: j.label || "", at: Date.now(), data: j,
      };
    } else if (E.month && todayKwh != null) {
      // 非本月窗口里今天那一格仍是今天的真实用量
      E.month.today = todayKwh;
    }
  }

  function load(force) {
    if (E.busy) {                      // 正在补历史，等它回来再发，别把点击吃掉
      setTimeout(() => load(force), 350);
      return;
    }
    if (!force && E.at && Date.now() - E.at < RELOAD && !(E.data && E.data.partial)) return;
    E.busy = true;
    const url = `/api/energy?range=${E.range}&offset=${E.offset}${force ? "&refresh=1" : ""}`;
    fetch(url)
      .then(r => r.json())
      .then(j => {
        if (j && j.ok) {
          E.data = j;
          E.at = Date.now();
          E.error = null;
          noteMonth(j);
        } else {
          E.error = (j && j.error) || "读取失败";
        }
        render();
        // 空调的历史电量要一天一天补，没补完就再来一轮
        if (j && j.ok && j.partial && E.polls < 12) {
          E.polls++;
          setTimeout(() => { E.busy = false; load(true); }, 1200);
        }
      })
      .catch(e => {
        console.warn("[energy]", e);
        E.error = "读取失败";
        render();
      })
      .finally(() => { E.busy = false; });
  }

  /* ---------------- 数字区 ---------------- */
  function paintNumbers() {
    const d = E.data || {};
    // 图下一行小字：把“总量含今天”说清楚，和米家 App 的数字对不上时能自己看明白
    const note = $("en-sub");
    if (note) {
      const dates = d.dates || [], tot = d.total || [];
      const ti = d.today ? dates.indexOf(d.today) : -1;
      let sumPrev = 0;
      for (let i = 0; i < tot.length; i++) if (i !== ti) sumPrev += tot[i] || 0;
      const todayV = ti >= 0 ? (tot[ti] || 0) : 0;
      const bits = [];
      if (ti >= 0) {
        bits.push(`今日 ${todayV.toFixed(2)} · 截至昨日 ${sumPrev.toFixed(2)} kWh`);
      }
      if (d.partial) bits.push("统计中…");
      if (!bits.length) bits.push("米家云端记录");
      note.textContent = bits.join(" · ");
      note.title = "插座电量取自米家云端日累计；空调伴侣云端不存电量，由功率上报点积分估算（误差约 ±3%）；" +
        "“总量”包含今天到此刻的用量，所以通常比 App 显示的整月数字略大。";
    }

    const lab = $("en-label");
    if (lab) lab.textContent = d.label || "—";

    const avg = $("en-avg"), tot = $("en-total");
    if (avg) avg.textContent = d.avg == null ? "—" : d.avg.toFixed(2);
    if (tot) tot.textContent = d.sum == null ? "—" : fmtKwh(d.sum);

    const box = $("en-delta"), txt = $("en-delta-t");
    if (box && txt) {
      if (E.error) {
        txt.textContent = E.error;
        box.className = "en-delta";
        box.style.display = "";
      } else if (d.delta == null) {
        box.style.display = "none";
      } else {
        box.style.display = "";
        const down = d.delta < 0;
        box.className = "en-delta " + (down ? "down" : "up");
        const v = Math.abs(d.delta).toFixed(2);
        txt.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none"
          stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="${down ? "M12 5v14M6 13l6 6 6-6" : "M12 19V5M6 11l6-6 6 6"}"/></svg>
          较上月平均${down ? "降低" : "升高"} ${v} kWh`;
      }
    }
    paintCost();
  }

  /* ---------------- 柱状图 ---------------- */
  function scaleFor(peak) {
    const steps = [0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 500];
    for (const s of steps) {
      if (peak <= s * 4) {
        const top = Math.ceil(peak / s) * s;
        return { step: s, top, div: Math.max(1, Math.round(top / s)) };
      }
    }
    const s = Math.ceil(peak / 4);
    return { step: s, top: s * 4, div: 4 };
  }

  function drawChart() {
    const wrap = $("en-chart-wrap"), svg = $("en-chart");
    const gGrid = $("en-grid"), gBars = $("en-bars"),
          gLine = $("en-line"), gAxis = $("en-axis");
    const empty = $("en-empty");
    const d = E.data;
    if (!wrap || !svg || !d || !d.days || !d.days.length) return;

    // 同 climate.draw()：夹紧高度，防止容器被撑高后 viewBox 跟着无限长
    const W = wrap.clientWidth, H = Math.min(wrap.clientHeight, 420);
    if (!W || !H) return;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

    const PAD = { l: 4, r: 40, t: 16, b: 22 };
    const PW = W - PAD.l - PAD.r, PH = H - PAD.t - PAD.b;
    const n = d.days.length;
    const vals = d.total || [];
    const peak = Math.max(...vals.map(v => v || 0), d.avg || 0, 0.01);
    const sc = scaleFor(peak);
    const Y = v => PAD.t + PH * (1 - Math.min(1, Math.max(0, v) / sc.top));
    const slot = PW / n;
    const bw = Math.max(3, Math.min(n <= 10 ? 26 : 11, slot * 0.6));

    if (empty) empty.classList.toggle("hidden", vals.some(v => v > 0));

    // 网格 + 右侧刻度
    let grid = "", axis = "";
    for (let i = 0; i <= sc.div; i++) {
      const v = (sc.top / sc.div) * i, y = Y(v);
      grid += `<line x1="${PAD.l}" y1="${y.toFixed(1)}" x2="${(PAD.l + PW).toFixed(1)}"
        y2="${y.toFixed(1)}" stroke="var(--line)" stroke-width="1"
        stroke-dasharray="${i === 0 ? "0" : "2 5"}" opacity=".55"/>`;
      const label = sc.step < 1 ? v.toFixed(1) : String(Math.round(v));
      axis += `<text x="${(PAD.l + PW + 6).toFixed(1)}" y="${(y + 3.4).toFixed(1)}"
        font-size="9.5" fill="var(--faint)">${label}</text>`;
    }

    // 柱子
    let bars = "";
    for (let i = 0; i < n; i++) {
      const v = vals[i];
      if (v == null || v <= 0) continue;
      const x = PAD.l + slot * (i + 0.5) - bw / 2;
      const y = Y(v), h = Math.max(1.5, Y(0) - y);
      bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}"
        height="${h.toFixed(1)}" rx="1.6" fill="var(--en-bar)"/>`;
    }

    // 日均虚线 + 绿色胶囊
    let line = "";
    if (d.avg > 0 && d.avg <= sc.top * 1.001) {
      const y = Y(d.avg);
      line += `<line x1="${PAD.l}" y1="${y.toFixed(1)}" x2="${(PAD.l + PW).toFixed(1)}"
        y2="${y.toFixed(1)}" stroke="var(--en-avg)" stroke-width="1.2"
        stroke-dasharray="3 4" opacity=".85"/>`;
      const label = d.avg >= 10 ? d.avg.toFixed(1) : d.avg.toFixed(2);
      const pw = Math.max(30, label.length * 6.2 + 10);
      line += `<g>
        <rect x="${(PAD.l + PW - pw).toFixed(1)}" y="${(y - 8.5).toFixed(1)}"
          width="${pw.toFixed(1)}" height="17" rx="5" fill="var(--en-avg)"/>
        <text x="${(PAD.l + PW - pw / 2).toFixed(1)}" y="${(y + 3).toFixed(1)}"
          text-anchor="middle" font-size="9.5" font-weight="600"
          fill="#fff">${label}</text>
      </g>`;
    }

    // 底部日期刻度
    const step = Math.max(1, Math.ceil(n / 5));
    for (let i = 0; i < n; i += step) {
      const x = PAD.l + slot * (i + 0.5);
      axis += `<text x="${x.toFixed(1)}" y="${(H - 5).toFixed(1)}" text-anchor="middle"
        font-size="9.5" fill="var(--faint)">${d.days[i]}</text>`;
    }

    gGrid.innerHTML = grid;
    gBars.innerHTML = bars;
    gLine.innerHTML = line;
    gAxis.innerHTML = axis;
  }

  /* ---------------- 排行榜 ---------------- */
  const IC_PLUG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
      stroke-linecap="round" stroke-linejoin="round">
      <path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-6 6 6 6 0 0 1-6-6V8z"/><path d="M12 17v4"/></svg>`;
  const IC_AC = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
      stroke-linecap="round" stroke-linejoin="round">
      <rect x="3" y="5" width="18" height="8" rx="2.4"/><path d="M7 9h10"/>
      <path d="M7 16c0 1.6 1 2 1 3.2M12 16c0 2 1 2.4 1 4M17 16c0 1.2-1 1.6-1 3"/></svg>`;

  function paintRank() {
    const box = $("en-rank-list"), sec = $("en-rank");
    const d = E.data;
    if (!box) return;
    const list = (d && d.devices) || [];
    if (!list.length) {
      if (sec) sec.classList.add("hidden");
      return;
    }
    if (sec) sec.classList.remove("hidden");
    const top = list[0].kwh || 1;
    box.innerHTML = list.map(x => {
      const isAc = /ac|空调/i.test((x.model || "") + (x.name || ""));
      const w = Math.max(2, Math.round((x.kwh / top) * 100));
      return `<div class="en-row">
        <span class="en-ic ${isAc ? "ac" : "plug"}">${isAc ? IC_AC : IC_PLUG}</span>
        <div class="en-row-main">
          <div class="en-row-top">
            <b>${esc(x.name)}</b>
            <span class="en-row-kwh">${x.kwh.toFixed(2)} <em>kWh</em></span>
          </div>
          <div class="en-row-sub">${esc(x.room || "未分组")}</div>
          <div class="en-row-bar"><i style="width:${w}%"></i></div>
        </div>
      </div>`;
    }).join("");
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  /* ---------------- 当前功率卡 ---------------- */

  function powerRows() {
    const live = (typeof state !== "undefined" && state.live) || {};
    const nameOf = {};
    if (typeof state !== "undefined") {
      for (const d of state.devices || []) nameOf[d.did] = d.name;
    }
    const rows = [];
    for (const did in live) {
      const lv = live[did] || {};
      if (lv.watt == null || !isFinite(lv.watt)) continue;
      rows.push({ did, name: nameOf[did] || "设备", watt: lv.watt });
    }
    // 充电器是蓝牙直连的，不在米家设备表里，但它确实在给设备供电 ——
    // 用户希望「当前总功率」把这部分算进去。只在实际有输出时出现，
    // 0W 不占一行，免得空着的时候多一个没意义的条目。
    const cw = (typeof state !== "undefined" && Number(state.chargerW)) || 0;
    if (cw > 0.5) {
      rows.push({ did: "__charger__", name: "充电器", watt: cw, charger: true });
    }
    rows.sort((a, b) => b.watt - a.watt);
    return rows;
  }

  function paintPower() {
    const card = $("power-card");
    if (!card || !card.offsetParent) return;
    const rows = powerRows();
    const total = rows.reduce((a, r) => a + r.watt, 0);

    /* 第一格：当前功率（/api/live 的实时 watt + 蓝牙充电器 charger_w） */
    const num = $("pw-total");
    if (num) {
      const big = total >= 1000;
      num.textContent = big ? (total / 1000).toFixed(2) : String(Math.round(total));
      const unit = $("pw-total-u") || num.nextElementSibling;
      if (unit) unit.textContent = big ? "kW" : "W";
      num.title = "实时功率，包含米家设备和蓝牙充电器";
    }
    const sub = $("pw-sub");
    if (sub) {
      const live = rows.filter(r => r.watt >= 1).length;
      const hasChg = rows.some(r => r.charger);
      sub.textContent = rows.length
        ? `${live} 台在用电${hasChg ? " · 含充电器" : ""}${state && state.liveReady === false ? " · 读取中" : ""}`
        : "等待数据";
    }

    /* 第二、三格：今日 / 本月（复用 /api/energy 的既有回包，不另发请求） */
    const m = E.month;
    const todayEl = $("pw-today"), monthEl = $("pw-month");
    if (todayEl) {
      todayEl.textContent = m && m.today != null ? fmtKwh(m.today) : "—";
      todayEl.title = "今天 0 点到现在累计用电";
    }
    if (monthEl) {
      monthEl.textContent = m && m.sum != null ? fmtKwh(m.sum) : "—";
      monthEl.title = m ? `${m.label || "本月"}累计用电（含今天）` : "还在读取本月统计";
    }
    const tsub = $("pw-today-sub"), msub = $("pw-month-sub");
    if (tsub) tsub.textContent = m && m.today != null ? "截至现在" : "等待统计";
    if (msub) msub.textContent = m && m.label ? m.label : "本月";

    /* 「电费」归属在本月用电方块里：用本月快照算，切周/上月不影响它 */
    const costEl = $("pw-month-cost");
    if (costEl) {
      let cost = null;
      if (m && m.data) {
        cost = calcBill(m.data, E.tariff || DEFAULT_TARIFF).cost;
      } else if (m && m.sum != null) {
        const t = E.tariff || DEFAULT_TARIFF;
        cost = t.mode === "tier"
          ? tierCost(m.sum, 0, t) + Number(t.fixed || 0)
          : m.sum * Number(t.price || 0) + Number(t.fixed || 0);
      }
      costEl.textContent = cost == null ? "—" : fmtMoney(cost);
      costEl.title = "按当前电价估算，点击查看账单明细";
    }
  }

  /* ---------------- 主渲染 ---------------- */
  function render() {
    const sec = $("energy-section");
    if (!sec) return;
    /* 顶部三格方块不依赖能源卡是否可见：没有统计设备时能源卡隐藏，
       功率/今日/本月仍要能显示。 */
    paintPower();
    const d = E.data;
    const has = d && ((d.devices || []).length || (d.total || []).some(v => v));
    if (!has && !E.error) {
      sec.classList.add("hidden");
      return;
    }
    sec.classList.remove("hidden");
    paintNumbers();
    drawChart();
    paintRank();
  }

  /* ---------------- 交互 ---------------- */
  function wire() {
    const seg = $("en-range");
    if (seg) {
      const sync = () => seg.querySelectorAll("button").forEach(b =>
        b.classList.toggle("active", b.dataset.range === E.range));
      sync();
      seg.onclick = ev => {
        const b = ev.target.closest("button");
        if (!b || b.dataset.range === E.range) return;
        E.range = b.dataset.range;
        E.offset = 0;
        E.at = 0;
        E.polls = 0;
        sync();
        render();
        load(true);
      };
    }

    const prev = $("en-prev"), next = $("en-next");
    if (prev) prev.onclick = () => {
      E.offset = Math.min(24, E.offset + 1);
      E.at = 0; E.polls = 0;
      render(); load(true);
    };
    if (next) next.onclick = () => {
      if (E.offset <= 0) return;
      E.offset -= 1;
      E.at = 0; E.polls = 0;
      render(); load(true);
    };
  }

  function safe(fn) {
    try { fn(); } catch (e) { console.warn("[energy]", e); }
  }

  window.Energy = {
    init() {
      safe(() => {
        /* 首屏固定取"当前月 / offset=0"：顶部三格方块的今日、本月只有这一档
           回包能一次给全。它仍然是原来那一次 /api/energy 请求，没有新增。
           卡片的周/月切换照常可用，只是每次启动从本月开始。 */
        E.range = "month";
        E.offset = 0;
        loadTariff();
        wire();
        wireBill();
      });
      safe(paintPower);
      safe(() => load(true));
    },
    update() { safe(render); },
    refresh() { safe(() => load(true)); },
    // 主页面拿到充电器功率后立刻重画功率卡，不用等下面那个 4 秒定时器
    paintPower() { safe(paintPower); },
  };

  // 当前功率是实时的，顶部三格每 4 秒跟一次
  setInterval(() => {
    const card = $("power-card");
    if (!card || document.hidden || !card.offsetParent) return;
    safe(paintPower);
    const sec = $("energy-section");
    if (sec && !sec.classList.contains("hidden") && sec.offsetParent) {
      safe(paintNumbers);
    }
  }, 4000);

  // 云端统计隔 5 分钟拉一次
  setInterval(() => {
    const sec = $("energy-section");
    if (!sec || document.hidden || !sec.offsetParent) return;
    if (E.offset !== 0) return;
    if (Date.now() - E.at < RELOAD || E.busy) return;
    load(false);
  }, 60000);

  document.addEventListener("DOMContentLoaded", () => {
    window.Energy.init();
    const wrap = $("en-chart-wrap");
    if (wrap && window.ResizeObserver) {
      let lastW = -1, lastH = -1;
      new ResizeObserver((ents) => {
        // 尺寸没变就不重画：防止 draw 改 viewBox → 容器变高 → 再回调的死循环
        const r = ents[0].contentRect;
        if (Math.abs(r.width - lastW) < 1 && Math.abs(r.height - lastH) < 1) return;
        lastW = r.width; lastH = r.height;
        const sec = $("energy-section");
        if (!sec || sec.classList.contains("hidden") || !sec.offsetParent) return;
        safe(drawChart);
      }).observe(wrap);
    }
  });
})();
