# -*- coding: utf-8 -*-
"""Agent 闭环的**预算分配**护栏。

核心回归（2026-10-04 实测的真 bug）
----------------------------------
现象: 进程启动后的**第一次**排程被严重劣化 —— 4 菜时 Γ 1→8 分、T 37→52 分
（离线基准 vs 慢模型首请求）。

根因: `orchestrator._run_inner` 里在线 LLM 的两次调用（目标塑形 + 策略提议）
吃光 `time_budget=3.6s` ⇒ `left = max(0, 3.6 - elapsed) ≈ 0`
⇒ `solver_budgets(4, 0)` 两条精解轨全 `use_*=False`
⇒ **整个精解段被跳过, 交付贪心解**。
在线闸门（`FT_LLM_ONLINE_GATE`）是在本轮**结束后**才落下的,
所以每次进程启动后的第一次请求都中招。

而精解轨是最大的质量杠杆（第三轨净收益 T −21.6% / Γ −32.0%）——
"LLM 慢"绝不该升级成"丢掉质量最好的那条轨"。

修复: 先给精解段扣出一份"能开轨"的预算（`_solver_reserve`）,
再在其中切出 LLM 配额（`llm_quota`）与策略枚举保底（`_ENUM_RESERVE`）。
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.agent import llm                                        # noqa: E402
from app.agent import orchestrator as orch                       # noqa: E402
from app.agent.orchestrator import (run, _solver_reserve,        # noqa: E402
                                    _ENUM_RESERVE, _LLM_MIN_ROUNDTRIP,
                                    _SOLVER_POOL_RATIO)
from app.parser.recipe_parser import parse_recipe                # noqa: E402
from app.scheduler.dispatch import (solver_budgets,              # noqa: E402
                                    min_pool_for_tracks, MIN_USEFUL)
from app.scheduler.metrics import evaluate                       # noqa: E402
from app.spec.adapter import get, all_names, plan_recipes        # noqa: E402


@pytest.fixture(autouse=True)
def _pin_ft_exact(monkeypatch):
    """钉住 FT_EXACT=on —— 否则本文件的"两轨都要开"断言会变成
    "开发机 shell 有没有 export FT_EXACT" 的函数。需要关掉它的用例
    自己再 monkeypatch.setenv（同一实例，后设的覆盖先设的）。"""
    monkeypatch.setenv("FT_EXACT", "on")


# --------------------------------------------------------------------------
# 1. 反解口径必须与 solver_budgets 完全一致（不许两处各写一份公式）
# --------------------------------------------------------------------------
@pytest.mark.parametrize("n", [2, 3, 4, 6, 7, 8, 10, 12, 20])
def test_min_pool_is_tight_and_sufficient(n):
    """min_pool 必须是**紧**下界：恰好够开轨，且减一点点就不够。"""
    pool = min_pool_for_tracks(n)
    assert pool > 0.0
    b = solver_budgets(n, pool)
    assert b["use_cpsat"] and b["use_exact"], f"N={n} 给了 {pool}s 仍开不了轨"
    # 紧性：略小于它就该关（二分收敛到 ~1e-12，留 1% 余量做浮点缓冲）
    b2 = solver_budgets(n, pool * 0.99)
    assert not (b2["use_cpsat"] and b2["use_exact"]), \
        f"N={n} min_pool={pool} 不够紧（{pool*0.99}s 时两轨仍开）"


def test_min_pool_zero_when_track_cannot_open():
    """N 大到 cap 本身低于 MIN_USEFUL 时，该轨无论多少预算都不开 ⇒ 返回 0。

    这条是"不需要为它预留"的语义 —— 不能返回 inf 让调用方去处理。
    """
    assert min_pool_for_tracks(40) == 0.0
    assert solver_budgets(40, 1e6)["use_cpsat"] is False


def test_min_pool_handles_ft_exact_off(monkeypatch):
    """FT_EXACT=off 时只该为 CP-SAT 预留，不能因为精确轨被关就返回 0。"""
    monkeypatch.setenv("FT_EXACT", "off")
    pool = min_pool_for_tracks(4, want_cpsat=True, want_exact=True)
    assert pool > 0.0, "精确轨被全局关掉后，CP-SAT 的预留也不该丢"
    assert solver_budgets(4, pool)["use_cpsat"] is True


# --------------------------------------------------------------------------
# 2. 预留必须真的够开两条轨（N ≤ 20 的全部生产规模）
# --------------------------------------------------------------------------
@pytest.mark.parametrize("n", [2, 3, 4, 6, 7, 8, 10, 12, 20])
def test_reserve_actually_enables_both_tracks(n):
    r = _solver_reserve(n, use_cpsat=True)
    assert r > 0.0
    b = solver_budgets(n, r * _SOLVER_POOL_RATIO)
    assert b["use_cpsat"] and b["use_exact"], \
        f"N={n} reserve={r} 换算成池后仍开不了轨: {b}"


def test_reserve_is_zero_only_when_tracks_impossible():
    assert _solver_reserve(40, use_cpsat=True) == 0.0


# --------------------------------------------------------------------------
# 3. 端到端：慢模型首请求**不许**比离线基准差（核心回归）
# --------------------------------------------------------------------------
def _pick_recipes(seed=11, k=4):
    import random
    random.seed(seed)
    names = all_names()
    warm = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"])
            for x in names[:3]]
    plan_recipes(warm)                       # 预热解析 / 先验缓存
    pick = random.sample(names, k)
    return pick, [parse_recipe(x, get(x)["ingredients"], get(x)["steps"])
                  for x in pick]


def _arm_online(monkeypatch):
    """把 LLM 开关打开，并钉住"闸门不落"（模拟进程启动后的首次请求）。"""
    monkeypatch.setenv("FT_LLM", "on")
    monkeypatch.setenv("FT_AGENT_LLM", "on")
    monkeypatch.setenv("FT_LLM_ONLINE_GATE", "off")
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "breaker_open", lambda: False)
    monkeypatch.setattr(llm, "online_usable", lambda: True)


def test_slow_llm_first_request_keeps_solver_track(monkeypatch):
    """慢模型把预算吃光时，精解轨**必须**仍然跑 —— 这就是那个 bug 的根因。"""
    _arm_online(monkeypatch)
    pick, recs = _pick_recipes()
    budget = 2.0

    def slow_chat_json(system, user, cache_key=None, temperature=0.1,
                       timeout=20.0):
        time.sleep(float(timeout or 1.0))
        llm.STATS["calls"] += 1
        llm.STATS["deadline_miss"] += 1
        return None

    monkeypatch.setattr(llm, "chat_json", slow_chat_json)

    res = run(recs, use_llm=True, time_budget=budget)
    assert any("+" in s.strategy for s in res.trace), \
        f"慢模型首请求把精解轨整个跳过了（trace={[s.strategy for s in res.trace]}）"
    # 枚举也必须跑完：propose 被跳过时 cand 就是全策略，而"试完 6 条策略"
    # 是贪心质量的保证 —— 不能让 LLM 的配额把它挤掉（_ENUM_RESERVE 的作用）。
    from app.scheduler.engine import STRATEGIES
    tried = {s.strategy for s in res.trace if "+" not in s.strategy}
    assert tried == set(STRATEGIES.keys()), \
        f"慢模型时策略枚举没跑完（只试了 {sorted(tried)}）"

    # 质量不许劣化：与同一预算下的离线基准比（容差很小，因为路径本就相同）
    off = run(recs, use_llm=False, time_budget=budget)
    k_slow, k_off = evaluate(res.plan), evaluate(off.plan)
    assert k_slow.finish_gap_min <= k_off.finish_gap_min + 0.5, \
        f"出锅差劣化: 慢模型 {k_slow.finish_gap_min} vs 离线 {k_off.finish_gap_min}"
    assert k_slow.makespan_min <= k_off.makespan_min + 2.0, \
        f"总时长劣化: 慢模型 {k_slow.makespan_min} vs 离线 {k_off.makespan_min}"
    assert k_slow.conflict_count == 0 and k_slow.depend_violation == 0


def test_fast_llm_still_uses_model_and_solver(monkeypatch):
    """反向回归：修复不能把 LLM 一起废掉 —— 快模型时塑形/提议都要生效。"""
    _arm_online(monkeypatch)
    pick, recs = _pick_recipes()

    def fast_chat_json(system, user, cache_key=None, temperature=0.1,
                       timeout=20.0):
        llm.STATS["calls"] += 1
        if "可选策略" in user:
            return {"strategies": ["serve_hot_last", "critical_path", "balanced"],
                    "reason": "test"}
        return {"w_gap": 90, "w_makespan": 1, "rationale": "test"}

    monkeypatch.setattr(llm, "chat_json", fast_chat_json)

    res = run(recs, use_llm=True, user_text="", time_budget=2.0)
    assert res.policy.source == "llm", "目标塑形没有命中模型"
    assert any("+" in s.strategy for s in res.trace), "精解轨没有跑"
    assert res.llm_calls >= 2, f"提议似乎没生效（calls={res.llm_calls}）"


def test_llm_budget_never_eats_solver_reserve(monkeypatch):
    """结构性断言：LLM 配额 + 枚举保底 = 精解段之前的全部预算，
    且精解段剩余的池**一定**够开两条轨（不看运气、只看算术）。"""
    for n in (3, 4, 7, 12, 20):
        reserve = _solver_reserve(n, True)
        llm_budget = 3.6 - reserve
        llm_quota = max(0.0, llm_budget - _ENUM_RESERVE)
        left = 3.6 - (llm_quota + _ENUM_RESERVE)      # 最坏：LLM 与枚举都用满
        b = solver_budgets(n, left * _SOLVER_POOL_RATIO)
        assert b["use_cpsat"] and b["use_exact"], \
            f"N={n} 最坏情况下精解轨仍被饿死: {b}"
    assert _LLM_MIN_ROUNDTRIP > 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
