"""执行态回写 + 偏差判定 (v3.9)。

★ 为什么要有这个文件

    `data/schedules_user.json` 存的是**计划**, 不是**执行结果**。系统排完程、发完通知
    就失明了 —— 它不知道用户第 3 步到底做了没有、有没有晚。于是「动态事件·重排」
    只能靠用户在下拉里**手动**选插单 / 故障 / 延迟。
    本模块补的是 Agent 闭环里唯一缺失的那一跳: 让"实际发生了什么"回到系统里。

设计取舍（改动前必读）

    1. **只回写, 不擅自改计划。** 偏差达到阈值只给**建议**, 用户点确认才真的重排。
       与 v3.8 的确认纪律同源: 能回退的给撤销, 回不去的给确认。自动改计划会让用户
       在不知情的情况下被换掉一份已经在执行的方案 —— 那比重排本身更破坏信任。
    2. **触发阈值用保守量, 最终收益靠实测。** 累计延迟取「最大单步超出」而不是求和
       —— 并行分支上的延迟**不叠加**, 求和会高估, 于是 3 道菜各晚 1 分钟就被判成
       晚 3 分钟, 触发一次毫无必要的重排。取最大值宁可**低估**(漏一次建议)也不要
       **高估**(噪音重排)。而"重排到底有没有更好"交给 `/reschedule` 已有的 `pareto`
       字段诚实汇报, 本模块**不做预测** —— 预测错了比不预测更糟。
    3. **原子写 + 损坏留证。** 半截 JSON 会让下次启动静默丢整份; 损坏的文件改名成
       `.bad` 留证而不是删掉 ("我的记录全没了"这种事必须留下痕迹)。与
       `llm_settings` / `schedules_user.json` 同一条纪律。
    4. 落盘到 `data/exec_user.json` —— **第五份用户层**, 必须进 `SKIP_NAMES`。

数据形状::

    {
      "version": 1,
      "runs": {"<sid>": {"sid","ts","dishes","makespan",
                         "steps": {"菜名#N": {"state","planned","actual","at"}}}},
      "samples": {"<bucket>": {"n":12, "sum_planned":96.0, "sum_actual":132.0}}
    }

    `samples` 是给 `calib.py` 做经验回流用的聚合量 —— 只存**和**, 不存逐条明细,
    这样文件不会随使用次数无限增长。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

_VERSION = 1

# ⚠ 隔离开关: 与 FT_DEVICES_FILE / FT_SCHEDULES_FILE 同一套纪律。
#   没有它, 跑一次测试就会往开发机真文件里塞一堆 "1-0-0" 之类的执行记录。
_PATH = os.environ.get("FT_EXEC_FILE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "exec_user.json")

# 建议重排的最小延迟(分钟)。太小 ⇒ 每次手抖都弹建议, 用户立刻学会无视它。
REPLAN_MIN_DELTA = float(os.environ.get("FT_EXEC_REPLAN_MIN", "3.0"))

_MAX_RUNS = 30          # 只保留最近 30 次排程的执行态


def _empty() -> dict:
    return {"version": _VERSION, "runs": {}, "samples": {}}


def _load() -> dict:
    if not os.path.exists(_PATH):
        return _empty()
    try:
        with open(_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or "runs" not in d:
            return _empty()
        d.setdefault("samples", {})
        return d
    except Exception:
        # 与 schedules_user.json 同一条纪律: 坏文件改名留证, 不静默删除
        try:
            os.replace(_PATH, _PATH + ".bad")
        except Exception:
            pass
        return _empty()


def _write(d: dict) -> None:
    """原子写。半截 JSON ⇒ 下次启动静默丢整份执行态。"""
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _PATH)
    except Exception:
        # 落盘失败不该让"记录一步执行"这个动作失败
        pass


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def report(sid: str, task_id: str, state: str, *,
           planned: Optional[float] = None, actual: Optional[float] = None,
           bucket: str = "", dishes: Optional[list] = None,
           makespan: Optional[float] = None,
           replace_samples: bool = True) -> dict:
    """回写一步执行态, 返回该次排程的**当前**执行态快照。

    `state` ∈ `done` | `delayed` | `skip`。
    ⚠ `actual` 为空时按 `planned` 记(准时完成) —— 不猜、不填 0,
      否则"用户没填实际时长"会被当成"这步耗时 0 分钟", 直接把校准系数拉塌。
    """
    d = _load()
    run = d["runs"].setdefault(sid, {
        "sid": sid, "ts": _now(), "dishes": dishes or [],
        "makespan": makespan, "steps": {},
    })
    if dishes:
        run["dishes"] = dishes
    if makespan is not None:
        run["makespan"] = makespan

    prev = run["steps"].get(task_id) or {}
    # ⚠ 样本是**增量**累加的; 同一步重复上报时必须先扣掉上一次的贡献,
    #   否则同一步点 5 次「完成」就变成 5 个样本, 校准系数被这一条带跑。
    _sample_add(d["samples"], prev.get("bucket") or bucket,
                prev.get("planned"), prev.get("actual"), -1)

    p = float(planned) if planned is not None else None
    a = float(actual) if (actual is not None and actual != "") else None
    if state == "done" and a is None:
        a = p                      # 没报实际时长 ⇒ 按准时算
    rec = {"state": state, "planned": p, "actual": a, "at": _now()}
    if bucket:
        rec["bucket"] = bucket
    run["steps"][task_id] = rec

    if replace_samples and p is not None and a is not None and bucket:
        _sample_add(d["samples"], bucket, p, a, +1)

    # 只保留最近 _MAX_RUNS 次, 防止文件无限增长
    if len(d["runs"]) > _MAX_RUNS:
        for k in sorted(d["runs"], key=lambda k: d["runs"][k].get("ts", ""))[
                : len(d["runs"]) - _MAX_RUNS]:
            d["runs"].pop(k, None)

    _write(d)
    return run


def _sample_add(samples: dict, bucket: str, planned, actual, sign: int) -> None:
    if not bucket or planned is None or actual is None:
        return
    s = samples.setdefault(bucket, {"n": 0, "sum_planned": 0.0, "sum_actual": 0.0})
    s["n"] += sign
    s["sum_planned"] += sign * float(planned)
    s["sum_actual"] += sign * float(actual)
    if s["n"] <= 0:
        samples.pop(bucket, None)


def get(sid: str) -> dict:
    """取一次排程的执行态。没有记录 ⇒ 返回空快照(不是 404) —— 没开始执行是正常状态。"""
    d = _load()
    run = d["runs"].get(sid)
    return run or {"sid": sid, "ts": "", "dishes": [], "makespan": None, "steps": {}}


def samples() -> Dict[str, dict]:
    return _load().get("samples") or {}


def deviation(sid: str) -> dict:
    """偏差评估: 值不值得重排。

    ⚠ 累计延迟取**最大单步超出量**, 不是求和 —— 并行分支上的延迟不叠加。
      代价是串行多步延迟会被低估, 但这只影响"要不要弹建议"(漏一次),
      不影响正确性; 而"重排到底有没有更好"由 `/reschedule` 的 pareto 实测判定。
    """
    run = get(sid)
    steps = run.get("steps") or {}
    done = [k for k, v in steps.items() if v.get("state") == "done"]
    delayed = []
    worst = 0.0
    for k, v in steps.items():
        if v.get("state") != "delayed":
            continue
        p, a = v.get("planned"), v.get("actual")
        over = (a - p) if (p is not None and a is not None) else 0.0
        if over > 0:
            delayed.append({"task_id": k, "over": round(over, 1)})
            worst = max(worst, over)
    delta = round(worst, 1)
    return {
        "sid": sid,
        "delta_min": delta,
        "threshold_min": REPLAN_MIN_DELTA,
        "worth": delta >= REPLAN_MIN_DELTA,
        "done": len(done),
        "total": len(steps),
        "delayed": delayed,
        "reason": _reason(delta, delayed),
    }


def _reason(delta: float, delayed: list) -> str:
    if not delayed:
        return "目前没有记录到延迟。"
    if delta < REPLAN_MIN_DELTA:
        return (f"最慢的一步晚了 {delta} 分钟, 还没到 {REPLAN_MIN_DELTA} 分钟的"
                f"重排门槛 —— 按原计划继续更稳。")
    who = "、".join(x["task_id"] for x in delayed[:3])
    return f"{who} 共晚了约 {delta} 分钟, 后面的步骤会被整体推后。"


def clear(sid: str = "") -> int:
    """清空执行态。`sid` 为空则全清。返回被清掉的次数。"""
    d = _load()
    if sid:
        n = 1 if sid in d["runs"] else 0
        d["runs"].pop(sid, None)
    else:
        n = len(d["runs"])
        d = _empty()
    _write(d)
    return n
