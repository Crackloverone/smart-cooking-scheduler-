"""排程诊断: 关键路径 / 风险提示 / 冲突归因。

对应建议文本《智能烹饪调度详细注意事项》：
  · §七.1 输出内容 —— "关键路径：整个订单的关键任务列表"；
    "风险提示：哪些任务时间余量很小，容易延误（**红色预警项**）"；
  · §四.3 冲突处理 —— "优先告警，返回冲突原因（例如：蒸箱容量不足 / 操作员不足，
    无法按时出餐）"；
  · §三.3 关键路径识别 —— "找出整个订单从最早备菜到最终出餐的最长链路，
    这条路径决定整体出餐时间"。

**为什么单独一个模块，而不是塞进 `metrics`**
`metrics` 是**评分口径**：里面每个数字都要能进目标函数或评分表，改动它等于动基线。
本模块是**诊断口径**：只给人看，**不参与任何优化决策**，随便加都不影响排程结果。
两者混在一起，读代码的人会以为"风险分"是个评分项。

**关键路径为什么用"零余量链"而不是"最长时长链"**
教材式的"按任务时长累加找最长链"在这里会**指错方向**：本系统会刻意给短菜加空等
让它和长菜同时出锅（同出锅对齐），于是那条"时长最长"的链往往只到 makespan 的
一半左右，真正的瓶颈在别处。本模块改用**零余量链**：从决定出餐时刻的那个任务
往回走，每一步都要求"前一步结束 == 后一步开始"（中间没有缓冲）。
这条链的语义非常干净 —— **链上任何一步延误 X 分钟，出餐就晚 X 分钟**，
这正是"时间余量很小、容易延误"的那个集合。
"""
from __future__ import annotations

from collections import defaultdict

# 余量 <= 此值(分钟)视为"紧贴"。取 0.01: 排程输出保留 1 位小数, 0.0 就是真的无缝。
ZERO_SLACK_EPS = 0.01

# 不占真实资源的伪工位 —— 它们不构成排队, 不能进关键路径的排队边。
NONRES_UNITS = ("等待", "提前准备")

# 单台设备占用率超过此值 -> 报"瓶颈", 对应建议文本 §四.3 的"蒸箱容量不足"。
BOTTLENECK_UTIL = 85.0


def _live(plan) -> list:
    """当天(非提前准备)的任务。t<0 的是前一天备菜, 不在出餐关键路径上。"""
    return [t for t in plan.tasks if t.start_min >= 0]


def _makespan(plan) -> float:
    return round(max(float(plan.makespan_min),
                     max((t.end_min for t in _live(plan)), default=0.0)), 1)


def _chamber_of(unit_id: str) -> str:
    u = unit_id[len("预热-"):] if unit_id.startswith("预热-") else unit_id
    return u.split("-")[0]


def _edges(plan):
    """返回 (preds, succs)，每个值是 [(task_id, start_min)]。

    两类边缺一不可：
      ① **依赖边**：同一道菜里 A 是 B 的前置（depends_on）；
      ② **排队边**：同一工位上 A 紧挨在 B 前面 —— 容量为 1 时这条边才是真正的
         "排队"约束，只看依赖会漏掉设备瓶颈。
    """
    live = _live(plan)
    by_id = {t.task_id: t for t in plan.tasks}
    preds: dict = defaultdict(list)
    succs: dict = defaultdict(list)
    for t in live:
        for d in (t.depends_on or []):
            if d in by_id:
                preds[t.task_id].append((d, by_id[d].start_min))
                succs[d].append((t.task_id, t.start_min))
    by_unit: dict = defaultdict(list)
    for t in live:
        if t.unit_id in NONRES_UNITS:
            continue
        by_unit[t.unit_id].append(t)
    for _unit, group in by_unit.items():
        group = sorted(group, key=lambda x: (x.start_min, x.end_min))
        for a, b in zip(group, group[1:]):
            preds[b.task_id].append((a.task_id, a.start_min))
            succs[a.task_id].append((b.task_id, b.start_min))
    return preds, succs


def _zero_slack_chain(plan, tail, preds, by_id) -> list:
    """从 tail 往回走，每步都要求"前一步结束 == 本步开始"（容差内）。

    ⚠ 这个定义在**到处都有对齐空等**的计划里会退化：只要尾步前面留了 1 分钟
    对齐缓冲，链就断在尾步上（实测某 5 菜实例退化成长度 1）。所以它只是首选，
    断了就由 `_longest_duration_chain` 兜底。
    """
    chain, cur, guard = [], tail.task_id, 0
    while cur is not None and guard <= len(plan.tasks) + 1:
        guard += 1
        if cur in chain:                     # 成环: 立刻停, 绝不死循环
            break
        chain.append(cur)
        t = by_id[cur]
        nxt = None
        for pid, _ps in preds.get(cur, ()):
            p = by_id.get(pid)
            if p is None:
                continue
            if abs(p.end_min - t.start_min) <= ZERO_SLACK_EPS:
                if nxt is None or p.end_min > by_id[nxt].end_min:
                    nxt = pid
        cur = nxt
    chain.reverse()
    return chain


