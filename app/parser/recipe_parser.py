"""
L1 菜谱结构化解析器 (规则版)

从自然语言烹饪步骤中抽取:
  设备 / 温度 / 时长 / 工序类型 / 是否人工介入 / 是否无人值守

设计原则: 可解释、零依赖、快。模糊句交给上层 LLM 兜底 (见 llm_fallback)。
"""
from __future__ import annotations

import os
import re
from typing import Optional

from app.models.schema import (
    RecipeStep, Recipe,
    DEVICE_HOOD, DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN,
    DEVICE_FRIDGE, DEVICE_DISHWASHER, DEVICE_WATER, DEVICE_NONE,
    PREP, MARINADE, COOK, WAIT, PLATE,
)
from app.spec import device_spec as _ds

# ---------- 设备关键词映射 (按优先级, 长词在前) ----------
# (匹配词, 归一设备, 是否加热类)
#
# v3.0: 这张表**已迁移到 app/spec/device_spec.py**, 那里还带"配件/复合设备"的
# 消歧逻辑。这里保留同名常量只为向后兼容(早期脚本直接 import 它), 解析实际
# 走 device_spec.find_device。
DEVICE_RULES = _ds.DEVICE_ALIAS_RULES

# 无人值守 (加热中不需人工盯着)
UNATTENDED_VERBS = [
    "炖", "焖", "熬", "慢煮", "慢炖", "煨", "蒸", "烤", "烘干",
    "冷藏", "冷冻", "静置", "发酵", "腌制", "醒发", "保温", "收汁",
]

# ---------- 装载/启动型动作 ----------
# 官方菜谱大量使用这种占位句:
#   "放入方太智能烹饪设备第2层，开启电源，取出水箱加满水，选择'开始烹饪'按键，按屏幕提示操作"
# 人工只做"装载 + 按键"(约 1 分钟), 之后是设备按程序长时间运行。
# 旧逻辑因为这类句子**不含**"蒸/烤/炖/焖"等烹饪动词, 把 unattended 判成 False,
# 于是整段 15~25 分钟被记成"人工占用":
#   - 双手被堵死, 炖煮期间根本无法并行切下一道菜的料 (README 宣称的并行能力失效)
#   - 赛题"用户连续手动操作时长"这一项被凭空抬高 (官方 100 道菜里有 57 步是这种句子,
#     累计锁死 618 分钟人工时间)
# 这里识别"设备程序启动"信号, 把它归为无人值守的设备占用。
LOAD_START_VERBS = re.compile(
    r"开启电源|打开电源|接通电源|开始烹饪|屏幕提示|点击.{0,4}开始"
)

# 持续动手守卫: 装载句里若夹着"炒/翻拌/煎炸"这类必须守在灶前的动作,
# 说明这一步人真的要全程操作, 不能整段判为无人值守。
# (实例: "烹香酷炒汇"的套餐步 —— 启动后还要"预热结束后放入…取出菜品…翻拌均匀")
HANDS_ON_VERBS = re.compile(r"炒|煸|煎|炸|翻拌|翻动|搅拌|揉|擀")

# 可"程序化运行"的加热设备: 一旦开始烹饪, 人不必守着
HEATING_DEVICES = (DEVICE_STOVE, DEVICE_STEAMER, DEVICE_OVEN)

# ---------- 冰箱"取出" vs "放入" (v3.1) ----------
# 从冰箱/冷冻室**取出**东西再加工, 是手上活, 不是冰箱在工作;
# 只有"放进冰箱"才是真的占用冰箱。
#   "面团从冰箱中取出，案板上撒少许面粉，将面团擀开成直径约为25cm的圆形"  -> 取出
#   "取出冰箱的面团擀成长23厘米"                                        -> 取出
#   "取出冷冻的印度飞饼皮"                                              -> 取出(冷冻是食材定语)
#   "将面团放入冰箱冷藏1小时" / "放入冰箱冷藏室2小时"                    -> 放入(保持冰箱任务)
_FROM_FRIDGE = re.compile(
    r"(?:从|自)?(?:冰箱|冷藏室|冷冻室|冷藏|冷冻)[^。；;，,]{0,8}?(?:取出|拿出|端出)"
    r"|(?:取出|拿出|端出)[^。；;，,]{0,10}?(?:冰箱|冷藏室|冷冻室|冷冻的|冷藏的)")
_INTO_FRIDGE = re.compile(
    r"(?:放入|放进|放回|置于|移入|存入|进|入)(?:冰箱|冷藏室|冷冻室)"
    r"|(?:冰箱|冷藏室|冷冻室)[^。；;，,]{0,6}?(?:冷藏|冷冻|静置|腌制|醒发|发酵)")


def is_fridge_retrieval(text: str) -> bool:
    """这句话是不是"从冰箱/冷冻室取出某物"。

    解析层用它清掉误挂的冰箱设备, 输出层用它把这类句子判成"食材处理"而不是"装盘"
    (FINISH_RE 里的"取出"会误命中) —— 两处必须同口径, 所以做成公共判据。
    """
    if not text:
        return False
    return bool(_FROM_FRIDGE.search(text)) and not _INTO_FRIDGE.search(text)

# ---------- 收尾/上桌句 ----------
# "再浇上蒸鱼豉油即可" 含"蒸"字(来自调料名"蒸鱼豉油"), 会被兜底推断误判成
# "蒸箱 100℃ × 18 分钟" —— 一个 30 秒的手上动作变成独占蒸箱的长任务。
# v2.2 扩充: 补上"取出/开箱门/尽情享用/就做好了/装入…即可"这类上桌句。
# 实测官方 100 道菜里有 22 条这种句子被推成 >=8 分钟的设备任务(虚占 549 分钟),
# 其中 15 条正好是**菜品末步** —— 末步时长就是出锅差的下界,
# 两道菜共用一台蒸箱/烤箱时, 出锅差直接被顶到 18~25 分钟。
FINISH_RE = re.compile(r"浇上|淋上|淋入|撒上|撒入|点缀|装盘|摆盘|盛出|盛入|"
                       r"盛盘|出锅装|上桌|蘸食|拌匀即可|即可出锅|"
                       r"取出|开箱门|打开箱门|打开烤箱门|打开蒸箱门|"
                       r"享用|品尝|就做好了|即可食用|即可享用|"
                       r"装入|摆入|端上|趁热食用|脱模即可")

# 设备"正在工作"的动作词 —— 用来把"纯收尾句"和"真的在做饭的句子"分开。
# 关键: 不能收裸"烤/蒸", 否则"将**烤**好的鸡翅包饭取出即可"会被当成烹饪动作。
# 收尾句的判据是: 命中 FINISH_RE 但**没有任何主动加热动作**。
ACTIVE_COOK = re.compile(
    r"加热|烧热|烧至|烧开|煮沸|煮开|热油|油温|预热|启动|开始烹饪|"
    r"翻炒|爆炒|煸炒|煎至|煎成|炸至|炸成|烤至|继续烤|再烤|烘烤|烤制|"
    r"蒸制|蒸煮|清蒸|继续蒸|再蒸|"
    r"炖|焖|煸|焯|焗|煲|炒|煎|炸|煮"
)

