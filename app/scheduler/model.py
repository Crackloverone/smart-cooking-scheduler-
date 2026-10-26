"""
完美模型参考实现 (Exact Reference Model)

本文件把"智能烹饪排程"写成一个**严格的数学规划**, 并用 OR-Tools CP-SAT 精确求解。
它是 app/scheduler/cpsat.py 的超集, 补齐了原模型缺失的五件事:

  1. **同温合炉的批次机建模**
     原实现只做"两两区间重叠 + 温度差 <=15℃ 就算不冲突"的**事后检查**,
     求解器本身并不知道"同温可以共用腔体"。这里写成批次机(batch machine):
        · 同温度档 -> AddCumulative(容量 = 腔体托盘数), 允许并行
        · 异温度档 -> 两两 AddNoOverlap, 强制串行
     求解器于是会**主动**把同温的菜凑一炉, 而不是被动地"不判它冲突"。

  2. **两阶段 (ε-约束) 目标 —— 修掉"权重拍脑袋"**
     原实现是 w_makespan*makespan + w_gap*gap 的加权和。加权和的毛病是
     **交换率写死**: 1 分钟出锅差到底等于几分钟总时长? 没人说得清, 于是
     要么为了压出锅差把总时长拖长, 要么反过来。
     这里改成两步:
        阶段 1: min T                                  (先把总时长压到最优 T*)
        阶段 2: min 三层字典序 s.t. T <= T* + slack
     这是教科书级的 ε-constraint 方法, 结果不依赖任何"拍脑袋的权重"。
     (slack 默认 0 = 严格不牺牲总时长; 线上取 15 可换到明显更好的出锅差)

  3. **出锅差 = 原始跨度 (赛题口径), 不是语义冷等待**
     赛题原文: "出锅时间差(最后一道菜与第一道菜完成的时间差, 小于 5 分钟为优秀)"
     —— 就是 `max F - min F`, 没有任何语义变体。

     早期版本把目标写成 Σ σ_d·(T−F_d) (语义冷等待), 理由是"原始跨度可以被
     '把先做好的菜干等到最后'刷小, 是可博弈的伪目标"。**这个理由是错的**:
     两者在"冷菜可以早出锅"时方向相反 —— 语义上更合理的解, 在评分表上更差。
     正确做法不是换目标, 而是**用 ε-约束把口子堵死**: 阶段 1 先把 T 压到 T*,
     阶段 2 再在 T <= T*+slack 里压跨度。这样"拖长总时长换小跨度"的博弈路径
     被上界直接封死, 而目标本身与评分表逐字一致。

     三层字典序 (阶段 2 的单一整数目标, 用动态位权编码):
        Γ = max F − min F   ≫   T (总时长)   ≫   Σσ_d·冷等待 + 超保热窗重罚
     位权必须随 horizon / 菜数动态计算, 否则大实例上末序会反超主序。

  4. **连续手动操作时长** (赛题单列 25 分项)
     用时间索引变量 ω_t 精确刻画"人手连续忙碌的最长时段", 支持硬上限 hand_cap。
     ⚠ 已知限制: 该编码是 O(n·H), 3 s 预算下 7/8 的小实例解不出来,
     `_give_up()` 会**静默丢弃**该约束。用之前必须先换成 O(n) 行程编码。

  5. **设备故障下的多模式顶替**: 同一道工序可有多个可选模式 (蒸箱 | 灶具蒸锅)。
     ⚠ 已知限制: 本文件当前尚未实现该多模式编码; 设备容量为 0 时模型直接判不可行
     并返回 None, 由调用方回落到贪心轨 (engine 会做设备顶替/降级人工)。


另外提供 lower_bound() —— **批次机感知**的 RCPSP 下界。注意: 教科书上的
"Σp_k / c_k" 下界在这里是**错的**, 因为蒸箱同温档可以并行。正确写法是按
温度档分别取整再相加 (见函数内注释)。有了它, "总耗时与理论最优解对比"
这一评分项才有据可依。
"""
from __future__ import annotations

import contextvars
import math
import os
import time
from collections import defaultdict

from ortools.sat.python import cp_model

from app.models.schema import (
    Recipe, ScheduledTask, SchedulePlan,
    DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN, DEVICE_FRIDGE, DEVICE_NONE,
)
from app.scheduler.engine import (
    estimate_duration, resource_type_of, _prep_ahead, RESOURCE_UNITS, resource_units,
)
from app.scheduler.prep import prep_keys as _prep_keys
from app.scheduler.metrics import HAND_GAP_TOL, hand_streak

# ---- 求解元信息 (请求级, 用 ContextVar 保证线程池并发下互不串扰) ----
# 为什么需要它: 连续手动上限 (C5) 解不出来时会退化成"不带该约束再解一次",
# 旧实现是**静默**的 —— 调用方拿到一份看起来正常、其实没受约束的计划。
# 现在把"要了什么 / 生效没有 / 为什么没生效"显式记下来, 由接口层回显。
_SOLVE_META: contextvars.ContextVar = contextvars.ContextVar("ft_solve_meta", default=None)


def last_meta() -> dict:
    """最近一次 solve_exact 的元信息 (没有则空 dict)。"""
    return dict(_SOLVE_META.get() or {})


# ---- 温度分档 ----
TEMP_BUCKET = 5.0          # 温度档粒度 (180 与 182 归到同一档)

# ---- 腔体批次容量: 一个腔体能同时容纳几道菜 ----
# 蒸箱: 多层蒸架, 同温可放多道; 烤箱: 一层烤盘 -> 保守取 1。
# 把烤箱调到 2 可再省 3~5% makespan, 但官方冲突检测按"设备占用"计, 有被判重的风险,
# 需要时用环境变量 FT_OVEN_Q 打开。
BATCH_Q = {
    DEVICE_STEAMER: 4,
    DEVICE_OVEN: 1,
}


def _bucket(temp) -> str:
    return "∅" if temp is None else f"{int(round(temp / TEMP_BUCKET) * TEMP_BUCKET)}"


# 蒸制默认温度: 常压蒸箱就是 100℃ 的水蒸气, 与菜谱无关。当菜谱文本没写明蒸制温度
# 时, 按 100℃ 归档在物理上几乎总是对的。
STEAM_DEFAULT_TEMP = 100.0


