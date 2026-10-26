"""pytest 全局前置：把整个测试套件钉在**离线档**。

为什么必须显式钉住（而不是"反正没配密钥就自动离线"）
----------------------------------------------------
改造前 `llm.enabled()` 要求 `bool(FT_LLM_API_KEY)`，而仓库里 `API_KEY` 默认空
⇒ 测试天然离线，于是离线开关靠"四个测试模块各自在 import 期 `os.environ[...]="off"`
的副作用"（见 test_core / test_model / test_dispatch / test_provenance）。
那套做法有两个隐患：
  · **谁先被 import 谁生效** —— 换个文件顺序、或单跑某个文件，离线档就没了；
  · 它把"离线"这件事描述成"碰巧"，而不是"要求"。

2026-10-04 默认 provider 切成**本地 Ollama** 之后，隐患变成真事故：
Ollama 是免密钥的，`enabled()` 默认为 True ⇒ 只要本机跑着 Ollama，
任何走到 enrich / prior / notify 的测试都会真的发 HTTP 请求，
结果变成"这台机器装没装模型"的函数：不可复现、还慢几十倍。

2026-10-06 加了**出厂默认配置层**（`app/agent/llm_defaults.json`，随包分发，
"发给别人就能用"）之后，这件事**更**要钉死：开箱档是 `openai` + 一把真密钥，
`enabled()` 开箱即 True ⇒ 不钉离线档的话，整个套件会真的往中转站打请求
（配额、时延、结果全部取决于网络，而且会烧掉那把钥匙的额度）。

需要真调模型的用例自己用 `monkeypatch.setenv` 打开（见 test_warmup_probe.py：
它把 `FT_WARM_LLM` 设回 on，再 monkeypatch 掉 `llm.chat_json`）。
"""
from __future__ import annotations

import os
import tempfile

import pytest

