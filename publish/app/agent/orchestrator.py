"""
Agent 决策闭环 (Agentic Scheduling Loop)

这是本项目"模型参与调度决策"的**在线体现**。闭环形态:

    ┌─ 1. 目标塑形 (LLM) ── 用户口语 -> 多目标权重向量 policy
    │
    │  2. 加载语义先验 (缓存, 0 LLM) ── 趁热/耐放/可备菜/可合炉
    │
    │  3. 策略提议 (LLM) ── 在策略空间里挑出值得试的几条 + 说明理由
    │        ↓
    │  4. 求解器执行 (工具调用) ── 贪心快评 (毫秒) 选出最优策略
    │        ↓
    │  5. 校验器反馈 (工具调用) ── 冲突/依赖/出锅差/连续手动
    │        ↓
    │  6. 收敛判定 ── 达标则停; 不达标且预算允许 -> LLM 反思调整 -> 回 4
    │        ↓
    └─ 7. CP-SAT 在胜出策略上精解 -> 最终排程

关键工程约束: 中转站单次 LLM 调用约 6s, 赛题要求初始排程 <5s。
因此本闭环**默认不调用 LLM** (use_llm=False), 退化为"多策略枚举择优" —— 单靠
这一步就已经优于原来的单策略贪心; 需要展示完整 Agent 能力时才打开 use_llm。
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field, asdict

from app.models.schema import Recipe, SchedulePlan
from app.scheduler import engine
from app.scheduler.dispatch import solver_budgets, min_pool_for_tracks
from app.scheduler.engine import STRATEGIES, DEFAULT_STRATEGY
from app.scheduler.metrics import evaluate, hand_streak
from app.scheduler.checker import check_plan
from app.agent import llm
from app.agent.objective import SchedulingPolicy, shape_policy, score_plan
from app.agent.priors import get_priors, prior_brief


@dataclass
class AgentStep:
    rnd: int
    strategy: str
    makespan: float
    gap: float
    conflict: int
    hand_streak: float
    score: float
    source: str          # rule | llm
    note: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class AgentResult:
    plan: SchedulePlan
    policy: SchedulingPolicy
    priors: dict
    trace: list = field(default_factory=list)
    explanation: str = ""
    llm_calls: int = 0
    llm_ms: float = 0.0
    elapsed_ms: float = 0.0

    def to_dict(self):
        return {
            "plan": self.plan.to_dict(),
            "policy": self.policy.to_dict(),
            "priors": {k: v.to_dict() for k, v in self.priors.items()},
            "trace": [s.to_dict() for s in self.trace],
            "explanation": self.explanation,
            "llm": {"calls": self.llm_calls, "ms": round(self.llm_ms, 1)},
            "elapsed_ms": self.elapsed_ms,
        }


# ---------------- LLM: 策略提议 ----------------
_PROPOSE_SYS = ("你是烹饪排程的策略决策器。你要在给定策略空间里挑出最值得尝试的几条, "
                "只输出 JSON, 不要解释。")

_PROPOSE_USER = """要做 {n} 道菜: {dishes}
用户偏好: {pref}
已塑形的目标权重: 总时长={wm} 同出锅={wg} 人工均衡={wh} 保热={whot}

菜的调度语义先验:
{priors}

可选策略:
- critical_path: 关键路径优先 (通用稳妥)
- serve_hot_last: 保热优先, 把必须趁热的菜压到最后
- prep_first: 备菜前置, 把切配/腌制移出关键路径压缩总时长
- compress: 长任务先行, 极限压缩 makespan
- balanced: 人工负载均衡, 打散连续手动操作

