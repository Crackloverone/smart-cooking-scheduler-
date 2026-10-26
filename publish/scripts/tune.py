"""
参数扫描: 找出"出锅差达标率"与"总时长"的最佳平衡点。

扫描两个旋钮:
  - gap_coef : CP-SAT 目标里出锅差的放大系数 (越大越追求同时出锅)
  - budget   : CP-SAT 求解预算 (越久解越优, 但吃掉性能分)
用法: python -m scripts.tune [组数]
"""
from __future__ import annotations
import os, random, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.parser.recipe_parser import parse_recipe
from app.spec.adapter import get, all_names
from app.scheduler import engine
from app.scheduler.metrics import evaluate
from app.agent.priors import get_priors
from app.agent.objective import shape_policy

os.environ.setdefault("FT_LLM", "off")


def trial(cases, gap_coef, budget, strategy="critical_path"):
    gaps, makes, conf, times = [], [], [], []
    for recs in cases:
        t0 = time.time()
        greedy = engine._schedule(recs, strategy=strategy)
        best, best_key = greedy, (greedy.finish_gap_min, greedy.makespan_min)
        try:
            from app.scheduler.cpsat import solve as _cpsat
            from app.models.schema import Recipe as _R
            from app.scheduler.engine import _prep_ahead, estimate_duration
            slim = []
            for rec in recs:
                steps = [s for s in rec.steps
                         if not _prep_ahead(s.device, s.unattended, estimate_duration(s))]
                if steps:
                    slim.append(_R(name=rec.name, ingredients=rec.ingredients,
                                   steps=steps, label=rec.label))
            cp = _cpsat(slim, time_budget=budget, gap_coef=gap_coef)
            if cp is not None:
                k = (cp.finish_gap_min, cp.makespan_min)
                # 以出锅差为主、总时长为辅评判
                if k[0] < best_key[0] or (k[0] == best_key[0] and k[1] < best_key[1]):
                    best, best_key = cp, k
        except Exception:
            pass
        times.append((time.time() - t0) * 1000)
        kk = evaluate(best)
        gaps.append(best.finish_gap_min)
        makes.append(best.makespan_min)
        conf.append(kk.conflict_count + kk.depend_violation)
    keep = [i for i, g in enumerate(gaps) if g < 300]
    g2 = [gaps[i] for i in keep]
    return {
        "gap_coef": gap_coef, "budget": budget,
        "gap均值": round(sum(g2) / len(g2), 1),
        "<5min占比": f"{sum(1 for g in g2 if g <= 5) / len(g2) * 100:.0f}%",
        "makespan均值": round(sum(makes[i] for i in keep) / len(keep), 1),
        "冲突": sum(conf),
        "耗时均值ms": round(sum(times) / len(times), 1),
        "耗时max_ms": round(max(times), 1),
    }


def main(n=20):
    random.seed(42)
    names = all_names()
    picks = [random.sample(names, random.randint(3, 5)) for _ in range(n)]
    cases = [[parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in p]
             for p in picks]
    print(f"{n} 组固定样本, 扫描 gap_coef × budget")
    rows = []
    for coef in (0.25, 0.5, 1.0, 2.0):
        for budget in (0.6, 1.2):
            r = trial(cases, coef, budget)
            rows.append(r)
            print(r)
    best = min(rows, key=lambda r: (float(r["<5min占比"].rstrip("%")) * -1, r["makespan均值"]))
    print("\n最优配置:", best)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
