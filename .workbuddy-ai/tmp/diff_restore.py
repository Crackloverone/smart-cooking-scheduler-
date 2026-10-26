# -*- coding: utf-8 -*-
"""逐组 diff: 补步骤 ON/OFF 到底改变了哪一组、改变了什么。"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
for k in ("FT_AGENT_LLM", "FT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ.setdefault(k, "off")

from app.scheduler import engine                      # noqa: E402
from app.scheduler.metrics import evaluate            # noqa: E402
from app.spec.adapter import all_names, get, plan_recipes  # noqa: E402
from app.parser.recipe_parser import parse_recipe     # noqa: E402

N, SEED = 50, 42
names = all_names()
_real = engine._restore_missing_steps


def run(restore):
    engine._restore_missing_steps = _real if restore else (lambda p, r: p)
    random.seed(SEED)
    picks = [random.sample(names, random.randint(3, 5)) for _ in range(N)]
    out = []
    for pick in picks:
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        p = plan_recipes(recs, agent=True)
        out.append((evaluate(p), p))
    engine._restore_missing_steps = _real
    return picks, out


picks, on = run(True)
_, off = run(False)

diff = 0
for i in range(N):
    a, pa = on[i]
    b, pb = off[i]
    if (a.finish_gap_min, a.makespan_min) != (b.finish_gap_min, b.makespan_min):
        diff += 1
        if diff <= 6:
            print(f"组#{i} {picks[i]}")
            print(f"   ON  gap={a.finish_gap_min:5.1f} mk={a.makespan_min:6.1f} "
                  f"tasks={a.total_tasks} hand={a.hand_streak_min:.1f} "
                  f"util={a.device_utilization:.1f}")
            print(f"   OFF gap={b.finish_gap_min:5.1f} mk={b.makespan_min:6.1f} "
                  f"tasks={b.total_tasks} hand={b.hand_streak_min:.1f} "
                  f"util={b.device_utilization:.1f}")
print(f"有差异的组: {diff}/{N}")
