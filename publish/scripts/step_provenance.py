# -*- coding: utf-8 -*-
"""步骤溯源体检 —— 排程出的每一步都必须能在菜谱原文找到出处, 且原文的每一步都不许消失。

为什么需要它
------------
历史事故 (v2.x): `engine._add_kitchen_semantics()` 会**凭空合成**两类任务塞进时间轴 ——
  · 「烟机联动」45 条: 而官方《接口规范》全文 0 次提到"烟机", 100 道菜谱 0 次提及;
  · 「预热」148 条: 而数据里只有 5 道菜真的需要预热 (「豆沙松糕」还明确写了**无需预热**)。
前端甘特图因此出现"菜谱里根本没有的步骤", 用户一眼就能看出来。

反向事故 (v3.x 实测抓到): 预热合并曾经用 `raw_text.endswith("预热")` 识别预热节点, 而
「豆沙松糕」第 9 步原文是 "...100℃，25分钟，**无需预热**" —— 句尾恰好是"预热"。
一旦它和别的菜的预热区间相邻, 就会被并进"预热共享"行, 于是这条**真实的 25 分钟蒸箱
烹饪步从甘特图上消失**。所以护栏必须**双向**守: 不能多, 也不能少。

三条断言 (都是硬断言)
--------------------
  R1 **有 dish 的任务**: `(step_id, raw_text)` 必须精确落在该菜 `parse_recipe` 产出的
     步骤集合里。多一条 = 凭空步骤。
  R2 **无 dish 的任务 (合成行)**: 只允许"预热共享"这一种, 且
       · raw_text 必须恰好是 `蒸箱预热`/`烤箱预热` (合成节点的精确文本);
       · `dish_list` 非空, 且每个菜都确实出现在本次排程里。
     除此之外的任何无主任务 (含 烟机联动) 一律判违规 —— 因为数据里没有它的出处。
  R3 **完整性**: 每道菜的**真实加热步骤**(灶具/蒸箱/烤箱, 且不是预热节点) 必须仍然在
     计划里。少了 = 真实步骤被合并/删掉了。这条只在**多菜**计划上有效 ——
     预热合并需要同设备同温的 ≥2 条预热, 单菜永远触发不了。

用法
----
    python -m scripts.step_provenance            # 全量 100 道菜 + 多菜组合
    python -m scripts.step_provenance 20         # 只查前 20 道 (快速自检)
退出码 0 = 全绿; 1 = 有违规 (可直接进 CI)。
"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _k in ("FT_AGENT_LLM", "FT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ.setdefault(_k, "off")

from app.models.schema import (DEVICE_OVEN, DEVICE_STEAMER, DEVICE_STOVE)  # noqa: E402
from app.parser.recipe_parser import is_preheat_step, parse_recipe        # noqa: E402
from app.spec.adapter import all_names, get, plan_recipes                 # noqa: E402
from app.scheduler.engine import PREHEAT_NODE_TEXTS                       # noqa: E402

PREHEAT_MARK = "预热"
HEATING = (DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN)


def _parse(name: str):
    return parse_recipe(name, get(name)["ingredients"], get(name)["steps"])


def _check_plan(plan, recs, violations: list[dict], tag: str):
    """对一个计划跑 R1 / R2 / R3。"""
    steps_of = {r.name: {(s.step_id, s.raw_text) for s in r.steps} for r in recs}
    in_pick = {r.name for r in recs}

    for t in plan.tasks:
        if t.start_min < 0:                        # 备菜前置窗口, 不进当天时间轴
            continue
        if t.dish:
            # ---- R1 ----
            if (t.step_id, t.raw_text) not in steps_of.get(t.dish, set()):
                violations.append({
                    "scope": tag, "dish": t.dish, "task_id": t.task_id,
                    "kind": "凭空步骤(R1)",
                    "detail": f"step#{t.step_id} 不在原文: {t.raw_text[:44]}"})
            continue
        # ---- R2: 无 dish 的合成行 ----
        if t.raw_text not in PREHEAT_NODE_TEXTS or t.device not in (DEVICE_OVEN,
                                                                    DEVICE_STEAMER):
            violations.append({
                "scope": tag, "dish": "", "task_id": t.task_id,
                "kind": "非法合成行(R2)",
                "detail": f"device={t.device} text={t.raw_text[:40]}"})
            continue
        if not t.dish_list:
            violations.append({
                "scope": tag, "dish": "", "task_id": t.task_id,
                "kind": "预热共享无主(R2)",
                "detail": "dish_list 为空, 无法说明服务于哪道菜"})
        for d in t.dish_list:
            if d not in in_pick:
                violations.append({
                    "scope": tag, "dish": "", "task_id": t.task_id,
                    "kind": "预热共享串菜(R2)",
                    "detail": f"声称服务未参与排程的菜: {d}"})

    # ---- R3: 完整性 ----
    for r in recs:
        need = {s.step_id for s in r.steps
                if s.device in HEATING and not is_preheat_step(s)}
        got = {t.step_id for t in plan.tasks if t.dish == r.name}
        missing = need - got
        if missing:
            violations.append({
                "scope": tag, "dish": r.name, "task_id": "",
                "kind": "真实步骤消失(R3)",
                "detail": f"这些加热步骤没进计划: {sorted(missing)}"})

    # ---- R4: 依赖不得违反 ----
    # 补回"提前准备"行时最容易踩的坑: 步骤挪到 t<0, 而它的前置还在当天 t>0。
    # 实测这一条会让精解轨被 1e6 惩罚否决, 指标反而从 35 掉到 340。
    by_id = {t.task_id: t for t in plan.tasks}
    for t in plan.tasks:
        for dep in (t.depends_on or []):
            d = by_id.get(dep)
            if d is not None and d.end_min > t.start_min + 1e-6:
                violations.append({
                    "scope": tag, "dish": t.dish or "", "task_id": t.task_id,
                    "kind": "依赖违反(R4)",
                    "detail": f"前置 {dep} 结束 {d.end_min} > 本步开始 {t.start_min}"})


def _preheat_dishes(names: list[str]) -> list[str]:
    """哪些菜解析出了真实的预热节点。"""
    out = []
    for n in names:
        if any(is_preheat_step(s) for s in _parse(n).steps):
            out.append(n)
    return out


def _combos(names: list[str], rng: random.Random) -> list[list[str]]:
    """构造多菜组合 —— 预热合并只有多菜才会发生, 必须专门覆盖。"""
    ph = _preheat_dishes(names)
    combos: list[list[str]] = []
    if ph:
        combos.append(list(ph))                       # 全部预热菜一起 -> 最易触发合并
        for i in range(len(ph)):                      # 两两组合
            for j in range(i + 1, len(ph)):
                combos.append([ph[i], ph[j]])
    combos.append(["豆沙松糕", "油醋汁剁肉饼蒸和乐蟹"])   # 回归: 句尾"无需预热"陷阱
    for _ in range(6):
        k = min(len(names), rng.randint(2, 5))
        if k >= 2:
            combos.append(rng.sample(names, k))
    return combos


def audit(names: list[str] | None = None, with_combos: bool = True) -> dict:
    """返回 {"checked": n, "combos": n, "violations": [...]}。"""
    names = list(names or all_names())
    violations: list[dict] = []

    for name in names:
        _check_plan(plan_recipes([_parse(name)], agent=True), [_parse(name)],
                    violations, tag=f"单菜:{name}")

    combos = []
    if with_combos:
        rng = random.Random(42)
        for pick in _combos(names, rng):
            recs = [_parse(x) for x in pick]
            combos.append(pick)
            _check_plan(plan_recipes(recs, agent=True), recs,
                        violations, tag="多菜:" + "/".join(pick))

    return {"checked": len(names), "combos": len(combos), "violations": violations}


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    names = all_names()[:limit] if limit else None
    res = audit(names)
    v = res["violations"]
    print(f"步骤溯源体检: 单菜 {res['checked']} 道 + 多菜组合 {res['combos']} 组, "
          f"违规 {len(v)} 条")
    if not v:
        print("  ✓ 全绿 —— 排程出的每一步都能在原文找到出处, 原文的每一步也都还在")
        return 0
    from collections import Counter
    for kind, n in Counter(x["kind"] for x in v).most_common():
        print(f"  ✗ {kind}: {n} 条")
    for x in v[:25]:
        print(f"    [{x['scope']}] {x['task_id']} :: {x['detail']}")
    if len(v) > 25:
        print(f"    ... 另有 {len(v) - 25} 条")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
