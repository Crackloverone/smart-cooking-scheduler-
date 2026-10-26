"""
设备能力模型 (对齐《核心调度算法设计文档》3.1.2 / 3.1.3 与官方《设备参数清单参考》)

三件事:
  1. 设备别名映射   (3.1.2): "常规烘焙"/"蒸烤盘"/"方太智能烹饪设备" 等别名
                             归一到标准设备 + 标准模式
  2. 能力模型       (2.2)  : 每种设备的合法模式枚举, 以及每个模式的温度/时长范围
  3. 参数合法性校验 (3.1.3): 超出范围的自动修正到合法边界值

为什么必须有这一层
------------------
审计(2026-10-01)发现, 缺了它会有三类实打实的错误:
  · "蒸烤盘" 是**配件**(烤盘), 却被 DEVICE_RULES 的 "蒸烤" 词条命中 -> 判成蒸箱。
    榴莲酥/法式羊排明明是烤菜(烤盘+油纸+蒸烤一体机), 整道菜被排进蒸箱。
  · "常规烘焙" 是烤箱的一个模式名, 9 道菜在用, 既没被识别成设备也没被识别成模式。
  · 越界参数直接透传: 烤箱 40℃ (低于下限 60)、烤箱 240℃ (高于上限 230)。
    官方规范 5 要求"设备参数必须合法", 这是可以被机器判出来的硬伤。
  · 66 条蒸/烤步骤没有合法模式, 适配器回落的 "标准模式" **不在官方枚举里**,
    等于又造了一个非法值。
"""
from __future__ import annotations
import re
from typing import Optional

from app.models.schema import (
    DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN, DEVICE_FRIDGE,
    DEVICE_HOOD, DEVICE_DISHWASHER, DEVICE_WATER, DEVICE_NONE,
)

# ---------- 内部哨兵: 不是真设备, 只是解析过程中的中间态 ----------
PART = "__part__"        # 配件(烤盘/蒸架), 不能当设备用
AMBIGUOUS = "__amb__"    # 蒸烤一体机这类复合设备, 需按上下文消歧

# ---------- 3.1.2 设备别名映射表 ----------
# 顺序即优先级, 长词在前。每项: (别名列表, 标准设备, 顺带确定的标准模式或 None)
DEVICE_ALIAS_RULES = [
    # 配件: 命中就跳过, 绝不能拿它当设备 (否则"蒸烤盘"里的"蒸烤"会命中蒸箱)
    (["蒸烤盘", "烤盘", "蒸盘", "烤架", "蒸架", "油纸", "锡纸", "铝箔"], PART, None),
    # 复合设备: 蒸烤一体机/方太智能烹饪设备, 到底是蒸还是烤要看上下文
    (["蒸烤一体机", "蒸烤箱", "蒸烤箱"], AMBIGUOUS, None),
    (["方太智能烹饪设备", "智能烹饪设备", "方太烹饪设备", "烹饪机", "方太"], AMBIGUOUS, None),
    # 明确带模式名的别名 (文档 3.1.2 点名要处理这种)
    # 官方《设备参数清单参考》里烤箱的全部模式名都要能认出来, 否则会掉到
    # 兜底推断(而兜底看到"蒸烤盘"里的"蒸"就判蒸箱)。实测「茯苓饼干」
    # 「将蒸烤盘置于机器中间层，设置全开烤150℃，30分钟」因为漏了"全开烤"
    # 被判成蒸箱 150℃(越界, 又被钳到 120℃) —— 一句话两个错。
    (["全开烤"], DEVICE_OVEN, "全开烤"),
    (["顶部烤"], DEVICE_OVEN, "顶部烤"),
    (["蔬果干"], DEVICE_OVEN, "蔬果干"),
    (["上下火"], DEVICE_OVEN, "上下火"),
    (["常规烘焙"], DEVICE_OVEN, "上下火"),
    (["热风烤", "热风"], DEVICE_OVEN, "鼓风烤"),
    (["空气炸"], DEVICE_OVEN, "空气炸"),
    (["加湿烤"], DEVICE_OVEN, "加湿烤"),
    (["环风烤"], DEVICE_OVEN, "环风烤"),
    (["脱脂烤"], DEVICE_OVEN, "脱脂烤"),
    (["脱脂蒸"], DEVICE_STEAMER, "脱脂蒸"),
    (["鲜嫩蒸"], DEVICE_STEAMER, "鲜嫩蒸"),
    (["多层蒸"], DEVICE_STEAMER, "多层蒸"),
    (["普通蒸"], DEVICE_STEAMER, "普通蒸"),
    # 常规设备
    (["电蒸箱", "蒸箱", "蒸笼", "蒸锅"], DEVICE_STEAMER, None),
    (["烤箱", "烤炉", "烘箱"], DEVICE_OVEN, None),
    (["洗碗机"], DEVICE_DISHWASHER, None),
    (["烟机", "油烟机", "排烟"], DEVICE_HOOD, None),
    # v3.1: 去掉裸「净水」—— 它会命中**食材**「纯净水」。官方 100 道菜里
    # 「纯净水」出现 4 次(轻松一锅蒸/沙参老鸭麦冬汤/烹香酷炒汇×2), 全部是配料,
    # 却被判成"净咖一体机在工作", 排程里凭空多出 4 条设备任务。
    (["净咖一体机", "净咖机", "净水机", "净水器", "清水机"], DEVICE_WATER, None),
    (["冷藏", "冷冻", "冰箱"], DEVICE_FRIDGE, None),
    (["主锅", "炒锅", "汤锅", "平底锅", "铁锅", "锅"], DEVICE_STOVE, None),
    (["灶具", "燃气灶", "灶台", "灶"], DEVICE_STOVE, None),
]

