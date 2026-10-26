"""
核心正确性测试
覆盖: 设备零冲突 / 依赖不违反 / 重排最小扰动 / 解析抽取 /
     Agent 闭环 / CP-SAT 单元分配 / 通知可操作性 / 官方接口 schema

运行:
    python -m pytest tests/ -v
    python tests/test_core.py          # 不装 pytest 也能跑

说明: 测试全程 FT_LLM=off, 不依赖网络, 保证离线可重复。
"""
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["FT_LLM"] = "off"          # 测试离线路径, 不触发任何模型调用
os.environ["FT_AGENT_LLM"] = "off"

from app.parser.recipe_parser import parse_recipe
from app.scheduler.engine import schedule, reschedule
from app.scheduler.metrics import evaluate, perturbation, hand_streak
from app.scheduler.checker import check_plan
from app.notifier.notifications import generate_notifications
from app.agent.priors import get_priors
from app.agent.objective import shape_policy


def _mk(name, steps):
    return parse_recipe(name, "", steps)


# ---------------- 原有基础用例 ----------------
def test_no_device_conflict():
    recs = [
        _mk("蒸鱼", "第1步：洗净；第2步：放入蒸箱普通蒸100度8分钟"),
        _mk("炒菜", "第1步：切菜；第2步：锅中放油炒5分钟"),
        _mk("炖肉", "第1步：焯水；第2步：小火炖30分钟"),
    ]
    plan = schedule(recs)
    kpi = evaluate(plan)
    assert kpi.conflict_count == 0, f"存在设备冲突: {kpi.conflict_count}"


def test_no_dependency_violation():
    recs = [_mk("汤", "第1步：烧水；第2步：放入食材煮20分钟；第3步：加盐出锅")]
    plan = schedule(recs)
    kpi = evaluate(plan)
    assert kpi.depend_violation == 0, f"存在依赖违反: {kpi.depend_violation}"


def test_finish_gap_reasonable():
    recs = [
        _mk("蒸鱼", "第1步：腌制10分钟；第2步：蒸箱蒸8分钟"),
        _mk("炒菜", "第1步：切配；第2步：炒5分钟"),
    ]
    plan = schedule(recs)
    assert plan.finish_gap_min <= 15, f"出锅差过大: {plan.finish_gap_min}"


def test_reschedule_keeps_completed():
    """动态重排: 已完成/已开始步骤时间必须一字不动 (最小扰动原则)。"""
    recs = [
        _mk("蒸鱼", "第1步：洗净；第2步：腌制10分钟；第3步：蒸8分钟"),
        _mk("炒菜", "第1步：切菜；第2步：炒5分钟"),
    ]
    base = schedule(recs)
    fixed = {}
    for t in base.tasks:
        if t.step_id == 1:   # 第一步已完成
            fixed[(t.dish, t.step_id)] = t
    new_dish = _mk("汤", "第1步：烧水；第2步：煮15分钟")
    new_plan = reschedule(recs, fixed, new_dish=new_dish)
    kpi = evaluate(new_plan)
    assert kpi.conflict_count == 0, f"重排后出现设备冲突: {kpi.conflict_count}"
    assert kpi.depend_violation == 0, f"重排后出现依赖违反: {kpi.depend_violation}"
    for key, st in fixed.items():
        match = [t for t in new_plan.tasks if t.task_id == st.task_id]
        assert match, f"冻结任务丢失: {st.task_id}"
        assert match[0].start_min == st.start_min, \
            f"冻结任务被挪动: {st.task_id} {st.start_min} -> {match[0].start_min}"


def test_parser_extracts_time_device():
    r = _mk("x", "第1步：放入蒸箱普通蒸100度8分钟")
    s = r.steps[0]
    assert s.device == "蒸箱"
    assert s.duration_min == 8.0
    assert s.temperature_c == 100.0


def test_parser_compound_split():
    r = _mk("x", "第1步：加热10分钟；再设置：熬煮30分钟")
    timed = [s for s in r.steps if s.duration_min]
    assert len(timed) == 2, f"复合句未拆开: {[s.duration_min for s in r.steps]}"


# ---------------- v2.1 回归用例 ----------------
def test_soak_is_wait_not_steam():
    """浸泡/泡发是等待, 不能被兜底推断成蒸箱加热 (否则4小时泡米变240分钟蒸制)。"""
    r = _mk("糯米烧麦", "第1步：提前把糯米温水浸泡4小时；第2步：放入蒸箱蒸40分钟")
    s = r.steps[0]
    assert s.device == "人工", f"浸泡被误判为设备: {s.device}"
    assert s.unattended is True
    assert s.duration_min == 240.0
    assert r.steps[1].device == "蒸箱"


def test_soak_word_does_not_erase_a_real_device_task():
    """v2.4 回归: 复合句里的"晾凉/冷却/解冻"是收尾, 不能把整句真实设备作业一起抹掉。

    官方 100 道菜里有 12 句这种复合句, 其中 6 句形如
    "…放入烤箱第3层, 选择'开始烹饪'按键…烘烤结束, 取出晾凉" ——
    旧实现整句降级成 WAIT, 该菜直接丢掉主烹饪步 (法式羊排全菜只有 1 步, 被抹掉后
    整道菜没有任何设备任务)。反过来, 没有设备信号的"温水浸泡4小时"必须仍然是等待。
    """
    # ① 带设备程序启动信号 ⇒ 保持设备任务
    r = _mk("黑森林蛋糕", "第1步：将烤盘放入烤箱第二层，选择“开始烹饪”按键，按屏幕提示操作。"
                          "烘烤结束，取出模具倒扣在烤架上，充分晾凉")
    assert r.steps[0].device == "烤箱", f"真实烤箱作业被等待词抹掉: {r.steps[0].device}"
    assert r.steps[0].prep_type != "等待"

    # ② 点名设备 + 显式时长 ⇒ 保持设备任务
    r2 = _mk("枣泥糕", "第1步：干红枣温水浸泡1小时。将浸泡好的红枣放入蒸箱，设置100℃，蒸制30分钟")
    assert r2.steps[0].device == "蒸箱", f"带设备与时长的句子被误降级: {r2.steps[0].device}"

    # ③ 灶具 + 主动烹饪动作 ⇒ 保持设备任务
    r3 = _mk("麻辣对虾", "第1步：将炒锅加热，倒入食用油、姜末、蒜末，小火煸炒至香辣味出现，"
                          "盛于小碗中，待冷却后加入料酒")
    assert r3.steps[0].device == "灶具", f"灶前手上活被冷却词抹掉: {r3.steps[0].device}"

    # ④ 反例保护: 没有设备信号的"浸泡"必须仍然是等待
    r4 = _mk("糯米烧麦", "第1步：提前把糯米温水浸泡4小时")
    assert r4.steps[0].device == "人工" and r4.steps[0].prep_type == "等待"
    assert r4.steps[0].duration_min == 240.0

    # ⑤ 反例保护: 烤箱/蒸箱的纯收尾句("烘烤结束后取出晾凉")仍是等待
    r5 = _mk("玛格丽特", "第1步：烘烤结束后，取出晾凉即可食用")
    assert r5.steps[0].prep_type in ("等待", "装盘"), f"纯收尾句未被降级: {r5.steps[0].prep_type}"


def test_official_library_every_dish_has_a_heating_step():
    """官方 100 道菜里, 除"披萨面团"(只做面团, 本身不含烹饪)外都应解析出加热设备。"""
    from app.spec.adapter import get as _get, all_names as _names
    from app.models.schema import DEVICE_NONE as _NONE
    HEAT = ("灶具", "蒸箱", "烤箱")
    missing = []
    for nm in _names():
        r = parse_recipe(nm, _get(nm)["ingredients"], _get(nm)["steps"])
        if not any(s.device in HEAT for s in r.steps):
            missing.append(nm)
    assert missing == ["披萨面团"], f"这些菜解析不出任何加热设备: {missing}"


def test_prep_ahead_keeps_dependency():
    """提前准备的集合必须是"前置链完整"的, 否则会把后续步骤挪到它前置之前。"""
    recs = [_mk("A", "第1步：蒸箱蒸30分钟；第2步：放入冰箱冷藏2小时；第3步：装盘")]
    plan = schedule(recs)
    kpi = evaluate(plan)
    assert kpi.depend_violation == 0, "提前准备破坏了依赖"
    assert kpi.conflict_count == 0


def test_cpsat_unit_assignment_no_overlap():
    """CP-SAT 输出的单元号必须与真实占用一致 (按开始时间装箱, 不是按字典序发号)。"""
    from app.scheduler.cpsat import solve
    recs = [
        _mk("蒸鱼", "第1步：切配；第2步：蒸箱蒸10分钟"),
        _mk("烤鸡", "第1步：腌制10分钟；第2步：烤箱烤25分钟；第3步：装盘"),
        _mk("炒菜", "第1步：切菜；第2步：炒8分钟"),
    ]
    cp = solve(recs, time_budget=1.0)
    if cp is None:
        return   # 环境无 ortools 时跳过
    kpi = evaluate(cp)
    assert kpi.conflict_count == 0, f"CP-SAT 结果存在冲突: {kpi.conflict_count}"
    # 零长区间回归: 所有任务时长至少 1 分钟
    assert all(t.duration_min >= 1.0 for t in cp.tasks), "出现零长任务区间"


def test_agent_loop_returns_valid_plan():
    """Agent 闭环: 有决策轨迹、零冲突、零依赖违反。"""
    from app.agent.orchestrator import run
    recs = [
        _mk("蒸鱼", "第1步：洗净；第2步：腌制10分钟；第3步：蒸箱蒸8分钟"),
        _mk("炒菜", "第1步：切菜；第2步：炒5分钟"),
        _mk("汤", "第1步：烧水；第2步：炖30分钟"),
    ]
    res = run(recs, use_llm=False)
    assert res.trace, "闭环没有产生任何决策轨迹"
    kpi = evaluate(res.plan)
    assert kpi.conflict_count == 0 and kpi.depend_violation == 0
    assert res.explanation, "缺少排程解释"


def test_policy_shaping_from_text():
    """目标塑形: 用户说"一起上桌"应该显著提高同出锅权重。"""
    base = shape_policy("", 3)
    feast = shape_policy("请客, 要一起上桌", 3)
    assert feast.w_gap > base.w_gap, "同出锅权重没有因用户偏好而提高"
    rush = shape_policy("我赶时间, 半小时搞定", 3)
    assert rush.w_makespan > base.w_makespan


def test_priors_rule_fallback():
    """断网时先验退化为规则, 但字段必须完整可用。"""
    recs = [_mk("清蒸鱼", "第1步：切配；第2步：蒸箱蒸10分钟")]
    p = get_priors(recs, use_llm=False)[("清蒸鱼")]
    assert 0.0 <= p.serve_hot <= 1.0
    assert p.hold_min > 0
    assert p.source in ("rule", "llm-cache")


