"""L1 结构化解析全量体检：跑 100 道菜，统计设备/时长/依赖/人工识别情况。

结果存 `data/l1_report.json`，作为技术文档的准确率证据。

两块内容
--------
**A. 覆盖率（全量 100 道菜）** —— 回答"解析器在官方库上跑得动吗"。
**B. 对照准确率（`data/l1_gold.json` 人工标注集）** —— 回答"解析得**对不对**"。

为什么必须有 B
--------------
A 只能自证。赛题 L1 的 20 分是**与标注答案逐条比对**打出来的：
「设备识别准确率 / 时间提取准确率 / 依赖关系推理正确率 / 人工介入判断准确率」。
没有标注集，就只能报一个"覆盖率 54.3%"——那既不是准确率，也没法回答
"你怎么证明时间提取是准的"。

⚠ 口径说明（v2.3 修正，仍然成立）
--------------------------------
旧版只报一个 `时长提取率 = 有时长的步数 / 总步数`，**口径是错的**：
官方 100 道菜共 762 步，其中 481 步是"洋葱胡萝卜洗净切碎"这类**人工准备步**，
它们在原文里本来就不写时长（也不需要），却全部被算进了分母。
于是报出 29%，看起来像解析器很弱，实际是分母里混了 63% 无关样本。

现在按**步骤类别**分别统计：
  - 设备工作步骤（蒸/烤/炖/煮/炒…）：**必须**有时长 —— 这才是真正要考的口径
  - 人工准备步骤：不需要显式时长（排程时按 `estimate_duration` 估 1~3 分钟）
并额外给出**原文显式时长**作为"解析能力的理论上限"。

⚠ B 的准确率同样只在 **gold 非 null** 的步上统计 —— 原文没写温度/时长的步，
"解析值对不对"这件事本身无定义，把它算进分母会造出一个假的低分
（旧版的 13.7% 就是这么来的）。
"""
from __future__ import annotations

import json
import os
import re

