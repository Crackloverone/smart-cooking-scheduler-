# -*- coding: utf-8 -*-
"""数据审计: 100 道菜谱里到底写了什么设备/模式/预热/上桌语。

只读, 不改任何东西。用于给"删除凭空步骤"提供数据依据。
"""
import csv
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.parser.recipe_parser import parse_recipe, split_steps, expand_compound  # noqa: E402

CSV = os.path.join(os.path.dirname(__file__), "..", "..", "data", "recipes_official_100.csv")

rows = []
with open(CSV, encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        rows.append(r)

print(f"菜谱条数: {len(rows)}")

# ---------- 1. 设备词命中 ----------
DEV_WORDS = {
    "烟机": ["烟机", "油烟机", "排烟"],
    "蒸箱": ["蒸箱", "蒸烤", "蒸笼", "电蒸"],
    "烤箱": ["烤箱", "烤炉"],
    "冰箱": ["冰箱", "冷藏", "冷冻", "解冻"],
    "洗碗机": ["洗碗机"],
    "净水/净咖": ["净咖", "净水机", "净水器", "清水机", "纯净水", "净水"],
    "灶具": ["灶具", "燃气灶", "灶台", "炒锅", "汤锅", "平底锅", "主锅", "铁锅"],
}
dev_cnt = Counter()
dev_sent = defaultdict(list)
for r in rows:
    for stext in split_steps(r["烹饪步骤"]):
        for seg in expand_compound(stext):
            for dev, ws in DEV_WORDS.items():
                for w in ws:
                    if w in seg:
                        dev_cnt[dev] += 1
                        dev_sent[dev].append((r["名称"], w, seg))
                        break
print("\n=== 设备词在步骤中的命中次数 ===")
for k, v in dev_cnt.most_common():
    print(f"  {k:10s} {v}")

# ---------- 2. 预热 ----------
print("\n=== 提到「预热」的句子 ===")
preheat_dishes = set()
neg_dishes = set()
for r in rows:
    for stext in split_steps(r["烹饪步骤"]):
        for seg in expand_compound(stext):
            if "预热" in seg:
                preheat_dishes.add(r["名称"])
                neg = bool(re.search(r"无需预热|不用预热|不必预热|免预热|不需要预热|无需提前预热", seg))
                if neg:
                    neg_dishes.add(r["名称"])
                print(f"  [{'否定' if neg else '正用'}] {r['名称']}: {seg[:110]}")
print(f"  -> 提到预热的菜 {len(preheat_dishes)} 道: {sorted(preheat_dishes)}")
print(f"  -> 其中明确「无需预热」的菜 {len(neg_dishes)} 道: {sorted(neg_dishes)}")

# ---------- 3. 官方模式名 ----------
MODES = ["普通蒸", "鲜嫩蒸", "脱脂蒸", "多层蒸",
         "上下火", "全开烤", "鼓风烤", "顶部烤", "加湿烤", "环风烤", "脱脂烤", "空气炸", "蔬果干",
         "日常洗", "母婴洗", "超净洗", "冷藏", "冷冻", "解冻"]
mode_cnt = Counter()
for r in rows:
    for stext in split_steps(r["烹饪步骤"]):
        for m in MODES:
            if m in stext:
                mode_cnt[m] += 1
print("\n=== 菜谱文本里出现的官方模式名 ===")
for k, v in mode_cnt.most_common():
    print(f"  {k:8s} {v}")
if not mode_cnt:
    print("  (无)")

# ---------- 4. 上桌语 ----------
SERVE = re.compile(r"尽情享用|即可享用|即可食用|开吃|趁热享用|就做好了|可以享用了|享用吧")
print("\n=== 纯上桌语步骤 (末步) ===")
n_serve = 0
serve_last = 0
for r in rows:
    segs = []
    for stext in split_steps(r["烹饪步骤"]):
        segs.extend(expand_compound(stext))
    for i, seg in enumerate(segs):
        if SERVE.search(seg):
            n_serve += 1
            if i == len(segs) - 1:
                serve_last += 1
            print(f"  {r['名称']} [第{i+1}/{len(segs)}步]: {seg[:80]}")
print(f"  -> 共 {n_serve} 条, 其中 {serve_last} 条是末步")

# ---------- 5. 解析结果: 设备分布 / 合成任务需求 ----------
print("\n=== 解析后设备分布 (762 步口径) ===")
dist = Counter()
steps_total = 0
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        steps_total += 1
        dist[s.device] += 1
print(f"  总步骤 {steps_total}")
for k, v in dist.most_common():
    print(f"  {k:10s} {v}")

print("\n=== 解析后模式分布 (非空) ===")
mdist = Counter()
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        if s.mode:
            mdist[(s.device, s.mode)] += 1
for k, v in mdist.most_common():
    print(f"  {k} {v}")

print("\n=== 解析后温度分布 (按设备) ===")
tdist = defaultdict(Counter)
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        if s.temperature_c is not None:
            tdist[s.device][s.temperature_c] += 1
for dev, c in tdist.items():
    print(f"  {dev}: {sorted(c.items())}")

print("\n=== 解析出的步骤里含「纯净水/净水」的 ===")
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        if "净水" in s.raw_text or "纯净水" in s.raw_text:
            print(f"  {r['名称']} dev={s.device} src={s.device_source} | {s.raw_text[:80]}")