# --------------------------------------------------------------------------
# 主导工序先验 (2026-10-04): 旧实现把 **72/100** 道菜判成 fry
#
# 旧判据 = "步骤里出现任意一个 灶具/烤箱 的炒烤步 ⇒ fry", 而 `_FRY` 又收了"烤|酥"。
# 后果(全部静默, 只是让保热语义失真):
#   · 雪媚娘(#1「炒锅慢火炒手粉」) / 黑森林蛋糕 / 糖霜饼干 ⇒ 冰皮冷甜品与烘焙甜点
#     全被判成"6 分钟内必须吃掉";
#   · 清蒸鲈鱼(#5「烧热油淋在鱼身上」) ⇒ 蒸菜被一个收尾步盖过蒸箱;
#   · 竹荪排骨汤 ⇒ 汤被判成爆炒(hold 45 → 6)。
# 新判据按"**上桌时的状态**": ① 末道设备工序是冷藏 ⇒ cold; ② 菜名强信号; ③ 主导设备。
# --------------------------------------------------------------------------
def _kind_of_named(name: str) -> str:
    """按官方菜谱原文取主导工序 (用真实 ingredients + steps, 不手写步骤)。"""
    from app.agent.priors import _kind_of
    from app.spec.adapter import get
    r = get(name)
    return _kind_of(parse_recipe(name, r["ingredients"], r["steps"]))


def test_kind_ignores_incidental_prep_step_for_cold_dessert():
    """冷甜品里的"炒手粉"是辅料处理, 不能把整道菜判成爆炒。"""
    from app.agent.priors import _kind_of
    r = _mk("雪媚娘",
            "第1步：取糯米粉用干净炒锅慢火炒至微黄色，取出备用；"
            "第2步：粉浆入蒸箱普通蒸15分钟；"
            "第3步：面团封保鲜膜入冰箱冷藏；"
            "第4步：包好后即可食用，冷藏后口感更好")
    assert _kind_of(r) == "cold", f"冷甜品被判成 {_kind_of(r)}"


def test_kind_ignores_finishing_fry_step_for_steamed_dish():
    """蒸菜最后"烧热油淋在鱼身上"是收尾步, 不能盖过蒸箱。"""
    from app.agent.priors import _kind_of
    r = _mk("清蒸鲈鱼",
            "第1步：鲈鱼改刀；第2步：入蒸箱普通蒸100度8分钟；"
            "第3步：炒锅烧热油淋在鱼身上，趁热食用")
    assert _kind_of(r) == "steam", f"蒸菜被判成 {_kind_of(r)}"


def test_kind_fridge_is_cold_only_when_it_is_the_serving_step():
    """冷藏定型 ≠ 冷食: 后面还有烘烤的"冷藏醒面"不许判 cold。"""
    from app.agent.priors import _kind_of
    chilled_dough = _mk("黄油饼干",
                        "第1步：面团放冰箱冷冻20分钟定型；"
                        "第2步：生坯放入烤箱，180度烤15分钟；"
                        "第3步：取出放凉")
    assert _kind_of(chilled_dough) == "bake", "冷藏醒面被误判成冷食"
    chilled_dish = _mk("红豆奶冻卷",
                       "第1步：糊倒入模具；第2步：放入冰箱冷藏4小时即可食用")
    assert _kind_of(chilled_dish) == "cold", "冷藏成型的甜品没判成冷食"


def test_kind_fridge_with_explicit_serve_hot_is_not_cold():
    """原文同时给了"趁热"时不许判冷食。"""
    from app.agent.priors import _kind_of
    r = _mk("银耳羹", "第1步：入蒸箱蒸30分钟；第2步：取出撒枸杞，趁热或冷藏后食用")
    assert _kind_of(r) == "soup", f"说了趁热还被判成 {_kind_of(r)}"


def test_kind_bake_is_a_real_kind_with_longer_hold():
    """烘焙必须与炒炸分开: 甜点"6 分钟内必须吃掉"是错的。"""
    from app.agent.priors import _kind_of, _HOLD_BY_KIND, _SERVE_HOT_BY_KIND
    r = _mk("黄油小饼干", "第1步：和面；第2步：烤盘放入烤箱，170度烤13分钟")
    assert _kind_of(r) == "bake", f"烘焙被判成 {_kind_of(r)}"
    assert _HOLD_BY_KIND["bake"] > _HOLD_BY_KIND["fry"], "烘焙的保热时长没比炒炸宽松"
    assert _SERVE_HOT_BY_KIND["bake"] < _SERVE_HOT_BY_KIND["fry"]


def test_kind_hold_order_matches_culinary_reality():
    """保热语义序: 可放时长 凉菜 > 汤 > 烘焙 > 蒸 > 炒炸; 趁热程度反之。"""
    from app.agent.priors import _HOLD_BY_KIND as h, _SERVE_HOT_BY_KIND as s
    assert h["cold"] > h["soup"] > h["bake"] > h["steam"] > h["fry"]
    assert s["fry"] > s["steam"] > s["bake"] > s["soup"] > s["cold"]


def test_kind_of_official_corpus_is_not_degenerate():
    """官方 100 道菜的主导工序不许再退化成"全是 fry"。

    旧实现: fry 72 / steam 18 / cold 3 / other 7 —— 单类占 72%, 这个先验等于没信息量。
    这里只钉宽松上界 + 几个"上桌状态确定"的锚点菜, 不锁具体分布。
    """
    from collections import Counter
    from app.spec.adapter import all_names
    kinds = Counter(_kind_of_named(n) for n in all_names())
    total = sum(kinds.values())
    assert kinds["fry"] < total * 0.4, f"主导工序又退化了: {dict(kinds)}"
    for k in ("bake", "steam", "cold", "soup"):
        assert kinds[k] > 0, f"{k} 一类都没有: {dict(kinds)}"
    for nm, want in (("雪媚娘", "cold"), ("黑森林蛋糕", "cold"), ("清蒸鲈鱼", "steam"),
                     ("竹荪排骨汤", "soup"), ("糖霜饼干", "bake"), ("红豆糯米糕", "bake")):
        got = _kind_of_named(nm)
        assert got == want, f"{nm} 判成 {got}, 应为 {want}"



def test_notifications_actionable():
    """通知必须 精准(带菜名+动作) / 简洁(一句话) / 可操作。"""
    recs = [
        _mk("红烧排骨", "第1步：切块；第2步：焯水5分钟；第3步：小火炖30分钟；第4步：装盘"),
        _mk("清蒸鲈鱼", "第1步：改刀；第2步：蒸箱蒸8分钟"),
    ]
    plan = schedule(recs)
    notes = generate_notifications(plan, priors=get_priors(recs, use_llm=False),
                                   use_llm=False)
    assert notes, "没有生成任何通知"
    for n in notes:
        assert 0 < len(n["text"]) <= 60, f"通知文案过长: {n['text']}"
        assert n["at_min"] >= 0
    # 至少有一条提到菜名
    assert any(any(d in n["text"] for d in ("红烧排骨", "清蒸鲈鱼")) for n in notes)


def test_official_recipes_count_100():
    """官方菜谱 100 道, 重名菜品不能因字典去重而丢失。"""
    from app.spec.adapter import all_names, get
    names = all_names()
    assert len(names) == 100, f"菜谱数应为 100, 实际 {len(names)}"
    assert all(get(n) for n in names), "存在取不到内容的菜名"


def test_build_plan_schema():
    """官方评分接口: 字段齐备, detailTimeline 按 type 升序。"""
    from app.spec.adapter import all_names, build_plan
    names = all_names()
    out = build_plan([{"id": 1, "name": names[0]}, {"id": 2, "name": names[1]}])
    for k in ("overview", "cookingTimeline", "ingredientsSummary",
              "detailTimeline", "recipeDetail"):
        assert k in out, f"缺少字段 {k}"
    assert out["overview"]["recipeCount"] == 2
    types = [r["type"] for r in out["detailTimeline"]]
    assert types == sorted(types), "detailTimeline 未按 type 升序"
    assert out["recipeDetail"][0]["cookingSteps"], "recipeDetail 缺少步骤"


def test_no_negative_preheat():
    """预热节点不能落到负时刻 (输出里出现 -5-0 这种区间)。

    ⚠ 只约束**预热节点**。真正的"提前准备"(备菜/腌制/浸泡)本来就该在 t<0 ——
    见 `engine._to_plan` 的口径(`start_min < 0` = 提前准备)与
    `engine._rebase_to_first_resource`。原实现写成 `all(t.start_min >= 0)`,
    把两者混为一谈: 它在 2026-10-06 的"锚点修复"(让第一个**占用资源**的任务
    从 t=0 开始)之后当场变红 —— 修复本身是对的, 红的是这条断言太宽。
    """
    recs = [_mk("烤鸡", "第1步：腌制10分钟；第2步：烤箱烤25分钟")]
    plan = schedule(recs)
    bad = [t.task_id for t in plan.tasks
           if t.start_min < 0 and t.raw_text in ("蒸箱预热", "烤箱预热")]
    assert not bad, f"预热落到负时刻: {bad}"


def test_negative_axis_holds_only_non_resource_tasks():
    """t<0 只允许"等待/提前准备"; 且第一个**占用资源**的任务必须正好在 t=0。

    为什么 (2026-10-06 锚点修复): `_rebase_to_first_resource` 会把时间轴整体
    左移到"第一个资源任务从 0 开始", 于是不占资源的备菜与腌制落到负轴。
    若哪天平移算错、把灶具/蒸箱/烤箱/双手的任务也推成负数, 甘特图上就会出现
    "昨天就在炒菜"这种荒谬区间 —— 这条把它钉死。

    修复前的现场(同一个菜谱): 腌制 0→10、烤箱 10→35, 前 10 分钟烤箱全空;
    修复后: 腌制 -10→0、烤箱 0→25。
    """
    recs = [_mk("烤鸡", "第1步：腌制10分钟；第2步：烤箱烤25分钟")]
    plan = schedule(recs)
    bad = [t.task_id for t in plan.tasks
           if t.start_min < 0 and t.unit_id not in ("等待", "提前准备")]
    assert not bad, f"资源任务落到负轴: {bad}"
    res = [t for t in plan.tasks if t.unit_id not in ("等待", "提前准备")]
    assert res, "计划里没有任何占用资源的任务"
    assert min(t.start_min for t in res) == 0.0, \
        f"第一个资源任务不在 t=0: {min(t.start_min for t in res)}"


