"""
L4 通知生成: 从排程中生成精准、简洁、可操作的用户提醒。

赛题要求(20 分, 专家评审): 精准(带菜名+动作) / 简洁(一句话) / 可操作(告诉用户现在该干什么)。

v2 改动
-------
原实现是纯模板, 只有两句干巴巴的话 ("现在可以开始处理X"), 拿不到"可操作性"分。
现在改成 **LLM 语境化生成 + 模板兜底**:

  - 模板路径 (在线默认, 零延迟): 基于排程与语义先验合成三类节点, 并补上原实现缺失的
    第四类 —— "趁等待间隙插活" (对应赛题原文: "现在可以开始切西兰花")
  - LLM 路径 (演示/答辩用): 把整条时间线交给模型, 生成口语化、带上下文的一句话提醒
"""
from __future__ import annotations

from app.models.schema import SchedulePlan
from app.agent import llm

# 动作词表: 越具体越好 —— 通知的"可操作性"分就靠它。
# 原实现兜底词是"处理", "现在开始处理X" 这种话拿不到精准分。
_ACT = [("翻炒", "翻炒"), ("煸炒", "翻炒"), ("爆炒", "翻炒"), ("炒", "翻炒"),
        ("烤", "烘烤"), ("蒸", "蒸制"), ("炖", "慢炖"), ("煲", "煲汤"),
        ("煮", "烹煮"), ("焯", "焯水"), ("煎", "煎制"), ("炸", "油炸"),
        ("取出", "取出"), ("端出", "端出"), ("装盘", "装盘"), ("摆盘", "摆盘"),
        ("点缀", "点缀"), ("冷藏", "冷藏"), ("冷冻", "冷冻"), ("腌制", "腌制"),
        ("改刀", "改刀"), ("去鳃", "清理"), ("切", "切配"), ("剁", "剁"),
        ("斩", "斩"), ("拍", "拍松"), ("剥", "剥"), ("洗净", "清洗"),
        ("洗", "清洗"), ("沥干", "沥干"), ("捞出", "捞出"), ("浸泡", "浸泡"),
        ("静置", "静置"), ("解冻", "解冻"), ("拌匀", "拌匀"), ("搅拌", "搅拌"),
        ("抓匀", "抓匀"), ("揉", "揉面"), ("擀", "擀"), ("包", "包制"),
        ("卷", "卷起"), ("捏", "捏合"), ("裹", "裹"), ("涂抹", "涂抹"),
        ("刷", "刷"), ("倒入", "倒料"), ("加入", "加料"), ("放入", "放入"),
        ("码放", "码放"), ("摆上", "摆放"), ("铺开", "铺开"), ("淋", "淋"),
        ("撒", "撒"), ("混合", "混合"), ("过滤", "过滤"), ("滤", "过滤"),
        ("预热", "预热"), ("烧热", "烧热"), ("烧开", "烧开"), ("煮沸", "煮沸")]


def _short(raw: str, n: int = 16) -> str:
    raw = raw.strip().strip("。；;")
    return raw if len(raw) <= n else raw[:n] + "…"


def _action_of(raw: str, device: str = "") -> str:
    """从原文里取一个**具体动作**词。

    ⚠ 蒸箱/烤箱**先按设备定动作**，不要走关键词：官方菜谱里蒸菜的装载句写的是
      「将蒸**烤**架放入中间层」——里面的"烤"会让关键词表命中"烘烤"，
      于是实测出现过「3分钟后**烘烤**竹荪排骨汤完成」这种把蒸菜说成烤菜的提醒。
      设备已经把答案说清楚了（蒸箱=蒸制 / 烤箱=烘烤），没必要再去猜。
    """
    if device == "蒸箱":
        return "蒸制"
    if device == "烤箱":
        return "烘烤"
    for kw, act in _ACT:
        if kw in raw:
            return act
    return "备菜"


def _is_manual(t) -> bool:
    return t.needs_human and not t.unattended and t.unit_id not in ("等待", "提前准备")


# 动作的"信息量"分档 —— 同一道菜在一个时间块里只报一次时，用它挑最该说的那个。
#
# 为什么需要：原实现直接取"块里前 2 个任务的动作"，于是同一道菜会被并列成
# **自相矛盾**的一句（实测）：
#     「现在开始取出苹果芒果派、放入苹果芒果派，共8步」
# 那三个动作其实分别是「面团从冰箱取出擀开」「把水果放入派皮」「放上蒸烤盘刷蛋液」，
# 真正该说的是最后那个**烘烤**。取放类动作（取出/放入）在块里几乎总是噪声。
_HIGH = {"翻炒", "烘烤", "蒸制", "慢炖", "煲汤", "烹煮", "焯水", "煎制", "油炸",
         "腌制", "冷藏", "冷冻", "预热", "烧热", "烧开", "煮沸", "揉面", "包制",
         "擀", "装盘", "摆盘", "切配", "剁", "斩", "拍松"}
_LOW = {"取出", "端出", "放入", "捞出", "点缀", "备菜"}


