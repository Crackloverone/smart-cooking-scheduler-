"""诊断: 排程输出里有多少"菜谱原文不存在的步骤"。

判据: 排程任务的 raw_text 必须能在该菜的解析步骤里找到出处;
      找不到的 = 系统自己造的。
用法: python .workbuddy-ai/tmp/step_audit.py [菜名...]
"""
import os, sys, re, collections
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app.spec.adapter import all_names, get as get_recipe, plan_recipes
from app.parser.recipe_parser import parse_recipe

# 已知的"合成"任务特征
SYNTH_PREFIX = ("烟机联动",)


def audit(names):
    tot_raw = tot_parsed = tot_task = 0
    synth = collections.Counter()
    orphan = collections.Counter()
    per_recipe = []
    for name in names:
        rec = get_recipe(name)
        if not rec:
            continue
        n_raw = len(re.findall(r"第\s*\d+\s*步", rec["steps"]))
        rc = parse_recipe(name, rec["ingredients"], rec["steps"])
        plan = plan_recipes([rc], agent=False)
        live = [t for t in plan.tasks if t.start_min >= 0]
        src = {s.raw_text for s in rc.steps}
        n_synth = 0
        for t in live:
            if t.raw_text in SYNTH_PREFIX or t.raw_text.endswith("预热") and t.step_id == 0:
                synth[t.raw_text if t.raw_text in SYNTH_PREFIX else t.raw_text.replace(
                    t.raw_text[:2], "", 1) or t.raw_text] += 1
                n_synth += 1
            elif t.raw_text not in src:
                orphan[t.raw_text[:40]] += 1
        tot_raw += n_raw
        tot_parsed += len(rc.steps)
        tot_task += len(live)
        per_recipe.append((name, n_raw, len(rc.steps), len(live), n_synth))
    return tot_raw, tot_parsed, tot_task, synth, orphan, per_recipe


if __name__ == "__main__":
    names = sys.argv[1:] or all_names()
    raw, parsed, task, synth, orphan, per = audit(names)
    print(f"菜谱数            : {len(per)}")
    print(f"原文「第N步」总数 : {raw}")
    print(f"解析出步骤数      : {parsed}")
    print(f"排程任务数(>=0)   : {task}")
    print()
    print("== 合成任务(原文没有) ==")
    for k, v in synth.most_common():
        print(f"  {k:20s} {v}")
    print(f"  合计 {sum(synth.values())} 个")
    print()
    print("== 找不到出处的任务(前20) ==")
    for k, v in orphan.most_common(20):
        print(f"  [{v:3d}] {k}")
    print(f"  合计 {sum(orphan.values())} 个")
    print()
    print("== 每菜 原文步数 / 解析步数 / 排程任务数 / 合成数 (前25) ==")
    for n, a, b, c, d in per[:25]:
        flag = "  <<<" if d else ""
        print(f"  {n[:14]:16s} {a:3d} / {b:3d} / {c:3d} / {d}{flag}")