def test_no_resource_task_before_day_start():
    """**非备菜**任务不得早于 t=0 —— 上面那条测不到的缺口 (2026-10-06 抽查发现)。

    为什么上面那条测不出来: 它用的合成菜「腌制10分钟 + 烤箱25分钟」里, 腌制结束
    恰好就是 0, 所以第一个资源任务天然在 0。真实菜谱里却有另一种形态:
    备菜步整体右对齐到 0 (`_build_tasks`: cur=-sum(dur)) ⇒ 备菜段的 end_min 是
    **负数**; 若某条烹饪步的前置只是一个**早早结束的备菜步**(而不是最后一条备菜),
    那么 `dep_ready` 就是负数, 贪心会把这条**占设备/占人工**的步骤排到 t<0。

    后果不止显示: `_to_plan` 判"当天烹饪"的口径是 `start_min >= 0`,
    于是这条真实烹饪步**既不计入 T/Γ, 也不进 `check_plan`**(checker 只查 start>=0)。
    实测官方 100 道里 8 道中招:
      低温牛排#2 蒸箱 -2→6 / 芋头蛋卷#2 蒸箱 -2→6 / 血糯米仙草奶茶#8 蒸箱 -2→6 /
      麻辣对虾#4 双手 -15→-13 / 金龙吐瑞#4 双手 -15→-13 /
      轻松一锅蒸#3 双手 -10→-8 / 金银蒜蒸鸡胸肉#4 双手 -60→-58 /
      油醋汁剁肉饼蒸和乐蟹#6 蒸箱 -2→3

    修复: 贪心的 `dep_ready` 对**非备菜**任务夹到 >= 0 (与精确模型
    `NewIntVar(0, horizon)` 同口径)。修复后 100 道扫描该计数归零,
    且 bench 50 基线不动 (总时长 62.9 / 出锅差中位 2.0 / 冲突 0)。

    ⚠ 必须走 `adapter.plan_recipes` (Agent 闭环 = 线上/API 的真实路径)。
      裸调 `engine.schedule([rec])` **复现不出来** —— 它走的是另一组策略与
      `allow_preprocess` 默认值, 产出的备菜形态不同 (实测该路径下蒸箱恰好在 0)。
      护栏一旦挂错入口就会永远绿, 这是这个 bug 第一次没被测出来的原因。
    """
    from app.spec.adapter import get, plan_recipes
    cases = ["低温牛排", "金银蒜蒸鸡胸肉", "芋头蛋卷"]
    for name in cases:
        d = get(name)
        assert d is not None, f"官方菜谱缺失: {name}"
        plan = plan_recipes([parse_recipe(name, d["ingredients"], d["steps"])])
        bad = [f"{t.task_id}({t.unit_id} {t.start_min}→{t.end_min})"
               for t in plan.tasks
               if t.unit_id not in ("等待", "提前准备") and t.start_min < 0]
        assert not bad, f"{name}: 资源任务落到 t<0 -> {bad}"


def test_device_failure_degrades_instead_of_crashing():
    """
    设备故障(单元数置 0)时不能崩: 候选槽是空集, 原实现直接 TypeError -> HTTP 500。
    现在的降级链是 顶替设备(蒸箱/烤箱 -> 灶具) -> 人工。
    """
    import app.scheduler.engine as eng
    from app.parser.recipe_parser import parse_recipe
    from app.spec.adapter import get
    recs = [parse_recipe(n, get(n)["ingredients"], get(n)["steps"])
            for n in ("清蒸鲈鱼", "鸡翅包饭")]
    saved = eng.RESOURCE_UNITS["蒸箱"]
    try:
        eng.RESOURCE_UNITS["蒸箱"] = 0
        plan = eng.schedule(recs, use_cpsat=False)
        assert plan.tasks, "设备故障后排程为空"
        assert not any(t.unit_id.startswith("蒸箱") for t in plan.tasks), \
            "蒸箱已故障, 计划里却仍占用蒸箱"
    finally:
        eng.RESOURCE_UNITS["蒸箱"] = saved


def test_agent_loop_respects_frozen_tasks():
    """闭环带冻结集求解时, 已完成步骤必须停在原位 (不能被'择优'挪走)。"""
    from app.agent.orchestrator import run
    recs = [_mk("A", "第1步：切菜5分钟；第2步：蒸箱蒸10分钟；第3步：装盘2分钟")]
    base = schedule(recs)
    frozen = {(t.dish, t.step_id): t for t in base.tasks if t.step_id == 1}
    want = base.tasks[0].start_min
    res = run(recs, hard_frozen=frozen, use_cpsat=False, time_budget=2.0)
    got = [t for t in res.plan.tasks if t.step_id == 1]
    assert got, "冻结任务丢失"
    assert abs(got[0].start_min - want) < 1e-6, \
        f"冻结任务被挪动: {want} -> {got[0].start_min}"


def test_agent_plan_falls_back_to_rules_without_leaking_errors():
    """
    自然语言选菜: 模型不可用时必须仍能选出菜并给出排程。
    原实现把 "HTTP Error 429" 这类内部错误原文直接返回客户端, 且 picked 为空。
    """
    from app.api.main import agent_plan, AgentPlanIn
    out = agent_plan(AgentPlanIn(text="来两个蒸菜和一道汤"))
    assert "error" not in out, f"内部错误原文外泄: {out.get('error')}"
    assert out["picked"], "模型不可用时没能选出任何菜"
    assert out["plan"] is not None, "选到菜却没有产出排程"
    assert out["source"] in ("rule", "llm")


def test_recipe_category_matches_dish_name():
    """
    分类必须是可信的: 前端按它做筛选。
    原实现把步骤全文纳入匹配, 步骤里的"汤汁/蒸箱"噪声会把蒸鱼判成汤品、把派判成蒸菜。
    """
    from app.spec.adapter import _category_of
    assert _category_of("豉汁蒸草鱼", "烹饪结束后倒掉盘内汤汁") == "蒸箱料理"
    assert _category_of("苹果芒果派", "放入蒸箱") == "烤箱烘焙"
    assert _category_of("清蒸鲈鱼", "") == "蒸箱料理"


def test_hand_streak_metric():
    recs = [_mk("A", "第1步：切菜5分钟；第2步：切肉5分钟")]
    plan = schedule(recs)
    assert hand_streak(plan) >= 0


# ---------------- 收尾/告警/通知回归 ----------------
def test_finish_step_not_treated_as_heating():
    """
    "再浇上蒸鱼豉油即可" 含"蒸"字(来自调料名"蒸鱼豉油")。
    若被兜底推断成蒸箱 18 分钟, 会虚增总时长并独占蒸箱 —— 必须按人工收尾处理。
    """
    from app.models.schema import DEVICE_NONE
    r = _mk("家常鲈鱼", "第1步：放入蒸箱蒸8分钟；第2步：再浇上蒸鱼豉油即可")
    last = r.steps[-1]
    assert last.device == DEVICE_NONE, \
        f"收尾步骤被误判成设备任务: {last.device}"
    assert last.needs_human and not last.unattended, "收尾应是人工步骤"


def test_load_and_start_step_is_unattended():
    """
    装载/启动型加热步骤不能整段占双手。

    官方菜谱写 "放入方太智能烹饪设备第2层，开启电源，取出水箱加满水，
    选择'开始烹饪'按键，按屏幕提示操作" —— 人工只做装载与按键(约1分钟),
    之后蒸箱按程序运行 15 分钟。旧逻辑因句子不含"蒸/烤"等动词,
    把整段 15 分钟记成人工占用, 既堵死双手又虚增"连续手动操作时长"。
    """
    r = _mk("酿苦瓜", "第1步：切配；第2步：放入方太智能烹饪设备第2层，开启电源，"
                      "取出水箱加满水，选择“开始烹饪”按键，按屏幕提示操作")
    st = r.steps[-1]
    assert st.device == "蒸箱", f"设备应识别为蒸箱: {st.device}"
    assert st.unattended is True, "装载/启动型步骤应为无人值守"
    assert st.needs_human is False, "装载/启动型步骤不应整段占双手"


def test_hands_on_step_not_treated_as_unattended():
    """
    反向守卫: 装载句里夹着"炒/翻拌"这类必须守在灶前的动作时, 仍按人工处理。
    实例: "烹香酷炒汇"的套餐步 —— 启动后还要"预热结束后放入…取出菜品…翻拌均匀"。
    """
    r = _mk("烹香酷炒汇", "第1步：打开设备，选择“开始烹饪”按键，按屏幕提示操作。"
                          "预热结束后，将黄金炒饭放入机器第1层。"
                          "待屏幕提示后，取出菜品，黄金炒饭翻拌均匀")
    st = r.steps[-1]
    assert st.needs_human is True, "含持续动手动作的步骤不能判为无人值守"


def test_official_load_steps_do_not_lock_hands():
    """
    端到端回归: 官方 100 道菜里"装载/启动型"蒸箱烤箱步骤, 必须全部不占双手。
    (修前 57 步占双手, 累计锁死 618 分钟人工时间线)

    例外: 句子里夹着"炒/翻拌"等持续动手动作的混合步骤(如"烹香酷炒汇"的套餐步),
    按 HANDS_ON_VERBS 守卫保留人工判定 —— 这类不在本用例的覆盖范围内。
    """
    from app.parser.recipe_parser import HANDS_ON_VERBS
    from app.spec.adapter import get, all_names
    locked, guarded = [], 0
    for nm in all_names():
        r = get(nm)
        if not r:
            continue
        for st in parse_recipe(nm, r["ingredients"], r["steps"]).steps:
            if st.device not in ("蒸箱", "烤箱") or not st.needs_human:
                continue
            if "开始烹饪" not in st.raw_text:
                continue
            if HANDS_ON_VERBS.search(st.raw_text):
                guarded += 1
                continue
            locked.append(f"{nm}#{st.step_id}")
    assert not locked, f"这些装载/启动型步骤仍占双手: {locked[:8]}"
    assert guarded <= 2, f"守卫拦下的步骤过多, 规则可能失效: {guarded}"


def test_steamer_temp_sort_none_safe():
    """
    回归: 蒸箱同温共享的占用表按 (起,止) 排序时不能把 temp 当排序键。
    旧实现用裸 sorted(occ), 当两个蒸箱区间起止完全相同、温度一个 None 一个数值时,
    元组比较落到第三项 -> "float < None" 抛 TypeError, 排程整体崩掉(HTTP 500)。
    触发组合: 轻松一锅蒸 + 家常鲈鱼 + 蒸咸蛋酿苦瓜 + 银鱼蒸蛋。
    """
    from app.spec.adapter import get
    names = ["轻松一锅蒸", "家常鲈鱼", "蒸咸蛋酿苦瓜", "银鱼蒸蛋"]
    recs = []
    for nm in names:
        r = get(nm)
        if not r:
            continue
        recs.append(parse_recipe(nm, r["ingredients"], r["steps"]))
    if len(recs) < 2:
        return
    plan = schedule(recs)          # 旧实现此处抛 TypeError
    kpi = evaluate(plan)
    assert kpi.conflict_count == 0, f"冲突: {kpi.conflict_count}"
    assert kpi.depend_violation == 0, f"依赖违反: {kpi.depend_violation}"


def test_finish_step_is_plate_not_long_device_task():
    """
    收尾/上桌句不能变成"长时设备任务"。

    "烹饪结束后取出即可" / "开箱门，取出烤好的饼干即可" / "尽情享用美味的X吧"
    这类句子没有时长, 旧逻辑会按菜名兜底推成"烤箱 25 分钟" ——
    既白占单台烤箱, 又让**末步时长成为出锅差的下界**。
    实测官方 100 道菜里 22 条这种句子被推成 >=8 分钟设备任务(虚占 549 分钟),
    其中 15 条正好是菜品末步。
    """
    from app.models.schema import PLATE
    from app.scheduler.engine import estimate_duration
    r = _mk("鸡仔饼", "第1步：揉面；第2步：烹饪结束后取出即可")
    st = r.steps[-1]
    assert st.device == "人工", f"收尾句被推成设备任务: {st.device}"
    assert st.prep_type == PLATE, f"收尾句工序类型应为上桌: {st.prep_type}"
    assert st.needs_human and not st.unattended
    assert estimate_duration(st) == 1.0, "收尾动作应估为 1 分钟, 不是切配的 2 分钟"


