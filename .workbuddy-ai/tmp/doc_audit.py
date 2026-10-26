# -*- coding: utf-8 -*-
"""对照《核心调度算法设计文档》逐条实测: 哪些已落地, 哪些还是缺口。

只读, 不改任何东西。
"""
import csv
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.parser.recipe_parser import parse_recipe  # noqa: E402
from app.spec import device_spec as _ds  # noqa: E402

CSV = os.path.join(os.path.dirname(__file__), "..", "..", "data", "recipes_official_100.csv")
rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))

print("=" * 70)
print("A. L1 设备分布 + 参数合法性")
print("=" * 70)
dist = Counter()
bad_temp, bad_mode, bad_dur = [], [], []
n_steps = 0
ph_steps = 0
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        n_steps += 1
        dist[s.device] += 1
        if s.device_source == "预热":
            ph_steps += 1
        if s.device in _ds.DEVICE_SPEC:
            if not _ds.is_legal(s.device, s.mode, s.temperature_c, s.duration_min):
                bad_mode.append((r["名称"], s.step_id, s.device, s.mode,
                                 s.temperature_c, s.duration_min, s.raw_text[:60]))
print(f"总步骤 {n_steps} (含拆出的预热 {ph_steps} 条)")
for k, v in dist.most_common():
    print(f"  {k:10s} {v}")
print(f"\n参数非法步骤: 模式非法/越界 {len(bad_mode)} 条")
for b in bad_mode[:20]:
    print("   ", b)

print()
print("=" * 70)
print("B. 预热来源 (文档 3.1.4 / 4.1)")
print("=" * 70)
ph_by_dish = defaultdict(int)
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        if s.device_source == "预热":
            ph_by_dish[r["名称"]] += 1
print(f"产出预热的菜 {len(ph_by_dish)} 道: {dict(ph_by_dish)}")
print("提到预热的菜 (数据): 半熟芝士蛋糕/草莓蛋挞/油醋汁剁肉饼蒸和乐蟹/烹香酷炒汇/蔬菜塔/豆沙松糕")

print()
print("=" * 70)
print("C. 净水/净咖 误判检查 (纯净水是食材)")
print("=" * 70)
n_water = 0
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        if s.device == "净水机":
            n_water += 1
            print(f"  {r['名称']:12s} src={s.device_source!r} | {s.raw_text[:80]}")
print(f"  -> 共 {n_water} 条")

print()
print("=" * 70)
print("D. 端到端: 3 菜计划的 detailTimeline 溯源")
print("=" * 70)
from app.spec.adapter import build_plan, get  # noqa: E402
from app.spec.adapter import last_plan  # noqa: E402

names = ["鸡翅包饭", "豉汁蒸草鱼", "苹果芒果派"]
items = [{"id": get(n)["id"], "name": n} for n in names]
resp = build_plan(items)
print(f"detailTimeline 行数: {len(resp['detailTimeline'])}")
for row in resp["detailTimeline"]:
    print(f"  {row['timeInterval']:>9s} type={row['type']} {row['title']:6s} "
          f"| {row['list'][0][:52]}")

plan = last_plan()
print("\n排程任务 (含合成):")
for t in sorted(plan.tasks, key=lambda x: x.start_min):
    tag = "合成" if (t.step_id == 0 and not t.dish) else "真实"
    print(f"  [{tag}] {t.start_min:7.1f}-{t.end_min:7.1f} {t.unit_id:14s} "
          f"{t.dish}#{t.step_id} | {t.raw_text[:46]}")
print(f"\nmakespan={plan.makespan_min} gap={plan.finish_gap_min}")
print(f"overview={resp['overview']}")

print()
print("=" * 70)
print("E. 文档 5.1 六条功能断言 (自查)")
print("=" * 70)
tl = resp["detailTimeline"]
# 1 排序
keys = [(r["type"], int(r["timeInterval"].split("-")[0])) for r in tl]
print(f"1) detailTimeline 排序正确: {keys == sorted(keys)}")
# 2 设备独占
from app.scheduler.checker import check  # noqa: E402
try:
    conf = check(plan)
    print(f"2) 设备冲突: {conf}")
except Exception as e:
    print(f"2) checker 调用异常: {e}")
# 3 timeSave >= 0
print(f"3) timeSave={resp['overview']['timeSave']} (>=0: "
      f"{int(resp['overview']['timeSave']) >= 0})")
# 4 菜名一致
print(f"4) 菜名一致: {[c['name'] for c in resp['cookingTimeline']] == names}")
# 5 参数合法
illegal = [r for r in tl if r.get("parameters") and any(
    (isinstance(p.get("temperature"), (int, float)) and p["temperature"] < -30) or
    (isinstance(p.get("temperature"), (int, float)) and p["temperature"] > 300)
    for p in r["parameters"])]
print(f"5) parameters 明显越界行: {len(illegal)}")
# 6 finishTime >= 每道菜 endTime
ft = resp["overview"]["finishTime"]
ends = [c["endTime"] for c in resp["cookingTimeline"]]
print(f"6) finishTime={ft} vs ends={ends}: {all(ft >= e for e in ends)}")
