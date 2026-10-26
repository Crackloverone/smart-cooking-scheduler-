"""L1 解析质量探针：按步骤类别拆分时长覆盖率，并量化"收尾句被误判成设备烹饪步"的虚占。

跑法：python -m scripts.l1_probe
"""
from __future__ import annotations

import os
import re

# 默认离线：L1 探针量的是**规则解析**质量，不是模型。
# 默认 provider 换成本地 Ollama（免密钥）之后 enabled() 默认为 True，
# 而 parse_recipe 会在设备参数缺失时调 enrich（走模型）——
# 那会让"解析覆盖率"变成"这台机器装没装模型"的函数。理由详见 scripts/_offline.py。
for _k in ("FT_LLM", "FT_AGENT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ.setdefault(_k, "off")

from app.parser.recipe_parser import (
    ACTIVE_COOK, DONE_REF, FINISH_RE, HEATING_DEVICES, LOAD_START_VERBS,
    find_device, parse_recipe,
)
from app.scheduler.engine import estimate_duration
from app.spec.adapter import all_names, get

_DUR = re.compile(r"\d+(?:\.\d+)?\s*分钟|\d+(?:\.\d+)?\s*个?小时|\d+(?:\.\d+)?\s*秒")


def _raw_steps(rec) -> list[str]:
    return [s.strip(" ；;。\n")
            for s in re.split(r"第\s*\d+\s*步[：:。]?", rec["steps"])
            if s.strip(" ；;。\n")]


def probe() -> dict:
    names = all_names()
    # ---- 1) 解析后：按类别统计时长覆盖率 ----
    stat = {"设备工作": [0, 0], "人工准备": [0, 0]}   # [总数, 有时长]
    for n in names:
        rec = get(n)
        p = parse_recipe(n, rec["ingredients"], rec["steps"])
        for s in p.steps:
            is_cook = (s.device and s.device != "人工") or s.prep_type == "烹饪"
            k = "设备工作" if is_cook else "人工准备"
            stat[k][0] += 1
            if s.duration_min:
                stat[k][1] += 1

    # ---- 2) 文本里的显式时长（解析器能力上限） ----
    raw_tot = raw_dur = 0
    for n in names:
        for s in _raw_steps(get(n)):
            raw_tot += 1
            if _DUR.search(s):
                raw_dur += 1

    # ---- 3) 收尾句被误判成设备烹饪步（虚占设备） ----
    # 判据分两层，缺一不可：
    #   raw  = 原文层面的潜在命中（含加热设备词 + 收尾语义 + 无时长）
    #   存活 = **解析后**该步的设备仍然是加热设备 —— 这才是真 bug
    #          （解析器命中 is_finish 时会把设备清成"人工"，见 recipe_parser）
    raw_hits, survived = [], []
    for n in names:
        rec = get(n)
        for raw in _raw_steps(rec):
            dev, _ = find_device(raw)
            if dev not in HEATING_DEVICES or _DUR.search(raw):
                continue
            if LOAD_START_VERBS.search(raw) or not FINISH_RE.search(raw):
                continue
            if ACTIVE_COOK.search(raw) and not DONE_REF.search(raw):
                continue
            raw_hits.append((n, dev, raw))
        p = parse_recipe(n, rec["ingredients"], rec["steps"])
        for s in p.steps:
            if s.device not in HEATING_DEVICES or s.duration_min:
                continue
            if not FINISH_RE.search(s.raw_text) or LOAD_START_VERBS.search(s.raw_text):
                continue
            if ACTIVE_COOK.search(s.raw_text) and not DONE_REF.search(s.raw_text):
                continue
            survived.append((n, s.device, s.raw_text, estimate_duration(s)))

    return {"stat": stat, "raw": (raw_tot, raw_dur),
            "raw_hits": raw_hits, "phantom": survived}


if __name__ == "__main__":
    r = probe()
    st = r["stat"]
    for k, (t, d) in st.items():
        pct = round(d / t * 100, 1) if t else 0.0
        note = "（不需要显式时长）" if k == "人工准备" else "（真正要考的口径）"
        print(f"{k}: 总 {t}  有时长 {d}  = {pct}%  {note}")
    rt, rd = r["raw"]
    print(f"\n文本显式时长: {rd}/{rt} = {round(rd/rt*100,1)}%  ← 解析能力的理论上限")
    rh, ph = r["raw_hits"], r["phantom"]
    print(f"\n收尾句潜在命中(原文层): {len(rh)} 条")
    waste = sum(x[3] for x in ph)
    print(f"解析后仍虚占加热设备: {len(ph)} 条, 虚占 {round(waste,1)} 分钟"
          f"   {'✅ 已清零' if not ph else '⚠ 未清零'}")
    by_dev: dict = {}
    for _, dev, _, _ in ph:
        by_dev[dev] = by_dev.get(dev, 0) + 1
    if by_dev:
        print("  按设备:", by_dev)
    for n, dev, s, d in ph[:15]:
        print(f"  [{dev} +{d:.0f}min] {n} | {s[:70]}")
