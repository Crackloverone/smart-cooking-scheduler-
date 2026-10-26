# -*- coding: utf-8 -*-
"""验证 gap=0 是否可行: 把香辣烤鸡爪#5 钉到 229 再解。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
for k in ("FT_AGENT_LLM", "FT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ.setdefault(k, "off")

from app.spec.adapter import get                     # noqa: E402
from app.parser.recipe_parser import parse_recipe    # noqa: E402
from app.scheduler import model as M                 # noqa: E402

PICK = ['本帮白切鸡', '拿破仑酥', '柠檬干', '香辣烤鸡爪', '轻松一锅蒸']
recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in PICK]


def show(tag, p):
    if p is None:
        print(f"  [{tag}] None (不可行)")
        return
    print(f"  [{tag}] makespan={p.makespan_min} gap={p.finish_gap_min} "
          f"status={p.solver_status} dish_finish={p.dish_finish}")


print("== 基线 ==")
show("base", M.solve_exact(recs, time_budget=8.0, slack=15, allow_preprocess=True))

print("== 钉死 香辣烤鸡爪#5 @229 (期望 gap=0) ==")
show("fixed 229", M.solve_exact(recs, time_budget=8.0, slack=15,
                                allow_preprocess=True,
                                fixed={("香辣烤鸡爪", 5): 229.0}))

print("== 钉死 香辣烤鸡爪#5 @221 ==")
show("fixed 221", M.solve_exact(recs, time_budget=8.0, slack=15,
                                allow_preprocess=True,
                                fixed={("香辣烤鸡爪", 5): 221.0}))

print("== 只用贪心暖启动 hint ==")
from app.scheduler import engine                     # noqa: E402
g = engine.schedule(recs, use_cpsat=False, use_exact=False)
hint = {(t.dish, t.step_id): float(t.start_min) for t in g.tasks
        if t.dish and t.start_min >= 0}
show("with hint", M.solve_exact(recs, time_budget=8.0, slack=15,
                                allow_preprocess=True, hint=hint))

print("== slack=0 ==")
show("slack0", M.solve_exact(recs, time_budget=8.0, slack=0, allow_preprocess=True))
print("== slack=60 ==")
show("slack60", M.solve_exact(recs, time_budget=8.0, slack=60, allow_preprocess=True))
