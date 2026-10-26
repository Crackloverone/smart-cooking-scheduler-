"""
独立自检器: 排程输出前做硬约束校验。
自动评分 65% 考设备冲突与依赖合法性, 这里在返回前再验一遍, 异常直接暴露。
"""
from __future__ import annotations
from collections import defaultdict

from app.models.schema import DEVICE_STEAMER

STEAM_TEMP_TOL = 15.0
PREHEAT_TEMP_TOL = 10.0      # 与 engine.PREHEAT_TEMP_TOL 同口径 (文档 4.1.2)


def _same_temp(a, b, tol: float = STEAM_TEMP_TOL) -> bool:
    ta = getattr(a, "temp_c", None)
    tb = getattr(b, "temp_c", None)
    if ta is None or tb is None:
        return True
    return abs(float(ta) - float(tb)) <= tol


def _is_preheat(t) -> bool:
    return (getattr(t, "raw_text", "") or "").endswith("预热")


def overlap_allowed(a, b) -> bool:
    """两个任务时间上重叠, 但**不该被判成设备冲突**的两种情形:

    1. 蒸箱同温(±15℃): 赛题明确"蒸箱不能同时进行不同温度的蒸制",
       反过来就是同温可以共享腔体 —— 两盘 100℃ 的菜能一炉出。
    2. 同设备同温的**预热节点** (v3.0, 文档 4.1.2):
       "多道菜如果设备预热温度完全一致, 可共享一次预热任务"。
       预热在 v3.0 里已是 L1 拆出来的真实步骤(占设备槽), 共享时区间会重叠,
       若不豁免就会被误判成冲突 —— 那等于逼着系统为同一温度预热两次。
    """
    if _is_preheat(a) and _is_preheat(b) and a.device == b.device:
        return _same_temp(a, b, PREHEAT_TEMP_TOL)
    if getattr(a, "device", None) == DEVICE_STEAMER:
        return _same_temp(a, b)
    return False


def check_plan(plan) -> dict:
    """返回 {conflict, depend_violation, ok, details}"""
    tasks = [t for t in plan.tasks if t.start_min >= 0]
    by_unit = defaultdict(list)
    for t in tasks:
        if t.unit_id in ("等待", "提前准备"):
            continue
        by_unit[t.unit_id].append(t)
    conflicts = []
    for unit, ts in by_unit.items():
        ts = sorted(ts, key=lambda x: x.start_min)
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                if b.start_min >= a.end_min - 0.01:
                    break
                if overlap_allowed(a, b):
                    continue
                conflicts.append(
                    f"{unit}: {a.task_id}({a.start_min}-{a.end_min}) 叠 "
                    f"{b.task_id}({b.start_min}-{b.end_min})")
    # 2) 依赖: 后一步开始 >= 前置结束
    by_id = {t.task_id: t for t in plan.tasks}
    dv = []
    for t in tasks:
        for dep in t.depends_on:
            if dep in by_id and t.start_min < by_id[dep].end_min - 0.01:
                dv.append(f"{t.task_id} 早于前置 {dep}")
    return {
        "ok": not conflicts and not dv,
        "conflict": len(conflicts),
        "depend_violation": len(dv),
        "details": conflicts[:3] + dv[:3],
    }
