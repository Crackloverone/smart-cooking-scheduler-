"""
目标塑形 (Objective Shaping)

现状问题: 原实现把三个目标压成一组**硬编码常数** (WAIT_COST=1.0 / 出锅差权重 60.0),
用户说"我赶时间"和"请客要同时上桌", 排出来的计划一模一样 —— 模型没有参与目标决策。

本模块让 LLM 把**用户自然语言偏好**翻译成多目标权重向量, 交给求解器执行:
  "半小时内搞定"      -> w_makespan 高, 目标出锅差放宽
  "请客, 要一起上桌"   -> w_gap 高, target_gap 收紧
  "别太累, 少动手"     -> w_hand 高 (打散连续手动操作)
  "要热乎的"          -> w_hot 高 (保热优先, 炒菜压到最后)

规则快速路径 (毫秒) 覆盖绝大多数口语; LLM 只在规则没命中且在线预算允许时补充,
保证 30% 的性能分不受影响。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict

from app.agent import llm
from app.scheduler.metrics import dish_cook_finish


@dataclass
class SchedulingPolicy:
    w_makespan: float = 1.0     # 总时长权重
    w_gap: float = 60.0         # 同出锅对齐权重 (原实现即 60.0)
    w_hand: float = 0.5         # 人工负载均衡权重
    w_switch: float = 2.0       # 设备换型/升温惩罚
    w_hot: float = 30.0         # 保热优先权重
    target_gap: float = 5.0     # 目标出锅差(分钟), 优秀线
    allow_preprocess: bool = False   # 是否把备菜移出关键路径
    prefer_share: bool = True        # 是否允许同温合炉
    strategy: str = "critical_path"
    rationale: str = ""
    source: str = "rule"             # rule | llm

    def to_dict(self):
        return asdict(self)


# ---------- 规则快速路径 ----------
_RUSH = r"赶时间|快点|尽快|来不及|半小时|分钟搞定|抓紧|越快越好|省时间|时间紧"
_FEAST = r"请客|宴请|一起上|同时上桌|同时出锅|仪式感|一桌|上齐|齐了|聚会"
_LAZY = r"少动手|别太累|轻松|简单点|省事|不想麻烦|省力|不用盯"
_HOT = r"热乎|趁热|烫|热腾腾|冒热气"
_BATCH = r"备菜|提前准备|预处理|先切好|一锅|合炉|一起烤|一起蒸"

_STRATEGY_BY_HINT = {
    "rush": "compress",
    "feast": "serve_hot_last",
    "lazy": "balanced",
    "hot": "serve_hot_last",
    "batch": "prep_first",
}


def rule_policy(user_text: str) -> SchedulingPolicy:
    t = user_text or ""
    pol = SchedulingPolicy()
    hits = []
    if re.search(_RUSH, t):
        pol.w_makespan, pol.target_gap = 4.0, 10.0
        hits.append("rush")
    if re.search(_FEAST, t):
        pol.w_gap, pol.target_gap = 160.0, 3.0
        hits.append("feast")
    if re.search(_LAZY, t):
        pol.w_hand = 4.0
        hits.append("lazy")
    if re.search(_HOT, t):
        pol.w_hot = 90.0
        hits.append("hot")
    if re.search(_BATCH, t):
        pol.allow_preprocess = True
        hits.append("batch")
    pol.strategy = _STRATEGY_BY_HINT.get(hits[0], "critical_path") if hits else "critical_path"
    pol.rationale = ("命中偏好: " + ",".join(hits)) if hits else "默认均衡策略"
    pol.source = "rule"
    return pol


# ---------- 规则选菜 (自然语言 -> 菜名) ----------
# 与 rule_policy 同一层: 规则快速路径兜住绝大多数口语, LLM 只在规则没命中时补充。
# 关键是**模型不可用时也要能选出菜**, 否则整个 /api/agent_plan 端点直接废掉。
# 用户口语 -> 菜名里该出现的关键词。直接匹配菜名而不是分类字段:
# 菜名是权威的("清蒸鲈鱼"就是要蒸), 而分类是派生出来的, 一旦它的规则变动
# 就会把选菜结果带偏。
_PICK_HINT = [
    (r"蒸|清蒸|蒸箱", r"蒸"),
    (r"烤|烘焙|饼干|蛋糕|派|酥|焗", r"烤|焗|酥|派|饼干|蛋糕"),
    (r"汤|羹|煲|炖", r"汤|羹|煲"),
    (r"炒|快炒|爆炒|小炒|快手", r"炒"),
    (r"鱼|海鲜|虾", r"鱼|虾|蟹|贝"),
    (r"鸡", r"鸡"),
    (r"牛|排骨|肉", r"牛|排骨|肉"),
    (r"主食|饭|面|粥", r"饭|面|粥"),
]
_NUM_CN = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}


def rule_pick(user_text: str, metas: list[dict], default_n: int = 3) -> list[str]:
    """
    按用户口语从菜库挑 2-5 道菜。纯规则、零延迟、无网络依赖。
    metas: [{'name','category','steps','minutes'}, ...] (只用 name / minutes)
    """
    t = user_text or ""
    names = [m_["name"] for m_ in metas]
    mins = {m_["name"]: (m_.get("minutes") or 0) for m_ in metas}

    # 1) 数量: 优先阿拉伯数字, 其次中文数字
    n = 0
    m = re.search(r"([1-9])\s*道", t)
    if m:
        n = int(m.group(1))
    else:
        for cn, v in _NUM_CN.items():
            if re.search(rf"{cn}\s*道", t):
                n = v
                break
    if not n:
        # "一荤两素" 这类: 把提到的份数加起来
        parts = [_NUM_CN[c] for c in _NUM_CN if re.search(rf"{c}\s*[荤素汤菜]", t)]
        n = sum(parts) if parts else 0
    n = max(2, min(5, n or default_n))

    # 2) 按用户提到的意图顺序, 在菜名里轮流取, 保证"一蒸一汤"这种混搭被满足
    wanted = [pat for upat, pat in _PICK_HINT if re.search(upat, t)]
    picked: list[str] = []
    if wanted:
        i = 0
        while len(picked) < n and i < n * len(wanted) * 2:
            pat = wanted[i % len(wanted)]
            for nm in names:
                if nm not in picked and re.search(pat, nm):
                    picked.append(nm)
                    break
            i += 1

    # 3) 补位: 没提到偏好或菜不够, 用没选过的菜补齐 (快的优先)。
    #    注意 minutes=0 是"没识别出时长", 不是"0 分钟就能做好", 必须排最后,
    #    否则补位补出来的全是解析失败的菜。
    if len(picked) < n:
        rest = sorted((nm for nm in names if nm not in picked),
                      key=lambda x: (mins[x] <= 0, mins[x]))
        picked.extend(rest[:n - len(picked)])
    return picked[:n]


# ---------- LLM 精化 ----------
_SYS = ("你是烹饪排程的目标决策器。把用户的口语偏好翻译成排程权重向量, "
        "只输出 JSON, 不要解释。")

_USER = """用户说: "{text}"
本次要做 {n} 道菜。

