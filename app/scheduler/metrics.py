"""
排程 KPI 指标 (移植自 paichan/metrics.py, 适配烹饪场景)
对应赛题评分项: 出锅差/总时长/设备利用率/冲突数/扰动任务数
"""
from __future__ import annotations
from dataclasses import dataclass, field
from app.models.schema import (SchedulePlan, DEVICE_STOVE, DEVICE_STEAMER,
                               DEVICE_OVEN, DEVICE_FRIDGE, DEVICE_HOOD)
from app.scheduler.engine import RESOURCE_UNITS

# 参与"设备利用率"主指标的腔体: 灶具(2 个灶眼)/蒸箱(1)/烤箱(1)。
# 只统计这三类, 理由是它们才是"把菜做熟"的产能 —— 冰箱是储藏/等待设备(长时间必然
# 空闲, 计入只会平白压低所有方案的分数), 烟机是灶具的派生联动(计入等于把灶具重复
# 算一遍)。两者仍出现在 util_detail 明细里, 方便定位空闲。
UTIL_SCOPE = (DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN)
UTIL_DETAIL_SCOPE = UTIL_SCOPE + (DEVICE_FRIDGE, DEVICE_HOOD)

# ---- 「连续手动操作时长」B 的唯一权威定义 (v2.3) ----
# 赛题把它列为独立评分项, 但没给容差口径。这里定死一个常量, 供三处共用:
#   · metrics.hand_streak()      —— 度量
#   · model.solve_exact(hand_cap) —— 约束
#   · 求解后的自证校验            —— 断言"约束真的成立"
# 取值 1.0 分钟: 手上有活的人不会因为中间空了 1 分钟就"解除连续状态";
# 而 >=2 分钟的空隙确实构成一次真正的停顿。
# 注意: 模型约束的是**这个含容差的口径**, 它恒 >= 严格口径 (任何空隙都断开),
# 所以"B <= cap"这个保证在两种读法下都成立 —— 取更保守的一侧。
HAND_GAP_TOL = 1.0


@dataclass
class ScheduleKPI:
    total_tasks: int
    makespan_min: float
    finish_gap_min: float        # 出锅时间差 (优秀<5min)
    device_utilization: float    # 设备(灶具/蒸箱/烤箱)忙碌占比, 见 _utilization 口径说明
    manual_tasks: int
    unattended_tasks: int
    conflict_count: int          # 同设备时间重叠 (应为0)
    depend_violation: int        # 依赖违反 (应为0)
    perturbation_count: int = 0  # 重排后被挪动的任务数 (最小扰动)
    hand_streak_min: float = 0.0  # 用户连续手动操作最长时长 (对应"连续手动操作时长"评分项)
    util_detail: dict = field(default_factory=dict)  # 各设备单元占用率, 便于定位瓶颈


