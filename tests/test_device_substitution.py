"""设备顶替/降级后的标签一致性 —— 以及"故障设备不得被占用"。

事故背景（v3.1 修复）
--------------------
`engine._schedule_once()` 在设备容量被置 0 时会把任务顶替到别的设备
（`DEVICE_FALLBACK`）或降级为人工。**原实现只改 `res`（真实占用），不改 `device`
（对外标签）**，于是产出 `device="蒸箱"` + `unit_id="灶具-1"` 这种自相矛盾的任务
—— 实测 6 个场景共 68 条。

后果不是"显示难看"这么轻：`checker.overlap_allowed()` 是按 `device` 判
"蒸箱同温可共享腔体"豁免的，一个其实占着灶具、却仍标蒸箱的任务会套用该豁免，
**把真实的灶具冲突判成合法重叠**（静默漏报）。

另一处同源修复在 `agent/orchestrator.py`：设备故障时 `use_cpsat=False`，
原实现把**整个精解块**一起跳过，连"认容量覆盖的精确轨"也一并丢了
（`model.capacities()` 读的是 `engine.resource_units()`，是认覆盖的）。
"""
from __future__ import annotations

import pytest

from app.parser.recipe_parser import parse_recipe
from app.scheduler import engine
from app.scheduler.engine import resource_override, schedule
from app.spec.adapter import all_names, get

# unit_id 前缀 → 设备。不占当天设备的两种 unit_id 单独列出。
_DEVICE_PREFIXES = ("灶具", "蒸箱", "烤箱", "冰箱", "洗碗机", "净咖一体机", "烟机")
_NO_DEVICE_UNITS = ("等待", "提前准备")

_NAMES = all_names()[:12]
_RECIPES = [parse_recipe(n, get(n)["ingredients"], get(n)["steps"]) for n in _NAMES]


def _label_conflicts(plan) -> list[str]:
    """device 与"实际占用的 unit"不一致的任务。"""
    bad = []
    for t in plan.tasks:
        unit = str(t.unit_id)
        if unit in _NO_DEVICE_UNITS:
            # 提前准备 / 等待行不占当天设备，device 保留原值是有意的。
            continue
        if unit == "双手-1":
            if t.device != "人工":
                bad.append(f"{t.task_id}: device={t.device} 却占用双手")
            continue
        prefix = unit.rsplit("-", 1)[0]
        if prefix in _DEVICE_PREFIXES and prefix != t.device:
            bad.append(f"{t.task_id}: device={t.device} 却占用 {unit}")
    return bad


def test_device_label_matches_occupied_unit_normally() -> None:
    """正常排程下 device 与 unit_id 必须一致（兼防修复本身引入新不一致）。"""
    bad = _label_conflicts(schedule(_RECIPES, use_cpsat=False))
    assert not bad, "device 与 unit_id 不一致:\n  " + "\n  ".join(bad[:15])


@pytest.mark.parametrize("dead", ["灶具", "蒸箱", "烤箱", "冰箱"])
def test_device_label_follows_substitution_when_a_device_dies(dead: str) -> None:
    """设备故障后，被顶替/降级的任务必须**改名**，不能留下矛盾的旧标签。"""
    with resource_override({dead: 0}):
        plan = schedule(_RECIPES, use_cpsat=False)
    bad = _label_conflicts(plan)
    assert not bad, (f"{dead}=0 时 device 与 unit_id 不一致:\n  " + "\n  ".join(bad[:15]))


@pytest.mark.parametrize("dead", ["灶具", "蒸箱", "烤箱", "冰箱"])
def test_dead_device_is_never_occupied(dead: str) -> None:
    """容量为 0 的设备不得被任何任务占用 —— 这是"设备故障"的硬契约。"""
    with resource_override({dead: 0}):
        plan = schedule(_RECIPES, use_cpsat=False)
    used = [f"{t.task_id}({t.unit_id})" for t in plan.tasks
            if str(t.unit_id).rsplit("-", 1)[0] == dead]
    assert not used, f"{dead} 已故障却被占用: {used[:10]}"


@pytest.mark.parametrize("dead", ["蒸箱", "烤箱"])
def test_substitution_moves_work_instead_of_dropping_it(dead: str) -> None:
    """顶替必须是"换个设备做"，不能靠"少排几步"把指标做好看。

    只断言"故障设备不被占用"是不够的 —— 把任务整段删掉同样满足它。
    这里要求：原本落在故障设备上的任务**仍然存在**（只是换了设备）。
    """
    base = schedule(_RECIPES, use_cpsat=False)
    victims = {t.task_id for t in base.tasks if t.device == dead}
    assert victims, f"前提不成立：正常情况下没有任务落在 {dead} 上，本用例失去意义"
    with resource_override({dead: 0}):
        plan = schedule(_RECIPES, use_cpsat=False)
    have = {t.task_id for t in plan.tasks}
    dropped = sorted(victims - have)
    assert not dropped, f"{dead} 故障后有任务被整段丢弃: {dropped}"


def test_device_failure_still_runs_the_exact_track(monkeypatch) -> None:
    """设备故障（use_cpsat=False）时，精确轨仍必须被调用。

    `use_cpsat=False` 的**唯一**理由是 CP-SAT 的模型不读资源容量覆盖；
    精确轨读的是 `engine.resource_units()`，没有这个毛病，不该被连带关掉。
    """
    from app.agent import orchestrator

    seen: list[tuple] = []
    real = engine.schedule

    def spy(*a, **kw):
        seen.append((kw.get("use_cpsat"), kw.get("use_exact")))
        return real(*a, **kw)

    monkeypatch.setattr(engine, "schedule", spy)
    orchestrator.run(_RECIPES[:6], use_llm=False, time_budget=3.0, use_cpsat=False)

    assert any(c is False and e is True for c, e in seen), (
        "设备故障时精确轨没有被调用（精解块被整体跳过）；"
        f"实际 (use_cpsat, use_exact) = {seen}")


def test_device_failure_plan_respects_capacity_end_to_end() -> None:
    """端到端：故障设备在最终计划里既不被占用、也不残留旧标签。"""
    dead = "蒸箱"
    with resource_override({dead: 0}):
        plan = schedule(_RECIPES, use_cpsat=False, use_exact=True)
    occupied = [t.task_id for t in plan.tasks if str(t.unit_id).startswith(dead)]
    assert not occupied, f"{dead} 已故障却被占用: {occupied[:10]}"
    assert not _label_conflicts(plan), "存在 device/unit_id 不一致的任务"
