"""
产品问题修复（P-01 ~ P-04）的回归护栏。

四项各自锁一条**不变量**，而不是锁实现细节 —— 断言写得越像实现越容易假报警。

    P-01 移动端      : 必须存在响应式断点，且手机上三栏/两栏真的降级为单列；
                       甘特条必须走 Pointer Events（只绑 mousedown 时触屏点不开）。
    P-02 LLM 可观测  : /api/llm_status 必须能区分 在线 / 熔断 / 未配置 三态，
                       且**绝不回显密钥**。
    P-03 菜谱增删改查 : 官方 data/recipes_official_100.csv 永不被改写（增删改只写
                       用户层），新增的菜必须能进排程候选，删除可恢复、幂等、
                       可被开关关掉。
    P-04 步骤详情    : 排程视图必须能读到 raw_text 原文，且合成行(dish 为空)
                       与提前准备行(start_min<0) 都有专门处理。
    P-05 后端不可达  : 所有请求必须走同一个出口，网络层失败要给出**结论**而不是
                       永远「加载中...」；HTTP 4xx 反过来不能被当成「后端挂了」。
    P-06 访问令牌    : 后端开 FT_AUTH_TOKEN 时前端要能带上令牌（从 ?token= 取，
                       存 localStorage，绝不硬编码），且 401 的提示不能被
                       「后端恢复了」顺手关掉。
    P-07 列表口径    : 接口带 limit 时，界面上写的条数必须与**实际渲染的条数**一致，
                       详情取不到时不能把 undefined 拼进模板。

接口层按仓库惯例**直接调用路由函数**（见 requirements.txt 说明：仓库不依赖 httpx，
scripts/api_probe.py 也是这么做的），不引入 TestClient。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

HTML_PATH = Path(os.environ.get("FT_WEB_HTML")
                 or (Path(__file__).resolve().parents[1] / "app" / "web" / "index.html"))


@pytest.fixture(scope="module")
def html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _fn_body(html: str, header: str) -> str:
    """截出某个函数体（从 header 到第一个顶格 `}` 之前）。"""
    i = html.index(header)
    seg = html[i:]
    j = seg.index("\n}\n")
    return seg[: j + 3]


def _strip_js_comments(src: str) -> str:
    """去掉 JS 注释，只留可执行代码。

    为什么必须去：本文件的护栏大量使用 `assert "xxx" not in body` 形式，
    而源码里**恰恰有一条注释在解释"这里不能调 xxx"** ——
    注释里出现被禁字符串会让护栏假报警（真实代码是对的，却报红）。
    护栏假报警比漏报更危险：改到没人信的时候，真回归也会被无视。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


# ===========================================================================
# P-01 移动端：布局降级 + 触控
# ===========================================================================


def test_has_responsive_breakpoints(html: str):
    """必须真的存在 @media 断点。

    修复前全文件 @media 出现 0 次 —— 任何屏幕宽度都套用桌面栅格，
    `.grid3` 两侧列写死 260px，390px 手机上根本装不下。
    """
    assert "@media" in html, "没有任何响应式断点，手机端仍是桌面布局"
    assert re.search(r"@media\s*\(max-width:\s*640px\)", html), "缺少手机断点(≤640px)"


def _media_block(html: str, query: str) -> str:
    """截出某个 @media 块的正文（到块尾或 </style> 为止）。"""
    i = html.index(query)
    seg = html[i:]
    return seg[: seg.index("</style>")] if "</style>" in seg else seg


def _declares_single_column(block: str, cls: str) -> bool:
    """块里是否存在一条规则，把 `cls` 所在选择器降成单列。

    必须按**选择器列表**逐个比对，不能用 `re.search(r"\\.grid3\\{")` 去扫 ——
    分组选择器 `.grid3,.grid2{...}` 里 `.grid3` 后面跟的是逗号不是花括号，
    正则会跳过它、改去命中块里另一条 `#v-plan .grid3{...}`，于是
    "分组规则被改成两列"这种变异照样通过（变异测试实测漏放过一次）。
    """
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", block):
        sels = [s.strip() for s in m.group(1).split(",")]
        body = _norm(m.group(2))
        if not body.startswith("grid-template-columns:"):
            continue
        cols = body[len("grid-template-columns:"):].split(";")[0]
        if cols != "minmax(0,1fr)":
            continue
        if any(s == cls or s.endswith(" " + cls) for s in sels):
            return True
    return False


def test_grids_collapse_to_one_column_on_phone(html: str):
    """手机上 .grid3 / .grid2 必须降级为单列。"""
    block = _media_block(html, "@media (max-width:640px)")
    for cls in (".grid3", ".grid2"):
        assert _declares_single_column(block, cls), (
            f"手机断点里没有把 {cls} 降成单列的规则"
        )


def test_gantt_bars_use_pointer_events(html: str):
    """甘特条必须走 Pointer Events。

    旧实现只绑 mousedown：手机上既拖不动、也点不开步骤详情。
    Pointer Events 一套代码同时覆盖鼠标/触屏/触控笔。
    """
    assert "addEventListener('pointerdown'" in html, "甘特条没绑 pointerdown，触屏拖不动"
    assert "pointercancel" in html, "缺少 pointercancel 处理，手势被系统接管后状态会卡住"
    assert "onmousedown" not in html, "还残留 mousedown 绑定，触屏路径没走通"


def test_gantt_bar_declares_touch_action(html: str):
    """甘特条要声明 touch-action，否则横向拖拽会被浏览器当成页面滚动吞掉。

    pan-y = 横向手势归拖拽、纵向仍交给页面滚动。对鼠标是空操作。
    """
    m = re.search(r"\.sbar\{([^}]*)\}", html)
    assert m and "touch-action:pan-y" in _norm(m.group(1)), (
        ".sbar 没写 touch-action:pan-y，触屏拖拽会被滚动抢走"
    )


