# -*- coding: utf-8 -*-
"""第二轮审计: 温度越界来源 / 净水误判 / 上桌语中的真实动作。"""
import csv
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from app.parser.recipe_parser import parse_recipe  # noqa: E402

CSV = os.path.join(os.path.dirname(__file__), "..", "..", "data", "recipes_official_100.csv")
rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))

RANGE = {"蒸箱": (85.0, 120.0), "烤箱": (60.0, 230.0)}

print("=== 温度越界的步骤 ===")
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        if s.temperature_c is None or s.device not in RANGE:
            continue
        lo, hi = RANGE[s.device]
        if not (lo <= s.temperature_c <= hi):
            print(f"  {r['名称']:12s} {s.device} {s.temperature_c}℃ mode={s.mode!r}")
            print(f"      | {s.raw_text[:130]}")

print("\n=== 设备=净水机 的全部步骤 ===")
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        if s.device == "净水机":
            print(f"  {r['名称']:12s} dur={s.duration_min} type={s.prep_type} mode={s.mode!r}")
            print(f"      | {s.raw_text[:130]}")

print("\n=== 灶具温度 180℃ 的来源 ===")
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        if s.device == "灶具" and s.temperature_c:
            print(f"  {r['名称']:12s} {s.temperature_c}℃ | {s.raw_text[:130]}")

print("\n=== 解析出时长 None 但设备是加热设备的步骤 (会被 estimate_duration 兜底) ===")
n = 0
for r in rows:
    rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"])
    for s in rec.steps:
        if s.device in ("蒸箱", "烤箱", "灶具") and s.duration_min is None:
            n += 1
            if n <= 15:
                print(f"  {r['名称']:12s} {s.device} mode={s.mode!r} | {s.raw_text[:100]}")
print(f"  -> 共 {n} 条")

print("\n=== 提到「洗碗机 / 烟机 / 净咖」的步骤 ===")
hit = 0
for r in rows:
    for kw in ("洗碗机", "烟机", "油烟机", "排烟", "净咖", "净水机", "净水器"):
        if kw in r["烹饪步骤"]:
            hit += 1
            print(f"  {r['名称']}: {kw}")
print(f"  -> 共 {hit} 条")
