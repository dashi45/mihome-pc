# -*- coding: utf-8 -*-
"""只读探测：米家的「手动场景」和「自动化」能不能从云端列出来。

**只调 list 类接口，不触发、不改任何东西。**
输出写到 _probe_scene.json，方便对着看结构。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app as A          # 复用它的 token 路径 / 事件循环 / call()

# 候选接口：场景与自动化在不同版本里路径不一样，挨个试，能回就记下来
CANDIDATES = [
    ("/home/scene/list", {}),
    ("/home/scene/list", {"home_id": None}),
    ("/home/automation/list", {}),
    ("/app/home/automation/list", {}),
    ("/home/get_automation_list", {}),
]


async def _run():
    account, service, devices, homes = await A._do_restore()
    out = {"homes": [{"id": h.get("id"), "name": h.get("name")} for h in homes],
           "tried": []}
    hid = (homes[0].get("id") if homes else None)
    for path, extra in CANDIDATES:
        body = dict(extra)
        if body.get("home_id", "missing") is None:
            body["home_id"] = hid
        try:
            res = await service.miio_request(path, body)
            out["tried"].append({"path": path, "body": body, "ok": True,
                                 "result": res})
        except Exception as e:
            out["tried"].append({"path": path, "body": body, "ok": False,
                                 "error": str(e)[:300]})
    return out


try:
    data = A.call(_run(), timeout=60)
except Exception as e:
    data = {"fatal": str(e)[:400]}

dst = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   "_probe_scene.json")
with open(dst, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)

for it in data.get("tried", []):
    flag = "OK " if it["ok"] else "ERR"
    print("[%s] %-26s %s" % (flag, it["path"],
                             (json.dumps(it.get("result"), ensure_ascii=False)[:120]
                              if it["ok"] else it.get("error", "")[:120])))
if data.get("fatal"):
    print("fatal:", data["fatal"])
print("dump ->", dst)