def test_tap_targets_are_big_enough_on_phone(html: str):
    """手机断点里必须把点击目标抬到 ≥44px（手指够得着的通行下限）。"""
    css = _norm(_media_block(html, "@media (max-width:640px)"))
    assert "button{min-height:44px}" in css, "手机端按钮没有抬到 44px 高"
    assert re.search(r"\.sbar\{[^}]*height:44px", css), "手机端甘特条仍是 30px，点不中"
    assert re.search(r"\.item\{[^}]*min-height:44px", css), "手机端列表项点击区不足 44px"


# ===========================================================================
# P-02 LLM 可观测性
# ===========================================================================


@pytest.fixture
def main_mod(tmp_path, monkeypatch):
    """导入 app.api.main，并把菜谱库的**用户层**文件指到临时目录。

    绝不能让测试碰到仓库里的 data/recipes_user.json —— 那是真实数据。
    （这条纪律继承自原来的档案功能：官方基线只读，用户改动只写用户层。）
    """
    from app.api import main as M
    from app.spec import recipe_store as RS
    monkeypatch.setattr(RS, "_USER_JSON", str(tmp_path / "recipes_user.json"))
    RS.invalidate()
    yield M
    RS.invalidate()


def test_llm_status_reports_off_when_no_key(main_mod, monkeypatch):
    from app.agent import llm
    # 必须显式打开全局开关: tests/conftest.py 在收集期就把 FT_LLM 置成了 off
    # （离线档），那是**整个进程**的副作用。
    # 不显式设置的话, 这条用例测到的其实是"全局关闭", 而不是"没配密钥"。
    monkeypatch.setenv("FT_LLM", "on")
    # provider 也要显式指定成 openai: 本机"开箱档"由出厂层
    # （app/agent/llm_defaults.json）决定，**不是**本地 Ollama —— 而 Ollama 免密钥，
    # "没配密钥"对它根本不是一种状态，enabled() 照样是 True。
    # 这条用例测的是"需要密钥的 provider 在没密钥时如何表现"，所以必须显式钉住。
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "API_KEY", "")
    d = main_mod.llm_status()
    assert d["state"] == "off"
    assert d["api_key_set"] is False
    assert d["api_key_required"] is True
    assert d["label"]


def test_llm_status_reports_online_for_a_keyless_local_provider(main_mod, monkeypatch):
    """本地 Ollama 免密钥 ⇒ 没配密钥也必须是在线，而不是"未配置模型"。

    这条是"接上本地模型但 AI 毫无反应"的直接守卫：改造前 `enabled()` 拿
    `bool(API_KEY)` 当总开关，本地模型会被永远判成未配置，全链路静默降级 ——
    而页面不报任何错，只能靠人盯着"AI 点菜怎么老是规则结果"去猜。
    """
    from app.agent import llm
    monkeypatch.setenv("FT_LLM", "on")
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "API_KEY", "")
    llm.breaker_reset()
    try:
        d = main_mod.llm_status()
        assert d["api_key_required"] is False
        assert d["is_local"] is True
        assert d["state"] == "online", "本地模型免密钥，不该因为'没密钥'被判成离线"
        assert d["enabled"] is True
        assert "未配置" not in d["label"], "本地档不该说'未配置'"
    finally:
        llm.breaker_reset()


def test_llm_status_explains_local_failure_in_actionable_words(main_mod, monkeypatch):
    """本地档探测失败的文案必须**可执行**。

    通用文案"启动探测未在预算内通过"对本地部署毫无指导性 —— 用户看着这句话
    不知道该去查什么。本地档 99% 的失败原因就是"Ollama 没起"或"模型没 pull"。
    """
    from app.agent import llm
    monkeypatch.setenv("FT_LLM", "on")
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    llm.breaker_reset()
    try:
        llm.force_open("warmup")
        d = main_mod.llm_status()
        assert d["state"] == "breaker"
        assert "Ollama" in d["breaker_reason_text"], (
            f"本地档的失败原因没有指向 Ollama: {d['breaker_reason_text']!r}"
        )
    finally:
        llm.breaker_reset()


def test_llm_status_reports_breaker_with_reason(main_mod, monkeypatch):
    """熔断时必须说清**为什么**、还有多久重试 —— 这正是修复前完全缺失的信息。"""
    from app.agent import llm
    monkeypatch.setenv("FT_LLM", "on")     # 同上: 抵消 conftest 的模块级 FT_LLM=off
    monkeypatch.setattr(llm, "API_KEY", "dummy-key")
    llm.breaker_reset()
    try:
        llm.force_open("quota")
        d = main_mod.llm_status()
        assert d["state"] == "breaker"
        assert d["breaker_reason"] == "quota"
        assert d["breaker_reason_text"], "熔断原因没有翻译成人话"
        assert d["retry_after_s"] > 0, "没告诉用户还要等多久"
    finally:
        llm.breaker_reset()


def test_llm_status_never_leaks_the_api_key(main_mod, monkeypatch):
    """状态接口只能暴露"密钥是否已配置"，绝不能回显密钥本身。"""
    from app.agent import llm
    secret = "sk-this-must-never-appear-1234567890"
    monkeypatch.setattr(llm, "API_KEY", secret)
    llm.breaker_reset()
    d = main_mod.llm_status()
    assert d["api_key_set"] is True
    assert secret not in json.dumps(d, ensure_ascii=False), "状态接口泄漏了 API Key"


def test_frontend_shows_llm_state_badge(html: str):
    """界面必须有模型状态徽标，且三态样式齐全（不能只显示"在线/离线"两种）。"""
    assert 'id="llmBadge"' in html, "顶栏没有模型状态徽标"
    assert "/api/llm_status" in html, "前端没有调用状态接口"
    assert "function refreshLlmStatus(" in html, "refreshLlmStatus() 未定义"
    for cls in (".llm-badge.on", ".llm-badge.warn", ".llm-badge.off"):
        assert cls in html, f"缺少状态样式 {cls}，三态区分不出来"


