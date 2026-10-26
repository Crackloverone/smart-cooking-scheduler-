"""精确模型 (app/scheduler/model.py) 回归测试。

为什么单独建一个文件
--------------------
model.py 是整个项目"排程"这件事的核心交付物 (RCPSP-Batch-Lag 建模 + 三层字典序目标
+ 批次机感知下界), 但在本文件出现之前 **零测试覆盖** —— tests/test_core.py 从头到尾
没有 import 过它。这意味着模型里任何一次改动(温度推断、关键路径下界、目标位权)
都只能靠人肉跑脚本比对, 回归风险极高。

这里只测**不变量** (invariant), 不测具体数值 —— 数值会随参数漂移, 不变量不会:
  - 温度推断的优先级
  - 关键路径在并行分支上不等于时长求和 (D11)
  - 批次机下界必须按温度档打包 (否则高估)
  - LB <= makespan 恒成立
  - 精确解无冲突 / 无依赖违反 / 可复现 (D12)
  - 目标确实优化的是"原始出锅跨度", 不是语义冷等待 (D9)

运行:
    python -m pytest tests/test_model.py -v
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["FT_LLM"] = "off"
os.environ["FT_AGENT_LLM"] = "off"

import pytest

from app.parser.recipe_parser import parse_recipe
from app.scheduler import model as M
from app.scheduler.engine import schedule
from app.models.schema import DEVICE_STEAMER, DEVICE_OVEN, DEVICE_STOVE


def _mk(name, steps):
    return parse_recipe(name, "", steps)


# --------------------------------------------------------------------------
# 1. 温度推断 (D10)
# --------------------------------------------------------------------------
def test_bucket_groups_near_temps():
    """同一温度档 = 同腔体可并行; 180 与 182 必须归到同一档。"""
    assert M._bucket(180.0) == M._bucket(182.0)
    assert M._bucket(None) == "∅"


def test_bucket_none_is_its_own_sentinel():
    """None 用独立哨兵值表示, 不能与任何真实温度撞档。"""
    assert M._bucket(None) not in {M._bucket(0), M._bucket(100), M._bucket(180)}


def test_infer_temp_steamer_defaults_to_100():
    """蒸箱未标温度 -> 100℃ (常压水蒸气), 而不是留空独占一炉。"""
    assert M._infer_temp(DEVICE_STEAMER, None, []) == M.STEAM_DEFAULT_TEMP


def test_infer_temp_prefers_sibling_mode():
    """同菜同设备已有温度时, 取众数, 且优先于蒸箱默认值。"""
    assert M._infer_temp(DEVICE_STEAMER, None, [120.0, 120.0, 100.0]) == 120.0
    assert M._infer_temp(DEVICE_OVEN, None, [180.0, 180.0, 200.0]) == 180.0


def test_infer_temp_oven_unknown_stays_none():
    """烤箱温度差异太大, 不猜 —— 留空让它保守独占一炉。"""
    assert M._infer_temp(DEVICE_OVEN, None, []) is None


def test_infer_temp_keeps_explicit_value():
    """已标注的温度原样返回, 推断不覆盖真实数据。"""
    assert M._infer_temp(DEVICE_STEAMER, 115.0, [100.0]) == 115.0


def test_extract_tasks_fills_steamer_temp():
    """端到端: 一道未标温度的蒸菜, 抽出的任务温度应为 100 而非 None。"""
    recs = [_mk("蒸蛋", "第1步：打蛋；第2步：放入蒸箱普通蒸8分钟")]
    tasks = M.extract_tasks(recs)
    steam = [t for t in tasks if t["res"] == DEVICE_STEAMER]
    assert steam, "应当抽出蒸箱任务"
    assert all(t["temp"] == M.STEAM_DEFAULT_TEMP for t in steam)


# --------------------------------------------------------------------------
# 2. 关键路径下界 (D11)
# --------------------------------------------------------------------------
def test_critical_path_parallel_branches_not_sum():
    """并行分支: 关键路 = 最长分支, 不是两支之和。

    这是 D11 的核心: 旧实现用"菜内时长求和"当 lb_cp, 在有并行分支的菜谱上
    会得到**高于真实最优**的"下界", 于是出现 makespan < LB 这种不可能的结论。
    """
    tasks = [
        {"key": ("菜", 1), "dur": 3, "deps": []},
        {"key": ("菜", 2), "dur": 15, "deps": [("菜", 1)]},   # 分支 A: 3+15=18
        {"key": ("菜", 3), "dur": 2, "deps": [("菜", 1)]},    # 分支 B: 3+2=5
    ]
    assert M._critical_path(tasks) == 18
    assert M._critical_path(tasks) < sum(t["dur"] for t in tasks)


def test_critical_path_linear_chain_equals_sum():
    """纯串行链上, 关键路恰好等于时长之和。"""
    tasks = [
        {"key": ("菜", 1), "dur": 5, "deps": []},
        {"key": ("菜", 2), "dur": 7, "deps": [("菜", 1)]},
        {"key": ("菜", 3), "dur": 2, "deps": [("菜", 2)]},
    ]
    assert M._critical_path(tasks) == 14


def test_critical_path_empty():
    assert M._critical_path([]) == 0


def test_critical_path_survives_cycle():
    """依赖成环时不得死循环 (返回有限值即可, 排程本身另有环检测)。"""
    tasks = [
        {"key": ("菜", 1), "dur": 3, "deps": [("菜", 2)]},
        {"key": ("菜", 2), "dur": 4, "deps": [("菜", 1)]},
    ]
    assert M._critical_path(tasks) < 1e6


# --------------------------------------------------------------------------
# 3. 批次机感知下界
# --------------------------------------------------------------------------
def test_lower_bound_batch_machine_serializes_buckets():
    """批次机下界必须**按温度档分别打包再相加**, 比"全设备一起打包"更紧。

    两个不同温度档的蒸制各 10 min, Q=4:
      按温度档:  ceil(10/4) + ceil(10/4) = 3 + 3 = 6
      不分温度档: ceil(20/4)             = 5
    6 > 5 —— 异温档必须串行, 所以 6 是更紧(仍然有效)的下界。
    若把两个档混在一起算, 会误以为"20 分钟的活 5 分钟能排完"。
    """
    import math
    tasks = [
        {"key": ("a", 1), "dur": 10, "deps": [], "res": DEVICE_STEAMER,
         "temp": 100.0, "hands": False},
        {"key": ("b", 1), "dur": 10, "deps": [], "res": DEVICE_STEAMER,
         "temp": 120.0, "hands": False},
    ]
    per = {}
    for t in tasks:
        per.setdefault(t["res"], {}).setdefault(M._bucket(t["temp"]), 0)
        per[t["res"]][M._bucket(t["temp"])] += t["dur"]
    q = M.BATCH_Q[DEVICE_STEAMER]
    lb_by_bucket = sum(math.ceil(v / q) for v in per[DEVICE_STEAMER].values())
    lb_pooled = math.ceil(sum(per[DEVICE_STEAMER].values()) / q)
    assert lb_by_bucket == 6
    assert lb_by_bucket > lb_pooled, "按档打包必须比混档打包更紧"


def test_lower_bound_same_bucket_shares_chamber():
    """同温档: 蒸箱 Q=4, 三道各 5 min 的同温蒸菜可共用腔体 -> 只算 5 min。"""
    recs = [
        _mk("蒸A", "第1步：洗净；第2步：蒸箱普通蒸100度5分钟"),
        _mk("蒸B", "第1步：洗净；第2步：蒸箱普通蒸100度5分钟"),
        _mk("蒸C", "第1步：洗净；第2步：蒸箱普通蒸100度5分钟"),
    ]
    lb = M.lower_bound(recs)
    assert lb["lb_energy"] <= 5, f"同温应共享腔体, 得到 {lb['lb_energy']}"


def test_lower_bound_unknown_temp_not_counted():
    """温度未知的任务不摊到任何温度档 (摊了会得到无效的高估下界)。"""
    recs = [_mk("烤X", "第1步：处理；第2步：放入烤箱烤20分钟")]
    tasks = M.extract_tasks(recs)
    oven = [t for t in tasks if t["res"] == DEVICE_OVEN]
    assert oven and oven[0]["temp"] is None, "烤箱无兄弟温度时应当留空"
    lb = M.lower_bound(recs)
    assert lb["lb"] >= 0     # 不崩即可; 重点是下面 makespan >= LB 的用例


@pytest.mark.parametrize("seed", [3, 7])
def test_lower_bound_never_exceeds_makespan(seed):
    """LB <= makespan 必须恒成立 —— 否则"相对最优差距"的汇报没有意义。"""
    import random
    rnd = random.Random(seed)
    pool = [
        "第1步：洗净切块；第2步：放入蒸箱普通蒸100度10分钟；第3步：取出摆盘",
        "第1步：腌制10分钟；第2步：锅中热油炒8分钟；第3步：调味出锅",
        "第1步：焯水；第2步：小火炖25分钟；第3步：加盐",
        "第1步：和面；第2步：放入烤箱上下火180度烤20分钟；第3步：取出晾凉",
        "第1步：泡发3小时；第2步：上锅蒸15分钟；第3步：淋汁",
    ]
    for _ in range(4):
        picks = rnd.sample(pool, 3)
        recs = [_mk(f"菜{i}", s) for i, s in enumerate(picks, 1)]
        plan = schedule(recs)
        lb = M.lower_bound_from_plan(plan)
        assert plan.makespan_min >= lb["lb"] - 1e-6, (
            f"总时长 {plan.makespan_min} < 下界 {lb['lb']}")


# --------------------------------------------------------------------------
# 4. 精确模型健全性 + 可复现 (D12)
# --------------------------------------------------------------------------
def _rec(name: str):
    """从官方 100 道菜库取一道菜并解析 —— 用**真实数据**跑测试, 而不是手搓样例。"""
    from app.parser.recipe_parser import parse_recipe
    from app.spec.adapter import get
    r = get(name)
    return parse_recipe(name, r["ingredients"], r["steps"])


def _sample_recipes():
    return [
        _mk("清蒸鱼", "第1步：洗净改刀；第2步：蒸箱普通蒸100度12分钟；第3步：淋热油"),
        _mk("小炒肉", "第1步：切肉腌制5分钟；第2步：锅中热油炒6分钟；第3步：出锅"),
        _mk("烤鸡翅", "第1步：腌制20分钟；第2步：放入烤箱上下火200度烤18分钟"),
    ]


def test_solve_exact_returns_plan():
    plan = M.solve_exact(_sample_recipes(), time_budget=2.0)
    assert plan is not None
    assert plan.tasks, "精确解不应为空"


def test_solve_exact_no_conflict_no_dep_violation():
    plan = M.solve_exact(_sample_recipes(), time_budget=2.0)
    from app.scheduler.checker import check_plan
    chk = check_plan(plan)
    # check_plan 的返回形状在不同版本略有差异, 统一取错误列表
    errs = chk.get("errors") if isinstance(chk, dict) else chk
    assert not errs, f"精确解存在冲突/依赖违反: {errs}"


def test_solve_exact_meets_lower_bound():
    plan = M.solve_exact(_sample_recipes(), time_budget=2.0)
    lb = M.lower_bound_from_plan(plan)
    assert plan.makespan_min >= lb["lb"] - 1e-6


def test_solve_exact_is_deterministic():
    """同一实例两次求解的总时长必须一致 (D12: 并列最优曾导致结果不可复现)。"""
    recs = _sample_recipes()
    a = M.solve_exact(recs, time_budget=2.0)
    b = M.solve_exact(recs, time_budget=2.0)
    assert a.makespan_min == b.makespan_min, (
        f"结果不可复现: {a.makespan_min} vs {b.makespan_min}")


def test_solve_exact_spread_is_raw_span():
    """出锅差 = max(各菜完成) - min(各菜完成), 即赛题口径的原始跨度 (D9)。

    而不是语义冷等待 Σσ_d(T−F_d)。两者在"冷菜可以早出锅"时方向相反。
    """
    plan = M.solve_exact(_sample_recipes(), time_budget=2.0)
    finishes = [v for v in plan.dish_finish.values()]
    assert finishes
    raw_span = max(finishes) - min(finishes)
    assert abs(plan.finish_gap_min - raw_span) < 1.5, (
        f"finish_gap={plan.finish_gap_min} 与原始跨度 {raw_span} 不符")


def test_solve_exact_empty_recipes_returns_none():
    assert M.solve_exact([], time_budget=1.0) is None


# --------------------------------------------------------------------------
# 5. 两条轨任务集口径一致 (D5)
# --------------------------------------------------------------------------
def test_extract_tasks_allow_preprocess_default_matches_engine():
    """默认 allow_preprocess=True, 与 engine 的 prep_ahead 口径一致。

    若默认取 False, 精确轨会把备菜步排进人工池 -> 任务集与贪心轨不同 ->
    "精确解更优"的对比失去意义 (实测总时长虚高 54%)。
    """
    import inspect
    sig = inspect.signature(M.solve_exact)
    assert sig.parameters["allow_preprocess"].default is True
    sig2 = inspect.signature(M.extract_tasks)
    assert sig2.parameters["allow_preprocess"].default is False, (
        "extract_tasks 的默认由调用方(solve_exact)显式传入, 保持 False 以免误用")


# --------------------------------------------------------------------------
# 6. 连续手动上限 (C5) —— 编码正确性 + 口径统一 (v2.3)
# --------------------------------------------------------------------------
def test_hand_gap_tol_is_single_source_of_truth():
    """B 的定义只有一个来源: metrics.HAND_GAP_TOL。

    旧问题: 模型按"严格无空闲"算连续, 而 metrics.hand_streak 允许 1 分钟空隙,
    同一份计划两边报数不同 —— "模型保证 B<=cap" 这句话就没有意义了。
    """
    import inspect
    from app.scheduler.metrics import HAND_GAP_TOL, hand_streak
    assert HAND_GAP_TOL == 1.0
    assert inspect.signature(hand_streak).parameters["gap_tol"].default == HAND_GAP_TOL


def test_hand_streak_tolerance_semantics():
    """1 分钟空隙算连续; 2 分钟空隙算断开。"""
    from app.models.schema import ScheduledTask, SchedulePlan
    from app.scheduler.metrics import hand_streak

    def mk(s, e):
        return ScheduledTask(task_id=f"t{s}", dish="d", step_id=int(s), raw_text="x",
                             device="人工", unit_id="双手-1", start_min=float(s),
                             end_min=float(e), duration_min=float(e - s),
                             needs_human=True, unattended=False)

    # 10-20 + 21-31 (空隙 1 分钟) -> 连续, 共 20
    p1 = SchedulePlan(tasks=[mk(10, 20), mk(21, 31)], makespan_min=31.0, finish_gap_min=0.0)
    assert hand_streak(p1) == 20.0
    # 10-20 + 22-32 (空隙 2 分钟) -> 断开, 最长 10
    p2 = SchedulePlan(tasks=[mk(10, 20), mk(22, 32)], makespan_min=32.0, finish_gap_min=0.0)
    assert hand_streak(p2) == 10.0


def test_hand_cap_is_actually_enforced():
    """开了 hand_cap 之后, 交出的计划必须真的满足它 —— 不是"模型里写了"而已。

    这条测试的对手是旧实现: 时间索引编码 O(n·H) 在预算内解不出来,
    `_give_up()` 会**静默丢弃**约束, 返回一份看起来正常、其实没受约束的计划。
    现在 (a) 改用 O(m) 行程编码 (见 (C5) 的说明), (b) 解不出来时**显式标记**
    `hand_cap_enforced=false` + 原因, (c) 解完用 metrics.hand_streak 复核一次。
    """
    from app.scheduler.metrics import hand_streak
    from app.spec.adapter import all_names, get
    from app.scheduler import engine as E

    names = all_names()
    recs = [_rec(n) for n in names[:3]]
    greedy = E._schedule(recs)
    hint = {(t.dish, t.step_id): float(t.start_min)
            for t in greedy.tasks if t.dish and t.start_min >= 0}
    longest = max((t.duration_min for t in greedy.tasks if t.needs_human), default=1.0)
    cap = int(max(longest, hand_streak(greedy) * 0.75))

    plan = M.solve_exact(recs, time_budget=6.0, hand_cap=cap, hint=hint)
    assert plan is not None
    meta = M.last_meta()
    if meta.get("hand_cap_enforced"):
        assert hand_streak(plan) <= cap + 1e-6
    else:
        # 解不出来时必须**显式标记**, 不允许静默
        assert meta.get("hand_cap_requested") == cap
        assert meta.get("hand_cap_dropped_reason")


def test_hand_cap_solvable_at_production_budget():
    """行程编码必须能在**生产预算 (1.5 s)** 内解出满足上限的计划。

    对手是旧实现: O(n·H) 的时间索引编码在 1.5 s 内解不出来 ⇒ 降级 ⇒ 交付一份
    **违反上限**的计划。实测同一用例(下面这个 5 菜组合)旧编码要 8 s 才 FEASIBLE,
    新编码 1.5 s 直接 OPTIMAL —— 说明卡点是搜索规模, 不是不可行。
    """
    from app.scheduler.metrics import hand_streak
    from app.scheduler import engine as E

    names = ["小碗蒸肉", "榴莲酥", "烤鱿鱼干", "大理石饼干", "茯苓饼干"]
    recs = [_rec(n) for n in names]
    greedy = E._schedule(recs)
    longest = max((t.duration_min for t in greedy.tasks if t.needs_human), default=1.0)
    hint = {(t.dish, t.step_id): float(t.start_min)
            for t in greedy.tasks if t.dish and t.start_min >= 0}
    cap = int(max(longest, 30))
    assert hand_streak(greedy) > cap, "前提失效: 贪心解本就满足上限, 本用例不再有区分度"

    plan = M.solve_exact(recs, time_budget=1.5, hand_cap=cap, hint=hint)
    assert plan is not None
    meta = M.last_meta()
    assert meta.get("hand_cap_enforced") is True, (
        f"1.5 s 生产预算内没能满足 hand_cap (status={meta.get('status')}) —— "
        "行程编码是否退化回了 O(n·H)?")
    assert hand_streak(plan) <= cap + 1e-6


def test_engine_keeps_hand_cap_satisfying_plan(monkeypatch):
    """择优环节**不得**把"满足 hand_cap"的计划丢掉。

    对手是旧实现: `engine.schedule` 的三轨择优只按 (Γ,T) 字典序比较, 而贪心轨与
    CP-SAT 轨**根本不接受** hand_cap ⇒ 精确轨那份真正达标的计划, 只要 Γ 差一点点
    就会被挤掉, 最终交付一份违反上限的计划。实测 seed=11 的 6 组里有 2 组如此。
    口径: 硬约束只做**可行性**, 绝不进目标函数。
    """
    from app.scheduler.metrics import hand_streak
    from app.scheduler import engine as E

    names = ["小碗蒸肉", "榴莲酥", "烤鱿鱼干", "大理石饼干", "茯苓饼干"]
    recs = [_rec(n) for n in names]
    monkeypatch.setenv("FT_HAND_CAP", "30")
    plan = E.schedule(recs)
    assert hand_streak(plan) <= 30 + 1e-6, "择优把满足 hand_cap 的计划丢掉了"


def test_solve_meta_always_reports_hand_cap_state():
    """无论走哪条路径, meta 都要能回答"要没要上限、生效没有"。"""
    from app.spec.adapter import all_names
    recs = [_rec(n) for n in all_names()[:2]]
    M.solve_exact(recs, time_budget=2.0)
    meta = M.last_meta()
    assert "hand_cap_requested" in meta and "hand_cap_enforced" in meta
    assert meta["hand_cap_requested"] is None      # 没要


def test_lower_bound_from_plan_never_exceeds_makespan():
    """从计划反推的下界恒 <= makespan —— 否则汇报自相矛盾。"""
    from app.scheduler import engine as E
    from app.spec.adapter import all_names
    for i in range(5):
        recs = [_rec(n) for n in all_names()[i * 3:i * 3 + 3]]
        plan = E.schedule(recs, use_cpsat=False, use_exact=False)
        lb = M.lower_bound_from_plan(plan)
        live = [t for t in plan.tasks if t.start_min >= 0]
        mk = max(float(plan.makespan_min), max((t.end_min for t in live), default=0.0))
        assert lb["lb"] <= mk + 1e-6, f"下界 {lb['lb']} > makespan {mk}"


def test_horizon_is_tightened_by_hint():
    """给了完整可行解 hint 时, 时间轴必须被收紧 —— 这是 hand_cap 能解出来的前提。"""
    from app.scheduler import engine as E
    from app.spec.adapter import all_names
    recs = [_rec(n) for n in all_names()[:4]]
    greedy = E._schedule(recs)
    hint = {(t.dish, t.step_id): float(t.start_min)
            for t in greedy.tasks if t.dish and t.start_min >= 0}
    M.solve_exact(recs, time_budget=2.0, hint=hint)
    h_tight = M.last_meta()["horizon"]
    M.solve_exact(recs, time_budget=2.0)
    h_loose = M.last_meta()["horizon"]
    assert h_tight <= h_loose, f"hint 没起到收紧作用: {h_tight} vs {h_loose}"


# --------------------------------------------------------------------------
# 5. 下界口径回归 (2026-10-04 修掉的两个**静默**缺陷)
#
# 为什么这两条必须有独立测试:
#   原来那三条"看着很稳"的护栏**结构上抓不到它们** ——
#     · test_critical_path_* 自造 dict, 键类型**本来就匹配**, 走不到键转换那一行;
#     · test_lower_bound_from_plan_never_exceeds_makespan 只断言 lb <= makespan,
#       而下界被低估时这个断言**恒真**;
#     · lb_probe 只报数, 从不与"手算关键路"对照。
#   结果: 官方 5 菜实例真实关键路 54 被报成 18, LB 从 54 掉到 27, gap 从 0% 虚报成 100%,
#   全程零报错。下面每一条都**真的去调被测代码**并断言具体数值。
# --------------------------------------------------------------------------
def _syn_task(dish, sid, start, dur, device=DEVICE_STEAMER, unit=None,
              deps=(), human=False):
    from app.models.schema import ScheduledTask
    return ScheduledTask(
        task_id=f"{dish}#{sid}", dish=dish, step_id=sid, raw_text="",
        device=device, unit_id=unit or f"{device}-1",
        start_min=float(start), end_min=float(start + dur), duration_min=float(dur),
        needs_human=human, unattended=not human,
        depends_on=[f"{d[0]}#{d[1]}" for d in deps])


def test_dep_key_normalizes_step_id_to_int():
    """`_dep_key("菜#3")` 必须给出 (菜, 3) —— step_id 是 **int**, 不是字符串。

    缺陷史: 旧写法 `tuple(d.split("#"))` 给出 ("菜","3"), 与 `_critical_path` 里
    `dur` 的键 ("菜", 3) 永不相等 ⇒ `[d for d in deps if d in dur]` 恒为空 ⇒
    **所有依赖边被静默丢弃**, 关键路退化成"最长单步时长"。
    """
    assert M._dep_key("菜#3") == ("菜", 3)
    assert isinstance(M._dep_key("菜#3")[1], int)
    assert M._dep_key("菜#x") == ("菜", "x")        # 异常输入不得抛
    assert M._dep_key("没有分隔符") == ("没有分隔符", "")


def test_lower_bound_critical_path_follows_real_edges():
    """下界的关键路必须**真的沿依赖边走**, 而不是"最长单步时长"。

    构造: 链 5 -> 7 -> 2 (共 14) + 一个 10 分钟的孤立长步。
    正确 lb_cp = 14; 边被丢弃时会退化成 10。
    """
    from app.models.schema import SchedulePlan
    plan = SchedulePlan(tasks=[
        _syn_task("甲", 1, 0, 5),
        _syn_task("甲", 2, 5, 7, deps=[("甲", 1)]),
        _syn_task("甲", 3, 12, 2, deps=[("甲", 2)]),
        _syn_task("乙", 1, 0, 10),
    ], makespan_min=14.0)
    lb = M.lower_bound_from_plan(plan)
    assert lb["lb_cp"] == 14, f"关键路没沿依赖边走, 得到 {lb['lb_cp']}"


def test_lower_bound_counts_wait_steps_in_critical_path():
    """"等待"步不占机器/人手, 但**是真实流逝时间**, 必须计入关键路。

    缺陷史: 旧实现把 `unit_id in ("等待","提前准备")` 一并排除, 把链切断 ——
    官方实例「三味蒸鳗鱼」的 腌制10min / 浸泡20min 都成了断点,
    真实活跃关键路 54 被算成 25。
    """
    from app.models.schema import SchedulePlan
    plan = SchedulePlan(tasks=[
        _syn_task("甲", 1, 0, 10, device="人工", unit="等待"),
        _syn_task("甲", 2, 10, 8, deps=[("甲", 1)], human=True),
        _syn_task("甲", 3, 18, 20, device="人工", unit="等待", deps=[("甲", 2)]),
        _syn_task("甲", 4, 38, 8, deps=[("甲", 3)], human=True),
    ], makespan_min=46.0)
    lb = M.lower_bound_from_plan(plan)
    assert lb["lb_cp"] == 46, f"等待步没计入关键路, 得到 {lb['lb_cp']}"
    # 但等待步**不许**进人工总工时 / 批次机产能
    assert lb["lb_hand"] == 16, f"等待步混进了人工工时: {lb['lb_hand']}"


def test_lower_bound_gap_is_meaningful_on_real_recipes():
    """真实菜谱上最优性间隙必须落在合理范围 —— 抓"下界又静默变小"的数量级回归。

    修好前: gap 中位 28.6% / max 576%; 修好后: 中位 0% / 72% 实例 makespan == LB。
    这里只取宽松上限 (60%), 不锁具体数值。
    """
    import statistics
    from app.spec.adapter import all_names
    names = all_names()
    gaps = []
    for i in range(6):
        recs = [_rec(n) for n in names[i * 4:i * 4 + 4]]
        plan = schedule(recs, use_cpsat=False, use_exact=False)
        live = [t for t in plan.tasks if t.start_min >= 0]
        mk = max(float(plan.makespan_min), max((t.end_min for t in live), default=0.0))
        lb = M.lower_bound_from_plan(plan)
        if lb["lb"] > 0:
            gaps.append((mk - lb["lb"]) / lb["lb"] * 100)
    assert gaps, "没有产出任何用例"
    assert statistics.median(gaps) <= 60, f"gap 中位异常: {statistics.median(gaps)}"


# --------------------------------------------------------------------------
# 6. 保热基准 (2026-10-04): 冷等待惩罚必须从"出锅时刻"起算
#
# 漏洞形态: 旧实现保热/冷等待用 dish_finish(该菜**所有步骤**的 max end) 当基准。
# 于是"把一条 1 分钟的收尾步(撒葱花 / 尽情品尝吧)停在时间线最末端"会让
# 完成时刻 == T ⇒ cold = 0 ⇒ 惩罚被完全掩掉, 求解器**被奖励**去"早做熟、干等到最后"。
# 实测 60 组: 报告 Γ 与真实烹饪跨度差额中位 23 min, 最坏 175 min。
# --------------------------------------------------------------------------
def test_dish_cook_finish_excludes_trailing_steps():
    """`dish_cook_finish` 取"最后一次进加热设备"的结束, 不是最后一步。"""
    from app.models.schema import SchedulePlan
    from app.scheduler.metrics import dish_cook_finish
    plan = SchedulePlan(
        tasks=[
            _syn_task("甲", 1, 0, 20, device=DEVICE_STEAMER),
            _syn_task("甲", 2, 100, 1, device="人工", unit="双手-1", deps=[("甲", 1)]),
        ],
        makespan_min=101.0, dish_finish={"甲": 101.0})
    assert dish_cook_finish(plan)["甲"] == 20, "收尾步不该算进出锅时刻"


def test_dish_cook_finish_falls_back_for_cold_dishes():
    """没有任何加热步的菜(凉拌/沙拉)退化为完成时刻, 不得缺席。"""
    from app.models.schema import SchedulePlan
    from app.scheduler.metrics import dish_cook_finish
    plan = SchedulePlan(
        tasks=[_syn_task("乙", 1, 0, 5, device="人工", unit="双手-1", human=True)],
        makespan_min=5.0, dish_finish={"乙": 5.0})
    assert dish_cook_finish(plan)["乙"] == 5.0


def test_hot_penalty_base_is_cook_finish_not_finish_time():
    """保热基准必须是"出锅时刻" —— 用完成时刻时该项恒 ≈ 0, 等于没参与择优。

    缺陷史: 旧实现用 `plan.dish_finish`(该菜**所有步骤**的 max end)。把一条 1 分钟的
    收尾步("尽情品尝吧")停在时间线最末端 ⇒ 完成时刻 == T ⇒ 惩罚被掩掉,
    求解器**被奖励**去"早做熟、干等到最后"。实测 60 组: 报告 Γ 与真实烹饪跨度
    差额中位 23 min, 最坏 175 min。
    """
    import inspect
    from app.agent import objective
    src = inspect.getsource(objective.score_plan)
    # ⚠ 必须先剥掉注释再断言: 上面那段口径说明里就写着这个函数名 —— 直接对全文做
    #    子串断言会被自己的注释误伤(项目里栽过两次)。
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    assert "dish_cook_finish" in code, "保热基准又退回完成时刻了"
    assert "FT_HOT_BASE" in inspect.getsource(objective.hot_base), "口径开关丢了, A/B 没法复现"
    assert "FT_HOT_BUDGET" in inspect.getsource(objective.hot_budget)


def test_hot_penalty_is_capped_by_a_budget_priced_in_gap(monkeypatch):
    """保热项必须有**以 Γ 计价的总预算** —— 它只做破平局, 不许夺 Γ 的权。

    量过 (6 seed × 50 组, `.cache/_hot_budget.py`):
      · 完成时刻基准 + 无预算 ⇒ 该项恒 ≈ 0, 出锅差<5min **90.3%** (等于没参与择优);
      · 出锅时刻基准 + 无预算 ⇒ 保热违规最小(1.7), 但 <5min 掉到 **80.3%**
        —— 一道菜等 170 min ⇒ 30×0.9×170 ≈ 4590 ≫ w_gap·Γ = 180;
      · 出锅时刻基准 + 预算 = N 分钟 Γ ⇒ 任何 Γ 领先 >N 分钟的方案都不会被翻盘。
    """
    from app.models.schema import SchedulePlan
    from app.agent.priors import DishPrior
    from app.agent.objective import score_plan, SchedulingPolicy
    from app.scheduler.metrics import evaluate

    def hot_term(budget):
        monkeypatch.setenv("FT_HOT_BASE", "cook")
        monkeypatch.setenv("FT_HOT_BUDGET", str(budget))
        # 甲 10 分钟出锅, 乙到 110 才出锅 ⇒ 甲出锅后干等 100 分钟 (hold 10 ⇒ over 90)
        plan = SchedulePlan(
            tasks=[_syn_task("甲", 1, 0, 10),
                   _syn_task("乙", 1, 100, 10, device=DEVICE_OVEN)],
            makespan_min=110.0, finish_gap_min=100.0,
            dish_finish={"甲": 10.0, "乙": 110.0})
        priors = {"甲": DishPrior(dish="甲", serve_hot=1.0, hold_min=10.0),
                  "乙": DishPrior(dish="乙", serve_hot=1.0, hold_min=10.0)}
        pol = SchedulingPolicy()
        kpi = evaluate(plan)
        return score_plan(plan, kpi, pol, priors) - score_plan(plan, kpi, pol, None), pol

    d0, _ = hot_term(0)
    assert d0 == 0, "预算=0 时保热项仍在参与择优(应与旧行为完全等价)"
    d1, pol = hot_term(1)
    assert 0 < d1 <= pol.w_gap + 1e-6, f"保热项超过了 1 分钟 Γ 的预算: {d1}"
    doff, pol = hot_term("off")
    assert abs(doff - pol.w_hot * 1.0 * 90) < 1e-6, f"无预算时的原始保热值不对: {doff}"
    assert doff > pol.w_gap, "无预算时保热项本应能夺权 —— 这正是必须加预算的原因"


def test_exact_model_uses_cook_finish_for_cold():
    """model.solve_exact 的 cold/over 必须建在 F_cook 上, 且开关可退回旧口径。"""
    import inspect
    from app.scheduler import model as _M
    src = inspect.getsource(_M.solve_exact)
    assert "F_cook" in src and "FT_COLD_BASE" in src, "精确模型的保热基准被改回完成时刻了"
    assert "F_cook.get(dish, f)" in src, "cold 没改用 F_cook"


# --------------------------------------------------------------------------
# 6. 出锅差 Γ 的独立下界 (2026-10-04 新增)
#
# 背景: 此前只有**总时长 T** 的下界, Γ (评分表主项) **完全没有下界**,
# 于是"Γ 是不是最优"只能靠"相信求解器"。gamma_lb_from_plan 用"末步串行差"
# 给出一个**放松但严格有效**的下界: 同一容量 1 资源上的各菜末步必须串行,
# 首末差距 >= Σd − max d。三条排除项 (蒸箱/容量>1/等待) 漏掉任何一条都会
# 造出"下界 > 真实 Γ"的假下界。
# --------------------------------------------------------------------------
def _plan_of(tasks, gap=None):
    from app.models.schema import SchedulePlan
    fin = {}
    for t in tasks:
        if t.dish and t.start_min >= 0:
            fin[t.dish] = max(fin.get(t.dish, 0.0), t.end_min)
    mk = max((t.end_min for t in tasks), default=0.0)
    g = (max(fin.values()) - min(fin.values())) if fin else 0.0
    return SchedulePlan(tasks=list(tasks), makespan_min=mk,
                        finish_gap_min=(gap if gap is not None else g), dish_finish=fin)


def test_gamma_lb_counts_serial_last_steps():
    """三菜末步都在 `双手-1` (容量 1) 上, 时长 2/1/1 ⇒ Γ_lb = (2+1+1) − 2 = 2。"""
    from app.models.schema import DEVICE_NONE
    tasks = [_syn_task("甲", 1, 0, 2, device=DEVICE_NONE, unit="双手-1", human=True),
             _syn_task("乙", 1, 2, 1, device=DEVICE_NONE, unit="双手-1", human=True),
             _syn_task("丙", 1, 3, 1, device=DEVICE_NONE, unit="双手-1", human=True)]
    assert M.gamma_lb_from_plan(_plan_of(tasks)) == 2.0


def test_gamma_lb_excludes_steamer_and_stove():
    """蒸箱 (槽位号不是机器) 与灶具 (2 个灶眼) 上的末步**不得**计入串行差。"""
    # 三个末步都在蒸箱的不同"槽位"上 -> 同温可无限共享 -> 下界必须是 0
    steam = [_syn_task("甲", 1, 0, 10, unit="蒸箱-1"),
             _syn_task("乙", 1, 0, 10, unit="蒸箱-2"),
             _syn_task("丙", 1, 0, 10, unit="蒸箱-3")]
    assert M.gamma_lb_from_plan(_plan_of(steam)) == 0.0
    # 三个末步都在灶具 -> 2 个灶眼可并行 -> 也不得计入
    stove = [_syn_task("甲", 1, 0, 3, device=DEVICE_STOVE, unit="灶具-1"),
             _syn_task("乙", 1, 3, 3, device=DEVICE_STOVE, unit="灶具-1"),
             _syn_task("丙", 1, 6, 3, device=DEVICE_STOVE, unit="灶具-1")]
    assert M.gamma_lb_from_plan(_plan_of(stove)) == 0.0


def test_gamma_lb_ignores_wait_steps():
    """`等待`不占资源, 不能当"末步"参与串行差 (否则会算出 Γ_lb > Γ 的假下界)。"""
    from app.models.schema import DEVICE_NONE
    tasks = [_syn_task("甲", 1, 0, 2, device=DEVICE_NONE, unit="双手-1", human=True),
             _syn_task("乙", 1, 0, 2, device=DEVICE_NONE, unit="双手-1", human=True),
             _syn_task("丙", 1, 5, 40, device=DEVICE_NONE, unit="等待")]
    # 丙的"末步"是等待步 -> 必须被跳过, 于是只有甲/乙两个末步: 2+2-2 = 2
    assert M.gamma_lb_from_plan(_plan_of(tasks)) == 2.0


def test_gamma_lb_never_exceeds_finish_gap():
    """真实菜谱上 Γ_lb <= Γ 必须恒成立 —— 否则就是假下界。"""
    from app.scheduler import engine as E
    from app.spec.adapter import all_names
    for i in range(6):
        recs = [_rec(n) for n in all_names()[i * 3:i * 3 + 4]]
        plan = E.schedule(recs, use_cpsat=False, use_exact=False)
        glb = M.gamma_lb_from_plan(plan)
        assert glb <= float(plan.finish_gap_min) + 1e-6, (
            f"Γ_lb {glb} > Γ {plan.finish_gap_min}")
