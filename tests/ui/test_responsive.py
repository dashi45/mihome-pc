# -*- coding: utf-8 -*-
"""窗口尺寸 / 分辨率的回归守卫（纯文本断言，不启动程序、不联网）

为什么需要它：
  窗口拉小以后"图形崩了"是个不会报错的 bug —— 页面照样能用，只是卡片被顶到
  2000px 高、曲线和柱状图被拉成一条扁线。根因是一个正反馈循环：

      draw() 用 wrap.clientHeight 当 viewBox 高度
        → SVG 是流内元素且 height:100%，容器高度 auto 时百分比失效
        → SVG 退回"按 viewBox 比例算固有高度" = 又把自己的高度算进容器
        → 容器变高 → ResizeObserver 回调 → draw() 又读到更大的高度 …
      实测后果：dash 行高 2293px，环境卡/能源卡各 2294px，SVG 883 高。

  修法是把这条链从三处同时切断（CSS 定高 + SVG 绝对定位 + JS 夹紧），
  任何一处被改回去都会重新长起来，所以钉在源码层面。

实测依据（真实 WebView2 窗口 1506x923 @125% DPI，CDP 量）：
  1106 CSS 宽：dash 1058（两列）  · 1520：629（三列） · 2560：629
  全宽度段 viewBox 与渲染尺寸 1:1，overflowX = 0

跑法：.venv\\Scripts\\python.exe -u tests/rwd_test.py
"""
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PASS = FAIL = 0


