"""
Bench 测试: 随机 N 组 3-5 道菜组合, 统计核心指标。

v2.1: 默认走与 /api/plan **完全相同**的排程入口 (Agent 闭环),
      不再单独调求解器 —— 否则测出来的性能与质量和评分路径不是一回事。
用法: python -m scripts.bench [组数] [plain]
输出: 控制台 + data/bench_report.json
"""
from __future__ import annotations
import json, os, random, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts._offline import force_offline_default

# 基准量的是**排程器**，不是模型：默认离线，保证与文档里那组基线同口径。
# 要测"模型在环"的版本：FT_LLM=on FT_AGENT_LLM=on python -m scripts.bench 50
force_offline_default()

from app.parser.recipe_parser import parse_recipe
from app.spec.adapter import get, all_names, plan_recipes
from app.scheduler.metrics import evaluate


def run(n=50, seed=42, agent=True, out_name="bench_report.json"):
    random.seed(seed)
    names = all_names()
    gaps, makespans, conflicts, times, hands = [], [], [], [], []
    # 预热: 首次调用含 OR-Tools / 规则表冷启动(实测可达数秒), 与线上稳态无关。
    # 线上由 start.bat 自检步骤在服务接受请求前完成同样的预热, 故计时从稳态开始。
    try:
        _warm = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in names[:3]]
        plan_recipes(_warm, agent=agent)
    except Exception:
        pass
    for _ in range(n):
        pick = random.sample(names, random.randint(3, 5))
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        t0 = time.time()
        p = plan_recipes(recs, agent=agent)
        times.append((time.time() - t0) * 1000)
        k = evaluate(p)
        gaps.append(p.finish_gap_min)
        makespans.append(p.makespan_min)
        conflicts.append(k.conflict_count + k.depend_violation)
        hands.append(k.hand_streak_min)
    keep = [i for i, g in enumerate(gaps) if g < 300]   # 排除超长冷藏/浸泡离群
    g2 = [gaps[i] for i in keep]
    # 关于 排程耗时_max_ms: 未设 FT_AGENT_LLM=off 时, 第一次调用会真的去连模型并
    # 等到超时(约 3.5s), 熔断打开后其余组恢复正常。max 动辄 3~4s 是这个原因, 不是
    # 求解器慢 —— 要测稳态性能请加 `FT_AGENT_LLM=off FT_LLM=off`
    # (实测离线: mean≈65ms / p95≈120ms / max≈190ms)。
    report = {
        "模式": "Agent闭环" if agent else "单策略求解器",
        "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "环境": (f"FT_EXACT={os.environ.get('FT_EXACT', 'on')} · "
                 f"FT_AGENT_LLM={os.environ.get('FT_AGENT_LLM', 'on')} · seed={seed}"),
        "组数": n,
        "出锅差_中位数_min": sorted(g2)[len(g2) // 2],
        "出锅差_均值_min": round(sum(g2) / len(g2), 1),
        "出锅差<5min占比": f"{sum(1 for g in g2 if g <= 5) / len(g2) * 100:.0f}%",
        "总时长_均值_min": round(sum(makespans[i] for i in keep) / len(keep), 1),
        "连续手动_均值_min": round(sum(hands[i] for i in keep) / len(keep), 1),
        "总冲突数": sum(conflicts),
        "排程耗时_均值_ms": round(sum(times) / len(times), 2),
        "排程耗时_p95_ms": round(sorted(times)[int(len(times) * 0.95)], 2),
        "排程耗时_max_ms": round(max(times), 2),
    }
    out = os.path.join(ROOT, "data", out_name)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    agent = (sys.argv[2] != "plain") if len(sys.argv) > 2 else True
    run(n=n, agent=agent)
