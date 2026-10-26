# -*- coding: utf-8 -*-
"""逐轨打点: 为什么 group#18 最终 gap=8 而不是 0。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
for k in ("FT_AGENT_LLM", "FT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ.setdefault(k, "off")

from app.spec.adapter import get, plan_recipes          # noqa: E402
from app.parser.recipe_parser import parse_recipe       # noqa: E402
from app.scheduler import engine                        # noqa: E402

PICK = ['本帮白切鸡', '拿破仑酥', '柠檬干', '香辣烤鸡爪', '轻松一锅蒸']

recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in PICK]


def show(tag, p):
    if p is None:
        print(f"  [{tag}] None")
        return
    print(f"  [{tag}] makespan={p.makespan_min} gap={p.finish_gap_min} "
          f"status={getattr(p, 'solver_status', '?')}")
    print(f"          dish_finish={p.dish_finish}")


print("== 贪心轨 (use_cpsat=False, use_exact=False) ==")
show("greedy", engine.schedule(recs, use_cpsat=False, use_exact=False))

print("== 精确轨单独调 ==")
from app.scheduler import model as _model                    # noqa: E402
for ap in (False, True):
    ex = _model.solve_exact(recs, time_budget=5.0, slack=15, allow_preprocess=ap)
    show(f"exact ap={ap}", ex)
    print("          meta=", _model._SOLVE_META.get())

print("== 完整三轨 ==")
show("three-track", engine.schedule(recs))

print("== plan_recipes(agent=True) ==")
show("plan_recipes", plan_recipes(recs, agent=True))
