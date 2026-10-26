# -*- coding: utf-8 -*-
"""找出"配件词剥离"打开后出锅差变差的具体组, 看差在哪。"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("FT_AGENT_LLM", "off")
os.environ.setdefault("FT_LLM", "off")
os.environ.setdefault("FT_WARM_LLM", "off")
os.environ.setdefault("FT_NOTIFY_LLM", "off")

from app.parser import recipe_parser as RP  # noqa: E402
from app.spec import device_spec as DS  # noqa: E402
from app.spec.adapter import get, all_names, plan_recipes  # noqa: E402
from app.parser.recipe_parser import parse_recipe  # noqa: E402

N, SEED = 50, 42
names = all_names()
_real = DS.strip_parts


def run(picks, strip):
    DS.strip_parts = _real if strip else (lambda t: t or "")
    out = []
    for pick in picks:
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        p = plan_recipes(recs, agent=True)
        out.append(p)
    DS.strip_parts = _real
    return out


random.seed(SEED)
picks = [random.sample(names, random.randint(3, 5)) for _ in range(N)]

on = run(picks, True)
off = run(picks, False)

bad = []
for i, pick in enumerate(picks):
    a, b = on[i], off[i]
    if a.finish_gap_min > 5 >= b.finish_gap_min:
        bad.append((a.finish_gap_min - b.finish_gap_min, i, pick, a, b))
bad.sort(reverse=True)

print(f"变差的组: {len(bad)} / {N}\n")
for d, i, pick, a, b in bad[:5]:
    print("=" * 72)
    print(f"组#{i} {pick}   gap {b.finish_gap_min} -> {a.finish_gap_min}")
    for tag, p in (("剥离关(旧)", b), ("剥离开(新)", a)):
        print(f"  [{tag}] makespan={p.makespan_min} gap={p.finish_gap_min} "
              f"dish_finish={p.dish_finish}")
    print("  --- 新口径的任务表 ---")
    for t in sorted(a.tasks, key=lambda x: x.start_min):
        if t.start_min < 0:
            continue
        print(f"    {t.start_min:7.1f}-{t.end_min:7.1f} {t.unit_id:14s} "
              f"{t.dish}#{t.step_id} | {t.raw_text[:44]}")
