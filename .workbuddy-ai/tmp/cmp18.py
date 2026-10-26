# -*- coding: utf-8 -*-
"""新旧解析对比 group#18: 旧口径为什么能 gap=0。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
for k in ("FT_AGENT_LLM", "FT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ.setdefault(k, "off")

from app.spec import device_spec as DS               # noqa: E402
from app.spec.adapter import get, plan_recipes       # noqa: E402
from app.parser.recipe_parser import parse_recipe    # noqa: E402

PICK = ['本帮白切鸡', '拿破仑酥', '柠檬干', '香辣烤鸡爪', '轻松一锅蒸']
_real = DS.strip_parts


def run(strip):
    DS.strip_parts = _real if strip else (lambda t: t or "")
    recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in PICK]
    DS.strip_parts = _real
    return recs, plan_recipes(recs, agent=True)


for strip, tag in ((False, "旧口径(不剥离)"), (True, "新口径(剥离)")):
    recs, p = run(strip)
    print("=" * 78)
    print(f"[{tag}] makespan={p.makespan_min} gap={p.finish_gap_min}")
    print(f"  dish_finish={p.dish_finish}")
    print("  --- 解析出的步骤 ---")
    for r in recs:
        if r.name not in ("柠檬干", "香辣烤鸡爪"):
            continue
        for s in r.steps:
            print(f"    {r.name}#{s.step_id} dev={str(s.device):8s} "
                  f"temp={s.temperature_c} dur={s.duration_min} "
                  f"src={getattr(s, 'device_source', '')} | {s.raw_text[:40]}")
    print("  --- 排程 ---")
    for t in sorted(p.tasks, key=lambda x: x.start_min):
        if t.dish in ("柠檬干", "香辣烤鸡爪"):
            print(f"    {t.start_min:7.1f}-{t.end_min:7.1f} {t.unit_id:10s} "
                  f"{t.dish}#{t.step_id} | {t.raw_text[:34]}")
