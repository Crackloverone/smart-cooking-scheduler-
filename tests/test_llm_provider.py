"""
模型接入层（`app/agent/llm.py`）的 provider 分流守卫。

背景
----
2026-10-04 把默认模型从"API Key + 远端中转站"改成"本地 Ollama"。这次改造
真正容易出错的地方**不是**"能不能发出请求"，而是三件静默失败：

1. **拿 `bool(API_KEY)` 当总开关** —— Ollama 免密钥，于是本地模型被永久判成
   "未配置"，全链路走规则兜底，而页面不报任何错。现象是"接上 Ollama 之后
   AI 毫无反应"，只能靠人盯着输出猜。
2. **端点/模型名跨 provider 共用** —— 只改 provider 不改端点，请求会带着远端
   模型名打到本机 11434，Ollama 返回 404 `model not found`，同样静默降级。
3. **`.env` 里的空值把默认值顶掉** —— `FT_LLM_BASE_URL=`（留空表示"用默认"）
   会让 `os.environ.get(k, default)` 返回空串而不是 default，请求发到
   `/chat/completions` 这种相对路径上。改造前默认值硬编码在 `get()` 里，
   正好被 `.env.local` 的空值躲过去了；默认值改成分 provider 之后必踩。

这三条都不会让测试变红、也不会让页面报错，所以钉在这里。
"""
from __future__ import annotations

import json
import os
import urllib.error
from pathlib import Path

import pytest

from app.agent import llm

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def online(monkeypatch):
    """打开全局开关（conftest 把整个套件钉在离线档），并复位熔断器。"""
    monkeypatch.setenv("FT_LLM", "on")
    llm.breaker_reset()
    yield
    llm.breaker_reset()


# ---------------------------------------------------------------- 配置读取
@pytest.mark.parametrize("value", ["", "   "])
def test_cfg_treats_a_blank_value_as_unset(monkeypatch, value):
    """空串 / 纯空白必须回落到默认值 —— 这是 `.env` 写 `KEY=` 的语义。"""
    monkeypatch.setenv("FT_TEST_CFG", value)
    assert llm._cfg("FT_TEST_CFG", "默认") == "默认"


def test_cfg_prefers_a_real_value(monkeypatch):
    monkeypatch.setenv("FT_TEST_CFG", " 真值 ")
    assert llm._cfg("FT_TEST_CFG", "默认") == "真值", "值要 strip 掉两端空白"


def test_base_url_never_ends_with_a_slash():
    """端点末尾带斜杠会拼出 `//chat/completions`，部分网关会 404。"""
    assert llm.BASE_URL == llm.BASE_URL.rstrip("/"), f"BASE_URL={llm.BASE_URL!r}"


# ---------------------------------------------------------------- 默认档位
def test_default_provider_is_local_ollama():
    """**代码兜底档**必须是本地 Ollama —— 这一层不许带凭据，钉住它。

    注意这条断言只覆盖 `_DEFAULTS`（最后一层兜底）。"开箱用哪个档"由**出厂层**
    `app/agent/llm_defaults.json` 决定，见下面 test_factory_defaults_* 一组。
    两层分工不能混：出厂层是数据（可换、可删、随包走），代码默认是常量（不带凭据）。
    """
    assert llm._DEFAULTS["ollama"] == ("http://127.0.0.1:11434/v1", "qwen2.5:7b"), (
        "本地默认端点/模型被改了；改可以，但要同时改这条断言和文档"
    )


def test_third_party_relay_appears_only_in_the_factory_file():
    """第三方中转站域名**只允许出现在出厂默认配置文件里**（一个文件，仅此一处）。

    历史：`FT_LLM_BASE_URL` 的默认值曾是某个中转站地址，硬编码在代码里，会随源码 /
    镜像 / 提交 zip 一起发出去，换站还得改代码。

    2026-10-06 产品要求改成"**发出去对方第一时间就能用**"，于是开箱配置回到仓库 ——
    但放法变了：写进 `app/agent/llm_defaults.json`（出厂层数据文件），
    **不写进任何 .py / .html / .mjs**。区别是实质性的：
      · 数据文件可以整份删掉（回落到本地 Ollama 档），代码里的常量删不掉；
      · 换站/轮换密钥 = 改一个 json + 重新打包，不用改代码、不用重跑测试；
      · 这一层能被用户层整层压过，用户改过之后就再也不会被它影响。

    域名不写死在断言里，而是**从那个文件里读出来**再全仓扫描 ——
    换中转站时这条护栏自动跟着走，不用改测试。
    """
    import urllib.parse
    factory_path = ROOT / "app" / "agent" / "llm_defaults.json"
    assert factory_path.exists(), (
        "出厂默认配置文件不见了 —— 发出去的包会退回本地 Ollama 档，接收方用不了模型"
    )
    factory = json.loads(factory_path.read_text(encoding="utf-8"))
    host = urllib.parse.urlsplit(str(factory["remote"]["base_url"])).hostname
    assert host, "出厂配置里的 remote.base_url 解析不出主机名"

    # 扫"随程序发出去的代码"。文档 / 记忆 / data / publish 不在内 ——
    # 前者是给人看的说明，后者是生成物（且 data/ 里那份用户层本来就可能存着它）。
    roots = [ROOT / "app", ROOT / "scripts", ROOT / "deploy"]
    allowed = {factory_path.resolve()}
    suffixes = {".py", ".mjs", ".js", ".html", ".htm", ".json", ".ps1", ".cmd", ".bat", ".txt"}
    hits: list[str] = []
    for root in roots:
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in suffixes:
                continue
            if p.resolve() in allowed or "__pycache__" in p.parts:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if host in text:
                hits.append(str(p.relative_to(ROOT)))
    for extra in (".env.example", "Dockerfile"):
        p = ROOT / extra
        if p.exists() and host in p.read_text(encoding="utf-8", errors="replace"):
            hits.append(extra)

    assert not hits, (
        f"第三方域名 {host!r} 出现在 {hits} —— 它只允许出现在 "
        f"app/agent/llm_defaults.json 这一个文件里（那是可替换、可整份删掉的出厂层）"
    )


# ---------------------------------------------------------------- 出厂默认层
def test_factory_defaults_file_gives_a_working_config_out_of_the_box():
    """**出厂层必须真的能开箱用**：用户层为空时，provider / 端点 / 密钥 / 模型全都齐。

    这是本次改造的产品目标本身 —— "发给别人，别人第一时间就能用"。
    只断言"文件存在"是不够的：文件在、但 remote.api_key 漏了，表现就是
    "打开页面看着配好了、AI 一句话都不回"（`enabled()` 因缺密钥返回 False），
    而页面上不会有任何报错。
    """
    factory = llm._read_defaults()
    assert factory, f"出厂默认配置读不出来：{llm._DEFAULTS_PATH}"

    saved: dict = {}          # 用户层为空 = 接收方刚拿到包
    provider = llm._resolve_provider("", saved)
    assert provider == "openai", "出厂层没有把接入方式指到远程 —— 接收方还得自己配"
    assert llm._resolve_base_url("", saved, provider).startswith("https://")
    assert llm._resolve_api_key("", saved, provider), (
        "出厂层没带密钥 ⇒ enabled() 恒 False ⇒ 接收方拿到包也调不动模型，且界面不报错"
    )
    assert llm._resolve_model("", saved, provider) == (factory["models"]["openai"], "factory")