# ---------- 纯装载/取放句 (v3.3) ----------
# "将烤盘放入烤箱中" / "放入烤箱中" / "将装有饼坯的烤盘放入烤箱，关闭好"
# 这类句子的动作是**取放**, 设备并没有开始工作 —— 真正的工作在相邻的程序句里
# ("选择'常规烘焙'模式，以170℃烤13分钟")。
#
# 旧实现把它认成烤箱烹饪步: 因为没写时长, estimate_duration 补成 8 分钟, 于是
#   · **凭空多占烤箱 8 分钟** —— 烤箱容量 1, 是最稀缺的资源;
#   · 人工也被占一次(装载确实要动手, 这一半是对的);
#   · 「黄油小饼干」#9/#10 + #11 三句其实只是**一次 13 分钟烘烤** ⇒ 虚增 10 分钟烤箱占用。
# 实测官方 100 道菜 19 条中招(其中 15 条同菜同设备另有真时长步), 且 100% 集中在
# 装载句上 —— 全是"设备被占住但没在干活"。
#
# 判据(保守, 五条同时成立才摘设备):
#   ① 命中取放动词            ② 有加热设备(灶具/蒸箱/烤箱)
#   ③ **没有**程序启动信号    —— "开启电源/选择开始烹饪/按屏幕提示" 是设备任务
#   ④ **没有**主动加热动作    —— "将烤盘放入烤箱烤制" 是真烹饪(见 ACTIVE_COOK)
#   ⑤ 没写显式时长            —— 写了时长就是一次真实作业("放入烤箱烤15分钟")
# ⚠ ③④ 是必须的: 官方大量句子是"放入方太智能烹饪设备第2层, 开启电源, 选择开始烹饪,
#   按屏幕提示操作" —— 它同样是"放入", 但设备**真的要跑起来**(只是原文没给时长),
#   摘掉它会让这些菜整个丢掉主烹饪步。判③④把它救回来。
# ⚠ "放入预热好的烤箱中" 里的"预热"是**完成态定语**(同 DONE_REF 的原则), 不是本步动作,
#   判④之前必须先摘掉, 否则 ACTIVE_COOK 会命中"预热"而误放行(实测 2 条)。
_LOAD_ONLY = re.compile(r"放入|放进|推入|装入|摆入|置于")
_PREHEAT_DONE = re.compile(r"预热[好过的了后]{0,3}")

# 完成态引用: "炒好的X" / "烤好的X" / "烤制完成" —— 指的是**前序步骤的产物**,
# 不是本步的动作。这类句子是装配/摆盘, 不是设备在做饭。
# 反例(必须不命中): "烹饪结束后取出，将炒锅加热，倒入辣椒油烧至七成热，淋在鱼身上"
#   —— 含"取出", 但这是正经的灶具操作; 它不含"X好的", 所以不会被误降级。
DONE_REF = re.compile(r"(?:烤|蒸|炒|煎|炸|煮|炖|焖|烧|卤|焯)好(?:的|了的)?|"
                      r"(?:烤制|蒸制|烘烤|煎制|炒制|炖制|烹饪)\s*完成")

# ---------- 完成态引用的**扩展**形式 (v3.2) ----------
# DONE_REF 只认"X好(的)"。但菜谱里同一件事还有别的写法:
#   "蒸熟后剥去外皮"  "将蒸熟的血糯米"  "把煮沸的椰浆"  "煮软烂的栗子"  "炸过的金蒜"
# 这些同样是**引用前序步骤的产物**, 不是本步在做饭。实测官方 100 道菜里,
# 「芋头蛋卷#6」「血糯米仙草奶茶#9」等因此被凭空判成 15 分钟蒸箱任务。
#
# ⚠ 为什么只收 好/熟/透/沸/软/烂/过 这几个**状态词**, 且必须带 的/了/后:
#   真烹饪句里动词后面跟的是**火候副词**, 不是状态词 ——
#     "烧开后放入面条" / "煮开后下入" / "炒至微黄后加入" / "煎至两面金黄" / "煮20分钟"
#   这些一个都不能摘（摘了整道菜会丢掉真实的灶具/蒸箱作业）。所以:
#     · 不收 "开/至/成/到/干/香"  ⇒ "烧开后""炒至微黄后""炒香后的""水分炒干" 全部放行
#     · 必须紧跟 的/了/后        ⇒ "煮20分钟""烤35分钟" 不含这三个字, 天然放行
_DONE_STATE_RE = re.compile(
    r"(?:烤|蒸|炒|煎|炸|煮|炖|焖|烧|卤|焯|煸|焗|煲)"
    r"(?:好|熟|透|沸|软|烂|过)[^，。；、\s]{0,2}?(?:的|了|后)"
)
# ⚠ 实测记录（别再走回头路）: 试过给 need_fb 再补一条"设备装载信号"(_LOAD_HINT:
#   水箱|加水|第N层|中间层) + 把"香"也收进状态词表, 结果是**净负收益** ——
#   加热步数从 210 顶回 213, 新增的 6 条全是错的:
#     「豆沙松糕#3 边**加水**边翻拌成半湿的状态」-> 蒸箱 15 分钟（加水 ≠ 水箱注水）
#     「美式薯条#1 将薯条铺在蒸烤架上，放入机器中间层」-> 蒸箱 15 分钟（其实是烤）
#     「柠檬干#2 将柠檬片平铺在蒸烤架上，放入机器中间层」-> 蒸箱 15 分钟（其实是烘干）
#     「广式月饼（台式）#6 将蒸烤盘放入机器中间层」-> 蒸箱 15 分钟（其实是烤）
#   唯一收益是「麻辣对虾#3」的蒸箱 —— 而那条**不加 _LOAD_HINT 也能拿到**
#   （"炒香后的"里"炒"放行 ⇒ 仍走兜底推断 ⇒ 由"有孔蒸盘"的"蒸"判蒸箱）。
#   所以正确做法是：**只摘完成态引用，不要另外放宽 need_fb 的触发面**。

# ---------- 调料名里的"烹饪动词" (v3.2) ----------
# "蒸鱼豉油"是**调料**, 与"蒸"这个动作无关。官方 100 道菜里它出现 12 次,
# 典型中招: 「金银蒜蒸鸡胸肉#3」「加入蒸鱼豉油、黑胡椒粉、淀粉，抓匀，腌制1小时」
#   —— 本是**腌肉**的纯人工步, 却因"蒸"字被兜底推成"蒸箱 60 分钟"。
#   (FINISH_RE 那条守卫只覆盖"再浇上蒸鱼豉油即可"这种收尾句, 覆盖不到腌肉句。)
# 只收实测出现过的调料名; 表里没有的宁可不摘, 也不要用宽正则误伤真烹饪句。
_SEASONING_RE = re.compile(r"蒸鱼豉油|蒸鱼酱油|蒸鱼油|蒸肉粉")

# ---------- 步骤开头的**子菜名标签** (v3.2) ----------
# 套餐类菜谱(轻松一锅蒸/烹香酷炒汇/亲朋欢聚套餐)的每一步都以**子菜名**开头:
#   「腊味合蒸：处理食材…」  「黄金炒饭制作：1根火腿肠和40g胡萝卜切丁」
#   「白菜炖豆腐制作：50g五花肉切片，加入3g生抽腌制5分钟」
# 子菜名里带"蒸/炖/炒", 于是纯切配句被兜底推成设备任务:
#   轻松一锅蒸#4 -> 蒸箱 15 分钟 · 烹香酷炒汇#1 -> 灶具 5 分钟 · 烹香酷炒汇#10 -> 灶具 45 分钟
# 官方 100 道菜实测 16 条带这种标签, 其中 3 条中招。
# 只摘**行首**的"X："（2~12 字、不含标点），不动句子中间的冒号。
_LABEL_RE = re.compile(r"^[^，。；、\s：]{2,12}[：:]")

# 必须人工动手的动作 (手动操作)
HUMAN_VERBS = [
    "切", "洗", "剥", "剁", "斩", "切丝", "切片", "切块", "切末", "拍",
    "搅拌", "拌匀", "抓匀", "揉", "揉搓", "涂抹", "刮", "过滤", "滤",
    "取出", "放入", "摆入", "装盘", "摆盘", "点缀", "翻面", "翻炒",
    "煸炒", "炒", "煎", "炸", "焯", "捞出", "沥干", "腌制(?!.*分钟)",
    "开", "关", "选择", "按", "端", "倒", "淋", "撒", "搓", "卷", "包",
]

# 纯准备类 (无加热, 人工)
PREP_VERBS = ["切", "洗", "剥", "剁", "斩", "切丝", "切片", "切块", "切末",
              "拍", "搅拌", "拌匀", "抓匀", "揉", "涂抹", "刮", "过滤", "滤",
              "取出", "放入", "摆入", "装盘", "摆盘", "点缀", "捞出", "沥干"]


def find_device(text: str) -> tuple[str, str]:
    """返回 (归一设备, 命中的原文词)。无设备命中返回 (人工, '')

    v3.0: 委托 device_spec.find_device —— 它多了两件事:
      · 配件(蒸烤盘/烤盘/油纸)不再被当成设备。旧表用 "蒸烤" 匹配蒸箱,
        于是"将派皮放在铺有锡纸的蒸烤盘上"里的"蒸烤"把整句判成蒸箱。
      · 复合设备(蒸烤一体机/方太智能烹饪设备)按上下文消歧成蒸箱或烤箱。
    """
    dev, word, _mode = _ds.find_device(text)
    return dev, word


