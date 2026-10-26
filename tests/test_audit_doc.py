"""`scripts/audit_doc.py` 的自检 —— 重点守"审计脚本自己别误报"。

背景
----
5.1 里原来有一条断言：

```python
if ct["endTime"] > ov["finishTime"]:      # 比的是 "HH:MM" 字符串
    bad["finish_lt_end"] += 1
```

`"23:44" > "00:04"` 在**字典序**上成立。于是只要排程满足
「整桌跨过零点、但某道菜在零点前就收尾」，这条断言就会把
"菜品 23:44 收尾、整桌次日 00:04 上桌" 判成 "菜品比上桌还晚" —— 而排程完全正确。

实测触发窗口（seed=42 的那 40 个场景，`now` 取不同时刻）：

| 冻结时刻 | 跨零点场景 | 旧口径误报 |
|---|---|---|
| 20:00 | 0 | 0 |
| 21:00 | 1 | 0 |
| 22:00 | 0 | 0 |
| 22:30 | 1 | 2 |
| 23:00 | 9 | 1 |
| **23:15** | **10** | **4** |
| 23:30 | 11 | 1 |
| 23:45 | 19 | 0 |

也就是说这个审计的结论**取决于你几点跑它** —— 白天全绿、晚上报假缺陷。
这正是项目里那条教训：**护栏一假报警，真回归也会被无视**。
（真实发生：2026-10-03 21:58 跑 `audit_doc` 报出 3 条 `finish_lt_end`，
排查后确认排程正确、是审计自己的字符串比较跨了零点。）

⚠ **这张表会随排程变化而失效**：2026-10-04 改保热基准后，22:00 从"3 条误报"退化成
"0 个跨零点场景"，这条测试当场变红 —— 不是审计坏了，是**排程变短了**。
所以这里不再只钉死单一时刻：先试 `_TRIGGER`，造不出场景就自动扫候选时刻，
并断言"**确实存在某个时刻能让旧口径误报**"（这才是这条回归测试真正要守的性质）。
"""

from __future__ import annotations

import random
from datetime import datetime

from app.spec.adapter import all_names, build_plan
from scripts.audit_doc import _hm_to_min, audit_51, RESULT

# 首选触发时刻（实测跨零点 10 个场景 / 旧口径误报 4 条）
_TRIGGER = datetime(2026, 10, 3, 23, 15, 0)

# 候选池：首选是实测最优(跨零点 10 个场景 / 旧口径误报 4 条)；排程一变它就可能失效，
# 其余按"离零点近"排序作回退 —— 每个候选要跑 40 组排程，所以首选放在最前面。
_TRIGGER_CANDIDATES = (
    datetime(2026, 10, 3, 23, 15, 0), datetime(2026, 10, 3, 23, 30, 0),
    datetime(2026, 10, 3, 23, 45, 0), datetime(2026, 10, 3, 23, 0, 0),
    datetime(2026, 10, 3, 22, 30, 0), datetime(2026, 10, 3, 22, 0, 0),
    datetime(2026, 10, 3, 21, 0, 0),
)


def _trigger(monkeypatch) -> datetime:
    """挑一个能同时造出「跨零点场景 + 旧口径误报」的冻结时刻。

    不写死单一时刻的理由见模块 docstring 的 ⚠：排程一变，某个时刻就可能失效，
    而"测试前提不成立"的红灯会让人误以为审计坏了。这里改成**先试首选、再扫候选**，
    最终仍然断言"这个误报确实可达" —— 前提消失时必须报错，不能静默跳过。
    """
    for when in _TRIGGER_CANDIDATES:
        crossed, old_bad, new_bad = _scan(monkeypatch, when)
        if crossed > 0 and old_bad > 0:
            assert new_bad == 0, f"{when:%H:%M} 新口径仍有 {new_bad} 条误报"
            return when
    raise AssertionError(
        "没有任何候选时刻能造出旧口径误报 —— 这批场景或排程已变，"
        "请重新挑一个跨零点的时刻，否则这条回归测试已经失去意义"
    )


