"""P0-2 护栏 —— L1 的准确率必须**可测**，而且不许悄悄退化。

赛题 L1 的 20 分是"与标注答案逐条比对"打出来的：
设备识别准确率 / 时间提取准确率 / 依赖关系推理正确率 / 人工介入判断准确率。
没有标注集就只能自证 —— 自证在评委眼里等于没有。

这个文件做三件事：
  ① 钉住 `data/l1_gold.json` 自身的完整性（标注集坏了，下面的准确率全是假的）；
  ② 钉住五项准确率的**下限**（谁把解析器改差了，这里立刻红）；
  ③ 钉住 v3.10 新增的两处能力：兜底时长**品类分档**、中文数字时长。
"""
from __future__ import annotations

import json
import os

import pytest

from app.parser.recipe_parser import (cn_number_to_arabic, parse_duration,
                                      parse_recipe, split_steps)
from app.scheduler.engine import estimate_duration
from app.spec.adapter import all_names, get
from scripts.l1_audit import align_steps, gold_accuracy

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GOLD = os.path.join(_ROOT, "data", "l1_gold.json")

# 五项准确率的**下限**（当前实测值留约 2 个点余量）。
# ⚠ 改小它 = 把护栏关掉。要放宽必须说明为什么。
_FLOOR = {
    "设备识别准确率": 90.0,
    "温度提取准确率": 98.0,
    "时长提取准确率": 98.0,
    "人工介入判断准确率": 92.0,
    "依赖推理正确率": 96.0,
}


@pytest.fixture(scope="module")
def gold() -> dict:
    with open(_GOLD, encoding="utf-8") as f:
        return json.load(f)


# ---------- ① 标注集自身的完整性 ----------

def test_标注集覆盖_20_道菜且菜名都在官方库(gold):
    dishes = gold["dishes"]
    assert len(dishes) >= 20, "标注集至少要 20 道菜"
    names = set(all_names())
    missing = [n for n in dishes if n not in names]
    assert not missing, f"标注集里有官方库里不存在的菜名: {missing}"


def test_每个标注步都能在解析结果里找到对应(gold):
    """对齐不上的标注步 ⇒ 后面按原文步号比对的准确率会静默少算样本。

    ⚠ 判据是**按原文步号对齐**，不是"step_id 集合相等"：解析器会在加热段前
      插入合成步（「蒸箱预热」），`step_id` 会整体后移。要求集合相等等于
      禁止解析器插入预热步 —— 那是它**该做**的事。
    """
    bad = []
    for name, steps in gold["dishes"].items():
        rec = get(name)
        parsed = parse_recipe(name, rec["ingredients"], rec["steps"])
        by_raw, _ = align_steps(parsed.steps, split_steps(rec["steps"]))
        for g in steps:
            if g["step_id"] not in by_raw:
                bad.append(f"{name}#{g['step_id']}")
    assert not bad, f"这些标注步在解析结果里对不上（标注集或解析器变了吗）: {bad[:5]}"


def test_标注集四类设备都覆盖(gold):
    devs = {s["device"] for steps in gold["dishes"].values() for s in steps}
    assert {"人工", "灶具", "蒸箱", "烤箱", "冰箱"} <= devs, f"缺设备类别: {devs}"


# ---------- ② 五项准确率下限 ----------

def test_L1_五项准确率不得低于下限():
    acc = gold_accuracy()
    assert acc["可用"], acc
    bad = []
    for k, floor in _FLOOR.items():
        got = float(acc[k]["值"].rstrip("%"))
        if got < floor:
            bad.append(f"{k} {got}% < 下限 {floor}%（错例：{acc[k]['错例'][:2]}）")
    assert not bad, "L1 准确率退化：\n" + "\n".join(bad)


# ---------- ③ v3.10 新增能力 ----------

def test_中文数字时长可解析():
    assert parse_duration("放凉后放入冰箱冷藏两小时") == 120.0
    assert parse_duration("腌制一小时") == 60.0
    assert parse_duration("二十分钟") == 20.0
    assert parse_duration("半个小时") == 30.0
    assert parse_duration("两个半小时") == 150.0


def test_中文数字不许误伤非时长文本():
    """`十分美味`/`五分熟`/`一起`/`一半` 里的"分/一"不是时长单位。"""
    for t in ("十分美味", "五分熟", "一起拌匀", "一半水果", "三成热", "小火煸炒"):
        assert parse_duration(t) is None, f"{t!r} 被误判成时长"
    assert cn_number_to_arabic("一起拌匀") == "一起拌匀"


def test_兜底时长按品类分档_不再是一个设备一个常数():
    """用户点名的例子：香菇烤芦笋与巴斯克蛋糕不该同为同一个值。"""
    def dev_dur(name, dev="烤箱"):
        rec = get(name)
        p = parse_recipe(name, rec["ingredients"], rec["steps"])
        vals = [estimate_duration(s) for s in p.steps if s.device == dev]
        assert vals, f"{name} 没有解析出 {dev} 步"
        return max(vals)

    asparagus = dev_dur("香菇烤芦笋")      # 原文显式 190℃/8 分钟
    basque = dev_dur("巴斯克蛋糕")         # 原文没写时长 ⇒ 走烘焙档
    assert asparagus == 8.0, "原文显式时长必须被原样尊重"
    assert basque > asparagus, "蛋糕（烘焙档）不该与烤芦笋同长"


def test_兜底分档可一键退回旧行为(monkeypatch):
    monkeypatch.setenv("FT_DUR_TIERS", "off")
    rec = get("巴斯克蛋糕")
    p = parse_recipe("巴斯克蛋糕", rec["ingredients"], rec["steps"])
    vals = [estimate_duration(s) for s in p.steps if s.device == "烤箱"]
    assert vals and all(v == 8.0 for v in vals), \
        "FT_DUR_TIERS=off 时应回到旧常数（与历史基线逐位一致）"


def test_鸡精不算肉_不该把素菜推进长档():
    """实测踩到：食材里的「鸡精1克」曾让「蒜蓉粉丝蒸扇贝」被判成蒸肉档 18 分钟。"""
    rec = get("蒜蓉粉丝蒸扇贝")
    p = parse_recipe("蒜蓉粉丝蒸扇贝", rec["ingredients"], rec["steps"])
    vals = [estimate_duration(s) for s in p.steps if s.device == "蒸箱"]
    assert vals and max(vals) <= 10.0, f"扇贝是海鲜档，不该被判成肉档：{vals}"