# 复合设备的上下文消歧信号 (按先后判定)
_AMB_STEAM = re.compile(r"水箱|加水|注水|加满水|普通蒸|蒸制|清蒸|蒸煮|蒸箱|蒸盘|蒸架")
_AMB_OVEN = re.compile(r"烤盘|油纸|锡纸|铝箔|烘烤|烤制|上下火|烤架|第\s*\d+\s*层")


# ---------- 配件词剥离 (v3.1) ----------
# 配件名里的"蒸/烤"是**器具名的一部分**, 不代表这句话在烹饪。兜底推断
# (_infer_from)见到裸"蒸/烤"就判设备, 于是这些纯备料句全被推成 15~25 分钟的
# 设备作业, 白白占着容量为 1 的蒸箱/烤箱:
#   「把调好味的带鱼块码入蒸盘中」                      -> 蒸箱 15 分钟
#   「蒸烤盘上铺好锡纸，把拍碎的香茅草平铺在盘底」        -> 蒸箱 15 分钟
#   「将派皮放在铺有锡纸的蒸烤盘上，周围刷上蛋液」        -> 蒸箱 15 分钟
#   「烤盘内铺上一层锡纸，在锡纸上刷油，放入鸡翅」        -> 烤箱 25 分钟
# 官方 100 道菜实测 19 条中招。这里把配件词摘掉后再判断"这句话像不像在做饭"。
# v3.2 补 4 项: 烤碗/烤串/烤网/蒸碟 —— 同样是"器皿名里带蒸/烤", 漏了会出事:
#   「烤碗底部抹上一层椰子油，将一半糊状物倒入烤碗中，将其铺平」(椰香苹果香蕉烤燕麦)
#     -> 兜底推成"烤箱 25 分钟", 实际是往烤碗里倒糊的备料动作
#   「在烤盘中铺上锡纸，将烤串放在烤架上，刷上适量食用油」(香烤鲫鱼#3)
#     -> 烤盘/烤架/锡纸都摘了, 只剩"烤串"里的"烤" => 又是烤箱 25 分钟
#   「在蒸碟上放上油豆腐及排骨」(虾米豆豉蒸排骨#4) -> 蒸箱 15 分钟
PART_WORDS = ("蒸烤盘", "蒸烤架", "蒸盘", "烤盘", "烤架", "蒸架", "油纸", "锡纸", "铝箔",
              "烤碗", "烤串", "烤网", "蒸碟")
_PART_RE = re.compile("|".join(PART_WORDS))


def strip_parts(text: str) -> str:
    return _PART_RE.sub("", text or "")


def resolve_ambiguous(text: str) -> Optional[str]:
    """蒸烤一体机/方太智能烹饪设备 到底是当蒸箱还是烤箱用。

    官方 100 道菜里这类句子有 26 条, 全部是"放入设备第N层, 开启电源, 取出水箱加满水,
    选择开始烹饪"这种占位句 —— 设备名本身不带蒸/烤信号, 只能看同句的上下文:
      · 提到 水箱/加水/蒸制    -> 蒸箱
      · 提到 烤盘/油纸/锡纸/烘烤 -> 烤箱
    都提到时蒸优先(水箱是蒸的独有信号), 都没有时返回 None 交给兜底推断。
    """
    if _AMB_STEAM.search(text):
        return DEVICE_STEAMER
    if _AMB_OVEN.search(text):
        return DEVICE_OVEN
    return None