输出 JSON:
{{
  "w_makespan": 总时长权重(0.5~6, 越大越追求总时长最短),
  "w_gap": 同出锅权重(20~200, 越大越追求所有菜同时出锅),
  "w_hand": 人工均衡权重(0~6, 越大越打散连续手动操作),
  "w_hot": 保热权重(0~120, 越大越把爆炒/清蒸这类必须趁热的菜压到最后),
  "target_gap": 期望出锅差分钟(1~15),
  "allow_preprocess": true/false,  // 是否把切配/腌制提前到开火前完成
  "strategy": "critical_path|serve_hot_last|prep_first|compress|balanced",
  "rationale": "一句话说明你为什么这么定权重(25字内)"
}}"""


def llm_policy(user_text: str, n_dishes: int, timeout: float = 15.0) -> SchedulingPolicy | None:
    out = llm.chat_json(_SYS, _USER.format(text=user_text, n=n_dishes),
                        cache_key=None, temperature=0.1, timeout=timeout)
    if not isinstance(out, dict):
        return None
    base = rule_policy(user_text)
    try:
        pol = SchedulingPolicy(
            w_makespan=float(out.get("w_makespan", base.w_makespan)),
            w_gap=float(out.get("w_gap", base.w_gap)),
            w_hand=float(out.get("w_hand", base.w_hand)),
            w_hot=float(out.get("w_hot", base.w_hot)),
            target_gap=float(out.get("target_gap", base.target_gap)),
            allow_preprocess=bool(out.get("allow_preprocess", base.allow_preprocess)),
            prefer_share=base.prefer_share,
            strategy=str(out.get("strategy") or base.strategy),
            rationale=str(out.get("rationale", ""))[:40],
            source="llm",
        )
    except Exception:
        return None
    return pol


def shape_policy(user_text: str, n_dishes: int = 3,
                 use_llm: bool = False, timeout: float = 15.0) -> SchedulingPolicy:
    """规则优先; 规则无命中且允许 LLM 时才问模型。永不失败。"""
    base = rule_policy(user_text)
    if use_llm and base.source == "rule" and not base.rationale.startswith("命中"):
        try:
            pol = llm_policy(user_text, n_dishes, timeout=timeout)
            if pol is not None:
                return pol
        except Exception:
            pass
    return base


# ---------- 统一打分 (供策略择优使用) ----------
#
# 保热项的两个口径决策 (v2.3, 2026-10-04), 都是**量过之后**才定的:
#
# ① 基准用 `dish_cook_finish`(出锅时刻), 不用 `plan.dish_finish`(完成时刻)。
#    用完成时刻时这一项**恒 ≈ 0**, 等于从来没参与过择优 —— 把一条 1 分钟的收尾步
#    ("尽情品尝吧")停在时间线末端, 完成时刻就等于 T, 惩罚被完全掩掉。实测 60 组
#    随机实例里, 报告 Γ 与"真实烹饪跨度"差 >5 min 的有 49 组(中位 23, 最坏 175)。
#    FT_HOT_BASE=finish 可退回旧口径, 供 A/B。
#
# ② 但**必须同时给它一个总预算**, 否则它从"没用"直接变成"夺权": 它是个无界加权项,
#    一道菜等 170 min ⇒ 30×0.9×170 ≈ 4590, 而 w_gap·Γ 只有 60×3 = 180
#    ⇒ 策略择优被整个改写, 代价是出锅差<5min 占比 90.3% → 80.3%(6 seed, 300 组)。
#    预算**以"分钟的 Γ"计价**: `min(hot, w_gap × FT_HOT_BUDGET)`, 于是语义清晰 ——
#    "保热最多花掉 N 分钟的 Γ", 任何 Γ 领先超过 N 分钟的方案都不会被保热翻盘。
#    FT_HOT_BUDGET=0 等价于旧行为(该项彻底不参与), off 则无界。
#
#    默认 0.5 是**扫出来的**(4 seed × 60 组 lb_probe, 括号内为 6 seed × 50 组 bench 的 <5min):
#
#      budget   makespan==可证下界   gap 均值   保热违规   <5min
#      ── 旧口径(完成时刻, 无界)  65.0%      3.43     3.68    90.3%
#      0                        65.8%      3.18     3.68    90.3%
#      0.5  ← 采用              65.0%      3.23     ~3.5    90.3%
#      1                        64.5%      3.43     3.38    90.3%
#      2                        62.8%      3.60     3.40    90.3%
#      4                        62.0%      3.75     3.17    90.3%
#      off(无界)                  —         —       1.04    80.7%
#
#    读法: **出锅差 Γ 对 [0, 4] 整个区间免疫**(评分表主项, 必须是平的);
#    但预算一大就开始用 makespan 去换保热 —— 2 已经掉了 2pp 的"makespan == 可证下界"。
#    0.5 是三条轴上都不劣于旧口径的最小值, 所以取它: **保热只做微调, 不参与交易**。
_HOT_BUDGET_DEFAULT = 0.5


def _hot_flag(name: str, default: float) -> float:
    """读一个"以分钟计"的预算开关。未设 -> 默认; off/0/false/no/inf -> 无界。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    if str(raw).strip().lower() in ("off", "false", "no", "inf", ""):
        return float("inf")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def hot_budget() -> float:
    """保热项的总预算, 以"分钟的 Γ"计价 (见上方 ②)。0 = 该项不参与。"""
    return _hot_flag("FT_HOT_BUDGET", _HOT_BUDGET_DEFAULT)


