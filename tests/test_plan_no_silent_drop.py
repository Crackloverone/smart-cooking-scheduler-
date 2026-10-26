"""P0-1 / P0-3 护栏 —— `/api/plan` 既不许**静默丢菜**，也不许**串味厨房配置**。

背景
----
赛题《接口规范》第 8 条：`cookingTimeline` 与 `recipeDetail` 必须覆盖请求中的
**全部**菜谱，不得遗漏或增加。

原实现：

    for it in request_items:
        rec = get(it["name"])
        if not rec:
            continue          # ← 库外菜名被静默吞掉

后果是响应里 `recipeCount` 变小、`recipeDetail` 少一条，而调用方**无从察觉**：
自动评分脚本不会报错，只会按缺失计 0。这类"看起来正常的残缺计划"是最难排查的
故障 —— 所以护栏的判据必须落在**行为**上，而不是"源码里有没有 continue"。

另一半是赛题原文的"菜谱组合**及厨房设备条件**"：`devices` 必须走请求级覆盖，
既不能改全局 `RESOURCE_UNITS`（并发会互相踩），也要能复现同一份计划。

判据
----
① 请求里每一道菜，要么出现在响应里，要么整个请求以 **422** 明确失败 —— 二者必居其一；
② 只要 HTTP 200，就必须 `recipeCount == len(请求)`；
③ 422 的 detail 必须**列出**到底是哪几道菜解析不了（不能只说"出错了"）；
④ `devices` 覆盖生效、请求间互不串味、同一输入可复现。
"""
from __future__ import annotations

import random

import pytest
from fastapi import HTTPException

from app.api.main import PlanItem, plan_endpoint
from app.spec.adapter import all_names, normalize_name, resolve_items


def _body(names: list[str], devices: dict | None = None, **extra) -> object:
    items = [PlanItem(id=i + 1, name=n, **extra).model_dump() for i, n in enumerate(names)]
    return {"recipes": items, "devices": devices} if devices is not None else items


def _call(names: list[str], devices: dict | None = None, **extra):
    """调端点，把 422/400 也当成一种正常返回值（不抛出去）。"""
    try:
        return 200, plan_endpoint(_body(names, devices, **extra))
    except HTTPException as exc:
        return exc.status_code, exc.detail


# ---------- ① 库内菜名：正常路径 ----------

def test_库内菜名_recipeCount_必须等于请求长度():
    names = all_names()
    pick = names[:5]
    status, out = _call(pick)
    assert status == 200
    assert out["overview"]["recipeCount"] == len(pick)
    assert len(out["cookingTimeline"]) == len(pick)
    assert len(out["recipeDetail"]) == len(pick)
    # 名称必须与请求**完全一致**（规范第 7 条）
    assert [x["name"] for x in out["recipeDetail"]] == pick


# ---------- ② 库外菜名：必须显式失败，不许静默丢 ----------

def test_库外菜名_报422而不是返回残缺计划():
    status, detail = _call(["佛跳墙", "鸡翅包饭"])
    assert status == 422, "库外菜名必须显式失败，不能返回少了菜的计划"
    assert detail["error"] == "unresolved_recipes"
    assert detail["unresolved"] == ["佛跳墙"], "必须点名是哪几道菜解析不了"


def test_全部库外_也报422():
    status, detail = _call(["佛跳墙", "满汉全席"])
    assert status == 422
    assert set(detail["unresolved"]) == {"佛跳墙", "满汉全席"}


def test_库外菜名_自带steps_可以正常排程():
    """评委用自有菜谱时的正路：把 steps 一起传进来。"""
    status, _ = _call(["我家私房菜", "鸡翅包饭"])
    assert status == 422, "没带 steps 的库外菜名仍应报 422"

    body = [
        {"id": 1, "name": "我家私房菜",
         "steps": "第1步：将500g排骨洗净切块；第2步：放入蒸箱开启普通蒸100度20分钟；"
                  "第3步：出锅装盘"},
        {"id": 2, "name": "鸡翅包饭"},
    ]
    out = plan_endpoint(body)
    assert out["overview"]["recipeCount"] == 2
    assert [x["name"] for x in out["recipeDetail"]] == ["我家私房菜", "鸡翅包饭"]


# ---------- ③ 菜名归一：只是"别误报"，不许模糊匹配 ----------

