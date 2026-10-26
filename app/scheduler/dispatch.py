# -*- coding: utf-8 -*-
"""求解器分流 (设计文档 §3.3.3 落地)。

文档原文
--------
> N ≤ 6 用 CP-SAT 精确求解, N > 6 用启发式, 保证响应时间。

为什么不是照字面实现
--------------------
照字面做会**比现状更差**: 本系统不是"二选一", 而是**三轨集成** ——
    贪心(5 策略, 毫秒级兜底) → CP-SAT 加权和 → 精确模型 model.py
三轨各自产出可行解, 最后按 (出锅差 Γ, 总时长 T) 字典序取最优。
实测 80 组 seed=11: 开三轨 82.3 / 2.11 / 29.6 vs 关三轨 109.9 / 3.70 / 50.8。
所以 "N>6 只跑启发式" 等于主动把已经算出来的更优解扔掉。

文档这条规则的**真实意图是控制响应时间**, 而实测证明大 N 确实会超线:

    N    中位ms   最大ms   超5s
    6     323     584      0
    8     372     993      0
   10     616    1176      0
   12     876    4492      0
   15    2907    4190      0
   20    3506    5044      2/9     ← 已经破线

根因: 最终精解段的预算是**写死的** (CP-SAT ≤1.2 s + 精确模型 ≤1.5 s), 不随 N 收缩,
而求解器的规模随 N 增长 —— 于是"固定预算 × 更大规模"必然撞上 5 s 线。

因此本模块的落地方式是: **保留三轨, 但让预算随菜数收缩**, 并给出硬下限 ——
预算低于下限时干脆不启动该轨 (省掉建模开销), 由贪心轨兜底。这样:
  · N ≤ 6  : 与文档一致 (CP-SAT + 精确模型全预算);
  · N > 6  : 按 6/N 收缩, 规模越大给得越少, 总耗时留在 5 s 线内;
  · 任何 N  : 贪心轨永远先跑, 所以**任何情况下都有一份合法排程**。
"""
from __future__ import annotations

import os

# 单轨预算硬上限 (与文档 §3.3.3 的"响应时间"口径配套)
CP_SAT_CAP = 1.2
EXACT_CAP = 1.5

# 低于这个预算就不启动该轨 —— 建模开销比它能找到的改进还贵
MIN_USEFUL = 0.25

# 文档的分流阈值
SPLIT_N = 6

_OFF = ("off", "0", "false", "no")


def solver_budgets(n_dishes: int, pool: float) -> dict:
    """按菜数给两条精解轨分配预算。

    n_dishes : 参与排程的菜数 N
    pool     : 当前还剩多少秒可以给求解器 (由调用方从总预算里算出来)

    返回 {"use_cpsat", "use_exact", "cpsat", "exact"}。
    `use_*` 为 False 表示该轨预算不足 (或被全局开关关掉), 直接跳过 ——
    而不是给个 0.01 s 假装在跑。
    """
    pool = max(0.0, float(pool))
    if n_dishes <= SPLIT_N:
        cpsat, exact = CP_SAT_CAP, EXACT_CAP
    else:
        k = SPLIT_N / float(n_dishes)          # 6/7, 6/8, ... 随 N 单调收缩
        cpsat, exact = CP_SAT_CAP * k, EXACT_CAP * k

    cpsat = min(cpsat, pool * 0.45)
    exact = min(exact, pool * 0.55)

    # 全局熔断开关必须在这里也认。
    # 为什么: engine.schedule 只在 `use_exact is None` 时才去读 FT_EXACT, 而调用方
    # (orchestrator) 现在**总是显式传** use_exact —— 于是 FT_EXACT=off 会被绕过,
    # 消融实验/verify_model 的"关第三轨"对照组会静默失效 (实测 60 组全打平)。
    if os.environ.get("FT_EXACT", "on").lower() in _OFF:
        exact = 0.0

    return {
        "use_cpsat": cpsat >= MIN_USEFUL,
        "use_exact": exact >= MIN_USEFUL,
        "cpsat": round(max(0.0, cpsat), 3),
        "exact": round(max(0.0, exact), 3),
    }


def _min_pool_single(n_dishes: int, key: str) -> float:
    """反解: 要让 `solver_budgets(n, pool)[key]` 为 True, pool 至少要多少。

    用**二分调用 solver_budgets 本身**求，而不是照抄一遍 0.45/0.55 的公式 ——
    公式一旦在两处各写一份，改一处漏一处就会静默漂移（本项目已有前科：
    备菜判据三轨各写各的，精解轨靠"少排几步"刷出 +54% 虚高）。

    返回 0.0 表示**该轨在当前 N 下无论给多少预算都开不起来**
    （cap 本身已低于 MIN_USEFUL，例如 N=40 时 1.2×6/40 < 0.25）
    —— 此时"不需要为它预留"。
    """
    def ok(pool: float) -> bool:
        return bool(solver_budgets(n_dishes, pool)[key])

    if ok(0.0):
        return 0.0
    hi = 1.0
    while not ok(hi):
        hi *= 2.0
        if hi > 1e6:
            return 0.0
    lo = 0.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


def min_pool_for_tracks(n_dishes: int, *, want_cpsat: bool = True,
                        want_exact: bool = True) -> float:
    """要让指定的精解轨开起来, 求解器池(pool)至少需要多少秒。

    调用方 (orchestrator) 用它把**精解轨的预算从 LLM 手里抢回来**：
    在线闭环里 LLM 的两次调用实测能吃光整个 time_budget, 于是
    `solver_budgets(n, 0)` 把两条轨全关, 直接交付贪心解 —— 实测 4 菜时
    出锅差 Γ 1→8 分、总时长 T 37→52 分（离线基准 vs 慢模型首请求）。
    精解轨是最大的质量杠杆（第三轨净收益 T −21.6% / Γ −32.0%）, 必须先保它。

    `FT_EXACT=off` 时 exact 轨恒不可用, 本函数对它返回 0.0（不预留）——
    逐轨分别求再取最大, 所以不会因为"精确轨被关"而连 CP-SAT 的预留一起丢掉。
    """
    need = 0.0
    if want_cpsat:
        need = max(need, _min_pool_single(n_dishes, "use_cpsat"))
    if want_exact:
        need = max(need, _min_pool_single(n_dishes, "use_exact"))
    return round(need, 4)