def test_user_settings_win_over_the_factory_defaults():
    """用户层**整层优先** —— 接收方改过之后，重新发一版程序不许把它盖回去。

    这是"出厂层"和"用户层"必须分开两个文件的原因：出厂层随包走（重新打包会覆盖），
    用户层被 `sync_publish` 刻意排除（重新发版不会碰）。合成一份的话，
    每次发新版都会把接收方配好的地址和密钥换回默认值 —— 而且是静默的。
    """
    saved = {"provider": "openai", "model": "my-own-model:1b",
             "remote": {"base_url": "https://mine.example/v1", "api_key": "sk-mine"},
             "models": {"openai": "my-own-model:1b"}}

    assert llm._resolve_provider("", saved) == "openai"
    assert llm._resolve_base_url("", saved, "openai") == "https://mine.example/v1"
    assert llm._resolve_api_key("", saved, "openai") == "sk-mine"
    assert llm._resolve_model("", saved, "openai") == ("my-own-model:1b", "settings")


def test_env_still_beats_both_file_layers():
    """环境变量优先级**高于**出厂层 —— 否则容器里钉死接入方式会被随包的文件推翻。"""
    assert llm._resolve_provider("ollama", {}) == "ollama"
    assert llm._resolve_base_url("http://127.0.0.1:9999/v1", {}, "openai") == \
        "http://127.0.0.1:9999/v1"
    assert llm._resolve_api_key("sk-env", {}, "openai") == "sk-env"
    assert llm._resolve_model("env-model:1b", {}, "openai") == ("env-model:1b", "env")


def test_factory_layer_falls_back_to_code_defaults_when_absent_or_broken(monkeypatch):
    """出厂层缺失 / 坏形状 ⇒ 安静回落到代码默认档（本地 Ollama），不许抛。

    接收方完全可能主动删掉这个文件（不想用随包附带的凭据）——
    那时候必须还能跑，而不是服务起不来。
    """
    # ① 整层缺失（文件被删掉）
    monkeypatch.setattr(llm, "_FACTORY", {})
    assert llm._resolve_provider("", {}) == "ollama"
    assert llm._resolve_api_key("", {}, "openai") == "", "出厂层为空却漏出了凭据"
    assert llm._resolve_model("", {}, "ollama") == (llm._DEFAULTS["ollama"][1], "default")
    assert llm._resolve_base_url("", {}, "openai") == llm._DEFAULTS["openai"][0]

    # ② 形状坏掉（remote / models 不是 dict）—— 当空处理，不能炸
    monkeypatch.setattr(llm, "_FACTORY",
                        {"provider": "openai", "remote": "坏形状", "models": 123})
    assert llm._resolve_provider("", {}) == "openai", "provider 字段本身是好的，照用"
    assert llm._resolve_api_key("", {}, "openai") == ""
    assert llm._resolve_base_url("", {}, "openai") == llm._DEFAULTS["openai"][0]
    assert llm._resolve_model("", {}, "openai") == (llm._DEFAULTS["openai"][1], "default")

    # ③ provider 值不认识 —— 按 ollama 处理（与 _norm_provider 同款口径）
    monkeypatch.setattr(llm, "_FACTORY", {"provider": "不认识", "remote": {}})
    assert llm._resolve_provider("", {}) == "ollama"


def test_broken_factory_file_is_ignored_not_fatal(tmp_path, monkeypatch):
    """文件存在但内容是半截 json ⇒ 当空处理（与用户层同款纪律），只留 warning。"""
    p = tmp_path / "llm_defaults.json"
    p.write_text('{"provider": "openai", "remote": {"base_url"', encoding="utf-8")
    monkeypatch.setattr(llm, "_DEFAULTS_PATH", str(p))

    assert llm._read_defaults() == {}, "坏文件必须当空，不能把异常抛给调用方"


def test_switching_provider_without_a_model_keeps_the_factory_model(tmp_settings, monkeypatch):
    """切档时没显式给模型 ⇒ 落盘的模型名要取**出厂层**的，不是代码默认的。

    踩法很隐蔽：接收方的用户层是空的，出厂层配的是 `gpt-5.6-sol`。
    他打开设置页、什么都不改、直接点保存 → `set_provider("openai", model=None)`。
    若这里回落到 `_DEFAULTS["openai"][1]`（`gpt-4o-mini`），用户层就写下了一个
    **中转站上并不存在**的模型名 ⇒ 之后每次调用 404 ⇒ 静默降级成规则兜底，
    而页面上写着"模型：gpt-4o-mini"，一切看起来都很正常。
    """
    monkeypatch.setattr(llm, "_FACTORY", {"provider": "openai",
                                          "models": {"openai": "factory-model:1b"}})

    res = llm.set_provider("openai")          # 不传 model / base_url / api_key
    assert res["ok"] is True, res

    assert llm.MODEL == "factory-model:1b", (
        "没给模型时落盘了代码默认值 —— 会把出厂层配好的模型名换掉"
    )
    raw = json.loads(open(llm._SETTINGS_PATH, encoding="utf-8").read())
    assert raw["model"] == "factory-model:1b"


# ---------------------------------------------------------------- 密钥打码
def test_mask_key_keeps_the_ends_and_hides_the_middle():
    """打码规则：前 6 + 省略号 + 后 4；太短的整把打掉。

    为什么不干脆不回传：只说"已配置"时用户没法确认配的是**哪一把**钥匙
    （换过 key 之后尤其明显），于是会反复重填、把好着的配置改坏。
    前 6 + 后 4 够辨认，中间那段（密钥的熵几乎全在这里）不出去。
    """
    key = "sk-pro100x-cmfp694l0VD44SGic3NVW61FjlGc837EMAPI7vnESb0"
    masked = llm.mask_key(key)

    assert masked == "sk-pro…ESb0"
    assert key not in masked
    assert masked not in key
    assert len(masked) < len(key) / 2, "打码后还是太长，等于把密钥漏出去大半"

    # 太短的：露头露尾就等于把整把钥匙给出去
    assert llm.mask_key("sk-abc") == "***"
    assert llm.mask_key("") == ""
    assert llm.mask_key(None) == ""


def test_looks_masked_recognises_previews_but_not_real_keys():
    """判据必须只命中"打码预览"，不能误伤真密钥（误伤 = 用户永远存不上钥匙）。"""
    real = ["sk-pro100x-cmfp694l0VD44SGic3NVW61FjlGc837EMAPI7vnESb0",
            "sk-test-key", "gsk_abcdefghijklmnop", "vllm-local-token-1234567890"]
    for k in real:
        assert llm.looks_masked(k) is False, f"{k!r} 被误判成打码值 —— 用户会存不上钥匙"

    for fake in ["sk-pro…ESb0", "***", "sk-****abcd", llm.mask_key(real[0])]:
        assert llm.looks_masked(fake) is True, f"{fake!r} 没被认出是打码值"