def find_device(text: str) -> tuple[str, str, Optional[str]]:
    """返回 (标准设备, 命中的原文词, 顺带确定的标准模式或 None)。

    与旧 find_device 的差别: 配件词被跳过(不再把"蒸烤盘"当蒸箱),
    复合设备按上下文消歧, 模式名型别名("常规烘焙")一次给出设备+模式。
    """
    if not text:
        return DEVICE_NONE, "", None
    amb = None
    for words, dev, mode in DEVICE_ALIAS_RULES:
        for w in words:
            if w not in text:
                continue
            if dev == PART:
                continue                      # 配件: 忽略, 继续找真设备
            if dev == AMBIGUOUS:
                # 复合设备命中即**优先于**后面的常规设备词。
                # 反例: "将冷冻榴莲酥…放入蒸烤一体机第2层" 里的"冷冻"是**食材名**
                # 的一部分, 若让后面的"冷藏/冷冻->冰箱"先返回, 整道烘焙菜会被
                # 判成冰箱。所以这里先解析, 解析得出就立刻返回。
                real = resolve_ambiguous(text)
                if real:
                    return real, w, mode
                if amb is None:
                    amb = (w, mode)
                continue
            return dev, w, mode
    # 复合设备消歧失败(句子里既没有蒸信号也没有烤信号): 交给兜底推断,
    # 但**不能**让更低优先级的"冷藏/冷冻"这类状态词抢走 —— 只在没有歧义候选时
    # 才允许常规词生效, 而常规词在这一趟里已经先返回过了, 故此处直接报无设备。
    if amb is not None:
        return DEVICE_NONE, amb[0], amb[1]
    return DEVICE_NONE, "", None


# ---------- 2.2 / 3.1.3 设备能力模型 ----------
# 模式 -> (温度下限, 温度上限, 时长下限, 时长上限); 温度/时长为 None 表示该维度不限。
# 完全照抄官方《设备参数清单参考》, 不自行发挥。
DEVICE_SPEC = {
    DEVICE_STEAMER: {
        "default_mode": "普通蒸",
        "default_temp": 100.0,
        "modes": {
            "普通蒸": (91.0, 100.0, 1.0, 300.0),
            "鲜嫩蒸": (30.0, 90.0, 1.0, 300.0),
            "脱脂蒸": (110.0, 120.0, 1.0, 300.0),
            "多层蒸": (85.0, 100.0, 1.0, 300.0),
        },
    },
    DEVICE_OVEN: {
        "default_mode": "上下火",
        "default_temp": 180.0,
        "modes": {
            "上下火": (60.0, 230.0, 1.0, 300.0),
            "全开烤": (60.0, 230.0, 1.0, 300.0),
            "鼓风烤": (60.0, 230.0, 1.0, 300.0),
            "顶部烤": (60.0, 230.0, 1.0, 300.0),
            "加湿烤": (60.0, 230.0, 1.0, 300.0),
            "环风烤": (60.0, 230.0, 1.0, 300.0),
            "脱脂烤": (100.0, 230.0, 1.0, 300.0),
            "空气炸": (180.0, 230.0, 1.0, 300.0),
            "蔬果干": (60.0, 120.0, 1.0, 720.0),
        },
    },
    DEVICE_FRIDGE: {
        "default_mode": "冷藏",
        "default_temp": 4.0,
        "modes": {
            "冷藏": (0.0, 10.0, 1.0, 1440.0),
            "冷冻": (-24.0, -15.0, 1.0, 1440.0),
            "解冻": (0.0, 10.0, 1.0, 1440.0),
        },
    },
    DEVICE_STOVE: {
        # 灶具官方只给了"火力档位 1~7", 模式名用烹饪动作兜底(规范允许"中小火"这类文本)
        "default_mode": "爆炒",
        "default_temp": None,
        "modes": {
            "爆炒": (None, None, 1.0, 120.0),
            "煎制": (None, None, 1.0, 120.0),
            "油炸": (None, None, 1.0, 120.0),
            "炖煮": (None, None, 1.0, 300.0),
            "焖烧": (None, None, 1.0, 300.0),
            "熬煮": (None, None, 1.0, 300.0),
            "焯水": (None, None, 1.0, 60.0),
            "大火": (None, None, 1.0, 300.0),
            "中火": (None, None, 1.0, 300.0),
            "小火": (None, None, 1.0, 300.0),
        },
    },
    DEVICE_DISHWASHER: {
        "default_mode": "日常洗", "default_temp": None,
        "modes": {"日常洗": (None, None, 1.0, 300.0),
                  "母婴洗": (None, None, 1.0, 300.0),
                  "超净洗": (None, None, 1.0, 300.0)},
    },
    DEVICE_WATER: {
        "default_mode": "即热出水", "default_temp": None,
        "modes": {"即热出水": (None, None, 1.0, 60.0)},
    },
}

