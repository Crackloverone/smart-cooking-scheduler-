"""
官方接口规范适配器 (ZX-2026-030 评分接口)
输入: [{id, name}]
输出: overview / cookingTimeline / ingredientsSummary / detailTimeline / recipeDetail
"""
from __future__ import annotations
import os, re
from datetime import datetime, timedelta
from app.parser.recipe_parser import parse_recipe, is_fridge_retrieval
from app.scheduler.engine import schedule
from app.spec import device_spec as _ds
from app.spec import recipe_store as _store

_DB: dict = {}      # 菜名 -> 菜谱 (重名保留第一条, 供按名查询)
_ROWS: list = []    # 全部条数, 按原始顺序 (官方菜谱存在重名菜品, 不能去重丢条)
_VER = None         # 派生缓存的版本戳 (官方层/用户层 mtime)


def _load():
    """把菜谱库灌进 `_ROWS` / `_DB`。

    **唯一真源是 `app/spec/recipe_store.py`**（官方 100 道 CSV + 用户层增删改）。
    这里不再自己 open CSV —— 原来两处各读各的，后果是"用户在菜谱库里新增的菜
    排不出来"（库里有、排程看不到），这是最难查的一类不一致。

    缓存按 recipe_store.version() 失效，而不是 `if _DB: return`：
    后者是一次性硬缓存，用户改完菜谱在本进程内永远不会生效。
    """
    global _VER
    v = _store.version()
    if _VER == v and _ROWS:
        return
    _ROWS.clear()
    _DB.clear()
    for r in _store.all_records():
        rec = {"name": r["name"], "id": r["id"],
               "ingredients": r["ingredients"], "steps": r["steps"]}
        _ROWS.append(rec)
        _DB.setdefault(rec["name"], rec)
    _VER = v


def all_names() -> list[str]:
    """官方 100 道菜名 (含重名菜品, 条数与赛题一致)。"""
    _load()
    return [r["name"] for r in _ROWS]


def _category_of(name: str, steps_text: str) -> str:
    """
    菜品分类。前端按它做分类筛选, 所以必须站得住。

    v2.2 修复: 原实现把**步骤全文**和菜名拼在一起匹配, 且"汤|炖|煲|羹"判断在最前面。
    官方菜谱的步骤里到处是"倒掉汤汁""加入高汤""蒸箱预热"这类噪声, 于是:
        '豉汁蒸草鱼' -> 步骤里出现"汤"   -> 汤羹炖品   (实际是蒸菜)
        '苹果芒果派' -> 步骤里出现"蒸箱" -> 蒸箱料理   (实际是烘焙)
    分类错了, 前端"蒸箱料理"筛选里会混进派, 用户一眼就能看出不对。

    现在分两级: 菜名是强信号(优先), 步骤只在菜名无信号时作弱信号,
    且只认"蒸箱/烤箱/炖煮"这种明确的设备/工艺词。
    """
    for pat, cat in (
        (r"羹|炖|煲|汤$|汤品|浓汤", "汤羹炖品"),
        (r"蒸", "蒸箱料理"),
        (r"烤|焗|焙|派|酥|挞|饼干|蛋糕|面包|布丁", "烤箱烘焙"),
        (r"炒|爆|煎|炸", "灶眼快炒"),
    ):
        if re.search(pat, name):
            return cat
    # 菜名没信号 -> 看步骤里的设备/工艺词 (弱信号)
    if re.search(r"蒸箱|普通蒸|蒸制|高温蒸", steps_text):
        return "蒸箱料理"
    if re.search(r"烤箱|上下火|烤制|烘烤|预热.*烤", steps_text):
        return "烤箱烘焙"
    if re.search(r"炖煮|煲汤|熬煮|小火炖", steps_text):
        return "汤羹炖品"
    return "主食套餐"


