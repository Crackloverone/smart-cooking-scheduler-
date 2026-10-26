"""
启动预热（`app/api/main.py::_warmup`）的回归守卫。

为什么单独测这一块
------------------
预热的第 3 步是"模型可用性探测"，它决定**熔断器要不要在启动时就落定**。
落不定的代价全部由**第一个真实请求**承担 —— 实测：

    首个 /schedule   6735~7813 ms   （LLM 调用 2 次，每次都等满超时）
    之后同一实例       150~228 ms   （熔断已打开，0 次调用）

也就是 <5s 的性能预算被首请求一口吃掉。而两个坑都很隐蔽：

1. **探测路径必须与 Agent 一致。** 老实现用纯文本 `llm.chat([...ping])` 探，
   而 Agent 走 `llm.chat_json(..., json_mode=True)`。请求体不同，可能一个通一个不通。
2. **判据是"能否在预算内返回"，不是"连不连得上"。** 实测模型端可连通，
   但单次响应 2.3~4.0s，而 `FT_LLM_TIMEOUT` 默认 3.0s —— 调用被超时截断，
   探测却报"模型可用"，首请求照样白等。
   Agent 一次排程会发 1~2 次调用，单次 >1.5s 就落不进 <5s，所以探测预算必须收紧。

这两条一旦被改回去，功能测试全绿、只有"首请求慢 40 倍"，极难被发现 —— 所以钉在这里。
"""
from __future__ import annotations

import pytest

from app.agent import llm
from app.api import main


@pytest.fixture
def reset_breaker(monkeypatch):
    """把熔断器/在线闸门复位，并让预热的前两步（菜谱预热）空转，只留模型探测。"""
    monkeypatch.setattr(llm, "_CB", {"fails": 0, "open_until": 0.0, "reason": ""})
    llm.reset_online_gate()
    # 闸门本身可被 FT_LLM_ONLINE_GATE=off 绕过（开发机上可能 export 了），
    # 不清掉的话"闸门该不该落下"就取决于谁的 shell 了。
    monkeypatch.delenv("FT_LLM_ONLINE_GATE", raising=False)
    monkeypatch.setenv("FT_WARM_LLM", "on")
    monkeypatch.setattr(main, "all_recipe_names", lambda: [])
    monkeypatch.setattr(main, "plan_recipes", lambda *a, **k: None)
    yield


def test_warmup_probes_via_the_same_path_as_the_agent(monkeypatch, reset_breaker):
    """探测必须走 chat_json（和 Agent 同一条路），不能只发纯文本。"""
    seen: dict = {}

    def fake_chat_json(system, user, **kw):
        seen["system"] = system
        seen["user"] = user
        seen.update(kw)
        return None

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: pytest.fail(
        "预热不该用纯文本 llm.chat 探测 —— 它与 Agent 的调用路径不一致"))

    main._warmup()

    assert seen, "预热必须真的探测模型，否则首请求要自己攒够失败次数"
    assert seen.get("timeout") is not None


def test_warmup_probe_budget_is_tight_enough_for_the_scoring_line(monkeypatch, reset_breaker):
    """远端 provider 的探测预算必须收紧：单次调用超过 ~1.5s 就落不进 <5s 的评分线。

    ⚠ 这条只对 `openai`（远端中转站/云厂商）成立。**本地 Ollama 的预算是 60s**，
    理由不是"本地更快"，而是"本地**更慢**"—— 它首次调用要把权重读进显存
    （实测 7B/Q4 ≈10s）。拿 1.5s 去探本地模型，结果是每次启动都自我熔断、
    本地模型永远用不上。所以判据必须按 provider 分档，不能一刀切
    （两条默认值本身由 test_provider_default_budgets_are_pinned 钉住）。
    """
    from app.agent import llm
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "TIMEOUT", 3.0)
    monkeypatch.setattr(llm, "WARMUP_BUDGET", 1.5)
    seen: dict = {}
    monkeypatch.setattr(llm, "chat_json",
                        lambda s, u, **kw: (seen.update(kw), None)[1])

    main._warmup()

    budget = seen.get("timeout")
    assert budget is not None, "探测必须显式给超时预算"
    assert budget <= 1.5, (
        f"探测预算 {budget}s 太松：模型端实测 2.3~4.0s，"
        "给到 3s 会让探测误判为可用，首请求仍要白等超时"
    )