# 烟机档位 (文档 4.2): 不进 DAG, 只按灶具任务类型派生
HOOD_LEVELS = ("弱挡", "强档")
_HOOD_STRONG = re.compile(r"爆炒|煎炸|油炸|煎|炸|翻炒|煸炒|煸|爆")
_HOOD_WEAK = re.compile(r"炖|煮|煲|焖|煨|卤|焯|熬|汤")


def hood_level(text: str, mode: str = "") -> Optional[str]:
    """文档 4.2: 爆炒/煎炸 -> 强档; 炖煮/煲汤 -> 弱档。都不是返回 None。"""
    blob = f"{text or ''} {mode or ''}"
    if _HOOD_STRONG.search(blob):
        return "强档"
    if _HOOD_WEAK.search(blob):
        return "弱挡"
    return None


# ---------- 灶具火力 (文档 2.2: 官方只给"火力档位 1~7", 没有温度参数) ----------
# 菜谱里的"油温上升至180℃"是**油温**, 不是设备设定值; 直接拿它当
# cookingParameters.temperature 输出, 等于给灶具编了一个不存在的温度档。
# 官方规范 4.5.2 明确"无法数字化时可使用'中小火'", 所以这里归一到文本火力档。
_HEAT_BY_MODE = {"爆炒": "大火", "煎制": "大火", "油炸": "大火", "焯水": "大火",
                 "炖煮": "小火", "焖烧": "小火", "熬煮": "小火", "慢煮": "小火"}
_HEAT_STRONG = re.compile(r"大火|旺火|猛火|七成热|八成热|九成热|煮沸|烧开|滚水")
_HEAT_WEAK = re.compile(r"小火|微火|文火|慢炖|焖|煨|保温|三四成热|五成热|六成热")


def stove_heat(text: str, mode: str = "") -> str:
    """把灶具步骤归一到 大火/中火/小火 (规范 4.5.2 允许文本火力)。"""
    if mode in _HEAT_BY_MODE:
        return _HEAT_BY_MODE[mode]
    blob = f"{text or ''} {mode or ''}"
    if _HEAT_STRONG.search(blob):
        return "大火"
    if _HEAT_WEAK.search(blob):
        return "小火"
    return "中火"


def default_mode(device: str) -> str:
    """该设备的默认合法模式。适配器在模式为空时用它, 不能再回落到 "标准模式"
    —— 那个值不在官方枚举里, 输出它等于又造一个非法参数。"""
    return DEVICE_SPEC.get(device, {}).get("default_mode", "")


def default_temp(device: str) -> Optional[float]:
    return DEVICE_SPEC.get(device, {}).get("default_temp")


def legal_modes(device: str) -> list[str]:
    return list(DEVICE_SPEC.get(device, {}).get("modes", {}))


def _clamp(v, lo, hi):
    if v is None or lo is None or hi is None:
        return v
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def fit_mode(device: str, mode: str, temp: Optional[float]) -> str:
    """给设备挑一个**合法**模式。

    · 模式本身合法 -> 直接用;
    · 模式非法/为空 -> 用温度反查: 选温度落在区间内的模式, 都不落就用默认模式。
    """
    spec = DEVICE_SPEC.get(device)
    if not spec:
        return mode or ""
    modes = spec["modes"]
    if mode in modes:
        return mode
    if temp is not None:
        for m, (tlo, thi, _dlo, _dhi) in modes.items():
            if tlo is not None and tlo <= temp <= thi:
                return m
    return spec["default_mode"]


def clamp_params(device: str, mode: str, temp: Optional[float],
                 duration: Optional[float]) -> tuple[str, Optional[float], Optional[float]]:
    """文档 3.1.3: 参数合法性校验, 超出设备范围自动修正为合法边界值。

    返回 (合法模式, 钳制后的温度, 钳制后的时长)。设备无能力模型的原样返回。
    """
    spec = DEVICE_SPEC.get(device)
    if not spec:
        return mode, temp, duration
    modes = spec["modes"]
    m = fit_mode(device, mode, temp)
    rng = modes.get(m)
    if rng:
        tlo, thi, dlo, dhi = rng
        temp = _clamp(temp, tlo, thi)
        duration = _clamp(duration, dlo, dhi)
    return m, temp, duration


def is_legal(device: str, mode: str, temp: Optional[float],
             duration: Optional[float]) -> bool:
    """参数是否全部落在合法范围 (供自检/测试用)。"""
    spec = DEVICE_SPEC.get(device)
    if not spec:
        return True
    modes = spec["modes"]
    if mode not in modes:
        return False
    tlo, thi, dlo, dhi = modes[mode]
    if temp is not None and tlo is not None and not (tlo <= temp <= thi):
        return False
    if duration is not None and dlo is not None and not (dlo <= duration <= dhi):
        return False
    return True
