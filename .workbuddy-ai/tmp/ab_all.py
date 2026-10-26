# -*- coding: utf-8 -*-
"""决定性 A/B: 三个改动各自对基准的影响。"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
for k in ("FT_AGENT_LLM", "FT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ.setdefault(k, "off")

from collections import defaultdict
from app.scheduler import engine, prep as PREP        # noqa: E402
from app.scheduler.metrics import evaluate            # noqa: E402
from app.spec.adapter import all_names, get, plan_recipes  # noqa: E402
from app.parser.recipe_parser import parse_recipe     # noqa: E402

N, SEED = 50, 42
names = all_names()
GOOD_NODES = engine.PREHEAT_NODE_TEXTS
GOOD_RESTORE = engine._restore_missing_steps
GOOD_PREP = PREP.prep_keys


class _EndsWith:
    def __contains__(self, x):
        return isinstance(x, str) and x.endswith("预热")


def _old_prep_keys(recipes, priors=None, allow_preprocess=False):
    """旧口径: hard 分支跳过前缀链检查。"""
    by_dish = defaultdict(list)
    for rec in recipes:
        for st in rec.steps:
            by_dish[rec.name].append(st)
    out = set()
    for dish, steps in by_dish.items():
        steps.sort(key=lambda s: s.step_id)
        prior_ids = set()
        if priors and allow_preprocess and dish in priors:
            prior_ids = set(getattr(priors[dish], "prep_step_ids", []) or [])
        for st in steps:
            key = (dish, st.step_id)
            if key in out:
                continue
            hard = PREP.is_hard_prep(st.device, st.unattended,
                                     engine.estimate_duration(st))
            soft = st.step_id in prior_ids
            if hard or (soft and all((dish, d) in out for d in st.depends_on)):
                out.add(key)
    return out


def run(tag, restore, nodes, pk):
    engine._restore_missing_steps = GOOD_RESTORE if restore else (lambda p, r: p)
    engine.PREHEAT_NODE_TEXTS = nodes
    PREP.prep_keys = pk
    engine.prep_keys = pk
    # model.py 是 `from prep import prep_keys as _prep_keys`, 必须**同时**覆盖,
    # 否则精确轨仍走新判据, 这个 A/B 就是无效的 (上一版就踩了这个坑)。
    import app.scheduler.model as _M
    _M._prep_keys = pk
    random.seed(SEED)
    picks = [random.sample(names, random.randint(3, 5)) for _ in range(N)]
    gaps, mks, ok = [], [], 0
    for pick in picks:
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        k = evaluate(plan_recipes(recs, agent=True))
        gaps.append(k.finish_gap_min); mks.append(k.makespan_min)
        if k.finish_gap_min <= 5:
            ok += 1
    engine._restore_missing_steps = GOOD_RESTORE
    engine.PREHEAT_NODE_TEXTS = GOOD_NODES
    engine.prep_keys = GOOD_PREP
    PREP.prep_keys = GOOD_PREP
    print(f"{tag:30s} gap均值{sum(gaps)/N:5.2f} <5min {ok/N*100:4.0f}%  "
          f"总时长均值{sum(mks)/N:6.1f}")


run("全关(=改动前)", False, _EndsWith(), _old_prep_keys)
run("只开 prep统一", False, _EndsWith(), GOOD_PREP)
run("只开 补步骤", True, _EndsWith(), _old_prep_keys)
run("只开 合并过滤", False, GOOD_NODES, _old_prep_keys)
run("全开(=当前)", True, GOOD_NODES, GOOD_PREP)
