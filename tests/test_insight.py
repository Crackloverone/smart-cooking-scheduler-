"""设备降温缓冲 + 排程诊断 (insight) 的护栏。

对应建议文本《智能烹饪调度详细注意事项》：
  · §一.3 / §三.2 / §八.1 —— 同腔体高温切低温必须留降温时间；
  · §七.1 —— 输出关键路径与"时间余量很小、容易延误"的红色预警项；
  · §四.3 —— 冲突要优先告警并给出原因。

每条护栏都做过**变异自检**（注入已知缺陷 ⇒ 断言必红），见文件末尾的说明。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.models.schema import SchedulePlan, ScheduledTask
from app.parser.recipe_parser import parse_recipe
from app.scheduler import engine as E
from app.scheduler import insight as I
from app.scheduler.metrics import device_switches
from app.spec.adapter import all_names, get, plan_recipes


def _mk(name, steps):
    return parse_recipe(name, "", steps)


def _task(tid, start, end, unit, device, temp, dur=None, dish=None, deps=None):
    return ScheduledTask(
        task_id=tid, dish=dish if dish is not None else tid.split("#")[0],
        step_id=int(tid.split("#")[1]) if "#" in tid else 1, raw_text="",
        device=device, unit_id=unit, start_min=start, end_min=end,
        duration_min=dur if dur is not None else (end - start),
        temp_c=temp, depends_on=list(deps or []),
    )


# ---------------------------------------------------------------- 一、降温口径

def test_cooldown_只罚降温():
    """降温要留时间；同温/升温一律为 0（升温成本由 L1 的预热步建模，不能重复计）。"""
    assert E.cooldown_minutes(200, 180) > 0
    assert E.cooldown_minutes(150, 180) == 0        # 升温
    assert E.cooldown_minutes(180, 180) == 0        # 同温
    assert E.cooldown_minutes(180, 170) == 0        # 差 10 <= 同温容差 15


def test_cooldown_温度未知为0():
    assert E.cooldown_minutes(None, 180) == 0
    assert E.cooldown_minutes(180, None) == 0


def test_cooldown_开关可关闭(monkeypatch):
    """FT_COOLDOWN=off 必须让整条链路退回旧行为（供 A/B 对照）。"""
    monkeypatch.setenv("FT_COOLDOWN", "off")
    assert E.cooldown_minutes(200, 100) == 0.0
    assert not E.cooldown_enabled()
    monkeypatch.setenv("FT_COOLDOWN", "on")
    assert E.cooldown_minutes(200, 100) > 0.0


def test_cooldown_速率可覆盖(monkeypatch):
    monkeypatch.setenv("FT_COOLDOWN", "on")
    monkeypatch.setenv("FT_COOLDOWN_RATE", "4")
    assert E.cooldown_minutes(200, 100) == pytest.approx(4.0)
    monkeypatch.setenv("FT_COOLDOWN_RATE", "0")
    assert E.cooldown_minutes(200, 100) == 0.0


# ------------------------------------------------------- 二、校验器能检出违规

def test_device_switches_检出背靠背异温():
    plan = SchedulePlan(tasks=[
        _task("A#1", 0, 10, "烤箱-1", "烤箱", 200.0),
        _task("B#1", 10, 20, "烤箱-1", "烤箱", 180.0),
    ])
    sw = device_switches(plan)
    assert len(sw) == 1, "200℃ 紧接 180℃ 必须被判为缺少降温"
    assert sw[0][0] == "烤箱" and sw[0][1] == "A#1" and sw[0][2] == "B#1"


def test_device_switches_同温与升温不报():
    same = SchedulePlan(tasks=[
        _task("A#1", 0, 10, "烤箱-1", "烤箱", 180.0),
        _task("B#1", 10, 20, "烤箱-1", "烤箱", 182.0),   # 差 2 <= 容差
    ])
    up = SchedulePlan(tasks=[
        _task("A#1", 0, 10, "烤箱-1", "烤箱", 150.0),
        _task("B#1", 10, 20, "烤箱-1", "烤箱", 200.0),   # 升温
    ])
    assert device_switches(same) == []
    assert device_switches(up) == []


def test_device_switches_留够时间不报():
    """中间空档 >= 所需降温时间就不算违规 —— 判据是"留够没有", 不是"是否相邻"。"""
    plan = SchedulePlan(tasks=[
        _task("A#1", 0, 10, "烤箱-1", "烤箱", 200.0),
        _task("B#1", 10 + E.cooldown_minutes(200, 180) + 0.1, 30, "烤箱-1", "烤箱", 180.0),
    ])
    assert device_switches(plan) == []


def test_device_switches_同温重叠不报():
    """同温**重叠**（batch machine 合炉，gap 为负）绝不能被报成降温违规。

    ⚠ 这条专门钉 `device_switches` 里的 `if need <= 0: continue` 早退：
      去掉它之后，同温合炉的两个重叠任务会算出 `gap < -1e-6` ⇒ **被判成违规**。
      而"同温紧邻（gap == 0）"那种写法测不出来 —— `0 < -1e-6` 恰好为假。
    """
    plan = SchedulePlan(tasks=[
        _task("A#1", 0, 20, "烤箱-1", "烤箱", 180.0),
        _task("B#1", 10, 30, "烤箱-1", "烤箱", 180.0),   # 同温合炉: 重叠 10 分钟
    ])
    assert device_switches(plan) == [], "同温合炉的重叠被误报成降温违规"


# --------------------------------------------- 三、贪心轨真的把缓冲排进去

#: seed=42 第 23 组 —— 实测天然含一次「烤箱 200℃ → 180℃」的异温背靠背。
#: 写死菜名而不是随机采样，是为了让护栏**确定性**地守住"择优门真的在起作用"。
_CD_GROUP = ["半熟芝士蛋糕", "Q弹果冻蛋羹", "金银蒜蒸鸡胸肉",
             "原味墨西哥面包", "香辣烤小龙虾"]


def _cd_group_recipes():
    names = [n for n in _CD_GROUP if n in set(all_names())]
    assert len(names) >= 3, f"官方菜库缺少测试用菜: {names}"
    return [parse_recipe(n, get(n)["ingredients"], get(n)["steps"]) for n in names]


def test_贪心轨插入降温缓冲():
    rec = _mk("烤两盘", "第1步：烤箱200度烤10分钟；第2步：烤箱180度烤10分钟")
    plan = E.schedule([rec], use_cpsat=False, use_exact=False)
    ovens = sorted((t for t in plan.tasks if t.device == "烤箱" and t.start_min >= 0),
                   key=lambda t: t.start_min)
    assert len(ovens) >= 2, f"没排出两个烤箱任务: {[(t.task_id, t.temp_c) for t in ovens]}"
    a, b = ovens[0], ovens[1]
    need = E.cooldown_minutes(a.temp_c, b.temp_c)
    assert need > 0, f"这两步应当异温: {a.temp_c} -> {b.temp_c}"
    assert b.start_min - a.end_min >= need - 1e-6, (
        f"异温背靠背没留降温: 只留 {b.start_min - a.end_min:.2f}, 需要 {need:.2f}")


def test_蒸箱轨插入降温缓冲():
    """⚠ 降温缓冲有**两份实现**，必须各测一条：

      · 烤箱按独占处理（`shared=False`）⇒ 走 `_ResourceTimeline._fit`；
      · 蒸箱是同温合炉（`shared=True`）⇒ 走 `_temp_share_fit`。

    只测烤箱那条，`_temp_share_fit` 里的降温删掉也照样全绿（实测变异漏网）。
    """
    rec = _mk("蒸两盘", "第1步：蒸箱120度蒸10分钟；第2步：蒸箱100度蒸10分钟")
    plan = E.schedule([rec], use_cpsat=False, use_exact=False)
    boxes = sorted((t for t in plan.tasks if t.device == "蒸箱" and t.start_min >= 0),
                   key=lambda t: t.start_min)
    assert len(boxes) >= 2, f"没排出两个蒸箱任务: {[(t.task_id, t.temp_c) for t in boxes]}"
    a, b = boxes[0], boxes[1]
    need = E.cooldown_minutes(a.temp_c, b.temp_c)
    assert need > 0, f"这两步应当异温: {a.temp_c} -> {b.temp_c}"
    assert b.start_min - a.end_min >= need - 1e-6, (
        f"蒸箱异温背靠背没留降温: 只留 {b.start_min - a.end_min:.2f}, 需要 {need:.2f}")


def test_择优门消除降温违规(monkeypatch):
    """真实用例: 关掉降温时这份计划**确实**违规; 预算给足后必须一条不剩。

    菜名与 seed 是实测挑出来的（seed=42 的第 23 组）—— 它天然含一次
    烤箱 200℃ → 180℃ 的背靠背。写死菜名而不是随机采样, 是为了让护栏**确定性**
    地守住"择优门真的在起作用", 而不是碰运气。
    """
    monkeypatch.setenv("FT_CD_BUDGET", "999")         # ε=∞ ⇒ 无条件合规
    recs = _cd_group_recipes()

    monkeypatch.setenv("FT_COOLDOWN", "off")
    off_plan = plan_recipes(recs, agent=True)
    monkeypatch.setenv("FT_COOLDOWN", "on")           # 用 on 档校验器量 off 档的计划
    off_viol = device_switches(off_plan)

    on_plan = plan_recipes(recs, agent=True)
    on_viol = device_switches(on_plan)
    assert on_viol == [], f"预算给足后仍有违规: {on_viol}"
    # 这条断言是护栏的"前提成立"检查: 若哪天数据变了导致 off 档也不再违规,
    # 说明这个用例已经守不住东西了 —— 与其静默失效, 不如显式报出来。
    assert off_viol, ("该用例已失效: 关掉降温也不再产生违规, 请换一个用例 "
                      "(它本来是用来证明'门真的在起作用'的)")


def test_降温门_超预算不拿分数换物理(monkeypatch):
    """⚠ 这条守的是"无界合规优先"那个坑。

    精确轨 / CP-SAT 轨的批次机里没有"降温"这一项，所以它们会给出"Γ 很好但降温违规"
    的解；贪心轨插了缓冲、合规，但 Γ 可能很差。**无界**地合规优先会拿一处降温违规
    换掉整份计划 —— 实测本组（seed=42 #23）**Γ 从 2.0 涨到 191.4（95 倍）**。
    有预算之后：预算外保留更优解，违规交给 `insight` 报红色预警。
    """
    recs = _cd_group_recipes()
    monkeypatch.setenv("FT_COOLDOWN", "off")
    base = plan_recipes(recs, agent=True)
    monkeypatch.setenv("FT_COOLDOWN", "on")
    monkeypatch.setenv("FT_CD_BUDGET", "0")
    plan = plan_recipes(recs, agent=True)
    assert plan.finish_gap_min == pytest.approx(base.finish_gap_min), (
        f"预算=0 时降温不该拿 Γ 去换合规: {plan.finish_gap_min} vs {base.finish_gap_min}")
    # 前提成立检查: 这一组本来就是"关掉降温会违规"的那组 ⇒ 预算=0 必须真的留有违规
    assert device_switches(plan), "该用例已失效: 预算=0 时没有违规可保留"


def test_prefer_cd_预算语义():
    """`prefer_cd` 的纯函数口径（ε-约束：预算内换合规解，预算外保留更优解）。"""
    class P:
        def __init__(self, g):
            self.finish_gap_min, self.makespan_min = g, 0.0

    a, ok, bad = P(2.0), P(2.0), P(9.0)
    assert E.prefer_cd(a, ok, 5.0) is ok, "同分时该选合规解（免费合规）"
    assert E.prefer_cd(a, bad, 5.0) is a, "超预算该保留更优解"
    assert E.prefer_cd(a, bad, 10.0) is bad, "预算内该换成合规解"
    assert E.prefer_cd(a, None, 5.0) is a, "没有合规解时退回原解"
    assert E.prefer_cd(None, bad, 5.0) is bad


def test_降温ε约束不能换掉满足hand_cap的解(monkeypatch):
    """⚠ 降温是**附加**精化，绝不能把 hand_cap（用户/赛题显式给的硬指标）换掉。

    `best_cd` 若只判"降温合规"，一份"降温合规但超连续手动上限"的贪心解
    就会按 Γ 把 hand_cap 达标的精确解换掉 —— 实测 hand_streak 99 > 30。
    这是"两道可行性门不能互相覆盖"在 ε-约束下的等价形态。
    """
    from app.scheduler.metrics import hand_streak

    names = [n for n in ["小碗蒸肉", "榴莲酥", "烤鱿鱼干", "大理石饼干", "茯苓饼干"]
             if n in set(all_names())]
    assert len(names) >= 3, f"官方菜库缺少测试用菜: {names}"
    recs = [parse_recipe(n, get(n)["ingredients"], get(n)["steps"]) for n in names]
    monkeypatch.setenv("FT_HAND_CAP", "30")
    monkeypatch.setenv("FT_COOLDOWN", "on")
    monkeypatch.setenv("FT_CD_BUDGET", "999")     # 预算给足 ⇒ 降温最有话语权
    plan = E.schedule(recs)
    assert hand_streak(plan) <= 30 + 1e-6, (
        f"降温的 ε-约束把 hand_cap 的保证换掉了: hand_streak={hand_streak(plan)}")


# ------------------------------------------------------------ 四、诊断模块

def test_insight_空计划不崩():
    r = I.analyze(SchedulePlan(tasks=[]))
    assert r["risks"] == [] and r["critical_path"]["task_ids"] == []
    assert r["high"] == 0 and r["mid"] == 0


def test_insight_跨度不超过总时长():
    recs = [_mk("蒸鱼", "第1步：切配；第2步：蒸箱100度蒸8分钟"),
            _mk("炒菜", "第1步：切配；第2步：锅中炒5分钟")]
    plan = E.schedule(recs)
    cp = I.critical_path(plan)
    assert cp["task_ids"], "关键路径不该为空"
    assert cp["span_min"] <= cp["makespan_min"] + 1e-6, \
        f"跨度 {cp['span_min']} 超过了总时长 {cp['makespan_min']}"


def test_insight_链上任务都存在():
    recs = [_mk("蒸鱼", "第1步：切配；第2步：蒸箱100度蒸8分钟"),
            _mk("炒菜", "第1步：切配；第2步：锅中炒5分钟")]
    plan = E.schedule(recs)
    ids = set(I.critical_path(plan)["task_ids"])
    assert ids <= {t.task_id for t in plan.tasks}


def test_insight_成环不死循环():
    """依赖成环时**必须**收敛返回, 不能挂死（关键路径是记忆化递归 + 环检测）。"""
    a = _task("A#1", 0, 5, "灶具-1", "灶具", None, deps=["B#1"])
    b = _task("B#1", 5, 10, "灶具-1", "灶具", None, deps=["A#1"])
    cp = I.critical_path(SchedulePlan(tasks=[a, b]))
    assert len(cp["task_ids"]) <= 2


def test_insight_零余量链成环立刻停():
    """`_zero_slack_chain` 的成环守卫: 畸形依赖（互为前置且时长为 0）时必须立刻停。

    ⚠ 必须**直接调内部函数**并手工造一个真正的零余量环 ——
      靠 `critical_path` 绕是造不出来的: 只要时长 > 0, "A.end == B.start 且
      B.end == A.start" 就退化成零时长, 正常排程永远碰不到这个分支。
      这个守卫防的是**外部传入/畸形**计划把关键路径算成死循环。
    """
    a = _task("A#1", 0, 0, "灶具-1", "灶具", None, deps=["B#1"])
    b = _task("B#1", 0, 0, "灶具-1", "灶具", None, deps=["A#1"])
    plan = SchedulePlan(tasks=[a, b])
    by_id = {t.task_id: t for t in plan.tasks}
    preds, _succs = I._edges(plan)
    chain = I._zero_slack_chain(plan, by_id["A#1"], preds, by_id)
    assert chain, "成环也必须给出链（不能返回空）"
    assert len(chain) <= 2, f"成环没停住: {chain}"


def test_insight_最长时长链成环不死循环():
    """`_longest_duration_chain` 的 onstack 环检测: 递归必须收敛（否则 RecursionError）。"""
    a = _task("A#1", 0, 5, "灶具-1", "灶具", None, deps=["B#1"])
    b = _task("B#1", 5, 10, "灶具-1", "灶具", None, deps=["A#1"])
    plan = SchedulePlan(tasks=[a, b])
    by_id = {t.task_id: t for t in plan.tasks}
    preds, _succs = I._edges(plan)
    chain = I._longest_duration_chain(I._live(plan), preds, by_id)
    assert chain and set(chain) <= {"A#1", "B#1"}


def test_insight_零余量链退化时退回最长链():
    """计划里留了对齐空等 ⇒ 零余量链退化成长度 1 ⇒ 必须退回最长工序链并标注口径。

    `method` 字段是给读的人用的：两种口径的 `span_min` 完全不同，
    不标注就会有人拿"最长工序链"去对"零余量链"的数字，怎么算都对不上。
    """
    plan = SchedulePlan(tasks=[
        _task("A#1", 0, 3, "灶具-1", "灶具", None, deps=[]),
        _task("A#2", 4, 10, "灶具-1", "灶具", None, deps=["A#1"]),   # 留了 1 分钟空等
    ])
    cp = I.critical_path(plan)
    assert cp["method"] == "longest_duration", f"没有退回兜底口径: {cp}"
    assert len(cp["task_ids"]) == 2, cp


def test_insight_降温违规报成high():
    plan = SchedulePlan(tasks=[
        _task("A#1", 0, 10, "烤箱-1", "烤箱", 200.0),
        _task("B#1", 10, 20, "烤箱-1", "烤箱", 180.0),
    ])
    rs = I.risks(plan)
    cool = [r for r in rs if r["kind"] == "cooldown"]
    assert cool and cool[0]["level"] == "high", f"降温违规必须报红色预警: {rs}"
    assert "降温" in cool[0]["message"]


def test_insight_风险结构合法():
    recs = [_mk("蒸鱼", "第1步：切配；第2步：蒸箱100度蒸8分钟"),
            _mk("炒菜", "第1步：切配；第2步：锅中炒5分钟")]
    r = I.analyze(E.schedule(recs))
    assert set(r) >= {"critical_path", "risks", "high", "mid", "summary"}
    for item in r["risks"]:
        assert item["level"] in ("high", "mid")
        assert set(item) >= {"level", "kind", "task_id", "dish", "message"}
        assert item["message"]
    assert r["high"] + r["mid"] == len(r["risks"])


def test_insight_不抛异常_即使任务缺字段():
    """诊断是只读旁路: 数据畸形时给 error, 绝不把异常抛给排程调用方。"""
    bad = SchedulePlan(tasks=[ScheduledTask(task_id="x#1", dish="", step_id=1,
                                            raw_text="", device="人工")])
    r = I.analyze(bad)
    assert "critical_path" in r and "risks" in r


# -------------------------------------------------- 五、接口与前端接线

def test_schedule端点带诊断字段():
    from app.api.main import ScheduleIn, do_schedule
    res = do_schedule(ScheduleIn(recipes=[{"name": "本帮白切鸡"},
                                          {"name": "栗子冰皮月饼"}], notify=False))
    ins = res.get("insight")
    assert isinstance(ins, dict), "/schedule 必须回带 insight 诊断"
    assert "critical_path" in ins and "risks" in ins


# ---------------------------------------------- 五之二、前端诊断面板（已移除）
#
# 2026-10-08：按用户要求，**排程诊断面板整体下掉**（HTML 容器 `#riskPanel`、
# `.riskpanel` 样式、`renderInsight()` 与它的调用点全部删除）。原来这里有两
# 条静态护栏：`test_前端有诊断面板且无裸模板` 与 `test_风险面板不抢甘特图的高度`，
# 以及配套的 `_js_fn_body()` 大括号配平工具 —— 随面板一起删掉了。
#
# ⚠ **后端没动**：`/schedule` 仍然回带 `insight` 字段（见上面的
#   `test_schedule端点带诊断字段`），`app/scheduler/insight.py` 也照常工作。
#   它现在是纯 API 能力，界面不再消费它。
# ⚠ 如果以后要把面板加回来，务必记住它当年踩的坑：它是 `#ganttWrap`（flex 列）
#   的兄弟节点、而 `#gantt` 是 `flex:1` —— 新块必须 `flex:none` + `max-height`，
#   否则一有内容就把甘特图挤到 35px。桌面断点下 `#v-plan` 是**固定高度**
#   （`calc(100vh - --chrome)`）、没有 `#gantt{min-height}` 兜底，所以这个挤压
#   是**静默**的：静态测试看不见，只有真 Edge 排版探针会红。
