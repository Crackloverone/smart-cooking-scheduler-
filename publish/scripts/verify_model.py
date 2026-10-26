"""健全性自检 + 三轨对比。

检查项 (任一失败即说明模型不可信):
  1. 冲突数 = 0, 依赖违规 = 0
  2. 下界 <= 总时长  (绝不允许出现 makespan < LB)
  3. 交付版三指标对比 —— 分三列:
       交付版(关第三轨)  FT_EXACT=off 的 Agent 闭环 (即接入精确模型之前的水平)
       交付版(开第三轨)  FT_EXACT=on  (线上默认)
       精确模型          model.solve_with_gap(time_budget=3.0) 的上限参考

用法: python -m scripts.verify_model [N] [seed]
"""
from __future__ import annotations
import os, random, sys, time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("FT_LLM", "off")
os.environ.setdefault("FT_AGENT_LLM", "off")
os.environ.setdefault("FT_NOTIFY_LLM", "off")

from app.parser.recipe_parser import parse_recipe
from app.spec.adapter import get, all_names, plan_recipes
from app.scheduler import model
from app.scheduler.metrics import evaluate
from app.agent.priors import get_priors

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 11
random.seed(SEED)
names = all_names()
sets = [[parse_recipe(x, get(x)["ingredients"], get(x)["steps"])
         for x in random.sample(names, random.randint(3, 5))] for _ in range(N)]

print(f"== 健全性自检: {N} 组随机用例 (seed={SEED}) ==")
bad = Counter()
st = []
A = dict(mk=0.0, gp=0.0, hs=0.0, n=0, t=0.0)      # 交付版: 关第三轨
B = dict(mk=0.0, gp=0.0, hs=0.0, n=0, t=0.0)      # 交付版: 开第三轨 (线上默认)
E = dict(mk=0.0, gp=0.0, hs=0.0, n=0, t=0.0)      # 精确模型上限参考
W = T = L = 0
for i, recs in enumerate(sets):
    os.environ["FT_EXACT"] = "off"
    t0 = time.time()
    pa = plan_recipes(recs, agent=True)
    A["t"] += (time.time() - t0) * 1000
    ka = evaluate(pa)
    os.environ["FT_EXACT"] = "on"
    t0 = time.time()
    pb = plan_recipes(recs, agent=True)
    B["t"] += (time.time() - t0) * 1000
    kb = evaluate(pb)
    pr = get_priors(recs)
    t0 = time.time()
    r = model.solve_with_gap(recs, priors=pr, time_budget=3.0)
    et = (time.time() - t0) * 1000
    if not r.get("ok"):
        bad["精确模型无解"] += 1
        continue
    pe, ke = r["plan"], None
    ke = evaluate(pe)
    st.append(r["status"])
    if ke.conflict_count:
        bad["冲突"] += 1
    if ke.depend_violation:
        bad["依赖违规"] += 1
    if pe.makespan_min < r["lb"]["lb"] - 1e-6:
        bad["总时长<下界"] += 1
    for d in (ka, kb):
        if d.conflict_count:
            bad["交付版冲突"] += 1
        if d.depend_violation:
            bad["交付版依赖违规"] += 1
    for d, p, k in ((A, pa, ka), (B, pb, kb), (E, pe, ke)):
        d["mk"] += p.makespan_min; d["gp"] += p.finish_gap_min
        d["hs"] += k.hand_streak_min; d["n"] += 1
    E["t"] += et
    if (pb.makespan_min, pb.finish_gap_min) < (pa.makespan_min, pa.finish_gap_min):
        W += 1
    elif (pb.makespan_min, pb.finish_gap_min) > (pa.makespan_min, pa.finish_gap_min):
        L += 1
    else:
        T += 1

print("  求解状态分布:", Counter(st).most_common())
print("  问题计数:", dict(bad) or "全部通过 (冲突 0 / 依赖违规 0 / 总时长 >= 下界)")
n = A["n"]
print()
print(f"  交付版 关第三轨      总时长 {A['mk']/n:6.1f}   出锅差 {A['gp']/n:5.2f}   "
      f"连续手动 {A['hs']/n:5.1f}   {A['t']/n:6.0f} ms")
print(f"  交付版 开第三轨      总时长 {B['mk']/n:6.1f}   出锅差 {B['gp']/n:5.2f}   "
      f"连续手动 {B['hs']/n:5.1f}   {B['t']/n:6.0f} ms")
print(f"  精确模型上限参考     总时长 {E['mk']/n:6.1f}   出锅差 {E['gp']/n:5.2f}   "
      f"连续手动 {E['hs']/n:5.1f}   {E['t']/n:6.0f} ms")
print(f"  第三轨净收益         总时长 {(B['mk']-A['mk'])/A['mk']*100:+6.1f}%   "
      f"出锅差 {(B['gp']-A['gp'])/max(A['gp'],1e-9)*100:+6.1f}%   "
      f"连续手动 {(B['hs']-A['hs'])/A['hs']*100:+6.1f}%")
print(f"  逐例判定 (开轨 vs 关轨): 更优 {W} / 打平 {T} / 更差 {L}   (共 {n} 组)")