def test_frontend_explains_the_ft_llm_kill_switch(html: str):
    """`FT_LLM=off` 是**全局总闸**，设置页改不动它 —— 界面必须把这件事说清楚。

    不点明的话：用户在设置页把「接入方式」切成「远程」、填好地址和 Key、
    看到"保存成功"，然后 AI 对话**永远**回"离线模式"。他会去翻模型配置、
    翻网络、翻防火墙，怎么都想不到是 `.env.local` 里那一行
    （部署时误加了 `setup.ps1 -Offline`）。2026-10-04 迁移验收时实测踩到。
    """
    assert "function llmKillSwitchOff(" in html, "缺少总闸识别函数"
    body = _fn_body(html, "function llmKillSwitchOff(d){")
    assert "switches" in body and "FT_LLM" in body, (
        "llmKillSwitchOff 必须读 /api/llm_status 的 switches.FT_LLM，"
        "不能写成永远返回 false 的空壳"
    )
    # 两处都要用：徽标 tooltip（鼠标悬停能查到）+ 降级提示（不悬停也能看到）。
    # 少一处 = 有一半用户看不到这条关键线索。
    badge = _fn_body(html, "async function refreshLlmStatus(){")
    toast = _fn_body(html, "function maybeNotifyLlmDegrade(){")
    assert "llmKillSwitchOff(d)" in _norm(badge), "徽标 tooltip 没有报出「总闸被关」"
    assert "llmKillSwitchOff(d)" in _norm(toast), "降级提示没有报出「总闸被关」"


def test_test_connection_failure_names_the_real_cause(html: str):
    """「测试连接」失败时必须按**真实原因**给提示，不能一律怪地址与密钥。

    后端 `/api/chat` 早就返回了 `reason`（disabled / breaker / error），前端不读它、
    统一写「请核对远程接口地址与 API Key」的后果：线上最常见的失败其实是总闸
    `FT_LLM=off` —— 那时地址和密钥**全是对的**，用户照着提示翻遍模型配置也找不到
    问题，最后只能怀疑「程序坏了」。2026-10-04 实测截图：28ms 失败、密钥显示已配置。
    """
    assert "function llmFailReason(" in html, "缺少「失败原因分流」函数"
    body = _fn_body(html, "function llmFailReason(d){")
    assert "reason" in body, "llmFailReason 必须读后端返回的 reason，否则等于没分流"
    # 三条分支缺一不可：总闸 / 熔断 / 真·网络错误。
    assert "llmKillSwitchOff(st)" in _norm(body), (
        "没识别总闸，会把总闸误报成地址或密钥问题"
    )
    assert "'breaker'" in body or '"breaker"' in body, "没处理熔断分支"
    # 调用点：testModel 必须走它，且**不能**再硬编码那句误导文案。
    tm = _strip_js_comments(_fn_body(html, "async function testModel(){"))
    assert "llmFailReason(d)" in _norm(tm), "testModel 没有用失败原因分流"
    assert _norm("请核对远程接口地址与 API Key") not in _norm(tm), (
        "testModel 里又硬编码了「核对远程接口地址与 API Key」—— 必须交给 llmFailReason"
    )
    # ⚠ **接线**：llmFailReason 判断总闸靠的是 window._llmState，而它由
    #   refreshLlmStatus() 落盘。少了这一行赋值，函数结构完全没变、所有静态断言
    #   照样全绿，但界面上**永远识别不出总闸** —— 正是"结构对、接线断"。
    #   （真跑页面的行为体检见 scripts/ui_probe_llm_off.mjs，本仓库不把 node 依赖
    #     塞进 pytest，所以这里用一条可被变异打穿的静态断言把接线钉住。）
    assert "window._llmState=d;" in _norm(html), (
        "refreshLlmStatus 没有把状态落到 window._llmState，llmFailReason 识别不出总闸"
    )


def test_settings_page_warns_when_the_kill_switch_is_off(html: str):
    """总闸关闭时，设置页必须在**动手之前**就说清楚，而不是等用户填完再报错。

    `FT_LLM=off` 下这张卡片里填什么都不生效；不点破它，用户会把地址 / 密钥 /
    模型名反复改、反复点「测试连接」，最后拿到一句风马牛不相及的报错。
    """
    body = _fn_body(html, "async function loadModelSettings(){")
    assert "llmKillSwitchOff(st)" in _norm(body), (
        "loadModelSettings 没有检测总闸，用户进来只会看到一句普通的配置说明"
    )
    assert "FT_LLM=off" in body, "设置页提示里没有点明 FT_LLM=off 这个具体开关名"
    assert ".set-hint.warn" in html, "缺少总闸警示样式，提示与普通说明分不出来"


def test_frontend_notifies_degradation_only_once(html: str):
    """降级提示必须是一次性的，不能每次排程都弹。"""
    body = _fn_body(html, "function maybeNotifyLlmDegrade(){")
    # 查**声明级**的早退语句，而不是 `"LLM_NOTIFIED" in body` ——
    # 后者在"早退被删掉、只剩赋值"的变异版本里依然为真（变异测试实测踩到过）。
    assert "if(LLM_NOTIFIED)return;" in _norm(body), (
        "没有一次性早退，降级提示会每次排程都弹"
    )
    assert "LLM_NOTIFIED=true;" in _norm(body), "没有置位一次性标记"


# ===========================================================================
# P-03 菜谱库：增删改查（官方数据只读 + 用户层可恢复）
# ===========================================================================
#
# 数据分两层：官方 data/recipes_official_100.csv 是赛题下发的评测数据（只读），
# 用户的新增/修改/删除只写 data/recipes_user.json。
# 这条约束继承自原来的"档案"功能，理由也一样：
#   ① 硬删一份随包数据是不可逆的；
#   ② adapter 直接读它决定"哪些菜能排"，改写它会**静默改变排程行为**
#      —— 而且只有重跑基准才看得出来。