def test_local_provider_budget_tolerates_a_cold_model_load(monkeypatch, reset_breaker):
    """本地 provider 的探测预算必须容得下"把模型读进显存"这一次冷启动。

    判据用**实测值**：Ollama 加载 qwen2.5:7b(Q4_K_M) 约 10s，权重被换出后重载
    同样量级。1.5s 或 3s 的预算都会失败 ⇒ 启动即熔断 ⇒ 本地模型形同虚设。
    同时必须**不超过单次调用超时**（main 里取 min(TIMEOUT, WARMUP_BUDGET)）：
    一个只在超过单次超时之后才通过的探测，是在说假话 —— 真实调用照样会超时。
    """
    from app.agent import llm
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "TIMEOUT", 30.0)
    monkeypatch.setattr(llm, "WARMUP_BUDGET", 60.0)
    seen: dict = {}
    monkeypatch.setattr(llm, "chat_json",
                        lambda s, u, **kw: (seen.update(kw), ["ok"])[1])

    main._warmup()

    budget = seen.get("timeout")
    assert budget is not None, "探测必须显式给超时预算"
    assert budget >= 10.0, (
        f"本地探测预算只有 {budget}s，装不下 ~10s 的冷启动 ⇒ 每次启动都会误熔断"
    )
    assert budget <= llm.TIMEOUT, (
        f"探测预算 {budget}s 超过了单次调用超时 {llm.TIMEOUT}s —— "
        "'探测通过'就成了假话，真实调用一样会超时"
    )


def test_provider_default_budgets_are_pinned():
    """默认预算必须按 provider 分流 —— 这是本次改造的核心结论，钉在代码里。

    写成断言而不是只写注释：以后有人"顺手统一一下默认值"会立刻红。
    顺带钉住"密钥要求也按 provider 分流"：本地 Ollama 免密钥，
    若这条被改回 `bool(API_KEY)`，本地模型会被永久判成"未配置"并静默降级。
    """
    from app.agent import llm
    assert set(llm._DEFAULT_TIMEOUT) == {"ollama", "openai"}, "超时默认值必须两个 provider 各一份"
    assert set(llm._DEFAULT_WARMUP) == {"ollama", "openai"}, "探测预算必须两个 provider 各一份"
    assert llm._DEFAULT_WARMUP["ollama"] >= 10.0 > llm._DEFAULT_WARMUP["openai"], (
        "本地探测预算必须显著大于远端档 —— 本地慢在冷启动，远端慢在 RTT"
    )
    assert llm._DEFAULT_TIMEOUT["ollama"] > llm._DEFAULT_TIMEOUT["openai"], (
        "本地单次超时必须更宽松：权重被换出后重载要 ~10s，3s 会把正常模型判成故障"
    )
    assert llm.api_key_required() is (llm.PROVIDER != "ollama")


def test_warmup_force_opens_breaker_when_model_misses_the_budget(monkeypatch, reset_breaker):
    """**宽预算档**（本地 60s）探测超时 → 必须立刻熔断，把代价从"第一个真实请求"挪到"启动"。

    判据是"预算有多紧"而不是 provider 叫什么名字（见 llm.probe_miss_is_fatal）：
    本地档给 60s 是为了容下 ~10s 的权重冷加载，连它都答不出来 = 模型没装 / 服务没起，
    那就是真故障，熔断是对的。
    """
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "WARMUP_BUDGET", 60.0)
    monkeypatch.setattr(llm, "chat_json", lambda s, u, **kw: None)

    main._warmup()

    assert llm.breaker_open(), "探测失败却没熔断 ⇒ 第一个真实请求要自己攒失败次数"
    assert llm.breaker_reason() == "warmup"


def test_warmup_on_a_tight_budget_only_closes_the_online_loop(monkeypatch, reset_breaker):
    """**紧预算档**（远端 1.5s）探测超时 → 只落在线闭环闸门，**绝不能熔断**。

    这是 2026-10-03 接土豆中转站时实测到的真问题：中转站单次往返 2.3~2.9s，
    而远端档的探测预算是 1.5s（为了对齐 <5s 的性能线）⇒ **每次启动都必然超时**。
    若照旧 force_open，后果是每次重启都把 **AI 对话** 一起关掉 120s
    —— 而那恰好是用户第一次点它的时刻，表现成"接了远程模型，AI 完全没反应"。

    正确做法：只关闭环（排程立刻改走规则，不再白等），对话照常用模型。
    """
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "WARMUP_BUDGET", 1.5)
    monkeypatch.setattr(llm, "TIMEOUT", 20.0)
    monkeypatch.setattr(llm, "chat_json", lambda s, u, **kw: None)

    main._warmup()

    assert llm.breaker_open() is False, (
        "紧预算超时不是故障：熔断会把 AI 对话也一起关掉 120s"
    )
    assert llm.online_usable() is False, (
        "闭环必须被关掉 —— 否则每次排程都要先白等满预算才回落规则"
    )
    assert "1.5" in llm.online_gate_reason(), llm.online_gate_reason()


