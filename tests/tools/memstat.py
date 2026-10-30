# -*- coding: utf-8 -*-
"""临时：量整个应用的内存。只用于优化前后对比，不进正式流程。"""
import psutil


def mb(v):
    return v / 1048576.0


def main():
    wv_p = wv_w = 0
    print("== webview（pywebview profile）==")
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if p.info["name"] != "msedgewebview2.exe":
                continue
            cl = " ".join(p.info["cmdline"] or [])
            if "pywebview" not in cl:
                continue
            m = p.memory_info()
            kind = next((x.split("=")[1] for x in (p.info["cmdline"] or [])
                         if x.startswith("--type=")), "browser")
            print("  %-10s %-6d 私有=%5.0fMB ws=%5.0fMB" %
                  (kind, p.pid, mb(m.private), mb(m.rss)))
            wv_p += m.private
            wv_w += m.rss
        except Exception:
            pass
    print("  webview 合计: 私有 %.0fMB / ws %.0fMB" % (mb(wv_p), mb(wv_w)))

    print("\n== 其他 ==")
    o_p = o_w = 0
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = p.info["name"]
            cl = " ".join(p.info["cmdline"] or [])
            if name == "ffmpeg.exe":
                if "go2rtc" not in cl and "mihome" not in cl:
                    continue
            elif name == "go2rtc-tk.exe":
                pass
            elif name == "pythonw.exe":
                if "mihome" not in cl:
                    continue
            elif name == "米家.exe":
                # onefile 打包版有两个：bootloader 父进程（约 1MB）
                # 和真正跑应用的那个（约 70MB，pywebview/pythonnet 的运行时）
                pass
            else:
                continue
            m = p.memory_info()
            print("  %-14s %-6d 私有=%5.0fMB ws=%5.0fMB" %
                  (name, p.pid, mb(m.private), mb(m.rss)))
            o_p += m.private
            o_w += m.rss
        except Exception:
            pass
    print("  其他合计: 私有 %.0fMB / ws %.0fMB" % (mb(o_p), mb(o_w)))
    print("\n== 总计: 私有 %.0fMB / 工作集 %.0fMB ==" %
          (mb(wv_p + o_p), mb(wv_w + o_w)))


if __name__ == "__main__":
    main()