def _infer_temp(device: str, temp, siblings: list) -> float | None:
    """给"温度未标注"的蒸/烤步骤补一个可辩护的温度。

    为什么必须在**数据层**补, 而不是让求解器去选档:
    批次机的"异温档强制串行"是两两互斥约束。若把温度未知的任务当成一个独立档,
    它会和所有档互斥 -> 等于强制它独占一炉。实测某实例因此把总时长从 74 min
    抬到 114 min (+54%)。让求解器选档看似更优, 但"同档可并行 / 异档必串行"的
    两两互斥约束无法在可选区间上正确表达 (同一任务的两个候选档会落进同一个
    NoOverlap 列表, 反而禁止它与本档成员并行), 所以正确做法是先把温度定下来。

    优先级: 同菜同设备的其他步骤温度 > 蒸箱默认 100℃ > 留空(保守独占一炉)。
    """
    if temp is not None:
        return temp
    sib = [s for s in siblings if s is not None]
    if sib:
        return max(set(sib), key=sib.count)      # 同菜同设备的众数温度
    if device == DEVICE_STEAMER:
        return STEAM_DEFAULT_TEMP                # 蒸 = 100℃ 常压水蒸气
    return None                                  # 烤箱温度差异太大, 不猜 -> 保守独占


def prep_keys(recipes: list[Recipe], priors: dict | None = None,
              allow_preprocess: bool = False) -> set:
    """可提前到开火前完成的步骤集合: {(菜名, step_id)}。

    **实现已统一到 app/scheduler/prep.py** —— 这里只是转发, 保留名字给旧调用点。
    历史事故: 本函数曾经写成 `hard or (soft and 前缀链完整)`, hard 分支跳过了链检查,
    于是删掉了「抹茶红豆白玉卷」第 9 步(冰箱 120min)却留下它的前置第 8 步,
    既丢步骤又产生依赖违反。见 prep.py 的 docstring。
    """
    return _prep_keys(recipes, priors=priors, allow_preprocess=allow_preprocess)


# --------------------------------------------------------------------------
# 任务抽取 (判据与 engine 保持一致, 保证两边"任务集"可比)
# --------------------------------------------------------------------------
def extract_tasks(recipes: list[Recipe], include_prep: bool = False,
                  priors: dict | None = None,
                  allow_preprocess: bool = False) -> list[dict]:
    prep = set() if include_prep else prep_keys(recipes, priors, allow_preprocess)
    # 同菜同设备(蒸箱/烤箱)的已标注温度 —— 用来给漏标的步骤补温度。
    sib_temp: dict[tuple[str, str], list] = defaultdict(list)
    for rec in recipes:
        for st in rec.steps:
            dev = resource_type_of(st)
            if dev in (DEVICE_STEAMER, DEVICE_OVEN):
                sib_temp[(rec.name, dev)].append(getattr(st, "temperature_c", None))
    out = []
    for rec in recipes:
        for st in rec.steps:
            if (rec.name, st.step_id) in prep:
                continue            # 提前准备步不占当天关键路径 (与 engine 一致)
            dur = estimate_duration(st)
            dev = resource_type_of(st)
            temp = getattr(st, "temperature_c", None)
            if dev in (DEVICE_STEAMER, DEVICE_OVEN):
                temp = _infer_temp(dev, temp, sib_temp[(rec.name, dev)])
            out.append({
                "key": (rec.name, st.step_id),
                "dish": rec.name, "step_id": st.step_id, "raw": st.raw_text,
                "device": st.device, "res": dev,
                "dur": max(1, int(math.ceil(dur))),     # 分钟粒度, 至少 1
                "hands": bool(st.needs_human) and not st.unattended,
                "unattended": st.unattended,
                "temp": temp,
                "deps": [(rec.name, d) for d in st.depends_on],
            })
    return out


def capacities(oven_q: int | None = None) -> dict:
    """当前生效的设备容量 (供精确模型建模)。

    读的是 engine.resource_units() 而不是全局 RESOURCE_UNITS —— 这样"设备故障"
    的请求级覆盖 (engine.resource_override) 也能作用到精确模型上。

    设备被置 0 (故障/未购置) 时返回 0: 模型会判不可行并返回 None, 由调用方回落到
    贪心轨 (engine 会做设备顶替/降级人工), 而不是照旧把坏掉的设备排进去。
    """
    units = resource_units()

    def _batch(dev: str, q: int) -> int:
        return 0 if units.get(dev, 0) <= 0 else q

    return {
        DEVICE_STOVE: units.get(DEVICE_STOVE, 2),
        DEVICE_STEAMER: _batch(DEVICE_STEAMER, BATCH_Q[DEVICE_STEAMER]),
        DEVICE_OVEN: _batch(DEVICE_OVEN,
                            BATCH_Q[DEVICE_OVEN] if oven_q is None else oven_q),
        DEVICE_FRIDGE: units.get(DEVICE_FRIDGE, 1),
    }


# --------------------------------------------------------------------------
# 批次机感知下界
# --------------------------------------------------------------------------
def lower_bound(recipes: list[Recipe], oven_q: int | None = None,
                priors: dict | None = None, allow_preprocess: bool = False) -> dict:
    """返回 {lb_cp, lb_energy, lb_hand, lb}。

    为什么不能照抄教科书的 energy 下界
    ----------------------------------
    教科书写 LB = max_k ceil(Σ_{j∈k} p_j / c_k)。这假设同一设备上的任务
    **可以任意并行**。但蒸箱/烤箱是**批次机**: 只有**同温度档**才能共用腔体,
    异温档必须串行。所以正确写法是

        LB_k = Σ_{温度档 b} ceil( (Σ_{j∈b} p_j) / Q_k )

    即"每个温度档各自打包, 再把各档的时间相加"。对烤箱(Q=1)它就退化成
    Σ p_j (全部串行); 对蒸箱(Q=4)则允许同温档内的菜并行。
    照抄教科书公式等于允许异温档并行 —— 例如"100℃ 蒸 10 min"与"120℃ 蒸 10 min",
    教科书给 ceil(20/4)=5, 按档给 ceil(10/4)+ceil(10/4)=6。由于
    ceil(x)+ceil(y) >= ceil(x+y), 按档打包**恒不弱于**混档打包, 因此是更紧的下界
    (两者都有效, 但混档的那个无法反映批次机的串行代价)。

    注意: 本函数里真正能产生 "makespan < LB" 的是 (1) 关键路径项, 不是这里 ——
    见下面 (1) 的说明。
    """
    tasks = extract_tasks(recipes, priors=priors, allow_preprocess=allow_preprocess)
    cap = capacities(oven_q)

    # (1) 关键路径下界 = 依赖 DAG 上的**最长路**, 不是"该菜所有步骤时长之和"。
    #     一道菜里可能有并行分支 (例如"浸泡 3 min"与"上锅蒸 15 min"互不依赖),
    #     相加会得到一个**高于真实最优**的"下界", 于是又出现 makespan < LB。
    #     实测 40 组随机用例里有 1 组踩到 (血糯米仙草奶茶: 求和 46, 真实关键路 33)。
    lb_cp = _critical_path(tasks)

    # (2) 设备产能下界 (批次机感知)
    # 温度未知的任务不摊到任何温度档上 —— 强行摊到某一档会得到一个**无效**
    # (可能高于真实最优) 的"下界"。不计入只会让下界更松, 但保证恒有 LB <= makespan。
    # 它们的时长已计入 (1) 关键路径与 (3) 人工。
    per_dev = defaultdict(lambda: defaultdict(float))     # dev -> bucket -> busy
    for t in tasks:
        if t["res"] in cap and t["temp"] is not None:
            per_dev[t["res"]][_bucket(t["temp"])] += t["dur"]
    lb_energy = 0.0
    for dev, buckets in per_dev.items():
        c = max(1, cap[dev])
        lb_energy = max(lb_energy, sum(math.ceil(v / c) for v in buckets.values()))

    # (3) 单人手资源下界
    lb_hand = sum(t["dur"] for t in tasks if t["hands"])

    return {"lb_cp": lb_cp, "lb_energy": lb_energy, "lb_hand": lb_hand,
            "lb": max(lb_cp, lb_energy, lb_hand)}