def test_masked_key_is_rejected_on_write(tmp_settings):
    """**把打码预览当密钥提交必须 400** —— 这是整套改动里最容易踩的一脚。

    界面把 `sk-pro1…ESb0` 显示在标签行里，用户很可能顺手复制进输入框、点保存。
    真写进去之后所有调用 401/403，而 llm 的失败是**静默降级**，
    现场只剩"AI 突然不工作了"，根本联想不到是保存那一下干的。

    同时必须守住：拒绝时**什么都不许改**（不能"校验失败却已经把 provider 改掉"），
    并且留空仍然是"保持原样"（否则用户被逼着重填一遍真钥匙）。
    """
    llm.set_provider("openai", base_url="https://relay.example/v1",
                     api_key="sk-keep-me-original", model="gpt-5.6-sol")
    before = {k: getattr(llm, k) for k in _SETTING_KEYS}
    before_raw = open(llm._SETTINGS_PATH, encoding="utf-8").read()

    for fake in (llm.mask_key("sk-keep-me-original"), "sk-****abcd", "***"):
        res = llm.set_provider("openai", api_key=fake)
        assert res["ok"] is False, f"{fake!r} 被当成真密钥写进去了"
        assert "打码" in res["error"], res["error"]
        assert llm.API_KEY == "sk-keep-me-original", f"{fake!r}: 拒绝时却把密钥改掉了"
        assert {k: getattr(llm, k) for k in _SETTING_KEYS} == before, (
            f"{fake!r}: 校验失败却已经改了全局配置"
        )
        assert open(llm._SETTINGS_PATH, encoding="utf-8").read() == before_raw, (
            f"{fake!r}: 校验失败却已经写盘"
        )

    # 留空 = 保持原样，仍然要能正常保存（别把用户逼得非重填不可）
    assert llm.set_provider("openai", api_key="")["ok"] is True
    assert llm.API_KEY == "sk-keep-me-original"


def test_remote_view_exposes_a_masked_preview_but_never_the_key(tmp_settings, monkeypatch):
    """`remote_view()` 回**打码预览**，绝不回原文；且要报"生效值"。

    `/api/model_settings` 是无鉴权的（本机演示 / 发布出去的链接都能读）。
    打码预览只够辨认是哪一把钥匙，不足以拿去调用；再加上后端拒绝把掩码写回去，
    这个字段就是安全的。

    `effective_*` 必须报**生效值**：接收方的用户层是空的，真正在跑的是随包附带的
    出厂预置。只报"已保存"会让界面显示"未配置"，而用户明明能正常用 ——
    他会以为配置丢了，再去乱填一遍。
    """
    monkeypatch.setattr(llm, "_FACTORY",
                        {"provider": "openai",
                         "remote": {"base_url": "https://factory.example/v1",
                                    "api_key": "sk-factory-key-0123456789"},
                         "models": {"openai": "factory-model:1b"}})

    view = llm.remote_view()          # 用户层是空的（tmp_settings 指向空目录）
    blob = json.dumps(view, ensure_ascii=False)

    assert "sk-factory-key-0123456789" not in blob
    assert view["api_key_set"] is True
    assert view["api_key_masked"] == llm.mask_key("sk-factory-key-0123456789")
    assert view["base_url"] == "", "用户层没存过 ⇒ 回填值就该是空"
    assert view["effective_base_url"] == "https://factory.example/v1"
    assert view["effective_model"] == "factory-model:1b"


def test_factory_file_lives_where_it_will_actually_be_shipped():
    """出厂文件必须落在 `app/` 下 —— 位置本身就是"能不能发出去"的一部分。

    `scripts/sync_publish.py` 同步 `app/` 全量、但把 `data/llm_settings.json`
    排除掉（那是开发机自己的选择）。把出厂层放进 `data/` 会踩同一个坑：
    本地一切正常、发出去的包里没有它，接收方解压后只能跑本地 Ollama 档，
    而**没有任何报错** —— 正是这次要修的那个问题。
    """
    p = Path(llm._DEFAULTS_PATH).resolve()
    app_root = (ROOT / "app").resolve()
    assert app_root in p.parents, f"出厂层跑到了 {p} —— 它必须在 app/ 下才会随包发出"
    assert p.name == "llm_defaults.json"
    assert p.exists(), "出厂文件不存在"

    # 另外两条分发通道也要能带上它：
    #   · Docker：`Dockerfile` 是 `COPY app ./app`，只要 `.dockerignore` 没排除它就行
    #     （对照：`data/llm_settings.json` 是**被明确排除**的，这正是它不能当出厂层的原因）；
    #   · Windows zip：`pack.ps1` 从 `publish/` 取，`sync_publish` 全量同步 `app/`。
    di = (ROOT / ".dockerignore")
    if di.exists():
        patterns = [ln.strip() for ln in di.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
        assert not any(pat in ("app/agent/llm_defaults.json", "*.json", "app/**")
                       for pat in patterns), (
            f".dockerignore 把出厂层排除了（命中 {patterns}）—— 镜像里就没有开箱配置了"
        )
    df = (ROOT / "Dockerfile")
    if df.exists():
        assert "COPY app" in df.read_text(encoding="utf-8"), (
            "Dockerfile 不再整体 COPY app/ —— 出厂层可能进不了镜像"
        )


def test_out_of_the_box_chain_is_usable_when_the_user_layer_is_absent(tmp_path, monkeypatch):
    """端到端（接收方视角）：**没有用户层**时，整条链路必须已经可用。

    这是本次改造的验收判据本身 —— "发给别人，别人第一时间就能用"。
    单独测 `_resolve_*` 是不够的：它们全对，而 `enabled()` 仍可能因为别的原因
    返回 False（比如 provider 与 api_key 的搭配判断），表现就是"打开页面看着配好了、
    AI 一句话都不回"，页面上没有任何报错。
    """
    from app.api import main as M

    # conftest 把整套钉在离线档（FT_LLM=off），而这里要验的正是"开箱就是在线"。
    # 打开总闸不会引发真实网络请求：本用例只读配置与状态端点，
    # 而 provider=openai 时 installed_models() 直接早退，不碰本机 Ollama。
    monkeypatch.setenv("FT_LLM", "on")
    monkeypatch.setattr(llm, "_SETTINGS_PATH", str(tmp_path / "从未存在过.json"))
    llm._apply({})                 # 与 set_provider 落盘后走同一条重算路径
    llm._recompute_timeouts()
    try:
        assert llm.PROVIDER == "openai", "接收方拿到的还是本地档 ⇒ 得自己配一遍"
        assert llm.BASE_URL.startswith("https://")
        assert llm.API_KEY, "没密钥 ⇒ enabled() 恒 False ⇒ AI 静默不工作"
        assert llm.MODEL_SOURCE == "factory"
        assert llm.enabled() is True

        cfg = M.get_model_settings()
        assert cfg["remote"]["api_key_set"] is True
        # 用户层是空的，但界面必须显示**生效值**，否则接收方以为配置丢了
        assert cfg["remote"]["effective_base_url"] == llm.BASE_URL
        assert cfg["factory_defaults"]["present"] is True
        st = M.llm_status()
        assert st["state"] == "online", st
        # 整条链路都不许把密钥原文漏出去
        blob = json.dumps(cfg, ensure_ascii=False) + json.dumps(st, ensure_ascii=False)
        assert llm.API_KEY not in blob
    finally:
        # 复原成"真实用户层"下的解析结果，免得污染同进程里的其它用例
        llm._apply(llm._read_settings())
        llm._recompute_timeouts()


def test_status_endpoint_reports_the_masked_key():
    """`/api/llm_status` 也要回打码预览 —— 顶部徽标的 tooltip 用的就是它。

    只回"已配置"时，换过钥匙的人无法确认现在用的到底是哪一把 ——
    而这恰好是排查"我明明换了 key，怎么还是 401"时唯一能自证的信息。
    """
    from app.api import main as M
    d = M.llm_status()
    assert d["api_key_masked"] == llm.mask_key(llm.API_KEY)
    assert "api_key_masked" in d, "状态端点没回打码预览"


def test_factory_view_never_leaks_the_key():
    """出厂层的诊断视图同样只给打码预览。

    ⚠ 这条**不能**用 `tmp_settings` 夹具：那个夹具会把 `_FACTORY` 摘掉，
    而这里要验的正是"真实的出厂文件里那把钥匙不会被回传"。
    """
    view = llm.factory_view()
    blob = json.dumps(view, ensure_ascii=False)
    factory = llm._read_defaults()
    raw = str((factory.get("remote") or {}).get("api_key") or "")
    assert raw, "出厂层没带密钥，这条断言就失去意义了"
    assert raw not in blob
    assert view["present"] is True
    assert view["api_key_masked"] == llm.mask_key(raw)


# ---------------------------------------------------------------- 密钥要求
def test_local_provider_is_enabled_without_a_key(monkeypatch, online):
    """本地 Ollama 免密钥 —— 没有密钥也必须是在线。"""
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "API_KEY", "")
    assert llm.api_key_required() is False
    assert llm.enabled() is True, "本地模型没有密钥，不该因为'没密钥'被关掉"


