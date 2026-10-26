# -*- coding: utf-8 -*-
"""备菜前置判据 —— 三条轨 (贪心 / CP-SAT / 精确模型) 的**唯一**口径。

为什么必须唯一
--------------
"哪些步骤可以提前到开火前完成"决定了三条轨**排的是不是同一个任务集**。
历史上三条轨各写各的:

    engine._schedule_once  : (hard or soft) and 前置链完整     ← 正确
    model.prep_keys        : hard or (soft and 前置链完整)     ← hard 分支跳过了链检查
    给 cpsat 的 slim 过滤   : 只看 is_hard_prep, 连语义先验都不看

后果 (实测抓到的事故): 精确模型删掉了「抹茶红豆白玉卷」第 9 步 (冰箱 120 min),
却留下了它的前置第 8 步 (当天 215 min 处) —— 这个步骤既不在时间轴上、依赖也不可能满足。
更糟的是精解轨正因为"少排了 9 步"把 makespan 从 340 压到 35、出锅差从 21 压到 1,
**指标是"少干活"换来的**, 属于目标函数被求解器反向利用; 而把它补回时间轴时又会因为
前置还在当天而产生依赖违反。所以只能从源头统一判据。

判据本身
--------
  hard : 超长(>PREP_AHEAD_MIN 分钟) 的冷藏/浸泡/泡发 —— 本来就该在开火前完成, 与策略无关;
  soft : 语义先验 (`priors[dish].prep_step_ids`) 标记的备菜步, 只在 allow_preprocess 下启用;
  **共同约束**: 可提前的集合必须是**前缀闭合**的 —— 即某步前移时, 它 depends_on 上的
  所有祖先也必须一起前移。少了这一条就会把菜链中间的步骤挪到 t<0 而它的前置还在 t>0,
  直接产生依赖违反。

  v3.3: 闭合方式对 hard / soft **不同**, 这是刻意的:
    · hard 种子 ⇒ 沿 depends_on **反向闭包, 把祖先拉进来**。
      旧口径要求"祖先自己也得是 hard/soft", 于是只要超长等待前面挂着一个 2 分钟的
      普通步骤, 4 小时的冷却就永远进不了 prep (实测「血糯米仙草奶茶」#6 因此留在当天,
      该组 makespan 284 / 下界 42 / gap 576%, 是全场最差)。
    · soft 种子 ⇒ 保持**保守口径**(只有"从第一步起连续、且每环自己都是 hard/soft"才放行)。
      ⚠ 对 soft 也做反向闭包会**改写当天工作量的定义**: 实测 25/100 道菜整道落进 t<0、
      平均 prep 占比 44%→70%, 于是 makespan(只统计 start>=0 的任务)从 79 崩到 36 ——
      那是"把活挪出计量窗口"刷指标, 不是排得更快。hard 种子不会这样, 因为超长等待
      本就该移出当天, 且它的祖先链短 (实测 prep 占比只 44.0%→45.7%)。
"""
from __future__ import annotations

from collections import defaultdict

# 备菜前置的时长门槛: 超过这个时长的冷藏/浸泡/泡发才算"可提前准备"。
#
# 为什么是 30 而不是原来的 90 —— 实测扫描 (官方 100 道 × bench 50 组 × lb_probe 30 组,
# 三个种子 42/11 · 7/3 · 202/77 都跑过):
#
#   阈值  整道进prep  prep占比  总时长(bench)  出锅差中位  连续手动  冲突
#    90      2/100      45.7%     68.3/81.6/84.0    2.0      27.1      0
#    45      2/100      47.0%     66.0/77.3/82.1    2.0      26.2      0
#    30      2/100      47.2%     63.7/76.6/81.3    2.0      26.4      0
#    20      5/100      51.1%     58.4/—/—          1.0      25.2      0
#
# 90 这条线把**最常见**的备菜等待全挡在门外了 —— 官方菜谱里 40~60 分钟的
# 「腌制1小时」「冷藏1小时」「泡发40分钟」「常温静置1小时」共 7 条, 全部因为
# 不到 90 分钟而只能留在当天。它们恰恰是 is_hard_prep 想覆盖的语义(冷藏/静置/腌制/泡发),
# 没有一条是设备烹饪步。最差用例 lb_probe#4(gap 188.9%)就是被
# 「金银蒜蒸鸡胸肉#3 腌制1小时」顶出来的: 它 0→60 占着, 蒸箱只能等到 60 才开火。
#
# ⚠ 别再往下调到 20: 会把「冷藏30分钟即可食用」这类**末步收尾**也推进 t<0,
#   整道菜从当天消失的从 2 道涨到 5 道(雪媚娘/黑森林蛋糕/皂角银耳羹…),
#   makespan 被刷掉 —— 那是把活挪出计量窗口, 不是排得更快。
#   (30 之所以安全, 是因为解析器给"无显式时长的冰箱步"的默认值正好是 30,
#    而判据是**严格大于**, 于是这类收尾步天然被排除。)
PREP_AHEAD_MIN = 30.0