def test_official_csv_is_never_rewritten(main_mod):
    """官方菜谱文件是只读基线 —— 增、改、删、恢复都绝不能碰它。"""
    from app.spec import recipe_store as RS
    p = Path(RS._OFFICIAL_CSV)
    before = hashlib.sha256(p.read_bytes()).hexdigest()
    # 增 + 改 + 删（自建菜：真删）
    rec = main_mod.create_recipe(main_mod.RecipeEditIn(name="测试菜", steps="第1步：煮。"))
    rid = rec["recipe"]["id"]
    main_mod.update_recipe(rid, main_mod.RecipeEditIn(name="测试菜改", steps="第1步：煮开。"))
    main_mod.delete_recipe(rid)
    # 删 + 恢复（官方菜：软删）
    off_id = main_mod.list_recipes()["recipes"][0]["id"]
    main_mod.delete_recipe(off_id)
    main_mod.restore_recipe(off_id)
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before, (
        "官方 recipes_official_100.csv 被改写了 —— 增删改必须只作用于用户层"
    )


def test_library_defaults_to_the_official_100(main_mod):
    """默认库就是这 100 道（含 1 组重名 ⇒ 99 个不同菜名，但必须返回 100 条）。"""
    d = main_mod.list_recipes()
    assert d["total"] == 100, f"官方菜谱应为 100 条，实际 {d['total']}"
    assert d["added_total"] == 0 and d["deleted_total"] == 0


def test_create_then_it_becomes_schedulable(main_mod):
    """新增的菜必须能进排程候选 —— 否则"增"只是改了个列表，是半截功能。"""
    from app.spec.adapter import all_names
    rec = main_mod.create_recipe(main_mod.RecipeEditIn(
        name="测试番茄炒蛋", ingredients="主料：番茄2个；鸡蛋3个",
        steps="第1步：鸡蛋打散。；第2步：锅中注油烧热，倒入蛋液炒散。；第3步：下番茄炒匀加盐。"))
    assert rec["ok"] is True
    assert "测试番茄炒蛋" in all_names(), "新增的菜没有进入排程候选集"


def test_create_requires_a_name_and_some_content(main_mod):
    """校验失败必须是 400（用户的输入问题），不是 500。"""
    with pytest.raises(HTTPException) as e:
        main_mod.create_recipe(main_mod.RecipeEditIn(name="   ", steps="第1步：煮。"))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e2:
        main_mod.create_recipe(main_mod.RecipeEditIn(name="只有名字"))
    assert e2.value.status_code == 400, "食材与步骤全空也必须被拒"


def test_update_changes_the_scheduling_text(main_mod):
    """改完必须真的影响排程读取的原文，而不只是展示层。"""
    from app.spec.adapter import get
    rid = main_mod.create_recipe(main_mod.RecipeEditIn(
        name="测试菜U", steps="第1步：锅中注油烧热，下蛋液炒散。"))["recipe"]["id"]
    main_mod.update_recipe(rid, main_mod.RecipeEditIn(
        name="测试菜U", steps="第1步：放入蒸箱，100℃蒸20分钟。"))
    assert "蒸箱" in get("测试菜U")["steps"], "修改没有落到排程读取的原文上"


def test_delete_official_recipe_is_recoverable(main_mod):
    """官方菜删除后必须可恢复 —— 官方 CSV 是赛题数据，硬删不可逆。"""
    rid = main_mod.list_recipes()["recipes"][0]["id"]
    res = main_mod.delete_recipe(rid)
    assert res["recoverable"] is True, "官方菜的删除必须是可恢复的"
    d = main_mod.list_recipes()
    assert d["total"] == 99, f"删除官方菜后应为 99，实际 {d['total']}"
    assert d["deleted_total"] == 1
    assert rid not in [r["id"] for r in d["recipes"]], "已删除的菜仍出现在默认列表里"
    # 已删除的详情仍要能取到（带 deleted 标记），否则前端画不出"恢复"按钮
    assert main_mod.recipe_detail(rid)["deleted"] is True
    main_mod.restore_recipe(rid)
    assert main_mod.list_recipes()["total"] == 100, "恢复后没有回到 100 条"


def test_deleted_view_exposes_items_for_restore(main_mod):
    """必须有一条路径能**看到**已删除的菜谱，否则"可恢复"是句空话。"""
    rid = main_mod.list_recipes()["recipes"][0]["id"]
    main_mod.delete_recipe(rid)
    only_del = main_mod.list_recipes(deleted="1")
    assert [r["id"] for r in only_del["recipes"]] == [rid]
    assert only_del["recipes"][0]["deleted"] is True
    main_mod.restore_recipe(rid)
    assert main_mod.list_recipes(deleted="1")["recipes"] == [], "恢复后不该再出现在已删除档"


def test_delete_is_idempotent_for_official(main_mod):
    rid = main_mod.list_recipes()["recipes"][0]["id"]
    assert main_mod.delete_recipe(rid)["deleted"] == 1
    assert main_mod.delete_recipe(rid)["deleted"] == 0, "重复删除应当是幂等的"
    main_mod.restore_recipe(rid)


def test_delete_user_recipe_really_removes_it(main_mod):
    """自建菜是真删 —— 否则会留下一条**谁也看不到、也恢复不了**的垃圾墓碑。"""
    base = main_mod.list_recipes()["total"]
    rid = main_mod.create_recipe(main_mod.RecipeEditIn(name="测试菜D", steps="第1步：煮。"))["recipe"]["id"]
    assert main_mod.list_recipes()["total"] == base + 1
    res = main_mod.delete_recipe(rid)
    assert res["recoverable"] is False and res["purged"] == 1
    d = main_mod.list_recipes()
    assert d["total"] == base, "删除后列表条数没变"
    assert d["deleted_total"] == 0, "自建菜的删除不该在 deleted_total 里留下计数"
    assert rid not in [r["id"] for r in main_mod.list_recipes(deleted="all")["recipes"]], \
        "自建菜被真删后，任何档位都不该再看到它"