def test_step_verb_beats_dish_name_in_fallback():
    """
    设备兜底推断必须"动词优先、菜名兜底"。

    "栗子冰皮月饼"的菜名里有"饼"字, 旧实现把菜名和步骤拼成一个 blob 一起匹配,
    于是该菜**每一步**都被推成"烤箱 25 分钟" —— 连"倒入色拉油炒"也是。
    步骤文本里有明确烹饪动词时, 必须按动词定设备。
    """
    r = _mk("栗子冰皮月饼",
            "第1步：倒入适量色拉油炒，水分炒干，加入适量白糖，翻炒均匀后就可以做馅了")
    st = r.steps[0]
    assert st.device == "灶具", f"含「炒」的步骤应按动词判为灶具, 实际: {st.device}"
    assert (st.duration_min or 0) < 25.0, "不应继承菜名兜底的烤箱 25 分钟"


def test_official_close_steps_are_not_device_tasks():
    """
    端到端回归: 官方 100 道菜里"收尾/上桌句"不得再被当成 >=8 分钟的设备任务。
    例外: 含设备程序启动信号的装载步(如"…开启电源…取出水箱加满水…")是正经设备任务。
    """
    from app.parser.recipe_parser import FINISH_RE, LOAD_START_VERBS, parse_duration
    from app.spec.adapter import get, all_names
    bad = []
    for nm in all_names():
        r = get(nm)
        if not r:
            continue
        for st in parse_recipe(nm, r["ingredients"], r["steps"]).steps:
            if st.device not in ("蒸箱", "烤箱"):
                continue
            if not (st.duration_min and st.duration_min >= 8):
                continue
            if LOAD_START_VERBS.search(st.raw_text):
                continue
            # v3.0 收紧口径: 原文**自己写了时长**的一律是真实的设备作业, 不是收尾句。
            # 例: "…选择普通蒸模式，100度蒸制40分钟；使其成熟。取出放凉" —— 末尾
            # "取出放凉"命中 FINISH_RE, 但这确实是一次 40 分钟的蒸制。旧口径会把
            # 这类句子算进 bad, 于是"设备/模式识别变准(新增 普通蒸/常规烘焙 别名)"
            # 反而被判成回归。真正的误判是**时长靠 estimate_duration 兜底**的那些:
            # 收尾句本身不含时长, 被补成 8 分钟设备占用才是要防的事。
            if parse_duration(st.raw_text) is not None:
                continue
            if FINISH_RE.search(st.raw_text):
                bad.append(f"{nm}#{st.step_id}({st.device}{st.duration_min}min)")
    assert not bad, f"仍有收尾句被当成长时设备任务: {bad[:8]}"


def test_cpsat_uses_same_duration_estimate_as_greedy():
    """
    双轨一致性回归: CP-SAT 轨与贪心轨必须共用 estimate_duration()。

    旧实现里 cpsat.py 写的是 `float(st.duration_min or 5)` —— 所有没写时长的步骤
    一律按 5 分钟算, 而贪心轨用 estimate_duration()(收尾 1 分钟 / 切配 2 分钟 /
    设备 8 分钟)。后果: CP-SAT 交出的排程里**每个末步都是 5 分钟**,
    5 道菜的人工收尾串行就把出锅差顶到 20 分钟; 而 engine.schedule 只按
    `cp.finish_gap_min <= greedy.finish_gap_min + 1` 择优, 于是这份
    "看着齐、其实慢"的解会胜出。实测修前 <5min 占比 58%, 修后 82%。
    """
    from app.scheduler.cpsat import solve
    from app.scheduler.engine import estimate_duration, schedule
    recs = [_mk("测试菜", "第1步：切菜；第2步：蒸箱蒸10分钟；第3步：撒上葱花即可食用")]
    cp = solve(recs, time_budget=1.0)
    if cp is None:
        return                      # 环境无 ortools 时跳过
    got = {(t.dish, t.step_id): t.duration_min for t in cp.tasks}
    for st in recs[0].steps:
        want = max(1.0, float(int(estimate_duration(st) + 0.999)))
        assert got[("测试菜", st.step_id)] == want, (
            f"步骤{st.step_id} 在 CP-SAT 轨估成 {got[('测试菜', st.step_id)]} 分钟, "
            f"贪心轨是 {want} 分钟 —— 两条轨的任务集不一致")
    # 贪心轨也应为同一套时长
    g = {(t.dish, t.step_id): t.duration_min for t in schedule(recs, use_cpsat=False).tasks}
    assert g[("测试菜", 3)] == 1.0, f"收尾步贪心轨时长异常: {g[('测试菜', 3)]}"


def test_official_api_spec_conformance():
    """
    官方接口规范合规回归 (2026_09_26_recipes_100/智能烹饪调度Agent接口规范.md)。

    逐条校验第 5 节「格式及校验要求」1~16 与 4.5.x 字段约束。曾经踩过的坑:
      - 要求6  `timeInterval` 结束值 > `timeSpent`(timeSpent 用了加合成任务前的 makespan,
               烟机联动会延到 end+3, 于是出现 "214-227 但 timeSpent=226" 的自相矛盾)
      - 要求11 步骤标题用了"蒸烤类/炒制类"这种**分类名**, 规范要"烘烤/蒸煮/爆炒"等**动作名**
      - 4.5.2  type=3 的纯人工句(含"汤"字)缺 recipeNames
    """
    import re as _re
    from app.spec.adapter import build_plan, all_names
    import random as _rnd

    def check(p, req):
        v = []
        for f in ("overview", "cookingTimeline", "ingredientsSummary",
                  "detailTimeline", "recipeDetail"):
            if f not in p:
                return [f"4.1 缺顶层字段 {f}"]
        for bad in ("code", "message", "data"):
            if bad in p:
                v.append(f"第2节 不得有 {bad} 包装")
        ov = p["overview"]
        if ov.get("recipeCount") != len(req):
            v.append("要求8 recipeCount 不等于请求长度")
        if float(ov.get("timeSave") or 0) < 0:
            v.append("要求5 timeSave < 0")
        spent = float(ov.get("timeSpent") or 0)

        def mm(s):
            h, m = str(s).split(":")
            return int(h) * 60 + int(m)
        for x in p["cookingTimeline"]:
            if x.get("product") not in ("灶具", "蒸箱", "烤箱", "冰箱", "洗碗机",
                                        "净水机", "烟机"):
                v.append(f"4.3 product 非法: {x.get('product')!r}")
            if not _re.match(r"^\d{2}:\d{2}$", str(x.get("startTime", ""))):
                v.append("4.3 startTime 非 HH:mm")
            if not _re.match(r"^\d{2}:\d{2}$", str(x.get("endTime", ""))):
                v.append("4.3 endTime 非 HH:mm")
        # 要求3: finishTime 不得早于任何一道菜的 endTime。
        # ⚠ **必须用环形差判, 不能裸比 mm 值**: adapter.hhmm() 是 `now + offset` 的
        #   %H:%M, 跨午夜会回绕 —— now=23:10 排 50 分钟 ⇒ "00:00"。裸比会让
        #   "00:00 < 23:52" 假报违规(实测 now=23:10 时 8 组里中 1 组, 只在临近午夜
        #   复现 ⇒ 表现为"随机红一次"的偶发失败, 会让整套护栏失信)。
        #   HH:mm 表达不了"次日", 回绕是格式本身的限制 —— 语义上 00:00 确实晚于
        #   23:52, 错的是比较方式, 不是计划。
        #   判据: dish_end <= makespan ⇒ (finish − end) % 1440 == makespan − end <= makespan。
        _mk = float(ov["timeSpent"] or 0)
        for x in p["cookingTimeline"]:
            if (mm(ov["finishTime"]) - mm(x["endTime"])) % 1440 > _mk + 1:
                v.append("要求3 finishTime 早于某道菜的 endTime")
                break
        if sorted(x["name"] for x in p["cookingTimeline"]) != sorted(req):
            v.append("要求8 cookingTimeline 未覆盖全部菜谱")
        if sorted(x["name"] for x in p["recipeDetail"]) != sorted(req):
            v.append("要求8 recipeDetail 未覆盖全部菜谱")
        prev = None
        for s in p["detailTimeline"]:
            if s.get("type") not in (1, 2, 3, 4, 5, 6):
                v.append(f"4.5 type 越界: {s.get('type')}")
            m = _re.match(r"^(\d+)-(\d+)$", str(s.get("timeInterval", "")))
            if not m:
                v.append(f"4.5 timeInterval 非法: {s.get('timeInterval')!r}")
                continue
            a, b = int(m.group(1)), int(m.group(2))
            if b < a:
                v.append("4.5 timeInterval 结束早于开始")
            if b > spent + 1e-6:
                v.append(f"要求6 timeInterval {s['timeInterval']} > timeSpent {spent}")
            t = s["type"]
            if prev and (t < prev[0] or (t == prev[0] and a < prev[1])):
                v.append("要求9 detailTimeline 未按 type/时间升序")
            prev = (t, a)
            if t in (3, 4, 5) and not s.get("recipeNames"):
                v.append(f"4.5.2 type={t} 缺 recipeNames")
            if not any(k in str(s.get("title", "")) for k in
                       ("预热", "烘烤", "蒸煮", "蒸饪", "煎炸", "炖煮", "爆炒",
                        "油炸", "焯水", "装盘", "食材处理", "腌制备料", "烟机联动")):
                v.append(f"要求11 标题非规范简洁名称: {s.get('title')!r}")
            for prm in s.get("parameters") or []:
                if prm.get("recipeName") and prm["recipeName"] not in s.get("recipeNames", []):
                    v.append("要求10 parameters.recipeName 不在 recipeNames 中")
            if "预热" in str(s.get("title", "")):
                if not s.get("parameters"):
                    v.append("4.5.2 预热步骤缺 parameters")
                elif b <= a:
                    v.append("4.5.2 预热 timeInterval 未反映真实预热时间")
        return v

    names = all_names()
    rnd = _rnd.Random(7)
    for _ in range(8):
        pick = rnd.sample(names, rnd.randint(2, 5))
        plan = build_plan([{"id": i + 1, "name": n} for i, n in enumerate(pick)])
        errs = check(plan, pick)
        assert not errs, f"组合 {pick} 违反官方接口规范: {errs[:5]}"


def test_api_times_are_rollover_safe(monkeypatch):
    """跨午夜的 HH:mm 回绕, 不得让 finishTime 看起来早于某道菜的 endTime。

    缺陷史: `adapter.hhmm()` 是 `now + offset` 的 `%H:%M`, 跨午夜会回绕 ——
    now=23:10 排 50 分钟 ⇒ "00:00"。裸比分钟值会假报"要求3 违反"(实测 8 组里 1 组,
    只在临近午夜复现), 表现为**随机红一次的偶发失败**, 会让整套护栏失信。
    HH:mm 表达不了"次日", 回绕是格式本身的限制, 语义上 00:00 确实晚于 23:52 ——
    所以要改的是**比较方式**(按相对首时刻的偏移), 不是把计划压到午夜前。

    这里把时钟钉在 23:10 把这个场景变成必现, 断言回绕口径下不变量成立。
    """
    import datetime as _dt
    import random as _rnd
    from app.spec import adapter as _A
    from app.spec.adapter import build_plan, all_names

    class _FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 4, 23, 10, 0)

    monkeypatch.setattr(_A, "datetime", _FakeDT)
    names = all_names()
    rnd = _rnd.Random(7)

    def mm(s):
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)

    for _ in range(8):
        pick = rnd.sample(names, rnd.randint(2, 5))
        p = build_plan([{"id": i + 1, "name": n} for i, n in enumerate(pick)])
        ov = p["overview"]
        mk = float(ov["timeSpent"] or 0)
        # 环形差: dish_end <= makespan ⇒ (finish − end) % 1440 == makespan − end <= makespan。
        # 裸比 mm 值在这里会假报违规(00:00 vs 23:52), 见本用例 docstring。
        for x in p["cookingTimeline"]:
            d = (mm(ov["finishTime"]) - mm(x["endTime"])) % 1440
            assert d <= mk + 1, (
                f"跨午夜回绕导致 finishTime 早于 endTime: {pick} "
                f"(finish={ov['finishTime']} end={x['endTime']} 环形差={d} makespan={mk})")


