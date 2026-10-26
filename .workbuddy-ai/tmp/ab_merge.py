# -*- coding: utf-8 -*-
"""A/B: _merge_preheat 的过滤器 (精确文本 vs endswith) 对基准的影响。"""
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
GOOD = engine.PREHEAT_NODE_TEXTS


def run(tag, node_texts):
    engine.PREHEAT_NODE_TEXTS = node_texts
    random.seed(SEED)
    picks = [random.sample(names, random.randint(3, 5)) for _ in range(N)]
    gaps, mks, ok = [], [], 0
    for pick in picks:
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        k = evaluate(plan_recipes(recs, agent=True))
        gaps.append(k.finish_gap_min); mks.append(k.makespan_min)
        if k.finish_gap_min <= 5:
            ok += 1
    engine.PREHEAT_NODE_TEXTS = GOOD
    print(f"{tag:34s} gap中位{sorted(gaps)[len(gaps)//2]:5.1f} 均值{sum(gaps)/N:5.2f} "
          f"<5min {ok/N*100:4.0f}%  总时长均值{sum(mks)/N:6.1f}")


class _EndsWith:
    """模拟旧过滤器: 任何以"预热"结尾的文本都算预热节点。"""
    def __contains__(self, x):
        return isinstance(x, str) and x.endswith("预热")


run("精确文本 (当前, 正确)", GOOD)
run("endswith  (旧口径, 会误吞)", _EndsWith())