def test_remote_provider_needs_a_key(monkeypatch, online):
    """远端 provider 没密钥必须直接关掉 —— 否则会发一次注定 401 的请求。"""
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "API_KEY", "")
    assert llm.api_key_required() is True
    assert llm.enabled() is False


def test_remote_provider_with_a_key_is_enabled(monkeypatch, online):
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "API_KEY", "sk-dummy")
    assert llm.enabled() is True


def test_global_off_switch_wins_over_everything(monkeypatch):
    """FT_LLM=off 必须无条件关闭 —— 离线评测档不能因为 provider 是本地就漏掉。"""
    monkeypatch.setenv("FT_LLM", "off")
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "API_KEY", "")
    assert llm.enabled() is False


# ---------------------------------------------------------------- 端点自检
@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:11434/v1", True),
    ("http://localhost:11434/v1", True),
    ("http://host.docker.internal:11434/v1", True),
    ("http://192.168.1.9:11434/v1", True),
    ("http://10.0.0.5:11434/v1", True),
    ("https://api.openai.com/v1", False),
    ("https://relay.example.com/v1", False),
])
def test_looks_local_recognises_local_endpoints(url, expected):
    """provider=ollama 却配了远端端点，是"改了一半"的典型症状，要能识别出来。"""
    assert llm._looks_local(url) is expected


# ---------------------------------------------------------------- HTTP 行为
class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _install_fake_urlopen(monkeypatch, seen, *, fail_first_400=False):
    """把 urlopen 换掉，记录每次请求体，并可选地让第一次调用返回 HTTP 400。"""
    def _fake(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        seen.append({"url": req.full_url, "headers": dict(req.headers), "body": body})
        if fail_first_400 and len(seen) == 1:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
        return _Resp({"choices": [{"message": {"content": '{"ok":true}'}}]})

    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake)


def test_local_provider_sends_no_authorization_header(monkeypatch, online):
    """免密钥端点**不能**带 `Authorization: Bearer `（空 token）。

    有的网关会把"提供了空凭据"当成错误凭据直接 401，而不是匿名访问 ——
    于是本地模型永远连不上，日志里只有一条被 debug 级别吞掉的 401。
    """
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "API_KEY", "")
    seen: list = []
    _install_fake_urlopen(monkeypatch, seen)

    out = llm.chat([{"role": "user", "content": "hi"}])

    assert out == '{"ok":true}'
    assert len(seen) == 1
    assert not [k for k in seen[0]["headers"] if k.lower() == "authorization"], (
        f"免密钥端点被注入了 Authorization 头: {seen[0]['headers']}"
    )


def test_remote_provider_sends_bearer_token(monkeypatch, online):
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "API_KEY", "sk-dummy")
    seen: list = []
    _install_fake_urlopen(monkeypatch, seen)

    llm.chat([{"role": "user", "content": "hi"}])

    auth = {k.lower(): v for k, v in seen[0]["headers"].items()}.get("authorization")
    assert auth == "Bearer sk-dummy"


def test_chat_url_is_built_from_the_configured_base_url(monkeypatch, online):
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "BASE_URL", "http://127.0.0.1:11434/v1")
    seen: list = []
    _install_fake_urlopen(monkeypatch, seen)

    llm.chat([{"role": "user", "content": "hi"}])

    assert seen[0]["url"] == "http://127.0.0.1:11434/v1/chat/completions"


def test_json_mode_retries_without_response_format_on_http_400(monkeypatch, online):
    """模型不支持结构化输出时会 400 —— 这与"模型不可用"是两回事。

    不能让它把熔断器打上去（那会把一个完全正常的模型关掉 120s），
    也不能直接把这次调用判死：去掉 response_format 原样重试一次，
    由 chat_json 的容错解析兜住（提示词里本来就写了"只输出 JSON"）。
    """
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    seen: list = []
    _install_fake_urlopen(monkeypatch, seen, fail_first_400=True)

    out = llm.chat_json("sys", "user", cache_key=None)

    assert out == {"ok": True}, "去掉 response_format 重试后仍应拿到解析结果"
    assert len(seen) == 2, f"应当只重试一次，实际发了 {len(seen)} 次"
    assert "response_format" in seen[0]["body"]
    assert "response_format" not in seen[1]["body"], "重试时没有去掉 response_format"
    assert not llm.breaker_open(), "一次 400 不该把熔断器打上去"


def test_http_429_still_trips_the_quota_breaker(monkeypatch, online):
    """远端额度耗尽的处理必须保持不变（本地 Ollama 永不返回 429，是死代码但别删）。"""
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "API_KEY", "sk-dummy")
    monkeypatch.setattr(llm, "CB_THRESHOLD", 1)

    def _fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake)
    llm.reset_stats()

    assert llm.chat([{"role": "user", "content": "hi"}]) is None
    assert llm.breaker_reason() == "quota"
    assert llm.STATS["rate_limited"] >= 1


def test_chat_is_a_noop_when_globally_off(monkeypatch):
    """离线档必须零网络尝试（连 urlopen 都不该被调用）。"""
    monkeypatch.setenv("FT_LLM", "off")
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("离线档发起了网络请求"))
    assert llm.chat([{"role": "user", "content": "hi"}]) is None
    assert llm.chat_json("sys", "user", cache_key=None) is None


def test_env_example_documents_the_provider_switch():
    """`.env.example` 必须写清"端点/模型名与 provider 要成套改"。

    这是最容易踩、最难查的一处：改一半的症状是 404 model not found + 静默降级。
    """
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "FT_LLM_PROVIDER" in text
    assert "ollama" in text
    assert "host.docker.internal" in text, "容器连不到宿主机 Ollama 这件事必须写在示例里"


# ------------------------------------------------- 预算超时 ≠ 服务坏了
def _raise(exc):
    def _fake(req, timeout=None):
        raise exc
    return _fake