def _longest_duration_chain(live, preds, by_id) -> list:
    """按**任务时长累加**的最长链（教材式关键路径），作为零余量链的兜底。

    它回答的是"哪条工序链本身最长"，不考虑对齐空等，因此长度恒 <= makespan。
    """
    length: dict = {}
    prev: dict = {}
    onstack: set = set()

    def _acc(tid: str) -> float:
        if tid in length:
            return length[tid]
        if tid in onstack:
            return 0.0                        # 成环: 退化为 0, 绝不死循环
        onstack.add(tid)
        best, bp = 0.0, None
        for pid, _ps in preds.get(tid, ()):
            v = _acc(pid)
            if v > best:
                best, bp = v, pid
        onstack.discard(tid)
        length[tid] = best + float(getattr(by_id[tid], "duration_min", 0.0) or 0.0)
        prev[tid] = bp
        return length[tid]

    tail = max((t.task_id for t in live), key=_acc)
    chain, cur = [], tail
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = prev.get(cur)
    chain.reverse()
    return chain


def critical_path(plan) -> dict:
    """关键路径：决定出餐时刻的那条链。

    优先用**零余量链**（语义最干净：链上任何一步延误 X 分钟，出餐就晚 X 分钟）；
    当计划里到处是对齐空等、零余量链退化成长度 1 时，退回**最长时长链**。
    `method` 字段显式说明用了哪一种，避免读的人拿两种口径互相对不上。
    """
    live = [t for t in _live(plan) if t.unit_id not in NONRES_UNITS]
    mk = _makespan(plan)
    if not live:
        return {"task_ids": [], "dishes": [], "span_min": 0.0, "makespan_min": mk,
                "method": "none", "note": "当天无设备/人工任务"}
    by_id = {t.task_id: t for t in plan.tasks}
    preds, _succs = _edges(plan)

    # 决定出餐时刻的任务 = 结束最晚的那个（并列时取开始更晚的）
    tail = max(live, key=lambda t: (t.end_min, t.start_min))
    chain = _zero_slack_chain(plan, tail, preds, by_id)
    method = "zero_slack"
    if len(chain) < 2:
        alt = _longest_duration_chain(live, preds, by_id)
        if len(alt) > len(chain):
            chain, method = alt, "longest_duration"
    first = by_id[chain[0]] if chain else tail
    span = round(mk - max(0.0, first.start_min), 1)
    note = ("零余量链：链上任何一步延误 X 分钟，出餐就晚 X 分钟。"
            if method == "zero_slack" else
            "最长工序链（计划里对齐空等较多，零余量链退化成长度 1，故退回本口径）："
            "链长 = Σ 任务时长，恒 <= 总时长。")
    return {
        "task_ids": chain,
        "dishes": sorted({by_id[i].dish for i in chain if getattr(by_id[i], "dish", "")}),
        "span_min": span,
        "makespan_min": mk,
        "method": method,
        "note": note,
    }


