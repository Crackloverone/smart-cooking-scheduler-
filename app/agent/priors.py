"""
调度语义先验 (Scheduling Priors) —— "模型参与调度决策"的核心载体

为什么需要它
------------
求解器只懂硬信息 (设备/时长/依赖)。但真实厨房里有一批**只能靠语义判断**的调度决策,
求解器结构上做不了, 而它们恰好对应赛题的三个高分项:

  | 语义先验              | 影响赛题评分项                        |
  |----------------------|------------------------------------|
  | 哪些步骤能提前做(备菜) | 总烹饪时间压缩率 (多目标优化 25 分)   |
  | 哪道菜必须趁热上桌     | 同出锅时间差语义化 + 动态重排仲裁     |
  | 哪几道菜可同温合炉     | 并行度 / 设备零冲突 (多菜并行 25 分)  |
  | 哪一步可中途离开       | 识别可并行步骤 (多菜并行 25 分)       |
  | 人工强度分布           | 用户连续手动操作时长 (多目标 25 分)   |

工程约束
--------
中转站单次 LLM 调用约 6s, 赛题要求初始排程 <5s —— 所以先验**一律离线抽取并落盘**,
在线排程只读缓存 (0 延迟)。这就是"模型在环"但不牺牲性能的关键:
**模型参与的是决策本身, 而不是在线算力**。

调用链: extract_prior() -> 磁盘缓存 -> LLM 抽取 -> 规则兜底
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from app.models.schema import (
    Recipe, DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN, DEVICE_FRIDGE,
    DEVICE_NONE, PREP, PLATE,
)
from app.agent import llm

# ---------- 语义词典 (规则兜底用) ----------
#
# kind 决定 serve_hot / hold_min 两档保热语义, 直接喂给:
#   · objective.score_plan 的保热惩罚 (哪道菜必须压到最后)
#   · engine/model 的保热排序与冷等待项
#   · replan / notifications 的"趁热上桌"提醒 (阈值 serve_hot >= 0.85)
#
# v2.3 修复 (2026-10-04): 旧实现把 **72/100** 道菜判成 "fry"(趁热 0.95 / 可放 6 分)。
# 根因两条, 都不报错、只让先验静默失真:
#   ① `_FRY` 收了"烤|酥", 于是**任何**烤箱烘焙都落进 fry;
#   ② `has_fry` 对**任意一个**灶具/烤箱步生效, 于是"辅料处理/收尾淋油"也能定调。
# 受害最重的是甜品与蒸菜:
#   · 雪媚娘 #1「准备熟糯米粉: 炒锅慢火炒至微黄色」⇒ 一道冰皮冷甜品被判成爆炒;
#   · 黑森林蛋糕/糖霜饼干/广式月饼/红豆糯米糕 ⇒ 烘焙甜点全被判成"6 分钟内必须吃掉";
#   · 清蒸鲈鱼 #5「烧热油淋在鱼身上」⇒ 蒸菜被判成爆炒(蒸箱被一个淋油步盖过);
#   · 竹荪排骨汤 ⇒ 汤被判成爆炒(hold 45 → 6)。
#
# 新判据按"**上桌时的状态**"判, 三个信号依次为:
#   ① 末道设备工序是冷藏/冷冻 ⇒ 冷食 (冷甜品 / 冰皮月饼 / 慕斯);
#   ② 菜名强信号, **顺序即优先级**: 冷食 > 汤羹 > 烘焙 > 蒸 > 炒炸;
#   ③ 加热步的**主导设备** (平票按 蒸箱 > 烤箱 > 灶具), 因为淋热油/焯水这类
#      灶具收尾步在官方菜谱里非常多, 按"有没有炒"判必被它带偏。
_COLD_NAME = r"凉拌|沙拉|拌菜|泡菜|卤|醉|糟|冰皮|布丁|慕斯|双皮奶|龟苓膏|凉粉|凉面|冰镇"
_SOUP_NAME = r"汤|羹|煲|粥|烩"
_BAKE_NAME = r"蛋糕|面包|饼干|披萨|派|酥|挞|月饼|曲奇|吐司|司康|燕麦|玛芬|布朗尼|华夫|焗"
_STEAM_NAME = r"蒸|白灼|灼|蛋羹"
_FRY_NAME = r"炸|煎|炒|爆|煸"

# 主导设备 -> kind。灶具=炒煎炸, 蒸箱=蒸, 烤箱=烤(烘焙)
_DEV_KIND = {DEVICE_STOVE: "fry", DEVICE_STEAMER: "steam", DEVICE_OVEN: "bake"}
_HEAT_DEVS = (DEVICE_STEAMER, DEVICE_OVEN, DEVICE_STOVE)   # 顺序 = 平票优先级

_SERVE_HOT_BY_KIND = {
    "cold": 0.15, "soup": 0.45, "steam": 0.90, "fry": 0.95,
    "bake": 0.75, "other": 0.60,
}
_HOLD_BY_KIND = {
    "cold": 180.0, "soup": 45.0, "steam": 10.0, "fry": 6.0,
    "bake": 20.0, "other": 15.0,
}


def _dur(step) -> float:
    if step.duration_min is not None:
        return max(float(step.duration_min), 0.5)
    if step.device in (DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN):
        return 8.0
    if step.device == DEVICE_FRIDGE:
        return 30.0
    return 1.0 if step.prep_type == PLATE else (2.0 if step.prep_type == PREP else 3.0)


@dataclass
class DishPrior:
    dish: str
    serve_hot: float = 0.60          # 0..1, 必须趁热上桌的程度
    hold_min: float = 15.0           # 出锅后仍可接受的等待分钟
    prep_step_ids: list = field(default_factory=list)   # 可提前备好的步骤
    prep_lead_min: float = 0.0       # 提前准备总量(分钟) -> 移出关键路径可压缩总时长
    merge_group: str = ""            # 同温合炉组, 如 "蒸箱@100"
    interruptible: bool = False      # 加热中可否离开去干别的菜
    heat_critical: list = field(default_factory=list)   # 火候关键步骤(不可久放/等待)
    human_load: float = 0.5          # 人工强度 0..1
    source: str = "rule"             # rule | llm | llm-cache
    reason: str = ""                 # 一句话理由 (答辩可解释)

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "DishPrior":
        allowed = {k: v for k, v in d.items() if k in DishPrior.__dataclass_fields__}
        return DishPrior(**allowed)


# ---------------- 规则兜底 ----------------
def _served_cold(steps) -> bool:
    """末道**设备**工序是冷藏/冷冻 ⇒ 端上桌时是冷的。

    判据必须严, 两个条件都要满足:
      · 冰箱步之后只剩上桌动作 —— 否则"冷藏醒面/冷冻定型"会被误判
        (玛格丽特 #2 冷藏面团, 后面还有烘烤);
      · 原文没说"趁热" —— 否则"蒸好后…趁热或冷藏后食用"会被误判
        (皂角银耳羹 #4 一句里同时给了冷热两种吃法)。
    """
    last_i = None
    for i in range(len(steps) - 1, -1, -1):
        d = steps[i].device
        if d in _DEV_KIND or d == DEVICE_FRIDGE:
            last_i = i
            break
    if last_i is None or steps[last_i].device != DEVICE_FRIDGE:
        return False
    if "趁热" in (steps[last_i].raw_text or ""):
        return False
    return all(s.prep_type == PLATE for s in steps[last_i + 1:])


def _kind_of(recipe: Recipe) -> str:
    steps = list(recipe.steps)
    name = recipe.name
    # ① 上桌状态: 末道设备工序是冷藏/冷冻
    if _served_cold(steps):
        return "cold"
    # ② 菜名强信号 (顺序即优先级)
    #    「蛋羹」必须先于「羹」判 —— 鸡蛋羹是**蒸**出来的, 归汤羹会把 hold 从 10 分放宽到 45 分。
    for pat, kind in ((_COLD_NAME, "cold"), (_BAKE_NAME, "bake"),
                      (_STEAM_NAME, "steam"), (_SOUP_NAME, "soup"),
                      (_FRY_NAME, "fry")):
        if re.search(pat, name):
            return kind
    # ③ 菜名无信号 -> 加热步的主导设备 (平票按 蒸箱 > 烤箱 > 灶具)
    votes = {d: 0 for d in _HEAT_DEVS}
    for s in steps:
        if s.device in votes:
            votes[s.device] += 1
    if any(votes.values()):
        return _DEV_KIND[max(_HEAT_DEVS, key=lambda d: votes[d])]
    return "other"


def rule_prior(recipe: Recipe) -> DishPrior:
    """纯规则先验: 保证离线/断网也有语义调度能力。"""
    kind = _kind_of(recipe)
    serve_hot = _SERVE_HOT_BY_KIND[kind]
    hold_min = _HOLD_BY_KIND[kind]

    prep_ids, prep_min = [], 0.0
    heat_crit, merge = [], ""
    human_seconds = 0.0
    total_seconds = 0.0
    interruptible = False

    for s in recipe.steps:
        d = _dur(s)
        total_seconds += d
        # 人工强度
        if s.needs_human and not s.unattended:
            human_seconds += d
        # 可提前备菜: 纯切配/无设备人工步 (备菜是标准做法)
        if s.prep_type == PREP or (s.device == DEVICE_NONE and not s.unattended):
            prep_ids.append(s.step_id)
            prep_min += d
        # 可中途离开: 长时无人值守
        if s.unattended and d >= 10:
            interruptible = True
        # 火候关键: 灶具爆炒/煎炸, 或短时蒸
        if s.device == DEVICE_STOVE and re.search(r"炒|爆|煎|炸|煸", (s.mode or "") + s.raw_text):
            heat_crit.append(s.step_id)
        elif s.device == DEVICE_STEAMER and d <= 12:
            heat_crit.append(s.step_id)
        # 同温合炉组
        if s.device in (DEVICE_STEAMER, DEVICE_OVEN) and s.temperature_c:
            merge = f"{s.device}@{int(round(s.temperature_c / 5) * 5)}"

    human_load = round(human_seconds / total_seconds, 2) if total_seconds else 0.5
    return DishPrior(
        dish=recipe.name, serve_hot=serve_hot, hold_min=hold_min,
        prep_step_ids=prep_ids, prep_lead_min=round(min(prep_min, 45.0), 1),
        merge_group=merge, interruptible=interruptible,
        heat_critical=heat_crit, human_load=human_load,
        source="rule",
        reason=f"主导工序={kind}",
    )


# ---------------- LLM 抽取 ----------------
_SYS = (
    "你是家庭厨房的排程专家。给定一道菜的菜名、食材和自然语言步骤, "
    "你要判断它的**调度语义**(不是烹饪参数), 只输出 JSON, 不要解释。"
)

_USER_TMPL = """菜名: {name}
食材: {ingredients}
步骤:
{steps}

请输出 JSON:
{{
  "serve_hot": 0到1的小数, 表示"必须趁热上桌"的程度(爆炒/油炸/清蒸鱼=0.95, 炖汤=0.45, 凉拌/卤味=0.15),
  "hold_min": 数字, 出锅后还能等多少分钟不变质变味(油炸=3, 炒=6, 蒸=10, 烤=15, 炖汤=45, 凉菜=180),
  "prep_step_ids": [可以提前备好的步骤序号],  // 切配/腌制/解冻/焯水等不占用烹饪火候的步骤
  "interruptible": true/false,  // 加热过程中人能否离开去处理别的菜
  "heat_critical_step_ids": [火候关键、出锅后必须马上上桌的步骤序号],
  "merge_group": "蒸箱@100" 或 "烤箱@180" 或 "",  // 若该菜能与同温的菜合炉, 填设备@温度
  "reason": "一句话说明该菜的调度要点(20字内)"
}}
步骤序号从 1 开始。"""


def llm_prior(recipe: Recipe, timeout: float = 20.0) -> DishPrior | None:
    steps_text = "\n".join(f"{s.step_id}. {s.raw_text}" for s in recipe.steps)
    user = _USER_TMPL.format(name=recipe.name,
                             ingredients="；".join(recipe.ingredients)[:200],
                             steps=steps_text[:1500])
    key = f"prior::{recipe.name}"
    out = llm.chat_json(_SYS, user, cache_key=key, temperature=0.1, timeout=timeout)
    if not isinstance(out, dict):
        return None
    base = rule_prior(recipe)   # 兜底值, 缺字段时补
    try:
        prior = DishPrior(
            dish=recipe.name,
            serve_hot=float(out.get("serve_hot", base.serve_hot)),
            hold_min=float(out.get("hold_min", base.hold_min)),
            prep_step_ids=[int(x) for x in (out.get("prep_step_ids") or [])],
            prep_lead_min=base.prep_lead_min,
            merge_group=str(out.get("merge_group") or base.merge_group),
            interruptible=bool(out.get("interruptible", base.interruptible)),
            heat_critical=[int(x) for x in (out.get("heat_critical_step_ids") or [])],
            human_load=base.human_load,
            source="llm-cache",
            reason=str(out.get("reason", ""))[:40],
        )
    except Exception:
        return None
    # 合法性校验: LLM 可能幻觉出不存在的步骤号
    valid = {s.step_id for s in recipe.steps}
    prior.prep_step_ids = [i for i in prior.prep_step_ids if i in valid]
    prior.heat_critical = [i for i in prior.heat_critical if i in valid]
    prior.serve_hot = min(max(prior.serve_hot, 0.0), 1.0)
    prior.hold_min = min(max(prior.hold_min, 1.0), 240.0)
    return prior


def extract_prior(recipe: Recipe, use_llm: bool = True, timeout: float = 20.0,
                  allow_cache: bool = True) -> DishPrior:
    """缓存 -> LLM -> 规则兜底。任何异常都退化为规则先验, 永不失败。

    v2.1 修复: 原实现 `use_llm=False` 时**连缓存都不读**, 导致在线排程拿不到离线
    已经算好的模型先验, 只能退化为纯规则 —— 等于模型白算了一遍。
    现在缓存与在线调用解耦: `allow_cache=True`(默认) 时, 只要磁盘有就直接用,
    0 延迟、0 网络, 这才是"模型在环但不牺牲性能"的正确姿势。
    """
    key = f"prior::{recipe.name}"
    if allow_cache:
        hit = llm.cache_get(key)
        if isinstance(hit, dict):
            try:
                p = DishPrior.from_dict(hit)
                p.source = "llm-cache"
                return p
            except Exception:
                pass
    if use_llm:
        try:
            p = llm_prior(recipe, timeout=timeout)
            if p is not None:
                llm.cache_put(key, p.to_dict())
                llm.cache_flush()
                return p
        except Exception:
            pass
    return rule_prior(recipe)


def get_priors(recipes: list[Recipe], use_llm: bool = False,
               allow_cache: bool = True) -> dict:
    """返回 {菜名: DishPrior}。
    use_llm=False 时只读缓存/规则 —— 在线路径用这个, 保证零网络延迟。"""
    return {r.name: extract_prior(r, use_llm=use_llm, allow_cache=allow_cache)
            for r in recipes}


def prior_brief(priors: dict, limit: int = 6) -> str:
    """把先验压成一句话, 喂给 LLM 做策略决策 / 排程解释。"""
    rows = []
    for name, p in list(priors.items())[:limit]:
        rows.append(f"{name}: 趁热{p.serve_hot:.2f}/可放{p.hold_min:.0f}分/"
                    f"备菜{p.prep_lead_min:.0f}分/{'可离开' if p.interruptible else '需盯'}"
                    f"{'/' + p.merge_group if p.merge_group else ''}")
    return "\n".join(rows)