def test_budget_truncated_timeout_does_not_trip_the_breaker(monkeypatch, online):
    """调用方给的预算比模块默认更紧 ⇒ 这是"抢时间没抢到"，不是"服务坏了"。

    为什么必须分开（2026-10-04 实测）：本地 qwen2.5:7b 跑一次真实提议要 7.6s，
    而 Agent 闭环只给它 3.6s。把这类超时记进熔断器，后果是"模型慢"升级成
    "模型死了" —— 两次排程之后熔断打开，连不赶时间的 AI 对话一起被关 120s。
    """
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "API_KEY", "")
    monkeypatch.setattr(llm, "TIMEOUT", 30.0)
    monkeypatch.setattr(llm, "CB_THRESHOLD", 1)
    monkeypatch.setattr(llm.urllib.request, "urlopen", _raise(TimeoutError("timed out")))
    llm.reset_stats()
    llm.breaker_reset()

    assert llm.chat([{"role": "user", "content": "hi"}], timeout=3.6) is None

    assert llm.breaker_open() is False, (
        "把'预算不够'记成'服务坏了'，会把 AI 对话/通知一起关掉"
    )
    assert llm.STATS["deadline_miss"] == 1, "预算落空必须单独计数，否则它不可见"
    assert llm.STATS["failures"] == 0


def test_budget_truncated_timeout_is_recognised_through_urlerror(monkeypatch, online):
    """真实链路上超时是被 `URLError` 包着抛出来的，不能只认裸 TimeoutError。"""
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "TIMEOUT", 30.0)
    monkeypatch.setattr(llm, "CB_THRESHOLD", 1)
    err = urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr(llm.urllib.request, "urlopen", _raise(err))
    llm.reset_stats()
    llm.breaker_reset()

    assert llm.chat([{"role": "user", "content": "hi"}], timeout=3.6) is None
    assert llm.STATS["deadline_miss"] == 1
    assert llm.breaker_open() is False


def test_timeout_at_the_full_budget_still_trips_the_breaker(monkeypatch, online):
    """模块默认预算下还超时 ⇒ 服务真的不响应，必须熔断（原有行为不能丢）。"""
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "TIMEOUT", 3.0)
    monkeypatch.setattr(llm, "CB_THRESHOLD", 1)
    monkeypatch.setattr(llm.urllib.request, "urlopen", _raise(TimeoutError("timed out")))
    llm.reset_stats()
    llm.breaker_reset()

    assert llm.chat([{"role": "user", "content": "hi"}]) is None      # 不给 timeout

    assert llm.breaker_open() is True, "默认预算下超时说明服务真的不响应，不该被豁免"
    assert llm.STATS["deadline_miss"] == 0
    assert llm.STATS["failures"] == 1


# ------------------------------------------------- 在线闭环闸门
class _Res:
    def __init__(self, calls, ms, elapsed=3700.0):
        self.llm_calls = calls
        self.llm_ms = ms
        self.elapsed_ms = elapsed


def test_online_gate_closes_on_a_single_deadline_miss(monkeypatch):
    """闭环里只要**有一次**调用没能在预算内返回，就落闸门。

    实测场景（本地 qwen2.5:7b）：短提示词（目标塑形）3.3s 能压进 3.6s 预算，
    长提示词（策略提议）要 7.6s、必落空。只看"有没有成功过"会被那个 3.3s 的成功
    骗过去 —— 闸门不落，之后每次排程都要为提议白等满预算（闭环 3.8~4.1s vs
    规则路径 184ms），而那次提议什么都没换回来。
    """
    from app.agent import orchestrator as O
    monkeypatch.setenv("FT_LLM", "on")
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "API_KEY", "")
    llm.breaker_reset()
    llm.reset_online_gate()
    llm.reset_stats()
    llm.STATS["deadline_miss"] = 1          # 2 次调用里落空 1 次

    O._note_online_latency(True, _Res(2, 3313.0))

    assert llm.online_usable() is False
    assert "1/2" in llm.online_gate_reason(), llm.online_gate_reason()
    # 闸门只关闭环：模型本身仍是在线的，对话/通知照常用
    assert llm.enabled() is True
    assert O._llm_online(True) is False, "闸门落下后闭环仍会去调模型，等于每次白等满预算"
    llm.reset_online_gate()


def test_online_gate_closes_when_nothing_came_back(monkeypatch):
    """极端情形：闭环内每一次调用都落空。"""
    from app.agent import orchestrator as O
    monkeypatch.setenv("FT_LLM", "on")
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "API_KEY", "")
    llm.breaker_reset()
    llm.reset_online_gate()
    llm.reset_stats()
    llm.STATS["deadline_miss"] = 2

    O._note_online_latency(True, _Res(2, 0.0))

    assert llm.online_usable() is False
    llm.reset_online_gate()


def test_online_gate_stays_open_when_every_call_made_the_budget(monkeypatch):
    """全部调用都按时返回 ⇒ 不能落闸门（否则等于把在线能力白关掉）。"""
    from app.agent import orchestrator as O
    llm.reset_online_gate()
    llm.reset_stats()

    O._note_online_latency(True, _Res(2, 1500.0))
    assert llm.online_usable() is True

    llm.STATS["deadline_miss"] = 0
    O._note_online_latency(True, _Res(0, 0.0))     # 没调模型（离线/无预算）
    assert llm.online_usable() is True

    O._note_online_latency(False, _Res(3, 0.0))    # 闭环本来就没开
    assert llm.online_usable() is True


def test_online_gate_can_be_disabled_by_env(monkeypatch):
    """`FT_LLM_ONLINE_GATE=off` 时无视闸门 —— 想每次都硬试的人得有路可走。"""
    llm.reset_online_gate()
    llm.mark_online_too_slow("测试")
    try:
        assert llm.online_usable() is False
        monkeypatch.setenv("FT_LLM_ONLINE_GATE", "off")
        assert llm.online_usable() is True
    finally:
        llm.reset_online_gate()


def test_online_gate_reason_is_exposed_by_the_status_endpoint():
    """闸门状态必须能从 `/api/llm_status` 看出来。

    否则用户看到 `state=online` 却"排程没走模型"，无从判断到底哪里被关了 ——
    这正是"接了本地模型但感觉没用上"最可能的现场。
    """
    from app.api import main as main_mod
    d = main_mod.llm_status()
    assert "online_gate_ok" in d
    assert "online_gate_reason" in d


# ---------------------------------------------------------------------------
# 模型设置的持久化：两个"写坏了也不报错"的地方
# ---------------------------------------------------------------------------
def test_cache_flush_is_atomic(tmp_path, monkeypatch):
    """缓存落盘必须原子：中途失败不能把**已有文件**截断。

    `data/llm_agent_cache.json` 是 100+ 道菜的语义先验，靠 `_cache_load()` 一次读入。
    非原子写（`open(path,"w")` 先截断再写）被 kill / 磁盘满时会留下半截 json，
    下次启动解析失败 → `except: _CACHE = {}` → **整份缓存丢弃**，
    表现是"每道菜都重新调模型、系统莫名变慢"，而现场没有任何报错。

    这里用一个不可 JSON 序列化的值（set）在**写出过程中**触发异常，
    复现"写了一半就炸"的现场：原文件必须一个字节都没动。
    """
    p = tmp_path / "cache.json"
    original = '{"prior::旧菜": {"ok": 1}}'
    p.write_text(original, encoding="utf-8")

    monkeypatch.setattr(llm, "_CACHE_PATH", str(p))
    monkeypatch.setattr(llm, "_CACHE", {"prior::新菜": {1, 2, 3}})   # set 不可序列化
    monkeypatch.setattr(llm, "_CACHE_DIRTY", True)

    llm.cache_flush()          # 内部吞掉异常，但**不能**留下半截文件

    assert p.read_text(encoding="utf-8") == original, (
        "写盘失败时原缓存文件被破坏了（非原子写）—— 下次启动会整份丢弃"
    )