# 默认离线：L1 体检量的是**规则解析**质量，不是模型。
# 默认 provider 换成本地 Ollama（免密钥）之后 enabled() 默认为 True，
# 而 parse_recipe 会在设备参数缺失时调 enrich（走模型）——
# 那会让"准确率证据"变成"这台机器装没装模型"的函数。理由详见 scripts/_offline.py。
for _k in ("FT_LLM", "FT_AGENT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ.setdefault(_k, "off")

import difflib

from app.parser.recipe_parser import parse_recipe, split_steps
from app.spec.adapter import all_names, get

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_GOLD = os.path.join(_ROOT, "data", "l1_gold.json")

# 原文里写死了时长的判据（与 parse_duration 的能力边界对齐）
# ⚠ 中文数字也算"原文显式"：「冷藏**两**小时」是菜谱写死的事实，不是解析器猜的。
#   漏了它会把"解析能力的理论上限"报低（也让人误以为那一步只能靠兜底）。
_EXPLICIT = re.compile(
    r"[一二两三四五六七八九十]{1,3}\s*个?小时|"
    r"[一二两三四五六七八九十]{1,3}\s*分钟|半\s*个?\s*小时|"
    r"\d+(?:\.\d+)?\s*个?小时|\d+\s*个半\s*小时|半小时|"
    r"\d+(?:\.\d+)?\s*[-~～—至到]\s*\d+(?:\.\d+)?\s*分|"
    r"\d+(?:\.\d+)?\s*分钟|\d+(?:\.\d+)?\s*mins?\b|\d+(?:\.\d+)?\s*hrs?\b|"
    r"\d+(?:\.\d+)?\s*h\b|\d+(?:\.\d+)?\s*秒|\d+(?:\.\d+)?\s*secs?\b|"
    r"\d+(?:\.\d+)?\s*分(?!钟|熟|之|配)",
    re.I,
)


def _pct(a, b):
    return f"{round(a / b * 100, 1)}%" if b else "0%"


# ---------------- A. 全量覆盖率 ----------------

def coverage() -> dict:
    names = all_names()
    tot = dev = dep = hands = none_dev = 0
    dev_buckets: dict = {}
    cook = [0, 0, 0, 0]        # [总数, 有时长, 显式, 兜底]
    prep = [0, 0]
    branch = chain = 0
    parsed_cache = {}

    for n in names:
        rec = get(n)
        if not rec:
            continue
        p = parse_recipe(n, rec["ingredients"], rec["steps"])
        parsed_cache[n] = p
        for s in p.steps:
            tot += 1
            if s.device and s.device != "人工":
                dev += 1
                dev_buckets[s.device] = dev_buckets.get(s.device, 0) + 1
            else:
                none_dev += 1
            if s.depends_on:
                dep += 1
            if s.needs_human:
                hands += 1

            is_cook = (s.device and s.device != "人工") or s.prep_type == "烹饪"
            if is_cook:
                cook[0] += 1
                if s.duration_min:
                    cook[1] += 1
                    if _EXPLICIT.search(s.raw_text):
                        cook[2] += 1
                    else:
                        cook[3] += 1
            else:
                prep[0] += 1
                if s.duration_min:
                    prep[1] += 1

            if s.depends_on:
                if s.depends_on == [s.step_id - 1]:
                    chain += 1
                else:
                    branch += 1

    explicit_total = sum(1 for n, p in parsed_cache.items()
                         for s in p.steps if _EXPLICIT.search(s.raw_text))

    return {
        "菜谱数": len(names),
        "总步数": tot,
        "口径说明": ("时长覆盖率只对「设备工作步骤」有意义。人工准备步骤(切配/装盘)"
                 "在原文里本就不写时长、排程时按 estimate_duration 估 1~3 分钟，"
                 "把它们计入分母会得出无意义的低分。"),
        "设备工作步骤": {
            "总数": cook[0],
            "有时长": cook[1],
            "覆盖率": _pct(cook[1], cook[0]),
            "其中显式抽取": cook[2],
            "其中兜底推断": cook[3],
            "显式占比": _pct(cook[2], cook[0]),
        },
        "人工准备步骤": {"总数": prep[0], "有时长": prep[1],
                   "说明": "不需要显式时长"},
        "原文显式时长": {
            "命中步数": explicit_total,
            "提取率": _pct(explicit_total, tot),
            "说明": "解析能力的理论上限 —— 官方菜谱大多数步骤原文就没写时长",
        },
        "执行主体识别率": _pct(dev + none_dev, tot),
        "真实设备步骤": {"数量": dev, "占比": _pct(dev, tot),
                   "说明": "其余步骤的主体是「人工」—— 切配/装盘等纯手上动作"},
        "依赖标注率": _pct(dep, tot),
        "需人工步骤占比": _pct(hands, tot),
        "依赖图结构": {"线性链": chain, "并行分支": branch,
                  "说明": "并行分支 = 依赖指向非紧邻前序步（无人值守时放开人工切配）"},
        "设备分布": dev_buckets,
    }


# ---------------- B. 对照标注集的逐项准确率 ----------------

def _ancestors(steps_by_id: dict, sid: int, seen=None) -> set:
    """`sid` 的传递前置集合（含直接前置）。"""
    seen = seen if seen is not None else set()
    out = set()
    for pid in (steps_by_id.get(sid).depends_on if steps_by_id.get(sid) else []) or []:
        if pid in seen:
            continue
        seen.add(pid)
        out.add(pid)
        out |= _ancestors(steps_by_id, pid, seen)
    return out


def align_steps(parsed_steps: list, raw_texts: list[str]) -> tuple[dict, dict]:
    """把**解析出的步**对齐回**原文步号**。

    返回 `(by_raw, raw_of)`：
      · `by_raw` : `{原文步号: 解析步}` —— 用于逐项比对
      · `raw_of` : `{解析步 step_id: 原文步号}` —— 用于依赖闭包判断

    为什么必须对齐：解析器会在加热段前**插入合成步**（「蒸箱预热」「烤箱预热」），
    于是 `step_id` 整体后移 —— 直接按 step_id 比对会把 gold#6 拿去和 parsed#6
    （其实是预热步）比，得出一个既不真也不假的结论。

    匹配按 `raw_text` 与原文步的相似度取最佳；相似度低于 0.5 的判为**合成步**，
    不参与逐项比对，但在 `raw_of` 里继承**紧随其后那个真实步**的原文步号 ——
    否则"预热 → 正式烹饪"这条依赖链在原文步号坐标系里会断掉，
    把一条正确实现误报成"缺少前置依赖"。
    """
    by_raw: dict = {}
    best: dict = {}
    raw_of: dict = {}
    for s in parsed_steps:
        txt = getattr(s, "raw_text", "") or ""
        if not txt:
            continue
        scored = [(difflib.SequenceMatcher(None, txt, raw).ratio(), i + 1)
                  for i, raw in enumerate(raw_texts)]
        if not scored:
            continue
        ratio, k = max(scored)
        if ratio < 0.5:
            continue                      # 合成步：不参与比对
        raw_of[s.step_id] = k
        if ratio > best.get(k, 0.0):
            best[k] = ratio
            by_raw[k] = s
    # 合成步继承其后第一个已对齐步的原文步号
    pending: list = []
    for s in parsed_steps:
        if s.step_id in raw_of:
            for q in pending:
                raw_of[q] = raw_of[s.step_id]
            pending = []
        else:
            pending.append(s.step_id)
    last = max(raw_of.values(), default=0)
    for q in pending:
        raw_of[q] = last
    return by_raw, raw_of


def gold_accuracy(gold: dict | None = None) -> dict:
    """拿 `data/l1_gold.json` 逐项比对，返回五类准确率。"""
    if gold is None:
        if not os.path.exists(_GOLD):
            return {"可用": False, "说明": f"未找到标注集 {_GOLD}"}
        with open(_GOLD, encoding="utf-8") as f:
            gold = json.load(f)

    dishes = gold.get("dishes") or {}
    miss_dish: list[str] = []
    n_dev = ok_dev = 0
    n_temp = ok_temp = 0
    n_dur = ok_dur = 0
    n_human = ok_human = 0
    n_serial = ok_serial = 0
    n_step = 0
    # 诊断：错在哪
    dev_errors: list[str] = []
    dur_errors: list[str] = []
    temp_errors: list[str] = []
    human_errors: list[str] = []
    serial_errors: list[str] = []

    for name, gold_steps in dishes.items():
        rec = get(name)
        if not rec:
            miss_dish.append(name)
            continue
        parsed = parse_recipe(name, rec["ingredients"], rec["steps"])
        # 按原文步号对齐（解析器插入的合成步不参与比对）
        by_id, raw_of = align_steps(parsed.steps, split_steps(rec["steps"]))
        pid_by_id = {s.step_id: s for s in parsed.steps}
        for g in gold_steps:
            sid = g["step_id"]
            p = by_id.get(sid)
            if p is None:
                miss_dish.append(f"{name}#{sid}(解析后不存在)")
                continue
            n_step += 1
            tag = f"{name}#{sid}"

            # ① 设备识别
            n_dev += 1
            if (p.device or "人工") == g["device"]:
                ok_dev += 1
            else:
                dev_errors.append(f"{tag} 期望{g['device']} 实得{p.device}")

            # ② 温度提取（只在原文显式写了温度的步上判）
            if g.get("temperature_c") is not None:
                n_temp += 1
                got = p.temperature_c
                if got is not None and abs(float(got) - float(g["temperature_c"])) <= 1:
                    ok_temp += 1
                else:
                    temp_errors.append(f"{tag} 期望{g['temperature_c']} 实得{got}")

            # ③ 时长提取（同上，只在原文显式写了时长的步上判）
            if g.get("duration_min") is not None:
                n_dur += 1
                got = p.duration_min
                if got is not None and abs(float(got) - float(g["duration_min"])) <= 0.6:
                    ok_dur += 1
                else:
                    dur_errors.append(f"{tag} 期望{g['duration_min']} 实得{got}")

            # ④ 人工介入判断
            n_human += 1
            if bool(p.needs_human) == bool(g["needs_human"]):
                ok_human += 1
            else:
                human_errors.append(
                    f"{tag} 期望{'需人工' if g['needs_human'] else '无人值守'} "
                    f"实得{'需人工' if p.needs_human else '无人值守'}")

            # ⑤ 依赖关系（只判"该串行的有没有漏边"，不惩罚"多给了并行自由"）
            if g.get("serial", True):
                n_serial += 1
                prev = sid - 1
                # 判据: 这一步的**前置闭包**里，存在某步落在「第 prev 步或更晚」的原文位置上。
                # 用位置比较而不是"逐字包含 prev"，是为了容纳解析器插入的合成步
                # （预热插在中间，会把"是否直接依赖 #prev"这个判据打偏）。
                anc = _ancestors(pid_by_id, p.step_id)
                latest = max((raw_of.get(d, 0) for d in anc), default=0)
                if prev <= 0 or latest >= prev:
                    ok_serial += 1
                else:
                    serial_errors.append(
                        f"{tag} 缺少对 #{prev} 的前置依赖（前置最深只到原文 #{latest}）")

    return {
        "可用": True,
        "标注集": os.path.relpath(_GOLD, _ROOT).replace("\\", "/"),
        "覆盖菜谱数": len(dishes),
        "对照步数": n_step,
        "缺失或对不上的菜": miss_dish[:8],
        "设备识别准确率": {"正确": ok_dev, "样本": n_dev, "值": _pct(ok_dev, n_dev),
                    "错例": dev_errors[:6]},
        "温度提取准确率": {"正确": ok_temp, "样本": n_temp, "值": _pct(ok_temp, n_temp),
                    "说明": "只在原文显式写了温度的步上统计",
                    "错例": temp_errors[:6]},
        "时长提取准确率": {"正确": ok_dur, "样本": n_dur, "值": _pct(ok_dur, n_dur),
                    "说明": "只在原文显式写了时长的步上统计",
                    "错例": dur_errors[:6]},
        "人工介入判断准确率": {"正确": ok_human, "样本": n_human, "值": _pct(ok_human, n_human),
                     "错例": human_errors[:6]},
        "依赖推理正确率": {"正确": ok_serial, "样本": n_serial, "值": _pct(ok_serial, n_serial),
                    "说明": "只判「标注为必须串行的步是否漏了前置」，不惩罚提前备菜带来的并行放宽",
                    "错例": serial_errors[:6]},
    }


def audit() -> dict:
    r = coverage()
    r["对照标注集"] = gold_accuracy()
    return r


if __name__ == "__main__":
    r = audit()
    out = os.path.join(_ROOT, "data", "l1_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    print(json.dumps(r, ensure_ascii=False, indent=2))
