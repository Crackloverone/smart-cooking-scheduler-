"""
批量解析 recipes_sample_2000.csv 并输出统计报告。
用法: python scripts/batch_parse.py
"""
import csv, sys, os, collections, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts._offline import force_offline_default
force_offline_default()      # 批量解析量的是 L1 规则，不是模型：默认离线（见 _offline.py）

from app.parser.recipe_parser import parse_recipe, DEVICE_NONE

CSV_PATH = os.path.join(ROOT, "recipes_sample_2000.csv")


def main():
    rows = []
    with open(CSV_PATH, encoding="gbk") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    n_recipes = len(rows)
    n_steps = 0
    dev_counter = collections.Counter()
    has_dur = 0
    has_temp = 0
    n_human = 0
    n_unattended = 0
    n_prep = 0
    parsed_recipes = []

    for r in rows:
        rec = parse_recipe(r["名称"], r["食材清单"], r["烹饪步骤"], r.get("label", ""))
        parsed_recipes.append(rec.to_dict())
        for s in rec.steps:
            n_steps += 1
            dev_counter[s.device] += 1
            if s.duration_min is not None: has_dur += 1
            if s.temperature_c is not None: has_temp += 1
            if s.needs_human: n_human += 1
            if s.unattended: n_unattended += 1
            if s.prep_type == "切配": n_prep += 1

    print("=" * 50)
    print(f"菜谱总数: {n_recipes}")
    print(f"步骤总数: {n_steps}  (平均 {n_steps/n_recipes:.1f} 步/菜)")
    print("-" * 50)
    print(f"成功抽到时長的步骤: {has_dur} ({has_dur/n_steps*100:.1f}%)")
    print(f"成功抽到温度的步骤: {has_temp} ({has_temp/n_steps*100:.1f}%)")
    print(f"判定为人工操作:     {n_human} ({n_human/n_steps*100:.1f}%)")
    print(f"判定为无人值守:     {n_unattended} ({n_unattended/n_steps*100:.1f}%)")
    print(f"切配类步骤:         {n_prep} ({n_prep/n_steps*100:.1f}%)")
    print("-" * 50)
    print("设备分布:")
    for dev, c in dev_counter.most_common():
        print(f"  {dev:6s}: {c:5d}  ({c/n_steps*100:5.1f}%)")

    # 保存一份解析结果样例
    out = os.path.join(ROOT, "output", "parsed_sample.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(parsed_recipes[:20], f, ensure_ascii=False, indent=2)
    print("-" * 50)
    print(f"已写出前20道菜解析样例 -> {out}")


if __name__ == "__main__":
    main()
