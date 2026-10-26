"""
结构化数据模型 (L1 输出标准 Schema)

所有模块共用的数据结构定义。
- RecipeStep: 单条解析后的烹饪步骤
- Recipe: 一道菜的完整结构化表示
- ScheduledTask / SchedulePlan: 排程结果

注: 设备"能力参数"（合法模式 / 温度与时长区间）不在本文件 —— 唯一实现在
`app/spec/device_spec.py::DEVICE_SPEC`；"容量"（几台设备）在
`app/scheduler/engine.py::RESOURCE_UNITS` + 请求级 `resource_override()`。
本文件曾有 `DeviceConfig`/`DeviceUnit` 两个类 + `app/models/kitchen.py::default_kitchen()`，
是一套**从未接线的**早期设计（全仓库零引用），已于 v3.1 删除 ——
留着会让读代码的人以为设备配置在这里。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# 七类可调度设备
DEVICE_HOOD = "烟机"
DEVICE_STOVE = "灶具"
DEVICE_STEAMER = "蒸箱"
DEVICE_OVEN = "烤箱"
DEVICE_FRIDGE = "冰箱"
DEVICE_DISHWASHER = "洗碗机"
DEVICE_WATER = "净咖一体机"   # 以《设备参数清单参考》与设计文档 §1.2 的命名为准
                             # (赛题细则正文写"净水机", 解析层保留它作别名, 见 device_spec)
DEVICE_NONE = "人工"  # 纯人工操作, 不占用设备

DEVICES = [DEVICE_HOOD, DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN,
           DEVICE_FRIDGE, DEVICE_DISHWASHER, DEVICE_WATER]

# 工序大类
PREP = "切配"      # 切/洗/剥/配料, 纯人工
MARINADE = "腌制"   # 腌制等待
COOK = "烹饪"      # 加热烹饪
WAIT = "等待"      # 冷藏/静置等待
PLATE = "上桌"      # 收尾/装盘/撒葱花/淋汁, 30~60 秒的短人工动作
                   # (旧实现把这类句子标成"切配", 于是被估成 2 分钟;
                   #  一道菜的最后一步多是它, 直接成为"出锅差"的下界)


@dataclass
class RecipeStep:
    step_id: int
    raw_text: str
    device: str = DEVICE_NONE          # 七类设备 or 人工
    device_source: str = ""            # 原文命中的设备词, 如 "蒸烤一体机"
    mode: str = ""                     # 普通蒸/烘烤/翻炒/熬煮...
    temperature_c: Optional[float] = None
    duration_min: Optional[float] = None   # 统一换算为分钟
    needs_human: bool = True           # 是否需要人工介入(值守/操作)
    unattended: bool = False           # 是否无人值守(炖/焖/蒸/烤/冷藏等待)
    prep_type: str = COOK
    depends_on: list = field(default_factory=list)   # 同菜内前置 step_id
    notes: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class Recipe:
    name: str
    ingredients: list = field(default_factory=list)
    steps: list = field(default_factory=list)   # list[RecipeStep]
    label: str = ""

    def to_dict(self):
        d = asdict(self)
        return d


@dataclass
class ScheduledTask:
    task_id: str            # 唯一, e.g. "清蒸鳜鱼#step4"
    dish: str
    step_id: int
    raw_text: str
    device: str
    unit_id: str = ""
    start_min: float = 0    # 相对开饭倒数的绝对时间轴(分钟)
    end_min: float = 0
    duration_min: float = 0
    needs_human: bool = False
    unattended: bool = False
    temp_c: Optional[float] = None
    depends_on: list = field(default_factory=list)
    # 合成任务(预热/烟机联动)本身没有 dish, 但它们服务于哪些菜要能说清 ——
    # 官方接口规范要求设备工作步骤(type=3/4/5)必须返回 recipeNames。
    dish_list: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class SchedulePlan:
    tasks: list = field(default_factory=list)   # list[ScheduledTask]
    makespan_min: float = 0                     # 总时长
    finish_gap_min: float = 0                    # 最后一道与第一道出锅时间差
    dish_finish: dict = field(default_factory=dict)  # {菜名: end_min}
    notifications: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)
