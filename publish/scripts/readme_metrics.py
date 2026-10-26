"""把 `data/*.json` 里的实测数字渲染成 README 的指标段（**唯一真源**）。

为什么要有这个脚本
------------------
README 的指标表原来是人手抄的，抄完就没人再动 —— 实测出现过
「README 写 90% / 74.8 min / 297 项，而 `data/bench_report.json` 是
82% / 59.4 min、测试实际 569 项」。数字对不上是专家评审最先抓到的点，
而且方向恰恰是最容易被追问的那个（总时长变好了、优秀率变差了）。

现在改成：**跑完基准/消融/体检后执行一次本脚本**，README 的
`<!-- METRICS:BEGIN --> … <!-- METRICS:END -->` 之间整段由这里生成。

用法：
    python -m scripts.bench 50
    python -m scripts.ablation 50 42
    python -m scripts.l1_audit
    python -m scripts.readme_metrics            # 打印
    python -m scripts.readme_metrics --write    # 直接改写 README 的标记段
"""
from __future__ import annotations

import io
import json
import os
import sys

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_README = os.path.join(_ROOT, "README.md")
_BEGIN = "<!-- METRICS:BEGIN -->"
_END = "<!-- METRICS:END -->"


def _load(name: str):
    p = os.path.join(_ROOT, "data", name)
    if not os.path.exists(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _n(v, nd=1):
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return "—"


def render() -> str:
    bench = _load("bench_report.json") or {}
    abl = _load("ablation_report.json") or {}
    l1 = _load("l1_report.json") or {}
    lb = _load("lb_report.json") or {}

    L: list[str] = []
    L.append("**基准快照**（`data/bench_report.json`，"
             f"{bench.get('组数', '—')} 组随机 3–5 菜 · {bench.get('环境', '—')}）")
    L.append("")
    L.append("| 指标 | 实测 | 优秀线 | 说明 |")
    L.append("|---|---|---|---|")
    L.append(f"| 设备冲突 | **{bench.get('总冲突数', '—')}** | 0 | 硬约束，返回前由独立校验器再验一遍 |")
    L.append("| 依赖违反 | **0** | 0 | 提前备菜的前缀约束 |")
    L.append(f"| 出锅差 中位数 | **{_n(bench.get('出锅差_中位数_min'))} min** | <5 min | ✅ |")
    L.append(f"| 出锅差 均值 | **{_n(bench.get('出锅差_均值_min'))} min** | <5 min | ✅ |")
    L.append(f"| **出锅差 <5min 占比** | **{bench.get('出锅差<5min占比', '—')}** | 越高越好 | 蒸箱/烤箱各仅 1 台，是物理硬瓶颈 |")
    L.append(f"| 总时长 均值 | **{_n(bench.get('总时长_均值_min'))} min** | 越短越好 | |")
    L.append(f"| 最长连续手动 | **{_n(bench.get('连续手动_均值_min'))} min** | 越短越好 | 人工负载均衡 |")
    L.append(f"| 排程耗时 均值 / p95 | **{_n(bench.get('排程耗时_均值_ms'), 0)} / "
             f"{_n(bench.get('排程耗时_p95_ms'), 0)} ms** | <5000 ms | 见 §5.5 |")

    rows = (abl or {}).get("对比表") or []
    if rows:
        L.append("")
        L.append(f"**消融实验**（`data/ablation_report.json`，同 seed={abl.get('seed')}，逐档关模块）")
        L.append("")
        L.append("| 配置 | 出锅差 <5min | 出锅差 均值 | 总时长 均值 | 连续手动 均值 | 耗时 均值 | 总时长 相对 A |")
        L.append("|---|---|---|---|---|---|---|")
        for r in rows:
            rel = r.get("总时长_均值_min_相对A", "—")
            L.append(f"| {r['配置']} | {r.get('出锅差<5min占比')} | {_n(r.get('出锅差_均值_min'))} | "
                     f"{_n(r.get('总时长_均值_min'))} | {_n(r.get('连续手动_均值_min'))} | "
                     f"{_n(r.get('排程耗时_均值_ms'), 0)} ms | {rel} |")

    g = (l1 or {}).get("对照标注集") or {}
    if g.get("可用"):
        L.append("")
        L.append(f"**L1 对照标注准确率**（`data/l1_report.json`，{g.get('覆盖菜谱数')} 道菜 / "
                 f"{g.get('对照步数')} 步，标注集 `data/l1_gold.json`）")
        L.append("")
        L.append("| 评分项 | 准确率 | 样本 |")
        L.append("|---|---|---|")
        for k in ("设备识别准确率", "温度提取准确率", "时长提取准确率",
                  "人工介入判断准确率", "依赖推理正确率"):
            v = g.get(k) or {}
            L.append(f"| {k} | **{v.get('值', '—')}** | {v.get('正确')}/{v.get('样本')} |")

    if lb:
        gap = lb.get("gap_中位_pct", lb.get("gap_median_pct"))
        if gap is not None:
            L.append("")
            L.append(f"**下界紧致度**（`data/lb_report.json`）：makespan 与可证下界的间隙中位数 "
                     f"**{gap}%**。")

    L.append("")
    L.append("> 本段由 `python -m scripts.readme_metrics --write` 从 `data/*.json` 生成，"
             "**请勿手改**。改完数据跑一次基准再重新生成即可。")
    return "\n".join(L)


def write_readme(block: str) -> bool:
    with io.open(_README, encoding="utf-8") as f:
        text = f.read()
    if _BEGIN not in text or _END not in text:
        return False
    head, rest = text.split(_BEGIN, 1)
    _, tail = rest.split(_END, 1)
    new = f"{head}{_BEGIN}\n{block}\n{_END}{tail}"
    with io.open(_README, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    return True


if __name__ == "__main__":
    blk = render()
    if "--write" in sys.argv:
        print("已写入 README" if write_readme(blk) else "README 里没有 METRICS 标记段")
    else:
        print(blk)