def test_probe_miss_severity_follows_the_budget_not_the_provider_name(monkeypatch):
    """`probe_miss_is_fatal()` 的判据是预算宽窄，不是 provider 名字。

    这样"把 FT_WARM_LLM_BUDGET 调到 1.5s 的本地档"也不会在启动时把对话一起关掉 ——
    规则只有一条，不会出现"provider=openai 但端点其实在局域网里"这类组合的例外。
    """
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "WARMUP_BUDGET", 60.0)
    assert llm.probe_miss_is_fatal() is True

    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "WARMUP_BUDGET", 1.5)
    assert llm.probe_miss_is_fatal() is False

    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "WARMUP_BUDGET", 30.0)
    assert llm.probe_miss_is_fatal() is True, "预算宽 = 答不出来就是真故障，与 provider 无关"


def test_warmup_probes_the_model_before_warming_the_plan_path(monkeypatch, reset_breaker):
    """探测必须排在 `plan_recipes` 预热**之前**。

    为什么顺序是个真问题（2026-10-04 实测）：`plan_recipes` 自己就会调模型 ——
    Agent 闭环的策略提议走 `timeout=min(FT_LLM_TIMEOUT, FT_AGENT_BUDGET)`
    = min(30, 3.6) = **3.6s**。本地 Ollama 冷启动要 ~10s，于是预热里的这两次
    调用必然超时并**计入熔断器**：实测启动后 `stats.failures=2 / calls=3`，
    再差一次就到阈值 3 —— 服务还没接客就先把自己熔断了，
    而那两次超时还把启动时间白白拉长 7s。

    先探测（预算 60s）再预热，模型在"第一次赶时间的调用"之前就已经加载好。
    """
    order: list[str] = []
    monkeypatch.setattr(llm, "chat_json",
                        lambda *a, **k: (order.append("probe"), ["ok"])[1])
    monkeypatch.setattr(main, "all_recipe_names", lambda: ["占位菜"])
    monkeypatch.setattr(main, "get_recipe",
                        lambda n: {"ingredients": [], "steps": ""})
    monkeypatch.setattr(main, "parse_recipe", lambda *a, **k: object())
    monkeypatch.setattr(main, "plan_recipes",
                        lambda *a, **k: order.append("plan"))

    main._warmup()

    assert order == ["probe", "plan"], (
        f"预热顺序错了: {order} —— 先跑 plan_recipes 会让冷启动的模型"
        "撞上 3.6s 的闭环预算并计入熔断器"
    )


def test_warmup_keeps_breaker_closed_when_model_is_responsive(monkeypatch, reset_breaker):
    """模型在预算内答得出来时，不能误熔断（否则在线能力被白白关掉 120s）。"""
    monkeypatch.setattr(llm, "chat_json", lambda s, u, **kw: ["ok"])

    main._warmup()

    assert not llm.breaker_open(), "模型可用却被熔断，等于把在线能力关掉了"


def test_warmup_probe_does_not_pollute_the_semantic_cache(monkeypatch, reset_breaker):
    """探测结果不能落盘 —— 否则会往语义先验缓存里塞一条垃圾。"""
    seen: dict = {}
    monkeypatch.setattr(llm, "chat_json",
                        lambda s, u, **kw: (seen.update(kw), ["ok"])[1])

    main._warmup()

    assert seen.get("cache_key") is None, (
        f"探测传了 cache_key={seen.get('cache_key')!r}，会把探针结果写进缓存"
    )


def test_warmup_respects_the_global_off_switch(monkeypatch, reset_breaker):
    """FT_WARM_LLM=off 时必须完全不探测（离线评审档不能因为探测而变慢）。"""
    monkeypatch.setenv("FT_WARM_LLM", "off")
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: pytest.fail(
        "FT_WARM_LLM=off 时不该发起探测"))

    main._warmup()

    assert not llm.breaker_open()
