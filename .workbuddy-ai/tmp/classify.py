# -*- coding: utf-8 -*-
"""对每个退化组, 逐菜对比 新旧解析的设备任务, 人工核对"新的对不对"。"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
for k in ("FT_AGENT_LLM", "FT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ.setdefault(k, "off")

from app.spec import device_spec as DS               # noqa: E402
from app.spec.adapter import get, all_names, plan_recipes   # noqa: E402
from app.parser.recipe_parser import parse_recipe    # noqa: E402

N, SEED = 50, 42
names = all_names()
_real = DS.strip_parts
DEV = ("蒸箱", "烤箱", "灶具", "冰箱", "洗碗机", "净咖一体机", "烟机")


def run(picks, strip):
    DS.strip_parts = _real if strip else (lambda t: t or "")
    out = []
    for pick in picks:
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        out.append((recs, plan_recipes(recs, agent=True)))
    DS.strip_parts = _real
    return out


random.seed(SEED)
picks = [random.sample(names, random.randint(3, 5)) for _ in range(N)]
on = run(picks, True)
off = run(picks, False)


def devmap(recs):
    m = {}
    for r in recs:
        m[r.name] = [(s.step_id, s.device, s.duration_min, s.raw_text[:30])
                     for s in r.steps if s.device in DEV]
    return m


for i, pick in enumerate(picks):
    a, b = on[i][1], off[i][1]
    if not (a.finish_gap_min > 5 >= b.finish_gap_min):
        continue
    print("=" * 78)
    print(f"组#{i} {pick}   gap {b.finish_gap_min} -> {a.finish_gap_min}")
    mo, mn = devmap(off[i][0]), devmap(on[i][0])
    for d in pick:
        if mo.get(d) == mn.get(d):
            continue
        print(f"  ── {d}")
        print(f"     旧: {mo.get(d)}")
        print(f"     新: {mn.get(d)}")
