# -*- coding: utf-8 -*-
"""dump group#18 精确轨的任务区间, 并试探 spread=0 是否真的不可行。"""
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

print("== extract_tasks 明细 (allow_preprocess=True) ==")
tasks = M.extract_tasks(recs, priors=None, allow_preprocess=True)
for t in sorted(tasks, key=lambda x: (x["dish"], x["step_id"])):
    print(f"  {t['dish']:12s}#{t['step_id']:<2d} res={str(t['res']):8s} "
          f"dur={t['dur']:<5} hands={int(t['hands'])} deps={t['deps']} | {t['raw'][:36]}")

print()
ex = M.solve_exact(recs, time_budget=8.0, slack=15, allow_preprocess=True)
print("== 精确轨任务区间 ==")
for t in sorted(ex.tasks, key=lambda x: x.start_min):
    print(f"  {t.start_min:7.1f}-{t.end_min:7.1f} {t.unit_id:10s} "
          f"{t.dish}#{t.step_id:<2d} | {t.raw_text[:38]}")
print("dish_finish =", ex.dish_finish, " makespan =", ex.makespan_min,
      " gap =", ex.finish_gap_min)

# ---- 试探: 把"最早收尾的那道菜"的尾步强行延后, 看是否可行 ----
import collections                                       # noqa: E402
by_dish = collections.defaultdict(list)
for t in tasks:
    by_dish[t["dish"]].append(t)
latest = {}
for d, ts in by_dish.items():
    latest[d] = max(ts, key=lambda x: x["step_id"])
print("\n== 每道菜的'最后一步'(按 step_id) ==")
for d, t in latest.items():
    print(f"  {d:12s} -> #{t['step_id']} res={t['res']} dur={t['dur']} | {t['raw'][:36]}")