def _freeze(monkeypatch, when: datetime) -> None:
    """把 adapter 里的 `datetime.now()` 钉在 `when`。"""
    import app.spec.adapter as adapter

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):        # noqa: D102
            return when

    monkeypatch.setattr(adapter, "datetime", _FrozenDatetime)


def _old_check(ct: dict, ov: dict) -> bool:
    """修复前的判据（字符串比较）—— 只用于负向对照。"""
    return ct["endTime"] > ov["finishTime"]


def _new_check(ct: dict, ov: dict) -> bool:
    """修复后的判据（顺时针距离）—— 与 audit_51 里保持一致。"""
    delta = (_hm_to_min(ov["finishTime"]) - _hm_to_min(ct["endTime"])) % 1440
    return delta > int(ov["timeSpent"])


def _scan(monkeypatch, when: datetime) -> tuple[int, int, int]:
    """跑审计用的同一批 40 个场景。返回 (跨零点场景数, 旧口径误报, 新口径误报)。"""
    _freeze(monkeypatch, when)
    random.seed(42)
    names = all_names()
    crossed = old_bad = new_bad = 0
    for _ in range(40):
        pick = random.sample(names, random.randint(2, 5))
        out = build_plan([{"id": i, "name": x} for i, x in enumerate(pick)])
        ov = out["overview"]
        starts = [ct["startTime"] for ct in out["cookingTimeline"]]
        # HH:MM 字符串"回绕"了 ⇒ 时间轴穿过 24:00
        if starts and ov["finishTime"] < min(starts):
            crossed += 1
        for ct in out["cookingTimeline"]:
            old_bad += _old_check(ct, ov)
            new_bad += _new_check(ct, ov)
    return crossed, old_bad, new_bad


# --------------------------------------------------------------- 纯算术，秒过
def test_hm_to_min_basics() -> None:
    assert _hm_to_min("00:00") == 0
    assert _hm_to_min("00:04") == 4
    assert _hm_to_min("23:44") == 23 * 60 + 44


def test_string_comparison_is_genuinely_wrong_across_midnight() -> None:
    """先证明"旧口径确实会错"，后面的测试才有意义。"""
    assert "23:44" > "00:04", "旧口径的字符串比较在跨零点时确实会误判"
    # 新口径算的是"顺时针距离"：23:44 → 次日 00:04 是 20 分钟
    assert (_hm_to_min("00:04") - _hm_to_min("23:44")) % 1440 == 20
    # 反过来：菜品 00:10 收尾、整桌 00:04 上桌 ⇒ 确实晚了，必须能报出来
    assert (_hm_to_min("00:04") - _hm_to_min("00:10")) % 1440 == 1434


# --------------------------------------------------------------- 负向对照
def test_the_false_alarm_is_reachable(monkeypatch) -> None:
    """确认真的存在某个时刻能让旧口径误报 —— 否则下面的测试是空转。"""
    when = _trigger(monkeypatch)
    crossed, old_bad, new_bad = _scan(monkeypatch, when)
    assert crossed > 0, f"冻结在 {when:%H:%M} 没能造出跨零点场景，测试前提不成立"
    assert old_bad > 0, (
        "旧口径在跨零点场景下竟然没误报 —— 说明这批场景变了，"
        "请重新找一个能触发旧口径误报的时刻，否则这条回归测试已经失去意义"
    )
    assert new_bad == 0, f"新口径在触发点上仍有 {new_bad} 条误报"


# --------------------------------------------------------------- 真跑被测函数
def test_real_audit_51_passes_when_plans_cross_midnight(monkeypatch) -> None:
    """真跑 `audit_51` 本体（不是复刻逻辑），确认它跨零点也是 ok。"""
    _freeze(monkeypatch, _trigger(monkeypatch))
    RESULT.clear()
    audit_51(all_names())
    rows = [r for r in RESULT if r[0].startswith("5.1")]
    assert rows, "audit_51 没有往 RESULT 里写结果"
    section, status, detail = rows[0]
    assert status == "ok", (
        f"{section} 判为 {status}：{detail} —— "
        f"跨零点时 audit_doc 会自己报假缺陷（排程本身是对的）"
    )