def all_meta() -> list[dict]:
    """返回 [{name, category, steps, minutes}] 供前端分类筛选"""
    _load()
    out = []
    for rec in _ROWS:
        name = rec["name"]
        n_steps = len(re.findall(r"第\s*\d+\s*步", rec["steps"]))
        # 总时长: 累加文本里的分钟数
        mins = sum(float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*分钟", rec["steps"]))
        hours = sum(float(x) * 60 for x in re.findall(r"(\d+(?:\.\d+)?)\s*个?小时", rec["steps"]))
        out.append({"name": name, "category": _category_of(name, rec["steps"]),
                    "steps": n_steps, "minutes": int(mins + hours)})
    return out


def get(name: str):
    _load()
    return _DB.get(name)


# ---------- 菜名解析（官方评分接口的"不静默丢菜"闸门） ----------
# 赛题《接口规范》第 8 条: cookingTimeline / recipeDetail 必须覆盖请求中的**全部**菜谱。
# 原实现是 `if not rec: continue` —— 库外菜名被静默丢掉, 响应里 recipeCount 变小,
# 而调用方只会看到一份"看起来正常"的残缺计划（自动评分脚本不会报错, 只会按缺失计 0）。
#
# 两条补救路径:
#   ① 调用方随请求带上 ingredients / steps  -> 直接按传入内容解析（支持任意自有菜谱）;
#   ② 只给菜名 -> 先做**保守**的名字归一（空白/零宽/全角字符），再查库。
# 两条都拿不到 -> 由调用方报 422, 绝不在这里静默吞掉。

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\ufeff\u2060]")
_WS = re.compile(r"[\s\u3000]+")


def normalize_name(name: str) -> str:
    """保守归一: 去空白/零宽字符, 全角 ASCII 转半角。

    **不做**模糊匹配（不许把"豆豉蒸腐竹"匹配到"豆豉蒸腐竹（台式）"）——
    规范第 7 条要求输出名与请求名完全一致, 匹配过宽会让评委拿到"名字对了、
    内容却是另一道菜"的结果, 比直接报错更难排查。
    """
    if not name:
        return ""
    s = _ZERO_WIDTH.sub("", str(name))
    s = _WS.sub("", s)
    # 全角 -> 半角（仅 ASCII 可见区间）
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    return s


def resolve_items(request_items: list[dict]) -> tuple[list[dict], list[str]]:
    """把请求条目解析成可排程的 {name, ingredients, steps}。

    返回 `(可排程条目, 无法解析的菜名)`。
    `name` 一律**保持请求原文**（规范第 7 条: 名称必须与请求完全一致）。
    """
    _load()
    ok: list[dict] = []
    bad: list[str] = []
    for it in request_items:
        raw = str(it.get("name", ""))
        rec = get(raw) or get(normalize_name(raw))
        ing = str(it.get("ingredients") or "").strip()
        stp = str(it.get("steps") or "").strip()
        if rec:
            if not ing:
                ing = rec["ingredients"]
            if not stp:
                stp = rec["steps"]
        if not stp:
            bad.append(raw)
            continue
        ok.append({"name": raw, "ingredients": ing, "steps": stp})
    return ok, bad


# ---------- 食材解析 ----------
# 主料：xxx；辅料：xxx；调味品：xxx  或 A料：xxx B料：xxx
_MEAT = {"鸡","鸭","鱼","虾","肉","牛","羊","猪","排","蟹","贝","鱿","带","鲍","参","鸽","蛋","火腿","肠","里脊","五花","骨","翅","尾","头","足","鱿","墨鱼","蛤","蛎","螺","蛏","黄鳝","泥鳅"}
_SPICE = {"盐","糖","油","酱","醋","料酒","生抽","老抽","胡椒","椒","蒜","葱","姜","八角","桂皮","香","粉","汁","蜂蜜","芝麻","淀粉","生粉","面粉","水","酵母","泡打","可可","咖啡","香草","芥末","味精","鸡精","五香粉","南乳","麦芽糖","黄油","奶油","酵母","盐","酱油"}


def _split_ingredients(text: str):
    """返回 (主料list, 辅料/调味list) 每项 {name,unit}"""
    majors, minors = [], []
    # 先按 主料/辅料/调味品/A料/B料/C料 切段
    parts = re.split(r"(主料[：:]|辅料[：:]|调味品[：:]|A料[：:]|B料[：:]|C料[：:])", text)
    # parts: [前缀, key, val, key, val...]
    bucket = majors
    i = 1
    while i < len(parts):
        key = parts[i]
        val = parts[i+1] if i+1 < len(parts) else ""
        if "主料" in key:
            bucket = majors
        else:
            bucket = minors
        for item in re.split(r"[；;]", val):
            item = item.strip()
            if not item:
                continue
            m = re.match(r"^(.+?)(\d+(?:\.\d+)?\s*(?:克|g|G|ml|毫升|个|根|片|勺|茶匙|适量|克左右))$", item)
            if m:
                bucket.append({"name": m.group(1).strip(), "unit": m.group(2).strip()})
            else:
                bucket.append({"name": item, "unit": ""})
        i += 2
    return majors, minors


