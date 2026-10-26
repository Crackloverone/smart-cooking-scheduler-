"""经验回流: 用真实执行数据校准"没写时长"那部分步骤的估计值 (v3.9)。

★ 为什么要有这个文件

    `engine.estimate_duration` 对**原文没写时长**的步骤给的是设备档位兜底值
    (蒸烤 8 分钟 / 灶具 8 或 3 / 切配 2 / 收尾 1 ...)。官方菜谱里 87% 的设备步骤
    原文就不写时长, 所以**整个排程的精度大半压在这几个常数上**。
    而"上一轮这道菜实际花了多久"系统原来完全不知道 —— 第 100 次排程和第 1 次
    一模一样地从零算。本模块把执行态里攒下的 (预估, 实际) 对聚合成修正系数,
    让兜底值随真实经验漂移。

⚠⚠ 三条硬纪律（改这个文件前必读）

    1. **默认关闭。** `FT_CALIB=off`（缺省）。校准会改时长, 而时长直接决定
       (Γ, T) 权衡 —— 开档必须先跑 A/B 证明 **0 组更差** 才敢留。
       关档时 `apply()` 第一行就返回原值, 与旧行为**逐位一致**, 基线零风险。
    2. **只校准"没写时长"的步骤。** 原文写了"蒸 15 分钟"是事实, 不是估计,
       再准的统计也不能盖掉它。这条由 `engine.estimate_duration` 的结构保证:
       校准只在 `step.duration_min is None` 的分支里被调用。
    3. **系数必须 clamp + 样本门限。** 单次异常值(切菜时接了个电话)不该把整个
       档位抬走 ⇒ clamp 到 [0.5, 2.0]; 样本少于 `FT_CALIB_MIN_N`（默认 5）
       直接返回 1.0 —— 宁可不学, 也不从一个样本里学。

系数定义::

    ratio(bucket) = Σ实际 / Σ预估        (按和算, 不按比值平均 ——
                                          后者会让"1 分钟的步"和"30 分钟的步"等权)
    apply(bucket, base) = clamp(base * ratio, base*0.5, base*2.0)
"""

from __future__ import annotations

import os
import time
from typing import Dict, Optional

# ⚠ 默认 off —— 见文件头纪律 1。
_ENABLED = os.environ.get("FT_CALIB", "off").lower() not in (
    "off", "0", "false", "no", "")

_MIN_N = int(os.environ.get("FT_CALIB_MIN_N", "5"))
_LO, _HI = 0.5, 2.0
_TTL = 60.0          # 系数缓存秒数: 一次排程内不重复读盘


def enabled() -> bool:
    return _ENABLED


def reload_flag() -> None:
    """测试用: 让模块重新读一遍 FT_CALIB（改环境变量后调它）。"""
    global _ENABLED
    _ENABLED = os.environ.get("FT_CALIB", "off").lower() not in (
        "off", "0", "false", "no", "")


_C: Dict[str, dict] = {}
_AT = 0.0


def _ratios() -> Dict[str, float]:
    global _C, _AT
    if _C and (time.time() - _AT) < _TTL:
        return _C
    out: Dict[str, float] = {}
    try:
        from app.scheduler import exec_state
        for k, s in (exec_state.samples() or {}).items():
            n = int(s.get("n") or 0)
            sp = float(s.get("sum_planned") or 0.0)
            sa = float(s.get("sum_actual") or 0.0)
            if n < _MIN_N or sp <= 0 or sa <= 0:
                continue
            out[k] = sa / sp
    except Exception:
        # 校准是**增强**, 不是依赖 —— 读不到就当没学过, 绝不能让它阻断排程
        return {}
    _C, _AT = out, time.time()
    return out


def invalidate() -> None:
    """清空缓存（上报新执行态后调用, 否则本次进程内学不到刚攒的样本）。"""
    global _C, _AT
    _C, _AT = {}, 0.0


def apply(bucket: str, base: float) -> float:
    """把校准系数乘到兜底估计值上。关档 / 无样本 / 未达门限 ⇒ 原值返回。"""
    if not _ENABLED or not bucket:
        return base
    r = _ratios().get(bucket)
    if not r:
        return base
    v = base * r
    return max(base * _LO, min(base * _HI, v))


def snapshot() -> dict:
    """给界面看的"学到了什么" —— 含每个档位的样本数与是否真的生效。"""
    try:
        from app.scheduler import exec_state
        raw = exec_state.samples() or {}
    except Exception:
        raw = {}
    items = []
    for k in sorted(raw):
        s = raw[k]
        n = int(s.get("n") or 0)
        sp = float(s.get("sum_planned") or 0.0)
        sa = float(s.get("sum_actual") or 0.0)
        ratio = (sa / sp) if sp > 0 else 0.0
        items.append({
            "bucket": k, "n": n, "ratio": round(ratio, 3),
            "active": bool(_ENABLED) and n >= _MIN_N and sp > 0,
            "effective": round(max(_LO, min(_HI, ratio)), 3) if sp > 0 else 1.0,
        })
    return {"enabled": _ENABLED, "min_n": _MIN_N,
            "clamp": [_LO, _HI], "buckets": items}
