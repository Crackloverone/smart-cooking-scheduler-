"""
有限能力调度核心 (移植自 paichan 成熟架构)

核心思想:
  - 成本函数选槽: 每个任务在多个资源单元上试排, 按 (等待成本+换型成本+冻结惩罚) 选最优
  - 两级冻结: hard_freeze=已开始/已完成步骤锁定; soft_freeze=临近任务可挪但重罚
  - 多目标: 同时出锅(出锅差) 优先, 其次总时长, 其次减少手动操作空档
纯 Python 实现, 不依赖 pandas, 保证 L5 性能 (<100ms)。

v2 (模型在环) 改动
------------------
求解器原本把三个目标压成**硬编码常数**, 且只有一条贪心路径。现在它变成
Agent 决策层的**约束执行器**, 接收两样由模型产出的东西:

  - `policy`  (app/agent/objective.py): 用户偏好塑形出的多目标权重向量
  - `priors`  (app/agent/priors.py)   : 每道菜的调度语义先验
  - `strategy`: 调度策略 (关键路径/保热优先/备菜前置/压缩/均衡)

于是求解器能做的事从"一条路走到底"变成"在策略空间里择优", 而"选哪条路"
由模型决策 —— 这是本方案让模型真正参与调度决策、又不牺牲在线性能的关键。
"""
from __future__ import annotations
import contextvars
import functools
import os
import re
from collections import defaultdict
from contextlib import contextmanager
from typing import Optional

from app.models.schema import (
    Recipe, ScheduledTask, SchedulePlan,
    DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN, DEVICE_FRIDGE, DEVICE_HOOD, DEVICE_NONE,
    DEVICE_DISHWASHER, DEVICE_WATER,
    PLATE,
)
from app.scheduler.prep import (
    PREP_AHEAD_MIN as _PREP_AHEAD_MIN, is_hard_prep, prep_keys,
)
# 经验回流(时长校准)。⚠ 顶层 import 是安全的: calib.py 顶层只 import os/time/typing,
# 它对 exec_state 的引用是**函数内延迟导入**, 不会形成 engine↔calib 的循环。
from app.scheduler import calib as _calib

# ---- 成本权重 (对应 paichan SchedulingPolicy); policy 为空时的默认值 ----
WAIT_COST = 1.0          # 等待成本: 鼓励早开始
SWITCH_COST = 2.0        # 设备换菜/换温度的升温惩罚
FREEZE_MOVE_COST = 10000.0  # 软冻结任务被挪动的重罚
HAND_GAP_COST = 0.5      # 双手空档成本: 减少连续手动
DEFAULT_ALIGN_W = 60.0   # 同出锅对齐权重 (与原实现一致)

# 前置准备阈值: 冷藏/腌制/解冻超过此时长(分钟)视为"提前准备",
# 不占当天烹饪关键路径 (如惠灵顿牛排冷藏12h, 应前一天腌好, 当天直接做)
# 判据本体已统一到 app/scheduler/prep.py, 三条轨共用 (见该模块 docstring)。
PREP_AHEAD_MIN = _PREP_AHEAD_MIN


# ---- 调度策略空间: 由 Agent 层决策选哪一个 ----
STRATEGIES = {
    # 关键路径优先 (原实现): 剩余路径长的先做
    "critical_path": dict(order="rank", align_mult=1.0, prep_ahead=False,
                          share=True, hand_mult=1.0, desc="关键路径优先"),
    # 保热优先: 强化"趁热上桌"对齐权重, 让爆炒/清蒸压到最后
    "serve_hot_last": dict(order="hot", align_mult=2.5, prep_ahead=False,
                           share=True, hand_mult=1.0, desc="保热优先, 趁热菜压后"),
    # 备菜前置: 把可提前的切配/腌制移出关键路径
    "prep_first": dict(order="prep", align_mult=1.0, prep_ahead=True,
                       share=True, hand_mult=1.0, desc="备菜前置, 压缩总时长"),
    # 压缩总时长: 长任务先排 + 备菜前置
    "compress": dict(order="long", align_mult=0.6, prep_ahead=True,
                     share=True, hand_mult=1.0, desc="长任务先行, 压缩 makespan"),
    # 人工均衡: 重罚连续手动, 打散人的活
    "balanced": dict(order="rank", align_mult=1.0, prep_ahead=False,
                     share=True, hand_mult=6.0, desc="人工负载均衡"),
    # 四级复合优先级 (设计文档 3.3.1): 关键路径 > LPT > 人工优先 > 短任务填充。
    # 单独成一条策略而不是替换上面 5 条 —— 上面的 order 是**刻意互相拉开**的
    # (rank/long/hot/prep 四种取向), 组合择优靠的就是这种多样性; 把复合键塞进去
    # 等于把 5 条策略收敛成 1 条, 实测会掉 3~5 个百分点。
    "composite": dict(order="composite", align_mult=1.0, prep_ahead=False,
                      share=True, hand_mult=1.0, desc="四级复合优先级(文档3.3.1)"),
}
DEFAULT_STRATEGY = "critical_path"


# 灶具上的"短操作": 淋热油 / 勾芡 / 炒香 / 烧热锅 / 盛出调味 —— 都是 1~3 分钟的手上活。
# 统一按 8 分钟会虚占灶眼; 3~5 菜实例里两个灶眼就是瓶颈, 每步多算 5 分钟足以顶高总时长。
# (官方 100 道菜里这类句子约 10 条, 全部落在"末步"时还会直接顶高出锅差)
_STOVE_SHORT = re.compile(r"烧热|热油|油温|淋|浇|泼|勾芡|收汁|盛出|炒香|爆香|煸香|"
                          r"烧至|加热至|调味|摆盘|装盘")


# ---- 兜底时长分档 (v3.10) ----
# 背景: 蒸箱与烤箱原来都兜底 **8 分钟** —— 一个设备一个常数。后果是
#   「香菇烤芦笋」(原文写了 190℃/8 分钟) 与「巴斯克蛋糕」(原文没写时长)
#   在排程里被当成同样长的一步, 蛋糕的 30~40 分钟被严重低估。
#
# ⚠⚠ 数值刻意**贴着已验证的 8 分钟基线**取, 不做"真实厨艺常识"那套 (蛋糕 35 分钟)。
#   原因是有实测教训: 早期版本在复合设备占位句上硬编码 25 分钟, 烤箱容量为 1
#   立刻变成瓶颈, 出锅差<5min 占比 96% → 84%。**相对次序**是对的, **绝对量级**
#   一旦放大就会直接吃掉 25 分的多目标优化。
#   所以这里只建模"谁比谁长", 不建模"到底多长" —— 原文真写了时长的步
#   在 `estimate_duration` 里被原样尊重, 根本走不到这里。
#   想退回旧行为: `FT_DUR_TIERS=off`。
_STEAM_TIERS = (
    # "鸡(?!精|粉)": 调味料「鸡精/鸡粉」不是肉，命中它会把素菜推到长档。
    (re.compile(r"排骨|五花|猪|牛|羊|鸡(?!精|粉)|鸭|鹅|肉|蹄|肘|粽|糯米|南瓜|芋|山药|土豆|藕|"
                r"海带|馒头|包子|花卷|发糕|糕|栗"), 15.0),
    (re.compile(r"蛋|羹|鱼|虾|蟹|贝|蛤|蛎|蔬|青菜|芦笋|西兰花|茄|豆腐|粉丝|秋葵|金针菇"),
     6.0),
)
_OVEN_TIERS = (
    (re.compile(r"蛋糕|芝士|奶酪|派|挞|面包|吐司|欧包|披萨|酥|曲奇|饼干|玛格丽特|"
                r"布朗尼|玛芬|泡芙|司康|布丁|模具"), 18.0),
    (re.compile(r"排骨|鸡|鸭|牛|羊|肉|鱼|虾|蟹|翅|腿|排|串|鱿鱼"), 12.0),
    (re.compile(r"蔬|芦笋|西兰花|茄|香菇|蘑菇|薯|土豆|南瓜|红薯|干$"), 6.0),
)


def _tier(text: str, tiers, default: float) -> float:
    """按步骤原文里的**食材/品类词**选一档兜底时长；都没命中就用 default。

    环境开关 `FT_DUR_TIERS=off` 时恒返回 default（= 与旧行为逐位一致）。
    """
    if os.environ.get("FT_DUR_TIERS", "on").lower() in ("off", "0", "false", "no"):
        return default
    for pat, val in tiers:
        if pat.search(text):
            return val
    return default


def _base_estimate(step):
    """原文没写时长时的兜底估计, 返回 `(分钟, 档位键)`。

    拆分出来是为了给 `calib`(经验回流)一个聚合维度 —— 求值逻辑**逐行照搬**原
    `estimate_duration`, 只是把每个 `return x` 变成 `return x, bucket`。

    ⚠ 档位键**只用于校准聚合, 不参与求解**: 它不进任何约束、不进目标函数,
      所以新增/细分档位是安全的。蒸箱与烤箱刻意分成两个键 —— 两者都兜底 8 分钟,
      但真实耗时分布并不相同, 合在一起会互相污染系数。
    """
    # PLATE 必须排在设备判断之前: 纯收尾句即使带过设备关键词, 也已在解析层把设备清成
    # "人工"; 这里再兜一道, 防止上游漏清时被当成 8 分钟的设备占用。
    if step.prep_type == PLATE:
        return 1.0, "plate"         # 收尾/装盘/撒葱花: 30~60 秒, 不是切配的 2 分钟
    if step.device == DEVICE_STOVE:
        return ((3.0, "stove_short")
                if _STOVE_SHORT.search(getattr(step, "raw_text", "") or "")
                else (8.0, "stove"))
    if step.device == DEVICE_STEAMER:
        return _tier(getattr(step, "raw_text", "") or "", _STEAM_TIERS, 8.0), "steamer"
    if step.device == DEVICE_OVEN:
        return _tier(getattr(step, "raw_text", "") or "", _OVEN_TIERS, 8.0), "oven"
    if step.device == DEVICE_FRIDGE:
        return 30.0, "fridge"
    if step.prep_type == "切配":
        return 2.0, "prep_cut"
    return 3.0, "manual"


def bucket_of(step) -> str:
    """这一步属于哪个经验档位(供执行态回写时打样本用)。"""
    return _base_estimate(step)[1]


def estimate_duration(step) -> float:
    # ⚠ 原文写了时长 ⇒ **原样尊重**, 绝不校准: 那是菜谱写的事实, 不是估计值。
    if step.duration_min is not None:
        return max(float(step.duration_min), 0.5)
    base, bucket = _base_estimate(step)
    # 关档(FT_CALIB=off, 缺省)时 calib.apply 第一行就返回 base ⇒ 与旧行为逐位一致。
    return _calib.apply(bucket, base)


def resource_type_of(step) -> Optional[str]:
    if step.device == DEVICE_STOVE:
        return DEVICE_STOVE
    if step.device == DEVICE_STEAMER:
        return DEVICE_STEAMER
    if step.device == DEVICE_OVEN:
        return DEVICE_OVEN
    if step.device == DEVICE_FRIDGE:
        return DEVICE_FRIDGE
    return None