def hot_base() -> str:
    """保热基准: "cook"=出锅时刻(默认) / "finish"=完成时刻(旧口径, 供 A/B)。"""
    return "finish" if os.environ.get("FT_HOT_BASE", "cook").strip().lower() == "finish" else "cook"


def score_plan(plan, kpi, policy: SchedulingPolicy, priors: dict | None = None) -> float:
    """成本函数, 越小越好。硬约束违反 -> 天价惩罚。"""
    cost = 0.0
    cost += 1e6 * (kpi.conflict_count + kpi.depend_violation)
    cost += policy.w_makespan * kpi.makespan_min
    cost += policy.w_gap * kpi.finish_gap_min
    cost += policy.w_hand * getattr(kpi, "hand_streak_min", 0.0)
    # 保热: 必须趁热的菜若**出锅后**还要干等太久 -> 罚 (口径与预算见上方 ① ②)
    if priors:
        T = max((t.end_min for t in plan.tasks if t.start_min >= 0), default=0.0)
        fin_of = plan.dish_finish if hot_base() == "finish" else dish_cook_finish(plan)
        hot = 0.0
        for dish, fin in (plan.dish_finish or {}).items():
            p = priors.get(dish)
            if not p:
                continue
            wait = max(0.0, T - fin_of.get(dish, fin))   # 出锅后还要等多久才开饭
            hot += policy.w_hot * p.serve_hot * max(0.0, wait - p.hold_min)
        cost += min(hot, policy.w_gap * hot_budget())
    return round(cost, 2)
