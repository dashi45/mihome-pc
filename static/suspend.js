/* 后台挂起：窗口进托盘后，把所有周期任务统一掐掉。
 *
 * 为什么不逐个模块改判断：定时器分散在 8 个文件里，以后加新模块
 * 必然漏。在源头包一层 setInterval，回调先看挂起标志再执行 ——
 * 一次接管，以后所有人自动被管住，也不用改任何调用点。
 *
 * Python 侧（launcher）通过 evaluate_js 调 window.MiHomeSuspend(true/false)。
 * document.hidden 一并判断：普通最小化/切走时 pywebview 的事件可能
 * 不触发，浏览器自己的可见性是兜底。
 */
(function () {
  "use strict";

  if (window.__miSuspendInstalled) return;
  window.__miSuspendInstalled = true;

  const origSetInterval = window.setInterval;

  window.__miSuspended = false;

  window.setInterval = function (fn, ms) {
    if (typeof fn !== "function") {
      return origSetInterval.apply(window, arguments);
    }
    const wrapped = function () {
      if (window.__miSuspended || document.hidden) return;
      return fn.apply(this, arguments);
    };
    return origSetInterval.call(window, wrapped, ms);
  };

  /**
   * 挂起/恢复。app.js 会在 loaded 后把真正的停播/续播/刷新逻辑
   * 挂到 window.MiHome 的 onSuspend/onResume 上（那里能碰到模块作用域
   * 的摄像头状态，这里碰不到）。
   */
  window.MiHomeSuspend = function (on) {
    const next = !!on;
    if (window.__miSuspended === next) return;
    window.__miSuspended = next;
    const hook = window.MiHome;
    try {
      if (next && hook && hook.onSuspend) hook.onSuspend();
      if (!next && hook && hook.onResume) hook.onResume();
    } catch (e) { /* 挂起失败不能托盘收不进 */ }
  };
})();
