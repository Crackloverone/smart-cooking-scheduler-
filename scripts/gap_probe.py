"""
提升空间探针: 量化"算法还有多少提升空间", 分辨瓶颈到底在 搜索 / 硬件容量 / 输入精度。
用法: python -m scripts.gap_probe [组数] [seed]
"""
from __future__ import annotations
import copy, os, random, sys, time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts._offline import force_offline_default
force_offline_default()      # 探针量的是求解器，不是模型：默认离线（见 _offline.py）

from app.parser.recipe_parser import parse_recipe
from app.spec.adapter import get, all_names
from app.scheduler import model
from app.scheduler.engine import estimate_duration
from app.scheduler.metrics import evaluate
from app.agent.priors import get_priors

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 11


def gen(n, seed):
    random.seed(seed)
    names = all_names()
    return [[parse_recipe(x, get(x)["ingredients"], get(x)["steps"])
             for x in random.sample(names, random.randint(4, 5))] for _ in range(n)]


def run_sets(sets, pris, solver, **kw):
    plans, tss, lbs, sts = [], [], [], []
    for recs, pr in zip(sets, pris):
        t0 = time.time()
        res = solver(recs, priors=pr, **kw)
        tss.append((time.time() - t0) * 1000)
        if isinstance(res, dict):
            plans.append(res.get("plan"))
            lbs.append((res.get("lb") or {}).get("lb"))
            sts.append(res.get("status", "?"))
        else:
            plans.append(res); lbs.append(None); sts.append("LNS")
    return plans, tss, lbs, sts


def fmt(plans, tss, lbs, sts=None):
    idx = [i for i, p in enumerate(plans) if p and p.finish_gap_min < 300]
    if not idx:
        return "无有效样本"
    mk = sum(plans[i].makespan_min for i in idx) / len(idx)
    gp = sum(plans[i].finish_gap_min for i in idx) / len(idx)
    hs = sum(evaluate(plans[i]).hand_streak_min for i in idx) / len(idx)
    lbv = [lbs[i] for i in idx if lbs[i]]
    ratio = f"{mk / (sum(lbv) / len(lbv)):4.2f}" if lbv else "  --"
    st = f"   {Counter(sts).most_common(1)[0][0]}" if sts else ""
    return (f"总时长 {mk:6.1f}  /LB {ratio}  出锅差 {gp:5.2f}  "
            f"连续手动 {hs:5.1f}  {sum(tss)/len(tss):6.0f}ms{st}")


def main():
    sets = gen(N, SEED)
    pris = [get_priors(r) for r in sets]
    print(f"== 用例 {N} 组 (seed={SEED}, 每组 4-5 道菜, 已接入语义先验) ==")

    print("\n[1] 搜索空间: 单次求解 vs 更大算力 vs LNS 大邻域")
    for label, kw in [("单次 3s / 4 worker", dict(time_budget=3.0, workers=4)),
                      ("单次 3s / 8 worker", dict(time_budget=3.0, workers=8))]:
        p, t, l, s = run_sets(sets, pris, model.solve_with_gap, **kw)
        print(f"  {label:22s} {fmt(p, t, l, s)}")
    for rounds, keep in [(8, 0.6), (16, 0.5)]:
        p, t, l, s = run_sets(sets, pris, model.solve_lns, time_budget=3.0,
                              rounds=rounds, keep_ratio=keep)
        print(f"  {'LNS %d 轮 keep=%.1f' % (rounds, keep):22s} {fmt(p, t, l, s)}")

    print("\n[2] 硬件容量: 烤箱腔体 1 -> 2 (同温可双层同烤)")
    for q in (1, 2):
        p, t, l, s = run_sets(sets, pris, model.solve_with_gap, time_budget=3.0, oven_q=q)
        print(f"  {'oven_q=' + str(q):22s} {fmt(p, t, l, s)}")

    print("\n[3] 输入精度: 只扰动'缺时长靠默认值估'的步骤")
    miss = sum(1 for recs in sets for r in recs for st in r.steps if st.duration_min is None)
    tot = sum(len(r.steps) for recs in sets for r in recs)
    print(f"  缺显式时长的步骤: {miss}/{tot} 步 ({miss/tot*100:.0f}%)")
    for scale in (0.7, 1.0, 1.3):
        scaled = copy.deepcopy(sets)
        for recs in scaled:
            for r in recs:
                for st in r.steps:
                    if st.duration_min is None:
                        st.duration_min = max(1.0, round(estimate_duration(st) * scale, 1))
        spris = [get_priors(r) for r in scaled]
        p, t, l, s = run_sets(scaled, spris, model.solve_with_gap, time_budget=2.0)
        print(f"  {'缺时长步骤 x' + str(scale):22s} {fmt(p, t, l, s)}")


if __name__ == "__main__":
    main()