def _classify_ingredient(name: str) -> str:
    if any(s in name for s in _SPICE) and not any(m in name for m in _MEAT) and not any(v in name for v in ["菜","瓜","果","菇","笋","椒","芹","萝","卜","藕","耳","豆","蔬"]):
        return "调味品"
    if any(m in name for m in _MEAT):
        return "荤菜"
    return "素菜"


# ---------- 食材用量合并 (文档 3.4.2) ----------
_QTY = re.compile(r"^(\d+(?:\.\d+)?)\s*(.*)$")


def _qty(u: str):
    """把 "30克" 拆成 (30.0, "克"); "适量" 拆成 (None, "适量")。"""
    m = _QTY.match((u or "").strip())
    if not m:
        return None, (u or "").strip()
    return float(m.group(1)), m.group(2).strip()


def _merge_unit(a: str, b: str) -> str:
    """合并同名食材的两次用量。

    · "30克" + "20克"  -> "50克"      (同单位, 数值相加)
    · "2个"  + "3个"   -> "5个"
    · "适量" + "适量"  -> "适量"
    · "30克" + "2个"   -> "30克+2个"  (单位不同不能相加, 但也不能丢掉任何一个)
    · ""     + "30克"  -> "30克"
    """
    va, ua = _qty(a)
    vb, ub = _qty(b)
    if va is None and vb is None:
        return a or b
    if va is None or vb is None or ua != ub:
        parts = [p for p in (a, b) if p]
        out: list[str] = []
        for p in parts:
            if p not in out:
                out.append(p)
        return "+".join(out)
    s = va + vb
    s = int(s) if abs(s - int(s)) < 1e-9 else round(s, 2)
    return f"{s}{ua}"


# ---------- 步骤类型推断 ----------
def _step_type(raw: str, device: str, has_temp: bool) -> int:
    """官方接口规范 4.5.1 的 type 枚举 (1~6)。

    v2.2 修复: 3(炖煮类)/4(炒制类) 必须**真的用灶具**, 否则会产出一个
    "type=3 却没有 recipeNames"的行 —— 规范 4.5.2 明确要求设备工作步骤
    (type=3/4/5) 必须返回 recipeNames。旧实现只看文本关键词, 于是
    "汤汁浓稠后加入盐调味"这种纯人工收尾句(含"汤"字)被判成炖煮类。
    """
    dev = device if device and device != "人工" else ""
    # ---- 1) 设备优先 (v3.1 重排) ----
    # 规范 4.5.1: type=5 就是"蒸箱或烤箱工作(包括预热、蒸、烤)"。
    # 解析层已经保证**纯收尾句的设备被清成"人工"**, 所以"还挂着蒸箱/烤箱"的
    # 必然是设备工作步 —— 先按设备定性, 再谈文本关键词, 顺序反了就会出事:
    #   · 「开启蒸箱，取出水箱加满水，选择"智能菜谱-鱼类-豉汁蒸草鱼"…，100℃，8分钟」
    #     (44 条装载句之一) 里的「取出」让它被判成 type=6 装盘;
    #   · 菜名里的「豉**汁**」又让它被判成 type=2 腌制备料。
    #   两次都错在"先看词、后看设备"。这两条都是各菜的主烹饪步, 判错等于这道菜
    #   在甘特图上没有蒸制条、detailTimeline 里少一条设备工作行。
    if dev in ("蒸箱", "烤箱"):
        return 5
    if dev == "灶具":
        if re.search(r"炖|煮|焯|煲|焖|煨|卤|汤", raw):
            return 3
        if re.search(r"炒|煎|炸|爆|煸", raw):
            return 4
        return 3
    if dev == "冰箱":
        return 2
    if dev:
        return 1          # 洗碗机/净咖/烟机等辅助设备, 不是烹饪步骤
    # ---- 2) 无设备: 收尾 > 切配 > 腌制 ----
    # "从冰箱取出后接着擀/切"是备料不是装盘 —— 见 recipe_parser.is_fridge_retrieval
    if not is_fridge_retrieval(raw) and re.search(
            r"装盘|摆盘|出锅|浇汁|撒上|点缀|享用|品尝|取出|端上|上桌", raw):
        return 6
    if re.search(r"洗|切|剥|削皮|切丁|切片|切丝|切碎|准备|备用|擀|包|卷|缝|裹", raw):
        return 1
    if re.search(r"腌|调味|拌匀|酱料|汁", raw):
        return 2
    if re.search(r"炖|煮|焯|煲|汤", raw):
        return 3
    if re.search(r"炒|煎|炸|爆|煸", raw):
        return 4
    return 1


