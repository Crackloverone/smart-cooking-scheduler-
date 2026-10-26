"""排程历史的**落盘与还原** + 自然语言改菜单（v3.7）。

为什么值得单独立护栏
--------------------
落盘前这份历史是**纯内存**的 `deque(maxlen=20)`，每条只存
`time/dishes/makespan/gap` 四个字段：
  · 重启一次就全没了；
  · 即使不重启，点开历史也看不到当时的完整计划（甘特图 / 任务明细 / 通知 /
    诊断全都回不来）—— 用户的原话是"计划做出来留不住也拿不走"。

改完之后有三条**必须**守住的纪律，任何一条破掉都会静默出错：
  1. **隔离开关**：`FT_SCHEDULES_FILE` 必须能把用户层重定向走。没有它，
     `ui_probe.mjs` / `ui_click_audit.mjs` 每跑一次就往**真仓库**的
     `data/schedules_user.json` 里塞几条测试计划，用户打开"记录"页看到的
     全是自己没排过的菜。
  2. **列表只回摘要**：`/api/schedules` 绝不能带 `plan` —— 20 条完整计划
     一起塞进响应会让"打开记录页"变成一次几百 KB 的传输。
  3. **改菜单不许凭空造菜、不许静默丢菜、模型不可用时必须如实说**
     （`ok=false`），绝不返回一个"看起来成功"的假计划。
"""
from __future__ import annotations

import importlib
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def hist_env(tmp_path, monkeypatch):
    """把用户层文件指到 tmp_path，并在用例结束后清空内存里的历史。

    ⚠ `_SCHEDULE_HISTORY` 是**模块级 deque**。用例不清的话，后面所有
      "计数类"断言都会带着前面用例排过的菜 —— 报错会指向"某个数字不对"，
      没人会想到是这里漏了清理。
    """
    from app.api import main

    path = str(tmp_path / "schedules_user.json")
    monkeypatch.setattr(main, "_SCHEDULES_PATH", path, raising=False)
    main._SCHEDULE_HISTORY.clear()
    yield main, path
    main._SCHEDULE_HISTORY.clear()
    if os.path.exists(path):
        os.remove(path)


def _one(main, *names):
    from app.api.main import ScheduleIn, RecipeIn
    return main.do_schedule(ScheduleIn(
        recipes=[RecipeIn(name=n) for n in names], notify=False))


def test_schedules_path_can_be_redirected_by_env(tmp_path):
    """`FT_SCHEDULES_FILE` 必须能把**用户层**重定向到别处。

    ⚠ 这不是锦上添花的开关，而是**探针能不能安全跑**的前提：
      `ui_probe.mjs` 每跑一次就生成一次排程 ⇒ 不隔离就会写进真仓库。
    """
    import app.api.main as main

    orig = os.environ.get("FT_SCHEDULES_FILE")
    try:
        os.environ["FT_SCHEDULES_FILE"] = str(tmp_path / "schedules_user.json")
        reloaded = importlib.reload(main)
        assert reloaded._SCHEDULES_PATH == str(tmp_path / "schedules_user.json"), \
            f"FT_SCHEDULES_FILE 没生效，实际 {reloaded._SCHEDULES_PATH}"
    finally:
        if orig is None:
            os.environ.pop("FT_SCHEDULES_FILE", None)
        else:
            os.environ["FT_SCHEDULES_FILE"] = orig
        importlib.reload(main)


def test_conftest_pins_the_schedules_file():
    """conftest 必须在 import 期把用户层钉到临时目录。

    不钉的话：① pytest 会带着开发机上的历史启动，计数类断言静默变味；
    ② 更糟的是**每次 /schedule 都会落盘** —— 测试跑一遍就往真仓库里塞
    几十条测试计划（这一条与设备/模型配置不同：那两个只在"用户主动保存"时才写）。
    """
    src = open(os.path.join(ROOT, "tests", "conftest.py"), encoding="utf-8").read()
    assert "FT_SCHEDULES_FILE" in src, "conftest 没有隔离排程历史用户层"
    assert "mkdtemp" in src or "_no_such" in src, \
        "隔离路径必须落在临时目录（每跑一次 /schedule 就写一次，不能写在仓库里）"


