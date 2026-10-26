"""设备参数合法性护栏 —— 对齐设计文档 3.1.3「参数合法性」。

为什么单独一个文件
------------------
`app/spec/device_spec.py::is_legal()` 的 docstring 明写「供自检/测试用」，
但**全仓库没有任何自检或测试调用它** —— 一个专门为验证而写的函数，写完之后
全程没人调。这类"半成品"最危险：它看起来像"参数合法性已经验过了"，
实际是零覆盖。同理 `legal_modes()` 也从未被引用。

判据是**闭包性质**，不是逐个举例
--------------------------------
    clamp_params / fit_mode 的输出，必须永远能被 is_legal 判为合法。

举例只能证明"这几个输入对"；闭包能证明"任何输入都对"。所以这里遍历的是
`设备 × 各种模式(含非法) × 各种温度(含越界) × 各种时长(含越界)` 的**笛卡尔积**，
外加一条真链路断言（官方 100 道菜解析出的每一步都必须合法）。
"""
from __future__ import annotations

import itertools

import pytest

from app.spec.device_spec import (
    DEVICE_SPEC,
    clamp_params,
    default_mode,
    default_temp,
    fit_mode,
    is_legal,
    legal_modes,
)

DEVICES = sorted(DEVICE_SPEC)

# 越界样本刻意包含 None / 负数 / 0 / 极小 / 极大 —— 覆盖两种真实来源：
#   "解析器没读到参数"(None) 与 "解析器读错了参数"(0 / 越界值)。
TEMPS = [None, -50.0, -1.0, 0.0, 1.0, 25.0, 90.0, 100.0, 121.0, 180.0, 230.0, 250.0, 999.0]
DURATIONS = [None, -10.0, 0.0, 1.0, 5.0, 60.0, 300.0, 301.0, 1441.0, 99999.0]
# "标准模式" 是历史坑：它不在官方枚举里，`default_mode()` 的注释专门警告过
# "回落到它会又造一个非法参数"，所以必须把它当非法输入来压。
BAD_MODES = ["", "标准模式", "不存在的模式", "  ", "爆炒", "普通蒸"]


@pytest.mark.parametrize("device", DEVICES)
def test_clamp_params_output_is_always_legal(device: str) -> None:
    """核心闭包性质：钳制后的参数必须永远合法。"""
    for mode, temp, dur in itertools.product(BAD_MODES, TEMPS, DURATIONS):
        m, t, d = clamp_params(device, mode, temp, dur)
        assert is_legal(device, m, t, d), (
            f"clamp_params({device!r}, {mode!r}, {temp!r}, {dur!r}) "
            f"→ ({m!r}, {t!r}, {d!r}) 被判为非法参数")


@pytest.mark.parametrize("device", DEVICES)
def test_fit_mode_never_invents_an_illegal_mode(device: str) -> None:
    """fit_mode 的兜底必须落在合法模式表里，不能凭默认值造一个新枚举值。"""
    modes = legal_modes(device)
    for mode, temp in itertools.product(BAD_MODES, TEMPS):
        m = fit_mode(device, mode, temp)
        assert m in modes, (
            f"fit_mode({device!r}, {mode!r}, {temp!r}) → {m!r} 不在合法模式表里 {modes}")


@pytest.mark.parametrize("device", DEVICES)
def test_every_device_has_a_legal_default_mode(device: str) -> None:
    """默认模式自身必须合法 —— 否则 fit_mode 的兜底会直接产出非法值。"""
    modes = legal_modes(device)
    assert modes, f"{device} 没有任何合法模式"
    assert default_mode(device) in modes, (
        f"{device} 的默认模式 {default_mode(device)!r} 不在自己的合法模式表里 {modes}")


@pytest.mark.parametrize("device", DEVICES)
def test_default_temp_falls_inside_its_own_mode_range(device: str) -> None:
    """默认温度必须落在默认模式的温度区间内（否则"默认值"一出生就越界）。"""
    t = default_temp(device)
    if t is None:
        return
    tlo, thi, _dlo, _dhi = DEVICE_SPEC[device]["modes"][default_mode(device)]
    if tlo is None:
        return
    assert tlo <= t <= thi, (
        f"{device} 默认温度 {t} 不在 {default_mode(device)!r} 的区间 [{tlo}, {thi}] 内")


@pytest.mark.parametrize("device", DEVICES)
def test_device_spec_ranges_are_self_consistent(device: str) -> None:
    """能力表本身不能自相矛盾（区间反了会让 _clamp 把值钳到错误的一侧）。"""
    for mode, (tlo, thi, dlo, dhi) in DEVICE_SPEC[device]["modes"].items():
        if tlo is not None:
            assert tlo <= thi, f"{device}/{mode} 温度区间反了: {tlo} > {thi}"
        assert dlo <= dhi, f"{device}/{mode} 时长区间反了: {dlo} > {dhi}"
        assert dlo >= 0, f"{device}/{mode} 时长下限为负: {dlo}"


def test_unknown_device_passes_through_instead_of_crashing() -> None:
    """没有能力模型的设备（含"人工"）必须原样透传，且不判为非法。"""
    assert clamp_params("不存在的设备", "x", 12.0, 34.0) == ("x", 12.0, 34.0)
    assert is_legal("不存在的设备", "x", 12.0, 34.0) is True
    assert legal_modes("不存在的设备") == []
    assert fit_mode("不存在的设备", "", None) == ""


def test_parsed_official_recipes_are_parameter_legal() -> None:
    """真链路落地验证：官方 100 道菜解析出的每一步，参数都必须合法。

    前面几条证明 `clamp_params` 自身自洽；这条证明**解析器真的把它的输出用上了**。
    少了这条，"闭包性质"只是在测一个没人调的函数。
    """
    from app.parser.recipe_parser import parse_recipe
    from app.spec.adapter import all_names, get

    bad = []
    for name in all_names():
        rec = get(name)
        if not rec:
            continue
        for s in parse_recipe(name, rec["ingredients"], rec["steps"]).steps:
            if not is_legal(s.device, s.mode, s.temperature_c, s.duration_min):
                bad.append(f"{name}#{s.step_id} {s.device}/{s.mode} "
                           f"{s.temperature_c}℃ {s.duration_min}min")
    assert not bad, ("解析出的步骤含非法设备参数:\n  " + "\n  ".join(bad[:20])
                     + (f"\n  … 共 {len(bad)} 条" if len(bad) > 20 else ""))


def test_device_qty_limits_have_a_single_source():
    """数量上下限必须只有一个真源：`main._DEVICE_LIMITS`。

    设置页的数量框曾经把 `max="8"` 写死，而后端蒸箱/烤箱/冰箱的上限是 4 ——
    用户在界面上填 8 是**合法操作**（浏览器允许），提交才被后端判非法。
    同一件事有两份真源，改一处必然漏另一处。现在上下限由 `/api/device_spec`
    下发（`qty_min` / `qty_max`），这里钉住"下发值 == 校验值"。
    """
    from app.api import main as main_mod

    spec = main_mod.get_device_spec()["devices"]
    assert spec, "/api/device_spec 没有返回任何设备"
    for label, s in spec.items():
        assert label in main_mod._DEVICE_LIMITS, f"{label} 不在 _DEVICE_LIMITS 里"
        assert (s["qty_min"], s["qty_max"]) == main_mod._DEVICE_LIMITS[label], (
            f"{label} 下发的数量上限 {s['qty_max']} 与校验用的 "
            f"{main_mod._DEVICE_LIMITS[label][1]} 不一致"
        )