def test_mode_matches_official_device_enum():
    """
    设备模式必须落在官方《设备参数清单参考》的枚举内。

    infer_mode() 给的是**烹饪手法**(清蒸/翻炒/煎制), 而设备只认自己的模式名:
    蒸箱只认 普通蒸/鲜嫩蒸/脱脂蒸/多层蒸, 烤箱只认 上下火/全开烤/鼓风烤/…
    直接输出会出现"蒸箱·腌制""烤箱·煎制"这种设备与模式对不上的组合,
    违反接口规范 4.6.1 (cookingParameters.mode = 工作模式)。
    归一前 165 条设备步骤里有 24 条越界, 归一后 1 条。
    """
    OFFICIAL = {
        "蒸箱": {"普通蒸", "鲜嫩蒸", "脱脂蒸", "多层蒸"},
        "烤箱": {"上下火", "全开烤", "鼓风烤", "顶部烤", "加湿烤", "环风烤",
                 "脱脂烤", "空气炸", "蔬果干"},
        "冰箱": {"冷藏", "冷冻", "解冻"},
    }
    from app.spec.adapter import get, all_names
    bad = []
    for nm in all_names():
        r = get(nm)
        if not r:
            continue
        for st in parse_recipe(nm, r["ingredients"], r["steps"]).steps:
            if st.device in OFFICIAL and st.mode and st.mode not in OFFICIAL[st.device]:
                bad.append(f"{nm}#{st.step_id}({st.device}·{st.mode})")
    assert len(bad) <= 2, f"设备模式越出官方枚举: {bad[:8]}"


def test_unknown_dish_is_reported_not_silently_dropped():
    """菜名既不在官方菜库、又没给步骤 -> 必须显式告警, 不能静默丢菜。"""
    from app.api.main import do_schedule, ScheduleIn, RecipeIn
    body = ScheduleIn(recipes=[RecipeIn(name="蒜蓉西兰花")], notify=False)
    out = do_schedule(body)
    assert out.get("warnings"), "未命中的菜没有产生任何告警"


def test_notifications_no_empty_subject_no_duplicate():
    """
    通知回归: 不能有"预热空了，可以放入"这种没有菜名的空提醒,
    也不能同一道菜连发多条出锅提醒(会把真正要紧的提醒淹没)。
    """
    recs = [
        _mk("鲈鱼", "第1步：放入蒸箱普通蒸100度8分钟；"
                    "第2步：取出装盘；第3步：再浇上蒸鱼豉油即可"),
        _mk("鸡翅", "第1步：洗净；第2步：烤箱180度烤25分钟"),
    ]
    plan = schedule(recs)
    notes = generate_notifications(plan, priors=get_priors(recs))
    assert notes, "没有生成任何通知"
    for n in notes:
        assert n["text"].strip(), "出现空文案"
        assert "可以放入" not in n["text"] or n["text"].split("可以放入")[-1].strip(), \
            f"设备提醒缺菜名: {n['text']}"
    keys = [(n["at_min"], n["text"]) for n in notes]
    assert len(keys) == len(set(keys)), "存在完全重复的通知"
    finish = [n for n in notes if n["kind"] == "finish"]
    dishes = [n["text"] for n in finish]
    assert len(dishes) == len(set(dishes)), "同一道菜被重复提醒出锅"


def test_utilization_shared_chamber_counted_once():
    """蒸箱同温共享腔体: 两个并行任务只能算一次占用。

    旧口径把每个任务的 duration 直接相加当分子、用单元自身窗口当分母, 所以
    "两菜同温一锅蒸"会算出 80/40 = 200%。新口径取区间并集 → 40/(4 腔体×40) = 25%。
    """
    from app.models.schema import SchedulePlan, ScheduledTask
    tasks = [
        ScheduledTask(task_id="A#1", dish="A", step_id=1, raw_text="蒸", device="蒸箱",
                      unit_id="蒸箱-1", start_min=0, end_min=40, duration_min=40,
                      temp_c=100, unattended=True),
        ScheduledTask(task_id="B#1", dish="B", step_id=1, raw_text="蒸", device="蒸箱",
                      unit_id="蒸箱-1", start_min=0, end_min=40, duration_min=40,
                      temp_c=100, unattended=True),
    ]
    k = evaluate(SchedulePlan(tasks=tasks, makespan_min=40, finish_gap_min=0))
    # 腔体数 = 灶具 2 + 蒸箱 1 + 烤箱 1
    assert k.device_utilization == 25.0, f"共享腔体被重复计入: {k.device_utilization}"
    assert k.device_utilization <= 100.0


def test_utilization_single_task_unit_is_not_100():
    """只有一个任务的单元不该恒为 100%。

    旧口径分母是"单元自己的活跃窗口", 于是"只排一个任务"= 满分, 而"多用设备"
    反而掉分 —— 激励方向完全反了。新口径分母是全局 makespan × 腔体数。
    """
    from app.models.schema import SchedulePlan, ScheduledTask
    tasks = [ScheduledTask(task_id="A#1", dish="A", step_id=1, raw_text="蒸",
                           device="蒸箱", unit_id="蒸箱-1",
                           start_min=0, end_min=10, duration_min=10, unattended=True)]
    k = evaluate(SchedulePlan(tasks=tasks, makespan_min=60, finish_gap_min=0))
    assert k.device_utilization == 4.2, k.device_utilization      # 10 / (4×60)
    assert k.util_detail["蒸箱-1"] == 16.7, k.util_detail         # 10 / 60


def test_utilization_preheat_merged_into_own_chamber():
    """预热占的是同一个物理腔体, 明细里要能看出它属于哪台设备。"""
    from app.models.schema import SchedulePlan, ScheduledTask
    tasks = [
        ScheduledTask(task_id="预热@蒸箱@0", dish="", step_id=0, raw_text="蒸箱预热",
                      device="蒸箱", unit_id="预热-蒸箱-1", start_min=0, end_min=5,
                      duration_min=5, unattended=True, temp_c=100),
        ScheduledTask(task_id="A#1", dish="A", step_id=1, raw_text="蒸", device="蒸箱",
                      unit_id="蒸箱-1", start_min=5, end_min=45, duration_min=40,
                      unattended=True, temp_c=100),
    ]
    k = evaluate(SchedulePlan(tasks=tasks, makespan_min=45, finish_gap_min=0))
    assert k.util_detail["蒸箱-1"] == 88.9, k.util_detail
    assert k.util_detail["预热-蒸箱-1"] == 11.1, k.util_detail
    assert k.device_utilization == 25.0, k.device_utilization     # 45 / (4×45)


def test_utilization_stays_in_range_across_combos():
    """随机组合抽样: 利用率必须落在 [0,100], 且不会出现 100% 天花板扎堆。"""
    import random as _rnd
    from app.spec.adapter import get, all_names, plan_recipes
    names = all_names()
    rng = _rnd.Random(7)
    vals = []
    for _ in range(30):
        pick = rng.sample(names, rng.randint(3, 5))
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        vals.append(evaluate(plan_recipes(recs, agent=True)).device_utilization)
    assert all(0.0 <= v <= 100.0 for v in vals), f"利用率越界: {vals}"
    assert sum(1 for v in vals if v >= 99.9) == 0, f"出现 100% 天花板: {vals}"
    assert sum(vals) / len(vals) > 10.0, f"利用率低到异常, 口径可能写错: {vals}"


def test_reschedule_keeps_kitchen_semantics_and_no_duplicates():
    """动态重排必须和初次排程一样带预热任务, 且不能出现重复行。

    旧实现 reschedule() 直接返回 _schedule() 的结果, 漏掉了 _add_kitchen_semantics:
      ① 重排后的计划里没有预热行, 与同一道菜的初次排程前后不一致;
      ② 反过来, 被冻结回填的旧合成任务(task_id 形如 "预热@...")又留在输出里,
         于是前端甘特图凭空多/少行。

    v3.0 语义变更: 预热不再是"排程后合成的无主任务"(step_id=0 且 dish 为空),
    而是 L1 拆出来的**真实步骤节点**(有菜名、有编号、占设备槽)。
    所以判据从"有没有合成任务"改成"蛋糕这条链里有没有烤箱预热节点"。
    不写预热就不该凭空补; 烟机联动默认关闭(见 test_no_synthetic_hood_task)。
    """
    recs = [
        _mk("蛋糕", "第1步：拌面糊；第2步：烤箱预热10分钟；第3步：烤箱烤25分钟"),
        _mk("蒸鱼", "第1步：洗净切段；第2步：蒸箱蒸12分钟；第3步：装盘"),
    ]
    p1 = schedule(recs)
    ph1 = [t for t in p1.tasks if t.device == "烤箱" and t.raw_text.endswith("预热")]
    assert ph1, "原文写了预热却没有产出预热任务"
    # 预热必须是**真实节点**: 有菜名、占设备槽、无人值守(可与人工并行)
    assert ph1[0].dish == "蛋糕", f"预热节点应归属具体菜品, 实际 dish={ph1[0].dish!r}"
    assert not ph1[0].needs_human, "预热无人值守, 不应占用人工"
    assert ph1[0].duration_min >= 1, "预热时长必须是真实区间, 不能是 0 区间"
    assert not any(t.step_id == 0 and not t.dish for t in p1.tasks), \
        "不应再有'无主'的合成预热任务"

    # 朴素地冻结"前 2 步"
    fixed = {(t.dish, t.step_id): t for t in p1.tasks if t.step_id <= 2}
    p2 = reschedule(recs, fixed, new_dish=_mk("汤", "第1步：烧水；第2步：炖25分钟"))

    ph2 = [t for t in p2.tasks if t.device == "烤箱" and t.raw_text.endswith("预热")]
    assert ph2, "重排结果里没有预热任务"
    assert len(ph2) == len(ph1), f"重排后预热行数变了: {len(ph1)} -> {len(ph2)}"

    seen = {}
    for t in p2.tasks:
        key = (t.unit_id, round(t.start_min, 1))
        seen[key] = seen.get(key, 0) + 1
    dup = {k: v for k, v in seen.items() if v > 1}
    assert not dup, f"重排后出现同一设备同一时刻的重复任务: {dup}"

    # dish_finish 只能有真实菜名 —— 合成任务混进来会污染出锅差口径
    assert all(p2.dish_finish), f"dish_finish 混入空菜名: {sorted(p2.dish_finish)}"
    assert "" not in p2.dish_finish, "合成任务污染了 dish_finish"