def test_schedule_writes_a_full_record_and_persists(hist_env):
    """排一次程 ⇒ 历史里有一条**完整**记录，且真的落盘了。"""
    main, path = hist_env
    res = _one(main, "鸡翅包饭", "苹果芒果派")

    assert res.get("sid"), "/schedule 没有回 sid —— 前端拿不到句柄就还原不了"
    assert len(main._SCHEDULE_HISTORY) == 1
    rec = main._SCHEDULE_HISTORY[0]
    for k in ("plan", "kpi", "check", "insight", "bound"):
        assert rec.get(k), f"记录里缺 {k} —— 还原时这一块会是空的"
    assert rec["plan"]["tasks"], "计划里没有任务"

    assert os.path.exists(path), "历史没有落盘"
    on_disk = json.load(open(path, encoding="utf-8"))
    assert len(on_disk) == 1 and on_disk[0]["sid"] == rec["sid"]


def test_list_returns_summary_only(hist_env):
    """`/api/schedules` 只回摘要 —— 绝不能带 `plan`。

    带上的话，20 条完整计划会让"打开记录页"变成几百 KB 的传输，
    而列表上只显示 4 个字段。
    """
    main, _ = hist_env
    _one(main, "鸡翅包饭")
    out = main.list_schedules()
    assert out["count"] == 1
    item = out["history"][0]
    assert "plan" not in item, "列表里混进了完整计划 —— 响应体会随历史条数线性膨胀"
    for k in ("sid", "time", "date", "dishes", "makespan", "gap", "tasks"):
        assert k in item, f"摘要缺字段 {k}（前端列表要用）"


def test_detail_endpoint_returns_full_record_and_404s(hist_env):
    """详情端点回完整记录；找不到 ⇒ 404（不是空对象）。"""
    from fastapi import HTTPException

    main, _ = hist_env
    res = _one(main, "鸡翅包饭")
    rec = main.get_schedule(res["sid"])
    assert rec["plan"]["tasks"], "详情里没有任务"
    with pytest.raises(HTTPException) as ei:
        main.get_schedule("no-such-sid")
    assert ei.value.status_code == 404, "找不到记录必须 404（空对象会让前端拼出一片 undefined）"


def test_clear_removes_memory_and_file(hist_env):
    """清空必须**同时**清内存与落盘文件，且如实回报。"""
    main, path = hist_env
    _one(main, "鸡翅包饭")
    assert os.path.exists(path)
    out = main.clear_schedules()
    assert out["ok"] is True and out["cleared"] == 1
    assert main.list_schedules()["count"] == 0
    assert not os.path.exists(path), "文件还在 ⇒ 下次启动记录会'复活'"


def test_load_ignores_corrupt_file_without_crashing(hist_env, tmp_path):
    """坏文件 ⇒ 当空历史启动，且把坏文件**留证**（另存 .bad）而不是静默删掉。"""
    main, path = hist_env
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    main._SCHEDULE_HISTORY.clear()
    main._load_schedules()
    assert main.list_schedules()["count"] == 0
    assert os.path.exists(path + ".bad"), "坏文件被静默删掉了 —— '记录全没了'这种事必须留下痕迹"


def test_schedules_user_is_excluded_from_publish(sync_skip_names):
    """菜谱库/设备/模型之外，排程历史也必须排除在发布包之外。

    ⚠ 读副本（fixture `sync_skip_names`）而不是 `from scripts.sync_publish import`：
      后者读真文件，而 `_mutate_deploy_guards.py` 只变异副本 ⇒ 护栏空转（假绿）。
    """
    SKIP = sync_skip_names
    assert "schedules_user.json" in SKIP, \
        "排程历史会进发布包 —— 接收方打开「记录」看到的全是别人排过的菜"
    assert "recipes_official_100.csv" not in SKIP, \
        "官方菜谱基线被误当用户层排除了（接收方会一道菜都没有）"