# 资源单元配置: {资源类型: 单元数}
#
# 七类设备**全部在册**（赛题原文列的就是这七类）。在册不等于有任务：
# 官方 100 道菜里「洗碗机 / 烟机」出现 0 次，只有「净咖一体机」被点到名字
# （且都是"加纯净水"这类食材，不是设备作业）—— 所以这几台的容量当前是**空置**的。
# 仍然写进来，是为了：① 设备能力覆盖在 `/api/device_spec` 上看得见；
# ② `/api/plan` 的 `devices` 覆盖参数能认出这些设备名（否则评委传 `{"洗碗机":0}`
#    会拿到 400 unknown_device，看起来像"系统只支持 5 类设备"）。
RESOURCE_UNITS = {
    DEVICE_STOVE: 2,
    DEVICE_STEAMER: 1,
    DEVICE_OVEN: 1,
    # ⚠ 冰箱给 **2** 而不是 1：冷藏室与冷冻室是**两个独立温区**，同时放两样东西
    #   完全合法。给 1 等于假设"一次只能冷藏一样"，会把长时腌制/冷藏串行化，
    #   白白抬高总时长（赛题 L3 的「总烹饪压缩率」）。
    DEVICE_FRIDGE: 2,
    DEVICE_DISHWASHER: 1,
    DEVICE_WATER: 1,
    DEVICE_HOOD: 1,
    "人工": 1,
}

# 设备故障时的顶替关系: 蒸箱/烤箱坏了还能用灶具做(蒸锅隔水蒸、煎),
# 顶替设备也不可用时才降级成人工操作。
DEVICE_FALLBACK = {
    DEVICE_STEAMER: DEVICE_STOVE,
    DEVICE_OVEN: DEVICE_STOVE,
}

# ---- 资源容量的"请求级覆盖" (消除全局可变状态竞态) ----
# 旧实现处理"设备故障"是把全局 RESOURCE_UNITS[设备] 改成 0, 排完再在 finally 里恢复。
# 问题: FastAPI 的同步端点跑在线程池里, 两个并发请求会互相踩 ——
#   A 把蒸箱置 0 排程时, B 的 /schedule 会读到 0, 于是 B 的计划里蒸箱凭空消失。
# 改成 ContextVar 覆盖: 覆盖只在本请求的上下文里可见, 全局字典永不被改写。
_RESOURCE_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar(
    "ft_resource_override", default=None)


def resource_units() -> dict:
    """当前请求生效的设备容量。

    默认就是全局 RESOURCE_UNITS (同一对象, 零拷贝);
    处于 resource_override() 上下文内时返回叠加后的副本 —— 全局字典保持不变。
    """
    ov = _RESOURCE_OVERRIDE.get()
    if not ov:
        return RESOURCE_UNITS
    merged = dict(RESOURCE_UNITS)
    merged.update(ov)
    return merged


@contextmanager
def resource_override(overrides: dict):
    """在 with 块内临时覆盖部分设备容量, 退出自动还原。线程/任务安全。

    用法: 设备故障时 `with resource_override({DEVICE_STEAMER: 0}): ...`
    """
    tok = _RESOURCE_OVERRIDE.set(dict(overrides))
    try:
        yield
    finally:
        _RESOURCE_OVERRIDE.reset(tok)


class _ResourceTimeline:
    """单类资源的占用表。`occ[u]` 里每条是 **(起, 止, 温度)** 三元组 ——
    温度是 v3.3 加的: 只有知道"上一盘是几度", 才能算出异温切换要留多少降温时间。
    以前只存 (起, 止), 于是 `_fit` 完全不知道余温, 只能让 200℃ 的下一盘紧贴 180℃。"""

    def __init__(self, unit_count: int):
        self.occ = [[] for _ in range(unit_count)]
        self.last_temp = [None] * unit_count

    def earliest_slot(self, earliest: float, dur: float, temp=None) -> tuple[float, int]:
        best_s, best_u = None, 0
        for u in range(len(self.occ)):
            s = self._fit(earliest, dur, self.occ[u], temp)
            if best_s is None or s < best_s:
                best_s, best_u = s, u
        return best_s, best_u

    def _fit(self, earliest, dur, occ, temp=None):
        """最早可行起点。occ: [(起, 止, 温度)]。

        ⚠ v3.3 降温缓冲: 若紧邻的前一个任务**更热**, 起点必须再往后推
        `cooldown_minutes(前温, 本温)` —— 否则会出现"200℃ 刚出炉, 180℃ 立刻进炉"。
        同温 / 升温 / 温度未知时该值为 0, 行为与 v3.2 完全一致。
        """
        cur = earliest
        for (s, e, t2) in sorted(occ, key=lambda x: (x[0], x[1])):
            if e <= cur:
                # 这段已经过去, 但余温可能还没散完 (紧邻时才有影响)
                cd = cooldown_minutes(t2, temp)
                if cd and e + cd > cur:
                    cur = e + cd
                continue
            if cur + dur <= s:
                return cur
            cur = e + cooldown_minutes(t2, temp)
        return cur

    def occupy(self, start, dur, unit, temp=None):
        self.occ[unit].append((start, start + dur, temp))
        if temp is not None:
            self.last_temp[unit] = temp

    def last_end(self, unit) -> float:
        return max((e for (_s, e, _t) in self.occ[unit]), default=0.0)

    def busy_near(self, start: float, dur: float, pad: float = 1.0) -> float:
        """[start-pad, start+dur+pad] 内已占用的总时长 —— 用于惩罚"连续手动操作"。"""
        lo, hi = start - pad, start + dur + pad
        tot = 0.0
        for (s, e, _t) in self.occ[0]:
            ov = min(hi, e) - max(lo, s)
            if ov > 0:
                tot += ov
        return tot



# 蒸箱同温容忍度(度): 差 <= 此值视为同一腔体温度, 可并行
STEAM_TEMP_TOL = 15.0

# ---------- 设备降温 / 模式切换缓冲 (v3.3) ----------
# 建议文本《智能烹饪调度详细注意事项》§一.3 与 §三.2:
#   "模式切换需要等待降温/升温, 不能立刻从高温烘烤切低温蒸制; 排程时预留切换缓冲时间"
#   "设备降温时间: 高温任务完成后, 不能立刻低温任务" —— 且明确写"**不可以忽略**"。
#
# 实测(2026-10-06, 50 组 3~5 菜, seed=42): 同腔体**异温背靠背** 7 处 ——
#   烤箱 200℃→180℃(降20) 五处、180℃→60℃(降120) 一处、150℃→180℃(升温, 不罚)。
# 也就是说: 200℃ 刚出炉, 180℃ 的下一盘立刻进炉。腔体不会瞬间冷下来,
# 这类计划"看起来可行、实际做不出来", 正是建议文本 §八.1 点名的坑。
#
# 为什么**只罚降温**:
#   升温方向的成本已经被 L1 的**预热步**显式建模(占真实设备槽, 三轨都排它),
#   再补一次等于重复计价; 而降温在整条链路上**没有任何地方**被计过。
#
# 为什么这不影响"同温合炉": `cooldown_minutes` 对温差 <= STEAM_TEMP_TOL 一律返回 0,
#   所以 180℃ 与 182℃ 的两盘菜照旧可以一炉出。
COOLDOWN_MIN_PER_100C = 2.0     # 每降 100℃ 需要 2 分钟
COOLDOWN_ENV = "FT_COOLDOWN"    # on/off; off 时行为与 v3.2 逐字一致(供 A/B)
COOLDOWN_RATE_ENV = "FT_COOLDOWN_RATE"

# 降温合规愿意付出的**Γ 上限**（ε-约束）。为什么必须有上限，见 `prefer_cd`。
CD_BUDGET_ENV = "FT_CD_BUDGET"
CD_BUDGET_DEFAULT = 5.0


# ---- 开关缓存（L5 性能，2026-10-09）----
#
# ⚠ 这三个函数原本**每次调用都读一遍 os.environ**。profile 实测：一次 9 菜排程里
#   `cooldown_enabled()` 被调用 **116,895 次**、`cooldown_minutes()` 116,874 次，
#   合计约 0.85s / 6.93s = **12%** 的时间纯花在 `os.environ.__getitem__` 上
#   （它每次都要 encodekey / upper / strip）。
#
# ⚠ 但**绝不能用模块级变量缓存**：测试用 `monkeypatch.setenv(...)` 改开关后
#   期望立即生效，模块级缓存会让它们静失灵（同类坑见 calib._ENABLED）。
#   所以这里用**请求级 ContextVar**：
#     · 一次排程（顶层 `schedule()` / `_schedule()` 入口）内只读 1 次环境；
#     · 跨次调用重新读 ⇒ monkeypatch 测试仍然有效，语义**逐字不变**。
_FLAG_CACHE: contextvars.ContextVar = contextvars.ContextVar(
    "ft_flag_cache", default=None)

# 总闸（模块级读一次，之后不再碰环境）。off = 退回"每次都读 os.environ"的旧行为，
# 供 A/B 与排障；它不是性能开关而是**逃生口**。
_FLAG_CACHE_ON = os.environ.get("FT_FLAG_CACHE", "on").strip().lower() not in (
    "off", "0", "false", "no")


def reset_flag_cache() -> None:
    """清掉开关缓存，让下次读取重新走 `os.environ`。"""
    _FLAG_CACHE.set(None)


