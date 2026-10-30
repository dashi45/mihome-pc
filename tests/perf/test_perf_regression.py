# -*- coding: utf-8 -*-
"""界面流畅度的回归守卫（纯文本断言，不启动程序、不联网）

为什么需要它：
  这类问题**不会报错**，只表现为"用起来卡"。一旦有人把 height/width 动画、
  整块 innerHTML 重建、首屏同步加载 414KB 库这些写法改回去，肉眼很难立刻
  发现，但用户体感会立刻变差。所以把"已经验证过的写法"钉在源码层面。

实测依据（真实 WebView2 窗口，CDP 量 Performance.getMetrics）：
  改动前：空闲 41 次布局/秒 · 41 次样式重算/秒 · 主线程 5.3%
  改动后：空闲 1.5~2.3 次/秒 · 9~12 次/秒 · 主线程 0.8~0.9%
  归因方法：逐个 clearInterval 停用，停掉本机状态（pcstat）那个 2 秒轮询后
           布局直接归零 —— 其余 11 个定时器都不是主因。

跑法：.venv\\Scripts\\python.exe -u tests/perf_guard_test.py
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
    """取出某个选择器的完整规则体（不做完整 CSS 解析，够用就好）。"""
    i = css.find(selector + "{")
    if i < 0:
        return ""
    j = css.find("}", i)
    return css[i:j + 1] if j > 0 else ""


CSS = read("static/style.css")
PC = read("static/pcstat.js")
APP = read("static/app.js")
HTML = read("static/index.html")

# ---------------------------------------------------------------- 1
print("\n=== 1. 每核条 / 进度条：不许用触发布局的属性做动画 ===")
core = rule(CSS, ".pc-core i")
ok("scaleY" in core, "每核条用 transform:scaleY 画高度",
   "没找到 scaleY，可能被改回 height")
ok(not re.search(r"transition:\s*height", core), "每核条不再过渡 height")
ok("transform-origin:bottom" in core.replace(" ", ""),
   "每核条 scaleY 的原点在底部")

track = rule(CSS, ".pc-bar-row .track i")
ok("scaleX" in track, "进度条用 transform:scaleX 画宽度")
ok(not re.search(r"transition:\s*width", track), "进度条不再过渡 width")
ok("transform-origin:left" in track.replace(" ", ""),
   "进度条 scaleX 的原点在左侧")

ok("style.height" not in PC, "pcstat.js 里没有直接写 style.height",
   "写 height 会强制同步布局")
ok("style.width" not in PC, "pcstat.js 里没有直接写 style.width")

# ---------------------------------------------------------------- 2
def fn_body(src, name):
    """截出 IIFE 里某个函数的函数体（从 function 名往后到下一个同级函数）。"""
    i = src.find("function " + name + "(")
    if i < 0:
        return ""
    j = src.find("\n  function ", i + 1)
    return src[i:j if j > 0 else len(src)]


print("\n=== 2. 每 2 秒那一轮：不许整块重建 DOM ===")
# 关键不是"全程只有几处 innerHTML"（一次性初始化、气泡、日志都合理），
# 而是**每 2 秒都会跑的那几个绘制函数**里不许整块重建。
pb = fn_body(PC, "paintBars")
ok("box.innerHTML" not in pb,
   "paintBars 不重建行结构（只局部更新数值节点）",
   "内存/磁盘/显存三行每 2 秒都在整体重建")
pc = fn_body(PC, "paintCores")
ok("children.length !== per.length" in pc and "box.innerHTML" in pc,
   "paintCores 只在核数变化的分支里重建")
ok("__k" in pc, "每核条只在数值变化时才写 transform")
ok("innerHTML" not in fn_body(PC, "paintSpark"),
   "曲线只改 path 的 d，不重建 SVG")
ok("__sig" in PC, "tile 区用形态签名控制重建（结构不变就不重建）")
ok("__h" in PC, "进程行 / 底栏用内容签名跳过无变化的写入")
ok("__k" in PC, "每核条只在数值变化时才写 transform")

# --pc-c 被大量 var() 引用，改一次 = 整棵子树样式重算
ok(re.search(r"__c\s*!==\s*color", PC) is not None,
   "CSS 变量 --pc-c 只在颜色真变了才写",
   "否则每轮都触发整卡样式重算")

ok("requestAnimationFrame" in PC.split("function paint()")[1][:400]
   if "function paint()" in PC else False,
   "paint() 用 rAF 合并，同一次响应只落一次绘制")

# ---------------------------------------------------------------- 3
print("\n=== 3. 首屏不许同步加载摄像头解码库 ===")
ok("hls.min.js" not in HTML,
   "index.html 里没有 hls.min.js 的同步 script 标签",
   "它 414KB，不开摄像头时完全用不到")
ok("function ensureHls" in APP, "app.js 提供 ensureHls 按需加载")
ok("await ensureHls()" in APP, "startCam 真播之前会先等库就绪")
ok("hlsLoading" in APP, "并发的两次加载会复用同一个 Promise")

# ---------------------------------------------------------------- 4
print("\n=== 4. 入场动画只播首次，切房间不再错峰淡入 ===")
sect = rule(CSS, ".sect")
ok("animation:" not in sect, ".sect 基础规则不再带 animation")
ok("body.booting .sect" in CSS, "入场动画被限定在 body.booting 下")
card = rule(CSS, ".device-card")
ok("animation:" not in card, ".device-card 基础规则不再带 animation")
ok("body.booting .device-card" in CSS, "卡片入场同样只在首屏播")
ok("state.entered" in APP, "app.js 记得自己已经进过场")
ok('class="booting"' in HTML, "<body> 默认带 booting 类")

# ---------------------------------------------------------------- 5
print("\n=== 5. 别把已知的好东西改回去 ===")
ok("contain:layout paint style" in CSS,
   "卡片仍有 contain，重排不外溢到兄弟节点")
ok("backdrop-filter" in CSS and "滚动时不再用 backdrop-filter" in CSS,
   "滚动区仍然没有 backdrop-filter")
# 上一轮的成果：充电器写通道、功率合并
ok('methods=["GET", "POST"]' in read("routes/charger.py")
   or "methods=['GET', 'POST']" in read("routes/charger.py"),
   "充电器代理路由仍显式放行 POST")
ok("charger_w" in APP, "总功率仍包含充电器功率")

print("\n" + "=" * 46)
print("通过 %d / 失败 %d" % (PASS, FAIL))
if FAIL:
    print("❌ 有失败项")
    sys.exit(1)
print("✅ 全部通过")
