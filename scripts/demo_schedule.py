"""端到端 demo: 选菜 -> 解析 -> 调度 -> 冲突校验 -> 打印甘特

用法: python -m scripts.demo_schedule [菜名 ...]

数据源统一走 app.spec.adapter (官方 100 道菜库, data/recipes_official_100.csv) ——
与 /api/plan、/schedule 完全同源。
原实现读 recipes_sample_2000.csv (gbk), 该文件在仓库里不存在, 一跑就 FileNotFoundError;
后来改成读 recipes_full_2000.json 也不行: 那份数据的步骤是**无"第N步"标记的散句**,
解析器按"第N步"切分, 于是每道菜只解析出 1 步, 排程结果毫无意义。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts._offline import force_offline_default
force_offline_default()      # demo 量的是排程器，不是模型：默认离线（见 _offline.py）

from app.parser.recipe_parser import parse_recipe
from app.spec.adapter import all_names, get as get_recipe, plan_recipes


def load(pick: list[str]):
    """从官方菜库取菜谱并解析成 Recipe。"""
    recipes = []
    for name in pick:
        rec = get_recipe(name)
        if not rec:
            print(f"  !! 菜名不在官方菜库, 已跳过: {name}")
            continue
        recipes.append(parse_recipe(name, rec["ingredients"], rec["steps"]))
    return recipes


def check(plan) -> list[str]:
    """冲突/依赖自检 (与 app.scheduler.checker 同口径的最小实现)。"""
    errors = []
    by_unit = {}
    for t in plan.tasks:
        if t.unit_id in ("等待", "提前准备"):   # 非独占资源
            continue
        by_unit.setdefault(t.unit_id, []).append(t)
    for unit, ts in by_unit.items():
        ts = sorted(ts, key=lambda x: x.start_min)
        for a, b in zip(ts, ts[1:]):
            if b.start_min < a.end_min - 1e-6:
                errors.append(f"冲突: {unit} 上 {a.task_id}[{a.start_min},{a.end_min}] "
                              f"与 {b.task_id}[{b.start_min},{b.end_min}]")
    by_id = {t.task_id: t for t in plan.tasks}
    for t in plan.tasks:
        for dep in t.depends_on:
            if dep in by_id and by_id[dep].end_min > t.start_min + 1e-6:
                errors.append(f"依赖违反: {t.task_id} 在 {t.start_min} 开始, "
                              f"但前置 {dep} 到 {by_id[dep].end_min}")
    return errors


def main(pick: list[str]):
    recipes = load(pick)
    if not recipes:
        print("没有可排程的菜, 请检查菜名是否在官方菜库中。")
        return
    # 走**线上同一条**闭环 (策略择优 + CP-SAT + 精确模型三轨取最优),
    # 而不是 engine.schedule() 的单策略兜底 —— 否则 demo 演示的是最弱的那条路径。
    t0 = time.time()
    plan = plan_recipes(recipes)
    ms = (time.time() - t0) * 1000
    errors = check(plan)

    print(f"选菜: {[r.name for r in recipes]}")
    print(f"总时长: {plan.makespan_min} 分钟 | 出锅时间差: {plan.finish_gap_min} 分钟"
          f" | 求解 {ms:.0f} ms")
    print(f"各菜出锅: {plan.dish_finish}")
    print(f"冲突/依赖错误: {len(errors)}")
    for e in errors:
        print("  !!", e)
    print("=" * 64)
    for t in sorted(plan.tasks, key=lambda x: (x.start_min, x.dish)):
        bar = "█" * int(t.duration_min)
        tag = "无人值守" if t.unattended else "人工"
        print(f"  t={t.start_min:6.1f}-{t.end_min:6.1f} [{t.unit_id:10}] "
              f"{t.dish[:10]:10}#{t.step_id} {tag} {bar}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        pick = sys.argv[1:]
    else:
        # 默认取官方菜库前 4 道, 保证 demo 一定能跑通
        pick = all_names()[:4]
    main(pick)