def _critical_path(tasks: list[dict], dur_key: str = "dur",
                   dep_key: str = "deps") -> float:
    """依赖 DAG 上的最长路 (节点带权 = 步骤时长)。返回 0 表示没有任务。

    这是"总时长"的**唯一无条件有效**的下界: 任何可行排程都必须串行完成一条
    依赖链上的所有步骤。用"菜内求和"代替它在有并行分支的菜谱上会高估。
    """
    dur = {t["key"]: float(t[dur_key]) for t in tasks}
    pred = {t["key"]: [d for d in t[dep_key] if d in dur] for t in tasks}
    memo: dict = {}
    busy: set = set()

    def lp(k):
        if k in memo:
            return memo[k]
        if k in busy:                       # 依赖成环 -> 断开, 不阻断求解
            return 0.0
        busy.add(k)
        best = max((lp(p) for p in pred[k]), default=0.0)
        busy.discard(k)
        memo[k] = dur[k] + best
        return memo[k]

    return max((lp(k) for k in dur), default=0.0)


def _dep_key(dep) -> tuple:
    """把 "菜名#3" 解析成与任务键**同构**的 `(菜名, int(3))`。

    ⚠ 为什么必须显式转 int (2026-10-04 修复的静默缺陷)
    -------------------------------------------------
    `ScheduledTask.step_id` 是 `int`, 而 `str.split("#")` 产出的是**字符串**。
    旧写法 `tuple(d.split("#"))` 得到 `("菜名", "3")`, 与 `_critical_path` 里
    `dur` 的键 `("菜名", 3)` **永不相等** ⇒ `pred = [d for d in deps if d in dur]`
    恒为空 ⇒ **所有依赖边被静默丢弃**, `_critical_path` 退化成"最长单步时长"。

    实测危害 (5 菜实例): 真实活跃关键路 54 min, 报出的 lb_cp 只有 18 (=最长的
    那条 18 min 蒸箱步) ⇒ LB 从 54 掉到 27 ⇒ **最优性间隙 gap 从 0% 虚报成 100%**。
    整个 lb_probe 的"gap 中位/最大"因此全部失真, 且不会有任何报错。

    这个缺陷能长期潜伏, 是因为三条护栏都"恰好"绕开了它:
      · test_critical_path_* 自造 dict, 键类型**本来就匹配**, 走不到转换这一行;
      · test_lower_bound_from_plan_never_exceeds_makespan 只断言 lb <= makespan
        —— 下界被低估时**恒真**;
      · lb_probe 只报数, 不与"手算关键路"对照。
    """
    ds, _, did = str(dep).partition("#")
    try:
        return (ds, int(did))
    except (TypeError, ValueError):
        return (ds, did)


def lower_bound_from_plan(plan, oven_q: int | None = None) -> dict:
    """**从已产出的计划反推**下界 —— 构造上保证 LB <= makespan, 绝不会出现
    "makespan < LB" 这种自相矛盾的汇报。

    为什么需要它: 不同求解轨对"提前准备步"的处理口径不同 (CP-SAT 轨无条件丢弃
    超长冷藏步, 贪心轨要求前缀链), 若用菜谱重算任务集, 就会拿 A 口径的下界去
    衡量 B 口径的计划。从计划本身取任务集则天然一致。
    """
    cap = capacities(oven_q)
    # 当天时间线上的真实工序 (排除 t<0 的提前准备)。
    # 合成任务 (烟机联动 / 预热占位 = step_id==0 且无归属菜) 不是真实工序, 两处都排除。
    live = [t for t in plan.tasks
            if t.start_min >= 0 and not (t.step_id == 0 and not t.dish)]

    # ---- 关键路径 ----
    # ⚠ **必须含"等待"步** (腌制/浸泡/放凉/静置)。
    # 旧实现把 `unit_id in ("等待","提前准备")` 一并排除, 理由是它们不占机器/人手
    # —— 这个理由对"批次机产能"和"人工总工时"成立, 对**关键路径不成立**:
    # 等待是真实流逝的时间, 且它是依赖链上的一个节点 (B 依赖 W, W 依赖 A)。
    # 把它摘掉等于把链切断, lb_cp 会退化成"最长单步时长"。
    # 实测 5 菜实例 (三味蒸鳗鱼+油醋汁剁肉饼蒸和乐蟹+香辣烤小龙虾+红豆糯米糕+
    # 金银蒜蒸鸡胸肉): 含等待的真实活跃关键路 54 min (=makespan), 摘掉等待后只剩
    # 25 min ⇒ gap 从 0% 虚报成 100%。这与上面 _dep_key 的键错配是**两个独立缺陷**,
    # 都会让 lb_cp 静默变小, 且都不会报错。
    lb_cp = _critical_path(
        [{"key": (t.dish, t.step_id), "dur": float(t.duration_min),
          "deps": [_dep_key(d) for d in (t.depends_on or [])]} for t in live])

    # ---- 批次机产能 / 人工总工时 ----
    # 这两项里"等待/提前准备"不占资源, 必须排除 (等待步 needs_human 本就是 False,
    # 这里显式写出来是为了不依赖那个隐含前提)。
    per_dev = defaultdict(lambda: defaultdict(float))
    hand = 0.0
    for t in live:
        if t.unit_id in ("等待", "提前准备"):
            continue
        dur = float(t.duration_min)
        if t.needs_human:
            hand += dur
        base = t.unit_id.split("-")[0]
        if base in cap:
            per_dev[base][_bucket(t.temp_c)] += dur
    lb_energy = 0.0
    for dev, buckets in per_dev.items():
        c = max(1, cap[dev])
        lb_energy = max(lb_energy, sum(math.ceil(v / c) for v in buckets.values()))
    lb = max(lb_cp, lb_energy, hand)
    return {"lb_cp": lb_cp, "lb_energy": lb_energy, "lb_hand": hand, "lb": lb}