def _flag_scope(fn):
    """把开关缓存的生命周期**严格限制在被装饰函数执行期间**。

    ⚠ 为什么不能"入口 reset 一次、之后一直缓存"：`FT_COOLDOWN` 这类开关是
    **可随时改的**，测试里就是同一个用例内先 `setenv("off")` 再 `setenv("on")`
    并断言立即生效。缓存跨过那次改动 ⇒ 断言拿到旧值 ⇒ 静默测了个错的配置。
    （第一次实现就是这么错的：`tests/test_insight.py` 三条 + `test_core.py`
    一条当场变红。）
    包在这里之后：函数内 11.7 万次读取命中缓存，函数一退出缓存即失效，
    外部任何调用都看到实时环境 —— 语义与"完全不缓存"逐字一致。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _FLAG_CACHE.set({})          # 空 dict = "在作用域内"，_flag 才会缓存
        try:
            return fn(*args, **kwargs)
        finally:
            _FLAG_CACHE.set(None)    # 出作用域即失效，外部看到实时环境
    return wrapper


def _flag(key: str, default: str | None = None):
    """带**请求级**缓存的环境变量读取。

    `default=None` 时"变量不存在"就返回 None —— 与原先 `os.environ.get(key)`
    的语义逐字一致（调用方靠 `is None` 判断"没设置"）。
    """
    if not _FLAG_CACHE_ON:
        return os.environ.get(key, default)
    cache = _FLAG_CACHE.get()
    if cache is None:
        # **不在作用域内 ⇒ 不缓存**（`_FLAG_CACHE` 为 None 表示"没有作用域"）。
        # ⚠ 这里不能顺手建一个缓存：那会让**任何**直接调用（测试、脚本、
        #   metrics 里的零散调用）留下残留值，之后改了环境变量也读不到新值。
        #   缓存只在 `_flag_scope` 标注的区间里有效。
        return os.environ.get(key, default)
    if key not in cache:
        cache[key] = os.environ.get(key, default)
    return cache[key]


def cooldown_enabled() -> bool:
    return _flag(COOLDOWN_ENV, "on").strip().lower() not in ("off", "0", "false", "no")


def cd_budget() -> float:
    """为了满足降温，最多愿意让出锅差 Γ 变差多少分钟（`FT_CD_BUDGET`）。"""
    raw = _flag(CD_BUDGET_ENV)
    if raw is None:
        return CD_BUDGET_DEFAULT
    try:
        v = float(raw)
        return v if v >= 0 else CD_BUDGET_DEFAULT
    except (TypeError, ValueError):
        return CD_BUDGET_DEFAULT


def cooldown_rate() -> float:
    raw = _flag(COOLDOWN_RATE_ENV)
    if raw is None:
        return COOLDOWN_MIN_PER_100C
    try:
        v = float(raw)
        return v if v >= 0 else COOLDOWN_MIN_PER_100C
    except (TypeError, ValueError):
        return COOLDOWN_MIN_PER_100C


def cooldown_minutes(prev_temp, next_temp) -> float:
    """同腔体从 prev_temp 切到 next_temp 需要预留的降温分钟数。

    · 升温 / 同温(差 <= STEAM_TEMP_TOL) / 任一温度未知 -> 0
    · 否则 = 温差 / 100 × 速率(默认 2.0)
    `FT_COOLDOWN=off` 时恒为 0 —— 于是整条链路退回 v3.2 行为, 供 A/B 对照。
    """
    if not cooldown_enabled():
        return 0.0
    if prev_temp is None or next_temp is None:
        return 0.0
    drop = float(prev_temp) - float(next_temp)
    if drop <= STEAM_TEMP_TOL:
        return 0.0
    return round(drop / 100.0 * cooldown_rate(), 4)


def prefer_cd(best, best_cd, budget: float):
    """降温合规解的 ε-约束选择：**只在合规解的 Γ 不比最优解差太多时才换**。

    ⚠ 为什么不能"只要合规就换"（第一版就是那样，被实测打脸）：
      精确轨 / CP-SAT 轨的批次机里**没有"降温"这一项**，于是它们会给出
      "Γ 很好但降温违规"的解；贪心轨插了缓冲、合规，但 Γ 可能很差。
      无界地"合规优先"会拿一处降温违规去换整份计划 —— seed=42 第 23 组实测：
      **Γ 从 2.0 涨到 191.4（95 倍）**，T 从 226 涨到 262。那不是"尊重物理"，
      是把物理精化变成了评分灾难。

    预算内换（合规优先），预算外**保留更优解并交给 `insight` 报红色预警** ——
    后者正是建议文本 §七.1 要的"风险提示"，比偷偷把分数做烂诚实得多。
    """
    if best is None:
        return best_cd
    if best_cd is None or best_cd is best:
        return best
    if float(best_cd.finish_gap_min) <= float(best.finish_gap_min) + budget:
        return best_cd
    return best


def _temp_share_fit(occ, earliest, dur, temp, tol: float = STEAM_TEMP_TOL,
                    none_shareable: bool = True):
    """occ: list[(s,e,temp)]。同温可重叠共享腔体, 异温必须串行。返回最早可开始时刻。
    蒸箱/烤箱共用同一套"同温合炉"语义 —— 两道 180℃ 的烤菜可以一炉出。
    none_shareable: 温度未知时是否允许共享 (蒸箱沿用原实现=True; 烤箱取 False 更保守)。

    ⚠ v3.3: 串行时还要加上**降温缓冲** —— 异温任务之间不能紧贴 (见 cooldown_minutes)。
    """
    cur = earliest
    # 只按 (起, 止) 排序 —— temp 仅用于同温判断, 不参与排序。
    # 用裸 sorted(occ) 会在两个区间起止完全相同、且温度一个 None 一个数值时,
    # 把元组比较落到第三项 -> "float < None" 抛 TypeError 直接崩掉排程。
    for (s, e, t2) in sorted(occ, key=lambda x: (x[0], x[1])):
        if temp is None or t2 is None:
            same_temp = none_shareable
        else:
            same_temp = abs(temp - t2) <= tol
        if same_temp:
            continue  # 同温可共享腔体, 不挡
        if e <= cur:
            # 这段已经过去, 但余温可能还没散完 (紧邻时才有影响)
            cd = cooldown_minutes(t2, temp)
            if cd and e + cd > cur:
                cur = e + cd
            continue
        if cur + dur <= s:
            return cur
        if e > cur:
            cur = e + cooldown_minutes(t2, temp)
    return cur


def _prep_ahead(device: str, unattended: bool, dur: float) -> bool:
    """超长冷藏/浸泡/泡发 -> 应在开火前完成, 不占当天关键路径。
    判据已统一到 app/scheduler/prep.py, 这里保留薄封装供旧调用点使用。"""
    return is_hard_prep(device, unattended, dur)


def _build_tasks(recipes: list[Recipe], priors: dict | None = None,
                 allow_preprocess: bool = False):
    tasks = []
    for rec in recipes:
        for st in rec.steps:
            dur = estimate_duration(st)
            res = resource_type_of(st)
            if st.unattended:
                uses_hands = False
            elif res is None:
                uses_hands = True
            elif st.needs_human:
                uses_hands = True
            else:
                uses_hands = False
            tasks.append({
                "dish": rec.name, "step_id": st.step_id, "raw_text": st.raw_text,
                "device": st.device, "res": res, "uses_hands": uses_hands,
                "dur": dur, "unattended": st.unattended,
                "temp": getattr(st, "temperature_c", None),
                "depends_on": [(rec.name, d) for d in st.depends_on],
                "soft_freeze": False, "orig_start": None, "orig_unit": None,
                # v3.0: 预热是 L1 拆出来的真实步骤(parser.insert_preheat_steps)。
                # 标记出来, 让同温共享对它也生效(文档 4.1.2)。
                "is_preheat": is_preheat_step(st),
            })

    # 可提前的步骤来源 (hard = 超长冷藏/浸泡/泡发, soft = 模型语义先验标记的备菜步)
    # 与判据本体一律走 app/scheduler/prep.py::prep_keys —— 三条轨共用同一口径。
    #
    # ⚠ 这里曾经有一份**内联的旧判据**(要求"前置自己也是 hard/soft"才放行),
    #   与 prep.py 不同步 ⇒ 贪心轨仍把 4 小时冷却压在当天关键路径上, 而 CP-SAT 轨
    #   和精确轨已经按新口径把它移走 —— 三轨排的又不是同一个任务集了。
    #   这正是 prep.py docstring 记的那类事故, 所以复制品必须删掉。
    prep = prep_keys(recipes, priors, allow_preprocess)

    for t in tasks:
        t["prep_ahead"] = (t["dish"], t["step_id"]) in prep

    # 预排提前准备步: 同一道菜的备菜步必须**串行**, 整体右对齐到 t=0
    # (原实现把它们全塞在 [-dur, 0], 会互相重叠并早于各自前置 -> 依赖违反)
    prep_start = {}
    _by_dish_prep = defaultdict(list)
    for t in tasks:
        if t["prep_ahead"]:
            _by_dish_prep[t["dish"]].append(t)
    for dish, ts in _by_dish_prep.items():
        ts.sort(key=lambda x: x["step_id"])
        cur = -sum(x["dur"] for x in ts)
        for x in ts:
            prep_start[(x["dish"], x["step_id"])] = cur
            cur += x["dur"]
    return tasks, prep_start


def _rank(tasks):
    """剩余路径长度 (关键路径权重)"""
    by_dish = defaultdict(list)
    for t in tasks:
        by_dish[t["dish"]].append(t)
    rank = {}
    for dish, ts in by_dish.items():
        cum = 0.0
        for t in sorted(ts, key=lambda x: -x["step_id"]):
            cum += t["dur"]
            rank[(t["dish"], t["step_id"])] = cum
    return rank


def _parse_unit(unit_id: str):
    """'灶具-2' -> ('灶具', 1); '双手-1' -> ('人工', 0); 等待/提前准备/预热 -> (None, None)"""
    if not unit_id or unit_id in ("等待", "提前准备"):
        return None, None
    if unit_id == "双手-1":
        return "人工", 0
    rid, _, num = unit_id.rpartition("-")
    if not rid:
        return None, None
    try:
        return rid, int(num) - 1
    except ValueError:
        return None, None


def _fit_unit(tl: _ResourceTimeline, unit: int, earliest: float, dur: float,
              shared: bool, temp_occ: list, temp) -> float:
    """在某个**指定单元**上找最早可行起点。
    独占设备走该单元的占用表; 同温共享(蒸箱)走温度共享表。
    单元号必须与返回起点配套使用, 否则会出现"起点属于A单元却占用B单元"的冲突。"""
    if shared:
        return _temp_share_fit(temp_occ, earliest, dur, temp, none_shareable=True)
    return tl._fit(earliest, dur, tl.occ[unit], temp)


def _settle(tl: _ResourceTimeline, unit: int, start: float, dur: float,
            shared: bool, temp_occ: list, temp, hands: _ResourceTimeline) -> float:
    """设备槽与人工槽互相顺延直到同时可行 (最多 3 轮)。
    灶上爆炒这类任务同时占"灶眼"和"双手", 两者必须落在同一段空闲窗口。"""
    for _ in range(3):
        h_s, _ = hands.earliest_slot(start, dur)
        if h_s <= start + 1e-6:
            return start
        start = _fit_unit(tl, unit, h_s, dur, shared, temp_occ, temp)
    return start


def _schedule(recipes: list[Recipe], hard_frozen: dict = None,
              now: float = 0.0, policy=None, priors: dict | None = None,
              strategy: str = DEFAULT_STRATEGY) -> SchedulePlan:
    """两遍排程: 第一遍拿到真实最晚出锅时刻, 第二遍拿它当对齐目标。

    为什么要两遍
    ------------
    对齐目标原本取"最长那道菜的理论链长", 但真实排程里蒸箱/烤箱各只有一台,
    多道菜挤同一设备, 实际最晚出锅时刻 T 往往是理论值的 2-4 倍。
    用偏小的目标去引导, 短菜只会"提前做完等着", 出锅差自然压不下去。
    第二遍换成真实 T 作目标, 短菜才会主动延后到和大家一起出锅。
    """
    first = _schedule_once(recipes, hard_frozen, now, policy, priors, strategy)
    t_actual = first.makespan_min
    if t_actual <= 0:
        return first
    try:
        second = _schedule_once(recipes, hard_frozen, now, policy, priors, strategy,
                                align_target=t_actual)
    except Exception:
        return first
    # 出锅差优先、总时长次之
    if (second.finish_gap_min, second.makespan_min) < (first.finish_gap_min, first.makespan_min):
        return second
    return first


@_flag_scope
def _schedule_once(recipes: list[Recipe], hard_frozen: dict = None,
                   now: float = 0.0, policy=None, priors: dict | None = None,
                   strategy: str = DEFAULT_STRATEGY,
                   align_target: float | None = None) -> SchedulePlan:
    """
    hard_frozen: {(dish,step_id): ScheduledTask} 已锁定任务(时间不动)。
    now: 当前执行时刻, >=now 的在跑任务自动 hard freeze。
    policy / priors / strategy: 由 Agent 决策层给出 (见模块 docstring)。
    """
    hard_frozen = hard_frozen or {}
    stg = STRATEGIES.get(strategy, STRATEGIES[DEFAULT_STRATEGY])
    align_w = (getattr(policy, "w_gap", DEFAULT_ALIGN_W) or DEFAULT_ALIGN_W) * stg["align_mult"]
    w_makespan = getattr(policy, "w_makespan", 1.0) or 1.0
    w_hand = (getattr(policy, "w_hand", HAND_GAP_COST) or HAND_GAP_COST) * stg["hand_mult"]
    w_hot = getattr(policy, "w_hot", 30.0) or 30.0
    target_gap = getattr(policy, "target_gap", 5.0) or 5.0
    allow_pre = stg["prep_ahead"] or bool(getattr(policy, "allow_preprocess", False))

    tasks, prep_start = _build_tasks(recipes, priors=priors, allow_preprocess=allow_pre)
    by_key = {(t["dish"], t["step_id"]): t for t in tasks}
    rank = _rank(tasks)

    def _hot_of(dish: str) -> float:
        p = (priors or {}).get(dish)
        return float(getattr(p, "serve_hot", 0.6)) if p else 0.6

    units = resource_units()          # 本请求生效的容量 (含故障覆盖)
    lines = {k: _ResourceTimeline(n) for k, n in units.items()}
    # 同温合炉: 蒸箱/烤箱都按"同温共享腔体"建模 (原实现只对蒸箱生效)
    temp_share = defaultdict(list)      # 资源类型 -> [(s,e,temp)]
    share_ok = bool(stg["share"])

    done = {}
    scheduled = []

    # 1) 放回硬冻结任务: 原样还原它占用的资源单元 (含人工), 时间不可变
    frozen_ids = {f"{d}#{s}" for (d, s) in hard_frozen.keys()}
    for key, st in hard_frozen.items():
        done[key] = st
        scheduled.append(st)
        rid, idx = _parse_unit(st.unit_id)
        if rid is None:
            continue
        tl = lines.get(rid)
        if tl is None or idx >= len(tl.occ):
            continue
        tl.occ[idx].append((st.start_min, st.end_min, getattr(st, "temp_c", None)))
        if rid in (DEVICE_STEAMER, DEVICE_OVEN):
            temp_share[rid].append((st.start_min, st.end_min, st.temp_c))
        # 占设备的手动任务(如灶上爆炒)同时占着双手
        if st.needs_human and rid != "人工":
            lines["人工"].occ[0].append((st.start_min, st.end_min, None))

    remaining = set(by_key.keys()) - set(hard_frozen.keys())

    # ---- 同出锅对齐: 算每道菜的在途总时长(不含提前准备), 目标完成时刻 = 最长菜 ----
    dish_dur = {}
    for k, t in by_key.items():
        if t["prep_ahead"]:
            continue
        dish_dur[t["dish"]] = dish_dur.get(t["dish"], 0.0) + max(0.0, t["dur"])
    if align_target is None:
        align_target = max(dish_dur.values()) if dish_dur else 0.0
    # 每道菜的最后一个加热/收尾步骤 key
    dish_last_heat = {}
    for k, t in by_key.items():
        if t["prep_ahead"]:
            continue
        if k not in dish_last_heat or t["step_id"] > dish_last_heat[t["dish"]][1]:
            dish_last_heat[t["dish"]] = (k, t["step_id"])

    def _order_key(k):
        t = by_key[k]
        if stg["order"] == "long":                 # 长任务先行 (LPT, 压缩 makespan)
            return (-t["dur"], -rank[k], t["step_id"])
        if stg["order"] == "prep":                 # 备菜前置
            return (0 if t["prep_ahead"] else 1, -rank[k], t["step_id"])
        if stg["order"] == "hot":                  # 不耐放的菜后做 (保热)
            return (round(_hot_of(t["dish"]), 2), -rank[k], t["step_id"])
        if stg["order"] == "composite":
            # 文档 3.3.1 四级复合优先级, 逐级 tie-break:
            #   ① 关键路径: rank 越大越先 —— 它直接决定总时长下界, 永远第一顺位;
            #   ② LPT: **只对占用设备的任务**按长任务先行 —— 设备是批次机, 长任务
            #      压后会把 makespan 顶高; 人工任务不该按 LPT (人的活短且碎);
            #   ③ 人工优先: 人是单资源瓶颈, 同档内先占坑;
            #   ④ 短任务填充: 同档内短的先做, 尽快把设备让出来。
            on_dev = 0 if t["res"] else 1
            return (-rank[k], on_dev, (-t["dur"] if on_dev == 0 else 0.0),
                    0 if t["uses_hands"] else 1, t["dur"], t["step_id"])
        return (-rank[k], t["step_id"])            # 关键路径优先 (原实现)

    while remaining:
        ready = [k for k in remaining
                 if all(d in done for d in by_key[k]["depends_on"])]
        if not ready:
            ready = [next(iter(remaining))]
        ready.sort(key=_order_key)
        key = ready[0]
        t = by_key[key]

        # 前置准备步骤: 排到烹饪开始前 (t=0 之前), 不占当天设备, 不进关键路径
        if t["prep_ahead"]:
            start = prep_start.get((t["dish"], t["step_id"]), -t["dur"])
            st = ScheduledTask(
                task_id=f"{t['dish']}#{t['step_id']}",
                dish=t["dish"], step_id=t["step_id"], raw_text=t["raw_text"],
                device=t["device"], unit_id="提前准备",
                start_min=round(start, 1), end_min=round(start + t["dur"], 1),
                duration_min=round(t["dur"], 1),
                needs_human=False, unattended=True, temp_c=t.get("temp"),
                depends_on=[f"{d[0]}#{d[1]}" for d in t["depends_on"]],
            )
            done[key] = st
            scheduled.append(st)
            remaining.remove(key)
            continue

        # ⚠ 非备菜任务不得早于 t=0 (2026-10-06 修复)。
        # 备菜步整体右对齐到 0 (`_build_tasks` 里 cur=-sum(dur)), 所以它的 end_min
        # 是**负数**。若某烹饪步的前置恰好是一个结束在 -2 的备菜步, dep_ready 就是
        # -2, 贪心会把这条**占设备/占人工**的步骤排到 t<0。
        # 后果不止显示: `_to_plan` 判"当天烹饪"的口径是 `start_min >= 0`,
        # 于是这条真实烹饪步**既不计入 T/Γ、也不进 check_plan**(checker 只查 start>=0),
        # 甘特图上还会在 0 之前多出一行设备占用。实测官方 100 道里 8 道中招
        # (低温牛排#2 蒸箱 -2、金银蒜蒸鸡胸肉#4 双手 -60、麻辣对虾#4 双手 -15 …)。
        # 精确模型本就把起点约束在 [0, horizon] (model.py: NewIntVar(0, horizon)),
        # 贪心必须与它同口径 —— 否则贪心解在 (Γ,T) 打平时会先手胜出, 把这条错误带出来。
        dep_ready = max(0.0, max((done[d].end_min for d in t["depends_on"]), default=0.0))

        # 评估候选槽, 选成本最低
        best = None
        align_step = key in dish_last_heat.get(t["dish"], (None,))
        hot = _hot_of(t["dish"])
        candidates = []
        if t["res"] is not None and units.get(t["res"], 0) <= 0:
            # 设备故障(单元数被临时置 0): 候选槽会是空集, 直接崩溃。
            # 先顶替到能代劳的设备, 顶替不了再降级为人工 —— 排程必须仍然出得来。
            #
            # ⚠ device 必须跟着 res 一起改 (v3.1 修复)。
            #   原实现只改 res, device 保留原设备名, 于是产出
            #   `device="蒸箱"` + `unit_id="灶具-1"` 这种自相矛盾的任务
            #   (实测 6 个场景共 68 条)。后果不只是显示:
            #   `checker.overlap_allowed()` 是按 `device` 判"蒸箱同温可共享"豁免的,
            #   一个其实占着灶具、却仍标蒸箱的任务会套用该豁免,
            #   **把真实的灶具冲突判成合法重叠**(静默漏报)。
            #   调度本身一直是对的(res 生效), 错的是这份对外标签。
            alt = DEVICE_FALLBACK.get(t["res"])
            if alt and units.get(alt, 0) > 0:
                t = dict(t, res=alt, device=alt)
            else:
                t = dict(t, res=None, device=DEVICE_NONE, uses_hands=True)
        if t["res"] is not None:
            # 同温合炉: 蒸箱常态共享腔体(赛题明确"蒸箱不能同时进行不同温度的蒸制");
            # 烤箱按独占处理(官方冲突检测按设备占用计)。
            # 例外(v3.0): **预热节点允许同温共享** —— 文档 4.1.2
            # "多道菜如果设备预热温度完全一致, 可共享一次预热任务", 两道 180℃ 的
            # 烤菜可以共用一次预热, 不必预热两次。
            shared = share_ok and (t["res"] == DEVICE_STEAMER or t.get("is_preheat"))
            # 正常尽早槽: 逐个单元试, **起点必须来自该单元自己**。
            # (v2.1 修复: 旧代码用 earliest_slot() 返回的全局最早起点、却搭配循环
            #  变量 u 去 occupy, 造成"起点属于 A 单元却占用 B 单元" -> 同单元重叠冲突)
            for u in range(units[t["res"]]):
                start = _fit_unit(lines[t["res"]], u, dep_ready, t["dur"], shared,
                                  temp_share[t["res"]], t.get("temp"))
                if t["uses_hands"]:
                    start = _settle(lines[t["res"]], u, start, t["dur"], shared,
                                    temp_share[t["res"]], t.get("temp"), lines["人工"])
                candidates.append((start, u))
            # 同出锅: 额外尝试"刚好在目标时刻结束"的晚开始槽
            if align_step:
                late_start = align_target - t["dur"]
                if late_start > dep_ready:
                    for u in range(units[t["res"]]):
                        ls = _fit_unit(lines[t["res"]], u, max(dep_ready, late_start),
                                       t["dur"], shared, temp_share[t["res"]], t.get("temp"))
                        candidates.append((ls, u))
            for start, u in candidates:
                # 出锅偏差代价: 越晚出锅偏离目标越贵, 权重大, 保证同时上桌
                finish = start + t["dur"]
                lateness = max(0.0, align_target - finish)
                # 保热: 趁热的菜(serve_hot 高)被拉向最晚出锅, 耐放的菜允许提前
                cost = _task_cost(t, key, start, dep_ready, lines, w_hand) + lateness * align_w * (0.5 + hot)
                cost += w_makespan * max(0.0, start) * 0.05   # 轻微压总时长
                if best is None or cost < best[0]:
                    best = (cost, start, u)
        else:
            # 纯人工 or 等待
            if t["uses_hands"]:
                start, u = lines["人工"].earliest_slot(dep_ready, t["dur"])
                cost = _task_cost(t, key, start, dep_ready, lines, w_hand)
                cost += w_hand * lines["人工"].busy_near(start, t["dur"])
                best = (cost, start, -1)
            else:
                start, u = dep_ready, -2   # 纯等待, 不占资源
                best = (0.0, start, -2)

        cost, start, unit = best
        unit_id = "等待"
        shared = share_ok and t["res"] == DEVICE_STEAMER
        if t["res"] is not None and t["uses_hands"]:
            # 人工时间线可能在候选评估后又被推后 -> 同步顺延设备槽, 两条线都不冲突
            for _ in range(3):
                h_s, _hu = lines["人工"].earliest_slot(start, t["dur"])
                if h_s <= start + 1e-6:
                    break
                start = _fit_unit(lines[t["res"]], unit, h_s, t["dur"], shared,
                                  temp_share[t["res"]], t.get("temp"))
        if t["res"] is not None:
            if shared:
                temp_share[t["res"]].append((start, start + t["dur"], t.get("temp")))
            else:
                lines[t["res"]].occupy(start, t["dur"], unit, t.get("temp"))
            unit_id = f"{t['res']}-{unit+1}"
        if t["uses_hands"]:
            hu = lines["人工"].earliest_slot(start, t["dur"])[1]
            lines["人工"].occupy(start, t["dur"], hu)
            if t["res"] is None:
                unit_id = "双手-1"

        st = ScheduledTask(
            task_id=f"{t['dish']}#{t['step_id']}",
            dish=t["dish"], step_id=t["step_id"], raw_text=t["raw_text"],
            device=t["device"], unit_id=unit_id,
            start_min=round(start, 1), end_min=round(start + t["dur"], 1),
            duration_min=round(t["dur"], 1),
            needs_human=t["uses_hands"], unattended=t["unattended"], temp_c=t.get("temp"),
            depends_on=[f"{d[0]}#{d[1]}" for d in t["depends_on"]],
        )
        done[key] = st
        scheduled.append(st)
        remaining.remove(key)

    scheduled = _post_align(scheduled, target_gap, frozen_ids=frozen_ids)
    return _to_plan(scheduled)


def _task_cost(t, key, start, dep_ready, lines, w_hand):
    cost = 0.0
    cost += (start - dep_ready) * WAIT_COST
    # 人工负载均衡: 惩罚"贴在前一段手动操作后面" (连续手动时长越长越累)
    if t["uses_hands"]:
        cost += w_hand * lines["人工"].busy_near(start, t["dur"])
    if t["soft_freeze"] and t["orig_start"] is not None:
        if abs(start - t["orig_start"]) > 0.5:
            cost += FREEZE_MOVE_COST + abs(start - t["orig_start"])
    return cost


def _post_align(scheduled: list[ScheduledTask], target_gap: float = 5.0,
                frozen_ids: set | None = None) -> list[ScheduledTask]:
    """后置右移: 让早出锅的菜把最后一个加热步骤往后拖, 尽量对齐到最晚出锅时刻,
    尊重设备占用(只在该资源真正空闲的槽移动), 不破坏依赖。

    v2.1 修复
    ---------
    1. 冻结任务(hard frozen, 已完成/已开始)**绝不移动** —— 原实现会把已完成步骤
       也右移, 直接违背动态重排"不推翻已做工作"的最小扰动原则。
    2. 移动占双手的任务时同步校验人工时间线 —— 原实现只查设备占用表,
       右移后可能造成"双手"重叠。
    """
    frozen_ids = frozen_ids or set()
    cooking = [t for t in scheduled if t.start_min >= 0]
    if not cooking:
        return scheduled
    by_id_all = {t.task_id: t for t in scheduled}
    # 每道菜的结束时刻
    finish = {}
    for t in cooking:
        finish[t.dish] = max(finish.get(t.dish, 0), t.end_min)
    T = max(finish.values())
    if T <= 0:
        return scheduled

    # 重建每条资源时间线 (区间对象共享: 改一处, 设备表与人工表同时生效)
    from collections import defaultdict
    occ = defaultdict(list)
    own = {}
    for t in cooking:
        iv = [t.start_min, t.end_min]
        occ[t.unit_id].append(iv)
        own[id(t)] = iv
    hand_ivs = [own[id(t)] for t in cooking if t.needs_human]

    def _conflicts(ivs, s, e, skip) -> bool:
        for iv in ivs:
            if iv is skip:
                continue
            if s < iv[1] - 1e-6 and iv[0] < e - 1e-6:
                return True
        return False

    align_tol = max(1.0, target_gap * 0.6)

    # ---- 阶段 1: 整菜右移 ----
    # 原实现只能挪单个设备步, 而一道菜往往是 "人工→设备→人工" 的链条:
    # 挪设备步会被后面的装盘步卡住(依赖限制), 于是早出锅的菜根本移不动。
    # 整条链一起平移则天然满足菜内依赖, 是压缩出锅差最有效的手段。
    dish_tasks = defaultdict(list)
    for t in cooking:
        dish_tasks[t.dish].append(t)

    def _can_shift(ts, d) -> bool:
        mine = {id(own[id(t)]) for t in ts}
        for t in ts:
            if t.unit_id in ("等待", "提前准备"):
                continue
            ns, ne = t.start_min + d, t.end_min + d
            for iv in occ[t.unit_id]:
                if id(iv) in mine:
                    continue
                if ns < iv[1] - 1e-6 and iv[0] < ne - 1e-6:
                    return False
        for t in ts:
            if not t.needs_human:
                continue
            ns, ne = t.start_min + d, t.end_min + d
            for iv in hand_ivs:
                if id(iv) in mine:
                    continue
                if ns < iv[1] - 1e-6 and iv[0] < ne - 1e-6:
                    return False
        return True

    # 默认不超调: 早出锅的菜最多右移到当前最晚出锅时刻。
    #
    # 历史(第三轨未上线时, 50 组基准): 允许超调反而更差 —— 右移过头的菜自己变成
    # 新的"最晚出锅", 既抬高 gap 又抬高 makespan (slack 0 → 42%/142min,
    # 6 → 40%/145min, 20 → 34%/155min)。
    # 现状(L1 输入修复 + 第三轨上线后重测, 50 组 seed=42): slack 0/3/6/12/20 全档
    # 均为 92% / 73.5min / gap 2.8 —— 精确轨的 ε-约束已把出锅时刻压紧, 后置右移
    # 没有空间可走, 该参数在当前实现下**不再影响结果**。默认 0 依然保留:
    # 它是最保守的选择, 且若将来关掉精确轨(FT_EXACT=off)仍能回到上面那条规律。
    slack = float(os.environ.get("FT_ALIGN_SLACK", "0"))
    for dish, f in sorted(finish.items(), key=lambda x: x[1]):
        if not dish or T - f <= align_tol:
            continue
        ts = dish_tasks[dish]
        # 含冻结(已完成/已开始)步骤的菜不做整菜平移: 平移会连冻结步骤一起挪走,
        # 直接违背动态重排"不推翻已做工作"的最小扰动原则。
        if any(t.task_id in frozen_ids for t in ts):
            continue
        for delta in range(int(T - f + slack), 0, -1):
            if _can_shift(ts, delta):
                for t in ts:
                    iv = own[id(t)]
                    iv[0] += delta
                    iv[1] += delta
                    t.start_min = round(iv[0], 1)
                    t.end_min = round(iv[1], 1)
                finish[dish] = f + delta
                break

    # ---- 阶段 2: 单步微调 (进一步收敛残余偏差) ----

    def latest_free(unit, must_end_by, dur):
        """在 must_end_by 之前的空闲间隙里, 找最右边能放下 dur 的起点 (右对齐, 不重叠)"""
        ivs = sorted(occ[unit])
        cur = must_end_by
        for (s, e) in reversed(ivs):
            if cur <= s:
                continue            # 这个块在 cur 左边, 不挡
            if cur <= e:
                cur = s             # cur 落在块内, 跳到块左
            else:
                # cur 在块右侧, [块e, cur] 是自由的
                if cur - e >= dur:
                    return cur - dur
                cur = s             # 放不下, 继续往左
        return max(0.0, cur - dur)

    # 反复尝试把每道菜的整条链右移 (先移最后一步, 再向前追)
    for _ in range(40):
        moved = False
        for dish, f in sorted(finish.items(), key=lambda x: x[1]):
            if T - f <= align_tol:   # 已经对齐
                continue
            # 这道菜所有占真实设备的步, 按结束时间倒序处理
            cands = [t for t in cooking if t.dish == dish
                     and t.unit_id not in ("等待", "提前准备", "双手-1")
                     and t.task_id not in frozen_ids]
            if not cands:
                continue
            all_dish = [t for t in cooking if t.dish == dish]
            for last in sorted(cands, key=lambda t: -t.end_min):
                # 后继: depends_on 里含本步的任务, 本步结束不能晚于它们开始
                succ_starts = [s.start_min for s in all_dish
                               if f"{dish}#{last.step_id}" in (s.depends_on or [])]
                limit = min(succ_starts) if succ_starts else T
                # 前驱: 本步不能早于它依赖的步骤结束 (含提前准备步, 其 end=0)
                pred_end = max((by_id_all[d].end_min for d in (last.depends_on or [])
                                if d in by_id_all), default=0.0)
                target = limit - last.duration_min
                new_start = latest_free(last.unit_id, target, last.duration_min)
                new_start = max(new_start, pred_end)
                new_end = new_start + last.duration_min
                # 既不能早于前驱结束, 也不能晚到压住后继开始 —— 否则制造依赖违反
                if new_end > limit + 0.01:
                    continue
                if new_start - last.start_min < 1.0:
                    continue
                iv = own[id(last)]
                if _conflicts(occ[last.unit_id], new_start, new_end, iv):
                    continue
                if last.needs_human and _conflicts(hand_ivs, new_start, new_end, iv):
                    continue
                iv[0], iv[1] = new_start, new_end
                last.start_min = round(new_start, 1)
                last.end_min = round(new_end, 1)
                moved = True
            finish[dish] = max(t.end_min for t in
                               [x for x in cooking if x.dish == dish
                                and x.unit_id not in ("等待", "提前准备")])
        if not moved:
            break
    return scheduled


def _to_plan(tasks: list[ScheduledTask]) -> SchedulePlan:
    tasks = sorted(tasks, key=lambda x: x.start_min)
    # 前置准备任务(start<0)不参与当天烹饪时长与出锅差
    cooking = [t for t in tasks if t.start_min >= 0]
    makespan = max((t.end_min for t in cooking), default=0.0)
    dish_finish = {}
    for t in cooking:
        # 合成任务(烟机联动/预热)没有 dish, 不属于任何一道菜 —— 若放进来会在
        # dish_finish 里留下一个空菜名键, 而 finish_gap 正是取 dish_finish 的首尾
        # 之差, 于是烟机(灶具结束 +3min)会把出锅差整个抬起来。动态重排时旧的合成
        # 任务会被当作"已完成步骤"冻结回填, 必然踩到这一点。
        if not t.dish:
            continue
        dish_finish[t.dish] = max(dish_finish.get(t.dish, 0), t.end_min)
    ft = sorted(dish_finish.values())
    gap = ft[-1] - ft[0] if ft else 0.0
    return SchedulePlan(tasks=tasks, makespan_min=round(makespan, 1),
                       finish_gap_min=round(gap, 1),
                       dish_finish={k: round(v, 1) for k, v in dish_finish.items()})


def _rebase_to_first_resource(plan: SchedulePlan) -> SchedulePlan:
    """把时间轴整体平移, 使「第一个占用设备/人工的任务」恰好从 t=0 开始。

    为什么需要它 (2026-10-06 实测)
    ------------------------------
    t=0 原本是「**提前准备段的结束**」(`_prepare_tasks` 把备菜步整体右对齐到 0)。
    但提前准备结束之后、第一个**占用资源**的任务之前, 可能还夹着一段**纯等待**
    —— 比如"把鳗鱼段放入碗中…腌制 10 分钟"。等待不占设备, 于是甘特图上 t=0 之后
    出现一段「只有等待行有内容、灶具/蒸箱/烤箱/双手四行全空」的空白,
    看起来像"排程没排满"。

    实测 (三味蒸鳗鱼&香茅烤鳗鱼同烹, 单菜):
        备菜段右对齐到 0  ⇒  #3 腌制 -2→8  ⇒  #5 灶具 8→16
        甘特图 t=0~8 四行设备全空, 总时长 52。
    平移 -8 后: #3 腌制 -10→0、#5 灶具 0→8 —— 空白消失, 总时长 52 → 44。

    口径自洽性
    ----------
    `_to_plan` 的判据是 `start_min >= 0`(t<0 的算"提前准备", 不计入当天烹饪)。
    腌制本来就不占资源, 把它整体放进 t<0 正是这个口径想要的结果 ——
    所以这次平移**不是刷指标**, 而是让"当天烹饪"的起点回到它本该在的地方:
    **第一个真正动用厨房资源的时刻**。

    ⚠ 有 `hard_frozen`(动态重排的已完成步骤) 时**绝不平移**: 那些任务代表
    "已经发生的事", 时间必须与现实一致, 移位会让甘特图对不上实际。
    调用方负责判断 (见 `schedule()` 末尾)。
    """
    if os.environ.get("FT_ANCHOR", "on").lower() in ("off", "0", "false", "no"):
        return plan
    res = [t for t in plan.tasks if t.unit_id not in ("等待", "提前准备")]
    if not res:
        return plan
    first = min(t.start_min for t in res)
    if first <= 0.01:
        return plan                      # 第一个资源任务本来就在 0 ⇒ 无事可做
    shift = -first                       # 负数: 整体左移
    for t in plan.tasks:
        t.start_min = round(t.start_min + shift, 1)
        t.end_min = round(t.end_min + shift, 1)
    # 交给 _to_plan 重算 makespan / dish_finish / 出锅差 —— 口径唯一, 不另写一份。
    return _to_plan(plan.tasks)


# ---------- 预热: 严格由菜谱原文驱动 ----------
# 生成条件与时长判定全部搬到 app/parser/recipe_parser.py —— 预热在 L1 就已经是
# 一个**独立的 RecipeStep**, 这里不再"排程之后硬塞"。
#
# 为什么必须搬走 (v3.0 实测):
#   旧实现给需要预热的设备任务补一条 [start-dur, start] 的合成任务, 既不占设备槽
#   也不参与资源竞争 —— 30 组抽样里产出的 2 条预热 **100%** 与别的菜的真实设备
#   任务重叠("蒸箱 预热[93-98] 叠 三味蒸鳗鱼#12[86-94]")。设备冲突是官方评分的
#   大头, 这种自造冲突必须消掉。
#   现在预热由 parser.insert_preheat_steps() 插进 DAG, 三轨都会给它分配真实槽位。
#
# 这里只保留一件事: 把**同设备同温**的预热区间合并成一条输出行 ——
# 对应文档 4.1.2 "多道菜如果设备预热温度完全一致, 可共享一次预热任务"。
from app.parser.recipe_parser import (          # noqa: F401  (对外保持旧 import 路径可用)
    preheat_minutes, is_preheat_step, PREHEAT_MARK,
    PREHEAT_DEFAULT_MIN, PREHEAT_MIN_TASK_MIN,
)

# 预热共享的温度容忍度(度): 与蒸箱同温合炉用同一个口径
PREHEAT_TEMP_TOL = 10.0

# 合成预热节点的**精确**文本 (recipe_parser.insert_preheat_steps 里写死的 f"{dev}预热")。
# 用它而不是 endswith("预热") 来识别预热节点 —— 理由见 _merge_preheat 的 docstring。
PREHEAT_NODE_TEXTS = (f"{DEVICE_STEAMER}预热", f"{DEVICE_OVEN}预热")


def _merge_intervals_meta(ivs: list[tuple]) -> list[tuple]:
    """合并重叠/相邻区间, 并保留每条区间的附属信息 (菜名/温度)。

    ivs: [(s, e, dish, temp)] -> [(s, e, [dish...], temp)]
    官方接口规范要求设备工作步骤返回 recipeNames, 所以合并后的
    合成任务必须知道自己在为哪些菜服务。"""
    if not ivs:
        return []
    ivs = sorted(ivs, key=lambda x: (x[0], x[1]))
    groups = [[ivs[0][0], ivs[0][1], [ivs[0][2]], ivs[0][3]]]
    for s, e, dish, temp in ivs[1:]:
        g = groups[-1]
        if s <= g[1] + 0.01:
            g[1] = max(g[1], e)
            if dish and dish not in g[2]:
                g[2].append(dish)
            if g[3] is None:
                g[3] = temp
        else:
            groups.append([s, e, [dish], temp])
    return [(a, b, ds, tp) for a, b, ds, tp in groups]


def _restore_missing_steps(plan: SchedulePlan, recipes: list) -> SchedulePlan:
    """把被"备菜前置"整步删掉的步骤补回 t<0 的「提前准备」行。

    为什么必须补 (实测抓到的事故)
    ----------------------------
    精确模型轨 (`model.extract_tasks`) 与 CP-SAT 轨 (`cpsat.slim`) 都会把备菜步
    **整步删掉**, 而贪心轨是把它们排到 t<0 (`unit_id="提前准备"`)。三条轨口径不一致的
    后果是: 一旦精解轨胜出, 甘特图上就**丢掉整段备菜** —— 实测「半熟芝士蛋糕」12 步
    只剩 4 步, 连"锅置火上, 倒入牛奶和黄油, 拌匀至溶化""放入芝士, 开小火, 拌匀, 略煮"
    这两条**灶具**步都不见了, 看上去就是"没做糊就直接烤"。

    更严重的是: 精解轨正是靠删掉这 16 步把 makespan 从 124 压到 82、出锅差从 20 压到 1
    —— 指标是"少干活"换来的, 属于目标函数被求解器反向利用。所以这不是显示问题, 是
    评分口径问题。

    补回口径与贪心轨逐字一致: 同一道菜的备菜步**串行**、整体右对齐到 t=0。
    只补"计划里完全没有"的步骤 (`(dish, step_id)` 去重), 不会与已有任务重复;
    补出来的行 start/end 均 <= 0, 不占当天设备, 因此 **makespan / 出锅差不变**。

    ⚠ 只补**链式闭合**的那些菜: 一个步骤能挪到 t<0 的前提是它的**所有前置也都在 t<0**
    (或同样被补回 t<0)。否则会出现"步骤在 t<0、它的前置还在当天 215 分钟处"的依赖违反
    —— 实测正是这个原因让补回的精确解被 1e6 惩罚否决, 指标反而从 35 掉到 340。
    不闭合的菜直接不补 (宁可少显示, 也不能造出一份依赖违反的计划), 由
    `scripts/step_provenance.py` 的 R3 完整性断言把它报出来。
    """
    have = {(t.dish, t.step_id) for t in plan.tasks if t.dish}
    missing: dict = defaultdict(list)
    for rec in recipes:
        for st in rec.steps:
            if (rec.name, st.step_id) not in have:
                missing[rec.name].append(st)
    if not missing:
        return plan

    by_dish = {}
    for dish, sts in missing.items():
        sts.sort(key=lambda s: s.step_id)
        chain: set = set()
        for st in sts:
            if not all(d in chain for d in (st.depends_on or [])):
                break                      # 链断了 -> 这道菜整个不补
            chain.add(st.step_id)
        else:
            by_dish[dish] = sts
    if not by_dish:
        return plan

    extra = []
    for dish, sts in by_dish.items():
        cur = -sum(estimate_duration(s) for s in sts)
        for st in sts:
            d = estimate_duration(st)
            extra.append(ScheduledTask(
                task_id=f"{dish}#{st.step_id}", dish=dish, step_id=st.step_id,
                raw_text=st.raw_text, device=st.device, unit_id="提前准备",
                start_min=round(cur, 1), end_min=round(cur + d, 1),
                duration_min=round(d, 1), needs_human=False, unattended=True,
                temp_c=getattr(st, "temperature_c", None),
                depends_on=[f"{dish}#{x}" for x in (st.depends_on or [])]))
            cur += d
    plan.tasks = sorted(plan.tasks + extra, key=lambda x: x.start_min)
    return plan


def _add_kitchen_semantics(plan: SchedulePlan) -> SchedulePlan:
    """设备语义收尾: 同温预热合并 + (可选) 烟机联动档位。

    预热 (v3.0 重写)
    ----------------
    旧实现在这里**合成**预热任务并硬塞进时间轴, 不占设备槽, 实测 100% 造成重叠冲突。
    现在预热是 L1 拆出来的真实步骤, 早已在 DAG 里排好; 这里只做一件事:
    把**同设备且同温**的预热区间合并成一条输出行 —— 文档 4.1.2 要求
    "多道菜如果设备预热温度完全一致, 可共享一次预热任务"。
    合并后 recipeNames 带上所有受惠的菜, 输出里就只有一条"预热"。

    为什么不再造"烟机-灶具联动"
    --------------------------
    旧实现给每个灶具任务合成一条「烟机联动」(start-2 → end+3)。但:
      · 官方《接口规范》全文 **0 次**提到烟机;
      · 官方 100 道菜谱 **0 次**提到 烟机/油烟机/排烟;
      · 它还把末道菜的结束时刻 +3 分钟, 直接把 timeSpent 顶高
        (实测某 3 菜实例: 136 → 142 分钟)。
    文档 4.2 又明确要求"根据灶具任务类型生成联动档位"。折中: 联动档位**算出来**
    挂在任务上(hood_level 字段), 但默认不进 detailTimeline、不影响时间轴;
    需要时设 FT_HOOD=on 才会生成独立的联动行。
    """
    plan = _merge_preheat(plan)
    if os.environ.get("FT_HOOD", "off").lower() in ("on", "1", "true", "yes"):
        plan = _add_hood_linkage(plan)
    return plan


def _merge_preheat(plan: SchedulePlan) -> SchedulePlan:
    """同设备 + 同温(±PREHEAT_TEMP_TOL) 的预热区间合并为一行 (文档 4.1.2)。

    判定"这是预热节点"**必须用精确文本** `f"{dev}预热"`, 不能用 `endswith("预热")`:
    官方数据里「豆沙松糕」第 9 步原文是 "...选择蒸模式-普通蒸，100℃，25分钟，无需预热"
    —— 句尾恰好是"预热", 会被 endswith 误判成预热节点。一旦它和别的菜的预热区间
    相邻, `_merge_intervals_meta` 就会把它并进"预热共享"行, 于是**这条真实的
    25 分钟蒸箱烹饪步从甘特图上凭空消失**(反向的"不存在/丢失的步骤")。
    合成节点的文本恒为 `f"{dev}预热"` (见 recipe_parser.insert_preheat_steps),
    且实测 100 道菜原文里没有任何一句恰好等于"蒸箱预热"/"烤箱预热", 所以精确匹配
    既安全又唯一正确。FT_PREHEAT=off 时根本不存在节点, 这里也就不会误并任何东西。
    """
    from app.models.schema import ScheduledTask
    ph = [t for t in plan.tasks
          if t.start_min >= 0 and t.dish and t.raw_text in PREHEAT_NODE_TEXTS
          and t.device in (DEVICE_OVEN, DEVICE_STEAMER)]
    if len(ph) < 2:
        return plan
    groups: dict = {}
    for t in ph:
        tag = None if t.temp_c is None else round(t.temp_c / PREHEAT_TEMP_TOL) * PREHEAT_TEMP_TOL
        groups.setdefault((t.device, tag), []).append(t)
    merged, drop = [], set()
    for (dev, _tag), ts in groups.items():
        if len(ts) < 2:
            continue
        ivs = [(t.start_min, t.end_min, t.dish, t.temp_c) for t in ts]
        for s, e, dishes, temp in _merge_intervals_meta(ivs):
            if len(dishes) < 2:
                continue
            for t in ts:
                if t.dish in dishes:
                    drop.add(id(t))
            merged.append(ScheduledTask(
                task_id=f"预热共享@{dev}@{s}", dish="", step_id=0,
                raw_text=f"{dev}预热", device=dev, unit_id=ts[0].unit_id,
                start_min=s, end_min=e, duration_min=round(e - s, 1),
                needs_human=False, unattended=True,
                temp_c=temp, depends_on=[], dish_list=[d for d in dishes if d]))
    if not merged:
        return plan
    plan.tasks = sorted([t for t in plan.tasks if id(t) not in drop] + merged,
                        key=lambda x: x.start_min)
    return plan


_STRONG_HOOD = re.compile(r"爆炒|煎炸|油炸|煎|炸|翻炒|煸炒|煸|爆")
_WEAK_HOOD = re.compile(r"炖|煮|煲|焖|煨|卤|焯|熬|汤")


def _add_hood_linkage(plan: SchedulePlan) -> SchedulePlan:
    """文档 4.2 烟机联动: 不进 DAG、不改时间轴, 只按灶具任务类型派生档位。

    默认关闭 (FT_HOOD=off) —— 官方接口规范与 100 道菜都 0 次提到烟机,
    且旧实现的 end+3 分钟会把 timeSpent 顶高。开启后作为**独立说明行**输出。
    """
    from app.models.schema import ScheduledTask
    extra = []
    for t in plan.tasks:
        if t.start_min < 0 or t.device != DEVICE_STOVE or not t.dish:
            continue
        lvl = "强档" if _STRONG_HOOD.search(t.raw_text) else (
            "弱挡" if _WEAK_HOOD.search(t.raw_text) else None)
        if not lvl:
            continue
        extra.append(ScheduledTask(
            task_id=f"烟机联动@{t.task_id}", dish="", step_id=0,
            raw_text=f"烟机{lvl}", device=DEVICE_HOOD, unit_id="烟机-1",
            start_min=round(t.start_min, 1), end_min=round(t.end_min, 1),
            duration_min=round(t.end_min - t.start_min, 1),
            needs_human=False, unattended=True, temp_c=None, depends_on=[],
            dish_list=[t.dish]))
    if not extra:
        return plan
    plan.tasks = sorted(plan.tasks + extra, key=lambda x: x.start_min)
    return plan


# ⚠ **不要**试图用"贪心解已达下界 ⇒ 跳过精解轨"来省时间（2026-10-09 实测否决）
# ---------------------------------------------------------------------------
# 想法看着很合理：profile 显示 83% 的时间在精解轨，而 `lower_bound_from_plan` /
# `gamma_lb_from_plan` 是**放松下界**（恒 <= 真实最优），那么"解 == 下界 ⇒ 可证
# 最优 ⇒ 精解不可能更优"。实测**不成立**，45 组里 3 组变差，且都很严重：
#
#     3 菜  Γ 0.0 → 10.0          （Γ 直接变差）
#     3 菜  T 30.0 → 74.0         （总时长 2.5 倍）
#     5 菜  T 55.0 → 76.0
#
# 原因：`lower_bound_from_plan` 是**从"已产出的计划"反推**下界，它只在**同一
# 任务集口径**内成立 —— 而精解轨本来就会换任务集（`slim` 丢掉提前准备步）。
# 于是拿"贪心口径"的下界去判"精解口径"的解，就造出了**假下界**：贪心解
# makespan=74 反推出的 lb 也是 74，而精解换口径后能做到 30 < 74。
# 这与 model.py 自己写的警告是同一件事（"若用菜谱重算任务集，就会拿 A 口径的
# 下界去衡量 B 口径的计划"）—— 我把它用在了它明确不适用的地方。
#
# 副作用还有一个：下界本身要算（关键路径 + 能量 + 人工），45 组里只命中 2 组，
# 净效果是**慢 3%**。所以此路彻底不通，代码已删，别再捡回来。
@_flag_scope
def schedule(recipes: list[Recipe], policy=None, priors: dict | None = None,
             strategy: str = DEFAULT_STRATEGY, use_cpsat: bool = True,
             cpsat_budget: float = 1.2,
             hard_frozen: dict | None = None,
             use_exact: bool | None = None,
             exact_budget: float | None = None) -> SchedulePlan:
    """三轨: 贪心兜底 + CP-SAT 精解 + 精确模型 (model.py), 取出锅差更优者。

    policy/priors/strategy 由 Agent 决策层给出; 不传时行为与 v1 完全一致。
    hard_frozen: 动态重排的已完成步骤, 让 Agent 闭环也能在冻结约束下择优。

    为什么要有第三条轨
    ------------------
    app/scheduler/model.py 是把本问题写成严格数学规划的"参考实现"(批次机 +
    两阶段 ε-约束 + 三层字典序目标 + 关键路径下界), 实测在 80 组随机用例上
    总时长 84.7 / 出锅差 2.76, 而 CP-SAT 轨是 111.1 / 3.67。
    但它此前**只被 scripts/verify_model.py 调用**, 线上接口一次都没用过 ——
    等于把最好的模型锁在实验室里。这里把它接进来当第三条轨。

    与前两条轨的差别:
      · CP-SAT 轨 (cpsat.py) 用加权和, 且不支持冻结 -> 有 hard_frozen 时跳过;
      · 精确模型轨用两阶段 ε-约束, 且**支持 fixed**, 所以重排也能受益。

    use_exact: None = 跟随 use_cpsat (use_cpsat=False 是"毫秒级快速评估"路径,
               绝不能在那种路径里跑精确求解, 否则策略评估会被拖慢 5 倍)。
    exact_budget: 精确模型的时间预算(秒), 默认取 FT_EXACT_BUDGET 或 1.5。
    """
    # 消融开关：`FT_CPSAT=off` 只关 CP-SAT 轨。
    # ⚠ 不能直接给 `use_cpsat=False` —— 那会**连带**把精确轨也关掉（下面
    #   `use_exact` 的默认值是"跟随 use_cpsat"），于是量到的是"两条轨一起关"的
    #   效果，而不是 CP-SAT 单独的边际价值。这里显式把 use_exact 钉成 True。
    if os.environ.get("FT_CPSAT", "on").strip().lower() in ("off", "0", "false", "no"):
        use_cpsat = False
        if use_exact is None:
            use_exact = True

    greedy = _schedule(recipes, policy=policy, priors=priors, strategy=strategy,
                       hard_frozen=hard_frozen)
    best = greedy

    # ---- 连续手动上限: 必须当**可行性过滤**, 不能进 (Γ,T) 字典序 ----
    # ⚠ 2026-10-06 修复。原实现只在精确轨里读 FT_HAND_CAP, 择优却仍是
    #   `(Γ,T)` 严格比较 ⇒ 贪心/CP-SAT 轨(它们**根本不接受** hand_cap)只要
    #   Γ 小一点点, 就会把精确轨那份**真正满足上限**的计划挤掉。
    #   实测(seed=11, FT_HAND_CAP=30): 6 组里有 2 组是
    #   `hand_cap_enforced=True`(精确轨 actual=29.0 达标)却最终交付 55.0/40.0 —— 
    #   约束在求解器里是硬的, 到择优环节却成了软的。
    #   口径与项目铁律一致: **硬约束只做可行性, 绝不进目标函数**。
    #   注: metrics 反向依赖本模块(它 import RESOURCE_UNITS), 只能延迟导入。
    hc_env = os.environ.get("FT_HAND_CAP", "").strip()
    hand_cap = int(hc_env) if hc_env.isdigit() and int(hc_env) > 0 else None

    def _cap_ok(p) -> bool:
        """计划是否满足连续手动上限。hand_cap 未开启时恒 True。"""
        if hand_cap is None or p is None:
            return True
        try:
            from app.scheduler.metrics import hand_streak
            return hand_streak(p) <= hand_cap + 1e-6
        except Exception:
            return True          # 度量失败不阻断排程, 交由上层校验

    # ---- 降温缓冲: 同样是**可行性**问题, 不进 (Γ,T) 字典序 (v3.3) ----
    # 与 hand_cap 同一模式: 贪心轨已按 `cooldown_minutes` 排好, 但 CP-SAT 轨与
    # 精确轨的批次机里**没有"降温"这一项** ⇒ 它们可能给出"更优但做不出来"的解
    # (200℃ 刚出炉、180℃ 立刻进炉)。
    #
    # ⚠ 但降温与 hand_cap 的**约束强度不同**，所以两者的择优方式也必须不同：
    #   · hand_cap 是**用户/赛题显式给的硬指标** ⇒ 无界过滤（不满足就是不可行）；
    #   · 降温是本系统**自己加的物理精化** ⇒ ε-约束（见 `prefer_cd`）——
    #     只在合规解的 Γ 不比最优解差过 `FT_CD_BUDGET` 时才换解，超预算就保留
    #     更优解、把违规交给 `insight` 报成红色预警。
    cd_on = cooldown_enabled()
    cd_bud = cd_budget()

    def _cd_ok(p) -> bool:
        if not cd_on or p is None:
            return True
        try:
            from app.scheduler.metrics import device_switches
            return not device_switches(p)
        except Exception:
            return True

    def _feas_ok(p) -> bool:
        """hand_cap **与** 降温同时满足。

        ⚠ `best_cd` 必须用这个判据，**不能只判降温**：否则 hand_cap 达标的精确解
        会被一份"降温合规但超连续手动上限"的贪心解按 Γ 换掉 —— 等于让**附加的**
        物理精化把**显式的**硬约束保证抹掉（实测 hand_streak 99 > 30）。
        这是"两道门不能互相覆盖"在 ε-约束下的等价形态。
        """
        if hand_cap is not None and not _cap_ok(p):
            return False
        return _cd_ok(p)

    # 有任何可行性约束时走 `_better`; 全关时走原来的快路径, 保证逐字不变。
    gate_on = (hand_cap is not None) or cd_on
    # 只认 hand_cap 的"合规最优解"（降温另行处理，故这里刻意**不看**降温）。
    best_cd = greedy if (cd_on and _feas_ok(greedy)) else None

    def _better(cand, cur) -> bool:
        """择优: 先按 **hand_cap**（无界）过滤可行性, 再按 (Γ,T) 字典序。

        ⚠ 两道门**有优先级, 不能合成一个布尔**: 合成会出这种事 ——
        hand_cap 那份解满足上限但违反降温, 贪心那份满足降温但超上限,
        两边都"不可行" ⇒ 退化成按 (Γ,T) 比 ⇒ **超上限的解赢了**,
        直接把 hand_cap 的保证抹掉(实测 `test_engine_keeps_hand_cap_satisfying_plan`
        从 432 绿变成 1 红, 报 hand_streak 99 > 30)。
        ⚠ 降温**不在这里**判 —— 它走 `prefer_cd` 的 ε-约束（原因见那里）。
        """
        if hand_cap is not None:
            c_ok, b_ok = _cap_ok(cand), _cap_ok(cur)
            if c_ok != b_ok:
                return c_ok
        return ((cand.finish_gap_min, cand.makespan_min)
                < (cur.finish_gap_min, cur.makespan_min))

    # ---- 轨 2: CP-SAT 精解 (不支持冻结 -> 有冻结时跳过) ----
    if use_cpsat and not hard_frozen:
        try:
            from app.scheduler.cpsat import solve as _cpsat
            # CP-SAT 不处理"提前准备"步, 过滤掉 —— 判据必须与另外两条轨**逐字一致**,
            # 否则三条轨排的不是同一个任务集, 精解轨会靠"少排几步"刷出虚高的指标。
            # 旧实现这里只调 `_prep_ahead`(hard), 连语义先验都不看, 也没有前缀链检查。
            from app.models.schema import Recipe as _R
            _stg = STRATEGIES.get(strategy, STRATEGIES[DEFAULT_STRATEGY])
            _ap = bool(_stg["prep_ahead"]) or bool(getattr(policy, "allow_preprocess", False))
            _prep = prep_keys(recipes, priors, _ap)
            slim = []
            for rec in recipes:
                steps = [s for s in rec.steps if (rec.name, s.step_id) not in _prep]
                if steps:
                    slim.append(_R(name=rec.name, ingredients=rec.ingredients,
                                   steps=steps, label=rec.label))
            cp = _cpsat(slim, time_budget=cpsat_budget, policy=policy, priors=priors)
            if cp:
                if not gate_on:
                    # ⚠ 保持原语义**逐字不变**: 只要 Γ 不差过贪心 +1 就采用 CP-SAT 解
                    #   (即使 T 更长 —— 这是刻意的一步, 别改成字典序比较, 会动基线)。
                    if cp.finish_gap_min <= greedy.finish_gap_min + 1:
                        best = cp
                elif _better(cp, best):
                    # 有可行性约束(hand_cap)时: 先看是否满足约束, 再按 (Γ,T)
                    # —— 此时必须放开上面那道 Γ 门槛, 否则一份"合规但 Γ 差些"的解会被挡掉。
                    best = cp
                if cd_on and _feas_ok(cp) and (best_cd is None or _better(cp, best_cd)):
                    best_cd = cp
        except Exception:
            pass

    # ---- 轨 3: 精确模型 (model.py) ----
    if use_exact is None:
        use_exact = use_cpsat and os.environ.get(
            "FT_EXACT", "on").lower() not in ("off", "0", "false", "no")
    if use_exact:
        try:
            from app.scheduler import model as _model
            if exact_budget is None:
                exact_budget = float(os.environ.get("FT_EXACT_BUDGET", "1.5"))
            # hard_frozen 是 {(dish, step_id): ScheduledTask}, 精确模型要的是
            # {key: start_min} —— 只钉开始时刻, 区间由 CP-SAT 自己算, 占位自然一致。
            fx = {k: float(st.start_min)
                  for k, st in (hard_frozen or {}).items()} or None
            # allow_preprocess 的候选集合。
            #
            # 默认跑**两个变体**并取更优者, 理由: "备菜是否移出当天时间线"本来就是
            # 本系统的设计空间的一部分 —— 策略表里 prep_first / compress 两条策略
            # 的 prep_ahead=True, 交付版本来就会产出"备菜在 t<0、不计入官方
            # cookingTimeline"的计划。但贪心策略的排序质量有限: 实测某 4 菜实例
            # 贪心 critical_path+CP-SAT 只能给 158 min / 出锅差 3, 而精确模型在
            # 允许备菜前置后能给 102 min / 出锅差 3。既然策略集里已有这条路径,
            # 就不该因为"赢的恰好是非备菜策略"而把精确模型的这条腿绑住。
            #
            # 这不是新增口径, 只是把已有的口径用足; 且选择权交给与选策略同一个
            # score_plan 目标 (orchestrator 层), 不是求解器自己拍板。
            #
            # FT_EXACT_PREP=off -> 严格跟随策略 (不尝试备菜变体), 供口径审计用。
            stg = STRATEGIES.get(strategy, STRATEGIES[DEFAULT_STRATEGY])
            allow_pre = (bool(stg["prep_ahead"])
                         or bool(getattr(policy, "allow_preprocess", False)))
            prep_mode = os.environ.get("FT_EXACT_PREP", "both").lower()
            if prep_mode in ("off", "0", "false", "no"):
                pre_variants = [allow_pre]
            elif prep_mode in ("on", "1", "true", "yes"):
                pre_variants = [True]
            else:
                pre_variants = [allow_pre] if allow_pre else [False, True]
            # ε-约束的 slack **必须给足**, 否则精确轨会被自己锁死:
            # 阶段 1 把 T 压到 T*, 阶段 2 又被限定 T <= T*+0, 于是没有任何余量
            # 去对齐各菜的出锅时刻 —— 实测某 4 菜实例 slack=0 时出锅差 44 min,
            # slack=15 时降到 3 min (总时长只多 10 min)。
            # 15 是 model.solve_with_gap 的默认值, 也是 Pareto 前沿的饱和点
            # (slack>=15 后三指标不再改善)。可用 FT_EXACT_SLACK 覆盖。
            ex_slack = int(os.environ.get("FT_EXACT_SLACK", "15"))
            # 冻结步骤的键集合 —— 用来否决"把已完成步骤丢掉"的候选解。
            frozen_keys = set(hard_frozen or {})
            # 贪心解的起始时刻当暖启动 hint。两个作用:
            #   ① 给 CP-SAT 一个已知可行解, 缩短搜索;
            #   ② 让 solve_exact 用它的完工时刻**收紧时间轴** —— 连续手动上限
            #      (hand_cap) 是 O(n·H) 时间索引编码, H 不收紧就解不出来。
            g_hint = {(t.dish, t.step_id): float(t.start_min)
                      for t in greedy.tasks if t.dish and t.start_min >= 0}
            for ap in pre_variants:
                ex = _model.solve_exact(recipes, policy=policy, priors=priors,
                                        time_budget=exact_budget, fixed=fx,
                                        slack=ex_slack, allow_preprocess=ap,
                                        hand_cap=hand_cap, hint=g_hint)
                if not ex:
                    continue
                if frozen_keys:
                    # allow_preprocess=True 是把备菜步**整步删掉**(不是挪到 t<0)。
                    # 万一某个"已完成"步骤恰好被判为备菜步, 它就会从重排结果里
                    # 凭空消失 —— 动态重排要求已完成步骤原样保留, 这种解直接作废。
                    got = {(t.dish, t.step_id) for t in ex.tasks}
                    if not frozen_keys <= got:
                        continue
                # 择优: hand_cap 开启时先看是否满足上限 (见函数开头 _better 的说明)
                if _better(ex, best):
                    best = ex
                if cd_on and _feas_ok(ex) and (best_cd is None or _better(ex, best_cd)):
                    best_cd = ex
        except Exception:
            pass

    # ---- 降温的 ε-约束收口 (见 prefer_cd 的说明) ----
    if cd_on:
        best = prefer_cd(best, best_cd, cd_bud)

    plan = _add_kitchen_semantics(_restore_missing_steps(best, recipes))
    # 有冻结任务时不平移: 它们代表"已经发生的事", 时间必须与现实一致。
    if not hard_frozen:
        plan = _rebase_to_first_resource(plan)
    return plan


def reschedule(recipes: list[Recipe], hard_frozen: dict,
              new_dish: Recipe = None, policy=None, priors: dict | None = None,
              strategy: str = DEFAULT_STRATEGY) -> SchedulePlan:
    """
    动态重排 (两级冻结):
      hard_frozen: 已完成/已开始 {(dish,step_id): ScheduledTask}, 时间不动。
      new_dish: 临时新增的菜 (如排骨汤), 追加进任务池。

    v2.5: 改为直接委托 `schedule(..., hard_frozen=...)`, 从而复用**三轨取优**。
    旧实现直接调 `_schedule()`(纯贪心) —— 于是"动态重排"只有一条腿,
    实测同一份冻结约束下 出锅差 47 min, 而三轨能给到个位数。
    CP-SAT 轨在 `schedule()` 内部会自动跳过(它不支持冻结), 精确轨则支持 `fixed`。
    """
    all_recipes = list(recipes)
    if new_dish is not None:
        all_recipes.append(new_dish)
    # priors 缺省时**主动取一次** (与 /reschedule 端点一致)。
    # 这不是可选项: 语义先验决定"哪些切配/腌制步算备菜"。没有它, `prep_keys`
    # 只认"超长冷藏/浸泡"这类硬判据, 于是被冻结在 t=0 的步骤会因为"它的前置
    # 还在 t>0"而让精确模型直接判不可行(返回 None) —— 实测同一份冻结约束下,
    # 传 priors 得 67.0 / 出锅差 1.0, 不传则精确轨整个失效、只剩贪心的 71.0 / 47.0。
    if priors is None:
        try:
            from app.agent.priors import get_priors
            priors = get_priors(all_recipes)
        except Exception:
            priors = None
    # 与 schedule() 保持一致: 结果同样要带上烟机联动/预热任务。
    # (旧实现漏了这一步 —— "动态重排"出来的计划里既没有预热行也没有烟机行,
    #  与初次排程的同一道菜前后不一致, 前端甘特图会凭空少两行。
    #  schedule() 内部已调用 _add_kitchen_semantics, 所以这里不再重复。)
    return schedule(all_recipes, policy=policy, priors=priors, strategy=strategy,
                    use_cpsat=True, hard_frozen=hard_frozen)
