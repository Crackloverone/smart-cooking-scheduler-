"""L4 两级冻结动态重排 + KPI demo

菜谱来源与 /api/plan 完全一致 (官方菜库), 不再读已不存在的 recipes_sample_2000.csv。
"""
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("FT_AGENT_LLM", "off")
from app.parser.recipe_parser import parse_recipe
from app.scheduler.engine import schedule, reschedule
from app.scheduler.metrics import evaluate, perturbation
from app.notifier.notifications import generate_notifications
from app.spec.adapter import get, all_names


def R(name):
    if name not in all_names():
        raise SystemExit(f"菜库里没有「{name}」")
    r = get(name)
    return parse_recipe(name, r["ingredients"], r["steps"])


base = ["家常鲈鱼", "虾米豆豉蒸排骨", "腊味蒸素鸡"]
recs = [R(n) for n in base]
plan1 = schedule(recs)
k1 = evaluate(plan1)
print(f"【初始排程】总时长 {plan1.makespan_min}min, 出锅差 {plan1.finish_gap_min}min, 冲突{k1.conflict_count}")

# 已完成: 各菜前2步 (硬冻结, 时间不动)
fixed = {}
for t in plan1.tasks:
    if t.step_id <= 2:
        fixed[(t.dish, t.step_id)] = t
print(f"硬冻结已完成步骤: {len(fixed)} 步")

# 临时加一道菜 (排骨汤 -> 用现有菜模拟)
new_soup = R("竹荪排骨汤")
plan2 = reschedule(recs, fixed, new_dish=new_soup)
k2 = evaluate(plan2)
moved = perturbation(plan1, plan2)
print(f"\n【重排后】总时长 {plan2.makespan_min}min, 出锅差 {plan2.finish_gap_min}min")
print(f"冲突 {k2.conflict_count}, 依赖违反 {k2.depend_violation}, 设备利用率 {k2.device_utilization}%")
# 口径: 已用产能 / 可用产能 = 各腔体占用并集 / (腔体数 x makespan)。
# 主指标只算"把菜做熟"的 灶具2+蒸箱1+烤箱1; 明细里额外给冰箱/烟机, 便于定位空闲。
for _u, _v in sorted(k2.util_detail.items()):
    print(f"    {_u:<14} 占用 {_v:5.1f}%")
print(f"最小扰动: 被挪动任务 {moved} 个, 硬冻结 {len(fixed)} 步保持不动")
print(f"新出锅: {plan2.dish_finish}")

print("\n【通知样例】")
for n in generate_notifications(plan2)[:6]:
    print(f"  t={n['at_min']:6.1f} {n['text']}")