def test_restore_a_user_recipe_is_404_not_silent_success(main_mod):
    """恢复一个已被真删的自建菜：必须是 404，不能"静默成功 0 条"。"""
    rid = main_mod.create_recipe(main_mod.RecipeEditIn(name="测试菜R", steps="第1步：煮。"))["recipe"]["id"]
    main_mod.delete_recipe(rid)
    with pytest.raises(HTTPException) as e:
        main_mod.restore_recipe(rid)
    assert e.value.status_code == 404


def test_restore_an_untouched_official_recipe_is_a_noop(main_mod):
    rid = main_mod.list_recipes()["recipes"][0]["id"]
    assert main_mod.restore_recipe(rid)["restored"] == 0, "没删过的菜，恢复应当是无操作"


def test_delete_unknown_id_is_404_and_changes_nothing(main_mod):
    before = main_mod.list_recipes()["total"]
    with pytest.raises(HTTPException) as e:
        main_mod.delete_recipe("u_no_such_id")
    assert e.value.status_code == 404, "不存在的 id 必须 404，不能静默成功"
    assert main_mod.list_recipes()["total"] == before


def test_write_can_be_switched_off(main_mod, monkeypatch):
    """公开部署时必须能把写接口整体关掉（当前没有登录鉴权）。"""
    monkeypatch.setenv("FT_ALLOW_RECIPE_WRITE", "off")
    assert main_mod.list_recipes()["writable"] is False
    for fn in (lambda: main_mod.create_recipe(main_mod.RecipeEditIn(name="x", steps="y")),
               lambda: main_mod.update_recipe("u_1", main_mod.RecipeEditIn(name="x", steps="y")),
               lambda: main_mod.delete_recipe("u_1"),
               lambda: main_mod.restore_recipe("u_1")):
        with pytest.raises(HTTPException) as e:
            fn()
        assert e.value.status_code == 403


def test_frontend_has_confirm_and_restore_entry(html: str):
    """前端必须有二次确认与恢复入口 —— 只有删除按钮的"删除"是陷阱。"""
    assert "function deleteRecipe(" in html and "function restoreRecipe(" in html
    body = _fn_body(html, "async function deleteRecipe(id){")
    assert "confirm(" in body, "删除没有二次确认"
    # ⚠ 必须判在 **restoreRecipe 的函数体里**，不能判整个 html：v3.8 的撤销删除
    #   （`undoDeleteRecipe`）里也有一处 `/restore`，判全局的话"把恢复入口删掉"
    #   这个变异会被它兜住 —— 护栏永远绿（实测：变异自检 167/170 就是这么来的）。
    rec = _fn_body(html, "async function restoreRecipe(id){")
    assert "/restore" in rec, "没有恢复入口"
    assert 'id="libShowDeleted"' in html, "没有「显示已删除」开关，删完就找不回来了"


def test_frontend_sends_the_right_http_verb(html: str):
    """新增走 POST、修改走 PUT —— 写错动词会把"编辑"变成"又建一条"。"""
    body = _fn_body(html, "async function saveRecipe(id){")
    assert "id?'PUT':'POST'" in _norm(body), "没有按「有没有 id」区分 PUT / POST"

# ===========================================================================
# P-04 排程界面可查看操作步骤
# ===========================================================================


def test_task_table_shows_the_raw_step_text(html: str):
    """任务明细必须直接给出操作步骤原文，而不是只有"菜#步"。"""
    i = html.rindex("getElementById('tasks').innerHTML")
    seg = html[i: i + 1200]
    assert "操作步骤" in seg, "明细表没有「操作步骤」列"
    assert "t.raw_text" in seg, "明细表没有渲染 raw_text"


def test_step_detail_is_reachable_from_both_entry_points(html: str):
    """甘特条与明细行都要能打开步骤详情 —— 只做一处会漏掉一半用户。"""
    assert "function openStepDetail(" in html, "openStepDetail() 未定义"
    assert "function closeStepDetail(" in html, "closeStepDetail() 未定义"

    # 入口 1：明细行 onclick
    i = html.rindex("getElementById('tasks').innerHTML")
    assert "openStepDetail(" in html[i: i + 1200], "明细行点不开步骤详情"

    # 入口 2：甘特条（bindDrag 里按下标回调）
    drag = _fn_body(html, "function bindDrag(){")
    assert "openStepDetail(" in drag, "甘特条点不开步骤详情"
    assert "DRAG_SLOP_PX" in drag, "拖动与点击没有用位移阈值区分，轻点也会弹窗"


def test_step_detail_handles_synthetic_and_prep_rows(html: str):
    """两类特殊行必须有专门处理，否则详情面板会是空白/负数时间。

    ① 合成行：预热共享任务的 dish 为空，必须改显示 dish_list（服务于哪几道菜）；
    ② 提前准备行：start_min < 0，必须显示"提前"，不能把 -180 当时间展示。
    """
    body = _fn_body(html, "function openStepDetail(i){")
    # 必须查**条件语句本身**，不能只查 `"dish_list" in body` ——
    # 把条件改成 `if(false)` 之后，字符串 `dish_list` 还留在花括号里，
    # 断言照样为真（变异测试实测漏放过一次）。
    assert "if(synth&&(t.dish_list||[]).length)" in _norm(body), (
        "合成行(dish 为空)没有专门分支，会显示空白"
    )
    assert re.search(r"rows\.push\(\[[^\]]*dish_list", body), (
        "dish_list 没有真的写进详情面板"
    )
    assert "start_min<0" in _norm(body), "提前准备行没有专门分支，会显示负数时间"
    assert "raw_text" in body, "详情面板没有展示操作步骤原文"
    assert "depends_on" in body, "详情面板没有展示前置依赖"