def ok(cond, msg, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ %s" % msg, flush=True)
    else:
        FAIL += 1
        print("  ❌ %s  %s" % (msg, extra), flush=True)


def read(rel):
    with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def rule(css, selector):
    """取某条规则的声明块（不处理嵌套媒体查询，够用）。"""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else ""


def block(css, head):
    """取媒体查询整块（支持嵌套一层花括号）。"""
    i = css.find(head)
    if i < 0:
        return ""
    j = css.find("{", i)
    depth, k = 0, j
    while k < len(css):
        if css[k] == "{":
            depth += 1
        elif css[k] == "}":
            depth -= 1
            if depth == 0:
                return css[i:k + 1]
        k += 1
    return css[i:]


CSS = read("static/style.css")
CLIMATE = read("static/climate.js")
ENERGY = read("static/energy.js")

print("=== 1. 图表容器：高度必须是确定值，不能吃 flex 剩余空间")
for sel in ("#env-section .cv-chart-wrap", "#energy-section .en-chart-wrap"):
    body = rule(CSS, sel)
    ok(bool(body), "%s 规则存在" % sel)
    ok("height:clamp(" in body or re.search(r"height:\s*\d+px", body),
       "%s 用确定高度（clamp/px），不是 auto" % sel,
       "拿到：%s" % body.strip()[:90])
    ok("height:auto" not in body,
       "%s 没有 height:auto（auto + flex:1 = 正反馈）" % sel)
    ok("flex:1" not in body and "flex:1 1 auto" not in body,
       "%s 不再用 flex:1 吃剩余空间" % sel,
       "拿到：%s" % body.strip()[:90])
    ok("min-height:0" in body,
       "%s min-height 归零（否则 min-height 会顶住 auto）" % sel)

print("\n=== 2. SVG 必须绝对定位，不能参与父容器高度计算")
for sel, js in (("#cv-chart", CLIMATE), ("#en-chart", ENERGY)):
    body = rule(CSS, sel)
    ok("position:absolute" in body,
       "%s 是 position:absolute" % sel, "拿到：%s" % body.strip()[:80])
    ok("inset:0" in body, "%s inset:0 铺满容器" % sel)

print("\n=== 3. draw() 里必须夹紧高度（第二道闸）")
ok(re.search(r"H\s*=\s*Math\.min\([^)]*clientHeight", CLIMATE),
   "climate.draw() 的 H 有上限夹紧", "直接拿 clientHeight 会在容器被撑高时无限放大")
ok(re.search(r"H\s*=\s*Math\.min\([^)]*clientHeight", ENERGY),
   "energy.drawChart() 的 H 有上限夹紧")

print("\n=== 4. ResizeObserver 必须只在尺寸真变化时重绘")
for name, js in (("climate", CLIMATE), ("energy", ENERGY)):
    seg = js[js.find("new ResizeObserver"):js.find("new ResizeObserver") + 700]
    ok("lastW" in seg and "lastH" in seg,
       "%s 的 ResizeObserver 记住上次尺寸" % name,
       "没有这道判断就是 RO loop：改 viewBox → 容器变高 → 又回调")
    ok("< 1" in seg or "<= 1" in seg,
       "%s 用亚像素阈值比较尺寸" % name)

print("\n=== 5. 响应式断点齐全（三档 + 侧栏收窄 + 极窄图标条）")
for bp in ("max-width:1360px", "max-width:1200px", "max-width:1080px",
           "max-width:860px", "max-width:760px"):
    ok(bp in CSS, "存在 %s 断点" % bp)
dash = rule(CSS, ".dash")
ok("grid-template-areas" in dash, "dash 用命名区域重排（不是靠顺序）")
ok(len(re.findall(r"grid-template-areas", CSS)) >= 3,
   "dash 至少三套布局（三列/两列/单列）",
   "实际 %d 套" % len(re.findall(r"grid-template-areas", CSS)))
narrow = block(CSS, "@media (max-width:860px)")
ok(".sidebar{width:58px" in narrow.replace(" ", "") or "width:58px" in narrow,
   "极窄档侧栏退成 58px 图标条", "拿到：%s" % narrow[:120])
ok(".room-item .nm,.room-item .rt,.room-item .cnt{display:none}".replace(" ", "") in
   narrow.replace(" ", ""),
   "极窄档隐藏房间名/温度/数量")
ok("font-size:0" in narrow,
   "极窄档用 font-size:0 隐藏按钮纯文本（按钮里没有 span 可 display:none）")

print("\n=== 6. 栅格不许用 minmax(min(...))")
# 实测 WebView2 下 auto-fill + min() 会让整条规则失效，退化成单列
for sel in (".grid", ".mini-grid"):
    body = rule(CSS, sel)
    ok("minmax(min(" not in body.replace(" ", ""),
       "%s 没用 minmax(min())" % sel,
       "实测这条写法在 WebView2 里算不出列数，1520 宽也只有 1 列")

print("\n=== 7. 主题按钮图标有尺寸（JS 注入的 svg 没有 width/height 属性）")
tio = rule(CSS, "#theme-ico svg")
ok("width:15px" in tio and "height:15px" in tio,
   "#theme-ico svg 显式给了 15x15", "不给尺寸就是 0x0，极窄档按钮变空白")

print("\n=== 8. 窗口最小尺寸与 CSS 断点要能对上")
LAUNCH = read("launcher.py")
m = re.search(r"min_size=\((\d+),\s*(\d+)\)", LAUNCH)
ok(bool(m), "launcher 仍设置 min_size")
if m:
    mw = int(m.group(1))
    ok(mw >= 1000, "最小宽度 %d 与三档布局兼容（不再允许拉到 600 以下）" % mw)

print("\n=== 9. 不许把图表的固定高度改回 auto + 百分比")
bad = [s for s in ("cv-chart-wrap", "en-chart-wrap")
       if re.search(re.escape(s) + r"[^{]*\{[^}]*height:100%", CSS)]
ok(not bad, "没有把图表容器设成 height:100%（百分比在 auto 父级下无效）",
   "命中：%s" % bad)
ok("#cv-chart{position:absolute" in CSS.replace(" ", "") or
   "position:absolute" in rule(CSS, "#cv-chart"),
   "#cv-chart 保持绝对定位")
ok("position:absolute" in rule(CSS, "#en-chart"),
   "#en-chart 保持绝对定位")

print("\n=== 10. 与本次改动相关的既有能力没被碰坏")
APP = read("static/app.js")
ok("methods=[\"GET\", \"POST\"]" in read(os.path.join("routes", "charger.py")) or
   "methods=['GET', 'POST']" in read(os.path.join("routes", "charger.py")),
   "充电器代理仍放行 POST")
ok("charger_w" in APP, "总功率仍含充电器")
ok("renderGrid" in APP, "设备网格仍由 renderGrid 渲染（断点只改 CSS，不改结构）")

print("\n" + "=" * 46)
print("通过 %d / 失败 %d" % (PASS, FAIL))
if FAIL:
    print("❌ 有失败项")
    sys.exit(1)
print("✅ 全部通过")
