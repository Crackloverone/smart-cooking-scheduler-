"""模型调用失败的**可观测性**护栏（2026-10-09）。

起因是一个真实的现场：界面徽标写着「AI在线」、底部写着「已配置（sk-pro…ESb0）」，
但助手回的是「AI 助手当前不可用，已切换到离线模式」。用户完全无从下手 ——
真正的原因（上游 502「账号需实名认证」）只躺在服务端日志里。

两个根因，各钉一条：

1. **徽标只看熔断器**（开关开着 + 凭据非空），**不反映调用是否成功**。
   上游 502 这类失败在攒够 3 次阈值前不熔断 ⇒ 徽标一直绿色「在线」。
2. **响应体在 `_post` 里被直接丢掉** ⇒ 系统里根本不存在"为什么失败"这个信息。

修法：`_record_failure()` 留下 (状态码, 归纳原因, 是否确定性)，
`/api/llm_status` 与助手降级文案都把它带出来；确定性失败立刻熔断。

⚠ 铁律：**第三方域名只准出现在 `app/agent/llm_defaults.json`**
（`test_llm_provider.py::test_third_party_relay_appears_only_in_the_factory_file`）。
上游报错里常带自己的文档链接，所以 `_hint_of` 统一剥 URL —— 这里也钉一条。
"""
import json
from pathlib import Path

import pytest

from app.agent import llm
from app.chat import assistant

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def online(monkeypatch):
    """把 llm 摆成"远程档 + 有凭据 + 开关开着"的可调用状态，用完复位。"""
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "API_KEY", "sk-test-0000000000000000")
    monkeypatch.setenv("FT_LLM", "on")
    llm.breaker_reset()
    llm._LAST_FAIL.clear()
    yield
    llm.breaker_reset()
    llm._LAST_FAIL.clear()


def _fake_post(code: int, body: str = ""):
    """替身 `_post`：走与真实实现**同一条**失败记账路径。"""
    def f(payload, timeout):
        llm._record_failure(code, body)
        return "http", None, code
    return f


# --------------------------------------------------------------------- 1. 记录
def test_真实_post_把响应体交给记账(online, monkeypatch):
    """⚠ 这条必须走**真实的 `_post`**。

    其它用例用替身 `_fake_post`，而替身自己就调了 `_record_failure` —— 于是
    "把 `_post` 里那行 `_record_failure(e.code, body)` 删掉"这个变异**测不出来**
    （本轮变异自检实测踩到：16 项全绿，但功能已经废了）。
    所以这里 mock 掉 `urlopen`，让真实 `_post` 跑完整条 HTTPError 分支。
    """
    import io as _io
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 502, "Bad Gateway", {},
            _io.BytesIO("real-name verification required".encode()))

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    llm._LAST_FAIL.clear()
    status, content, code = llm._post({"model": "x", "messages": []}, 5)
    assert (status, code) == ("http", 502), (status, code)
    lf = llm.last_failure()
    assert lf.get("code") == 502, f"真实 _post 没记下状态码: {lf}"
    assert lf.get("hint"), "真实 _post 没有把响应体交给 _record_failure"


def test_失败原因被记录(online, monkeypatch):
    monkeypatch.setattr(llm, "_post", _fake_post(502, "real-name verification required"))
    assert llm.chat([{"role": "user", "content": "hi"}], timeout=5) is None
    lf = llm.last_failure()
    assert lf.get("code") == 502, f"没记下状态码: {lf}"
    assert lf.get("hint"), "没给出可操作的中文原因"


@pytest.mark.parametrize("body,expect", [
    ("real-name verification is required", "实名"),
    ("insufficient balance", "额度"),
    ("invalid api key", "凭据"),
])
def test_上游关键词能归纳成可操作原因(online, monkeypatch, body, expect):
    monkeypatch.setattr(llm, "_post", _fake_post(400, body))
    llm.chat([{"role": "user", "content": "hi"}], timeout=5)
    assert expect in llm.last_failure().get("hint", ""), llm.last_failure()


def test_失败记录里不含第三方域名或链接(online, monkeypatch):
    """上游报错带自己的文档链接 —— 必须剥掉（铁律：域名只准在出厂层）。"""
    monkeypatch.setattr(llm, "_post", _fake_post(
        502, "please verify at https://account.example.com/security?action=realname"))
    llm.chat([{"role": "user", "content": "hi"}], timeout=5)
    blob = json.dumps(llm.last_failure(), ensure_ascii=False)
    for bad in ("http://", "https://", "example.com"):
        assert bad not in blob, f"失败记录泄漏了 {bad}: {blob}"