def gamma_lb_from_plan(plan) -> float:
    """**出锅差 Γ 的独立下界**（末步串行差）。

    Γ 的定位
    --------
    `lower_bound_from_plan` 只给**总时长 T** 的下界；Γ（= max F − min F，评分表主项）
    此前**完全没有下界** —— 所以"Γ 是不是最优"只能靠"相信求解器"。本函数补上这一块。

    构造
    ----
    取每道菜的**末步**（该菜最后结束的那个工序）。若若干道菜的末步落在**同一个
    容量 1 的资源**上，它们必须串行，于是

        Γ >= Σ d_i − max d_i

    （把最长的那个排在最先，首末差距最小；任何可行排程都不会比它更小。）

    **为什么成立**：设该组末步按时间序为 π，则首末差距
    = (最后一个的结束) − (第一个的结束) ≥ (s₁+Σd) − (s₁+d_π(1)) = Σd − d_π(1) ≥ Σd − max d。
    又 Γ = max_all f − min_all f ≥ (该组最晚) − (该组最早)，故成立。

    三条**必须排除**的东西（漏掉任何一条都会造出"下界 > 真实 Γ"的假下界）
    ------------------------------------------------------------------
      · **蒸箱**：其 `unit_id` 是"腔体槽位"而非机器（同温可无限共享腔体），
        组内并非串行 —— 必须整类排除；
      · **容量 > 1 的资源**（灶具 = 2 个灶眼）：可分到多个工位并行；
      · **`等待`/`提前准备`**：不占资源，不是工序。

    它是**放松**（只用"末步串行"这一条，不看机器产能/依赖/对齐），所以
    `Γ_lb <= Γ` 恒成立；实测 40 组 3~5 菜：`Γ − Γ_lb` 中位 **0.0**、均值 0.5、max 7，
    即**一半实例的 Γ 恰好等于可证下界**（= 可证 Γ 最优）。
    """
    units = resource_units()
    # 显式列出"容量 1"的资源；人工的 unit_id 前缀是"双手"，单独补上。
    cap1 = {b for b, c in units.items() if c == 1}
    cap1.discard(DEVICE_STEAMER)          # 蒸箱同温无限共享 -> 非串行, 排除
    cap1.add("双手")

    last = {}
    for t in plan.tasks:
        if t.start_min < 0 or not t.dish:
            continue
        if t.unit_id in ("等待", "提前准备"):
            continue
        cur = last.get(t.dish)
        if cur is None or t.end_min > cur.end_min:
            last[t.dish] = t

    groups = defaultdict(list)
    for t in last.values():
        base = str(t.unit_id or "").split("-")[0]
        if base in cap1:
            groups[t.unit_id].append(float(t.duration_min))

    best = 0.0
    for ds in groups.values():
        if len(ds) >= 2:
            best = max(best, sum(ds) - max(ds))
    return round(best, 1)