def test_preheat_occupies_device_slot_not_overlapping():
    """v3.0 回归: 预热必须在 DAG 里占真实设备槽, 不能与别的菜的设备任务重叠。

    旧实现把预热排程之后硬塞成 [start-dur, start] 的合成任务, 不占资源槽 ——
    实测 30 组抽样里 2/2 的预热都与别的菜真实设备任务重叠(如 "蒸箱预热[93-98]
    叠 三味蒸鳗鱼#12[86-94]")。设备冲突是官方评分的大头, 这类自造冲突必须消掉。
    """
    recs = [
        _mk("蛋糕", "第1步：拌面糊；第2步：烤箱预热10分钟；第3步：烤箱烤25分钟"),
        _mk("排骨", "第1步：洗净；第2步：烤箱烤30分钟"),
    ]
    p = schedule(recs)
    ph = [t for t in p.tasks if t.raw_text.endswith("预热") and t.device == "烤箱"]
    assert ph, "没有产出预热节点"
    assert ph[0].unit_id.startswith("烤箱"), \
        f"预热必须占用真实烤箱槽位, 实际 unit_id={ph[0].unit_id!r}"
    from app.scheduler.checker import check_plan
    ck = check_plan(p)
    assert ck["ok"], f"预热引入设备冲突或依赖违反: {ck['details']}"


def test_no_synthetic_hood_task():
    """
    烟机联动已整条删除, 不许再回来: 官方《接口规范》全文 0 次提到烟机,
    官方 100 道菜也 0 次提到 烟机/油烟机/排烟 —— 合成它等于凭空造步骤,
    还会把末道菜的结束时刻 +3min 顶高 timeSpent(实测某 3 菜 136 -> 142 分)。
    """
    recs = [
        _mk("A", "第1步：切配；第2步：蒸箱蒸15分钟"),
        _mk("B", "第1步：切配；第2步：灶具爆炒3分钟；第3步：装盘"),
    ]
    p = schedule(recs)
    hood = [t for t in p.tasks if t.unit_id.startswith("烟机")]
    assert not hood, f"又合成了烟机联动任务: {[(t.unit_id, t.start_min, t.end_min) for t in hood]}"

    # 合成任务没有 dish, 绝不能进 dish_finish —— 否则出锅差会被整个抬起
    assert "" not in p.dish_finish, "合成任务污染了 dish_finish"
    # 总时长只能由真实步骤决定, 不该被合成任务撑大
    real_end = max(t.end_min for t in p.tasks if t.dish and t.start_min >= 0)
    assert p.makespan_min <= real_end + 0.6, \
        f"总时长 {p.makespan_min} 超出真实最晚结束 {real_end}, 被合成任务抬高了"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = []
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed.append(fn.__name__)
        except Exception as e:
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
            failed.append(fn.__name__)
    print(f"\n{passed}/{len(fns)} 通过")
    if failed:
        print("失败用例:", "、".join(failed))
        sys.exit(1)


# --------------------------------------------------------------------------
# 动态重排: 可验证的不劣化保证 (v2.3)
# --------------------------------------------------------------------------
def test_reschedule_pareto_guarantees_are_checkable():
    """重排必须给出**能当场判定真假**的保证, 而不是一句"不劣化"。

    两条硬断言:
      ① 已完成步骤零变动 —— 赛题原文 "不推翻已完成步骤"
      ② 不劣于串行基线   —— 基线 = 原计划不动 + 新增菜整体串行接在末尾 (必然可行),
                            任何像样的重排都该赢它
    """
    from app.api.main import RescheduleIn, RecipeIn, do_reschedule
    from app.spec.adapter import all_names

    ns = all_names()
    r = do_reschedule(RescheduleIn(
        recipes=[RecipeIn(name=ns[0]), RecipeIn(name=ns[5])],
        completed=[], extra=RecipeIn(name=ns[20])))

    par = r.get("pareto")
    assert par, "重排响应必须带 pareto 块"
    assert par["保证"]["已完成步骤零变动"] is True
    assert par["保证"]["不劣于串行基线"] is True, (
        f"重排解劣于串行追加基线: 重排后 {par['重排后']} vs 基线 {par['串行基线']}")
    # 串行基线必须是一个有意义的对照 (新增菜确实带来了工作量)
    assert par["串行基线"]["总时长"] >= par["重排后"]["总时长"]


def test_reschedule_keeps_completed_steps_frozen():
    """已完成步骤的开始时刻必须逐一不变 —— 赛题 L4 的硬要求。"""
    from app.api.main import RescheduleIn, RecipeIn, do_reschedule
    from app.spec.adapter import all_names

    ns = all_names()
    base = do_reschedule(RescheduleIn(
        recipes=[RecipeIn(name=ns[0]), RecipeIn(name=ns[5])]))
    done = [t["task_id"] for t in base["plan"]["tasks"]
            if t["start_min"] >= 0 and not t["dish_list"]][:3]
    assert done, "基线上应该能挑出若干可冻结步骤"

    r = do_reschedule(RescheduleIn(
        recipes=[RecipeIn(name=ns[0]), RecipeIn(name=ns[5])],
        completed=done, extra=RecipeIn(name=ns[20])))
    assert r["locked"] == len(done), "冻结步数应与请求一致"
    assert r["pareto"]["保证"]["已完成步骤零变动"] is True
    assert not r["pareto"]["违反明细"]


def test_schedule_exposes_bound_and_model_meta():
    """/schedule 要能回答"离理论最优多远"和"精确轨做了什么"。

    /api/plan 必须保持纯官方 schema, 所以这些只能加在内部端点。
    """
    from app.api.main import ScheduleIn, RecipeIn, do_schedule
    from app.spec.adapter import all_names

    ns = all_names()
    r = do_schedule(ScheduleIn(recipes=[RecipeIn(name=n) for n in ns[:4]]))
    b = r.get("bound")
    assert b, "/schedule 必须返回 bound"
    for k in ("lb", "lb_cp", "lb_energy", "lb_hand", "makespan", "optimality_gap_pct"):
        assert k in b, f"bound 缺字段 {k}"
    assert b["lb"] <= b["makespan"] + 1e-6, "下界不能大于总时长"
    assert b["optimality_gap_pct"] >= 0
    assert "model" in r and "horizon" in r["model"]


# ---------------- v3.2 兜底推断的三类"假设备" ----------------
# 背景: 官方 100 道菜里, 有 20 步被兜底推断凭空判成"蒸箱/烤箱/灶具"的长任务,
# 累计虚占设备 149 分钟。三类成因都是"**烹饪动词出现在名词里**", 而不是本步在做饭:
#   ① 完成态引用: "炖好的猪肉" / "蒸熟的糯米" / "煮沸的椰浆"   (前序步骤的产物)
#   ② 配件/器皿名: "烤碗" / "烤串" / "蒸碟"                   (器皿自带"蒸/烤"字)
#   ③ 调料名/子菜名: "蒸鱼豉油" / "腊味合蒸：" / "黄金炒饭制作："  (名称自带动词)
# 它们共同抬高总时长(实测 82.0 → 79.2 分钟)、白占容量为 1 的蒸箱/烤箱。


def _steps_containing(name, kw):
    from app.spec.adapter import get
    r = get(name)
    assert r, f"官方菜谱缺失: {name}"
    hits = [s for s in parse_recipe(name, r["ingredients"], r["steps"]).steps
            if kw in (s.raw_text or "")]
    assert hits, f"{name} 里找不到含「{kw}」的步骤"
    return hits


def test_completion_state_reference_is_not_a_device_task():
    """① 完成态引用（"X好的/X熟的/X沸的/X过的"）指前序产物，不是本步在做饭。

    官方 100 道菜里 7 条中招，虚占设备 104 分钟，全是装配/装碗/沥干：
      · 小碗蒸肉#10「用筷子将**炖好的**猪肉，挨个放到刚刚的碗里，再均匀淋一些肉汁」
        → 判成"灶具 45 分钟炖煮"，占掉该菜 92 分钟 makespan 的一半
      · 芋头蛋卷#7「把**煎好的**蛋饼放案板上，均匀涂抹上**碾压好的**芋泥」→ 烤箱 25 分钟
    """
    cases = [
        ("小碗蒸肉", "炖好的猪肉"),
        ("芋头蛋卷", "煎好的蛋饼"),
        ("竹荪排骨汤", "焯好的排骨"),
        ("芝士番茄土豆泥", "煮好的土豆"),
        ("草莓蛋挞", "煮好的牛奶液"),
        ("立夏饭", "炒好的蔬菜肉"),
        ("荷叶蒸蟹饭", "炒好的饭"),
        ("血糯米仙草奶茶", "蒸熟的血糯米"),
    ]
    bad = []
    for name, kw in cases:
        for s in _steps_containing(name, kw):
            if s.device in ("灶具", "蒸箱", "烤箱"):
                bad.append(f"{name}: 「{kw}」被判成 {s.device} {s.duration_min} 分钟")
    assert not bad, "完成态引用被当成真烹饪任务:\n" + "\n".join(bad)


def test_completion_state_guard_keeps_real_cooking():
    """① 反向保护：真烹饪句里的**火候副词**（至/开/20分钟）一个都不能被一起摘掉。

    完成态守卫只认 好/熟/透/沸/软/烂/过 + 的/了/后；"烧开后""炒至微黄后""煮20分钟"
    这些是本步真的在做饭，误摘会让整道菜丢掉真实的灶具作业。
    """
    r = _mk("测试菜",
            "第1步：煮开后下入面条；"
            "第2步：将肉炒至微黄后加入老抽；"
            "第3步：盖上盖子煮20分钟")
    assert r.steps[0].device == "灶具", f"「煮开后」被误摘: {r.steps[0].device}"
    assert r.steps[1].device == "灶具", f"「炒至微黄后」被误摘: {r.steps[1].device}"
    assert r.steps[2].device == "灶具", f"「煮20分钟」被误摘: {r.steps[2].device}"
    assert r.steps[2].duration_min == 20.0

    # 完成态 + 真动作同句 ⇒ 仍按真动作判设备，且原文显式时长不被覆盖
    r2 = _mk("测试菜2", "第1步：将炖好的牛腩继续炖20分钟")
    assert r2.steps[0].device == "灶具", f"「继续炖」被误摘: {r2.steps[0].device}"
    assert r2.steps[0].duration_min == 20.0, "显式时长被兜底值覆盖"

    # "炒香后的配料" 必须放行 —— 「麻辣对虾」该步是唯一的蒸箱装载步, 摘了就整条菜丢设备。
    # ⚠ 不要为了救这一条去放宽 need_fb 的触发面（试过加"水箱/加水/第N层"装载信号，
    #   结果把「豆沙松糕#3 边加水边翻拌」「美式薯条#1 铺在蒸烤架上」等 6 条判成蒸箱）。
    r3 = _mk("麻辣对虾",
             "第1步：把炒香后的配料和对虾放入盘中上下翻拌均匀，放在有孔蒸盘上，"
             "放入机器中间层，取出水箱加满水")
    assert r3.steps[0].device == "蒸箱", (
        f"「炒香后的配料」被误摘，导致该菜丢掉蒸箱任务: {r3.steps[0].device}"
    )