def test_菜名空白与零宽字符可归一命中():
    assert normalize_name(" 鸡翅包饭 ") == "鸡翅包饭"
    assert normalize_name("鸡翅\u200b包饭") == "鸡翅包饭"
    assert normalize_name("ＡＢＣ１２３") == "ABC123"
    status, out = _call([" 鸡翅包饭 ", "酿苦瓜"])
    assert status == 200
    assert out["overview"]["recipeCount"] == 2
    # 输出名必须是**请求原文**，不能被归一后的名字替换
    assert [x["name"] for x in out["recipeDetail"]] == [" 鸡翅包饭 ", "酿苦瓜"]


def test_归一不许做模糊匹配():
    """「豆豉蒸腐竹（台式）」与「豆豉蒸腐竹」是两道菜，绝不能互相命中。"""
    assert normalize_name("豆豉蒸腐竹（台式）") != normalize_name("豆豉蒸腐竹")
    names = set(all_names())
    assert "豆豉蒸腐竹" in names and "豆豉蒸腐竹（台式）" in names
    ok, bad = resolve_items([{"name": "豆豉蒸腐竹"}])
    assert not bad and ok[0]["name"] == "豆豉蒸腐竹"


# ---------- ④ 总判据：任何组合下，200 就必须一道不少 ----------

def test_任何组合下_200就必须一道不少():
    """随机 12 组（库内 + 库外混搭），逐组检查"200 ⇒ recipeCount 等于请求数"。"""
    names = all_names()
    rnd = random.Random(20261008)
    outside = ["佛跳墙", "满汉全席", "宫保鸡丁"]
    for _ in range(12):
        k = rnd.randint(1, 5)
        req = [rnd.choice(names) for _ in range(k)]
        if rnd.random() < 0.5:                      # 随机掺入库外菜名
            req[rnd.randrange(k)] = rnd.choice(outside)
        status, out = _call(req)
        assert status in (200, 422), f"出现意外状态码 {status}"
        if status == 200:
            assert out["overview"]["recipeCount"] == len(req), (
                f"请求 {len(req)} 道菜，响应只报了 {out['overview']['recipeCount']} 道")
            assert len(out["recipeDetail"]) == len(req)
            assert len(out["cookingTimeline"]) == len(req)


# ---------- ⑤ 端点必须"先判后算"：不许先排程再发现少菜 ----------

def test_未解析判定发生在排程之前():
    """422 必须在调用求解器之前抛出 —— 否则白烧一次求解预算（还有 5s 性能线要守）。

    判据：把 `plan_recipes` 换成会爆炸的替身，库外菜名仍应安静地报 422。
    """
    import app.spec.adapter as adapter

    def boom(*a, **kw):  # pragma: no cover - 正常路径不会走到
        raise AssertionError("库外菜名不该走到排程")

    original = adapter.plan_recipes
    adapter.plan_recipes = boom
    try:
        status, detail = _call(["佛跳墙"])
        assert status == 422 and detail["unresolved"] == ["佛跳墙"]
    finally:
        adapter.plan_recipes = original


# ---------- ⑥ P0-3 厨房设备条件：请求级覆盖 ----------

def test_设备条件_无蒸箱时计划里不再出现蒸箱():
    """赛题原文的"及厨房设备条件"：一次请求里就能指定"没有蒸箱"。"""
    pick = ["清蒸鲈鱼", "香烤排骨", "蒜香烤茄子"]
    status, base = _call(pick)
    assert status == 200
    assert "蒸箱" in {x["product"] for x in base["cookingTimeline"]}, \
        "默认厨房下这几道菜应当用到蒸箱（否则本用例失去意义）"

    status, out = _call(pick, devices={"蒸箱": 0})
    assert status == 200
    assert out["overview"]["recipeCount"] == len(pick)
    assert "蒸箱" not in {x["product"] for x in out["cookingTimeline"]}, \
        "蒸箱容量置 0 后，主设备不该再报蒸箱（应顶替到灶具）"


def test_设备条件_单灶眼与双灶眼给出不同计划():
    pick = ["清炒时蔬", "香烤排骨", "蒜香烤茄子"]
    pick = [n for n in pick if n in set(all_names())][:3]
    s1, one = _call(pick, devices={"灶具": 1})
    s2, two = _call(pick, devices={"灶具": 2})
    assert s1 == 200 and s2 == 200
    assert float(one["overview"]["timeSpent"]) >= float(two["overview"]["timeSpent"]), \
        "只有一个灶眼不可能比两个灶眼更快"


