"""逐实例对比: 线上 Agent 闭环  vs  精确模型 (带语义先验)。"""
from __future__ import annotations
import os, random, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts._offline import force_offline_default
force_offline_default()      # 探针量的是求解器，不是模型：默认离线（见 _offline.py）

from app.parser.recipe_parser import parse_recipe
from app.spec.adapter import get, all_names, plan_recipes
from app.scheduler import model
from app.scheduler.metrics import evaluate
from app.agent.priors import get_priors

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 11
random.seed(SEED)
names = all_names()
sets = [[parse_recipe(x, get(x)["ingredients"], get(x)["steps"])
         for x in random.sample(names, random.randint(4, 5))] for _ in range(N)]

print(f"{'#':>2} {'线上:总时长':>9} {'出锅差':>6} {'手动':>5} | "
      f"{'精确:总时长':>9} {'出锅差':>6} {'手动':>5} | {'耗时(线上/精确)':>14}")
agg = {"a_mk": 0, "a_gp": 0, "a_hs": 0, "e_mk": 0, "e_gp": 0, "e_hs": 0, "n": 0}
for i, recs in enumerate(sets):
    ta = time.time(); pa = plan_recipes(recs, agent=True); ta = (time.time() - ta) * 1000
    ka = evaluate(pa)
    pr = get_priors(recs)
    te = time.time(); pe = model.solve_exact(recs, priors=pr, time_budget=3.0)
    te = (time.time() - te) * 1000
    if pe is None:
        print(f"{i:>2} 精确模型无解")
        continue
    ke = evaluate(pe)
    print(f"{i:>2} {pa.makespan_min:>9.1f} {pa.finish_gap_min:>6.1f} {ka.hand_streak_min:>5.1f} | "
          f"{pe.makespan_min:>9.1f} {pe.finish_gap_min:>6.1f} {ke.hand_streak_min:>5.1f} | "
          f"{ta:>6.0f}/{te:<6.0f}ms")
    for k, v in (("a_mk", pa.makespan_min), ("a_gp", pa.finish_gap_min), ("a_hs", ka.hand_streak_min),
                 ("e_mk", pe.makespan_min), ("e_gp", pe.finish_gap_min), ("e_hs", ke.hand_streak_min)):
        agg[k] += v
    agg["n"] += 1

n = agg["n"]
print(f"\n均值  线上 总时长 {agg['a_mk']/n:6.1f}  出锅差 {agg['a_gp']/n:5.2f}  手动 {agg['a_hs']/n:5.1f}")
print(f"      精确 总时长 {agg['e_mk']/n:6.1f}  出锅差 {agg['e_gp']/n:5.2f}  手动 {agg['e_hs']/n:5.1f}")
print(f"      相对变化  总时长 {(agg['e_mk']-agg['a_mk'])/agg['a_mk']*100:+.1f}%  "
      f"出锅差 {(agg['e_gp']-agg['a_gp'])/max(agg['a_gp'],1e-9)*100:+.1f}%  "
      f"手动 {(agg['e_hs']-agg['a_hs'])/max(agg['a_hs'],1e-9)*100:+.1f}%")
