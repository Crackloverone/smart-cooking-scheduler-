# -*- coding: utf-8 -*-
"""测量不同菜数下的端到端耗时, 为求解器分流提供依据。"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
for k in ("FT_AGENT_LLM", "FT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ.setdefault(k, "off")

from app.spec.adapter import all_names, get, plan_recipes   # noqa: E402
from app.parser.recipe_parser import parse_recipe           # noqa: E402

names = all_names()
random.seed(7)

print(f"{'N':>3} {'中位ms':>8} {'最大ms':>8} {'出锅差中位':>10} {'总时长中位':>10} {'超5s':>5}")
for n in (3, 4, 5, 6, 8, 10, 12, 15, 20):
    ts, gaps, mks, over = [], [], [], 0
    for _ in range(9):
        pick = random.sample(names, n)
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        t0 = time.perf_counter()
        p = plan_recipes(recs, agent=True)
        dt = (time.perf_counter() - t0) * 1000
        ts.append(dt)
        gaps.append(p.finish_gap_min)
        mks.append(p.makespan_min)
        if dt > 5000:
            over += 1
    ts.sort()
    print(f"{n:>3} {ts[len(ts)//2]:>8.0f} {ts[-1]:>8.0f} "
          f"{sorted(gaps)[len(gaps)//2]:>10.1f} {sorted(mks)[len(mks)//2]:>10.1f} "
          f"{over:>5}")