# 官方接口规范 5.11: 步骤标题优先用"预热/烘烤/蒸煮/蒸饪/煎炸/炖煮/爆炒/油炸/
# 焯水/装盘"等**动作名**, 不能用"蒸烤类/炒制类"这种分类名。
_TITLE_BY_TYPE = {1: "食材处理", 2: "腌制备料", 6: "装盘"}


def _step_title(raw: str, device: str, stype: int) -> str:
    if "预热" in raw:
        return "预热"
    if device == "烟机":
        # 100 道菜 0 次提到烟机, 这里只是"万一将来真写了"的兜底。
        # 标题用设备名本身, 不再叫"烟机联动" —— 官方规范里没有"联动"这个概念。
        return "烟机"
    if stype == 5:
        return "烘烤" if device == "烤箱" else "蒸煮"
    if stype == 3:
        return "焯水" if "焯" in raw else "炖煮"
    if stype == 4:
        if "炸" in raw:
            return "油炸"
        if "煎" in raw:
            return "煎炸"
        return "爆炒"
    return _TITLE_BY_TYPE.get(stype, "食材处理")


_LAST_AGENT: dict = {}   # 保留最近一次闭环结果, 供内部端点回显决策链
_LAST_PLAN: dict = {}    # 最近一次 build_plan 排出的 SchedulePlan 对象


def last_plan():
    """最近一次 build_plan 排出的 SchedulePlan 对象 (没有则 None)。

    为什么需要它: /api/plan 的响应必须是**纯官方 schema**(规范要求 16: 只能含合法
    JSON, 不得附加额外字段), 所以前端画甘特图/指标卡要用的内部字段
    (tasks / makespan_min / finish_gap_min ...) 不能塞进那个响应。只能在这里取。
    """
    return _LAST_PLAN.get("plan")


def plan_recipes(recipes: list, user_text: str = "", use_llm: bool | None = None,
                 agent: bool | None = None):
    """统一排程入口 —— 全项目只有这一处决定"怎么排"。

    agent=True (默认) : 走 Agent 决策闭环 (策略择优 + CP-SAT 精解 + 可选在线模型决策)
    agent=False       : 退回单策略求解器, 用于对照实验/性能基线

    闭环自带时间预算与熔断降级, 任何情况下都能返回一份合法排程。
    """
    if agent is None:
        agent = os.environ.get("FT_AGENT", "on").lower() not in ("off", "0", "false", "no")
    if not agent:
        _LAST_AGENT.pop("result", None)
        return schedule(recipes)
    # ⚠ 有**请求级设备容量覆盖**时必须关掉 CP-SAT。
    #   cpsat.py 的模型是固定编码的，不读 `engine.resource_units()` ——
    #   设备置 0 之后它照旧把坏设备排进去（实测：`devices={"蒸箱":0}` 排出来的
    #   计划里主设备仍是蒸箱，与"没有蒸箱"这个前提直接矛盾）。
    #   精确轨不受影响：它走 `model.capacities() -> engine.resource_units()`，是认覆盖的。
    #   放在这里而不是各调用方：本函数是**全项目唯一**决定"怎么排"的地方，
    #   散到调用方就会漏（/reschedule 当初就漏过一次）。
    from app.scheduler.engine import RESOURCE_UNITS as _U, resource_units as _ru
    _override_on = _ru() is not _U
    from app.agent.orchestrator import run as agent_run
    res = agent_run(recipes, user_text=user_text, use_llm=use_llm,
                    use_cpsat=not _override_on)
    _LAST_AGENT["result"] = res
    return res.plan


def last_agent_result():
    """最近一次闭环的 AgentResult (含 trace/llm 统计), 没有则 None。"""
    return _LAST_AGENT.get("result")