def test_illegal_model_name_in_settings_file_falls_back_to_default():
    """设置文件里的模型名必须过 `_MODEL_RE`，非法就回落默认。

    `data/llm_settings.json` 是用户能直接编辑的。手改出一个带空格 / 整段中文说明的
    值时，旧实现会**原样采用**，之后所有调用都带着这个不存在的模型名打到 Ollama
    拿 404 —— 而 llm 的失败是静默降级，现场只剩"选了模型但 AI 毫无反应"。

    环境变量那一层**不覆盖**（部署者写了就是他的意图，代码不该悄悄改掉）。

    ⚠ 这里显式传 `factory={}`：本用例验的是"用户层非法 ⇒ 回落**代码兜底档**"。
    不传的话缺省会用真实出厂层，期望值就得跟着运营数据走（见 tmp_settings 夹具的说明）。
    """
    # ⚠ 期望值必须跟着**实际传进去的 provider** 走，不能取 `llm._DEFAULTS[llm.PROVIDER]`。
    #   `llm.PROVIDER` 是 import 时从本机 `data/llm_settings.json` 算出来的全局量，
    #   本机设置文件里是 openai 时它就成了 "gpt-4o-mini"，而这里传的是 "ollama"，
    #   断言必然错位（2026-10-03 实测就是这么红的）。
    default = llm._DEFAULTS["ollama"][1]

    assert llm._resolve_model("", {"model": "qwen2.5:7b"}, "ollama", {}) == \
        ("qwen2.5:7b", "settings")
    assert llm._resolve_model("", {"model": "带空格的 名字"}, "ollama", {}) == (default, "default")
    assert llm._resolve_model("", {"model": "整段中文说明文字"}, "ollama", {}) == (default, "default")
    assert llm._resolve_model("", {"model": ""}, "ollama", {}) == (default, "default")

    # env 优先级最高；非法时仍然用它，只是会打一条 warning
    assert llm._resolve_model("env-model:1b", {"model": "settings-model:7b"}, "ollama", {}) == \
        ("env-model:1b", "env")
    assert llm._resolve_model("非法 环境值", {"model": "settings-model:7b"}, "ollama", {}) == \
        ("非法 环境值", "env")

    # 用户层的模型名非法 ⇒ 落到**出厂层**（不是直接跳到代码默认）——
    # 否则一次手改就会把随包配好的模型名也一起丢掉。
    fac = {"models": {"ollama": "factory-model:1b"}}
    assert llm._resolve_model("", {"model": "带空格的 名字"}, "ollama", fac) == \
        ("factory-model:1b", "factory")
    # 出厂层也非法 ⇒ 才落到代码默认
    assert llm._resolve_model("", {"model": "带空格的 名字"}, "ollama",
                              {"models": {"ollama": "也 非法"}}) == (default, "default")


def test_model_resolution_prefers_the_per_provider_entry():
    """**必须优先读 `models[provider]`，而不是全局的 `model`。**

    设置文件里同时存着两样东西：`models` = "每种接入方式各自记住的模型"，
    `model` = "当前生效的那一个"。只看 `model` 会出真事故：

        用户在设置页切到远程 → 文件里 `model = "gpt-5.6-sol"`
        之后在 .env 里用 FT_LLM_PROVIDER=ollama 把接入方式钉回本地
        → 全局 model 仍是远程那个名字
        → 请求带着 gpt-5.6-sol 打到本机 11434，Ollama 返回 404 model not found
        → 所有调用点**静默降级**，页面不报任何错

    现象是"部署时钉了本地，AI 就再也不动了"，没有这条断言根本查不出来。
    """
    saved = {"provider": "openai", "model": "gpt-5.6-sol",
             "models": {"ollama": "qwen2.5:7b", "openai": "gpt-5.6-sol"}}

    assert llm._resolve_model("", saved, "ollama", {}) == ("qwen2.5:7b", "settings"), (
        "钉回本地后仍拿着远程的模型名 —— 会打到本机 Ollama 拿 404 并静默降级"
    )
    assert llm._resolve_model("", saved, "openai", {}) == ("gpt-5.6-sol", "settings")


def test_model_resolution_falls_back_to_the_legacy_single_model_field():
    """老版本只写了全局 `model`（没有 `models` 映射）时仍要能读出来。"""
    assert llm._resolve_model("", {"model": "qwen2.5:7b"}, "ollama", {}) == ("qwen2.5:7b", "settings")
    assert llm._resolve_model("", {"models": "坏形状"}, "ollama", {}) == \
        (llm._DEFAULTS["ollama"][1], "default"), "坏形状的 models 要当空，不能炸"


# ---------------------------------------------------------------- 运行期切换
# 设置页可以在「本地 Ollama / 远程 OpenAI 兼容端点」之间切换（2026-10-03）。
# 这条路径上有三类**静默失败**，都不会让别的测试变红，所以逐条钉住：
#   ① 密钥被回传 —— `/api/model_settings` 是无鉴权公开的，回传等于把凭据挂公网；
#   ② 空串被当成"清空" —— 密钥框永远是空的（我们从不回填），
#      把空读成清空 ⇒ 用户每保存一次就抹掉一次自己的配置；
#   ③ 切换后不重算超时 / 不重置熔断 —— 远端档沿用 3s 会把实测 2.3~2.9s 的
#      中转站调用随机截断（"聊天时好时坏"），而旧模型的熔断结论会挡住新模型。
#
# 顺带：这些用例必须先把 `_ENV_*` 快照清空 —— 它们是在**模块导入时**读进模块级
# 常量的，conftest / .env 里任何一个写了 FT_LLM_PROVIDER，测的就变成"锁定分支"
# 而不是"切换分支"了。
_SETTING_KEYS = ("PROVIDER", "BASE_URL", "API_KEY", "MODEL", "MODEL_SOURCE",
                 "TIMEOUT", "WARMUP_BUDGET")


@pytest.fixture
def tmp_settings(monkeypatch, tmp_path):
    """把设置文件指到临时目录、清掉环境变量锁，并在**用例开始前**把模块级配置
    复位到"没有设置文件"的基线；用例结束后再还原成进来时的样子。

    ⚠ 为什么必须"开始前复位"，而不是只"用完还原"
    ---------------------------------------------
    `PROVIDER` / `MODEL` / `API_KEY` / `TIMEOUT` 这些是在 **import 时**由
    `llm.py` 第 282~285 行从 `data/llm_settings.json` 算出来的**模块级全局量**。
    而那个文件是**用户能直接改的**（设置页保存就写它）。

    所以如果夹具只"用完还原"，用例拿到的就是**开发者本机那份配置** ——
    测试结果变成"这台机器上谁最后动过设置页"的函数。2026-10-03 真实翻车：
    上一轮做端到端实测时把接入方式切成了 `openai` 并落盘，
    于是本文件 6 条用例开始报 `assert 'openai' == 'ollama'`、
    `assert '' == 'qwen2.5:7b'` —— 而代码本身一行都没错。

    复位用 `llm._apply({})`：它正是 `set_provider` / `set_model` 落盘后走的
    同一条重算路径，能把全局量算回"空设置文件 + 空出厂层"下的默认值
    （provider=ollama、model=qwen2.5:7b、api_key 空），不另造一套口径。
    """
    monkeypatch.setattr(llm, "_SETTINGS_PATH", str(tmp_path / "llm_settings.json"))
    # 出厂层也要摘掉。它的内容是仓库自带的（确定，不是"本机状态"），但本文件里
    # 大量用例断言的是**代码兜底档**（provider=ollama、model=qwen2.5:7b、api_key 空、
    # 校验失败后 provider 不变）。出厂层一旦参与解析，那些断言的期望值就得跟着
    # 出厂文件的内容走 —— 而那个文件是**运营数据**，改一次中转站就要改一片测试。
    # 所以这里把它摘掉，出厂层的行为由 test_factory_defaults_* 一组专门覆盖。
    monkeypatch.setattr(llm, "_FACTORY", {})
    for name in ("_ENV_PROVIDER", "_ENV_BASE_URL", "_ENV_API_KEY",
                 "_ENV_MODEL", "_ENV_TIMEOUT", "_ENV_WARMUP"):
        monkeypatch.setattr(llm, name, "")
    before = {k: getattr(llm, k) for k in _SETTING_KEYS}

    # ↓ 用例开始前复位到基线（顺序很重要：先清 _ENV_*，再 _apply）
    #   `_apply` 只重算 PROVIDER/BASE_URL/API_KEY/MODEL/MODEL_SOURCE 五个量；
    #   TIMEOUT / WARMUP_BUDGET 由 `_recompute_timeouts()` 按当前 provider 另算，
    #   所以两个都要调，否则超时那两项会留着上一个 provider 的值。
    llm._apply({})
    llm._recompute_timeouts()
    llm.breaker_reset()
    llm.reset_online_gate()
    yield
    for k, v in before.items():
        setattr(llm, k, v)
    llm.breaker_reset()
    llm.reset_online_gate()


