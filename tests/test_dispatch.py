# -*- coding: utf-8 -*-
"""求解器分流测试 (设计文档 §3.3.3)。

护栏要点:
  ① N <= 6 必须给满预算 (与文档一致);
  ② N > 6 预算必须**单调收缩** (否则大实例必然破 5s 线);
  ③ 剩余预算不足时必须**关掉**该轨, 而不是给个 0.01s 假装在跑;
  ④ 总预算永不超过 pool。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["FT_LLM"] = "off"

from app.scheduler.dispatch import (solver_budgets, SPLIT_N,   # noqa: E402
                                    CP_SAT_CAP, EXACT_CAP, MIN_USEFUL)


def test_small_n_gets_full_budget():
    b = solver_budgets(4, 10.0)
    assert b["use_cpsat"] and b["use_exact"]
    assert b["cpsat"] == CP_SAT_CAP and b["exact"] == EXACT_CAP


def test_split_threshold_is_documented_value():
    assert SPLIT_N == 6
    # 恰好 6 道仍是满预算, 7 道开始收缩 —— 阈值落在 6/7 之间
    assert solver_budgets(6, 10.0)["cpsat"] == CP_SAT_CAP
    assert solver_budgets(7, 10.0)["cpsat"] < CP_SAT_CAP


def test_large_n_shrinks_monotonically():
    prev = None
    for n in (7, 8, 10, 12, 15, 20, 40):
        b = solver_budgets(n, 10.0)
        if prev is not None:
            assert b["cpsat"] <= prev + 1e-9, f"N={n} 预算反而变大"
        prev = b["cpsat"]


def test_total_never_exceeds_pool():
    for pool in (0.0, 0.2, 1.0, 3.0, 10.0):
        for n in (3, 6, 7, 20):
            b = solver_budgets(n, pool)
            assert b["cpsat"] + b["exact"] <= pool + 1e-9, \
                f"n={n} pool={pool} 超支: {b}"


def test_insufficient_budget_disables_track():
    b = solver_budgets(3, 0.1)
    assert not b["use_cpsat"] and not b["use_exact"], \
        "预算不足时应当关掉轨道, 而不是给 0.01s 假装在跑"
    b2 = solver_budgets(3, 1.5)          # 0.45/0.55 分下来两轨都过下限
    assert b2["use_cpsat"] and b2["use_exact"]


def test_zero_pool_is_safe():
    b = solver_budgets(20, 0.0)
    assert b == {"use_cpsat": False, "use_exact": False, "cpsat": 0.0, "exact": 0.0}


def test_ft_exact_off_disables_exact_track(monkeypatch):
    """全局熔断开关必须在这里也认 —— 否则消融/对照组会静默失效。

    背景: engine.schedule 只在 `use_exact is None` 时才读 FT_EXACT, 而 orchestrator
    现在总是显式传 use_exact。少了这一条, FT_EXACT=off 会被绕过, verify_model 的
    "关第三轨"对照组实测 60 组全部打平 (看起来像"第三轨毫无价值")。
    """
    monkeypatch.setenv("FT_EXACT", "off")
    b = solver_budgets(4, 10.0)
    assert b["use_exact"] is False and b["exact"] == 0.0
    assert b["use_cpsat"] is True, "关掉精确轨不应连带关掉 CP-SAT 轨"
    monkeypatch.setenv("FT_EXACT", "on")
    assert solver_budgets(4, 10.0)["use_exact"] is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