def evaluate(plan: SchedulePlan) -> ScheduleKPI:
    # 冲突检测: 同 unit 时间重叠 (同温共享腔体/共享预热不算冲突, 见 checker.overlap_allowed)
    from app.scheduler.checker import overlap_allowed
    conflict = 0
    by_unit = {}
    for t in plan.tasks:
        if t.unit_id in ("等待", "提前准备"):
            continue
        by_unit.setdefault(t.unit_id, []).append(t)
    for unit, ts in by_unit.items():
        ts = sorted(ts, key=lambda x: x.start_min)
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                if b.start_min >= a.end_min - 1e-6:
                    break
                if not overlap_allowed(a, b):
                    conflict += 1

    # 依赖检测
    by_id = {t.task_id: t for t in plan.tasks}
    dv = 0
    for t in plan.tasks:
        for dep in t.depends_on:
            if dep in by_id and by_id[dep].end_min > t.start_min + 1e-6:
                dv += 1

    # ---- 设备利用率 ----
    # 旧口径(2026-09-28 修正)有三个结构性问题:
    #   分子 = 各任务 duration 直接相加, 分母 = 各单元自身 [首任务起, 末任务止]
    #   ① 蒸箱同温共享腔体时并行任务被重复计入分子 → 实测能算出 144.2% (>100%, 无意义)
    #   ② 只有一个任务的单元恒等于 100% (如 冰箱-1) → "少用设备"反而分高, 激励方向反了
    #   ③ 分母是单元自己的活跃窗口, 排程首尾的空闲完全不计, 与 makespan 无关
    # 新口径:
    #   分子 = 各腔体占用区间的**并集**时长 (同腔并行只算一次; 预热并入所属腔体)
    #   分母 = 腔体总数 × 全局 makespan
    #   → 利用率 = 已用产能 / 可用产能, 天然落在 [0,100]; 腔体闲置即被拉低。
    live = [t for t in plan.tasks if t.start_min >= 0]
    span = max((t.end_min for t in live), default=0.0)
    # plan.makespan_min 是在合成烟机/预热任务之前算的 (烟机会延到 +3min), 取真实结束时刻
    makespan = max(float(plan.makespan_min), span)

    util, detail = _utilization(by_unit, makespan)

    return ScheduleKPI(
        total_tasks=len(plan.tasks),
        makespan_min=round(makespan, 1),
        finish_gap_min=plan.finish_gap_min,
        device_utilization=util,
        manual_tasks=sum(1 for t in plan.tasks if t.needs_human),
        unattended_tasks=sum(1 for t in plan.tasks if t.unattended),
        conflict_count=conflict,
        depend_violation=dv,
        hand_streak_min=hand_streak(plan),
        util_detail=detail,
    )


def _union_minutes(tasks) -> float:
    """同一腔体上重叠的任务区间合并成不相交集合后的总占用时长。
    同温共享腔体的并行任务只算一次 —— 这是修掉 >100% 的关键。"""
    ivs = sorted((t.start_min, t.end_min) for t in tasks)
    total = 0.0
    cs = ce = None
    for s, e in ivs:
        if cs is None:
            cs, ce = s, e
        elif s <= ce + 1e-6:
            ce = max(ce, e)
        else:
            total += ce - cs
            cs, ce = s, e
    if cs is not None:
        total += ce - cs
    return total


def _chamber_of(unit_id: str) -> str:
    """把 unit_id 归一到一个物理设备类型。
    "预热-蒸箱-1" 占的就是 蒸箱-1 那个腔体, 必须并到同一台设备上算占用率。"""
    u = unit_id[len("预热-"):] if unit_id.startswith("预热-") else unit_id
    return u.split("-")[0]


def _utilization(by_unit: dict, makespan: float) -> tuple[float, dict]:
    """设备利用率 + 各单元明细。返回 (主指标%, {unit_id: 占用率%})。"""
    chambers = sum(RESOURCE_UNITS[d] for d in UTIL_SCOPE)
    if chambers <= 0 or makespan <= 0:
        return 0.0, {}
    busy = 0.0
    detail: dict = {}
    for unit, ts in by_unit.items():
        if not ts or unit.startswith("双手"):
            continue
        base = _chamber_of(unit)
        if base not in UTIL_DETAIL_SCOPE:
            continue
        used = _union_minutes(ts)
        detail[unit] = round(used / makespan * 100, 1)
        if base in UTIL_SCOPE:
            busy += used
    return round(busy / (chambers * makespan) * 100, 1), detail


