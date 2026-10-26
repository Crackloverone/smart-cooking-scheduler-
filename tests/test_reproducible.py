"""排程**可复现性**护栏（L5，2026-10-09）。

为什么这条比"快"重要
--------------------
评分要核对"总耗时与理论最优解对比"——同一输入两次给出不同计划，这项就没法算，
评委也无法复核。而实测确实抖过：

    CP-SAT 轨固定 `num_search_workers = 4`（多线程并行搜索）+ 墙钟超时 +
    接受 FEASIBLE 次优解 ⇒ 哪个 worker 先撞出好解取决于线程调度。
    同一 12 菜组合连跑 6 次：**134 / 135 / 139 / 143**（4 种不同结果）。
    20 菜更极端：**194 与 373.4**（差近 2 倍）。

修法是把 worker 数降到 1（单线程搜索 ⇒ 确定性）。实测**还更快**：
12 菜 1698ms vs 1789ms，20 菜 2014ms vs 2027ms —— 多 worker 在大问题上
的协调开销 + 时间预算被切成多份，抵消了并行收益。

⚠ 这些用例跑的是**真求解器**（3–12 菜），不是 mock；所以比普通单测慢，
   但它们是"结果能不能被复核"的唯一保证。
"""
import os
import random
from pathlib import Path

import pytest

from app.parser.recipe_parser import parse_recipe
from app.scheduler import engine
from app.spec.adapter import all_names, get, plan_recipes


@pytest.fixture(scope="module")
def names():
    return all_names()


def _recs(names, sel):
    return [parse_recipe(n, get(n)["ingredients"], get(n)["steps"]) for n in sel]


def _signature(plan):
    """计划的**可比签名**：出锅差、总时长、任务数、每道菜的出锅时刻。"""
    return (round(plan.finish_gap_min, 3), round(plan.makespan_min, 3),
            len(plan.tasks),
            tuple(sorted((k, round(v, 3)) for k, v in (plan.dish_finish or {}).items())))


# --------------------------------------------------------------------------- 1
def test_同输入连跑结果逐位一致(names):
    """同一组合连跑 4 次，签名必须完全相同。

    用的是**已实测会抖**的那组 12 菜（seed=20261009 的采样序列第 3 组）：
    改回多 worker 时这条会立刻变红。
    """
    rnd = random.Random(20261009)
    sets = [rnd.sample(names, n) for n in (5, 9, 12, 20)]
    for sel in (sets[2], sets[3]):          # 12 菜（已知会抖）+ 20 菜（抖得最凶）
        recs = _recs(names, sel)
        plan_recipes(recs)                  # warm-up
        sigs = {_signature(plan_recipes(recs)) for _ in range(4)}
        assert len(sigs) == 1, (
            f"{len(sel)} 菜组合连跑 4 次出现 {len(sigs)} 种不同结果：\n"
            + "\n".join(f"    {s}" for s in sorted(sigs))
            + "\n  ⇒ CP-SAT 又被设成多 worker 了？见 app/scheduler/cpsat.py")


def test_赛题范围三到五菜稳定(names):
    """赛题只考 3–5 菜，这一档必须绝对稳定。"""
    rnd = random.Random(7)
    for _ in range(3):
        sel = rnd.sample(names, rnd.randint(3, 5))
        recs = _recs(names, sel)
        plan_recipes(recs)
        sigs = {_signature(plan_recipes(recs)) for _ in range(3)}
        assert len(sigs) == 1, f"{len(sel)} 菜不稳定：{sigs}"


# --------------------------------------------------------------------------- 2
def test_cpsat默认单线程(monkeypatch):
    """`num_search_workers` 默认必须是 1 —— 可复现性的**唯一**保证。

    ⚠ 别为了"快一点"改回 4：实测小问题只快 5%（110 vs 116ms，余量 30 倍，
      毫无意义），大问题反而更慢且**结果不可复现**。
    """
    monkeypatch.delenv("FT_CPSAT_WORKERS", raising=False)
    src = Path(__file__).resolve().parents[1] / "app" / "scheduler" / "cpsat.py"
    text = src.read_text(encoding="utf-8")
    assert 'os.environ.get("FT_CPSAT_WORKERS", "1")' in text, (
        "cpsat.py 的默认 worker 数不是 1 —— 排程结果会变得不可复现")
    assert "random_seed" in text, "cpsat.py 没有设 random_seed"


def test_开关缓存不改变结果(names):
    """`FT_FLAG_CACHE` 只影响"读几遍环境变量"，**绝不该**改变任何排程结果。

    （缓存本身是模块级常量，同进程内改不掉，所以这里只做行为等价性断言：
      开关打开时同一输入重复跑，结果依然稳定。）
    """
    sel = names[:5]
    recs = _recs(names, sel)
    plan_recipes(recs)
    sigs = {_signature(plan_recipes(recs)) for _ in range(3)}
    assert len(sigs) == 1, f"开关缓存打开了结果却变了：{sigs}"


def test_冷却开关缓存可被重置(monkeypatch):
    """`reset_flag_cache()` 必须让新设的环境变量立刻生效。

    ⚠ 这是"缓存"与"模块级变量"的分界：若用模块级变量，测试里
      `monkeypatch.setenv("FT_COOLDOWN_RATE", ...)` 会静默失效，
      于是所有依赖开关的用例都在测一个错误的配置。
    """
    monkeypatch.setenv("FT_COOLDOWN_RATE", "3.5")
    engine.reset_flag_cache()
    assert engine.cooldown_rate() == 3.5
    monkeypatch.setenv("FT_COOLDOWN_RATE", "5.0")
    engine.reset_flag_cache()
    assert engine.cooldown_rate() == 5.0
    monkeypatch.delenv("FT_COOLDOWN_RATE")
    engine.reset_flag_cache()
    assert engine.cooldown_rate() == engine.COOLDOWN_MIN_PER_100C


# --------------------------------------------------------------------------- 3
def test_可证最优短路没有被重新引入():
    """短路方案实测会**变差**（45 组里 3 组），代码已删 —— 这条钉住它别被捡回来。

    原因：`lower_bound_from_plan` 是**从计划反推**的下界，只在同一任务集口径内
    成立；精解轨会换口径（丢掉提前准备步），于是"解 == 下界"并不代表最优。
    实测反例：3 菜 Γ 0→10、3 菜 T 30→74、5 菜 T 55→76。
    """
    src = Path(__file__).resolve().parents[1] / "app" / "scheduler" / "engine.py"
    text = src.read_text(encoding="utf-8")
    assert "def provably_optimal" not in text, (
        "engine.py 里又出现了 provably_optimal —— 它会导致排程结果变差，"
        "见该文件里那段『不要试图用贪心解已达下界来跳过精解轨』的注释")
    assert "FT_PROOF_SKIP" not in text