def test_task_index_is_assigned_before_the_gantt_is_drawn(html: str):
    """_idx 必须在画甘特图**之前**赋值。

    renderSwim() 是在 `#gantt`.innerHTML=... 里被调用的，它靠 `t._idx` 渲染
    `data-idx`；赋值挪到后面，甘特条的 data-idx 就成了空串 —— 明细行能点开详情、
    甘特条点不开，而且不报任何错。jsdom 探针实测抓到过一次。
    """
    body = _fn_body(html, "function render(d){")
    assert "t._idx=i" in body, "render() 没有给任务标下标"
    assert body.index("t._idx=i") < body.index("renderSwim(cook)"), (
        "_idx 在画甘特图之后才赋值，甘特条的 data-idx 会是空串、点不开详情"
    )


def test_step_detail_modal_exists_and_closes(html: str):
    """弹层要有遮罩、关闭键与 Esc —— 盖住全屏时键盘用户唯一出路是 Esc。"""
    assert 'id="stepModal"' in html and 'id="stepModalBody"' in html
    assert "if(event.target===this)closeStepDetail()" in _norm(html), "点遮罩不能关闭"
    assert "e.key==='Escape'" in html, "Esc 不能关闭弹层"


def test_raw_step_text_is_escaped_before_injection(html: str):
    """raw_text 来自菜谱数据，拼进 innerHTML 前必须转义。

    不转义时，原文里的 < > & " ' 会破坏结构（最轻是显示错乱，最重是注入）。
    """
    assert "function esc(" in html, "缺少 esc() 转义助手"
    i = html.rindex("getElementById('tasks').innerHTML")
    seg = html[i: i + 1200]
    # 两处都要查**声明级**片段：只查 `"esc(t.raw_text" in seg` 的话，
    # 只要还有另一处转义在，删掉其中一处也照样为真（变异测试会漏放行）。
    assert 'title="${esc(t.raw_text' in seg, "明细表 raw_text 的 title 提示没转义"
    assert '>${esc(t.raw_text' in seg, "明细表 raw_text 正文没转义"
    body = _fn_body(html, "function openStepDetail(i){")
    assert "esc(t.raw_text" in body, "详情面板的 raw_text 没有转义"


# ===========================================================================
# P-05 后端不可达：不许永远"加载中..."
# ===========================================================================
#
# 这个页面是"薄客户端"：菜谱/设备/排程全部来自 FastAPI。
# 于是有两种必然失败的打开方式：
#   ① 双击 index.html（file://）—— 相对路径 /api/* 解析成 file:///api/*；
#   ② 从静态预览/静态服务器打开 —— 有 index.html 但没有后端。
# 修复前 loadRecipes() 的 await 直接 reject、没有任何失败态，占位符永远停在
# 「加载中...」，用户分不清"还在加载"和"已经失败"。下面几条锁住这个行为。


def test_all_api_calls_go_through_one_wrapper(html: str):
    """所有请求必须走同一个出口。

    漏掉一个裸 fetch 就会出现"某一块永远加载中" —— 而且只在那一个视图里复现，
    极难发现。所以这里直接数 `fetch(` 的出现次数：只允许 apiFetch 内部那一次。
    """
    assert "async function apiFetch(url,opts){" in html, "缺少统一请求出口 apiFetch"
    calls = [m.start() for m in re.finditer(r"\bfetch\(", html)]
    assert len(calls) == 1, (
        f"除 apiFetch 外还有 {len(calls) - 1} 处裸 fetch，"
        f"它们失败时会留下永远转圈的占位符"
    )
    i = html.index("async function apiFetch(url,opts){")
    j = html.index("\n}\n", i)
    assert i < calls[0] < j, "唯一那次 fetch 不在 apiFetch 里"


def test_backend_down_replaces_the_loading_placeholder(html: str):
    """后端不可达是**结论**，不是中间态 —— 必须把「加载中...」换掉并给出横幅。"""
    assert 'id="offlineBar"' in html, "缺少后端不可达横幅容器"
    assert 'id="offlineText"' in html, "横幅里没有文案节点"
    assert 'class="offline-bar" id="offlineBar"' in html, "横幅类名写错"
    body = _fn_body(html, "function markBackendDown(err){")
    assert "classList.add('on')" in body, "markBackendDown 没有把横幅显示出来"
    assert "'加载中...'" in body, "markBackendDown 没有识别并替换初始占位符"
    assert "后端未连接" in body, "markBackendDown 没有给出明确的结论文案"
    # 每个会渲染列表的加载函数都要有自己的 catch，否则该视图仍会永远转圈。
    for header in ("async function loadLib(q){",
                   "async function loadDetail(id){",
                   "async function loadDevices(){",
                   "async function loadHist(){"):
        fn = _fn_body(html, header)
        assert "catch(e){" in fn, f"{header} 没有失败分支，后端不可达时会一直「加载中...」"
    assert "后端未连接" in _fn_body(html, "async function loadRecipes(){")


def test_file_protocol_gets_its_own_hint(html: str):
    """file:// 打开要给"运行 start.bat"的具体指引，不能只说"连接失败"。"""
    # 地址抽成常量了（横幅要把它变成可点链接，title 里要纯文本），
    # 所以这里断言"常量定义对 + 提示真的引用了它"，而不是去提示正文里找字面量。
    assert "const BACKEND_ADDR='http://127.0.0.1:8137/app'" in html, "访问地址常量写错或缺失"
    body = _fn_body(html, "function backendHint(){")
    assert "location.protocol==='file:'" in body, "没有区分 file:// 这种最常见的误用"
    assert "start.bat" in body, "提示里没告诉用户怎么启动后端"
    assert "BACKEND_ADDR" in body, "提示里没给出正确访问地址"
    # 横幅要能点，title 要纯文本 —— 两版必须分开，否则 title 里会出现 HTML 标签。
    assert "function backendHintHtml(){" in html, "缺少把地址变成链接的版本"
    assert "innerHTML=backendHintHtml()" in _norm(html), "横幅没有用可点版本渲染"