def risks(plan, priors: dict | None = None) -> list:
    """风险项清单。每条: {level, kind, task_id, dish, message}。

    level: `high` = 硬约束/物理约束被违反（红色预警）；
           `mid` = 需要留意（关键路径无缓冲 / 瓶颈设备 / 保热超时）。

    ⚠ 刻意**不做"逐任务余量为 0 就报警"**：紧凑排程里背靠背本来就是优化目标，
    那样报出来的 14 项里 13 项是噪声，真风险会被淹没（第一版就是这么翻车的）。
    这里只报**集合级**的结论：关键路径、瓶颈设备、保热超时。
    """
    out: list = []
    by_id = {t.task_id: t for t in plan.tasks}
    mk = _makespan(plan)

    # ---- 1) 降温: 同腔体异温背靠背 (建议文本 §一.3 / §三.2) ----
    try:
        from app.scheduler.metrics import device_switches
        for ch, a, b, need in device_switches(plan):
            ta, tb = by_id.get(a), by_id.get(b)
            gap = (tb.start_min - ta.end_min) if (ta and tb) else 0.0
            out.append({
                "level": "high", "kind": "cooldown", "task_id": b,
                "dish": getattr(tb, "dish", ""),
                "message": (f"{ch} 从 {ta.temp_c:.0f}℃ 切到 {tb.temp_c:.0f}℃，"
                            f"只留 {gap:.1f} 分钟降温（需要 {need:.1f} 分钟）"),
            })
    except Exception:
        pass

    # ---- 2) 硬约束冲突 (建议文本 §四.3: 优先告警 + 给出原因) ----
    try:
        from app.scheduler.checker import check_plan
        chk = check_plan(plan)
        for d in chk.get("details", []):
            out.append({"level": "high", "kind": "conflict", "task_id": "",
                        "dish": "", "message": str(d)})
    except Exception:
        pass

    # ---- 3) 关键路径: 整条链无缓冲 (建议文本 §三.3 / §七.1) ----
    try:
        cp = critical_path(plan)
        if len(cp["task_ids"]) >= 2:
            hands = sum(1 for i in cp["task_ids"]
                        if getattr(by_id.get(i), "needs_human", False))
            head = (f"关键路径 {len(cp['task_ids'])} 步全程无缓冲（其中 {hands} 步要人工），"
                    f"任一延误直接推后出餐：" if cp["method"] == "zero_slack" else
                    f"最长工序链 {len(cp['task_ids'])} 步共 {cp['span_min']:.0f} 分钟"
                    f"（计划里对齐空等较多，零余量链退化成长度 1）：")
            out.append({
                "level": "mid", "kind": "critical", "task_id": "", "dish": "",
                "message": (head + " → ".join(cp["task_ids"][:6])
                            + ("…" if len(cp["task_ids"]) > 6 else "")),
            })
    except Exception:
        pass

    # ---- 4) 瓶颈设备 (建议文本 §四.3: "蒸箱容量不足 / 操作员不足") ----
    try:
        busy: dict = defaultdict(float)
        span: dict = {}
        for t in _live(plan):
            if t.unit_id in NONRES_UNITS:
                continue
            ch = _chamber_of(t.unit_id)
            busy[ch] += float(t.duration_min or 0.0)
            span[ch] = max(span.get(ch, 0.0), t.end_min)
        from app.scheduler.engine import resource_units
        units = resource_units()
        for ch, b in busy.items():
            cap = units.get(ch, 1) or 1
            if cap <= 0 or mk <= 0:
                continue
            util = b / (cap * mk) * 100.0
            if util >= BOTTLENECK_UTIL:
                out.append({
                    "level": "mid", "kind": "bottleneck", "task_id": "", "dish": "",
                    "message": (f"{ch} 占用率 {util:.0f}%（{cap} 个工位）接近饱和，"
                                f"再加菜就会排队顶后出餐"),
                })
    except Exception:
        pass

    # ---- 5) 保热: 必须趁热的菜出锅后要干等太久 (建议文本 §二.2 品质窗口) ----
    if priors:
        try:
            from app.scheduler.metrics import dish_cook_finish
            fin_of = dish_cook_finish(plan)
            for dish, fin in (plan.dish_finish or {}).items():
                p = priors.get(dish)
                if not p:
                    continue
                wait = max(0.0, mk - fin_of.get(dish, fin))
                hold = float(getattr(p, "hold_min", 15.0) or 15.0)
                if float(getattr(p, "serve_hot", 0.0) or 0.0) >= 0.85 and wait > hold:
                    out.append({
                        "level": "mid", "kind": "hot_wait", "task_id": "",
                        "dish": dish,
                        "message": (f"{dish} 出锅后要等 {wait:.0f} 分钟才开饭，"
                                    f"超过可放时长 {hold:.0f} 分钟，口感会掉"),
                    })
        except Exception:
            pass

    out.sort(key=lambda r: (0 if r["level"] == "high" else 1, r["kind"], r["task_id"]))
    return out


def analyze(plan, priors: dict | None = None) -> dict:
    """一次给全: 关键路径 + 风险清单 + 一句话结论。任何异常都不该影响排程返回。"""
    try:
        cp = critical_path(plan)
        rs = risks(plan, priors=priors)
    except Exception as exc:                      # 诊断失败不能拖垮排程
        return {"error": f"诊断失败(已忽略): {exc}", "critical_path": None,
                "risks": [], "high": 0, "mid": 0, "summary": "诊断不可用"}
    hi = sum(1 for r in rs if r["level"] == "high")
    mid = len(rs) - hi
    if not rs:
        summary = "无风险项：硬约束、设备降温、时间余量、保热窗口全部满足。"
    else:
        bits = []
        if hi:
            bits.append(f"{hi} 项红色预警（硬约束/物理约束）")
        if mid:
            bits.append(f"{mid} 项提示（关键路径/瓶颈/保热）")
        summary = "；".join(bits) + "。"
    return {"critical_path": cp, "risks": rs, "high": hi, "mid": mid, "summary": summary}
