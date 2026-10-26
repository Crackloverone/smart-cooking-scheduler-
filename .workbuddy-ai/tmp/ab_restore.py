# -*- coding: utf-8 -*-
"""A/B: 定位 82%->76% 的退化来自 _restore_missing_steps 还是 _merge_preheat 修正。"""
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
_real_restore = engine._restore_missing_steps


def run(tag, restore):
    engine._restore_missing_steps = _real_restore if restore else (lambda p, r: p)
    random.seed(SEED)
    picks = [random.sample(names, random.randint(3, 5)) for _ in range(N)]
    gaps, mks, ok = [], [], 0
    for pick in picks:
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        p = plan_recipes(recs, agent=True)
        k = evaluate(p)
        gaps.append(k.finish_gap_min)
        mks.append(k.makespan_min)
        if k.finish_gap_min <= 5:
            ok += 1
    engine._restore_missing_steps = _real_restore
    gaps.sort(); mks.sort()
    print(f"{tag:26s} 中位{gaps[len(gaps)//2]:6.1f} 均值{sum(gaps)/len(gaps):6.2f} "
          f"<5min {ok/N*100:5.0f}%  总时长中位{mks[len(mks)//2]:7.1f}")


run("补步骤 ON  (当前)", True)
run("补步骤 OFF", False)
