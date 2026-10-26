"""安全层回归 —— 令牌鉴权 / 限流 / CORS 默认值。

这三项都是 **opt-in**，所以每条都要验两个方向：

* **默认关闭**时行为必须与加它们之前**完全一致** —— 本机演示、`ui_probe.mjs`、
  `ui_layout_probe.mjs` 全靠这一点（它们直连 127.0.0.1:8137，不带任何令牌）。
* **打开**时必须真的挡住，而不是"配置读了但没生效"。

为什么用 `importlib.reload` 而不是改完 `os.environ` 直接调：配置是在**模块导入时**
读的（`_AUTH_TOKEN` / `_RL_MAX` / `_cors` 都是模块级常量），不重载就永远只测到
"默认关闭"那一半 —— 这正是那种"测试全绿但功能是坏的"的经典写法。

这里不走真实网络，直接把 ASGI scope 喂给 app —— 沙箱里回环会被代理拦
（`curl 127.0.0.1` 首次 200、随后 502），而且不需要起 uvicorn。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import pathlib
import re

import pytest

# 这四个变量都会影响模块级配置，每个用例前必须先清干净
_ENV_KEYS = ("FT_AUTH_TOKEN", "FT_RATE_LIMIT", "FT_CORS_ORIGINS", "FT_TRUST_PROXY")


def _load(monkeypatch, **env):
    """在指定的环境变量下重新导入 app.api.main，返回模块。"""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app.api.main as m
    return importlib.reload(m)


def _request(app, path, headers=None, method="GET", body=b"", ip="1.2.3.4"):
    """把一次请求直接喂给 ASGI app，返回 (status, headers_dict, body_bytes)。"""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": raw_headers,
        "client": (ip, 12345), "server": ("testserver", 80),
    }
    sent = []

    async def receive():
        if sent:
            # 第二次调用就挂住：真实客户端在响应结束前不会再发数据
            await asyncio.Event().wait()
        sent.append(1)
        return {"type": "http.request", "body": body, "more_body": False}

    out = {"status": None, "headers": {}, "body": b""}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    async def run():
        await app(scope, receive, send)

    asyncio.run(asyncio.wait_for(run(), timeout=20))
    return out["status"], out["headers"], out["body"]


# ---------------------------------------------------------------- 默认关闭
def test_without_token_everything_stays_open(monkeypatch):
    """不配 FT_AUTH_TOKEN 时，接口必须保持可匿名访问（本机演示/探针依赖这条）。"""
    m = _load(monkeypatch)
    assert m._AUTH_TOKEN == ""
    status, _, _ = _request(m.app, "/api/llm_status")
    assert status == 200, "默认不该拦 —— 否则 start.bat / ui_probe 会全部连不上"


def test_default_does_not_install_cors_middleware(monkeypatch):
    """CORS 默认不挂中间件（同源部署用不到跨域）。"""
    m = _load(monkeypatch)
    names = [x.cls.__name__ for x in m.app.user_middleware]
    assert "CORSMiddleware" not in names, "默认放开成 * 等于把接口对任意网页开放"
    assert "BaseHTTPMiddleware" in names, "鉴权/限流中间件本身要一直在（只是内部不拦）"


# ---------------------------------------------------------------- 鉴权
@pytest.mark.parametrize("path", ["/health", "/ready", "/"])
def test_health_and_static_pages_stay_open(monkeypatch, path):
    """健康检查与静态页必须豁免 —— 浏览器得先拿到页面才谈得上填令牌。

    `/` 是 307 重定向到 `/app`（不是 200），所以判据是"没被 401 挡住"。
    """
    m = _load(monkeypatch, FT_AUTH_TOKEN="s3cret")
    status, _, _ = _request(m.app, path)
    assert status != 401, f"{path} 被鉴权挡住了，浏览器将拿不到页面"
    assert status in (200, 307)


@pytest.mark.parametrize("headers", [
    {},                                                     # 完全没带
    {"X-Auth-Token": "wrong"},                              # 带错了
    {"Authorization": "Bearer wrong"},                      # Bearer 带错了
    {"Authorization": "Basic czNjcmV0"},                    # 认证方式不对
])
def test_protected_endpoints_reject_bad_credentials(monkeypatch, headers):
    m = _load(monkeypatch, FT_AUTH_TOKEN="s3cret")
    status, _, body = _request(m.app, "/api/llm_status", headers=headers)
    assert status == 401, f"{headers} 不该被放行"
    assert json.loads(body)["error"] == "unauthorized"


@pytest.mark.parametrize("headers", [
    {"X-Auth-Token": "s3cret"},
    {"Authorization": "Bearer s3cret"},
    {"authorization": "bearer s3cret"},                     # 大小写不敏感
])
def test_protected_endpoints_accept_valid_credentials(monkeypatch, headers):
    m = _load(monkeypatch, FT_AUTH_TOKEN="s3cret")
    status, _, _ = _request(m.app, "/api/llm_status", headers=headers)
    assert status == 200


def test_protected_post_is_also_guarded(monkeypatch):
    """写操作（能改设备配置 / 删档案的那类）必须同样被挡住。"""
    m = _load(monkeypatch, FT_AUTH_TOKEN="s3cret")
    status, _, _ = _request(m.app, "/api/schedule", method="POST",
                            headers={"Content-Type": "application/json"},
                            body=b'{"recipes": []}')
    assert status == 401


# ---------------------------------------------------------------- 限流
def test_rate_limit_blocks_after_threshold(monkeypatch):
    """超出阈值返回 429，并给出 Retry-After（客户端才知道该等多久）。"""
    m = _load(monkeypatch, FT_RATE_LIMIT="3/60")
    codes = [_request(m.app, "/api/llm_status", ip="9.9.9.9")[0] for _ in range(4)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    status, headers, _ = _request(m.app, "/api/llm_status", ip="9.9.9.9")
    assert status == 429
    assert int(headers.get("retry-after", "0")) >= 1


def test_rate_limit_is_per_ip(monkeypatch):
    """限流必须按 IP 隔离 —— 否则一个人刷爆，所有人一起被挡。"""
    m = _load(monkeypatch, FT_RATE_LIMIT="2/60")
    for _ in range(2):
        assert _request(m.app, "/api/llm_status", ip="1.1.1.1")[0] == 200
    assert _request(m.app, "/api/llm_status", ip="1.1.1.1")[0] == 429
    assert _request(m.app, "/api/llm_status", ip="2.2.2.2")[0] == 200, "别的 IP 不该被牵连"


def test_rate_limit_can_be_disabled(monkeypatch):
    m = _load(monkeypatch)                       # 不配 = 不限流
    codes = [_request(m.app, "/api/llm_status", ip="9.9.9.9")[0] for _ in range(30)]
    assert set(codes) == {200}


@pytest.mark.parametrize("raw,expect", [
    ("120/60", (120, 60)),
    ("10", (10, 60)),          # 只写次数，窗口默认 60s
    ("0/60", (0, 0)),          # 0 = 显式关掉
    ("", (0, 0)),
    ("garbage", (0, 0)),       # 解析失败按不限流，且不抛异常
])
def test_rate_limit_parsing(monkeypatch, raw, expect):
    m = _load(monkeypatch, FT_RATE_LIMIT=raw) if raw else _load(monkeypatch)
    assert (m._RL_MAX, m._RL_WINDOW) == expect


# ---------------------------------------------------------------- CORS
def test_cors_only_installed_when_configured(monkeypatch):
    m = _load(monkeypatch, FT_CORS_ORIGINS="https://cooking.example.com")
    names = [x.cls.__name__ for x in m.app.user_middleware]
    assert names[0] == "CORSMiddleware", "CORS 必须在最外层，401/429 才带得上跨域头"
    _, headers, _ = _request(m.app, "/health",
                             headers={"Origin": "https://cooking.example.com"})
    assert headers.get("access-control-allow-origin") == "https://cooking.example.com"


def test_unlisted_origin_gets_no_cors_header(monkeypatch):
    """只配了自己的域名，别的站点不该拿到放行头。"""
    m = _load(monkeypatch, FT_CORS_ORIGINS="https://cooking.example.com")
    _, headers, _ = _request(m.app, "/health", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in headers


# ---------------------------------------------------------------- 凭据分离
# .env 放非敏感配置（可随项目走），.env.local 放凭据（单独排除 / 单独轮换）。
def test_dotenv_reads_values_and_real_env_wins(monkeypatch, tmp_path):
    """真实环境变量必须优先于文件 —— 否则容器里注入的凭据会被仓库文件盖掉。"""
    from app.agent.llm import _load_dotenv
    p = tmp_path / ".env"
    p.write_text("FT_TEST_A=file\nFT_TEST_B=file\n# 注释\n\n没有等号的行\n", encoding="utf-8")
    monkeypatch.setenv("FT_TEST_A", "real")
    monkeypatch.delenv("FT_TEST_B", raising=False)
    _load_dotenv(str(p))
    try:
        assert os.environ["FT_TEST_A"] == "real"
        assert os.environ["FT_TEST_B"] == "file"
    finally:
        os.environ.pop("FT_TEST_B", None)


def test_env_local_overrides_env(monkeypatch, tmp_path):
    """`.env.local` 要能覆盖 `.env` —— 否则"把密钥挪过去"反而读不到。"""
    import app.agent.llm as llm
    (tmp_path / ".env").write_text("FT_TEST_C=from-env\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("FT_TEST_C=from-local\n", encoding="utf-8")
    monkeypatch.setattr(llm, "_ROOT", str(tmp_path))
    monkeypatch.delenv("FT_TEST_C", raising=False)
    llm._load_dotenv()
    try:
        assert os.environ["FT_TEST_C"] == "from-local"
    finally:
        os.environ.pop("FT_TEST_C", None)


def test_missing_dotenv_is_not_an_error(tmp_path):
    """没有 .env 是正常情况（公网部署直接注入环境变量），不能抛异常。"""
    from app.agent.llm import _load_dotenv
    _load_dotenv(str(tmp_path / "根本不存在.env"))


def test_unreadable_dotenv_warns_instead_of_failing_silently(tmp_path, caplog):
    """配置读不进来必须留痕。

    原来这里是 `except Exception: pass` —— 而"配置读不到 ⇒ API_KEY 为空 ⇒
    enabled() 恒 False"正是「远程访问时 AI 完全不工作」最经典的成因，
    静默会让人排查半天。
    """
    from app.agent.llm import _load_dotenv
    with caplog.at_level(logging.WARNING, logger="ft.llm"):
        _load_dotenv(str(tmp_path))          # 传目录 ⇒ open() 必然失败
    assert any("失败" in r.getMessage() for r in caplog.records), "读不到配置却没有任何痕迹"


# ---------------------------------------------------------------- 源码里不许有凭据
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),                                   # 中转站 / OpenAI 风格
    re.compile(r"""(?:api[_-]?key|secret|access[_-]?token)\s*=\s*["'][^"']{16,}["']""", re.I),
]


def test_no_credentials_hardcoded_in_source():
    """源码里不得出现凭据字面量 —— 凭据一律走环境变量。

    本项目历史上把 API key 硬编码在 `llm.py` 与 `chat/assistant.py` 各一份，
    随 Docker 镜像 / 提交 zip / 答辩 PPT 一起发出去过。这条护栏就是为了
    让那个坑不会回来：只扫**源码**（`app/` `scripts/`），不扫 `.env*` 与 `data/`。
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    hits = []
    for sub in ("app", "scripts"):
        for p in (root / sub).rglob("*"):
            if p.suffix not in (".py", ".mjs", ".js", ".html") or "__pycache__" in p.parts:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            for pat in _SECRET_PATTERNS:
                for m in pat.finditer(text):
                    hits.append(f"{p.relative_to(root)} → {m.group(0)[:20]}…")
    assert not hits, "源码里出现了疑似凭据字面量：\n  " + "\n  ".join(hits)


def test_shipped_tree_has_no_dotenv_files():
    """`publish/`（会被平台发布出去的那份）里不得有 .env / .env.local。

    凭据只应存在于开发机的项目根目录；一旦被拷进发布目录，就等于随线上一起公开。
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    pub = root / "publish"
    if not pub.exists():
        pytest.skip("还没有 publish/ 目录")
    leaked = [str(p.relative_to(root)) for p in pub.rglob("*")
              if p.is_file() and (p.name == ".env" or p.name.startswith(".env."))]
    assert not leaked, f"发布目录里带上了凭据文件：{leaked}"
