"""
动态重排仲裁 (L4 模型在环)

为什么需要它
------------
临时加一道菜 / 某台设备故障时, "怎么重排"本身是个决策:
  - 最小扰动: 只把新菜塞进现有空槽, 已排好的菜几乎不动 (用户安心, 但可能不齐)
  - 重新对齐: 允许挪动未开始的步骤, 让所有菜重新同时出锅 (出锅差好看, 但改动大)
  - 保热优先: 死守趁热菜的出锅时刻, 让耐放的菜(汤/炖菜)去让路

纯代码只能三选一写死, 而**选哪个取决于语义**: 新插入的是"10 分钟的快手菜"
还是"要炖 1 小时的汤"? 被影响的菜里有没有必须趁热的清蒸鱼? 这正是模型能
判断、求解器判断不了的地方。

工程约束
--------
在线默认只查缓存/走规则(零延迟); 开启 FT_AGENT_LLM 时才真的问模型,
并受统一超时与熔断保护, 拿不到答案就退回规则姿态, 绝不拖慢重排。
"""
from __future__ import annotations

from app.models.schema import SchedulePlan
from app.agent import llm

# 三种重排姿态 -> 对应的求解策略
STANCES = {
    "minimal": dict(strategy="critical_path", desc="最小扰动, 只填空闲槽"),
    "rebalance": dict(strategy="compress", desc="重新对齐出锅, 允许挪动未开始步骤"),
    "freeze_hot": dict(strategy="serve_hot_last", desc="保住趁热菜, 耐放的菜让路"),
}
DEFAULT_STANCE = "minimal"

_SYS = ("你是厨房排程的仲裁者。烹饪过程中发生了变更, 你要决定以什么姿态重排。"
        "只输出 JSON, 不要解释。")

_USER = """当前排程共 {n} 道菜, 总时长 {mk} 分钟, 出锅差 {gap} 分钟。
已完成(不可改动)的步骤: {done} 步。
变更: {event}

各菜出锅时刻与语义:
{dishes}
{fresh}

可选姿态:
- minimal   : 最小扰动, 只把新任务塞进现有空闲槽 (已排好的菜基本不动)
- rebalance : 重新对齐, 允许挪动尚未开始的步骤, 让所有菜尽量同时出锅
- freeze_hot: 保住必须趁热的菜(趁热分>=0.85)的出锅时刻, 让耐放的菜让路

输出 JSON: {{"stance": "上面三个之一", "reason": "一句话理由(25字内)"}}"""

_RULE_HINT = {
    "insert": "minimal",
    "device_fail": "rebalance",
}


def rule_stance(plan: SchedulePlan, priors: dict | None, event: str) -> tuple[str, str]:
    """规则兜底姿态: 看有没有必须趁热的菜还没出锅。"""
    hot_pending = 0
    for dish, p in (priors or {}).items():
        if getattr(p, "serve_hot", 0) >= 0.85:
            hot_pending += 1
    if hot_pending >= 2 or plan.finish_gap_min > 15:
        return "freeze_hot", "规则: 存在必须趁热的菜, 优先保住出锅时刻"
    return _RULE_HINT.get(event, "minimal"), "规则: 默认最小扰动"


def arbitrate(plan: SchedulePlan, priors: dict | None = None,
              completed_ids: list | None = None, event: str = "insert",
              fresh_dish: str = "", use_llm: bool = False,
              timeout: float = 2.0) -> tuple[str, str, bool]:
    """返回 (stance, reason, 是否来自模型)。任何异常都退化为规则姿态。"""
    completed_ids = completed_ids or []
    if (not use_llm or not llm.enabled() or llm.breaker_open()
            or not llm.online_usable()):
        # online_usable(): 重排仲裁的预算只有 FT_REPLAN_BUDGET(2.5s)，本地模型
        # 同样装不进去 —— 闸门落下时直接走规则姿态，别再白等一次超时。
        st, why = rule_stance(plan, priors, event)
        return st, why, False

    dishes = []
    for dish, fin in sorted((plan.dish_finish or {}).items(), key=lambda kv: kv[1]):
        p = (priors or {}).get(dish)
        hot = f"{p.serve_hot:.2f}" if p else "0.60"
        hold = f"{p.hold_min:.0f}分" if p else "15分"
        dishes.append(f"{dish}: 出锅{fin:.0f}分 趁热{hot} 可放{hold}")
    fresh = f"新插入的菜: {fresh_dish}" if fresh_dish else "无新增菜品"
    user = _USER.format(n=len(plan.dish_finish or {}), mk=plan.makespan_min,
                        gap=plan.finish_gap_min, done=len(completed_ids),
                        event={"insert": "临时加菜", "device_fail": "设备故障"}.get(event, event),
                        dishes="\n".join(dishes) or "(无)", fresh=fresh)
    out = llm.chat_json(_SYS, user, cache_key=None, temperature=0.1, timeout=timeout)
    if isinstance(out, dict) and out.get("stance") in STANCES:
        return out["stance"], str(out.get("reason", ""))[:40], True
    st, why = rule_stance(plan, priors, event)
    return st, why, False


def stance_to_strategy(stance: str) -> str:
    return STANCES.get(stance, STANCES[DEFAULT_STANCE])["strategy"]


def stance_desc(stance: str) -> str:
    return STANCES.get(stance, STANCES[DEFAULT_STANCE])["desc"]
