/* ==========================================================
   室外 · 天气 / 空气质量 / 室内外对比
   ----------------------------------------------------------
   数据来自 /api/weather（后端 weather.py，Open-Meteo，不需要 Key）。

   这一块是"不只米家"的第一层：室内传感器只管眼前这间屋子，
   室外才知道今天该不该开窗、要不要带伞、被子该不该晒。
   第二层是 /api/ext/push，任何能发 HTTP 的设备都能进来
   （见环境卡右上角的插头图标）。
   ========================================================== */
(function () {
  "use strict";

  const S = {
    data: null,
    busy: false,
    at: 0,
    err: "",
    indoor: null,      // {temp, hum, rank, room, n} —— 由 climate.js 广播
  };

  const $ = id => document.getElementById(id);
  const pad2 = n => String(n).padStart(2, "0");
  const hm = ts => {
    const d = new Date(ts * 1000);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  };
  const rnd = v => (v == null ? "—" : String(Math.round(v)));

  /* ---------------- 图标 ---------------- */
  /* 统一 24×24、线宽 1.7，和侧栏/卡片里的图标是同一套语言 */
  const P = {
    sun: '<circle cx="12" cy="12" r="4.1"/><path d="M12 2.7v2.1M12 19.2v2.1M2.7 12h2.1M19.2 12h2.1M5.5 5.5l1.5 1.5M17 17l1.5 1.5M18.5 5.5L17 7M7 17l-1.5 1.5"/>',
    moon: '<path d="M20.2 14.4A8.5 8.5 0 0 1 9.6 3.8a8.6 8.6 0 1 0 10.6 10.6z"/>',
    "sun-cloud":
      '<circle cx="8.4" cy="8" r="2.7"/><path d="M8.4 2.9v1.3M2.9 8h1.3M4.6 4.2l.9.9M12.2 4.2l-.9.9"/>' +
      '<path d="M8.7 19.4h8.1a3.1 3.1 0 0 0 .2-6.2 4.6 4.6 0 0 0-8.6-1.1 3.1 3.1 0 0 0 .3 7.3Z"/>',
    "moon-cloud":
      '<path d="M12.6 7.2A4.6 4.6 0 0 1 7.1 2.4a4.7 4.7 0 1 0 5.5 4.8z"/>' +
      '<path d="M8.7 19.6h8.1a3.1 3.1 0 0 0 .2-6.2 4.6 4.6 0 0 0-8.6-1.1 3.1 3.1 0 0 0 .3 7.3Z"/>',
    cloud: '<path d="M7.2 18.6h9.5a3.6 3.6 0 0 0 .3-7.2A5.2 5.2 0 0 0 7 10.4a3.6 3.6 0 0 0 .2 8.2Z"/>',
    fog:
      '<path d="M7.2 15.4h9.5a3.4 3.4 0 0 0 .3-6.8A5 5 0 0 0 7 8.6a3.4 3.4 0 0 0 .2 6.8Z"/>' +
      '<path d="M5.6 19h11.2M7.6 21.6h8.6"/>',
    drizzle:
      '<path d="M7.2 16h9.5a3.5 3.5 0 0 0 .3-7A5.1 5.1 0 0 0 7 9.2 3.5 3.5 0 0 0 7.2 16Z"/>' +
      '<path d="M9 18.6l-.8 1.8M13 18.6l-.8 1.8"/>',
    rain:
      '<path d="M7.2 15.4h9.5a3.5 3.5 0 0 0 .3-7A5.1 5.1 0 0 0 7 8.6a3.5 3.5 0 0 0 .2 6.8Z"/>' +
      '<path d="M8.8 18l-1.2 2.6M12.4 18l-1.2 2.6M16 18l-1.2 2.6"/>',
    "rain-2":
      '<path d="M7.2 14.6h9.5a3.5 3.5 0 0 0 .3-7A5.1 5.1 0 0 0 7 7.8a3.5 3.5 0 0 0 .2 6.8Z"/>' +
      '<path d="M7.8 17.2l-1.5 3.2M11.2 17.2l-1.5 3.2M14.6 17.2l-1.5 3.2M18 17.2l-1.5 3.2"/>',
    snow:
      '<path d="M7.2 15.4h9.5a3.5 3.5 0 0 0 .3-7A5.1 5.1 0 0 0 7 8.6a3.5 3.5 0 0 0 .2 6.8Z"/>' +
      '<circle cx="9" cy="19.4" r="1"/><circle cx="12.6" cy="20.6" r="1"/><circle cx="15.8" cy="19.4" r="1"/>',
    "snow-2":
      '<path d="M7.2 14.6h9.5a3.5 3.5 0 0 0 .3-7A5.1 5.1 0 0 0 7 7.8a3.5 3.5 0 0 0 .2 6.8Z"/>' +
      '<circle cx="8.2" cy="18.2" r="1"/><circle cx="12" cy="19.6" r="1"/><circle cx="15.8" cy="18.2" r="1"/>' +
      '<circle cx="10.2" cy="21.6" r="1"/><circle cx="13.8" cy="21.6" r="1"/>',
    sleet:
      '<path d="M7.2 15h9.5a3.5 3.5 0 0 0 .3-7A5.1 5.1 0 0 0 7 8.2a3.5 3.5 0 0 0 .2 6.8Z"/>' +
      '<path d="M10 17.6l-1.2 2.6"/><circle cx="14.4" cy="19.4" r="1"/>',
    storm:
      '<path d="M7.2 14.6h9.5a3.5 3.5 0 0 0 .3-7A5.1 5.1 0 0 0 7 7.8a3.5 3.5 0 0 0 .2 6.8Z"/>' +
      '<path d="M12.8 16.6l-2.6 3.4h2.2l-.7 2.2"/>',
  };

  function icon(name, size) {
    const body = P[name] || P.cloud;
    return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none"
      stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
      stroke-linejoin="round">${body}</svg>`;
  }

  /* ---------------- 取数 ---------------- */
  function load(force) {
    if (S.busy) return;
    S.busy = true;
    fetch("/api/weather" + (force ? "?force=1" : ""))
      .then(r => r.json())
      .then(j => {
        if (j && j.ok) {
          S.data = j;
          S.at = Date.now();
          S.err = j.error || "";
        } else {
          S.err = (j && j.error) || "天气获取失败";
        }
      })
      .catch(e => { S.err = "天气获取失败：" + (e.message || e); })
      .finally(() => { S.busy = false; paint(); });
  }

  /* ---------------- 现在 ---------------- */
  function renderNow(d) {
    const now = d.now || {};
    const g = $("wx-glyph");
    if (g) {
      g.className = "wx-glyph" + (now.icon === "sun" ? " is-sun" : "");
      g.innerHTML = icon(now.icon || "cloud", 36);
    }
    const t = $("wx-t");
    if (t) t.textContent = now.t == null ? "—" : String(Math.round(now.t));
    const desc = $("wx-desc");
    if (desc) {
      const feels = now.feels == null ? "" : `体感 ${Math.round(now.feels)}°`;
      desc.innerHTML = [now.text, feels].filter(Boolean)
        .map(x => `<span>${x}</span>`).join('<i>·</i>');
    }
  }

  /* ---------------- 顶栏里的天气一条 ---------------- */
  /* 宽屏不再把整张天气卡放在页面流里：顶栏只留温度/天气/城市/今日高低温，
     点一下才把原来的完整卡当弹层打开。窄屏顶栏不显示它，卡片照旧在流里。 */
  function paintMini(d) {
    const mini = $("wx-mini");
    if (!mini) return;
    const now = d.now || {}, td = d.today || {};
    const g = $("wx-mini-glyph");
    if (g) g.innerHTML = icon(now.icon || "cloud", 22);
    const t = $("wx-mini-t");
    if (t) t.textContent = now.t == null ? "—" : String(Math.round(now.t));
    const desc = $("wx-mini-desc");
    if (desc) desc.textContent = now.text || "天气";
    const place = $("wx-mini-place");
    if (place) place.textContent = (d.place || "定位中").split(" · ")[0];
    const hl = $("wx-mini-hl");
    if (hl) {
      hl.textContent = (td.lo == null || td.hi == null) ? "—"
        : `${Math.round(td.lo)}° ~ ${Math.round(td.hi)}°`;
    }
    mini.classList.toggle("wx-night", now.is_day === false);
    mini.title = [now.text,
      td.hi != null ? `今日 ${Math.round(td.lo)}° ~ ${Math.round(td.hi)}°` : "",
      "点击看天气详情"].filter(Boolean).join(" · ");
  }

  function wireMini() {
    const mini = $("wx-mini"), card = $("wx-card"), mask = $("wx-mask");
    if (!mini || !card || !mask) return;
    const close = () => {
      card.classList.remove("show");
      mask.classList.add("hidden");
      mask.classList.remove("show");
      mini.setAttribute("aria-expanded", "false");
    };
    const open = () => {
      card.classList.add("show");
      card.scrollTop = 0;
      mask.classList.remove("hidden");
      mask.classList.add("show");
      mini.setAttribute("aria-expanded", "true");
    };
    mini.setAttribute("aria-expanded", "false");
    mini.addEventListener("click", () =>
      card.classList.contains("show") ? close() : open());
    mask.addEventListener("click", close);
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && card.classList.contains("show")) close();
    });
    // 缩到单栏时弹层没有意义（窄屏本来就把完整天气卡放回页面流里）
    window.addEventListener("resize", () => {
      if (window.innerWidth < 1200) close();
    });
  }

  /* ---------------- 未来 24 小时（曲线） ---------------- */
  /* 24 个点不适合摊成一排卡片（会把顶部塞爆），一条曲线 + 四个时间刻度信息量更大。
     和 climate.js 用同一套 Catmull-Rom → 三次贝塞尔，保证两个图表的"手感"一致。 */
  function smooth(pts) {
    if (pts.length < 2) return "";
    let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1],
        p3 = pts[i + 2] || p2;
      const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
      const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ` +
        `${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    }
    return d;
  }

  function renderCurve(d) {
    const svg = $("wx-curve"), line = $("wx-curve-line"),
      area = $("wx-curve-area"), dot = $("wx-curve-dot");
    if (!line) return;
    const list = (d.hourly || []).slice(0, 24).filter(h => h.v != null);
    if (list.length < 2) { line.setAttribute("d", ""); area.setAttribute("d", ""); return; }

    const vs = list.map(h => h.v);
    const lo = Math.min(...vs), hi = Math.max(...vs);
    const span = Math.max(2, hi - lo);
    const W = 480, H = 52, P = 7;
    const x = i => (i / (list.length - 1)) * W;
    const y = v => H - P - ((v - lo) / span) * (H - P * 2);

    const pts = list.map((h, i) => [x(i), y(h.v)]);
    const path = smooth(pts);
    line.setAttribute("d", path);
    area.setAttribute("d", `${path} L${W},${H} L0,${H} Z`);
    if (dot) { dot.setAttribute("cx", 0.5); dot.setAttribute("cy", y(list[0].v).toFixed(1)); }
    if (svg) svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

    const hl = $("wx-hl");
    if (hl) hl.innerHTML = `最高 <b>${Math.round(hi)}°</b> 最低 <b>${Math.round(lo)}°</b>`;

    const ticks = $("wx-ticks");
    if (ticks) {
      ticks.innerHTML = [0, 6, 12, 18].filter(i => i < list.length).map(i => {
        const hh = new Date(list[i].t * 1000).getHours();
        return `<span style="left:${((i / (list.length - 1)) * 100).toFixed(1)}%">${
          i === 0 ? "现在" : pad2(hh) + "时"}</span>`;
      }).join("");
    }
  }

  /* ---------------- 未来 7 天（一列一天） ---------------- */
  function renderDays(d) {
    const box = $("wx-days");
    if (!box) return;
    const list = d.daily || [];
    if (!list.length) { box.innerHTML = ""; return; }
    let lo = Infinity, hi = -Infinity;
    for (const x of list) {
      if (x.lo != null) lo = Math.min(lo, x.lo);
      if (x.hi != null) hi = Math.max(hi, x.hi);
    }
    const span = Math.max(1, hi - lo);
    const DOW = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    box.innerHTML = list.slice(0, 7).map((x, i) => {
      const name = i === 0 ? "今天" : (i === 1 ? "明天" : DOW[x.dow]);
      // 一周尺度上的温度区间条：谁更热，扫一眼就知道
      const l = ((x.lo - lo) / span) * 100;
      const w = Math.max(10, ((x.hi - x.lo) / span) * 100);
      return `<div class="wx-d${i === 0 ? " today" : ""}" title="${name} ${rnd(x.lo)}° ~ ${rnd(x.hi)}°${
        x.pop >= 20 ? " · 降水 " + x.pop + "%" : ""}">
        <span class="wx-d-n">${name}</span>
        <span class="wx-d-i">${icon(x.icon, 16)}</span>
        <span class="wx-d-h">${rnd(x.hi)}°</span>
        <span class="wx-d-bar"><i style="left:${l.toFixed(1)}%;width:${w.toFixed(1)}%"></i></span>
        <span class="wx-d-l">${rnd(x.lo)}°${x.pop >= 20 ? `<em>${x.pop}%</em>` : ""}</span>
      </div>`;
    }).join("");
  }

  /* ---------------- 指标条 ---------------- */
  function renderFacts(d) {
    const box = $("wx-facts");
    if (!box) return;
    const now = d.now || {}, td = d.today || {}, a = d.aqi || {};
    const items = [];

    // 室内外对比放在最前面：这条信息栏在页面顶部，先回答"屋里屋外差多少"
    const ind = S.indoor || {};
    if (ind.temp != null && now.t != null) {
      const dt = ind.temp - now.t;
      const sign = Math.abs(dt) < 0.5 ? "≈" : (dt > 0 ? "+" : "−");
      items.push(`<span class="wx-f wx-cmp"><em>室内</em><b>${Math.round(ind.temp)}°</b>
        <i class="${Math.abs(dt) < 0.5 ? "" : dt > 0 ? "up" : "down"}">${sign}${
        Math.abs(dt) < 0.5 ? "同温" : Math.abs(dt).toFixed(1) + "°"}</i></span>`);
    } else if (ind.temp != null) {
      items.push(`<span class="wx-f wx-cmp"><em>室内</em><b>${Math.round(ind.temp)}°</b></span>`);
    }
    if (td.hi != null)
      items.push(`<span class="wx-f"><em>今日</em><b>${Math.round(td.lo)}°</b><em>~</em><b>${Math.round(td.hi)}°</b></span>`);
    if (now.hum != null)
      items.push(`<span class="wx-f"><em>湿度</em><b>${Math.round(now.hum)}%</b></span>`);
    if (now.wind != null)
      items.push(`<span class="wx-f"><em>风</em><b>${now.wind_dir || ""} ${Math.round(now.wind)}</b><em>km/h</em></span>`);
    if (a.us != null)
      items.push(`<span class="wx-f wx-aqi" style="--aqi:${a.color || "#8A8F98"}">
        <em>AQI</em><b>${Math.round(a.us)}</b><em>${a.lv || ""}</em></span>`);
    if (td.sunrise && td.sunset) {
      items.push(`<span class="wx-f"><em>日出</em><b>${hm(new Date(td.sunrise).getTime() / 1000)}</b></span>`);
      items.push(`<span class="wx-f"><em>日落</em><b>${hm(new Date(td.sunset).getTime() / 1000)}</b></span>`);
    }
    if (td.uv != null)
      items.push(`<span class="wx-f"><em>紫外线</em><b>${Math.round(td.uv)}</b></span>`);
    if (now.pressure != null)
      items.push(`<span class="wx-f"><em>气压</em><b>${Math.round(now.pressure)}</b><em>hPa</em></span>`);
    box.innerHTML = items.join("");
  }

  /* ---------------- 开窗建议 ---------------- */
  /* 把室内读数和室外对比，给一句能直接照做的话。
     判断顺序是有讲究的：空气差 / 下雨 这类"硬否决"先判，
     否则温差再合适也不该开窗。 */
  function advice(d) {
    const out = d.now || {};
    const ind = S.indoor || {};
    const aqi = (d.aqi || {}).us;
    const code = out.code == null ? 0 : out.code;
    const raining = (code >= 51 && code <= 82) || (code >= 95) || (out.precip || 0) > 0.2;

    if (aqi != null && aqi >= 150) {
      return { lv: "bad", text: `室外空气差（AQI ${Math.round(aqi)}），先关窗，开净化器` };
    }
    if (raining) {
      return { lv: "bad", text: "外面在下雨，关好窗，湿度会跟着往上走" };
    }
    if (ind.temp == null || out.t == null) {
      return {
        lv: "mute",
        text: out.t == null ? "暂时拿不到室外温度"
          : `室外 ${Math.round(out.t)}°，回到环境页可以看室内外温差`,
      };
    }
    const dt = ind.temp - out.t;
    const dh = (ind.hum == null || out.hum == null) ? null : ind.hum - out.hum;
    const outT = Math.round(out.t), inT = Math.round(ind.temp);

    if (dh != null && ind.hum > 70 && dh > 8) {
      return { lv: "good", text: `室内偏潮（${Math.round(ind.hum)}%），室外 ${outT}° 干爽，开窗半小时除湿` };
    }
    if (dh != null && ind.hum < 32 && dh < -10) {
      return { lv: "good", text: `室内偏干（${Math.round(ind.hum)}%），室外湿度 ${Math.round(out.hum)}%，可以开窗换换气` };
    }
    if (Math.abs(dt) <= 1.5) {
      return { lv: "mute", text: `室内外几乎一样（${inT}° / ${outT}°），开不开窗差别不大` };
    }
    if (dt > 0) {
      const cool = outT >= 12 && outT <= 26;
      return {
        lv: cool ? "good" : "mute",
        text: cool
          ? `室外比室内低 ${Math.round(dt)}°（${outT}°），开窗通风 20 分钟能明显降温`
          : `室外 ${outT}°，比室内低 ${Math.round(dt)}°，但外面也不凉快，开窗效果有限`,
      };
    }
    return {
      lv: outT <= 30 ? "good" : "bad",
      text: outT <= 30
        ? `室外比室内高 ${Math.round(-dt)}°（${outT}°），晒晒被子正好，人不在可以关窗保温`
        : `室外 ${outT}° 比室内还热，关窗开空调更省电`,
    };
  }

  function renderTip(d) {
    const box = $("wx-tip");
    if (!box) return;
    const a = advice(d);
    box.className = "wx-tip lv-" + a.lv;
    box.innerHTML = `<span class="wx-tip-ic">${icon(
      a.lv === "bad" ? "rain" : a.lv === "good" ? "sun-cloud" : "sun", 15)}</span>
      <span>${a.text}</span>`;
  }

  /* ---------------- 主绘制 ---------------- */
  function paint() {
    const card = $("wx-card");
    if (!card) return;
    const d = S.data;
    if (!d) {
      const sub = $("wx-sub");
      if (sub) sub.textContent = S.err || "正在获取天气…";
      return;
    }

    const place = $("wx-place-t");
    if (place) place.textContent = (d.place || "定位中").split(" · ")[0];

    const sub = $("wx-sub");
    if (sub) {
      const t = d.updated ? new Date(d.updated * 1000) : new Date();
      sub.textContent = `${pad2(t.getHours())}:${pad2(t.getMinutes())} 更新`
        + (d.stale ? " · 离线缓存" : "");
    }

    renderNow(d);
    renderCurve(d);
    renderDays(d);
    renderFacts(d);
    renderTip(d);
    paintMini(d);
    applyAutoTheme(d);

    // 每小时温差对照：给图表区域外的一条小提示（室内 vs 室外）
    card.classList.toggle("wx-night", !(d.now || {}).is_day);
  }

  /* ---------------- 跟随日照切主题 ---------------- */
  /* 自己加的一个小功能：日落之后界面自己变深色，日出之后自己变回来。
     默认关着 —— 主题是用户的偏好，不该被悄悄改掉。 */
  function autoOn() {
    try { return localStorage.getItem("mi-theme-auto") === "1"; } catch (e) { return false; }
  }

  function paintAutoBtn() {
    const btn = $("wx-auto"), lab = $("wx-auto-t"), ic = $("wx-auto-ic");
    if (!btn) return;
    const on = autoOn();
    btn.classList.toggle("on", on);
    if (lab) lab.textContent = on ? "跟随日照" : "跟随日照";
    if (ic) ic.innerHTML = icon(autoIsDay() ? "sun" : "moon", 12);
  }

  function autoIsDay() {
    const d = S.data || {};
    const td = d.today || {};
    if (!td.sunrise || !td.sunset) return (d.now || {}).is_day !== false;
    const now = Date.now();
    return now >= new Date(td.sunrise).getTime() && now < new Date(td.sunset).getTime();
  }

  function applyAutoTheme(d) {
    paintAutoBtn();
    if (!autoOn() || !d) return;
    const want = autoIsDay() ? "light" : "dark";
    if (document.documentElement.dataset.theme === want) return;
    document.documentElement.dataset.theme = want;
    try { localStorage.setItem("mi-theme", want); } catch (e) {}
    if (window.paintThemeBtn) window.paintThemeBtn();
  }

  function wireAuto() {
    const btn = $("wx-auto");
    if (!btn) return;
    btn.onclick = () => {
      const on = !autoOn();
      try { localStorage.setItem("mi-theme-auto", on ? "1" : "0"); } catch (e) {}
      paintAutoBtn();
      if (on) {
        const want = autoIsDay() ? "light" : "dark";
        document.documentElement.dataset.theme = want;
        try { localStorage.setItem("mi-theme", want); } catch (e) {}
        if (window.paintThemeBtn) window.paintThemeBtn();
      }
    };
    // 每分钟校一次（跨过日出/日落那一刻就切）
    setInterval(() => { if (!document.hidden) applyAutoTheme(S.data); }, 60000);
  }

  /* ---------------- 换城市 ---------------- */
  function wireCity() {
    const btn = $("wx-place"), pop = $("wx-city-pop"), inp = $("wx-city-input");
    if (!btn || !pop || !inp) return;

    const close = () => pop.classList.add("hidden");
    btn.onclick = ev => {
      ev.stopPropagation();
      pop.classList.toggle("hidden");
      if (!pop.classList.contains("hidden")) { inp.focus(); inp.select(); }
    };
    pop.onclick = ev => ev.stopPropagation();
    document.addEventListener("click", close);

    const go = () => {
      const city = inp.value.trim();
      if (!city) return;
      const sub = $("wx-sub");
      if (sub) sub.textContent = "正在查 " + city + "…";
      fetch("/api/weather/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ city }),
      })
        .then(r => r.json())
        .then(j => {
          if (j && j.ok) {
            S.data = j.weather || S.data;
            S.at = Date.now();
            close();
            inp.value = "";
            paint();
          } else {
            if (sub) sub.textContent = (j && j.error) || "没找到这个城市";
          }
        })
        .catch(e => { const s = $("wx-sub"); if (s) s.textContent = "换城市失败：" + (e.message || e); });
    };
    const sure = $("wx-city-go");
    if (sure) sure.onclick = go;
    inp.onkeydown = ev => {
      if (ev.key === "Enter") go();
      if (ev.key === "Escape") close();
    };
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    wireCity();
    wireAuto();
    wireMini();
    paint();
    load(false);

    // 天气 10 分钟一档，5 分钟拉一次足够（后端还有 10 分钟缓存）
    setInterval(() => {
      if (document.hidden) return;
      if (Date.now() - S.at < 300000) return;
      load(false);
    }, 60000);

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && Date.now() - S.at > 600000) load(false);
    });
  }

  window.Weather = {
    init,
    paint,
    reload(force) { load(!!force); },
    snapshot() { return S.data; },
  };

  // 室内读数变化 -> 重算开窗建议和室内外温差（只重绘这一块，不动别的）
  window.addEventListener("mi:climate", ev => {
    S.indoor = (ev && ev.detail) || null;
    if (S.data) { renderTip(S.data); renderFacts(S.data); }
  });

  document.addEventListener("DOMContentLoaded", init);
})();