def test_accessory_and_seasoning_names_are_not_device_tasks():
    """②③ 器皿名(烤碗/烤串/蒸碟)、调料名(蒸鱼豉油) 里的"蒸/烤"不是动作。"""
    cases = [
        ("椰香苹果香蕉烤燕麦", "烤碗底部"),      # 旧: 烤箱 25 分钟
        ("香烤鲫鱼", "将烤串放在烤架上"),        # 旧: 烤箱 25 分钟
        ("虾米豆豉蒸排骨", "在蒸碟上"),          # 旧: 蒸箱 15 分钟
        ("金银蒜蒸鸡胸肉", "加入蒸鱼豉油"),      # 旧: 蒸箱 60 分钟(其实是腌肉)
    ]
    bad = []
    for name, kw in cases:
        for s in _steps_containing(name, kw):
            if s.device in ("灶具", "蒸箱", "烤箱"):
                bad.append(f"{name}: 「{kw}」被判成 {s.device} {s.duration_min} 分钟")
    assert not bad, "器皿/调料名被当成烹饪动作:\n" + "\n".join(bad)


def test_tablespoon_is_not_a_stewing_verb():
    """"汤匙"是量词，不是"炖汤"的"汤"。

    官方库里「麻辣对虾」有两条同名菜，第二条第4步是纯占位句
    「选择"开始烹饪"按键，按屏幕提示操作」。步骤原文没有任何设备线索时，
    infer_fallback 会退到"菜名+食材清单"再匹配一次 —— 食材里的
    「食用油3**汤**匙」命中了 炖|煲|汤|焖|煨|卤，纯占位句被判成"灶具 45 分钟炖煮"。
    """
    from app.parser.recipe_parser import infer_fallback
    dev, temp, dur, mode, hit = infer_fallback(
        "试验菜", "选择“开始烹饪”按键，按屏幕提示操作",
        "食用油3汤匙；朝天椒4个；盐1克")
    assert not (hit and dev == "灶具" and dur == 45.0), (
        f"「汤匙」的量词「汤」被判成炖煮: device={dev} dur={dur}"
    )


def test_combo_dish_label_is_not_a_device_task():
    """③ 套餐菜每一步以**子菜名**开头("腊味合蒸：…")，子菜名里的动词不是本步动作。"""
    cases = [
        ("轻松一锅蒸", "腊味合蒸："),        # 旧: 蒸箱 15 分钟(其实是切配装碗)
        ("烹香酷炒汇", "黄金炒饭制作："),    # 旧: 灶具 5 分钟(其实是切丁)
        ("烹香酷炒汇", "白菜炖豆腐制作："),  # 旧: 灶具 45 分钟(其实是切片腌制)
    ]
    bad = []
    for name, kw in cases:
        for s in _steps_containing(name, kw):
            if s.device in ("灶具", "蒸箱", "烤箱"):
                bad.append(f"{name}: 「{kw}」被判成 {s.device} {s.duration_min} 分钟")
    assert not bad, "子菜名标签被当成烹饪动作:\n" + "\n".join(bad)


def test_official_corpus_has_no_phantom_device_steps():
    """端到端回归：官方 100 道菜的"占加热设备的步骤数"必须守在 212 以内。

    修复前 230 步；三类假设备（完成态引用/器皿名/调料与子菜名）修掉 20 步 → 210。
    这个数字是解析正确性的**指纹** —— 任何一条放宽兜底推断的改动都会把它顶上去，
    而"有加热设备的菜"必须始终是 99/100（不能靠"少判设备"来降低步数）。

    ⚠ 210 → 212 (2026-10-06 甘特图抽查发现): 复合设备继承的守卫写成
      `not any(s.device in HEATING_DEVICES)`, 而 HEATING_DEVICES 含**灶具** ——
      只要菜里有任何一个灶具步, 蒸烤一体机的继承就被整段跳过, 真正的烘烤步停在"人工"。
      修正为只看 蒸箱/烤箱 后, **恰好 2 步**从"人工"拿到设备, 且都是真设备步:
        · 「玛格丽特」#4「放入机器中间层，选择"开始烹饪"按键」→ 烤箱 180℃
          （原为人工 ⇒ 被 rule_prior 当备菜 ⇒ 整道菜 4 步落进 t<0,
            当天只剩 3 分钟"晾凉" ⇒ T=3.0min、设备利用率 0%, 烘烤段在甘特图上不存在）
        · 「芝士番茄土豆泥」#12「打开方太智能烹饪设备，选择"开始烹饪"按键」→ 烤箱 180℃
          （该菜第 13 步原文"烤好取出"印证是烤制菜, 原先整道无烘烤步）
      两处都有 `device_source='(复合设备推断)'` 可核；"有加热设备的菜"仍是 99/100。
      详细回归见 test_combo_device_inheritance_survives_stove_step。

    ⚠ 212 → 193 (2026-10-06, 纯装载/取放句修复): 见 test_pure_load_sentence_is_not_a_device_step。
      19 条"放入烤箱中"这类**取放句**不再算设备步(其中 15 条同菜同设备另有真时长步)
      ⇒ 净减 19(口径含预热节点; 预热节点本身 5 条**未变** —— 那 3 条"放入预热好的烤箱中"
      虽然摘了设备, 预热节点仍由 insert_preheat_steps 从原文取设备补回来, 见该函数内注释)。
      ⚠ 这次是**减少**, 方向与上面相反 —— 所以断言写 `<= 193`:
        任何"假设备任务又回来了"的改动都会把它顶上去; 而"有加热设备的菜"仍须 99/100
        (不能靠"少判设备"来降步数)。

    ⚠ 口径必须是 recipe_store.all_records()（官方 100 条**记录**）。
    用 adapter.all_names() 会因「麻辣对虾」重名而把同一条记录数两次，得 211。
    """
    from app.spec import recipe_store
    heat_steps = 0
    heat_dishes = 0
    total = 0
    for rec in recipe_store.all_records():
        total += 1
        steps = parse_recipe(rec["name"], rec["ingredients"], rec["steps"]).steps
        heat = [s for s in steps if s.device in ("灶具", "蒸箱", "烤箱")]
        heat_steps += len(heat)
        if heat:
            heat_dishes += 1
    assert total == 100, f"官方菜谱记录数从 100 变成 {total}"
    assert heat_dishes == 99, f"有加热设备的菜从 99 掉到 {heat_dishes} —— 设备被判少了"
    assert heat_steps <= 193, f"占加热设备的步骤数 {heat_steps} > 193（假设备任务又回来了）"


def test_pure_load_sentence_is_not_a_device_step():
    """纯装载/取放句不能被判成设备步 (v3.3, 2026-10-06)。

    "将烤盘放入烤箱中" / "放入烤箱中" / "将装有饼坯的烤盘放入烤箱，关闭好"
    的动作是**取放**, 设备并没有开始工作 —— 真正的工作在**相邻的程序句**里
    ("选择'常规烘焙'模式，以170℃烤13分钟")。

    旧实现把它认成烤箱烹饪步, 而它没写时长 ⇒ estimate_duration 补成 8 分钟 ⇒
      · **凭空多占烤箱 8 分钟**(烤箱容量 1, 是最稀缺的资源);
      · 人工也被占一次(装载确实要动手, 这一半是对的);
      · 「黄油小饼干」#9/#10 + #11 三句其实只是**一次 13 分钟烘烤** ⇒ 虚增 10 分钟烤箱占用。

    实测官方 100 道菜 19 条中招(15 条同菜同设备另有真时长步), 修掉后
    "占加热设备的步骤数" 212 → **190**, "有加热设备的菜" 仍 99/100。

    ⚠ 守卫(反向): 同样含"放入"但带**程序启动信号**的句子必须**保持设备步** ——
      "放入方太智能烹饪设备第2层，开启电源，选择'开始烹饪'按键，按屏幕提示操作"
      是官方最典型的设备任务(只是原文没给时长), 摘掉它整道菜会丢掉主烹饪步。
      判据里 `not LOAD_START_VERBS` / `not ACTIVE_COOK` 两条就是为它留的。
    """
    from app.models.schema import PREP
    from app.spec import recipe_store
    recs = {r["name"]: r for r in recipe_store.all_records()}

    def steps_of(name):
        r = recs[name]
        return {s.step_id: s
                for s in parse_recipe(r["name"], r["ingredients"], r["steps"]).steps}

    # ① 纯装载句 → 人工备料(不是设备步)
    for name, sid in (("黄油小饼干", 9), ("黄油小饼干", 10), ("培根披萨", 11),
                      ("培根披萨", 16), ("鸡翅包饭", 8), ("大理石饼干", 15),
                      ("香菇烤芦笋", 3), ("红枣酥饼", 15)):
        s = steps_of(name)[sid]
        assert s.device == "人工", \
            f"{name}#{sid}「{s.raw_text[:32]}」仍是 {s.device}（装载句虚占设备）"
        assert s.prep_type == PREP, \
            f"{name}#{sid} 应回落成人工备料, 实际 {s.prep_type}"

    # ② 同菜的真时长步必须还在(R3 口径: 真实加热步骤不许消失)
    s = steps_of("黄油小饼干")[11]
    assert s.device == "烤箱" and s.duration_min == 13.0, \
        f"黄油小饼干#11 真烘烤步丢了: {s.device} {s.duration_min}"

    # ③ 带程序启动信号的"放入"句必须**保持设备步**(反向守卫)
    #    这 4 条含"选择开始烹饪"/"根据屏幕提示操作" ⇒ 是官方最典型的设备任务。
    #    ⚠ 但它们**测不出 `not LOAD_START_VERBS` 这条判据** —— 前 3 条被 ACTIVE_COOK
    #      拦住, 「低温牛排」#2 被复合设备继承救回来(详见
    #      test_load_sentence_with_program_start_keeps_device 的说明)。
    #      这里只做"官方语料上的端到端回归", 判据本身由合成句钉。
    for name, sid, dev in (("酿苦瓜", 5, "蒸箱"), ("榴莲酥", 1, "烤箱"),
                           ("拿破仑酥", 8, "烤箱"), ("低温牛排", 2, "蒸箱")):
        s = steps_of(name)[sid]
        assert s.device == dev, \
            f"{name}#{sid} 被误摘设备: {s.device} != {dev}（真设备步丢了）"

    # ④ 真烹饪句里的"放入"不能误伤(ACTIVE_COOK 守卫)
    #    "锅中注油烧热，放入蔬菜、火腿肠、米饭炒匀" 含"放入"+灶具, 但是正经灶前活
    s = steps_of("鸡翅包饭")[4]
    assert s.device == "灶具", f"鸡翅包饭#4 真灶具步被误摘: {s.device}"
    s = steps_of("小碗蒸肉")[6]
    assert s.device == "灶具", f"小碗蒸肉#6 真灶具步被误摘: {s.device}"

    # ⑤ 摘设备**不能**把"预热"这个真实设备作业一起摘掉
    #    "放入预热好的烤箱中" 摘掉设备后, 预热节点必须仍由 insert_preheat_steps
    #    从**原文**取回设备补上(物理顺序: 预热 → 装载 → 烘烤)。
    #    ⚠ 这条是实测踩出来的: 不补的话官方预热节点从 5 条掉到 2 条,
    #      audit_doc 的 3.1.4(预热为独立任务节点) 会由 ok 变**缺陷**。
    from app.parser.recipe_parser import is_preheat_step
    for name in ("半熟芝士蛋糕", "草莓蛋挞", "蔬菜塔"):
        ph = [s for s in steps_of(name).values() if is_preheat_step(s)]
        assert ph, f"{name} 的预热节点丢了（摘装载句设备时把预热一起摘了）"
        assert all(s.device == "烤箱" for s in ph), \
            f"{name} 预热节点设备不对: {[s.device for s in ph]}"