def build_plan(request_items: list[dict], user_text: str = "",
               use_llm: bool | None = None,
               agent: bool | None = None) -> dict:
    """主入口: request_items = [{id, name}]

    v2.1: 排程不再直接调求解器, 而是走 Agent 决策闭环 (app/agent/orchestrator):
        目标塑形 -> 语义先验 -> 策略择优 -> 求解器执行 -> 校验反馈 -> (反思) -> CP-SAT 精解
    闭环内部有严格时间预算与熔断降级, 输出仍是**纯官方 schema**, 不附加任何字段。
    """
    _load()
    now = datetime.now()
    # 解析失败的不在这里静默跳过 —— 由调用方（/api/plan）显式报 422。
    recipes_input, _unresolved = resolve_items(request_items)

    plan = plan_recipes(
        [parse_recipe(r["name"], r["ingredients"], r["steps"]) for r in recipes_input],
        user_text=user_text, use_llm=use_llm, agent=agent,
    )
    _LAST_PLAN["plan"] = plan      # 供 last_plan() 回显 (不进入本函数返回值)
    makespan = plan.makespan_min

    # cookingTimeline: 每道菜的主设备和起止
    # 规范 4.3: product = "该菜谱使用的唯一主要设备"。
    # v2.2 修复: 旧实现取"该菜**第一个**任务的设备", 而绝大多数菜第一步是切配
    # (device=人工), 被兜底成"灶具" —— 于是清蒸带鱼、蒸箱菜全部显示"灶具"。
    # 现在取**单项最长的烹饪设备**(最能代表"这道菜靠什么做熟"), 同长再看累计占用。
    # 只用累计会被"冷藏/发酵"这类长等待带偏(苹果芒果派会被判成冰箱)。
    COOK_DEV = ("灶具", "蒸箱", "烤箱", "冰箱")
    dev_stat: dict = {}          # dish -> device -> [最长单项, 累计]
    dish_start, dish_end = {}, {}
    for t in plan.tasks:
        if t.start_min < 0 or not t.dish:
            continue
        dish_start[t.dish] = min(dish_start.get(t.dish, t.start_min), t.start_min)
        dish_end[t.dish] = max(dish_end.get(t.dish, t.end_min), t.end_min)
        if t.device in COOK_DEV:
            m = dev_stat.setdefault(t.dish, {})
            cur = m.setdefault(t.device, [0.0, 0.0])
            dur = max(0.0, float(t.duration_min or 0))
            cur[0] = max(cur[0], dur)
            cur[1] += dur
    dish_devices = {}
    for d, m in dev_stat.items():
        # 冰箱只作最后兜底: 它是储藏/等待设备, 不代表"这道菜靠什么做熟"
        # (否则苹果芒果派会被判成冰箱 —— 派皮冷藏比烘烤还久)
        cand = {k: v for k, v in m.items() if k in ("灶具", "蒸箱", "烤箱")} or m
        dish_devices[d] = max(cand.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))[0]

    def hhmm(offset_min: float) -> str:
        return (now + timedelta(minutes=offset_min)).strftime("%H:%M")

    cooking_timeline = []
    for r in recipes_input:
        n = r["name"]
        cooking_timeline.append({
            "name": n,
            "product": dish_devices.get(n, "灶具"),
            "startTime": hhmm(dish_start.get(n, 0)),
            "endTime": hhmm(dish_end.get(n, makespan)),
            "timeSpent": str(int(round(dish_end.get(n, makespan) - dish_start.get(n, 0)))),
        })

    # ingredientsSummary —— 文档 3.4.2: "按荤菜/素菜/调味品三类合并食材用量,
    # 同类食材合并数量, 不重复"。旧实现同名食材直接**覆盖** unit, 于是
    # "五花肉 200克"(A菜) + "五花肉 300克"(B菜) 会只剩 300克, 采购量凭空少一半。
    buckets = {"荤菜": {}, "素菜": {}, "调味品": {}}
    for r in recipes_input:
        # 用 resolve_items 解析好的内容, **不要**再 get(name) ——
        # 库外菜名（调用方自带 steps）在库里查不到, get() 返回 None 会直接崩。
        majors, minors = _split_ingredients(r["ingredients"])
        for ing in majors + minors:
            cat = _classify_ingredient(ing["name"])
            nm, unit = ing["name"], ing["unit"]
            buckets[cat][nm] = (_merge_unit(buckets[cat][nm], unit)
                                if nm in buckets[cat] else unit)
    ingredients_summary = [
        {"type": k, "list": [{"name": n, "unit": u} for n, u in v.items()]}
        for k, v in buckets.items()
    ]

    # detailTimeline: 把任务按时间合并成区间
    timeline_rows = []
    for t in plan.tasks:
        if t.start_min < 0:
            continue
        has_temp = getattr(t, "temp_c", None) is not None
        stype = _step_type(t.raw_text, t.device, has_temp)
        row = {
            "timeInterval": f"{int(t.start_min)}-{int(t.end_min)}",
            "title": _step_title(t.raw_text, t.device, stype),
            "type": stype,
            "list": [t.raw_text[:60]],
        }
        # 规范 4.5.2: 设备工作步骤(type=3/4/5)必须返回 recipeNames。
        # 合成任务(预热/烟机联动)自身没有 dish, 用 dish_list 补齐它服务的菜。
        dishes = list(getattr(t, "dish_list", None) or [])
        if t.dish and t.dish not in dishes:
            dishes.insert(0, t.dish)
        is_device_step = stype in (3, 4, 5)
        if (is_device_step or t.device not in ("", "人工")) and dishes:
            row["recipeNames"] = dishes
            # 预热必须给出工作温度与预热时间(规范 4.5.2)
            if "预热" in t.raw_text:
                temp = t.temp_c if t.temp_c else (180 if t.device == "烤箱" else 100)
            elif t.device == "灶具":
                # 灶具只有"火力档位", 没有温度参数 (官方设备清单)。
                # 输出"大火/中火/小火", 不再把油温当成设备设定值。
                temp = _ds.stove_heat(t.raw_text)
            else:
                temp = t.temp_c if t.temp_c else "中火"
            row["parameters"] = [{
                "recipeName": dishes[0],
                "temperature": temp,
                "time": max(1, int(round(t.duration_min))),
            }]
        timeline_rows.append(row)
    # 按 type 升序, 再按区间开始升序
    timeline_rows.sort(key=lambda x: (x["type"], int(x["timeInterval"].split("-")[0])))

    # recipeDetail
    recipe_detail = []
    for r in recipes_input:
        # 同上: 一律用已解析好的内容, 支持库外菜谱。
        majors, minors = _split_ingredients(r["ingredients"])
        parsed = parse_recipe(r["name"], r["ingredients"], r["steps"])
        steps = []
        for st in parsed.steps:
            d = {"describe": st.raw_text[:80]}
            if st.device and st.device != "人工" and st.duration_min:
                d["cookingParameters"] = {
                    # v3.0: 旧实现回落成 "标准模式" —— 这个值**不在官方模式枚举里**,
                    # 输出它等于又造一个非法参数 (官方规范 4.6.1 要求 mode 必填且合法)。
                    # 现在回落到该设备的官方默认模式 (蒸箱=普通蒸, 烤箱=上下火...)。
                    "mode": st.mode or _ds.default_mode(st.device) or "标准模式",
                    "temperature": (str(st.temperature_c) if st.temperature_c
                                    else _ds.stove_heat(st.raw_text, st.mode)),
                    "time": str(int(st.duration_min)),
                }
            steps.append(d)
        recipe_detail.append({
            "name": r["name"],
            "majorIngredients": majors,
            "minorIngredients": minors,
            "cookingSteps": steps,
        })

    # 规范要求4: timeSpent 为整个并行计划从开始到完成的**实际跨度**。
    # 必须用含合成任务(预热/烟机联动)在内的最晚结束时刻 —— plan.makespan_min
    # 是在合成任务加入之前算的, 而烟机联动会延到 end+3 分钟, 于是会出现
    # "timeInterval 214-227 但 timeSpent=226" 这种自相矛盾的输出(违反要求6)。
    live = [t for t in plan.tasks if t.start_min >= 0]
    span = max((t.end_min for t in live), default=0.0)
    makespan = max(float(makespan), span)
    serial = sum(float(t.duration_min) for t in live)
    time_save = max(0, int(round(serial - makespan)))

    return {
        "overview": {
            "finishTime": hhmm(makespan),
            "timeSpent": str(int(round(makespan))),
            "timeSave": str(time_save),
            "recipeCount": len(recipes_input),
        },
        "cookingTimeline": cooking_timeline,
        "ingredientsSummary": ingredients_summary,
        "detailTimeline": timeline_rows,
        "recipeDetail": recipe_detail,
    }
