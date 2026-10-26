# -*- coding: utf-8 -*-
"""步骤溯源护栏测试 —— 保证"甘特图上不会出现菜谱里没有的步骤"。

背景: v2.x 的 `_add_kitchen_semantics()` 会凭空合成「烟机联动」45 条 + 「预热」148 条,
而官方《接口规范》与 100 道菜谱里"烟机"出现 **0 次**、提到预热的只有 6 道菜。
用户反馈的"甘特图出现不存在的步骤"就是这个。

本文件做两件事:
  ① 断言官方 100 道菜全量排程后**零凭空步骤** (R1) 且**零非法合成行** (R2);
  ② 反向验证护栏**有牙齿** —— 故意重现旧的合成行为 / 注入假步骤, 必须被拦下。
     (只做正向断言的话, 护栏将来退化成恒真也没人发现。)

运行: python -m pytest tests/test_provenance.py -q
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["FT_LLM"] = "off"
os.environ["FT_AGENT_LLM"] = "off"

from app.models.schema import ScheduledTask                  # noqa: E402
from scripts import step_provenance as SP                    # noqa: E402


# ---------------- ① 正向: 官方库全量零违规 ----------------
def test_official_library_no_phantom_steps():
    """官方 100 道菜: 排程出的每一步都必须能在菜谱原文找到出处。"""
    res = SP.audit()
    assert res["checked"] == 100, f"应检查 100 道菜, 实际 {res['checked']}"
    assert res["violations"] == [], (
        "出现无法溯源到原文的步骤:\n" +
        "\n".join(f"  [{v['dish']}] {v['task_id']} :: {v['detail']}"
                  for v in res["violations"][:20]))


# ---------------- ② 反向: 护栏必须有牙齿 ----------------
def test_guardrail_catches_hood_synthesis(monkeypatch):
    """重现旧行为 (FT_HOOD=on 合成烟机联动行) -> 必须被判非法合成行。"""
    monkeypatch.setenv("FT_HOOD", "on")
    res = SP.audit(SP.all_names()[:12])
    kinds = {v["kind"] for v in res["violations"]}
    assert "非法合成行(R2)" in kinds, "护栏失效: 烟机联动合成行没有被拦下"
    assert any("烟机" in v["detail"] for v in res["violations"])


def test_guardrail_catches_injected_phantom_step(monkeypatch):
    """注入一条原文里不存在的步骤 -> R1 必须报警。"""
    real = SP.plan_recipes

    def _evil(recs, **kw):
        plan = real(recs, **kw)
        plan.tasks.append(ScheduledTask(
            task_id=f"{recs[0].name}#999", dish=recs[0].name, step_id=999,
            raw_text="这是菜谱里根本没有的一句话", device="烤箱",
            unit_id="烤箱-9", start_min=0.0, end_min=5.0, duration_min=5.0))
        return plan

    monkeypatch.setattr(SP, "plan_recipes", _evil)
    res = SP.audit(SP.all_names()[:3])
    assert any(v["kind"] == "凭空步骤(R1)" for v in res["violations"]), \
        "护栏失效: 凭空步骤没有被拦下"


def test_guardrail_catches_preheat_merge_without_owner(monkeypatch):
    """预热共享行必须说明服务于哪道菜 (dish_list 非空), 否则 R2 报警。"""
    real = SP.plan_recipes

    def _evil(recs, **kw):
        plan = real(recs, **kw)
        plan.tasks.append(ScheduledTask(
            task_id="预热共享@烤箱@0", dish="", step_id=0,
            raw_text="烤箱预热", device="烤箱", unit_id="烤箱-9",
            start_min=0.0, end_min=5.0, duration_min=5.0, dish_list=[]))
        return plan

    monkeypatch.setattr(SP, "plan_recipes", _evil)
    res = SP.audit(SP.all_names()[:3])
    assert any(v["kind"] == "预热共享无主(R2)" for v in res["violations"]), \
        "护栏失效: 无主预热共享行没有被拦下"


def test_guardrail_catches_vanished_real_step(monkeypatch):
    """R3: 真实加热步骤被"提前准备"整段删掉 -> 必须报警。

    这是实测抓到的事故: 精确模型轨把「半熟芝士蛋糕」#1~#8 (含两条**灶具**步) 整段删掉,
    甘特图上只剩 4 条烤箱步, 看上去就是"没做糊就直接烤"; 而它正是靠"少排 16 步"
    把 makespan 从 124 压到 82、出锅差从 20 压到 1。
    """
    real = SP.plan_recipes

    def _evil(recs, **kw):
        plan = real(recs, **kw)
        victim = recs[0].name
        plan.tasks = [t for t in plan.tasks
                      if not (t.dish == victim and t.device in SP.HEATING)]
        return plan

    monkeypatch.setattr(SP, "plan_recipes", _evil)
    res = SP.audit(SP.all_names()[:3], with_combos=False)
    assert any(v["kind"] == "真实步骤消失(R3)" for v in res["violations"]), \
        "护栏失效: 真实步骤被删掉却没有被拦下"


def test_guardrail_catches_dependency_violation(monkeypatch):
    """R4: 把某步挪到 t<0 而它的前置还在当天 -> 必须报警。"""
    real = SP.plan_recipes

    def _evil(recs, **kw):
        plan = real(recs, **kw)
        if len(plan.tasks) >= 2:
            t = plan.tasks[1]
            t.start_min = -999.0                  # 挪到它的前置之前
            t.end_min = -998.0
        return plan

    monkeypatch.setattr(SP, "plan_recipes", _evil)
    res = SP.audit(SP.all_names()[:3], with_combos=False)
    assert any(v["kind"] == "依赖违反(R4)" for v in res["violations"]), \
        "护栏失效: 依赖违反没有被拦下"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