def test_http_error_is_not_mistaken_for_backend_down(html: str):
    """HTTP 4xx/5xx 说明后端是**活的**，不能当成"后端挂了"。

    否则 P-03 的 403（`FT_ALLOW_RECIPE_WRITE=off`）会被横幅盖成"连不上后端"，
    用户永远看不到"写入已在服务端关闭"这句真正的解释。
    """
    body = _fn_body(html, "async function apiFetch(url,opts){")
    assert "r.ok" not in body, "apiFetch 里不应按 HTTP 状态判定「后端挂了」"
    assert "markBackendDown" in body, "apiFetch 没有在网络层失败时标记后端不可达"
    # 4xx 的处理权必须留在调用方：删菜谱的 403 分支还在。
    dele = _fn_body(html, "async function deleteRecipe(id){")
    assert "if(!r.ok)" in dele and "d.detail" in dele, "删菜谱丢掉了 403 的详细提示"


def test_llm_badge_does_not_override_backend_down(html: str):
    """refreshLlmStatus 的 catch 不能把「后端未连接」盖回含糊的「状态未知」。"""
    body = _fn_body(html, "async function refreshLlmStatus(){")
    assert "if(BACKEND_DOWN)return;" in body, (
        "模型状态查询失败时会覆盖 markBackendDown 写的徽标文案"
    )


def test_retry_button_is_wired(html: str):
    """横幅上的"重试"必须真的重新拉数据，而不是只把横幅关掉。"""
    assert 'onclick="retryBackend()"' in html, "重试按钮没有绑定"
    body = _fn_body(html, "function retryBackend(){")
    assert "markBackendUp()" in body, "重试没有先清掉失败态"
    assert "loadRecipes()" in body, "重试没有重新拉菜谱"
    assert "refreshLlmStatus(true)" in body, "重试没有重新拉模型状态"


# ===========================================================================
# P-06 访问令牌：前端要配合后端的可选鉴权（FT_AUTH_TOKEN）
# ===========================================================================
#
# 后端默认不鉴权（本机演示、探针直连都不带令牌），一旦部署者设了
# FT_AUTH_TOKEN，所有 /api/* 会 401。前端必须能"只改 URL 就可用"：
#   ① 从 ?token= 取一次 → 存 localStorage → 之后自动带上；
#   ② 401 要给出"该怎么修"的提示，且**不能**被误判成"后端连不上"；
#   ③ 令牌绝不写进源码（源码是给别人看的）。
# 这三条任意一条断了，都会表现为"部署后页面全空但看不出为什么"。


def test_access_token_comes_from_url_and_is_remembered(html: str):
    """令牌来源必须是 ?token= 或 localStorage，且要持久化。"""
    seg = html[html.index("let AUTH_TOKEN='';"):]
    seg = seg[: seg.index("let AUTH_FAILED=false;")]
    assert "new URLSearchParams(location.search).get('token')" in seg, (
        "没有从地址栏的 ?token= 取令牌 —— 部署者把带令牌的链接发出去就能直接用"
    )
    assert "localStorage.getItem('ft_token')" in seg, "没有记住上次的令牌，刷新一次就失效"
    assert "localStorage.setItem('ft_token'" in seg, "取到令牌后没有存起来"
    # 取值必须包在 try 里：隐私模式/禁用存储时 localStorage 会抛异常，
    # 那会让整个脚本在解析期就挂掉，页面彻底白屏。
    assert "try{" in seg and "catch(e){}" in seg, "localStorage 访问没有兜底，隐私模式会白屏"


def test_token_is_never_hardcoded_in_the_frontend(html: str):
    """令牌只能来自运行时输入，源码里不许出现字面量。

    做法：把所有 `AUTH_TOKEN = ...` 的右值收集起来，逐个要求它是空串、
    或引用运行时来源（URLSearchParams / localStorage / 输入框 / 变量）。

    ⚠ 判据是"**不许出现非空字符串字面量**"，不是"只许白名单里的那两种写法"。
    2026-10-03 加设置页的「访问令牌」保存按钮后，新增了
    `AUTH_TOKEN=localStorage.getItem('ft_token')||'';` —— 它引用了 localStorage，
    但整体不是白名单里的形状，旧断言直接把它判成"泄漏"。
    旧写法把**形状**当成了**风险**：真正会泄漏的是字面量，而字面量在第一条
    断言里被无条件拒绝（带 `||` 的复合表达式也一样拦得住）。
    """
    rhs = [m.strip() for m in re.findall(r"AUTH_TOKEN\s*=\s*([^;]+);", html)]
    assert rhs, "找不到 AUTH_TOKEN 的赋值语句"
    for expr in rhs:
        # ① 绝不允许非空字符串字面量 —— 这就是"把令牌写进源码"。
        assert not re.fullmatch(r"""['"][^'"]+['"]""", expr), (
            f"AUTH_TOKEN 被赋成了字符串字面量，令牌会随源码泄漏: {expr}"
        )
        # ② 右值必须是**运行时来源**：空串 / URL 参数 / 本地存储 / 输入框 / 变量。
        ok = (expr == "''"
              or "URLSearchParams" in expr
              or "localStorage" in expr
              or ".value" in expr
              or re.fullmatch(r"[A-Za-z_$][\w$]*", expr) is not None)
        assert ok, f"AUTH_TOKEN 的右值不是运行时来源，可能是硬编码: {expr}"


