"""
消融实验: 同一批样本上关掉不同模块, 量化每一层的独立贡献。

跑三档:
  A 交付版         Agent 闭环 + 三轨取优 (贪心 + CP-SAT + 精确模型)
  B 关第三轨       FT_EXACT=off   → 只剩 贪心 + CP-SAT
  C 单策略求解器   FT_AGENT=off   → 关掉 Agent 闭环, 退回确定性求解器

三档共用同一 seed / 同一菜名抽样序列 (bench 内部 random.seed(seed)),
所以差异只来自被关掉的模块, 不是样本抖动。

用法: python -m scripts.ablation [组数] [seed]
输出: data/ablation_report.json + 控制台表格
"""
from __future__ import annotations
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.bench import run

# (标签, 环境变量覆盖, 是否 Agent 闭环)
CONFIGS = [
    ("A_交付版", {}, True),
    ("B_关第三轨", {"FT_EXACT": "off"}, True),
    ("C_单策略求解器", {}, False),
]

_KEYS = ["出锅差<5min占比", "出锅差_中位数_min", "出锅差_均值_min",
         "总时长_均值_min", "连续手动_均值_min", "总冲突数",
         "排程耗时_均值_ms", "排程耗时_p95_ms"]


def main(n: int = 50, seed: int = 42) -> dict:
    saved = {k: os.environ.get(k) for k in ("FT_EXACT", "FT_AGENT")}
    results: dict[str, dict] = {}
    try:
        for label, env, agent in CONFIGS:
            for k in ("FT_EXACT", "FT_AGENT"):
                os.environ.pop(k, None)
            os.environ.update(env)
            print(f"\n===== {label} =====")
            results[label] = run(n=n, seed=seed, agent=agent,
                                 out_name=f"_ablation_{label}.json")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 清理中间文件, 只保留汇总
    for label, _, _ in CONFIGS:
        p = os.path.join(ROOT, "data", f"_ablation_{label}.json")
        try:
            os.remove(p)
        except OSError:
            pass

    base = results["A_交付版"]
    rows = []
    for label, _, _ in CONFIGS:
        r = results[label]
        row = {"配置": label}
        row.update({k: r[k] for k in _KEYS})
        if label != "A_交付版":
            for k in ("总时长_均值_min", "连续手动_均值_min"):
                b, v = base[k], r[k]
                row[f"{k}_相对A"] = f"{(v - b) / b * 100:+.1f}%"
        rows.append(row)

    report = {"组数": n, "seed": seed,
              "说明": ("同一 seed 下逐档关模块。A 为交付版; B 关掉精确模型轨; "
                       "C 再关掉 Agent 闭环。相对 A 的百分比 = (本档 - A) / A。"),
              "明细": results, "对比表": rows}
    out = os.path.join(ROOT, "data", "ablation_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n===== 消融汇总 (组数 %d, seed %d) =====" % (n, seed))
    head = ["配置", "出锅差<5min占比", "总时长_均值_min", "连续手动_均值_min",
            "排程耗时_均值_ms"]
    print(" | ".join(head))
    for row in rows:
        print(" | ".join(str(row[h]) for h in head))
    print(f"\n→ {out}")
    return report


if __name__ == "__main__":
    _n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    _s = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    main(_n, _s)
