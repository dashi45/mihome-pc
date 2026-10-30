# -*- coding: utf-8 -*-
"""临时：量页面滚动流畅度与空闲开销。

滚动用 JS 逐帧推进 scrollTop 并数 rAF 回调 —— 如果主线程被样式重算/布局
堵住，帧间隔会超过 16.7ms，差值就是掉帧。
空闲段量 12 秒内的布局/样式重算次数，反映后台轮询的代价。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
from cdp import connect, eval_js, metrics  # noqa: E402

SCROLL_JS = r"""
new Promise(function(res){
  var el = document.getElementById('scroller');
  el.scrollTop = 0;
  var frames = 0, start = 0, worst = 0, prev = 0, gaps = [];
  function step(ts){
    if (!start) { start = ts; prev = ts; }
    frames++;
    var dt = ts - prev; prev = ts;
    if (frames > 2) { gaps.push(dt); if (dt > worst) worst = dt; }
    el.scrollTop = el.scrollTop + 26;
    if (ts - start < 2500) requestAnimationFrame(step);
    else {
      gaps.sort(function(a,b){return b-a;});
      res(JSON.stringify({
        frames: frames,
        fps: Math.round(frames / ((ts - start) / 1000) * 10) / 10,
        worstGapMs: Math.round(worst * 10) / 10,
        over16: gaps.filter(function(g){ return g > 16.7; }).length,
        over33: gaps.filter(function(g){ return g > 33; }).length,
        scrolled: Math.round(el.scrollTop)
      }));
    }
  }
  requestAnimationFrame(step);
})
"""


def diff(a, b, keys):
    return {k: round(b.get(k, 0) - a.get(k, 0), 3) for k in keys if k in b}


def main():
    w = connect()
    w.call("Performance.enable")

    # --- 滚动段 ---
    before = metrics(w)
    res = json.loads(eval_js(w, SCROLL_JS))
    after = metrics(w)
    print("== 滚动 2.5 秒（每次 +26px，共 %d px）==" % res["scrolled"])
    print("  帧数 %d / 平均 %.1f fps / 最差帧间隔 %.1f ms" %
          (res["frames"], res["fps"], res["worstGapMs"]))
    print("  超过 16.7ms 的帧: %d 个；超过 33ms 的帧: %d 个" %
          (res["over16"], res["over33"]))
    d = diff(before, after, ["LayoutCount", "RecalcStyleCount", "LayoutDuration",
                             "RecalcStyleDuration", "TaskDuration", "ScriptDuration"])
    print("  滚动期间: 布局 %d 次 / %.0f ms，样式重算 %d 次 / %.0f ms" %
          (d["LayoutCount"], d["LayoutDuration"] * 1000,
           d["RecalcStyleCount"], d["RecalcStyleDuration"] * 1000))
    print("  主线程任务 %.0f ms，脚本 %.0f ms" %
          (d["TaskDuration"] * 1000, d["ScriptDuration"] * 1000))

    # --- 空闲段 ---
    eval_js(w, "document.getElementById('scroller').scrollTop = 0")
    b2 = metrics(w)
    time.sleep(12)
    a2 = metrics(w)
    d2 = diff(b2, a2, ["LayoutCount", "RecalcStyleCount", "LayoutDuration",
                       "RecalcStyleDuration", "TaskDuration", "ScriptDuration"])
    print("\n== 空闲 12 秒（含 6 轮本机刷新 / 2 轮环境刷新）==")
    print("  布局 %d 次 / %.0f ms；样式重算 %d 次 / %.0f ms" %
          (d2["LayoutCount"], d2["LayoutDuration"] * 1000,
           d2["RecalcStyleCount"], d2["RecalcStyleDuration"] * 1000))
    print("  主线程任务合计 %.0f ms（占 12 秒的 %.1f%%）" %
          (d2["TaskDuration"] * 1000, d2["TaskDuration"] / 12 * 100))


if __name__ == "__main__":
    main()