def test_成功后清空失败记录(online, monkeypatch):
    """链路恢复后界面上的失败提示必须消失，否则会一直误报。"""
    monkeypatch.setattr(llm, "_post", _fake_post(502, "boom"))
    llm.chat([{"role": "user", "content": "hi"}], timeout=5)
    assert llm.last_failure(), "前置条件：应当已记录一次失败"
    monkeypatch.setattr(llm, "_post", lambda p, t: ("ok", "好的", 200))
    assert llm.chat([{"role": "user", "content": "hi"}], timeout=5) == "好的"
    assert llm.last_failure() == {}, "成功后没清空失败记录"


# --------------------------------------------------------------------- 2. 熔断
def test_确定性失败立刻熔断(online, monkeypatch):
    """凭据被拒 / 实名要求这类失败重试注定一样 —— 不该让用户白等 3 个超时。

    更要紧的是：熔断打开后界面才显示「模型熔断中」，否则徽标一直挂绿色「在线」。
    """
    monkeypatch.setattr(llm, "_post", _fake_post(502, "real-name verification required"))
    llm.chat([{"role": "user", "content": "hi"}], timeout=5)
    assert llm.breaker_open() is True, "确定性失败没有立刻熔断"
    assert llm.breaker_reason() == "auth"


@pytest.mark.parametrize("code", [401, 402, 403, 429])
def test_凭据与额度类状态码也立刻熔断(online, monkeypatch, code):
    monkeypatch.setattr(llm, "_post", _fake_post(code, ""))
    llm.chat([{"role": "user", "content": "hi"}], timeout=5)
    assert llm.breaker_open() is True, f"HTTP {code} 没有立刻熔断"


def test_普通网络错误仍按阈值熔断(online, monkeypatch):
    """防止"立刻熔断"被做过头：一次偶发 500 不该把模型整路关掉。"""
    monkeypatch.setattr(llm, "_post", _fake_post(500, "internal server error"))
    llm.chat([{"role": "user", "content": "hi"}], timeout=5)
    assert llm.breaker_open() is False, "一次普通 5xx 就把模型熔断了"


# --------------------------------------------------------------------- 3. 呈现
def test_助手降级文案带出原因(online, monkeypatch):
    monkeypatch.setattr(llm, "_post", _fake_post(502, "real-name verification required"))
    out = assistant.chat([{"role": "user", "content": "hi"}])
    assert out["offline"] is True
    assert "502" in out["reply"], f"降级文案没说原因: {out['reply'][:120]}"
    assert "实名" in out["reply"]


def test_助手降级文案不含第三方域名(online, monkeypatch):
    monkeypatch.setattr(llm, "_post", _fake_post(
        502, "verify at https://account.example.com/x"))
    out = assistant.chat([{"role": "user", "content": "hi"}])
    for bad in ("http://", "https://", "example.com"):
        assert bad not in out["reply"], f"助手文案泄漏了 {bad}"


def test_状态端点暴露last_failure():
    """徽标要靠它才能从「在线」改成警示态。"""
    src = (ROOT / "app" / "api" / "main.py").read_text(encoding="utf-8")
    assert '"last_failure": _llm.last_failure()' in src, \
        "/api/llm_status 没有暴露 last_failure —— 徽标无法区分「在线」与「在线但调用失败」"


def test_前端徽标有在线但失败的分支():
    """钉住那条分支：state=online 但 last_failure 有值 ⇒ 显示警示而非绿色。

    ⚠ 判据必须落在"`failing` **被用于**显示"，不能只判"页面里有『模型调用失败』
    这几个字" —— 后者在 `failing` 被写死成 false 时**照样通过**（本轮变异自检
    实测踩到：16 项全绿但功能已废）。同类陷阱在本项目反复出现过，
    统一记在 `MEMORY-细则.md`。
    """
    import re
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    assert "d.last_failure" in html, "前端没有读 last_failure"
    assert "failing?'⚠ '" in html, "failing 没有参与图标选择"
    assert "failing?'warn'" in html, "failing 没有参与徽标样式选择"
    # ⚠ 还必须判**定义式**：把 `failing` 写死成 false 时，上面两条（判"使用处"）
    #   会照样通过 —— 本轮变异自检实测踩到第二次。分支要真的接上数据源才算数。
    m = re.search(r"const failing=([^;]+);", html)
    assert m, "找不到 failing 的定义"
    assert "lf.code" in m.group(1), \
        f"failing 没有真正依赖 last_failure（定义式：{m.group(1).strip()}）"