_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 只在**紧跟时长单位**时才替换，避免把"一起/一半/十分美味"这类词改坏。
# ⚠ 中文数字这一支**只认「分钟」不认光杆「分」**：`十分美味`/`五分熟` 里的"分"
#   不是时长单位，放宽到 `分(?!钟)` 会把它们变成 10 分钟 / 5 分钟（实测踩到）。
#   光杆「分」的写法几乎只出现在阿拉伯数字里（"约2分"），那一支已有专门处理。
_CN_NUM_BEFORE_UNIT = re.compile(
    r"([一二两三四五六七八九十]{1,3})(?=\s*个半\s*小时|\s*个?小时|\s*分钟|\s*秒)")


def cn_number_to_arabic(text: str) -> str:
    """把「两小时 / 二十分钟 / 半个小时」里的中文数字换成阿拉伯数字。

    ⚠ 为什么必须有：官方菜谱里「冷藏**两**小时」「腌制**一**小时」这类写法很常见，
    而 `parse_duration` 原来只认 `\\d+` —— 实测「巴斯克蛋糕」第7步
    「放凉后放入冰箱冷藏两小时」解析出来 `duration_min=None`，
    于是这一步在排程里按兜底的 30 分钟走，**真实是 120 分钟**。
    这类漏解析不报错、只静默给出一个偏短的计划。

    只替换**后面紧跟时长单位**的中文数字，`一半`/`一起`/`十分` 这类不受影响。
    """
    def _rep(m):
        s = m.group(1)
        if "十" in s:
            a, _, b = s.partition("十")
            tens = _CN_DIGIT.get(a, 1) if a else 1
            ones = _CN_DIGIT.get(b, 0) if b else 0
            return str(tens * 10 + ones)
        return str(_CN_DIGIT[s])

    return _CN_NUM_BEFORE_UNIT.sub(_rep, text)


def parse_duration(text: str) -> Optional[float]:
    """统一换算为分钟。支持:

    基础: 5分钟 / 1小时 / 2小时30分 / 30秒 / 1.5小时
    补充(v2.3): 2-3分钟(取中值) / 3~5分钟 / 2至3分钟 / 半小时 / 1个半小时
               30min / 45mins / 2h / 1.5hr / 45sec / 约2分

    顺序有讲究: 必须先"小时+分"再"纯小时"; 先"区间"再"纯分钟";
    先"N个半小时"再"半小时"(否则"1个半小时"会被当成 30 分钟)。
    """
    text = cn_number_to_arabic(text)
    # 小时 + 分钟
    m = re.search(r"(\d+(?:\.\d+)?)\s*个?小时\s*(?:(\d+(?:\.\d+)?)\s*分)", text)
    if m:
        return float(m.group(1)) * 60 + float(m.group(2))
    # N 个半小时  (必须排在"半小时"之前)
    m = re.search(r"(\d+)\s*个半\s*小时", text)
    if m:
        return float(m.group(1)) * 60 + 30
    # 纯小时
    m = re.search(r"(\d+(?:\.\d+)?)\s*个?小时", text)
    if m:
        return float(m.group(1)) * 60
    # 半小时 / 半个小时
    if re.search(r"半\s*个?\s*小时", text):
        return 30.0
    # 区间: 2-3分钟 / 3~5分钟 / 2至3分钟 / 2到3分钟  → 取中值
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~～—至到]\s*(\d+(?:\.\d+)?)\s*分", text)
    if m:
        return round((float(m.group(1)) + float(m.group(2))) / 2, 1)
    # 纯分钟
    m = re.search(r"(\d+(?:\.\d+)?)\s*分钟", text)
    if m:
        return float(m.group(1))
    # 英文分钟
    m = re.search(r"(\d+(?:\.\d+)?)\s*mins?\b", text, re.I)
    if m:
        return float(m.group(1))
    # 英文小时
    m = re.search(r"(\d+(?:\.\d+)?)\s*hrs?\b|(\d+(?:\.\d+)?)\s*h\b", text, re.I)
    if m:
        return float(m.group(1) or m.group(2)) * 60
    # 秒 -> 分钟
    m = re.search(r"(\d+(?:\.\d+)?)\s*秒", text)
    if m:
        return round(float(m.group(1)) / 60.0, 2)
    m = re.search(r"(\d+(?:\.\d+)?)\s*secs?\b", text, re.I)
    if m:
        return round(float(m.group(1)) / 60.0, 2)
    # "约2分" / "3分左右" —— 裸"分"。排除"3分熟"(牛排熟度)、"3分钟"
    m = re.search(r"(\d+(?:\.\d+)?)\s*分(?!钟|熟|之|配)", text)
    if m:
        return float(m.group(1))
    return None