def test_api_fetch_attaches_the_token_header(html: str):
    """所有请求（都走 apiFetch）必须自动带上令牌头。"""
    body = _fn_body(html, "async function apiFetch(url,opts){")
    assert "'X-Auth-Token'" in body, "apiFetch 没有注入 X-Auth-Token 头"
    assert "if(AUTH_TOKEN)" in body, (
        "应当只在拿到令牌时才加这个头 —— 后端没开鉴权时带了也无害，"
        "但要保证 headers 不被无谓地改写（调用方可能自带 headers）"
    )
    # 不能覆盖调用方自己的 headers（如 POST JSON 的 Content-Type）
    assert "Object.assign({},opts.headers," in body, (
        "注入令牌时覆盖了调用方的 headers，POST 的 Content-Type 会丢"
    )


def test_401_gets_its_own_message_not_backend_down(html: str):
    """401 = 后端活着但不认你，与「连不上」是两回事。"""
    assert "function markAuthRequired(){" in html, "缺少 401 的专属提示分支"
    code = _strip_js_comments(_fn_body(html, "async function apiFetch(url,opts){"))
    assert "r.status===401" in code, "apiFetch 没有识别 401"
    assert "markAuthRequired()" in code, "401 没有触发专属提示"
    # 关键：401 之后不能再走 markBackendDown，否则真原因（缺令牌）被盖成「连不上后端」。
    # 比的是**去注释后**的代码 —— 源码里有一条注释正在解释这件事，不去掉会假报警。
    after = code[code.index("r.status===401"):]
    assert "markBackendDown" not in after, (
        "401 分支里调了 markBackendDown —— 用户会看到「后端未连接」，"
        "而真正的原因是没带令牌，永远修不好"
    )
    # 提示要能指导用户怎么修，不能只说「未授权」
    auth = _fn_body(html, "function markAuthRequired(){")
    assert "?token=" in auth, "提示里没告诉用户怎么带上令牌"
    assert "FT_AUTH_TOKEN" in auth, "提示里没说明令牌由哪个环境变量设置"


def test_auth_failure_survives_backend_recovery(html: str):
    """「后端恢复了」不能顺手关掉令牌错误的横幅 —— 那是另一件事。"""
    body = _fn_body(html, "function markBackendUp(){")
    assert "AUTH_FAILED" in body, (
        "markBackendUp 无条件关横幅：令牌错误时会闪一下就被关掉，"
        "用户看不到原因。必须由 !AUTH_FAILED 挡住。"
    )


# ===========================================================================
# P-07 列表口径：界面写的条数必须等于真实渲染的条数
# ===========================================================================
#
# 接口是带 limit 的（选择列表只渲染前 80 行；菜谱库一次最多回 1000 行）。
# 修复前直接写 `d.total`，屏幕上出现「2000道」而实际只给了 120 行 ——
# 用户以为能翻到全部，找不到某道菜时会怀疑是搜索坏了。
#
# 菜谱库改为固定 100 道之后，这条不变量换了形态但没消失：**搜索态**下
# 写 total 会出现「共 100 道 · 显示前 3」—— 读起来像被 limit 截断了，
# 而实际是搜索命中了 3 道。所以必须把「库中总数」与「命中数」分开报。


def test_recipe_list_discloses_truncation(html: str):
    """只渲染前 N 条时必须说明「还有多少没显示」。"""
    body = _fn_body(html, "async function loadRecipes(){")
    assert "shown.map(" in body, "渲染的行不是截断后的 shown，等于把截断做在了别处"
    assert "allRecipes.length>shown.length" in body, "没有判断「是否真的被截断」"
    assert "shown.length" in body and "allRecipes.length" in body, (
        "截断提示里必须同时给出「显示了几条」和「一共几条」"
    )


def test_library_count_distinguishes_total_from_shown(html: str):
    """菜谱库角标必须区分「库中总数」「搜索命中数」「本次渲染条数」。"""
    assert 'id="libCount"' in html, "菜谱库缺少计数节点"
    # ⚠ 必须比**去注释后**的代码：`loadLib` 里恰好有一条注释在解释
    #   "角标必须反映真实渲染的条数（d.recipes.length）"。不去注释的话，
    #   把 `const shownN=d.recipes.length;` 变异成 `d.total` 这条断言**照样通过**
    #   —— 护栏命中注释而不是代码 = 它其实没有牙齿（2026-10-02 变异测试实测抓到）。
    body = _strip_js_comments(_fn_body(html, "async function loadLib(q){"))
    assert "d.matched" in body, (
        "没有用 matched —— 搜索时会显示「共 100 道 · 显示前 3」，"
        "看起来像被截断，实际是命中了 3 道"
    )
    assert "d.total" in body, "计数文案里缺了库中总数"
    assert "d.recipes.length" in body, (
        "计数没有反映**真实渲染条数** —— 只报接口给的数字，"
        "一旦接口和渲染不一致，界面上写的条数就是假的"
    )


def test_recipe_detail_handles_404_and_empty_payload(html: str):
    """取不到详情时要给结论，且绝不能把 undefined 拼进模板。"""
    body = _fn_body(html, "async function loadDetail(id){")
    assert "if(!r.ok)" in body, "详情没有处理 HTTP 错误"
    assert "r.status===404" in body, "404 没有专属文案（id 变更是最常见的失败原因）"
    assert "!d.name" in body, "没有兜住「接口返回空对象」这一种情况"
    # 模板已经搬到 recipeViewHtml()，所以空值兜底要在**那个**函数上验，
    # 否则这条断言会对着一个不含模板的函数空跑（= 护栏静默失效）。
    view = _fn_body(html, "function recipeViewHtml(d){")
    assert "esc(" in view, "详情模板没有做转义"
    bare = re.findall(r"\$\{d\.[a-z_]+(?<!\?)\}", view)
    assert not bare, (
        f"这些字段直接拼进了模板、没做 esc()/空值兜底，取不到时会渲染出 undefined: {bare}"
    )
    # 每个插值都必须是 esc(d.xxx) 或带 ?? 兜底
    for m in re.findall(r"\$\{(d\.[a-z_]+[^}]*)\}", view):
        assert m.startswith("esc(") or "??" in m, f"未受保护的插值: ${{{m}}}"


