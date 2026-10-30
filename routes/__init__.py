# -*- coding: utf-8 -*-
"""HTTP 路由层：一个领域一个蓝图，只做"解析请求 → 调服务 → 返回 JSON"。

蓝图**不带 url_prefix**：所有接口路径与拆分前逐字节一致。
前端不感知后台结构，老的 /api/* 调用一个都不用改。
"""
import importlib

#: 注册顺序无关紧要（没有 url_prefix，路径唯一），但保持和文件列表一致好读
BLUEPRINTS = (
    "auth", "system", "devices", "sched", "live", "weather",
    "pc", "events", "ext", "energy", "cam", "charger",
)


def register_all(app):
    names = []
    for name in BLUEPRINTS:
        mod = importlib.import_module("routes.%s" % name)
        app.register_blueprint(mod.bp)
        names.append(name)
    return names
