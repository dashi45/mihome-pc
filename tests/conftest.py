# -*- coding: utf-8 -*-
"""pytest 入口：把**脚本式**测试包装成 pytest 用例。

现有 40 个测试是脚本式的（顶层直接判断、打印 ✓/✗、末尾 sys.exit）。直接让
pytest 收集它们，会在 import 阶段就把文件跑一遍 —— e2e 那几个会当场开窗口、
抢托盘、往真实 Go2RTC 拉流，等于"收个集"就把机器点着了。

所以这里接管收集：每个 test_*.py 变成**一个用例**，在子进程里跑完再判结果。
好处是 `pytest tests/ -v` 真的能一条命令跑（并且能 -m 过滤、-k 选文件），
坏处是失败时只能看到输出末尾（脚本本身不是按用例组织的，也没法细分）。

两种用法都可以：

    pytest tests/ -v                     # 全部非界面（e2e/tray/ui 标了 gui）
    pytest tests/ -v -m "not gui"        # 同上，显式写法
    python tests/run_all.py              # 不依赖 pytest 的等价入口
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "tests", "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

#: 默认不跑的目录：会开窗口 / 需要真人看屏幕 / 需要**先有一个跑起来的实例**
#: （perf 走 CDP 连已启动的应用量帧率，没有实例必然连接失败 —— 它不是回归测试，
#:   是"想优化时手动量一把"的工具，放在这里而不是 tools/ 是因为它确实会断言）
GUI_DIRS = {"e2e", "ui", "tray", "perf"}
TOOL_DIRS = {"tools"}          # 探针脚本，不是测试
TIMEOUT = 180


# ---------------------------------------------------------------- 脚本式测试收集

def pytest_collect_file(file_path, parent):
    """只认 tests/<子目录>/test_*.py，且排除工具目录。"""
    path = str(file_path)
    if file_path.suffix != ".py" or not file_path.name.startswith("test_"):
        return None
    parts = path.replace("\\", "/").split("/")
    if "tests" not in parts:
        return None
    sub = parts[parts.index("tests") + 1] if parts.index("tests") + 1 < len(parts) - 1 else ""
    if sub in TOOL_DIRS:
        return None
    return ScriptTestFile.from_parent(parent, path=file_path, sub=sub)


class ScriptTestFile(pytest.File):
    def __init__(self, *, sub="", **kw):
        super().__init__(**kw)
        self.sub = sub

    def collect(self):
        yield ScriptTestItem.from_parent(self, name=self.path.name, sub=self.sub)


class ScriptTestItem(pytest.Item):
    def __init__(self, *, sub="", **kw):
        super().__init__(**kw)
        self.sub = sub
        self.add_marker(pytest.mark.gui if sub in GUI_DIRS else pytest.mark.script)

    def runtest(self):
        try:
            r = subprocess.run([sys.executable, "-u", str(self.path)],
                               cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise AssertionError("测试超时（%ds）" % TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
        bad = [ln for ln in out.splitlines() if "✗" in ln or "❌" in ln]
        if r.returncode != 0 or bad:
            tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-15:])
            raise AssertionError("rc=%s\n%s" % (r.returncode, tail))

    def repr_failure(self, excinfo):
        return str(excinfo.value)

    def reportinfo(self):
        return self.path, 0, "script: %s" % self.name


# ---------------------------------------------------------------- 公共 fixture


@pytest.fixture(scope="session")
def proj():
    """项目根目录。"""
    return ROOT


@pytest.fixture()
def tmp_home(tmp_path, monkeypatch):
    """把 config.BASE 指到临时目录，测试不碰真实数据（token/缓存/事件库）。

    注意 config 里的路径是**模块加载时**算好的常量，所以这里除了改 BASE，
    还要把各个 *_PATH 一起重定向 —— 这正是"用属性访问而非 from-import"的好处。
    """
    import config

    monkeypatch.setattr(config, "BASE", str(tmp_path), raising=False)
    for name, filename in (
            ("TOKEN_PATH", ".mi.token"), ("TOKEN_BAK", ".mi.token.bak"),
            ("SPEC_CACHE", "spec_cache.json"), ("DEV_CACHE", "devices_cache.json"),
            ("EVT_DB", "evt.db"), ("ENERGY_DAYS", ".energy_days.json")):
        monkeypatch.setattr(config, name, str(tmp_path / filename), raising=False)
    return tmp_path


@pytest.fixture()
def client():
    """Flask 测试客户端，自带"本机同源"请求头。

    守卫会对 Host/Origin 做严格校验（挡 DNS rebinding 和 CSRF），
    裸 client.get("/") 会被 403 挡掉 —— 这不是绕过，是模拟真实浏览器。
    """
    import app as app_module
    import config

    port = config.PORT
    web = app_module.app.test_client()
    web.environ_base["HTTP_HOST"] = "127.0.0.1:%d" % port
    web.environ_base["HTTP_ORIGIN"] = "http://127.0.0.1:%d" % port
    return web


@pytest.fixture()
def fake_miio():
    """能记录调用的假 MiIOService：想断言"到底发了什么请求"时用它。"""

    class FakeMiIO:
        def __init__(self):
            self.calls = []

        async def miot_get_props(self, *a, **kw):
            self.calls.append(("get_props", a, kw))
            return []

        async def miot_action(self, *a, **kw):
            self.calls.append(("action", a, kw))
            return {}

        async def miio_request(self, url, data=None, **kw):
            self.calls.append(("miio_request", url, data))
            return {}

    return FakeMiIO()