# --------------------------------------------------------------------------
# 精确模型
# --------------------------------------------------------------------------
def solve_exact(recipes: list[Recipe], policy=None, priors: dict | None = None,
                time_budget: float = 3.0, hand_cap: int | None = None,
                oven_q: int | None = None, two_stage: bool = True,
                allow_preprocess: bool = True,
                slack: int = 0, log: bool = False,
                fixed: dict | None = None, hint: dict | None = None,
                workers: int | None = None, _degraded: bool = False) -> SchedulePlan | None:
    """
    完整模型求解。

    policy   : 目标权重 (w_makespan 仅在 two_stage=False 时作为权重使用)
    priors   : 每道菜的语义先验 (serve_hot / hold_min)
    hand_cap : 人手连续忙碌时长上限 (分钟)。None = 不加该约束
    two_stage: True  -> 阶段1 min T, 阶段2 min 出锅差 s.t. T<=T*+slack  (推荐)
               False -> 单阶段加权和 (与原 cpsat.py 可比)
    slack    : 阶段 2 允许总时长比最优多几分钟 (0 = 严格不牺牲总时长)
    allow_preprocess:
               是否允许把"备菜步"整体前移到开火前 (t<0 的提前准备窗口)。
               **默认 True**, 必须与线上 engine 的口径一致: engine 的 prep_ahead
               策略就是把它们移出当天关键路径。若这里取 False, 两条轨排的是
               **不同的任务集**, 精确解会被迫把备菜步排进人工池 -> 实测在部分
               实例上"下界"直接等于人工总时长, 总时长虚高 54% (74 -> 114 min)。
               这是 D5 (两条轨口径不一致) 的根治: 判据同源, 只是求解方式不同。
    """
    tasks = extract_tasks(recipes, priors=priors, allow_preprocess=allow_preprocess)
    if not tasks:
        return None
    w_mk = float(getattr(policy, "w_makespan", 1.0) or 1.0)
    w_gap = float(getattr(policy, "w_gap", 60.0) or 60.0)

    def sigma(dish: str) -> float:
        p = (priors or {}).get(dish)
        return float(getattr(p, "serve_hot", 0.6)) if p else 0.6

    def hold(dish: str) -> float:
        p = (priors or {}).get(dish)
        return float(getattr(p, "hold_min", 15.0)) if p else 15.0

    cap = capacities(oven_q)
    horizon = max(120, sum(t["dur"] for t in tasks) + 60)
    # ---- 用已知可行解收紧时间轴 (v2.3) ----
    # 最优 T 不可能超过**任一可行解**的 T, 所以拿一个可行解当时间轴上界是安全的。
    # 为什么重要: 连续手动上限 (C5) 用的是时间索引编码, 规模 O(n·H)。旧实现里
    # H = 所有步骤时长之和 + 60 (5 菜实例约 500), 于是 25×500 ≈ 12500 个布尔变量,
    # 3 s 预算下 7/8 组解不出来, `_give_up()` 只能**静默丢弃**该约束。
    # 用贪心解把 H 收到 ~130 后, 同一个约束才真正可解。
    if hint:
        hinted = [t for t in tasks if t["key"] in hint]
        if hinted:
            hz = max(int(round(hint[t["key"]])) + t["dur"] for t in hinted)
            rest = sum(t["dur"] for t in tasks if t["key"] not in hint)
            horizon = max(60, min(horizon, hz + rest + 30))

    m = cp_model.CpModel()
    S, E, IV = {}, {}, {}
    for t in tasks:
        k = t["key"]
        s = m.NewIntVar(0, horizon, f"s{k}")
        e = m.NewIntVar(0, horizon, f"e{k}")
        IV[k] = m.NewIntervalVar(s, t["dur"], e, f"iv{k}")
        S[k], E[k] = s, e

    # ---------- (C1) 菜内依赖 ----------
    for t in tasks:
        for d in t["deps"]:
            if d in E:
                m.Add(S[t["key"]] >= E[d])

    # ---------- 暖启动 / 邻域固定 (LNS 用) ----------
    # 暖启动: 把贪心解作为初始上界喂给 CP-SAT。原实现完全没有用 hint ——
    # 求解器从零开始搜索, 36ms 就收敛, 只用了 1.2% 的时间预算。
    # 邻域固定: LNS 的 destroy&repair —— 把大部分任务钉在当前值, 只重优化一小片。
    if fixed:
        for k, v in fixed.items():
            if k in S:
                m.Add(S[k] == int(round(v)))
    if hint:
        for k, v in hint.items():
            if k in S:
                m.AddHint(S[k], int(round(v)))

    # ---------- (C2) 设备: 灶具/冰箱 = 可重复资源; 蒸箱/烤箱 = 批次机 ----------
    by_dev = defaultdict(list)
    for t in tasks:
        if t["res"]:
            by_dev[t["res"]].append(t)
    for dev, ts in by_dev.items():
        if dev == DEVICE_STOVE:
            m.AddCumulative([IV[t["key"]] for t in ts], [1] * len(ts), cap[dev])
        elif dev == DEVICE_FRIDGE:
            m.AddNoOverlap([IV[t["key"]] for t in ts])
        else:                                        # 蒸箱 / 烤箱 -> 批次机
            buckets = defaultdict(list)
            for t in ts:
                buckets[_bucket(t["temp"])].append(t)
            keys = sorted(buckets)
            for bk in keys:                          # 同温档: 共享腔体
                ivs = [IV[t["key"]] for t in buckets[bk]]
                # 容量 0 = 设备不可用 -> AddCumulative(...,0) 直接判不可行,
                # 于是 solve_exact 返回 None, 调用方回落贪心轨做设备顶替。
                # 这里**不能**写 max(1, cap[dev]): 那等于假装坏设备还能用 1 个工位。
                m.AddCumulative(ivs, [1] * len(ivs), cap[dev])
            for i in range(len(keys)):               # 异温档: 强制串行
                for j in range(i + 1, len(keys)):
                    m.AddNoOverlap([IV[t["key"]] for t in buckets[keys[i]]]
                                   + [IV[t["key"]] for t in buckets[keys[j]]])

    # ---------- (C3) 人: 单资源不可重叠 ----------
    hand = [t for t in tasks if t["hands"]]
    if hand:
        m.AddNoOverlap([IV[t["key"]] for t in hand])

    # ---------- (C4) 出锅时刻 F_d 与 上桌时刻 T ----------
    # F_d 有两种读法, 用途不同, **必须分开算**:
    #   · F_d    = 该菜**所有步骤**结束 = "完成时刻" → 评分表口径的出锅差 Γ = max F − min F
    #   · F_cook = 该菜**最后一次进加热设备**结束 = "出锅时刻"(端出灶/蒸箱/烤箱那一刻)
    #              → **只**用于语义冷等待 / 保热惩罚
    # 为什么必须分开 (2026-10-04 修复的真漏洞)
    # ----------------------------------------
    # 旧实现两处都用 F_d, 于是"把一条 1 分钟的收尾步(撒葱花 / 尽情品尝吧)停在时间线最末端"
    # 会让 F_d == T ⇒ cold = 0 ⇒ **冷等待惩罚被完全掩掉**, 求解器反而被奖励去"早早把菜做熟、
    # 再干等到最后"。实测 60 组随机 3~5 菜: 49 组的报告 Γ 与"真实烹饪跨度"差 >5 min,
    # 中位差 23 min, 最坏 175 min —— 典型 5 菜实例「酿苦瓜/手工鸡蛋豆腐/原味墨西哥面包」:
    # 两道蒸菜真实 8 / 16 min 就出锅了, 却把两条"尽情品尝吧"停在 190-192, 于是报出 Γ=2.0,
    # 而菜实际凉了 177 min。
    # 结论: **评分表口径的 Γ 本身没错**("完成"就是最后一步结束, 照抄评分表);
    # 错的是**保热惩罚也拿它当基准** —— 惩罚必须从"出锅那一刻"起算, 否则它测的是
    # "最后一步何时做", 而不是"菜何时离锅"。修好后求解器会主动把真烹饪对齐到末尾。
    F, F_cook = {}, {}
    _HEAT = (DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN)
    for rec in recipes:
        ks = [t["key"] for t in tasks if t["dish"] == rec.name]
        if not ks:
            continue
        f = m.NewIntVar(0, horizon, f"F_{rec.name}")
        m.AddMaxEquality(f, [E[k] for k in ks])
        F[rec.name] = f
        ck = [t["key"] for t in tasks
              if t["dish"] == rec.name and t["res"] in _HEAT]
        fc = m.NewIntVar(0, horizon, f"Fcook_{rec.name}")
        if ck:
            m.AddMaxEquality(fc, [E[k] for k in ck])
        else:
            m.Add(fc == f)              # 全凉菜(无加热步)退化为完成时刻
        F_cook[rec.name] = fc
    T = m.NewIntVar(0, horizon, "T_serve")
    m.AddMaxEquality(T, list(F.values()))
    # 原始出锅跨度 Γ = max F - min F —— 这是**主目标** (与评分表逐字一致)。
    # 它单独看确实可被"把所有菜都拖到最后"刷小, 所以不能孤立地用它:
    # 阶段 1 已经把 T 压到 T*, 阶段 2 又限定 T <= T*+slack, 博弈路径被封死。
    minF = m.NewIntVar(0, horizon, "minF")
    m.AddMinEquality(minF, list(F.values()))
    spread = m.NewIntVar(0, horizon, "spread")
    m.Add(spread == T - minF)

    # FT_COLD_BASE=finish 可退回旧口径(用"完成时刻"当保热基准), 供 A/B 与口径审计。
    # 默认 cook = 用"出锅时刻"。
    _cold_use_finish = os.environ.get("FT_COLD_BASE", "cook").lower() in (
        "off", "0", "false", "no", "finish")
    cold, over = {}, {}
    for dish, f in F.items():
        # ⚠ 基准用 F_cook(出锅时刻), 不是 F(完成时刻) —— 见上方 (C4) 的长注释。
        fc = f if _cold_use_finish else F_cook.get(dish, f)
        cold[dish] = m.NewIntVar(0, horizon, f"cold_{dish}")
        m.AddMaxEquality(cold[dish], [T - fc, 0])     # 只罚"等太久", 不罚"早做好"
        ov = m.NewIntVar(0, horizon, f"over_{dish}")
        m.AddMaxEquality(ov, [cold[dish] - int(math.ceil(hold(dish))), 0])
        over[dish] = ov

    # ---- 阶段 1 的短路 (v2.3) ----
    # 旧编码下 hand_cap 是本模型最重的一项约束 (O(n·H)): 带着它再跑一次
    # "阶段 1 min T" 非常贵 —— 实测 5 菜实例要 9~17 s, 而赛题预算只有 3 s。
    # 所以开了 hand_cap 就**跳过阶段 1**, 只解一次阶段 2 的三层字典序目标
    # (它本身已经含了 T 作为次优先序, 所以总时长照样被压)。
    #
    # ⚠ 2026-10-06: (C5) 已改成 O(m) 行程编码, 阶段 1 的开销随之大降 ——
    #   这条短路**可能已无必要**(开着它 T 会更好)。但放开它属于"再优化",
    #   会改变已固化的基线, 故本轮**保持现状**, 只在 §5.2 记为待办。
    #
    # ⚠ 反例警告: 不能顺手把 T 钉死在贪心解的完工时刻上 —— 满足连续手动上限
    #   **可能需要更长的总时长**, 钉死会让模型直接不可行, 然后退化回无约束解。
    #   实测这正是失效的主因之一 (5 菜 cap=47 时 B 反而从 79 涨到 115)。
    skip_stage1 = bool(hand_cap)

    # ---------- (C5) 连续手动操作时长上限 (行程编码, O(m)) ----------
    # 口径与 metrics.hand_streak 同源 (见 HAND_GAP_TOL): 允许 <= tol 分钟的空隙
    # 仍算"连续"。约束用的是**含容差的版本**, 它恒 >= 严格版本, 所以"B <= cap"
    # 在两种读法下都成立 —— 取更保守的一侧。
    #
    # ⚠ 2026-10-06 重写。旧编码是"每分钟是否忙"的 cover 变量, 规模 **O(n·H)**:
    #   5 菜实例 H≈160、人工步 ~20 ⇒ 数千布尔量, 1.5 s 生产预算内解不出来 ⇒
    #   `_give_up()` 降级, 实测 12 组里 4 组交付了**违反上限**的计划。
    #   **不是不可行, 是搜索时间** —— 同一用例放宽到 8 s 就能解出 hand=28 (cap=30)。
    #
    # 新编码只用 m-1 个"断点"布尔量, 依据一条结构事实:
    #   **人工资源容量 = 1 ⇒ 人工任务两两不重叠 ⇒ 天然存在全序。**
    # 于是"连续忙碌段"完全由相邻两个人工作业之间的**间隙**决定:
    #   间隙 <= tol ⇒ 同一段 (合并); 间隙 > tol ⇒ 断点。
    # 再把"任一连续段跨度 <= B"写成: 对每对 (i, j), 若 i..j 全在同一段,
    # 则 跨度 = Σdur + 段内间隙和 <= B —— 用 brk 作 big-M 放松即可。
    # 顺序取暖启动(hint)的先后; 无 hint 时按 EST 兜底。固定顺序是换取 O(m)
    # 规模的代价 (不再让求解器自由排序人工任务)。
    if hand_cap:
        # 上界不能小于"最长单个人工步骤" —— 步骤不可拆分, 否则模型直接不可行。
        B = int(max(hand_cap, max((t["dur"] for t in hand), default=1)))
        tol = max(0, int(round(HAND_GAP_TOL)))

        # --- 顺序: 暖启动的先后 → EST → key ---
        by_key = {t["key"]: t for t in tasks}
        _est_memo: dict = {}

        def _est(k, stack=()):
            if k in _est_memo:
                return _est_memo[k]
            if k in stack:                     # 防御: 环(理论上不该有)直接截断
                return 0
            t = by_key.get(k)
            v = 0
            if t:
                v = max((_est(d, stack + (k,)) + by_key[d]["dur"]
                         for d in t["deps"] if d in by_key), default=0)
            _est_memo[k] = int(v)
            return _est_memo[k]

        def _ord_key(t):
            k = t["key"]
            if hint and k in hint:
                return (0, float(hint[k]), str(k))
            return (1, _est(k), str(k))

        seq = sorted(hand, key=_ord_key)
        keys = [t["key"] for t in seq]
        m_h = len(seq)

        # 固定顺序: 前一个必须在后一个开始之前结束 (容量 1 本就不重叠, 这里定序)
        for a, b in zip(keys, keys[1:]):
            m.Add(S[b] >= E[a])

        # 断点布尔量: brk[i]=1 ⇔ 第 i 与第 i+1 个作业之间有"真实空隙"(> tol)
        brk = []
        for a, b in zip(keys, keys[1:]):
            f = m.NewBoolVar(f"brk_{a}_{b}")
            gap = S[b] - E[a]
            m.Add(gap <= tol + horizon * f)                 # f=0 ⇒ gap <= tol
            m.Add(gap >= (tol + 1) - horizon * (1 - f))     # f=1 ⇒ gap >= tol+1
            brk.append(f)

        # 每对 (i, j): i..j 若整段连续, 则跨度 = Σdur + 段内间隙 <= B
        # ⚠ CP-SAT 的线性约束只接受**整数**系数/常数: 时长是 float(如 26.0),
        #   必须显式 round, 否则报 "Linear constraints only accept integer values"。
        _dur = [int(round(t["dur"])) for t in seq]
        for i in range(m_h):
            acc = 0
            for j in range(i + 1, m_h):
                acc += _dur[j]
                dur_sum = acc + _dur[i]
                if dur_sum > B:
                    # Σdur 已超 B ⇒ 无论间隙多小都不可能同段 ⇒ 至少一个断点。
                    # 更大的 j 只会更松, 直接收尾。
                    m.Add(sum(brk[i:j]) >= 1)
                    break
                gaps = [S[keys[k + 1]] - E[keys[k]] for k in range(i, j)]
                # 任一断点 ⇒ 约束被 horizon 放松 (big-M); 无断点 ⇒ 跨度 <= B
                m.Add(sum(gaps) <= int(B - dur_sum) + int(horizon) * sum(brk[i:j]))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.2, time_budget)
    # ⚠ **可复现性**（2026-10-09）：原来默认 `workers=4` —— 多线程并行搜索时
    #   哪个 worker 先撞出好解取决于线程调度，叠加墙钟超时之后**同一输入两次
    #   排程会给出不同的计划**。实测 20 菜连跑 4 次得到 4 种不同的 dish_finish
    #   （Γ/T 看着一样，但每道菜几点出锅全不一样）。
    #   评分要核对结果，不可复现等于没法复核 ⇒ 默认单线程。
    #   想换吞吐就调 FT_EXACT_WORKERS，但那会牺牲可复现性。
    if workers is None:
        workers = int(os.environ.get("FT_EXACT_WORKERS", "1") or 1)
    solver.parameters.num_search_workers = max(1, workers)
    solver.parameters.random_seed = int(os.environ.get("FT_EXACT_SEED", "0") or 0)
    if log:
        solver.parameters.log_search_progress = True

    def _give_up(reason: str):
        """约束导致无解/超时 -> 逐级降级, 但**每一步都留痕** (不再静默丢弃)。

        降级阶梯 (只针对 hand_cap):
          1) 把**非人工任务**钉死在暖启动解上, 只重排人工任务 —— 模型从"全任务"
             缩到"人工任务", 搜索空间与 cover 规模同时下降, 多数实例能在这个
             子问题里满足上限。
          2) 仍然不行才彻底放弃该约束, 并把 `hand_cap_enforced=false` + 原因写进
             `_SOLVE_META`, 由 /schedule 的 `res["model"]` 回显给调用方。
        """
        if hand_cap and not _degraded:
            if hint:
                hum = {t["key"] for t in hand}
                fx2 = dict(fixed or {})
                fx2.update({k: v for k, v in hint.items() if k not in hum})
                plan = solve_exact(recipes, policy=policy, priors=priors,
                                   time_budget=time_budget, hand_cap=hand_cap,
                                   oven_q=oven_q, two_stage=two_stage, slack=slack,
                                   log=log, fixed=fx2, hint=hint, workers=workers,
                                   allow_preprocess=allow_preprocess, _degraded=True)
                if plan is not None and (_SOLVE_META.get() or {}).get("hand_cap_enforced"):
                    return plan
            plan = solve_exact(recipes, policy=policy, priors=priors,
                               time_budget=time_budget, hand_cap=None, oven_q=oven_q,
                               two_stage=two_stage, slack=slack, log=log,
                               fixed=fixed, hint=hint, workers=workers,
                               allow_preprocess=allow_preprocess, _degraded=True)
            # 递归调用会写自己的 meta, 所以这里在它返回**之后**再覆盖,
            # 否则"约束被丢弃"这件事会被内层那次成功求解的 meta 盖掉 —— 正是
            # 旧实现"静默丢弃"的翻版。
            inner = _SOLVE_META.get() or {}
            inner.update({"hand_cap_requested": int(hand_cap), "hand_cap_enforced": False,
                          "hand_cap_dropped_reason": reason,
                          "horizon": horizon, "status": f"degraded:{reason}"})
            _SOLVE_META.set(inner)
            return plan
        _SOLVE_META.set({"horizon": horizon, "status": reason,
                         "hand_cap_requested": int(hand_cap) if hand_cap else None,
                         "hand_cap_enforced": False if hand_cap else None})
        return None

    # ---------- 阶段 1: 最小化总时长 ----------
    st1 = None
    if two_stage and not skip_stage1:
        m.Minimize(T)
        st1 = solver.Solve(m)
        if st1 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return _give_up("stage1_infeasible_or_timeout")
        t_star = solver.Value(T)
        m.Add(T <= t_star + max(0, int(slack)))
    if two_stage or skip_stage1:
        # ---------- 阶段 2: 不牺牲总时长的前提下, 最小化出锅差 ----------
        # 三层字典序目标, 用一个整数目标 + 位权编码 (避免多次求解):
        #   主序 Γ = max F - min F : **评分表口径**
        #       赛题原文: "出锅时间差（最后一道菜与第一道菜完成的时间差,
        #       小于 5 分钟为优秀）" —— 就是原始跨度, 不是任何语义变体。
        #   次序 T (上桌/完工时刻): 总时长也是评分项 ("耗时对比"), 所以它必须
        #       排在"语义保热"之前 —— 语义项只能做兜底, 不许拿评分项去换。
        #   末序 语义冷等待 Σσ_d·cold_d + 超保热窗重罚: 在跨度与总时长都并列时,
        #       挑"热菜干等最少"的那一个 (防止为了对齐而让热菜放凉)。
        #
        # 为什么必须写位权 (而不是分多次求解):
        #   ① 若只 minimize 主序, 在 [T*, T*+slack] 内所有主序相同的解并列最优,
        #      求解器返回哪个 T 是**任意的** —— 实测同一实例随机报 74 或 79 分钟,
        #      让"总时长"无法复现。必须把 T 钉进目标。
        #   ② 位权必须动态算: 冷等待项的取值上限随 horizon 和菜数增长, 用常数权重
        #      (如 10^4) 会在大实例上被冷等待项反超, 字典序就失效了。
        H = horizon
        cold_obj = sum(int(round(100 * sigma(d))) * cold[d]
                       + 500 * int(round(100 * sigma(d))) * over[d] for d in F)
        A_COLD = 60000 * H * max(1, len(F))               # 冷等待项取值上限
        W_T = A_COLD + 1                                  # 总时长 > 冷等待
        W_SPREAD = W_T * (H + 1) + A_COLD + 1             # 跨度 > 总时长 + 冷等待
        m.Minimize(W_SPREAD * spread + W_T * T + cold_obj)
        st2 = solver.Solve(m)
        st = st2 if st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE) else st1
        if st is None:
            return _give_up("stage2_infeasible_or_timeout")
    else:
        m.Minimize(int(round(w_mk * 10)) * T
                   + sum(int(round(w_gap)) * int(round(sigma(d) * 10)) * cold[d]
                         + 500 * int(round(sigma(d) * 10)) * over[d] for d in F))
        st = solver.Solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return _give_up("single_stage_infeasible_or_timeout")

    # ---------- 还原排程 ----------
    out = []
    packed = defaultdict(list)

    def _unit(dev: str, s: float, e: float) -> str:
        """区间装箱: 给同一设备上的任务分配真实单元号, 保证同单元不重叠。"""
        for i, iv in enumerate(packed[dev]):
            if s >= iv[1] - 1e-6:
                iv[1] = e
                return f"{dev}-{i+1}"
        packed[dev].append([s, e])
        return f"{dev}-{len(packed[dev])}"

    for t in sorted(tasks, key=lambda x: solver.Value(S[x["key"]])):
        k = t["key"]
        s, e = float(solver.Value(S[k])), float(solver.Value(E[k]))
        if t["res"]:
            uid = _unit(t["res"], s, e)
        elif t["hands"]:
            uid = "双手-1"
        else:
            uid = "等待"
        out.append(ScheduledTask(
            task_id=f"{t['dish']}#{t['step_id']}", dish=t["dish"], step_id=t["step_id"],
            raw_text=t["raw"], device=t["device"], unit_id=uid,
            start_min=s, end_min=e, duration_min=float(t["dur"]),
            needs_human=t["hands"], unattended=t["unattended"], temp_c=t["temp"],
            depends_on=[f"{d[0]}#{d[1]}" for d in t["deps"]],
        ))
    out.sort(key=lambda x: x.start_min)
    makes = max((x.end_min for x in out), default=0.0)
    df = {}
    for x in out:
        df[x.dish] = max(df.get(x.dish, 0.0), x.end_min)
    ft = sorted(df.values())
    gap = ft[-1] - ft[0] if ft else 0.0
    plan = SchedulePlan(tasks=out, makespan_min=round(makes, 1),
                        finish_gap_min=round(gap, 1),
                        dish_finish={k: round(v, 1) for k, v in df.items()})
    plan.solver_status = "OPTIMAL" if st == cp_model.OPTIMAL else "FEASIBLE"
    # ---- 求解元信息 + 约束自证 (v2.3) ----
    # 即使求解器报出可行解, 也要**实测**一次约束是否真的成立: 手写线性化出错时,
    # 求解器会给出"满足模型、但违背意图"的解, 只看 status 是发现不了的。
    # 校验用的是 metrics.hand_streak —— 与约束同源, 保证两边量的是同一个量。
    meta = {"horizon": horizon, "status": plan.solver_status,
            "elapsed_ms": round(solver.WallTime() * 1000, 1)}
    if hand_cap:
        actual = hand_streak(plan)
        meta.update({"hand_cap_requested": int(hand_cap),
                     "hand_cap_actual": round(actual, 1),
                     "hand_cap_enforced": bool(actual <= B + 1e-6)})
    else:
        meta.update({"hand_cap_requested": None, "hand_cap_enforced": None})
    _SOLVE_META.set(meta)
    return plan