def test_设备条件_同一输入可复现():
    pick = ["清蒸鲈鱼", "香烤排骨"]
    _, a = _call(pick, devices={"灶具": 1})
    _, b = _call(pick, devices={"灶具": 1})
    assert a == b, "同一份 {菜名, devices} 必须给出逐位一致的计划"


def test_设备条件_不污染全局容量():
    """`devices` 只在本请求可见 —— 排完必须回到默认厨房。"""
    from app.scheduler.engine import RESOURCE_UNITS, resource_units
    before = dict(RESOURCE_UNITS)
    _call(["清蒸鲈鱼"], devices={"蒸箱": 0, "灶具": 1})
    assert RESOURCE_UNITS == before, "请求级覆盖改写了全局 RESOURCE_UNITS（并发会互相踩）"
    assert resource_units() is RESOURCE_UNITS, "请求外读到的应是全局容量本身"


def test_设备条件_非法输入报400():
    assert _call(["清蒸鲈鱼"], devices={"洗碗机": 1})[0] in (200, 400)
    s, d = _call(["清蒸鲈鱼"], devices={"不存在的设备": 1})
    assert s == 400 and d["error"] == "unknown_device"
    s, d = _call(["清蒸鲈鱼"], devices={"灶具": -1})
    assert s == 400 and d["error"] == "bad_capacity"
    s, d = _call(["清蒸鲈鱼"], devices={"灶具": "两个"})
    assert s == 400 and d["error"] == "bad_capacity"
    s, d = _call(["清蒸鲈鱼"], devices=[1, 2])
    assert s == 400 and d["error"] == "bad_devices"


def test_parse_只给菜名时回落官方菜库():
    """`POST /parse` 是 L1 的评分入口：只给菜名必须能解析出步骤。

    原实现直接拿空字符串去解析，`[{"name":"清蒸鲈鱼"}]` 返回的是 `{"steps": []}`
    —— 一个空壳，而接口文档写的是"留空则自动从官方菜库取"。评委按文档调这个端点
    做 L1 评分，会拿到"解析出来什么都没有"，看起来像解析器完全不工作。
    """
    from app.api.main import RecipeIn, parse as parse_endpoint
    out = parse_endpoint([RecipeIn(name="清蒸鲈鱼")])
    steps = out["parsed"][0]["steps"]
    assert steps, "只给菜名时没有回落到官方菜库（解析出 0 步）"
    assert any(s["device"] == "蒸箱" for s in steps), "清蒸鲈鱼应解析出蒸箱步"


def test_parse_菜名带空白也能命中():
    from app.api.main import RecipeIn, parse as parse_endpoint
    out = parse_endpoint([RecipeIn(name=" 清蒸鲈鱼 ")])
    assert out["parsed"][0]["steps"], "带空白的菜名没有归一命中"


def test_七类设备都在容量表里():
    """赛题原文列的是七类设备。在册 ≠ 有任务，但设备能力覆盖必须看得见。

    官方 100 道菜里「洗碗机 / 烟机」出现 0 次，所以这几台容量当前是空置的 ——
    仍然要写进 `RESOURCE_UNITS`，否则 `/api/plan` 的 `devices` 覆盖参数
    会把 `{"洗碗机":0}` 判成 `unknown_device`，看起来像"系统只支持 5 类设备"。
    """
    from app.models.schema import DEVICES
    from app.scheduler.engine import RESOURCE_UNITS
    missing = [d for d in DEVICES if d not in RESOURCE_UNITS]
    assert not missing, f"这些设备不在容量表里: {missing}"


def test_冰箱按温区计数不是按台数():
    """冷藏室与冷冻室可同时使用 —— 记 1 会把长时冷藏串行化，白白抬高总时长。"""
    from app.scheduler.engine import DEVICE_FRIDGE, RESOURCE_UNITS
    assert RESOURCE_UNITS[DEVICE_FRIDGE] >= 2


def test_裸数组形态仍然可用():
    """官方形态必须一字不改地继续工作（扩展字段是可选的，不是替换）。"""
    status, out = _call(["鸡翅包饭", "酿苦瓜"])
    assert status == 200
    assert set(out.keys()) == {"overview", "cookingTimeline", "ingredientsSummary",
                              "detailTimeline", "recipeDetail"}
    for bad in ("code", "message", "data"):
        assert bad not in out