def test_tmp_settings_gives_every_case_a_clean_slate(tmp_settings):
    """守夹具本身：用例开始时必须是**默认**配置，与本机 `data/llm_settings.json` 无关。

    这条断言在"夹具只还原、不复位"时会红 —— 只要本机设置文件被改过
    （比如刚在设置页切过接入方式），这正是上面那次翻车的现场。
    """
    assert llm.PROVIDER == "ollama", (
        "夹具没有在用例前复位接入方式 —— 本机 data/llm_settings.json 的内容泄漏进来了"
    )
    assert llm.MODEL == llm._DEFAULTS["ollama"][1]
    assert llm.API_KEY == ""
    assert llm.BASE_URL == llm._DEFAULTS["ollama"][0]
    assert llm.TIMEOUT == llm._DEFAULT_TIMEOUT["ollama"]


def test_switch_to_remote_takes_effect_immediately(tmp_settings):
    """切到远程：端点 / 密钥 / 模型立刻生效，并**重算超时默认值**。

    超时不重算就是那个"贴着线"的坑：远端档旧默认 3.0s，而中转站实测 2.3~2.9s
    ⇒ 时好时坏，且现场没有任何报错（llm 的失败是静默降级）。
    """
    res = llm.set_provider("openai", base_url="https://relay.example/v1/",
                           api_key="sk-test-key", model="gpt-5.6-sol")

    assert res["ok"] is True, res
    assert llm.PROVIDER == "openai"
    assert llm.BASE_URL == "https://relay.example/v1", "尾部斜杠必须被裁掉，否则拼出 //chat/completions"
    assert llm.API_KEY == "sk-test-key"
    assert llm.MODEL == "gpt-5.6-sol"
    assert llm.api_key_required() is True
    assert llm.TIMEOUT == llm._DEFAULT_TIMEOUT["openai"], "超时默认值必须跟着 provider 换"


def test_switch_remembers_each_providers_model(tmp_settings):
    """两种接入方式各自记住自己的模型 —— 来回切不该把对方的模型名带过去。"""
    llm.set_provider("openai", base_url="https://relay.example/v1",
                     api_key="sk-test-key", model="gpt-5.6-sol")
    assert llm.MODEL == "gpt-5.6-sol"

    res = llm.set_provider("ollama")
    assert res["ok"] is True, res
    assert llm.PROVIDER == "ollama"
    assert llm.MODEL == llm._DEFAULTS["ollama"][1], (
        "切回本地却还拿着远程的模型名 —— 会打到 11434 拿 404 并静默降级"
    )
    assert llm.API_KEY == "", "本地档绝不能把远程密钥继续挂在 Authorization 上发出去"

    assert llm.set_provider("openai")["ok"] is True
    assert llm.MODEL == "gpt-5.6-sol", "切回远程时该恢复它自己那个模型"
    assert llm.BASE_URL == "https://relay.example/v1"


def test_remote_view_never_leaks_the_api_key(tmp_settings):
    """`remote_view()` 只报"配没配"，**绝不回传密钥**。

    `/api/model_settings` 是无鉴权的（本机演示 / 发布出去的链接都能读）——
    回传密钥等于把用户的凭据挂在公网上。这条断言比"记得删掉那个字段"可靠：
    它检查的是**整份序列化结果里不含密钥串**，以后谁加字段都躲不过。
    """
    llm.set_provider("openai", base_url="https://relay.example/v1",
                     api_key="sk-secret-should-never-leak", model="gpt-5.6-sol")

    view = llm.remote_view()
    blob = json.dumps(view, ensure_ascii=False)

    assert "sk-secret-should-never-leak" not in blob
    assert "api_key" not in view, "字段本身就不该存在（存在就有被误用的可能）"
    assert view["api_key_set"] is True
    assert view["base_url"] == "https://relay.example/v1"
    assert view["model"] == "gpt-5.6-sol"
    # 打码预览是**允许**回传的（只够辨认，不足以调用），而且必须真的打码了
    assert view["api_key_masked"] == "sk-sec…leak"
    assert view["api_key_masked"] != "sk-secret-should-never-leak"


def test_model_settings_endpoint_never_returns_the_key(tmp_settings, monkeypatch):
    """端点级复核：`GET /api/model_settings` 的整个响应体里不许出现密钥串。"""
    from app.api import main as M
    # 绕开真实网络：installed_models 会去连本机 Ollama
    monkeypatch.setattr(llm, "installed_models", lambda *a, **k: (False, [], "未接入"))
    llm.set_provider("openai", base_url="https://relay.example/v1",
                     api_key="sk-secret-should-never-leak", model="gpt-5.6-sol")

    payload = M.get_model_settings()
    blob = json.dumps(payload, ensure_ascii=False)

    assert "sk-secret-should-never-leak" not in blob
    assert payload["provider"] == "openai"
    assert payload["is_local"] is False
    assert payload["remote"]["api_key_set"] is True
    # 打码预览可以回传（只够辨认是哪把钥匙），但绝不能是原文
    assert payload["remote"]["api_key_masked"] == "sk-sec…leak"
    # 出厂层现状要能被前端读到 —— 界面靠它说清"远程那一栏用的是随程序附带的预置"
    assert "factory_defaults" in payload
    assert set(payload["factory_defaults"]) >= {"present", "provider", "base_url",
                                                "model", "api_key_set", "api_key_masked"}
    # 选项清单来自后端 —— 前端不许自己再写一份（那样加一种接入方式时必有一边漏改）
    assert {p["id"] for p in payload["providers"]} == {"ollama", "openai"}
    assert payload["models_by_provider"]["openai"] == "gpt-5.6-sol"