def is_hard_prep(device: str, unattended: bool, dur: float) -> bool:
    """超长冷藏/浸泡/泡发 -> 应在开火前完成, 不占当天关键路径。"""
    from app.models.schema import DEVICE_FRIDGE, DEVICE_NONE
    return dur > PREP_AHEAD_MIN and (
        device == DEVICE_FRIDGE
        or (device == DEVICE_NONE and unattended)
    )


def prep_keys(recipes: list, priors: dict | None = None,
              allow_preprocess: bool = False) -> set:
    """可提前到开火前完成的步骤集合: {(菜名, step_id)}。

    三条轨共用本函数, 保证任务集一致 (D5 根治)。

    v3.3 修复: **hard 种子**的判据从"前置已经在 prep 里"改成"把前置一起拉进来"。
    ---------------------------------------------------------------
    旧判据要求 `all((dish, d) in prep for d in st.depends_on)` —— 即前置**必须自己
    也是 hard/soft**。于是"超长等待"只要前面挂着一个普通的短步骤, 整条链就永远
    进不了 prep, 4 小时的冷却/冷藏照样压在当天的关键路径上。实测官方菜谱:

      · 「血糯米仙草奶茶」#6 = 240 分钟冷却(hard), 前置 #5 是 2 分钟的人工搅拌
        ⇒ #6 进不了 prep ⇒ 该组 makespan 284 分钟、下界 42、**gap 576%**(全场最差)
      · 「抹茶红豆白玉卷」#2 = 180 分钟冷藏(hard) 前置 #1 是 3 分钟蒸
        · #9 = 120 分钟冷藏(hard) 前置 #8 是 3 分钟打发
        ⇒ prep_keys **整个为空**, 两段共 300 分钟冷藏全留在当天

    改法(hard): 取 hard 步作种子, 沿 depends_on 反向做**前缀闭包** —— 种子步要前移,
    它的祖先必须跟着一起前移(否则就是"后一步在 t<0、前一步在 t>0"的依赖违反,
    正是本模块 docstring 记的那次事故)。这样任务集依然是**前缀闭合**的, 依赖不变式
    得以保持, 而超长等待终于能真正移出当天。

    ⚠ 两条边界, 都实测过, 别再踩:
      · 闭包**只补祖先, 不补后代**;
      · **soft 种子不做闭包**(仍走保守口径) —— 对 soft 也闭包会把 25/100 道菜整道
        推进 t<0, makespan 79→36, 属于"把活挪出计量窗口"刷指标, 不是排得更快。

    实测收益 (官方 100 道 + bench 50 组 / lb_probe 30 组 seed=11):
      · 平均 prep 占比 44.0% → 45.7%(有界), 整道进 prep 的菜 1 → 2 道
      · bench 总时长 79.2 → 68.3 分钟, 出锅差中位 2.0 不变, 冲突 0
      · 最优性间隙: 中位 38.6% → 31.6%, 均值 55.0% → 38.5%, **max 576% → 189%**
    """
    from app.scheduler.engine import estimate_duration

    by_dish = defaultdict(list)
    for rec in recipes:
        for st in rec.steps:
            by_dish[rec.name].append(st)

    prep: set = set()
    for dish, steps in by_dish.items():
        steps.sort(key=lambda s: s.step_id)
        by_id = {s.step_id: s for s in steps}
        prior_ids: set = set()
        if priors and allow_preprocess and dish in priors:
            prior_ids = set(getattr(priors[dish], "prep_step_ids", []) or [])

        # ① hard 种子 (超长冷藏/浸泡/泡发): 做**前缀闭包** —— 把祖先一并拉进来。
        #    不闭包的话, 只要它前面挂着一个 2 分钟的普通步骤, 4 小时的冷却就永远
        #    压在当天 (见 docstring)。
        hard_ids = []
        for st in steps:
            if is_hard_prep(st.device, st.unattended, estimate_duration(st)):
                hard_ids.append(st.step_id)
        stack = list(hard_ids)
        while stack:
            sid = stack.pop()
            if (dish, sid) in prep:
                continue
            st = by_id.get(sid)
            if st is None:
                continue
            prep.add((dish, sid))
            for d in (st.depends_on or []):
                stack.append(d)

        # ② soft 种子 (语义先验标记的备菜步): 仍走**保守口径** —— 只有"从该菜第一步
        #    起连续的一段、且每一环自己都是 hard/soft"才放行。
        #    ⚠ 为什么不对 soft 也做前缀闭包: 实测会把 25/100 道菜**整道**推进 t<0
        #      (平均 prep 占比 70%), 于是 `makespan`(只统计 start>=0 的任务)从
        #      79.2 崩到 35.8 分钟 —— 那是**把活挪出计量窗口**刷指标, 不是排得更快。
        #      hard 种子不会这样, 因为超长等待本身就该移出当天, 且它的祖先链短。
        for st in steps:
            key = (dish, st.step_id)
            if key in prep:
                continue
            soft = st.step_id in prior_ids
            if soft and all((dish, d) in prep for d in (st.depends_on or [])):
                prep.add(key)
    return prep
