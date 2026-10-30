# -*- coding: utf-8 -*-
"""前端压缩：把 static/ 下的 js/css 用 esbuild 压成 min 版，并给出体积对比。

## 为什么要"按需启用"

这些文件是**本地**加载的（127.0.0.1，无网络往返、无缓存），81KB 的 js 解析
大概十几毫秒。压缩能省的是"解析时间"，省不出"等下载"。代价是排错难：
线上跑的是压缩后的代码，行号全没了。

所以默认只做"算一遍给你看"（dry run），真要切换加 `--apply`：

    python tools/build_frontend.py            # 只报告，不改文件
    python tools/build_frontend.py --apply    # 生成 static/min/ 并改写 index.html

回滚：`git checkout static/index.html` 即可，源文件一个都不动（只新增 static/min/）。

## 安全性

这批脚本不是 ES module，是共享全局的普通脚本。esbuild 对 script 格式只压
语法和局部变量，**不会重命名顶层全局**（否则 app.js 里的函数别的模块就调不到了）。
生成后会做一次校验：确认每个源文件里 `window.X = ` / `function X(` 的顶层名
在产物里仍然存在，不通过就报错不落盘。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
OUT = os.path.join(STATIC, "min")

# index.html 里引用的资源（hls.min.js 本来就是压缩版，且是懒加载，不处理）
TARGETS = [
    "suspend.js", "app.js", "dashdrag.js", "climate.js", "weather.js",
    "energy.js", "pcstat.js", "evt.js", "notify.js", "procs.js", "sched.js",
    "style.css",
]

ESBUILD_VER = "0.24.0"


def _esbuild():
    """优先用本地的 esbuild，其次用 npx 拉固定版本。

    Windows 上 npx 是 .cmd，CreateProcess 不能直接执行批处理，必须套一层
    cmd /c —— 直接传 ["npx", ...] 会得到 FileNotFoundError，看起来像"没装 Node"。
    """
    def _run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None

    local = shutil.which("esbuild")
    if local:
        return [local]

    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        return None
    base = ["cmd", "/c", npx] if npx.lower().endswith((".cmd", ".bat")) else [npx]
    cand = base + ["--yes", "esbuild@%s" % ESBUILD_VER]
    r = _run(cand + ["--version"])
    return cand if (r and r.returncode == 0) else None


def _globals_used(src):
    """粗提顶层全局名：`function foo(`、`window.foo =`、`const foo =`(顶层)。"""
    names = set()
    names |= set(re.findall(r"^function\s+([A-Za-z_$][\w$]*)", src, re.M))
    names |= set(re.findall(r"^window\.([A-Za-z_$][\w$]*)\s*=", src, re.M))
    names |= set(re.findall(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", src, re.M))
    return names


def build(apply_changes):
    tool = _esbuild()
    if not tool:
        print("✗ 找不到 esbuild（需要 Node.js / npx）")
        return 1
    # 先压到临时目录：dry run 不留任何东西，--apply 才落到 static/min/
    work = tempfile.mkdtemp(prefix="mihome-fe-")
    os.makedirs(OUT, exist_ok=True) if apply_changes else None

    rows, failed = [], []
    for name in TARGETS:
        src_path = os.path.join(STATIC, name)
        if not os.path.isfile(src_path):
            continue
        ext = "css" if name.endswith(".css") else "js"
        out_name = name.rsplit(".", 1)[0] + ".min." + ext
        out_path = os.path.join(OUT if apply_changes else work, out_name)
        cmd = tool + [src_path, "--minify", "--outfile=" + out_path,
                      "--log-level=error"]
        if ext == "css":
            cmd += ["--loader:.css=css"]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        if r.returncode != 0:
            failed.append((name, (r.stderr or "").strip().splitlines()[:2]))
            continue

        before = os.path.getsize(src_path)
        after = os.path.getsize(out_path)
        # 顶层全局不能被压掉，否则别的模块调不到
        if ext == "js":
            produced = open(out_path, encoding="utf-8", errors="replace").read()
            lost = [g for g in _globals_used(open(src_path, encoding="utf-8").read())
                    if g not in produced]
            if lost:
                failed.append((name, "顶层全局丢失: %s" % ", ".join(sorted(lost)[:5])))
                continue
        rows.append((name, before, after))

    print("\n%-16s %10s %10s %8s" % ("文件", "原始", "压缩后", "省下"))
    print("-" * 48)
    tb = ta = 0
    for name, b, a in rows:
        tb += b
        ta += a
        print("%-16s %9.1fK %9.1fK %7.0f%%" % (name, b / 1024, a / 1024,
                                               (1 - a / b) * 100 if b else 0))
    print("-" * 48)
    print("%-16s %9.1fK %9.1fK %7.0f%%" %
          ("合计", tb / 1024, ta / 1024, (1 - ta / tb) * 100 if tb else 0))

    if failed:
        print("\n✗ 以下文件压缩失败（未采用）：")
        for name, why in failed:
            print("   %-14s %s" % (name, why))

    if not apply_changes:
        shutil.rmtree(work, ignore_errors=True)
        print("\n（dry run：只报告，未改动任何文件。加 --apply 才真正切换）")
        return 0 if not failed else 1
    shutil.rmtree(work, ignore_errors=True)

    # 改写 index.html：/static/x.js → /static/min/x.min.js
    idx = os.path.join(STATIC, "index.html")
    html = open(idx, encoding="utf-8").read()
    n = 0
    for name, _b, _a in rows:
        ext = name.rsplit(".", 1)[1]
        out_name = name.rsplit(".", 1)[0] + ".min." + ext
        new, c = re.subn(r"/static/%s(?=[\"'])"
                         % re.escape(name), "/static/min/%s" % out_name, html)
        html, n = new, n + c
    open(idx, "w", encoding="utf-8").write(html)
    print("\n✓ index.html 已切换到压缩版（%d 处引用）" % n)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(build("--apply" in sys.argv))