def test_load_sentence_with_program_start_keeps_device():
    """反向守卫: 带**程序启动信号**的"放入"句必须保持设备步 —— 用合成句直接钉判据。

    ⚠ 为什么必须用合成句, 不能用官方语料:
      唯一"只被 `not LOAD_START_VERBS` 拦住"的句子是「低温牛排」#2
      「将牛排放入蒸烤架上，放入蒸烤箱第二层，蒸烤箱取出水箱加满水，根据屏幕提示操作」。
      但把这条判据删掉后它**仍会被复合设备继承救回来** —— 该菜清掉设备后不再有任何
      蒸箱/烤箱步, 于是 parse_recipe 末尾的 combo_device_of 按"加满水"把它判回蒸箱。
      实测: 删/留这条判据, 官方 100 道菜的解析指纹(sha256)**完全一致**
      (50e7fecd57db2899), 所以语料测不出它。而它在真实场景里是有用的:
      只要该菜**另有**一个蒸箱/烤箱步, 复合设备继承的守卫 `not any(蒸箱/烤箱)` 就为假,
      这条句子就会掉设备 ⇒ 整道菜丢掉主烹饪步。合成句把判据本身钉死。
    """
    # ① 有"屏幕提示" ⇒ 设备程序会跑起来 ⇒ 保持设备(这一条只有 LOAD_START 能拦)
    r = parse_recipe("甲", "", "第1步：切菜；第2步：将烤盘放入烤箱中，按屏幕提示操作")
    assert r.steps[1].device == "烤箱", \
        f"带「屏幕提示」的装载句掉了设备: {r.steps[1].device}"
    # ② 有"开启电源" ⇒ 同上
    r = parse_recipe("乙", "", "第1步：切菜；第2步：将烤盘放入烤箱中，开启电源")
    assert r.steps[1].device == "烤箱", \
        f"带「开启电源」的装载句掉了设备: {r.steps[1].device}"
    r = parse_recipe("戊", "", "第1步：切菜；第2步：将食材放入蒸箱，开启电源")
    assert r.steps[1].device == "蒸箱", \
        f"带「开启电源」的蒸箱装载句掉了设备: {r.steps[1].device}"
    # ③ 有主动加热动作 ⇒ 真烹饪, 保持设备(ACTIVE_COOK 守卫)
    r = parse_recipe("丙", "", "第1步：切菜；第2步：将烤盘放入烤箱中烤制")
    assert r.steps[1].device == "烤箱", \
        f"带「烤制」的装载句掉了设备: {r.steps[1].device}"
    # ④ 三条判据都不命中 ⇒ 纯取放, 摘设备
    r = parse_recipe("丁", "", "第1步：切菜；第2步：将烤盘放入烤箱中")
    assert r.steps[1].device == "人工", \
        f"纯取放句仍是设备步: {r.steps[1].device}"


def test_combo_device_inheritance_survives_stove_step():
    """复合设备(蒸烤一体机)继承不能被**灶具步**挡住 (2026-10-06 抽查发现)。

    旧守卫: `not any(s.device in HEATING_DEVICES)`, 而 HEATING_DEVICES=(灶具,蒸箱,烤箱)。
    「玛格丽特」第 1 步是「煮鸡蛋 15 分钟」(灶具) ⇒ 守卫为假 ⇒ 蒸烤一体机继承整段跳过
    ⇒ 第 4 步「放入机器中间层，选择"开始烹饪"按键」停在 人工/切配。
    后果链: rule_prior 把"人工步"一律当备菜 ⇒ 第 1~4 步全落进 t<0 ⇒ 当天只剩 3 分钟
    "晾凉" ⇒ T=3.0min、设备利用率 0%, **烘烤段在甘特图上根本不存在**。

    判据自相矛盾可直接复现: 同一句「放入机器中间层…」在**没有灶具步**的菜里能判成烤箱。
    """
    from app.spec.adapter import get
    cases = {"玛格丽特": "烤箱", "芝士番茄土豆泥": "烤箱"}
    for name, want in cases.items():
        d = get(name)
        assert d is not None, f"官方菜谱缺失: {name}"
        steps = parse_recipe(name, d["ingredients"], d["steps"]).steps
        devs = [s.device for s in steps]
        assert "灶具" in devs, f"{name}: 该用例本应含灶具步(否则复现不出守卫问题)"
        assert want in devs, f"{name}: 复合设备继承被灶具步挡住了 -> 设备={devs}"


# ---------------- v3.3 备菜前置（prep_keys）----------------
# 背景: "哪些步骤可以提前到开火前完成"决定了三条轨**排的是不是同一个任务集**。
# 旧判据要求"前置自己也是 hard/soft"，于是超长等待只要前面挂着一个普通短步骤，
# 整条链就永远进不了 prep，4 小时冷却照压在当天关键路径上。


def test_long_wait_is_moved_out_of_the_day():
    """超长等待必须真的移出当天 —— 哪怕它前面挂着一个 2 分钟的普通步骤。

    「血糯米仙草奶茶」#6 = 240 分钟冷却(hard)，前置 #5 只是 2 分钟的人工搅拌。
    旧判据 ⇒ #6 进不了 prep ⇒ 该组 makespan 284 分钟、下界 42、**gap 576%**
    （lb_probe seed=11 全场最差）。
    """
    from app.spec.adapter import get
    from app.scheduler.prep import prep_keys
    from app.agent.priors import get_priors

    nm = "血糯米仙草奶茶"
    r = get(nm)
    assert r, f"官方菜谱缺失: {nm}"
    rec = parse_recipe(nm, r["ingredients"], r["steps"])
    ids = {i for d, i in prep_keys([rec], get_priors([rec]), allow_preprocess=True)}
    assert 6 in ids, "240 分钟冷却没进 prep（又被前置卡住了）"
    # 前缀必须闭合：前移的步，它的祖先必须一起前移
    for d in (rec.steps[5].depends_on or []):
        assert d in ids, f"#6 前移了但前置 #{d} 还在当天 —— 会依赖违反"

    # 端到端：该步真的落在 t<0，且排程没有依赖违反
    plan = schedule([rec])
    kpi = evaluate(plan)
    assert kpi.depend_violation == 0, f"依赖违反 {kpi.depend_violation}"
    t6 = [t for t in plan.tasks if t.dish == nm and t.step_id == 6]
    assert t6, "步骤 #6 在计划里消失了"
    assert t6[0].end_min <= 1e-6, (
        f"240 分钟冷却仍压在当天: {t6[0].start_min}~{t6[0].end_min}"
    )


def test_prep_ahead_stays_prefix_closed_and_bounded():
    """① 前缀闭合（不产生依赖违反）② 有界（不许把整道菜推进 t<0 刷 makespan）。

    ⚠ 边界②是实测出来的：把**前缀闭包也套到 soft 种子**上，25/100 道菜会整道落进
      t<0、平均 prep 占比 44%→70%，`makespan`（只统计 start>=0 的任务）从 79 崩到
      36 分钟 —— 那是把活挪出计量窗口，不是排得更快。所以 soft 种子必须保持保守口径。
    """
    from app.spec import recipe_store
    from app.scheduler.prep import prep_keys
    from app.agent.priors import get_priors

    full_prep, ratios = [], []
    for rec0 in recipe_store.all_records():
        rec = parse_recipe(rec0["name"], rec0["ingredients"], rec0["steps"])
        pk = prep_keys([rec], get_priors([rec]), allow_preprocess=True)
        by_id = {s.step_id: s for s in rec.steps}
        ids = {i for d, i in pk if d == rec0["name"]}
        for sid in ids:                      # ① 前缀闭合
            for dep in (by_id[sid].depends_on or []):
                assert dep in ids, (
                    f"{rec0['name']}#{sid} 前移了，前置 #{dep} 却还在当天 —— 依赖违反"
                )
        n = len(rec.steps)
        ratios.append(len(pk) / n if n else 0)
        if n and len(ids) == n:
            full_prep.append(rec0["name"])
    assert len(full_prep) <= 3, (
        f"{len(full_prep)} 道菜整道被推进 t<0（makespan 会被刷掉）: {full_prep[:8]}"
    )
    avg = sum(ratios) / len(ratios)
    assert avg <= 0.50, f"平均 prep 占比 {avg:.1%} > 50% —— 当天工作量被改写"


def test_greedy_track_shares_the_prep_rule():
    """三条轨的"可提前"判据必须只有一份（prep.py::prep_keys）。

    历史上 engine._build_tasks 里有一份**内联的旧判据**，与 prep.py 的修复不同步 ⇒
    贪心轨仍把 4 小时冷却压在当天，而 CP-SAT 轨/精确轨已经把它移走 ——
    三轨排的又不是同一个任务集。判据本体绝不能再被复制一份。
    """
    import inspect
    from app.scheduler import engine as _e
    src = inspect.getsource(_e._build_tasks)
    assert "prep_keys(" in src, "_build_tasks 没有调用 prep_keys —— 判据又被复制了一份"
    assert 'all((t["dish"], d[1]) in prep' not in src, "内联的旧前缀判据又回来了"


def test_prep_ahead_threshold_stays_at_30():
    """备菜前置门槛 30 分钟 —— 有实测依据的边界，不是随手填的数。

    上调回 90 会把官方菜谱里 7 条「腌制1小时/冷藏1小时/泡发40分钟/常温静置1小时」
    重新挡在当天（lb_probe#4 的 gap 188.9% 就是被「腌制1小时」顶出来的）；
    下调到 20 会把「冷藏30分钟即可食用」这类**末步收尾**也推进 t<0，
    整道菜从当天消失的从 2 道涨到 5 道，makespan 被刷掉。
    """
    from app.scheduler import prep as _p
    assert _p.PREP_AHEAD_MIN == 30.0, (
        f"PREP_AHEAD_MIN={_p.PREP_AHEAD_MIN} —— 偏离 30 必须重跑阈值扫描并写明依据"
    )


def test_common_marinades_are_prep_able():
    """40~60 分钟的腌制/冷藏/泡发必须能提前 —— 它们是最常见的备菜等待。

    旧门槛 90 分钟把它们全挡在当天。最差用例（lb_probe seed=11 #4, gap 188.9%）
    就是被「金银蒜蒸鸡胸肉#3 腌制1小时」顶出来的：它 0→60 占着当天，
    蒸箱只能等到 60 分钟才开火，而 LB 只有 27 分钟。
    """
    from app.spec.adapter import get
    from app.scheduler.prep import prep_keys
    from app.agent.priors import get_priors

    cases = [
        ("金银蒜蒸鸡胸肉", 3),      # 加入蒸鱼豉油…腌制1小时
        ("嫩烤鸡胸肉", 1),          # 腌制1h
        ("广式月饼（台式）", 2),    # 常温静置1小时
        ("苹果芒果派", 6),          # 放冰箱冷藏1小时
        ("拿破仑酥", 7),            # 放入冰箱冷冻1小时
        ("皂角银耳羹", 1),          # 泡发40分钟
    ]
    bad = []
    for nm, sid in cases:
        r = get(nm)
        assert r, f"官方菜谱缺失: {nm}"
        rec = parse_recipe(nm, r["ingredients"], r["steps"])
        ids = {i for d, i in prep_keys([rec], get_priors([rec]), allow_preprocess=True)}
        if sid not in ids:
            bad.append(f"{nm}#{sid}")
    assert not bad, f"这些常见备菜等待没能提前（门槛被调回 90 了？）: {bad}"