# --------------------------------------------------------------------------
# 大邻域搜索 (LNS / destroy & repair)
# --------------------------------------------------------------------------
def _plan_key(plan) -> tuple:
    """字典序比较键: 先比总时长, 再比出锅差 —— 与两阶段目标一致。"""
    return (round(plan.makespan_min, 3), round(plan.finish_gap_min, 3))


def _starts_of(plan) -> dict:
    return {(t.dish, t.step_id): t.start_min for t in plan.tasks if t.dish}


def solve_lns(recipes: list[Recipe], policy=None, priors: dict | None = None,
              time_budget: float = 3.0, rounds: int = 8, keep_ratio: float = 0.6,
              seed: int = 0, **kw) -> SchedulePlan | None:
    """大邻域搜索: 每轮"打碎"一部分任务重新优化, 其余钉在当前值。

    为什么需要它
    ------------
    单次 CP-SAT 在 36ms 就收敛 (只用掉 1.2% 的预算), 说明它要么已证最优, 要么
    陷在某个局部结构里出不来。LNS 用**剩下的 98% 预算**反复跳出局部结构:
    固定 60% 的任务 -> 放开 40% -> 重解 -> 变优则接受。
    这是 RCPSP 大规模实例上最有效的元启发式之一, 且天然复用精确模型
    (不需要另写一套启发式代码)。

    返回字典序最优 (makespan, gap) 的排程。
    """
    import random as _random
    import time as _time

    t0 = _time.time()
    allow_pre = bool(kw.get("allow_preprocess", True))
    kw.setdefault("slack", 15)       # 与 solve_with_gap 一致, 否则比较口径不同
    tasks = extract_tasks(recipes, priors=priors, allow_preprocess=allow_pre)
    keys = [t["key"] for t in tasks]
    if not keys:
        return None

    # 初始解: 用 20% 预算拿一个可行解 (避免第一轮就吃满时间)
    best = solve_exact(recipes, policy=policy, priors=priors,
                       time_budget=max(0.3, time_budget * 0.2), **kw)
    if best is None:
        return None
    best_key = _plan_key(best)
    rnd = _random.Random(seed)

    for r in range(max(1, rounds)):
        left = time_budget - (_time.time() - t0)
        if left <= 0.15:
            break
        n_keep = max(1, min(len(keys) - 1, int(len(keys) * keep_ratio)))
        keep = set(rnd.sample(keys, n_keep))
        cur = _starts_of(best)
        fixed = {k: v for k, v in cur.items() if k in keep}
        cand = solve_exact(recipes, policy=policy, priors=priors,
                           time_budget=max(0.15, left / max(1, rounds - r)),
                           fixed=fixed, hint=cur, **kw)
        if cand is not None and _plan_key(cand) < best_key:
            best, best_key = cand, _plan_key(cand)
    return best


