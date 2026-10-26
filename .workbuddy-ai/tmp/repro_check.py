"""可复现性检查：同一组合连跑多次，结果是否逐位一致。

为什么查这个
------------
A/B 时发现 20 菜那组两次跑出 makespan **367 vs 158**（差 2.3 倍）。
开关缓存只影响"读几遍环境变量"，绝不该改变结果 ⇒ 说明排程本身**不确定**。

这比"慢"严重得多：评分脚本要核对结果，同一输入两次给不同计划，
轻则评委信任度扣分，重则"总耗时与理论最优解对比"这项根本没法算。
"""
import os
import random
import sys
import time

sys.path.insert(0, os.getcwd())
for k in ("FT_LLM", "FT_AGENT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ[k] = "off"
os.environ.setdefault("FT_DEVICES_FILE", os.path.join(os.getcwd(), ".cache", "_dev_rep.json"))
os.environ.setdefault("FT_SCHEDULES_FILE", os.path.join(os.getcwd(), ".cache", "_sched_rep.json"))
os.environ.setdefault("FT_EXEC_FILE", os.path.join(os.getcwd(), ".cache", "_exec_rep.json"))

from app.parser.recipe_parser import parse_recipe          # noqa: E402
from app.spec.adapter import all_names, get, plan_recipes  # noqa: E402

names = all_names()
rnd = random.Random(20261009)
sets = [rnd.sample(names, n) for n in (5, 9, 12, 20)]

print("同一组合连跑 5 次，看结果是否稳定\n")
bad = 0
for sel in sets:
    recs = [parse_recipe(n, get(n)["ingredients"], get(n)["steps"]) for n in sel]
    plan_recipes(recs)          # warm-up
    rows = []
    for _ in range(5):
        t0 = time.perf_counter()
        p = plan_recipes(recs)
        rows.append((round(p.makespan_min, 3), round(p.finish_gap_min, 3),
                     len(p.tasks), round((time.perf_counter() - t0) * 1000)))
    uniq = set((r[0], r[1], r[2]) for r in rows)
    stable = len(uniq) == 1
    if not stable:
        bad += 1
    print(f"{len(sel):>3} 菜  {'✓ 稳定' if stable else '✗ 不稳定'}")
    for r in rows:
        print(f"        makespan {r[0]:>7.2f}  gap {r[1]:>6.2f}  tasks {r[2]:>3}  {r[3]:>6} ms")
    if not stable:
        print(f"        → {len(uniq)} 种不同结果")

print()
print("不稳定的菜数档:", bad)
sys.exit(1 if bad else 0)