def _act_rank(act: str) -> int:
    if act in _HIGH:
        return 2
    if act in _LOW:
        return 0
    return 1


# 真正的**加热设备**。只有落在这三台上的步才配说"完成 / 趁热上桌"。
_HEATING = ("灶具", "蒸箱", "烤箱")


def _template(plan: SchedulePlan, priors: dict | None = None) -> list[dict]:
    notes = []
    cooking = [t for t in plan.tasks if t.start_min >= 0 and t.unit_id not in ("提前准备",)]
    if not cooking:
        return notes

    # 无人值守窗口: 用于"趁等待间隙插活"
    windows = [(t.start_min, t.end_min, t.dish, _action_of(t.raw_text, t.device))
               for t in cooking if t.unattended and t.duration_min >= 3 and t.dish]

    def in_window(start):
        for (s, e, dish, act) in windows:
            if s + 0.5 <= start <= e - 0.5:
                return dish, act
        return None

    # ---- A. 人工任务: 连续的手动操作合并成一条, 避免刷屏 ----
    manual = sorted([t for t in cooking if _is_manual(t)], key=lambda x: x.start_min)
    blocks, cur = [], None
    for t in manual:
        if cur and t.start_min - cur["end"] <= 1.0:
            cur["items"].append(t)
            cur["end"] = max(cur["end"], t.end_min)
        else:
            if cur:
                blocks.append(cur)
            cur = {"start": t.start_min, "end": t.end_min, "items": [t]}
    if cur:
        blocks.append(cur)

    for b in blocks:
        # ① **同一道菜只报一次**，动作取块内信息量最高的那个。
        #    原实现直接取前 2 个任务，会把同一道菜并列成自相矛盾的一句：
        #    「现在开始取出苹果芒果派、放入苹果芒果派，共8步」。
        per_dish: dict = {}
        for t in b["items"]:
            act = _action_of(t.raw_text, t.device)
            cur = per_dish.get(t.dish)
            if cur is None or _act_rank(act) > _act_rank(cur[0]):
                per_dish[t.dish] = (act, t)
        pairs = [f"{act}{dish}" for dish, (act, _t) in per_dish.items()]

        # ② 最多列 3 个**具体动作**（"共N步"不可操作，赛题要的是"告诉用户现在该干什么"）。
        #    还有更多时才补一句概括。
        ctx = in_window(b["start"])
        head = f"趁{ctx[0]}还在{ctx[1]}，先" if ctx else "现在开始"
        text = head + "、".join(pairs[:3])
        if len(pairs) > 3:
            text += f"等，共{len(b['items'])}步"
        notes.append({"at_min": round(b["start"], 1), "level": "info", "text": text,
                      "kind": "manual"})

    # ---- B. 无人值守步骤收尾: 提前提醒取出 ----
    # 一道菜只报"出锅"这一次(它最后一道加热步骤), 而不是每个中间步骤都报一遍,
    # 否则同一道菜会连发 3 条"X分钟后蒸制鲈鱼完成", 反而淹没真正要紧的提醒。
    last_cook: dict[str, object] = {}
    for t in cooking:
        # ⚠ 只认**真正的加热设备**。原判据只看 `unattended and duration>=3`，
        #   于是「尽情享用吧」这类收尾步（人工、3 分钟）也会被当成"烹饪完成"，
        #   产出实测见过的一句：「3分钟后备菜苹果芒果派完成，趁热上桌」——
        #   既没动作也不可操作，还挂着"趁热"（备菜跟趁热毫无关系）。
        if (t.unattended and t.duration_min >= 3 and t.dish
                and t.device in _HEATING
                # 动作必须**是件事**：取出/放入/备菜 不构成"某道菜做好了"的提醒。
                # 实测见过「2分钟后取出酿苦瓜完成，趁热上桌」——那一步其实是装载句。
                and _act_rank(_action_of(t.raw_text, t.device)) >= 1):
            cur = last_cook.get(t.dish)
            if cur is None or t.end_min > cur.end_min:   # type: ignore[union-attr]
                last_cook[t.dish] = t
    for t in last_cook.values():
        lead = min(3, max(1, int(t.duration_min * 0.25)))   # type: ignore[union-attr]
        act = _action_of(t.raw_text, t.device)              # type: ignore[union-attr]
        hot = ""
        if priors and t.dish in priors:                     # type: ignore[union-attr]
            p = priors[t.dish]                              # type: ignore[index]
            if p.serve_hot >= 0.85:
                hot = "，趁热上桌"
        notes.append({"at_min": round(t.end_min - lead, 1),  # type: ignore[union-attr]
                      "level": "warn",
                      "text": f"{lead}分钟后{act}{t.dish}完成{hot}",  # type: ignore[union-attr]
                      "kind": "finish"})

    # ---- C. 设备释放: 空了可以放入下一道菜 ----
    by_unit = {}
    for t in cooking:
        if t.unit_id not in ("等待", "双手-1", "提前准备"):
            by_unit.setdefault(t.unit_id, []).append(t)
    for unit, ts in by_unit.items():
        ts.sort(key=lambda x: x.start_min)
        for a, b in zip(ts, ts[1:]):
            # v3.0: 预热现在是真实步骤(unit_id 就是 "烤箱-1" 这种), 不再是
            # "预热-xxx" 伪任务。但"预热结束了, 设备空了"仍然不构成可操作提醒 ——
            # 设备本来就要紧接着烤同一道菜。判据改用 raw_text。
            if (a.raw_text or "").endswith("预热"):
                continue
            # 同一道菜换个阶段继续用这台设备, 不算"腾出来给下一道菜"
            if not b.dish or b.dish == a.dish:
                continue
            if b.start_min - a.end_min <= 5.0:
                notes.append({"at_min": round(a.end_min, 1), "level": "info",
                              "text": f"{unit.split('-')[0]}空了，可以放入{b.dish}",
                              "kind": "device"})

    notes.sort(key=lambda x: x["at_min"])
    # 同一时刻只留信息量最大的一条
    dedup, seen = [], set()
    for n in notes:
        key = (round(n["at_min"]), n["text"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(n)
    return dedup


# ---------------- LLM 语境化 ----------------
_SYS = ("你是家庭厨房的贴心助手。根据排程时间线, 生成用户提醒。"
        "要求: 每条一句话、口语化、必须带菜名和动作、告诉用户现在具体该干什么。只输出 JSON。")

_USER = """排程时间线(相对开饭时间轴, 单位分钟):
{timeline}

语义先验(趁热分越高越要趁热上桌; 可放分=出锅后还能等多久):
{priors}

请生成提醒, 只输出 JSON:
{{"notes": [{{"at_min": 数字, "level": "info|warn", "text": "一句话提醒"}}]}}

规则:
1. 人工操作前提醒, 并尽量利用"某道菜正在蒸/烤/炖"的等待间隙, 说清楚"趁X在蒸, 先切Y"
2. 无人值守步骤结束前 1-3 分钟提醒取出
3. 需要趁热的菜(趁热分>=0.85)出锅时提示"趁热上桌"
4. 不要超过 12 条, 不要重复, 不要说教"""


def _llm_notes(plan: SchedulePlan, priors: dict | None, timeout: float) -> list[dict] | None:
    rows = []
    for t in sorted(plan.tasks, key=lambda x: x.start_min):
        if t.start_min < 0 or not t.dish:
            continue
        rows.append(f"{t.start_min:.0f}-{t.end_min:.0f}分 {t.dish} | {t.device} | "
                    f"{'人工' if t.needs_human and not t.unattended else '无人值守'} | {_short(t.raw_text, 20)}")
    if not rows:
        return None
    ptext = "\n".join(f"{k}: 趁热{v.serve_hot:.2f} 可放{v.hold_min:.0f}分"
                      for k, v in (priors or {}).items()) or "(无)"
    out = llm.chat_json(_SYS, _USER.format(timeline="\n".join(rows), priors=ptext),
                        cache_key=None, temperature=0.4, timeout=timeout)
    if not isinstance(out, dict):
        return None
    notes = []
    T = max((t.end_min for t in plan.tasks if t.start_min >= 0), default=0)
    for n in (out.get("notes") or []):
        try:
            at = float(n["at_min"])
        except Exception:
            continue
        if not (-1 <= at <= T + 5):
            continue
        txt = str(n.get("text", "")).strip()
        if not txt:
            continue
        notes.append({"at_min": round(at, 1),
                      "level": "warn" if n.get("level") == "warn" else "info",
                      "text": txt[:60], "kind": "llm"})
    notes.sort(key=lambda x: x["at_min"])
    return notes[:12] or None


def generate_notifications(plan: SchedulePlan, priors: dict | None = None,
                           use_llm: bool = False, timeout: float = 15.0) -> list[dict]:
    """默认走模板 (零延迟); use_llm=True 时尝试 LLM 语境化, 失败自动回落模板。"""
    base = _template(plan, priors)
    if use_llm:
        try:
            got = _llm_notes(plan, priors, timeout)
            if got:
                return got
        except Exception:
            pass
    return base


def reschedule_notice(old: SchedulePlan, new: SchedulePlan,
                      completed_ids: list[str]) -> dict:
    """动态重排后的变更说明 (最小扰动原则)"""
    moved = []
    old_map = {t.task_id: t for t in old.tasks}
    for t in new.tasks:
        if t.task_id in completed_ids:
            continue
        o = old_map.get(t.task_id)
        if o and abs(o.start_min - t.start_min) > 0.5:
            moved.append({
                "task": t.task_id,
                "old_start": o.start_min,
                "new_start": t.start_min,
            })
    return {
        "已完成步骤保留": len(completed_ids),
        "调整步骤数": len(moved),
        "新出锅时间": new.dish_finish,
        "新总时长": new.makespan_min,
        "变更明细": moved,
    }