# ---------------------------------------------------------------------------
# 自然语言改菜单（/api/revise_plan）
# ---------------------------------------------------------------------------
def _revise(text, dishes):
    from app.api.main import ReviseIn, revise_plan
    return revise_plan(ReviseIn(text=text, dishes=list(dishes)))


def test_revise_offline_rule_drop_and_add(monkeypatch):
    """离线（无模型）时走关键词兜底：去掉 / 加上 / 提前 / 推后。"""
    monkeypatch.setenv("FT_LLM", "off")
    r = _revise("去掉鸡翅包饭，把苹果芒果派提前", ["鸡翅包饭", "苹果芒果派"])
    assert r["ok"] and r["source"] == "rule"
    assert r["order"] == ["苹果芒果派"] and r["remove"] == ["鸡翅包饭"]

    r = _revise("加个祛寒羊肉汤", ["鸡翅包饭"])
    assert r["ok"] and r["add"] == ["祛寒羊肉汤"]
    assert r["order"] == ["鸡翅包饭", "祛寒羊肉汤"]

    r = _revise("把竹荪排骨汤推到最后", ["竹荪排骨汤", "鸡翅包饭"])
    assert r["ok"] and r["order"] == ["鸡翅包饭", "竹荪排骨汤"]


def test_revise_is_clause_scoped(monkeypatch):
    """⚠ "去掉 A，把 B 提前" 里的「去掉」**不能**连坐 B。

    第一版是拿整句去撞关键词的，于是 B 被同一句里的"去掉"一起删掉
    （实测 order=["A"]、remove=[] 还报成 source="llm"）。必须按小句判。
    """
    monkeypatch.setenv("FT_LLM", "off")
    r = _revise("去掉鸡翅包饭，把苹果芒果派提前", ["鸡翅包饭", "苹果芒果派"])
    assert "苹果芒果派" in r["order"], "同一句里的「去掉」把没点到名的菜也删了"
    assert r["remove"] == ["鸡翅包饭"]


def test_revise_never_invents_dishes(monkeypatch):
    """不许凭空造菜：菜库里没有的名字一律丢弃。"""
    monkeypatch.setenv("FT_LLM", "off")
    r = _revise("加个不存在的菜", ["鸡翅包饭"])
    assert r["ok"] is False, "把认不出来的名字当成新增菜了"
    assert r["order"] == []


def test_revise_reports_failure_instead_of_silent_noop(monkeypatch):
    """没听懂就**如实说**（ok=false + 可照做的说明），绝不返回"看起来成功"的原菜单。

    ⚠ 这是本项目最在意的一类缺陷：`FT_LLM=off` 下若把"模型没返回"当成成功，
      界面会回一句"已按你的要求更新菜单"，而实际一个菜都没改。
    """
    monkeypatch.setenv("FT_LLM", "off")
    r = _revise("随便弄点好吃的", ["鸡翅包饭"])
    assert r["ok"] is False
    assert r["reason"] == "llm_off" and r["notice"], "没给可照做的说明"
    assert r["order"] == []

    r = _revise("", [])
    assert r["ok"] is False and r["reason"] == "empty"


def test_revise_source_is_never_llm_when_model_is_off(monkeypatch):
    """`FT_LLM=off` 时 source 绝不能报 "llm"（错报会让排查彻底跑偏）。"""
    monkeypatch.setenv("FT_LLM", "off")
    for text, dishes in [("去掉鸡翅包饭", ["鸡翅包饭"]),
                         ("加个祛寒羊肉汤", ["鸡翅包饭"]),
                         ("随便说说", ["鸡翅包饭"])]:
        assert _revise(text, dishes)["source"] != "llm", f"{text} 错报成模型结果"


def test_revise_caps_dish_count(monkeypatch):
    """最终菜数必须收敛到 5 道以内（前端"最多选5道"是同一条约定）。"""
    monkeypatch.setenv("FT_LLM", "off")
    from app.spec.adapter import all_names
    names = list(all_names())
    r = _revise("加上" + "、".join(names[:8]), [names[0]])
    assert r["ok"]
    assert len(r["order"]) <= 5, f"改完之后 {len(r['order'])} 道菜，超过了 5 道上限"