def dish_cook_finish(plan: SchedulePlan) -> dict:
    """{菜名: 出锅时刻} —— 该菜**最后一次进加热设备**的结束时刻。

    与 `plan.dish_finish` 的区别(以及为什么必须分开):
      · `plan.dish_finish` = 该菜**所有步骤**结束 = "完成时刻" ⇒ 评分表口径的出锅差 Γ 用它;
      · 本函数 = "出锅时刻"(端出灶/蒸箱/烤箱那一刻) ⇒ **保热/冷等待惩罚**用它。

    旧实现两者混用同一个值, 于是"把一条 1 分钟的收尾步停在时间线最末端"会让
    完成时刻 == T, 冷等待恒为 0 —— 惩罚被掩掉, 求解器被奖励去"早做熟、干等到最后"。
    实测 60 组里有 49 组的报告 Γ 与真实烹饪跨度差 >5 min(中位 23, 最坏 175)。

    没有加热步的菜(凉拌/沙拉/纯冷藏甜品)退化为它的完成时刻, 行为与旧口径一致。

    当前消费者: `model.solve_exact` 的 cold/over(自己建 F_cook 变量, 口径同此)、
    `objective.score_plan` 的保热项(2026-10-04 起改用本函数, 并给了它以 Γ 计价的
    总预算 `FT_HOT_BUDGET`, 否则它会夺走 Γ 的第一优先权 —— 见 objective.hot_budget)、
    保热口径审计脚本、以及 tests/test_model.py。
    """
    out: dict = {}
    for t in plan.tasks:
        if t.start_min < 0 or not t.dish or t.device not in UTIL_SCOPE:
            continue
        out[t.dish] = max(out.get(t.dish, 0.0), t.end_min)
    for dish, fin in (plan.dish_finish or {}).items():
        out.setdefault(dish, fin)
    return out


def hand_streak(plan: SchedulePlan, gap_tol: float = HAND_GAP_TOL) -> float:
    """最长"连续手动操作"时长: 人工任务按时间合并 (间隔 <= gap_tol 分钟视为连续),
    返回其中最长的连续块时长。对应赛题"用户连续手动操作时长"评分项, 越短越好。

    gap_tol 的默认值就是 HAND_GAP_TOL —— 与 model.solve_exact(hand_cap) 的约束口径
    同源, 保证"模型声称 B<=cap" 和 "评分表量出的 B" 是同一个量。"""
    hands = sorted((t for t in plan.tasks
                    if t.start_min >= 0 and t.needs_human and t.unit_id not in ("等待",)),
                   key=lambda x: x.start_min)
    if not hands:
        return 0.0
    best = cur = hands[0].duration_min
    cur_end = hands[0].end_min
    for t in hands[1:]:
        if t.start_min - cur_end <= gap_tol:
            cur += t.duration_min
        else:
            best = max(best, cur)
            cur = t.duration_min
        cur_end = max(cur_end, t.end_min)
    return round(max(best, cur), 1)


def perturbation(old: SchedulePlan, new: SchedulePlan) -> int:
    """重排后被挪动的任务数 (最小扰动指标)"""
    old_map = {t.task_id: t for t in old.tasks}
    moved = 0
    for t in new.tasks:
        o = old_map.get(t.task_id)
        if o and abs(o.start_min - t.start_min) > 0.5:
            moved += 1
    return moved


def device_switches(plan: SchedulePlan) -> list:
    """同腔体**异温背靠背**的位置 —— 需要降温缓冲却没留出来的那些。

    建议文本《详细注意事项》§一.3 / §三.2 / §八.1 点名的坑: "高温任务完成后,
    不能立刻低温任务", 且明确"降温时间不可以忽略"。

    判据与 `engine.cooldown_minutes` **同源**(唯一权威定义在那里):
      · 温差 <= STEAM_TEMP_TOL 不算切换 —— 同温合炉不受影响;
      · 升温方向不算 —— 它的成本已由 L1 的**预热步**显式建模, 重复计会双重罚款;
      · 中间空档 >= 所需降温时间就不算 —— 只有"紧贴/留得不够"才是违规。

    返回 [(腔体, 前任务, 后任务, 降温需求分钟), ...]。空列表 = 全部合规。
    """
    from app.scheduler.engine import cooldown_minutes
    by_ch: dict = {}
    for t in plan.tasks:
        if t.start_min < 0 or t.unit_id in ("等待", "提前准备", "双手-1"):
            continue
        by_ch.setdefault(_chamber_of(t.unit_id), []).append(t)
    out = []
    for ch, ts in by_ch.items():
        ts = sorted(ts, key=lambda x: (x.start_min, x.end_min))
        for a, b in zip(ts, ts[1:]):
            need = cooldown_minutes(getattr(a, "temp_c", None), getattr(b, "temp_c", None))
            if need <= 0:
                continue
            gap = b.start_min - a.end_min
            if gap < need - 1e-6:
                out.append((ch, a.task_id, b.task_id, need))
    return out