def test_blank_api_key_means_keep_it_not_clear_it(tmp_settings):
    """空串 = "保持原样"，**不是**"清空"。

    界面上密钥框永远是空的（我们从不回填），把空串当清空 ⇒
    用户每保存一次就把自己的密钥抹掉一次，而且是静默的。
    """
    llm.set_provider("openai", base_url="https://relay.example/v1",
                     api_key="sk-keep-me", model="gpt-5.6-sol")

    res = llm.set_provider("openai", base_url="https://relay2.example/v1",
                           api_key="", model="gpt-5.6-sol")

    assert res["ok"] is True, res
    assert llm.API_KEY == "sk-keep-me", "空密钥把已配置的密钥抹掉了"
    assert llm.BASE_URL == "https://relay2.example/v1", "非空字段仍要能改"


def test_switch_rejects_a_bad_base_url(tmp_settings):
    """接口地址必须以 http(s):// 开头 —— 否则请求会发到相对路径上，全是静默 404。"""
    for bad in ("ftp://relay.example/v1", "relay.example/v1", "file:///etc/passwd"):
        res = llm.set_provider("openai", base_url=bad, api_key="sk-x")
        assert res["ok"] is False, f"{bad!r} 不该被接受"
        assert "http" in res["error"]
        assert llm.PROVIDER == "ollama", f"{bad!r}: 校验失败却已经把接入方式改掉了"
        assert not os.path.exists(llm._SETTINGS_PATH), f"{bad!r}: 校验失败却已经写盘"


def test_blank_base_url_keeps_the_previous_one(tmp_settings):
    """纯空白 = 没填 = 保持原样（不报错，也不清空）。"""
    assert llm.set_provider("openai", base_url="https://relay.example/v1",
                            api_key="sk-x", model="m-1")["ok"] is True

    res = llm.set_provider("openai", base_url="   ")

    assert res["ok"] is True, res
    assert llm.BASE_URL == "https://relay.example/v1", "空白地址把已保存的地址清掉了"


def test_switch_rejects_an_unknown_provider(tmp_settings):
    """不认识的接入方式必须**显式报错**，不能静默回落 —— 静默回落会让用户
    以为"选上去了"，而实际跑的是另一种接入方式。"""
    res = llm.set_provider("gemini", model="gemini-pro")

    assert res["ok"] is False
    assert "gemini" in res["error"]
    assert llm.PROVIDER == "ollama"


def test_switch_rejects_an_illegal_model_name(tmp_settings):
    res = llm.set_provider("openai", api_key="sk-x", model="带空格的 模型名")
    assert res["ok"] is False
    assert "非法" in res["error"]


def test_switch_resets_the_breaker_and_the_online_gate(tmp_settings):
    """切换后必须重置熔断与闸门 —— 它们记的是**上一个模型**的结论。

    不重置的话：用户刚换到一个快模型，闭环却仍被闸门挡着，表现为
    "换了模型还是不走模型"，而怎么看状态都是 online。
    """
    llm.force_open("warmup")
    llm.mark_online_too_slow("旧模型太慢")
    assert llm.breaker_open() is True and llm.online_usable() is False

    assert llm.set_provider("openai", base_url="https://relay.example/v1",
                            api_key="sk-x", model="gpt-5.6-sol")["ok"] is True

    assert llm.breaker_open() is False
    assert llm.online_usable() is True


def test_env_provider_locks_the_switch_but_not_the_model(monkeypatch, tmp_settings):
    """`FT_LLM_PROVIDER` 钉死的是**接入方式**，不是模型。

    容器里钉死 provider 是常见做法（镜像里没有 Ollama）。锁定要能拦住切换，
    但"在同一个接入方式里换模型"必须照常可用 —— 否则一个环境变量会把整个
    设置页的模型选择也一起废掉。
    """
    monkeypatch.setattr(llm, "_ENV_PROVIDER", "ollama")

    res = llm.set_provider("openai", base_url="https://relay.example/v1", api_key="sk-x")
    assert res["ok"] is False
    assert "FT_LLM_PROVIDER" in res["error"]
    assert llm.PROVIDER == "ollama"

    assert llm.set_provider("ollama", model="qwen2.5:14b")["ok"] is True
    assert llm.MODEL == "qwen2.5:14b"


def test_env_timeout_wins_over_the_per_provider_default(monkeypatch, tmp_settings):
    """`FT_LLM_TIMEOUT` 一旦显式给了，就不许被 provider 默认值盖掉。"""
    monkeypatch.setattr(llm, "_ENV_TIMEOUT", "7.5")

    assert llm.set_provider("openai", base_url="https://relay.example/v1",
                            api_key="sk-x", model="gpt-5.6-sol")["ok"] is True

    assert llm.TIMEOUT == 7.5


def test_models_by_provider_reports_both_slots(tmp_settings):
    """回填用的映射：两种接入方式各自的模型都要能看到。"""
    llm.set_provider("openai", base_url="https://relay.example/v1",
                     api_key="sk-x", model="gpt-5.6-sol")

    by = llm.models_by_provider()
    assert set(by) == {"ollama", "openai"}
    assert by["openai"] == "gpt-5.6-sol"
    assert by["ollama"] == llm._DEFAULTS["ollama"][1]


def test_settings_file_keeps_the_remote_block_across_switches(tmp_settings):
    """落盘内容要能被重新解析出来（切回去不丢配置）。"""
    llm.set_provider("openai", base_url="https://relay.example/v1",
                     api_key="sk-x", model="gpt-5.6-sol")
    llm.set_provider("ollama")

    raw = json.loads(open(llm._SETTINGS_PATH, encoding="utf-8").read())
    assert raw["provider"] == "ollama"
    assert raw["remote"]["base_url"] == "https://relay.example/v1"
    assert raw["remote"]["api_key"] == "sk-x", "切到本地不该把远程密钥从盘上删掉"
    assert raw["models"]["openai"] == "gpt-5.6-sol"


def test_settings_path_can_be_redirected_by_env(tmp_path):
    """`FT_LLM_SETTINGS_FILE` 必须能把**用户层**重定向到别处。

    ⚠ 这不是"锦上添花"的开关，而是**探针能不能安全跑**的前提。没有它：
    `scripts/ui_click_audit.mjs` 遍历控件时会切一下「接入方式」下拉、再在模型
    输入框上按个回车触发 `saveModel()` —— 于是开发机上真实的
    `data/llm_settings.json` 被改写。而密钥与远程地址走的是"留空 = 保持原值"，
    所以**看起来什么都没坏**，用户下次打开页面才发现接入方式被换掉了。
    （2026-10-08 实测踩到：provider 从 openai 变成 ollama、model 变成本机下拉里那个。）

    与 `FT_DEVICES_FILE` 同一套纪律：conftest 在 import 期把它钉到一个不存在的路径。
    """
    import importlib

    orig = os.environ.get("FT_LLM_SETTINGS_FILE")
    try:
        os.environ["FT_LLM_SETTINGS_FILE"] = str(tmp_path / "llm_settings.json")
        reloaded = importlib.reload(llm)
        assert reloaded._SETTINGS_PATH == str(tmp_path / "llm_settings.json"), (
            "env 覆盖没生效 —— 用户层还指在 data/llm_settings.json 上，探针会改到真配置"
        )
    finally:
        # 显式还原（不能只靠 monkeypatch 的 teardown：这里还多做了一次 reload）
        if orig is None:
            os.environ.pop("FT_LLM_SETTINGS_FILE", None)
        else:
            os.environ["FT_LLM_SETTINGS_FILE"] = orig
        importlib.reload(llm)

    assert llm._SETTINGS_PATH == orig, "还原后路径没回到隔离值 —— 会把后面的用例带偏"