输出 JSON: {{"strategies": ["最推荐的","次推荐","第三"], "reason": "一句话理由(30字内)"}}
最多 3 条, 必须来自上面 5 个名字。"""


def propose_strategies(dishes: list[str], user_text: str, policy: SchedulingPolicy,
                       priors: dict, timeout: float = 15.0) -> tuple[list[str], str, bool]:
    """返回 (策略列表, 理由, 是否来自LLM)。LLM 失败则退化为全策略枚举。"""
    all_st = list(STRATEGIES.keys())
    user = _PROPOSE_USER.format(
        n=len(dishes), dishes="、".join(dishes), pref=user_text or "(无特别偏好)",
        wm=policy.w_makespan, wg=policy.w_gap, wh=policy.w_hand, whot=policy.w_hot,
        priors=prior_brief(priors))
    out = llm.chat_json(_PROPOSE_SYS, user, cache_key=None, temperature=0.1, timeout=timeout)
    if isinstance(out, dict):
        picked = [s for s in (out.get("strategies") or []) if s in STRATEGIES]
        if picked:
            return picked[:3], str(out.get("reason", ""))[:60], True
    return all_st, "规则兜底: 枚举全部策略择优", False


# ---------------- LLM: 反思 ----------------
_REFLECT_SYS = "你是烹饪排程的仲裁者。排程结果没达到目标, 你要决定怎么调整。只输出 JSON。"

_REFLECT_USER = """目标: 出锅差 <= {target} 分钟, 设备零冲突。
当前最优结果: 出锅差 {gap} 分钟, 总时长 {mk} 分钟, 冲突 {cf}, 最长连续手动 {hand} 分钟。
策略: {strategy} (第 {rnd} 轮)

各菜出锅时刻: {finish}
语义先验: 趁热分越高越不能等; 可放分是出锅后还能等的分钟数。

