"""
下界紧致度体检: 量出"交付版计划"距离可证下界还有多远。

为什么需要一个独立脚本:
  L3 要求"总烹饪时间最短"。只报 makespan 没有说服力 —— 必须给出
  **可证下界 LB** 与 **最优性间隙 gap = (makespan - LB) / LB**。
  LB 取「依赖关键路径 / 批次机产能 / 人工总工时」三者最大, 由
  model.lower_bound_from_plan() 从**已产出的计划**反推, 构造上保证 LB <= makespan,
  因此永远不会出现"总时长 < 下界"这种自相矛盾的汇报。

  注意方向: 人工项假设"单人无空闲连续作业", 是**放松**(偏松)的假设,
  所以它给的是 gap 的**保守上界** —— 真实间隙不会比报出来的更差。

用法: python -m scripts.lb_probe [组数] [seed]
输出: data/lb_report.json + 控制台汇总
"""
from __future__ import annotations
import json
import os
import random
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts._offline import force_offline_default
force_offline_default()      # 下界探针量的是求解器，不是模型：默认离线（见 _offline.py）

from app.parser.recipe_parser import parse_recipe
from app.spec.adapter import get, all_names, plan_recipes
from app.scheduler import model as M


def _binding(lb: dict) -> str:
    """哪个下界项在起约束作用 (决定 gap 的语义)。"""
    cand = {"关键路径": lb["lb_cp"], "批次机产能": lb["lb_energy"], "人工总工时": lb["lb_hand"]}
    top = max(cand.values())
    # 并列时全部列出
    names = [k for k, v in cand.items() if abs(v - top) < 1e-6]
    return "+".join(names) if len(names) > 1 else names[0]


def main(n: int = 60, seed: int = 11) -> dict:
    random.seed(seed)
    names = all_names()
    try:  # 预热 (OR-Tools 冷启动)
        warm = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in names[:3]]
        plan_recipes(warm)
    except Exception:
        pass

    gaps, mk_list, binding, exact_hit = [], [], Counter(), 0
    g_gaps, g_at_lb = [], 0
    for _ in range(n):
        pick = random.sample(names, random.randint(3, 5))
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        plan = plan_recipes(recs)
        live = [t for t in plan.tasks if t.start_min >= 0]
        mk = max(float(plan.makespan_min), max((t.end_min for t in live), default=0.0))
        if mk <= 0:
            continue
        lb = M.lower_bound_from_plan(plan)
        if lb["lb"] > 0:
            gaps.append((mk - lb["lb"]) / lb["lb"] * 100)
        mk_list.append(mk)
        binding[_binding(lb)] += 1
        if mk <= lb["lb"] + 1e-6:
            exact_hit += 1
        # Γ 的独立下界 (末步串行差) —— 出锅差此前没有下界, 这里补上。
        glb = M.gamma_lb_from_plan(plan)
        g = float(plan.finish_gap_min)
        g_gaps.append(g - glb)
        if g <= glb + 1e-6:
            g_at_lb += 1

    gaps_sorted = sorted(gaps)
    g_gaps_sorted = sorted(g_gaps)
    report = {
        "组数": len(mk_list), "seed": seed,
        "口径": ("gap = (makespan - LB) / LB。LB = max(依赖关键路径, 批次机产能, 人工总工时)。"
                 "人工项假设单人无空闲, 是放松假设 ⇒ gap 为**真实最优间隙的保守上界**。"),
        "gap_中位数_pct": round(gaps_sorted[len(gaps_sorted) // 2], 1),
        "gap_均值_pct": round(sum(gaps) / len(gaps), 1),
        "gap_p90_pct": round(gaps_sorted[int(len(gaps_sorted) * 0.9)], 1),
        "gap_max_pct": round(max(gaps), 1),
        "makespan_等于下界的组数": exact_hit,
        "makespan_等于下界占比": f"{exact_hit / len(mk_list) * 100:.0f}%",
        "总时长_下界违反数": sum(1 for g in gaps if g < -1e-6),
        "起约束作用的下界项": dict(binding.most_common()),
        # ---- 出锅差 Γ 的独立下界 (末步串行差) ----
        "Γ下界_口径": ("Γ_lb = Σd − max d, d 取同一**容量 1** 资源上的各菜**末步**时长。"
                       "蒸箱/灶具因可并行而排除; 是放松下界 ⇒ Γ_lb <= Γ 恒成立。"),
        "Γ_等于下界的组数": g_at_lb,
        "Γ_等于下界占比": f"{g_at_lb / len(g_gaps) * 100:.0f}%",
        "Γ减下界_中位": round(g_gaps_sorted[len(g_gaps_sorted) // 2], 1),
        "Γ减下界_均值": round(sum(g_gaps) / len(g_gaps), 1),
        "Γ减下界_max": round(max(g_gaps), 1),
        "Γ_下界违反数": sum(1 for v in g_gaps if v < -1e-6),
    }
    out = os.path.join(ROOT, "data", "lb_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    _n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    _s = int(sys.argv[2]) if len(sys.argv) > 2 else 11
    main(_n, _s)