# 无条件覆盖（不是 setdefault）—— 开发机上可能已经 export 了这些变量，
# 那样"离线档"就取决于谁的 shell 了。
for _k in ("FT_LLM", "FT_AGENT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ[_k] = "off"

# 设备容量的**用户层**（`data/devices_user.json`，见 app/api/main.py）必须隔离。
#
# 为什么：只要开发机上有人从界面改过一次设备（或选过一次厨房预设），这个文件
# 就被创建了；此后 `app.api.main` 一 import 就会把那份配置套进 `RESOURCE_UNITS`
# —— 于是所有"默认 2 灶眼 / 1 蒸箱"的断言**静默**变味。报错会指向某个排程结果
# 不对，而没人会想到去看 data/ 下多出来的一个 json。
#
# ⚠ 必须在 import 期设（`tests/test_warmup_probe.py` 是模块级 import
#   `app.api.main` 的），conftest 先于所有测试模块被加载，所以这里安全。
#   指到一个**不存在**的路径 ⇒ 走"按默认设备启动"分支。
os.environ["FT_DEVICES_FILE"] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_no_such_devices_user.json")

# 模型配置的**用户层**（`data/llm_settings.json`，见 app/agent/llm.py）同样必须隔离。
#
# 为什么：这份文件记着"当前接入方式 / 模型 ID / 远程地址 / 密钥"。开发机上只要
# 从设置页保存过一次，它就在了；此后 `app.agent.llm` 一 import 就会读它 ——
# 于是所有"默认档"断言静默变味（`test_llm_provider.py` 里有一整组夹具是专门
# 复位这个的，就是因为踩过）。
#
# ⚠ 更要命的是**写**：探针/测试一旦走到 saveModel()，就会把开发机上的真实
#   接入方式改掉，而密钥/地址因为"留空=保持原值"看起来毫无异常 —— 用户下次
#   打开页面才发现模型被换了（2026-10-08 实测踩到）。
os.environ["FT_LLM_SETTINGS_FILE"] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_no_such_llm_settings.json")

# 排程历史的**用户层**（`data/schedules_user.json`，见 app/api/main.py）同理。
#
# 为什么：v3.7 起排程历史落盘。这份文件里是"开发机排过的计划"，包含完整 plan。
#   ① 读：pytest 会带着它启动 ⇒ `/api/schedules` 的计数类断言静默变味
#      （`ui_probe.mjs` 有一条"前端计数 == 后端 count"，多出来的历史会让两边
#      都对不上，报出来像渲染 bug）。
#   ② 写：**每次 /schedule 都会落盘** —— 测试跑一遍就往真仓库里塞几十条测试
#      计划，用户打开"记录"页看到的全是自己没排过的菜。
#   指到**临时目录**里的一个不存在路径 ⇒ 当空历史启动，写入也落在临时目录，
#   不会往 tests/ 或 data/ 里塞东西（这一条与另外两个隔离开关不同：设备/模型
#   配置只在"用户主动保存"时才写，而排程历史**每跑一次 /schedule 就写一次**，
#   用"conftest 旁边的固定文件名"会让 tests/ 下很快出现一份几十条测试计划的
#   json —— 那本身就是本轮要修的那类污染）。
os.environ["FT_SCHEDULES_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="ft_test_schedules_"), "schedules_user.json")

# 执行态的**用户层**（`data/exec_user.json`，v3.9 起）与排程历史同一套纪律，
# 但**更危险一档**：它不仅会被读，还会**改排程结果**。
#
# 为什么：`samples` 里攒的是 (预估, 实际) 聚合量，开着 `FT_CALIB` 时会直接乘到
#   兜底时长上 —— 开发机上随便点几次"完成/延迟"，就会让 `estimate_duration`
#   不再返回 8.0 而是 8.0×某个系数。于是**一整批排程断言集体变味**，而报错指向
#   的是排程结果，没人会想到去看 data/ 下多出来的一个 json。
#   指到临时目录 ⇒ 测试写入落在临时目录，且默认档（FT_CALIB=off）下也读不到
#   任何真实样本。
os.environ["FT_EXEC_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="ft_test_exec_"), "exec_user.json")


# ---------------------------------------------------------------------------
# 发布包护栏的**读副本** helper
# ---------------------------------------------------------------------------
# ⚠ 盯 `scripts/sync_publish.py` 的护栏（`SKIP_NAMES` 里有哪些"用户层"文件）必须
#   **优先读 `FT_WIN_DEPLOY_DIR` 下的副本**，不能写成 `from scripts import sync_publish`。
#   原因：`scripts/_mutate_deploy_guards.py` 的安全设计是"从不改动仓库里的真文件" ——
#   它把 `sync_publish.py` 拷到临时目录、只变异那份副本。直接 import 读的是真文件
#   ⇒ 变异写副本**永远不生效** ⇒ 护栏空转，而变异脚本还会报"命中"（假绿）。
#   2026-10-08 实测：`recipes_user.json` 那两条变异就是这么空转的 —— 变异脚本报
#   31/31 全命中，实际上那两条根本没验过。
def sync_publish_skip_names() -> frozenset:
    """从 `sync_publish.py` **源码文本**里取出 `SKIP_NAMES`（优先读变异副本）。"""
    import re as _re
    from pathlib import Path as _Path

    env = os.environ.get("FT_WIN_DEPLOY_DIR")
    path = (_Path(env) / "sync_publish.py") if env else (
        _Path(__file__).resolve().parents[1] / "scripts" / "sync_publish.py")
    text = path.read_text(encoding="utf-8")
    m = _re.search(r"SKIP_NAMES\s*=\s*frozenset\(\{(.*?)\}\)", text, _re.S)
    assert m, f"在 {path} 里找不到 SKIP_NAMES 定义（护栏锚点失效）"
    return frozenset(_re.findall(r'"([^"]+)"', m.group(1)))


@pytest.fixture
def sync_skip_names() -> frozenset:
    """`sync_publish_skip_names()` 的 fixture 形态 —— 测试直接把它写进签名即可，
    免得每个测试文件都要 `from conftest import ...`（那依赖 pytest 的 rootdir
    把 `tests/` 塞进 sys.path，换个跑法就会 ImportError）。"""
    return sync_publish_skip_names()