def parse_temperature(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*℃", text)
    if m:
        return float(m.group(1))
    # "100度" 且后面不是 "度电/度数"
    m = re.search(r"(\d{2,3})\s*度(?!电|数)", text)
    if m:
        return float(m.group(1))
    return None


def infer_mode(text: str) -> str:
    for kw, mode in [
        # v3.0: 官方菜谱里"常规烘焙"是烤箱的一个**模式名**(9 道菜在用),
        # 旧表既没把它映射成设备也没映射成模式, 于是这 9 道菜拿不到合法模式。
        ("常规烘焙", "上下火"), ("加湿烤", "加湿烤"), ("环风烤", "环风烤"),
        ("脱脂烤", "脱脂烤"), ("脱脂蒸", "脱脂蒸"), ("鲜嫩蒸", "鲜嫩蒸"),
        ("多层蒸", "多层蒸"), ("空气炸", "空气炸"),
        ("普通蒸", "普通蒸"), ("清蒸", "清蒸"),
        ("烘烤", "烘烤"), ("焙烤", "烘烤"), ("热风", "热风烤"),
        ("翻炒", "翻炒"), ("煸炒", "煸炒"), ("炒", "爆炒"),
        ("熬煮", "熬煮"), ("慢煮", "慢煮"), ("炖", "炖煮"),
        ("焖", "焖烧"), ("炸", "油炸"), ("煎", "煎制"),
        ("腌制", "腌制"), ("冷藏", "冷藏"),
    ]:
        if kw in text:
            return mode
    return ""


# 官方《设备参数清单参考》里每种设备的合法模式枚举。
# infer_mode() 给的是**烹饪手法**(清蒸/翻炒/煎制), 而设备只认自己的模式名,
# 直接输出会出现"蒸箱·腌制""烤箱·煎制"这种设备与模式对不上的组合。
# 这里按设备把手法归一到官方模式名 (对应接口规范 4.6.1 cookingParameters.mode)。
_MODE_BY_DEVICE = {
    DEVICE_STEAMER: {
        "普通蒸": "普通蒸", "清蒸": "普通蒸", "蒸制": "普通蒸", "多层蒸": "多层蒸",
        "鲜嫩蒸": "鲜嫩蒸", "脱脂蒸": "脱脂蒸",
        "腌制": "普通蒸", "慢煮": "普通蒸", "熬煮": "普通蒸", "炖煮": "普通蒸",
        "焖烧": "普通蒸", "保温": "普通蒸",
    },
    DEVICE_OVEN: {
        "上下火": "上下火", "烘烤": "上下火", "焙烤": "上下火", "煎制": "上下火",
        "常规烘焙": "上下火",
        "热风烤": "鼓风烤", "热风": "鼓风烤", "全开烤": "全开烤", "顶部烤": "顶部烤",
        "加湿烤": "加湿烤", "环风烤": "环风烤", "脱脂烤": "脱脂烤",
        "油炸": "空气炸", "烘干": "蔬果干", "空气炸": "空气炸", "蔬果干": "蔬果干",
    },
    DEVICE_FRIDGE: {"冷藏": "冷藏", "冷冻": "冷冻", "解冻": "解冻",
                    "腌制": "冷藏", "静置": "冷藏", "醒发": "冷藏", "发酵": "冷藏"},
    DEVICE_STOVE: {
        "爆炒": "爆炒", "翻炒": "爆炒", "煸炒": "爆炒", "炒": "爆炒",
        "煎制": "煎制", "煎": "煎制", "油炸": "油炸", "炸": "油炸",
        "炖煮": "炖煮", "焖烧": "焖烧", "熬煮": "熬煮", "慢煮": "熬煮",
        "焯水": "焯水", "焯": "焯水", "大火": "大火", "中火": "中火", "小火": "小火",
    },
}


def normalize_mode(device: str, mode: str) -> str:
    """把烹饪手法归一成该设备的官方模式名。

    v2.3: 对**有官方模式枚举的设备**(蒸箱/烤箱/冰箱/灶具), 认不出的手法一律返回 "",
    不再原样透传。理由: 透传会产出"蒸箱·爆炒""烤箱·煎制"这种设备与模式对不上的组合
    (违反接口规范 4.6.1)。而这些错配的根源是 infer_mode 从**菜名或前序产物**里
    误匹配到动词 —— 例: "烹香酷炒汇"的装载步文本里含套餐名"黄金炒饭",
    于是被判成"蒸箱·爆炒"; "蔬菜塔"的装配步含"炒好的蔬菜丁", 被判成"烤箱·爆炒"。
    返回 "" 后由适配器回落"标准模式", 比输出一个错的模式诚实。
    """
    if not mode:
        return ""
    table = _MODE_BY_DEVICE.get(device)
    if not table:
        return mode          # 无官方枚举的设备(人工/烟机/净水机/洗碗机)原样返回
    return table.get(mode, "")


# ---------- 3.1.4 预热任务拆分 ----------
# 旧实现给**所有** dur>=5 的烤箱/蒸箱任务都补 5 分钟预热 (官方 100 道菜实测 148 条),
# 而数据里只有 6 道菜提到"预热", 其中「豆沙松糕」还明确写着"无需预热" ——
# 那是**凭空造步骤**。所以原则不变: 只有该任务自己的原文写了"预热"、且未被否定, 才补预热。
#
# v3.0 变的是**预热怎么落地**: 旧实现在排程**之后**硬塞一条 [start-dur, start] 的
# 合成任务, 既不占设备槽也不参与资源竞争 —— 实测(30 组)产出的预热 100% 与别的菜
# 真实设备任务重叠, 属于会被官方冲突检测判出来的硬伤。
# 现在按文档 3.1.4 在 L1 就把它拆成**独立的 RecipeStep**, 插进该菜的步骤链,
# 于是三轨(贪心/CP-SAT/精确模型)都会给它分配真实设备槽, 且它无人值守
# (needs_human=False), 天然可以和人工切配并行 —— 对应文档 4.1.3。
_PREHEAT_NEG = re.compile(
    r"无需预热|不用预热|不必预热|免预热|不需要预热|无需提前预热|不用提前预热")
# "预热10分钟" / "预热至180℃后烤10分钟" —— 只认紧跟在"预热"后面的显式时长
_PREHEAT_MIN_RE = re.compile(r"预热[^。；;]{0,12}?(\d+(?:\.\d+)?)\s*分钟")
PREHEAT_DEFAULT_MIN = 5.0     # 原文提到预热但没写时长时的保守取值
PREHEAT_MIN_TASK_MIN = 5.0    # 比这还短的任务不值得单列一条预热
PREHEAT_MARK = "预热"         # device_source 标记: 供调度层识别"这是预热节点"


def preheat_minutes(text: str) -> Optional[float]:
    """该步骤原文是否要求预热。返回预热时长(分钟), 不需要/未提及返回 None。"""
    if not text or "预热" not in text:
        return None
    if _PREHEAT_NEG.search(text):
        return None
    m = _PREHEAT_MIN_RE.search(text)
    return float(m.group(1)) if m else PREHEAT_DEFAULT_MIN


def is_preheat_step(step) -> bool:
    """这个步骤是不是拆出来的预热节点。"""
    return getattr(step, "device_source", "") == PREHEAT_MARK


def insert_preheat_steps(recipe: Recipe) -> Recipe:
    """文档 3.1.4: 把烤箱/蒸箱的预热拆成独立任务节点, 插进该菜步骤链。

    规则 (数据驱动, 不凭空造步骤):
      · 只有该步骤原文写了"预热"且未被否定;
      · 正式烹饪步 >= PREHEAT_MIN_TASK_MIN (太短不值得预热);
      · 同一道菜在同一个设备上**只预热一次** (第一处出现的地方);
      · 预热节点排在正式烹饪步**紧前面**, 正式步改为依赖它;
      · 预热无人值守(needs_human=False), 因此可以和人工切配并行。

    插入后全链 step_id 重新编号为 1..M, 依赖同步重映射, 保证下游
    (贪心/CP-SAT/精确模型/输出适配) 拿到的仍是连续整数编号。
    """
    seq = []                 # [(kind, step_or_device, deps_spec)]
    done_dev: set = set()

    # v3.3: 预热信号的**宿主步可能已被摘掉设备** —— 纯装载句 "放入预热好的烤箱中"
    #   在 parse_recipe 里按 _LOAD_ONLY 回落成了人工备料(摘设备是为了不虚占烤箱 8 分钟),
    #   但"预热"是一次**真实的设备作业**, 不能跟着一起消失。
    #   这里从**原文**把设备取回来, 预热节点仍插在该步紧前面(物理顺序: 预热 → 装载 → 烘烤)。
    #   实测官方 100 道菜有 3 条(半熟芝士蛋糕#10 / 草莓蛋挞#10 / 蔬菜塔#3),
    #   不补的话预热节点 5 → 2 条, audit_doc 的 3.1.4 会由 ok 变缺陷。
    def _preheat_device(st):
        if st.device in (DEVICE_OVEN, DEVICE_STEAMER):
            return st.device
        if st.device == DEVICE_NONE:
            dev, _w = find_device(st.raw_text)
            if dev in (DEVICE_OVEN, DEVICE_STEAMER):
                return dev
        return None

    for st in recipe.steps:
        deps = [("old", d) for d in (st.depends_on or [])]
        ph_dev = _preheat_device(st)
        # 时长口径与调度层一致: 原文没写时长时, 蒸箱/烤箱按 estimate_duration 的
        # 8 分钟兜底 (engine.estimate_duration 对蒸烤设备的默认值), 否则
        # "将生坯放入烤盘中，再推入预热的烤箱中" 这种没写时长的真实烤箱步会漏掉预热。
        # ⚠ 用 ph_dev 而不是 st.device 判: 上面那类句子现在 st.device 已是"人工",
        #   按 st.device 判会拿到 0.0 而漏掉预热。
        dur = float(st.duration_min) if st.duration_min else (8.0 if ph_dev else 0.0)
        if (ph_dev and ph_dev not in done_dev
                and dur >= PREHEAT_MIN_TASK_MIN
                and preheat_minutes(st.raw_text)):
            pm = preheat_minutes(st.raw_text)
            done_dev.add(ph_dev)
            seq.append(("ph", ph_dev, list(deps), pm, st))
            deps = deps + [("ph", ph_dev)]
        seq.append(("step", st, deps, None, None))

    new_id_of_old, new_id_of_ph = {}, {}
    for i, item in enumerate(seq, start=1):
        if item[0] == "step":
            new_id_of_old[item[1].step_id] = i
        else:
            new_id_of_ph[item[1]] = i

    steps = []
    for i, (kind, payload, deps, pm, host) in enumerate(seq, start=1):
        if kind == "step":
            st = payload
            nd = [new_id_of_old[d] for k, d in deps if k == "old" and d in new_id_of_old]
            nd += [new_id_of_ph[d] for k, d in deps if k == "ph" and d in new_id_of_ph]
            steps.append(RecipeStep(
                step_id=i, raw_text=st.raw_text, device=st.device,
                device_source=st.device_source, mode=st.mode,
                temperature_c=st.temperature_c, duration_min=st.duration_min,
                needs_human=st.needs_human, unattended=st.unattended,
                prep_type=st.prep_type, depends_on=nd, notes=st.notes,
            ))
        else:
            dev = payload
            nd = [new_id_of_old[d] for k, d in deps if k == "old" and d in new_id_of_old]
            steps.append(RecipeStep(
                step_id=i, raw_text=f"{dev}预热", device=dev,
                device_source=PREHEAT_MARK,
                mode=_ds.default_mode(dev),
                temperature_c=(host.temperature_c if host.temperature_c is not None
                               else _ds.default_temp(dev)),
                duration_min=pm, needs_human=False, unattended=True,
                prep_type=COOK, depends_on=nd, notes="",
            ))
    return Recipe(name=recipe.name, ingredients=recipe.ingredients,
                  steps=steps, label=recipe.label)


# ---------- 兜底时长的**品类分档** (v3.10) ----------
# 原来蒸箱一律 15/18 分钟、烤箱一律 25 分钟 —— 一个设备一个常数。后果是
# 「香菇烤芦笋」(原文写了 190℃/8 分钟) 与「巴斯克蛋糕」(原文没写时长, 实际 30+ 分钟)
# 在排程里被估成同样长。
#
# ⚠ 数值刻意**贴着旧常数**取（蒸箱默认仍是 15、烤箱默认仍是 25），只建模
#   「谁比谁长」。理由是历史实测教训：烤箱的 25 分钟曾把容量为 1 的烤箱
#   变成瓶颈（出锅差<5min 占比 96% → 84%），绝对量级一旦放大就直接吃掉
#   多目标优化的 25 分。原文真写了时长的步在 `estimate_duration` 里被原样尊重，
#   根本走不到这张表。
#   开关 `FT_DUR_TIERS=off` 退回旧常数。
# ⚠ "鸡(?!精|粉)"：调味料「鸡精/鸡粉」不是肉，命中它会把素菜推到长档
#   （实测「蒜蓉粉丝蒸扇贝」因为食材里有"鸡精1克"被判成蒸肉档 18 分钟）。
_STEAM_LONG = re.compile(
    r"排骨|五花|猪|牛|羊|鸡(?!精|粉)|鸭|鹅|肉|蹄|肘|粽|糯米|南瓜|芋|山药|土豆|藕|海带|"
    r"馒头|包子|花卷|发糕|糕|栗")
_STEAM_SHORT = re.compile(
    r"蛋|羹|鱼|虾|蟹|贝|蛤|蛎|蔬|青菜|芦笋|西兰花|茄|豆腐|粉丝|秋葵|金针菇")
_OVEN_BAKE = re.compile(
    r"蛋糕|芝士|奶酪|派|挞|面包|吐司|欧包|披萨|酥|曲奇|饼干|玛格丽特|布朗尼|"
    r"玛芬|泡芙|司康|布丁|模具")
_OVEN_ROAST = re.compile(r"排骨|鸡|鸭|牛|羊|肉|鱼|虾|蟹|翅|腿|排|串|鱿鱼")
_OVEN_VEG = re.compile(r"蔬|芦笋|西兰花|茄|香菇|蘑菇|薯|土豆|南瓜|红薯|柠檬|干$")


def _tiered(text: str, tiers, default: float) -> float:
    if os.environ.get("FT_DUR_TIERS", "on").lower() in ("off", "0", "false", "no"):
        return default
    for pat, val in tiers:
        if pat.search(text or ""):
            return val
    return default


def steam_duration(ctx: str, default: float = 15.0) -> float:
    return _tiered(ctx, ((_STEAM_LONG, 18.0), (_STEAM_SHORT, 10.0)), default)


def oven_duration(ctx: str, default: float = 25.0) -> float:
    return _tiered(ctx, ((_OVEN_BAKE, 22.0), (_OVEN_ROAST, 20.0), (_OVEN_VEG, 10.0)), default)


# ---------- 兜底推断: 78道菜写"开始烹饪"占位, 无设备/温度/时长 ----------
def _infer_from(text: str, ctx: str = ""):
    """在给定文本上做一次设备推断。命中返回 (设备, 温度或None, 时长, 模式, True)。

    这里**不**摘配件词: "蒸烤盘/烤盘"含有的"蒸/烤"恰恰是判断这道菜用什么设备的
    有效信号(例: "打开设备…选择开始烹饪…将黄金炒饭置于蒸烤盘上放入机器第1层"
    正是靠"蒸"认出蒸箱)。"配件词该不该触发兜底推断"由调用方在 need_fb 里决定 ——
    见 parse_recipe 里 `_ds.strip_parts(stext)` 那一段。
    """
    if re.search(r"清蒸|豉汁蒸|粉蒸|蒸鱼|蒸虾|蒸蛋|蒸排|梅菜", text):
        return DEVICE_STEAMER, 100.0, steam_duration(ctx or text, 18.0), "普通蒸", True
    if "蒸" in text or "水箱" in text or "加水" in text:
        return DEVICE_STEAMER, 100.0, steam_duration(ctx or text), "普通蒸", True
    if re.search(r"烤|派|酥|饼|焗|焙|叉烧|烤鸭", text):
        return DEVICE_OVEN, 180.0, oven_duration(ctx or text), "上下火", True
    # "汤(?!匙|勺)": 量词"汤匙/汤勺"不是烹饪动作。
    #   实例: 麻辣对虾(官方库里有两条同名菜, 这是第二条)#4「选择"开始烹饪"按键，按屏幕
    #   提示操作」—— 步骤原文没有任何设备线索, 退到"菜名+食材"再匹配时, 食材里的
    #   「食用油3**汤**匙」命中了"汤", 于是纯占位句被判成"灶具 45 分钟炖煮"。
    #   保留"肉汤/鸡汤/骨汤"（真·高汤食材, 暗示炖煮）的命中。
    if re.search(r"炖|煲|汤(?!匙|勺)|焖|煨|卤", text):
        return DEVICE_STOVE, None, 45.0, "炖煮", True
    if re.search(r"焯|白灼|水煮|煮", text):
        return DEVICE_STOVE, None, 8.0, "熬煮", True
    if re.search(r"炸|煎", text):
        return DEVICE_STOVE, None, 10.0, "煎炸", True
    if re.search(r"炒|爆|煸", text):
        return DEVICE_STOVE, None, 5.0, "爆炒", True
    return None


# ---------- 复合设备(蒸烤一体机)消歧 (v3.1) ----------
# 官方 100 道菜里有 56 道提到"蒸烤盘/蒸烤架/方太智能烹饪设备"这类**复合设备**
# (蒸箱+烤箱二合一), 绝大多数同时写了"蒸箱"或"烤箱"把答案说清楚;
# 但 3 道菜整篇不写设备名, 只写"放入机器中间层 / 选择开始烹饪":
#   美式薯条 · 柠檬干 · 巴斯克蛋糕
# 单看那句占位句没有任何设备线索, 于是整道菜一个加热设备都没有 ——
# 排程里这道菜"不占任何设备", 甘特图上也就没有它的烹饪条。
# 这里按复合设备的**同菜上下文**消歧: 有蒸信号(水箱/加水/蒸制)判蒸箱, 否则判烤箱。
# 实测这 3 道菜都是烤(薯条/柠檬干/蛋糕), 判烤箱正确。
_COMBO_HINT = re.compile(
    r"蒸烤盘|蒸烤架|蒸烤箱|蒸烤一体机|方太智能烹饪设备|智能烹饪设备|"
    r"烹饪设备|烹饪机|机器中间层|放入机器|机器第\s*\d+\s*层")
_COMBO_STEAM = re.compile(r"水箱|加水|加满水|注水|蒸制|清蒸|普通蒸|鲜嫩蒸|蒸煮|蒸箱")


def combo_device_of(text: str, ctx: str = ""):
    """复合设备(蒸烤一体机)按上下文消歧。返回 (设备, 温度, 模式, 时长) 或 None。

    **时长走品类分档**(v3.10): 这类占位句("选择开始烹饪, 按屏幕提示操作")原文没有时长,
    原来一律交给 engine 的 8 分钟常数 —— 于是「香菇烤芦笋」和「巴斯克蛋糕」
    在排程里一样长。现在按**菜名+食材**(`ctx`)分档:
    烘焙类 22 / 烤肉类 20 / 蔬菜类 10 / 蒸肉 18 / 蒸鱼虾蛋 10。

    ⚠ 量级仍然贴着旧基线取, 不做"真实厨艺常识"那套。历史实测: 早期版本在这里
      硬编码 25 分钟, 烤箱(容量 1)凭空变成瓶颈, 出锅差<5min 占比 96% → 84%。
      分档后的 A/B (`scripts.bench 50`) 三项指标在噪声内一致。
      `FT_DUR_TIERS=off` 退回 8 分钟常数。
    """
    if not text or not _COMBO_HINT.search(text):
        return None
    if _COMBO_STEAM.search(text):
        return DEVICE_STEAMER, 100.0, "普通蒸", steam_duration(ctx or text)
    return DEVICE_OVEN, 180.0, "上下火", oven_duration(ctx or text, 8.0)


def infer_fallback(recipe_name: str, stext: str, ingredients: str):
    """返回 (设备, 温度或None, 时长分钟, 模式, 是否命中)

    v2.2: **先只看步骤文本(动词优先), 未命中再看"菜名+食材"**。
    旧实现把菜名和步骤拼成一个 blob 一起匹配, 菜名里的字会污染推断:
      "栗子冰皮月饼"含"饼" -> 该菜每一步(包括"倒入色拉油炒")都被推成烤箱 25 分钟,
      虚占烤箱且把"末步时长"顶成出锅差的下界。
    """
    ctx = f"{recipe_name} {ingredients}"
    for text in (stext, recipe_name + " " + ingredients):
        hit = _infer_from(text, ctx)
        if hit:
            return hit
    return DEVICE_NONE, None, None, "", False


def classify_step(text: str, device: str, has_duration: bool,
                  duration: Optional[float]) -> tuple[str, bool, bool]:
    """返回 (prep_type, needs_human, unattended)"""
    unattended = any(v in text for v in UNATTENDED_VERBS) and (
        has_duration or device in (DEVICE_FRIDGE,)
    )
    human = any(v in text for v in HUMAN_VERBS)

    # 腌制/冷藏等待: 有持续时间且无人值守
    if ("腌制" in text or "冷藏" in text or "静置" in text or "醒发" in text) and has_duration:
        return WAIT, False, True
    if device == DEVICE_FRIDGE:
        return WAIT, False, True
    # 收尾/上桌型: "撒上葱花即可食用" / "开箱门取出即可" —— 30~60 秒的短人工动作。
    # 必须排在"加热类"判断之前, 且必须排在下面的 PREP 之前, 才能拿到更准的时长估计
    # (estimate_duration 对 PLATE 给 1 分钟, 而不是切配的 2 分钟)。
    if device == DEVICE_NONE and not has_duration and FINISH_RE.search(text):
        return PLATE, True, False

    if device == DEVICE_NONE and not has_duration:
        # 纯人工准备
        return PREP, True, False

    # 装载/启动型: 人工只做装载与按键, 之后设备程序化运行 -> 不占双手
    # (必须放在"加热类"判断之前: 这类句子不含烹饪动词, unattended 判不出来)
    if (device in HEATING_DEVICES
            and LOAD_START_VERBS.search(text)
            and not HANDS_ON_VERBS.search(text)):
        return COOK, False, True

    # 加热类
    if unattended:
        return COOK, False, True   # 炖煮蒸烤, 无人值守
    return COOK, human or True, False


# 步骤分句: 按 "第N步" 或 "；" 拆
STEP_SPLIT = re.compile(r"第\s*\d+\s*步[：:。]?")

# 复合操作切分: 同一"第N步"内的多段烹饪, 如 "...加热；再设置...熬煮；然后..."
SUBSPLIT = re.compile(r"[;；]\s*(?:再|接着|然后|之后|随后)")


def split_steps(raw: str) -> list[str]:
    raw = raw.strip()
    parts = STEP_SPLIT.split(raw)
    steps = [p.strip(" ；;。\n") for p in parts if p and p.strip(" ；;。\n")]
    return steps


def expand_compound(stext: str) -> list[str]:
    """把复合操作拆成子段, 使每段的时长/温度可独立抽取并调度。"""
    segs = [s.strip(" ；;。\n") for s in SUBSPLIT.split(stext) if s.strip()]
    # 只有当拆分后多段都带时长/温度时才视为复合操作, 否则保留整句
    if len(segs) <= 1:
        return [stext]
    timed = [s for s in segs if parse_duration(s) is not None]
    if len(timed) >= 2:
        return segs
    return [stext]


def parse_recipe(name: str, ingredients_text: str, steps_raw: str,
                 label: str = "") -> Recipe:
    ingredients = [x.strip() for x in re.split(r"[；;]", ingredients_text) if x.strip()]
    # 先按"第N步"切, 再把每步里的复合操作拆成子段
    expanded = []
    for stext in split_steps(steps_raw):
        expanded.extend(expand_compound(stext))

    recipe_steps = []
    COOK_VERBS = ("蒸", "烤", "炖", "煮", "炒", "煎", "炸", "焯", "焗", "焖", "煲")
    PLACEHOLDER = ("方太智能烹饪设备", "开始烹饪", "烹饪结束", "按屏幕提示")
    # 浸泡/泡发/解冻/过夜/放凉: 是"等待"不是加热, 一旦被兜底推断成蒸箱,
    # 会把 4 小时的泡米变成 240 分钟的蒸制, 直接毁掉整条排程 (见糯米烧麦)
    SOAK = re.compile(r"浸泡|泡发|泡开|泡透|解冻|过夜|静置|醒面|醒发|晾凉|放凉|冷却|沥水")
    # v3.1 修复: 从冰箱/冷冻室"取出"某物后接着加工 —— 这一步是**手上活**, 不是冰箱在工作。
    #   旧实现只看到"冰箱/冷藏/冷冻"字样就把它当冷藏任务, 再被 estimate_duration
    #   补成 30 分钟设备占用。官方 100 道菜实测 4 条中招, 典型:
    #     「面团从冰箱中取出，案板上撒少许面粉，将面团擀开成直径约为25cm的圆形」
    #     → 排成 0-30 的冰箱任务: 甘特图上凭空多出一条半小时的冰箱条, 而且这道菜的
    #       "第一道工序"被误算成 30 分钟(实际是 1~2 分钟的擀面)。
    #   判据: 句中说的是"从冰箱取出"(而不是"放进冰箱"), 且没写显式时长。
    #   反例保护: "将面团放入冰箱冷藏1小时" 有 放入 信号 + 显式时长, 完全不受影响;
    #            "取出冷冻的印度飞饼皮" 里的"冷冻"是**食材定语**, 也要清掉。
    # (正则见模块级 _FROM_FRIDGE / _INTO_FRIDGE)
    # 收尾/调味句: "再浇上蒸鱼豉油即可" 含"蒸"字(来自"蒸鱼豉油"这个调料名),
    # 会被兜底推断误判成"蒸箱 100℃ × 18 分钟" —— 一个 1 分钟的手上动作
    # 变成独占蒸箱的长任务, 既虚增总时长又堵死设备。这类句子一律按人工收尾处理。
    # (正则见模块级 FINISH_RE, classify_step 也用它判 PLATE 类型)
    FINISH = FINISH_RE
    for i, stext in enumerate(expanded, start=1):
        device, dev_word, alias_mode = _ds.find_device(stext)
        dur = parse_duration(stext)
        temp = parse_temperature(stext)
        mode = infer_mode(stext) or (alias_mode or "")
        is_soak = bool(SOAK.search(stext))
        # v2.4 修复: "等待词"不能无差别地整句降级。
        #   SOAK 里的词(晾凉/冷却/解冻/浸泡…)既可能是**整步的动作**, 也可能是
        #   复合句里顺带交代的收尾/定语。旧实现一律降级成 WAIT, 会把整段真实设备
        #   作业一起抹掉 —— 实测官方 100 道菜里有 12 句被误杀, 其中 6 句是
        #   "…放入烤箱第3层, 选择"开始烹饪"按键…烘烤结束, 取出晾凉":
        #   一次真实的烤箱作业被整句判成等待, 该菜直接丢掉主烹饪步
        #   (极端例子: 法式羊排全菜只有 1 步, 被抹掉后整道菜没有任何设备任务)。
        #   判据(保守, 只放行信号明确的三类):
        #     ① 同句带"开启电源/选择开始烹饪/按屏幕提示" ⇒ 是设备程序启动, 不是等待;
        #     ② 同句**点名了加热设备**且**给了显式时长** ⇒ 是一次真实的设备作业
        #        (反例保护: "温水浸泡4小时"没有设备名, 不会被放行, 仍归等待);
        #     ③ 灶具 + 主动烹饪动作 ⇒ 灶前手上活, 末尾的"冷却/沥水"只是收尾
        #        (只对灶具放行: 烤箱/蒸箱的"烘烤结束…取出晾凉"是纯收尾, 必须保持等待)。
        if is_soak and (LOAD_START_VERBS.search(stext)
                        or (device in HEATING_DEVICES and dur is not None)
                        or (device == DEVICE_STOVE and ACTIVE_COOK.search(stext))):
            is_soak = False
        # v3.1: 冰箱"取出"型句子 → 清设备, 按人工备料处理 (见上方 _FROM_FRIDGE 说明)
        is_from_fridge = (device == DEVICE_FRIDGE and dur is None
                          and is_fridge_retrieval(stext))
        if is_from_fridge:
            device, dev_word, alias_mode = DEVICE_NONE, "", None
        # 兜底: 没识别到设备但句子像烹饪动作/占位句, 按菜名+食材推断
        # 收尾/调味句(且没写时长)不参与推断 —— 见 FINISH 的说明
        # 守卫: 带"开启电源/选择开始烹饪"这类**设备程序启动**信号的句子是正经设备任务,
        #       即使含"取出"也多半是"取出水箱加满水", 不能当收尾句处理。
        # 收尾/调味句(且没写时长)不参与推断 —— 见 FINISH 的说明
        # 守卫 1: 带"开启电源/选择开始烹饪"这类**设备程序启动**信号的句子是正经设备任务,
        #         即使含"取出"也多半是"取出水箱加满水", 不能当收尾句处理。
        # 守卫 2 (v2.3): 句子里若还有**主动加热动作**(加热/烧至/翻炒/煮沸…),
        #         说明人真的在灶前干活, 也不是纯收尾句。
        #         实例: "烹饪结束后取出，将炒锅加热，倒入辣椒油烧至七成热，淋在鱼身上"
        #         —— 含"取出", 但这是正经的灶具操作, 不能降级成装盘。
        # 守卫 3 (v2.3): 但"炒好的X / 烤好的X / 烤制完成"是**引用前序产物**的完成态,
        #         此时主动作词出现在定语里, 不该算本步在做饭。
        #         实例: "将炒好的蔬菜丁装入烤好的土豆塔中即可" —— 装配步, 不是烤箱在烤。
        is_finish = (dur is None
                     and not LOAD_START_VERBS.search(stext)
                     and bool(FINISH.search(stext))
                     and (bool(DONE_REF.search(stext))
                          or not ACTIVE_COOK.search(stext)))
        # v3.3: **纯装载/取放句** —— 设备被占住但没在干活, 摘掉设备按人工处理。
        #   判据见模块级 _LOAD_ONLY / _PREHEAT_DONE 的说明。
        #   ⚠ 必须在 need_fb **之前**算(用原始 device), 但**在 need_fb 之后**才清设备:
        #     先清的话 device 变 人工 ⇒ need_fb 触发 ⇒ 兜底推断又把设备推回来(判据自相矛盾)。
        is_load_only = (dur is None
                        and device in HEATING_DEVICES
                        and bool(_LOAD_ONLY.search(stext))
                        and not LOAD_START_VERBS.search(stext)
                        and not ACTIVE_COOK.search(_PREHEAT_DONE.sub("", stext)))
        # v2.3 修复: 纯收尾句即使带了设备关键词, 也要把设备清掉。
        #   旧实现里 "打开烤箱门，将烤好的鸡翅包饭取出即可" 会被 find_device 认出"烤箱",
        #   于是变成一个**没有时长的烤箱烹饪步**, 排程时被 estimate_duration 补成 8 分钟
        #   的设备占用。实测官方 100 道菜里有 17 条这种句子, 累计虚占设备 136 分钟;
        #   其中"末步"还会直接顶高出锅差(末步时长就是出锅差的下界)。
        if is_finish and device in HEATING_DEVICES:
            device, dev_word = DEVICE_NONE, ""
        # v3.1: "这句话像不像在做饭"的判据要先摘掉**配件词**再看。
        #   「将派皮放在铺有锡纸的蒸烤盘上，周围刷上蛋液，撒些粗糖」是纯备料,
        #   但"蒸烤盘"里的"蒸"会让它被判成"像烹饪" → 兜底推成 15 分钟蒸箱任务。
        #   摘掉配件词后它不含任何烹饪动词, 于是不再触发兜底, 老老实实当人工备料。
        #   注意 PLACEHOLDER(开始烹饪/按屏幕提示) 仍看**原文** —— 那些是设备程序
        #   启动信号, 与配件词无关, 摘了会把真正的设备步漏掉。
        # v3.2: 兜底推断前必须先摘掉**完成态引用**（"炖好的猪肉"/"蒸熟的糯米"/"煮沸的椰浆"）。
        #   这类短语里的烹饪动词指的是**前序步骤的产物**，不是本步在做饭 —— 这正是
        #   is_finish 那条守卫(DONE_REF)已经承认的原则；只是守卫 3 还要求 FINISH_RE 命中，
        #   而"装碗/淋汁/碾泥/沥干"这类说法不在 FINISH_RE 里，于是整条守卫没兜住。
        #   实测官方 100 道菜有 **7 条**中招，虚占设备 **104 分钟**，全是装配/装碗/沥干：
        #     · 小碗蒸肉#10「用筷子将**炖好的**猪肉，挨个放到刚刚的碗里，再均匀淋一些肉汁」
        #       → 判成"灶具 45 分钟炖煮"，占掉该菜 92 分钟 makespan 的一半
        #     · 芋头蛋卷#7「把**煎好的**蛋饼放案板上，均匀涂抹上**碾压好的**芋泥」→ 烤箱 25 分钟
        #     · 竹荪排骨汤#5「**焯好的**排骨沥干，置于大碗中」→ 灶具 8 分钟
        #   摘掉 DONE_REF / 完成态扩展 / 调料名之后这些句子不含任何烹饪动词，回落成人工备料。
        #   反例保护（不受影响）："将炖好的牛腩继续炖20分钟" —— 摘掉"炖好的"后还剩"炖"，
        #   仍然走兜底推断拿设备，且原文已有显式时长，时长不会被覆盖。
        _fb_probe = _DONE_STATE_RE.sub(
            "", _SEASONING_RE.sub("", _ds.strip_parts(_LABEL_RE.sub("", stext))))
        need_fb = (not is_soak) and (not is_finish) and device == DEVICE_NONE and (
            any(v in _fb_probe for v in COOK_VERBS)
            or any(p in stext for p in PLACEHOLDER)
        )
        if need_fb:
            fb_dev, fb_temp, fb_dur, fb_mode, hit = infer_fallback(name, stext, ingredients_text)
            if hit:
                device = fb_dev
                dev_word = dev_word or "(推断)"
                temp = temp if temp is not None else fb_temp
                dur = dur if dur is not None else fb_dur
                mode = mode or fb_mode
        ptype, human, unatt = classify_step(stext, device, dur is not None, dur)
        if is_soak:
            # 归一成"等待": 不占设备、不需人盯, 超长时会被调度器移出关键路径
            device, dev_word = DEVICE_NONE, ""
            ptype, human, unatt = WAIT, False, True
        if is_from_fridge:
            # "取出…接着擀/切/包" 是切配备料。不覆盖的话会被 FINISH_RE 里的"取出"
            # 判成 PLATE(装盘), 输出里就成了一条 type=6 的"装盘"行。
            ptype, human, unatt = PREP, True, False
        if is_load_only:
            # 装载句: 摘设备, 回落成人工备料(PREP)。
            #   estimate_duration 对 PREP 给 2 分钟, 而不是烤箱/蒸箱的 8 分钟 ——
            #   这才是"人把烤盘放进烤箱"的真实量级(实测虚占设备 8 分钟/条)。
            #   ptype 也必须一起改: 留着"烹饪"会输出一条 device=人工 的烹饪行。
            device, dev_word = DEVICE_NONE, ""
            ptype, human, unatt = PREP, True, False
        # ---- 3.1.3 参数合法性校验: 超出设备能力范围自动修正到合法边界 ----
        # 只对**真的要进设备**的步骤做。浸泡/等待步已被清成"人工", 不会被钳。
        # 实测(官方 100 道菜): 修掉烤箱 40℃(下限 60)、烤箱 240℃(上限 230) 等越界;
        # 同时把"认不出手法"的蒸/烤步收敛到合法默认模式, 不再透传空模式。
        if device in _ds.DEVICE_SPEC:
            mode, temp, dur = _ds.clamp_params(device, mode, temp, dur)
        # 灶具没有温度参数(官方《设备参数清单参考》只给"火力档位 1~7")。
        # 菜谱里的"油温上升至180℃""烧至七成热"说的是**油温**, 不是设备设定值 ——
        # 拿它当 cookingParameters.temperature 输出, 等于给灶具编了个不存在的温度档。
        # 输出时改用 device_spec.stove_heat() 归一到"大火/中火/小火"(规范 4.5.2 允许)。
        if device == DEVICE_STOVE:
            temp = None
        recipe_steps.append(RecipeStep(
            step_id=i,
            raw_text=stext,
            device=device,
            device_source=dev_word,
            mode=normalize_mode(device, mode),
            temperature_c=temp,
            duration_min=dur,
            needs_human=human,
            unattended=unatt,
            prep_type=ptype,
            depends_on=[i - 1] if i > 1 else [],  # 线性依赖
        ))

    # LLM 补全: 仍缺设备/时长的加热步骤, 走大模型 (带缓存, 失败静默回退)
    need_llm = any(s.device == DEVICE_NONE and
                   any(v in s.raw_text for v in COOK_VERBS + PLACEHOLDER)
                   for s in recipe_steps)
    if need_llm:
        try:
            from app.parser.enrich import enrich_recipe
            llm = enrich_recipe(name, steps_raw)
            for s in recipe_steps:
                info = llm.get(s.step_id)
                if info and s.device == DEVICE_NONE:
                    if info.get("device"):
                        s.device = info["device"]
                        s.device_source = "(LLM)"
                    if info.get("duration_min"):
                        s.duration_min = float(info["duration_min"])
                    if info.get("temperature"):
                        s.temperature_c = float(info["temperature"]) if isinstance(info["temperature"], (int, float)) else s.temperature_c
                    if info.get("mode"):
                        s.mode = normalize_mode(s.device, info["mode"])
                    # LLM 给的参数同样要过合法性校验 (它可能给出 250℃ 这种越界值)
                    if s.device in _ds.DEVICE_SPEC:
                        s.mode, s.temperature_c, s.duration_min = _ds.clamp_params(
                            s.device, s.mode, s.temperature_c, s.duration_min)
        except Exception:
            pass

    # ---- v3.1: 复合设备(蒸烤一体机)的跨步继承 ----
    # 只在"这道菜**压根没有**蒸箱/烤箱"时才补, 所以对已经认对的菜零影响:
    #   美式薯条 第1步「将薯条铺在垫有硅油纸的蒸烤架上，放入机器中间层」(备料)
    #            第2步「选择"开始烹饪"按键，按屏幕提示操作」(真正的设备步, 但无线索)
    # 第2步单看只能判成人工 → 整道菜不占任何设备。用同菜其它步骤的复合设备线索补。
    #
    # ⚠ 守卫口径修正 (2026-10-06 抽查发现): 原来是 `not any(s.device in HEATING_DEVICES)`,
    #   而 HEATING_DEVICES = (灶具, 蒸箱, 烤箱) —— **灶具也算**。于是只要这道菜有
    #   任何一个灶具步(哪怕只是"煮鸡蛋15分钟"这种备料), 守卫就为假, 蒸烤一体机的
    #   继承被整个跳过, 真正的烘烤步永远停在"人工"。
    #   实测「玛格丽特」: #1 煮鸡蛋(灶具) 让守卫为假 ⇒ #4「放入机器中间层，选择
    #   "开始烹饪"按键」= 人工/切配 ⇒ 被 rule_prior 当成备菜 ⇒ 整道菜(4 步)落进 t<0,
    #   当天只剩 3 分钟的"晾凉" ⇒ T=3.0min、设备利用率 0%, **烘烤段在甘特图上不存在**。
    #   同一句单独解析(菜里没有灶具步)时是能判成"烤箱"的 —— 判据自相矛盾。
    #   改成只看蒸箱/烤箱: 灶具不该挡住复合设备的推断(它本来就不是同一台机器)。
    if not any(s.device in (DEVICE_STEAMER, DEVICE_OVEN) for s in recipe_steps):
        # ctx 用「菜名 + 食材 + 全部步骤原文」: 占位句本身没有品类线索,
        # 品类只能从整道菜看（菜名里有"蛋糕/烤芦笋"就够判档）。
        _ctx = f"{name} {ingredients_text} " + " ".join(s.raw_text for s in recipe_steps)
        hint = combo_device_of(" ".join(s.raw_text for s in recipe_steps) + " " + name, _ctx)
        if hint:
            hdev, htemp, hmode, hdur = hint
            for s in recipe_steps:
                if s.device == DEVICE_NONE and LOAD_START_VERBS.search(s.raw_text):
                    s.device = hdev
                    s.device_source = "(复合设备推断)"
                    s.mode = normalize_mode(hdev, hmode)
                    s.temperature_c = htemp
                    # 时长按品类分档写入（不再一律交给 engine 的 8 分钟常数）。
                    s.duration_min = hdur
                    s.prep_type, s.needs_human, s.unattended = COOK, False, True

    # ---- 依赖图: 线性链 + 安全并行分支 (v2.3) ----
    # 原实现是纯线性链 depends_on=[i-1], 把每一步都串起来。
    # 但"无人值守的设备程序运行"期间, 人完全可以去做另一件事的切配 ——
    # 这正是赛题 L2 点名要的"识别可并行步骤(如炖煮无人值守时可切另一道菜的食材)"。
    # 线性链会把"切配"强制排在"炖煮"之后, 把这个并行机会堵死。
    #
    # 这里只放松**最安全的一种形态**:
    #   前一步无人值守(设备程序跑) + 后一步是纯人工切配 + 后一步不消费前一步的产物
    # 并且**保持传递性**: 后一步的下一步必须同时等"设备步"和"这个切配步",
    # 否则会出现"切配提前跑完了, 但取用设备产物的那一步也跟着提前"的依赖违反。
    _CONSUME_PREV = re.compile(r"取出|捞出|沥干|沥水|倒出|盛出|盛入|捞出|滤出")
    for idx in range(2, len(recipe_steps)):
        prev, cur = recipe_steps[idx - 1], recipe_steps[idx]
        if not (prev.unattended and not prev.needs_human):
            continue
        if not (cur.prep_type == PREP and cur.needs_human and not cur.unattended):
            continue
        if _CONSUME_PREV.search(cur.raw_text):
            continue
        # 放松: 切配只需等"设备步"的前一步
        cur.depends_on = [recipe_steps[idx - 2].step_id]
        # 补传递性
        if idx + 1 < len(recipe_steps):
            nxt = recipe_steps[idx + 1]
            if prev.step_id not in nxt.depends_on:
                nxt.depends_on.append(prev.step_id)

    rec = Recipe(name=name, ingredients=ingredients, steps=recipe_steps, label=label)
    # ---- 3.1.4 预热任务拆分 ----
    # 拆成独立步骤节点后, 它会真正进入 DAG 并占用设备槽 (旧实现是排程后硬塞一条
    # 不占资源的合成任务, 实测 100% 与别的菜真实设备任务重叠)。
    # FT_PREHEAT=off 可关掉, 供口径对照。
    if os.environ.get("FT_PREHEAT", "on").lower() not in ("off", "0", "false", "no"):
        rec = insert_preheat_steps(rec)
    return rec