请决策(只输出 JSON):
{{"accept": true/false,   // 是否接受当前结果收工
  "next_strategy": "critical_path|serve_hot_last|prep_first|compress|balanced",
  "w_gap_mult": 1.0,      // 同出锅权重再乘多少(1~4), 想更齐就调大
  "sacrifice": "可以适当延后的菜名, 没有就填空",
  "reason": "一句话理由(30字内)"}}"""


def reflect(policy: SchedulingPolicy, best, rnd: int, timeout: float = 15.0):
    user = _REFLECT_USER.format(
        target=policy.target_gap, gap=best["gap"], mk=best["makespan"],
        cf=best["conflict"], hand=best["hand_streak"], strategy=best["strategy"],
        rnd=rnd, finish=best.get("finish", {}))
    out = llm.chat_json(_REFLECT_SYS, user, cache_key=None, temperature=0.1, timeout=timeout)
    if not isinstance(out, dict):
        return None
    return out


# ---------------- 主闭环 ----------------
def _eval_strategy(recipes, policy, priors, strategy, hard_frozen=None):
    """贪心快评一条策略 (毫秒级)。"""
    plan = engine.schedule(recipes, policy=policy, priors=priors,
                           strategy=strategy, use_cpsat=False,
                           hard_frozen=hard_frozen)
    chk = check_plan(plan)
    kpi = evaluate(plan)
    sc = score_plan(plan, kpi, policy, priors)
    return {
        "plan": plan, "kpi": kpi, "chk": chk, "score": sc,
        "strategy": strategy,
        "makespan": kpi.makespan_min, "gap": kpi.finish_gap_min,
        "conflict": kpi.conflict_count + kpi.depend_violation,
        "hand_streak": kpi.hand_streak_min,
        "finish": dict(plan.dish_finish or {}),
    }


_AGENT_LLM_DEFAULT = "on"      # 在线默认让模型参与决策 (有超时+熔断兜底)
_BUDGET_DEFAULT = 3.6          # 在线总预算(秒): 1 次模型往返(≤3s) + CP-SAT 精解仍在 5s 内
_REFLECT_MIN_LEFT = 2.5        # 剩余预算不足以再跑一次模型往返时, 不再反思

# 剩余时间中给"求解器池"的比例 —— 与下面 `solver_budgets(n, left * 0.75)` 必须一致。
# 留 25% 给建模/还原/后处理, 见 app/scheduler/dispatch.py 文档 §3.3.3。
_SOLVER_POOL_RATIO = 0.75
# 反解出来的预留时间再加一点余量: `left = time_budget - elapsed` 与
# `pre = time_budget - reserve` 之间的浮点差可能让 left 比 reserve 低 1ulp,
# 而 solver_budgets 的判据是 `>=` —— 差一点就整轨关掉。
_RESERVE_EPS = 0.02
# 一次模型往返的最小可用时间 (与 shape_policy 的 max(0.5, ...) 下限对齐)。
# 低于它就不值得为 LLM 开预算 —— 宁可整段留给精解轨。
_LLM_MIN_ROUNDTRIP = 0.5
# 给"策略枚举"的保底时间。枚举 6 条策略是**毫秒级**的 (每条一次贪心快评),
# 但它是贪心质量的保证 —— 不能让 LLM 把预算吃光后连一条策略都试不完。
# 实测 6 条 ≈ 0.2s, 这里给 3 倍余量。
_ENUM_RESERVE = 0.6


def _solver_reserve(n_dishes: int, use_cpsat: bool) -> float:
    """精解段开始前**必须**留给它的秒数 (从 time_budget 里扣)。

    ⚠ 为什么必须有它 (2026-10-04 修复的真 bug):
    在线闭环里 LLM 的两次调用 (目标塑形 + 策略提议) 实测能吃光整个 3.6s 预算,
    于是下面算出的 `left = max(0, time_budget - elapsed) ≈ 0`
    ⇒ `solver_budgets(4, 0)` 两条精解轨全 `use_*=False`
    ⇒ **整个精解段被跳过, 交付贪心解**。
    而在线闸门 (`FT_LLM_ONLINE_GATE`) 是在本轮**结束后**才落下的,
    所以**每次进程启动后的第一次请求都中招**。

    实测 (4 菜, seed=11): 离线基准 Γ=1/T=37 → 慢模型首请求 Γ=8/T=52。
    精解轨是最大的质量杠杆 (第三轨净收益 T −21.6% / Γ −32.0%),
    所以宁可少给 LLM 一点, 也绝不能让整个精解段消失。
    """
    pool = min_pool_for_tracks(n_dishes, want_cpsat=bool(use_cpsat),
                               want_exact=True)
    if pool <= 0.0:
        return 0.0          # 该 N 下两条轨本来就不会开 ⇒ 无需预留
    return pool / _SOLVER_POOL_RATIO + _RESERVE_EPS


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).lower() not in ("off", "0", "false", "no")


def _llm_online(use_llm: bool | None) -> bool:
    """是否真的可以在线调用模型: 开关 + 全局开关 + 熔断 + **闭环闸门** 都通过才行。

    最后一项（`llm.online_usable()`）是 2026-10-04 加的分档：熔断器管"服务坏了"，
    闸门管"服务好着、只是比闭环那几秒的预算慢"。本地 7B 模型一次真实提议实测 7.6s，
    装不进 3.6s 的闭环预算 —— 没有闸门时每次排程先白等 3.6s 才回落规则，
    还把熔断器喂到打开（两次排程之后连 AI 对话一起被关 120s）。
    闸门只关闭环，对话 / 通知 / 离线预热照常用模型。
    """
    if use_llm is None:
        use_llm = _env_flag("FT_AGENT_LLM", _AGENT_LLM_DEFAULT)
    return (bool(use_llm) and llm.enabled() and not llm.breaker_open()
            and llm.online_usable())


def run(recipes: list[Recipe], user_text: str = "", use_llm: bool | None = None,
        time_budget: float | None = None, max_rounds: int = 2,
        strategies: list[str] | None = None,
        warm_priors: bool = False,
        hard_frozen: dict | None = None,
        use_cpsat: bool = True) -> AgentResult:
    """
    recipes: 已解析的菜谱
    user_text: 用户自然语言偏好 (可为空)
    use_llm:  None=按环境变量 FT_AGENT_LLM 决定 (默认 on); True/False 强制
    time_budget: 秒, 默认 FT_AGENT_BUDGET 或 2.2s; 超出预算立刻收敛返回当前最优
    warm_priors: True 时才允许在线抽取语义先验 (离线预热脚本用);
                 在线路径一律只读磁盘缓存, 保证 0 网络延迟。
    hard_frozen: 动态重排的已完成步骤 {(菜名, 步骤号): ScheduledTask}。
                 传了它, 闭环里的每一次候选求解都必须尊重冻结, 且跳过 CP-SAT
                 (CP-SAT 不知道"已完成步骤不能动")。
    use_cpsat:   是否在胜出策略上再做 CP-SAT 精解。设备故障时必须关掉 ——
                 CP-SAT 的模型是固定的, 不读 RESOURCE_UNITS, 会照旧把坏掉的设备排进去。

    永不失败: 任何异常都会退化成 engine.schedule() 的单策略结果。
    """
    t0 = time.time()
    if time_budget is None:
        time_budget = float(os.environ.get("FT_AGENT_BUDGET", _BUDGET_DEFAULT))
    llm.reset_stats()
    n0 = llm.STATS["calls"]
    online = _llm_online(use_llm)

    # ---- 精解轨保底 (2026-10-04 修复) ----
    # 先给精解段扣出一份"能开轨"的预算, 再决定 LLM 能花多少。
    # 顺序不能反: 反了就是"LLM 先花, 花完看剩多少给求解器", 而 LLM 恰好能花光。
    reserve = _solver_reserve(len(recipes), use_cpsat)
    llm_budget = max(0.0, time_budget - reserve)
    if online and llm_budget < _LLM_MIN_ROUNDTRIP + _ENUM_RESERVE:
        # 连"一次模型往返 + 一轮策略枚举"都装不下 ⇒ 别开 LLM,
        # 把预算整段留给策略枚举与精解轨
        online = False

    dishes = [r.name for r in recipes]
    try:
        res = _run_inner(recipes, user_text, online, time_budget, llm_budget,
                         max_rounds, strategies, warm_priors, t0, n0,
                         hard_frozen, use_cpsat)
    except Exception:
        # 兜底: 闭环任何环节炸了, 也要交付一份合法排程
        plan = engine.schedule(recipes, hard_frozen=hard_frozen)
        res = AgentResult(
            plan=plan, policy=shape_policy(user_text, len(recipes), use_llm=False),
            priors={}, trace=[], explanation="闭环异常, 已回退单策略排程",
            llm_calls=llm.STATS["calls"] - n0, llm_ms=llm.STATS["ms"],
            elapsed_ms=round((time.time() - t0) * 1000, 1),
        )
    _note_online_latency(online, res)
    return res


def _note_online_latency(online: bool, res) -> None:
    """闭环里**有调用没能在预算内返回** ⇒ 这个模型装不进闭环的预算，落闸门。

    判据用 `STATS["deadline_miss"]`（"调用方给的预算比模块默认更紧、模型没跑完"
    的那几次，见 llm.chat）。`run()` 开头会 `llm.reset_stats()`，所以这个计数
    只反映**本轮闭环**。

    为什么"有一次落空"就够了（2026-10-04 实测）：
    本地 qwen2.5:7b 的短提示词（目标塑形）约 3.3s 能压进 3.6s 预算，而长提示词
    （策略提议）实测要 **7.6s**，必落空。只看"有没有成功过"会被那个 3.3s 的成功
    骗过去 —— 闸门不落，之后每次排程都要为提议白等满预算（实测闭环 3.8~4.1s，
    相对 184ms 的规则路径是 20 倍），而那次提议什么都没换回来。

    落闸门之后闭环走规则（毫秒级），模型仍然服务于 AI 对话 / 通知 / 离线预热；
    想无视闸门、每次都硬试：`FT_LLM_ONLINE_GATE=off`。
    """
    if not online or res.llm_calls <= 0:
        return
    misses = llm.STATS.get("deadline_miss", 0)
    if misses <= 0:
        return
    llm.mark_online_too_slow(
        f"闭环内 {misses}/{res.llm_calls} 次调用未能在预算内返回"
        f"（本轮闭环实际耗时 {res.elapsed_ms:.0f}ms）")


def _run_inner(recipes, user_text, online, time_budget, llm_budget, max_rounds,
               strategies, warm_priors, t0, n0,
               hard_frozen=None, use_cpsat=True) -> AgentResult:
    """time_budget = 总预算; llm_budget = 精解段之前所有工作(LLM + 策略枚举)的预算。

    两者之差就是**精解轨的保底**（见 `_solver_reserve`）。内部再把它切成
    `llm_quota`(给 LLM) + `_ENUM_RESERVE`(给策略枚举)：LLM 的每一次
    timeout / 剩余判断都基于 llm_quota，枚举的 break 基于 llm_budget，
    而最后精解段的 left 基于 time_budget —— 三者各守其位，谁也别越界。
    """
    llm_quota = max(0.0, llm_budget - _ENUM_RESERVE)
    policy = shape_policy(user_text, len(recipes), use_llm=online,
                          timeout=min(llm.TIMEOUT, max(0.5, llm_quota)))
    # 先验: 在线只读缓存 (模型离线算好落盘), 绝不为了先验去等网络
    priors = get_priors(recipes, use_llm=warm_priors, allow_cache=True)

    trace: list[AgentStep] = []
    best = None
    tried = set()

    # 候选策略: LLM 提议优先, 否则全枚举
    if strategies:
        cand, why, by_llm = strategies, "调用方指定", False
    elif online:
        left = llm_quota - (time.time() - t0)
        if left < _LLM_MIN_ROUNDTRIP:
            # 目标塑形已经把 LLM 的配额用掉了 ⇒ 不再为提议白等一次往返,
            # 直接枚举全部策略 (与离线路径一致), 把时间留给精解轨。
            cand, why, by_llm = (list(STRATEGIES.keys()),
                                 "LLM 配额不足: 枚举全部策略择优", False)
        else:
            cand, why, by_llm = propose_strategies(
                [r.name for r in recipes], user_text, policy, priors,
                timeout=min(llm.TIMEOUT, left))
    else:
        cand, why, by_llm = list(STRATEGIES.keys()), "规则兜底: 枚举全部策略择优", False

    def consider(stg: str, rnd: int, source: str, note: str = ""):
        nonlocal best
        if stg in tried:
            return
        tried.add(stg)
        r = _eval_strategy(recipes, policy, priors, stg, hard_frozen=hard_frozen)
        trace.append(AgentStep(rnd=rnd, strategy=stg, makespan=r["makespan"],
                               gap=r["gap"], conflict=r["conflict"],
                               hand_streak=r["hand_streak"], score=r["score"],
                               source=source, note=note))
        if best is None or r["score"] < best["score"]:
            best = r

    for stg in cand:
        consider(stg, 1, "llm" if by_llm else "rule", why if stg == cand[0] else "")
        # 上限用 llm_budget 而不是 time_budget: 策略枚举(毫秒级)也不该把
        # 精解轨的保底预算吃掉。正常情况(LLM 未超时/离线)枚举远跑不满它。
        if time.time() - t0 > llm_budget:
            break

    # 模型可能提议了空列表/全部重名, 保证至少有一条结果
    if best is None:
        consider(DEFAULT_STRATEGY, 1, "rule", "兜底策略")
    if best is None:
        raise RuntimeError("无任何可用策略")

    # ---- 反思循环: 未达标且预算允许 -> LLM 调整后重试 ----
    # 预算保护: 剩余时间不足以容纳一次模型往返时直接收工, 绝不硬等。
    converged = best and best["conflict"] == 0 and best["gap"] <= policy.target_gap
    rnd = 1
    while (not converged) and online and rnd < max_rounds:
        left = llm_budget - (time.time() - t0)
        # 只有在"还装得下一次完整模型往返"时才反思, 否则直接收工
        if left < _REFLECT_MIN_LEFT:
            break
        rnd += 1
        adj = reflect(policy, best, rnd, timeout=max(0.4, min(llm.TIMEOUT, left)))
        if not adj:
            break
        if adj.get("accept"):
            break
        mult = float(adj.get("w_gap_mult", 1.0) or 1.0)
        if 0.5 <= mult <= 5:
            policy.w_gap = round(policy.w_gap * mult, 1)
        nxt = adj.get("next_strategy")
        if nxt in STRATEGIES:
            consider(nxt, rnd, "llm", str(adj.get("reason", ""))[:40])
        else:
            # LLM 没给新策略 -> 用剩余策略补位
            for s in STRATEGIES:
                if s not in tried:
                    consider(s, rnd, "llm", "反思补位")
                    break
        converged = best and best["conflict"] == 0 and best["gap"] <= policy.target_gap

    # ---- 胜出策略上跑精解轨, 取更优者 ----
    # 预算按菜数分流 (app/scheduler/dispatch.py, 文档 §3.3.3 落地):
    # 旧实现把两条精解轨的预算**写死** (CP-SAT ≤1.2 s + 精确模型 ≤1.5 s), 不随 N 收缩,
    # 而求解器规模随 N 增长 —— 实测 N=20 时有 2/9 组突破 5 s 线。
    # 现在按 6/N 收缩, 并留 25% 余量给建模/还原/后处理。
    # ⚠ 这里的 left 用 **time_budget**(总预算) 而不是 llm_budget —— 前面所有环节
    #   都被 llm_budget 约束住了, 所以走到这里 left ≥ _solver_reserve(),
    #   精解轨**一定有预算**。这正是 2026-10-04 那个"首次请求被劣化"的修复点。
    # (engine.schedule 内部会在有冻结时自动跳过 CP-SAT)
    left = max(0.0, time_budget - (time.time() - t0))
    bud = solver_budgets(len(recipes), left * _SOLVER_POOL_RATIO)
    # ⚠ 调用方的 use_cpsat=False (设备故障) **只关 CP-SAT**, 不能连带关精确轨。
    #   必须关 CP-SAT: cpsat.py 的模型不读资源容量覆盖, 会把坏掉的设备照旧排进去。
    #   但不能连精确轨一起关: 它走 model.capacities() -> engine.resource_units(),
    #   **是认覆盖的** (实测蒸箱=0 时它判不可行返回 None, 由 engine 回落贪心)。
    #   v3.1 修复: 原实现把整个精解块写成 `if use_cpsat:`, 于是设备故障时两条精解轨
    #   **一起被跳过** —— 恰好在"最需要重新好好排一遍"的场景丢掉了质量最好的那条。
    use_cpsat_here = bool(bud["use_cpsat"]) and bool(use_cpsat)
    solver_tag = "+cpsat" if use_cpsat_here else "+exact"
    if use_cpsat_here or bud["use_exact"]:
      try:
        fine = engine.schedule(recipes, policy=policy, priors=priors,
                               strategy=best["strategy"],
                               use_cpsat=use_cpsat_here,
                               cpsat_budget=bud["cpsat"],
                               # 精确模型轨必须显式传 use_exact: 它的默认值是
                               # "跟随 use_cpsat", 而这里 CP-SAT 可能因预算不足被关掉,
                               # 但精确轨自己还有预算 —— 不显式传就会一起被误关。
                               use_exact=bud["use_exact"],
                               exact_budget=bud["exact"],
                               hard_frozen=hard_frozen)
        kpi_f = evaluate(fine)
        sc_f = score_plan(fine, kpi_f, policy, priors)
        if sc_f < best["score"]:
            trace.append(AgentStep(rnd=0, strategy=best["strategy"] + solver_tag,
                                   makespan=kpi_f.makespan_min, gap=kpi_f.finish_gap_min,
                                   conflict=kpi_f.conflict_count + kpi_f.depend_violation,
                                   hand_streak=kpi_f.hand_streak_min, score=sc_f,
                                   source="solver",
                                   note=("CP-SAT 精解" if use_cpsat_here
                                         else "精确模型精解（设备故障时不跑 CP-SAT）")))
            best = {"plan": fine, "kpi": kpi_f, "chk": check_plan(fine), "score": sc_f,
                    "strategy": best["strategy"] + solver_tag,
                    "makespan": kpi_f.makespan_min, "gap": kpi_f.finish_gap_min,
                    "conflict": kpi_f.conflict_count + kpi_f.depend_violation,
                    "hand_streak": kpi_f.hand_streak_min,
                    "finish": dict(fine.dish_finish or {})}
      except Exception:
        pass

    return AgentResult(
        plan=best["plan"], policy=policy, priors=priors, trace=trace,
        explanation=explain(recipes, policy, priors, best),
        llm_calls=llm.STATS["calls"] - n0,
        llm_ms=llm.STATS["ms"],
        elapsed_ms=round((time.time() - t0) * 1000, 1),
    )


# ---------------- 排程解释 (答辩/专家评审用) ----------------
def explain(recipes: list[Recipe], policy: SchedulingPolicy, priors: dict, best) -> str:
    """生成排程决策解释: 为什么选这条策略、为什么这么排。模板化, 零延迟。"""
    parts = []
    # 精解轨会给策略名加后缀（+cpsat / +exact），解释文案里要还原成基础策略名。
    stg = STRATEGIES.get(re.sub(r"\+(cpsat|exact)$", "", best["strategy"]), {})
    parts.append(f"调度策略「{stg.get('desc', best['strategy'])}」在本轮择优中胜出"
                 f"(总时长 {best['makespan']:.0f} 分, 出锅差 {best['gap']:.0f} 分, "
                 f"最长连续手动 {best['hand_streak']:.0f} 分, 冲突 {best['conflict']})。")
    if policy.rationale:
        parts.append(f"目标权重来自用户偏好: {policy.rationale}。")
    hot = sorted(priors.items(), key=lambda kv: -kv[1].serve_hot)[:2]
    if hot:
        parts.append("保热优先: " + "、".join(f"{k}(趁热{kv.serve_hot:.2f})" for k, kv in hot)
                     + " 被压到最后出锅。")
    hold = sorted(priors.items(), key=lambda kv: -kv[1].hold_min)[:1]
    if hold and hold[0][1].hold_min >= 30:
        parts.append(f"{hold[0][0]} 可放 {hold[0][1].hold_min:.0f} 分钟, 允许提前出锅腾出灶眼。")
    share = [f"{k}({v.merge_group})" for k, v in priors.items() if v.merge_group]
    if share:
        parts.append("同温合炉: " + "、".join(share) + " 可一炉出, 减少升温等待。")
    return " ".join(parts)