# --------------------------------------------------------------------------
# 便捷入口: 精确解 + 下界 + 最优性间隙
# --------------------------------------------------------------------------
def solve_with_gap(recipes: list[Recipe], policy=None, priors: dict | None = None,
                   time_budget: float = 3.0, oven_q: int | None = None,
                   allow_preprocess: bool = True, slack: int = 15,
                   **kw) -> dict:
    """精确解 + 下界 + 最优性间隙。

    slack 默认 15: 实测 40 组用例的 Pareto 前沿在 slack>=15 处**饱和** ——
    总时长 72.5 (-24.3%) / 出锅差 2.75 (-23.6%) / 连续手动 32.8 (-35.2%),
    三指标同时优于交付版, 且"出锅差 <= 5 min"的组数从 30/40 升到 36/40。
    slack 再大不再有收益 (只是把搜索空间放宽, 报告的总时长反而可能漂移)。
    """
    lb = lower_bound(recipes, oven_q=oven_q, priors=priors,
                     allow_preprocess=allow_preprocess)
    t0 = time.time()
    plan = solve_exact(recipes, policy=policy, priors=priors,
                       time_budget=time_budget, oven_q=oven_q, slack=slack,
                       allow_preprocess=allow_preprocess, **kw)
    ms = (time.time() - t0) * 1000
    if plan is None:
        return {"ok": False, "lb": lb, "elapsed_ms": round(ms, 1)}
    gap = (plan.makespan_min - lb["lb"]) / lb["lb"] * 100 if lb["lb"] > 0 else 0.0
    return {
        "ok": True, "plan": plan, "lb": lb,
        "makespan": plan.makespan_min, "finish_gap": plan.finish_gap_min,
        "optimality_gap_pct": round(gap, 1),
        "status": getattr(plan, "solver_status", "?"),
        "elapsed_ms": round(ms, 1),
    }
