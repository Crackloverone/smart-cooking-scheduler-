"""v3.9 执行态回写 + 经验回流（时长校准）护栏。

★ 这两个功能补的是 Agent 闭环里唯一缺失的那一跳: 排程历史存的是**计划**,
  不是**执行结果**。系统原来排完就失明, 重排只能靠用户手选「延迟」。

⚠⚠ 本文件最重要的一条是 `test_关档下时长估计与旧实现逐位一致` ——
   校准模块一旦在关档时"偷偷生效"，整批排程断言会集体变味，而报错指向的是
   排程结果，没人会想到去看 `data/exec_user.json`。所以这条是**基线守门**。

其余各条盯的是"学习"这事的边界：
   · 默认不许学（关档）
   · 样本不够不许学
   · 学出来的系数不许跑出 [0.5, 2.0]
   · 原文写了时长的步骤不许被学出来的系数覆盖（那是事实不是估计）
"""

import os

import pytest

from app.scheduler import calib, exec_state
from app.scheduler.engine import bucket_of, estimate_duration


class _S:
    """最小步骤桩 —— 只带 estimate_duration 会读的四个字段。"""

    def __init__(self, duration_min=None, device="", prep_type=None, raw_text=""):
        self.duration_min = duration_min
        self.device = device
        self.prep_type = prep_type
        self.raw_text = raw_text


# 七档兜底值（关档下的期望值，逐条对照旧实现）
_BASE_CASES = [
    ("蒸箱", "",        "",            8.0,  "steamer"),
    ("烤箱", "",        "",            8.0,  "oven"),
    ("灶具", "",        "大火烧热",     3.0,  "stove_short"),
    ("灶具", "",        "下锅翻炒",     8.0,  "stove"),
    ("冰箱", "",        "",           30.0,  "fridge"),
    ("人工", "切配",     "",            2.0,  "prep_cut"),
    ("人工", "",        "",            3.0,  "manual"),
    ("人工", "上桌",     "",            1.0,  "plate"),
]


def _seed(samples: dict) -> None:
    """直接把聚合样本写进(临时目录里的)执行态文件。"""
    d = exec_state._empty()
    d["samples"] = samples
    exec_state._write(d)
    calib.invalidate()


@pytest.fixture(autouse=True)
def _clean_state():
    """每条用例前后都清干净 —— 样本会跨用例污染系数。

    ⚠ teardown 里必须 `reload_flag()`：开档用例用 monkeypatch 改了 `FT_CALIB` 之后
    调了 `reload_flag()`，而 `calib._ENABLED` 是**模块级变量**，monkeypatch 撤环境
    变量并不会把它改回来。漏了这一句 ⇒ 本文件跑完之后**整批排程测试都带着校准跑**
    （报错会指向排程结果，没人会想到是这个文件干的）。
    """
    exec_state.clear()
    calib.invalidate()
    yield
    exec_state.clear()
    calib.invalidate()
    os.environ.pop("FT_CALIB", None)      # 不依赖与 monkeypatch 的 teardown 顺序
    calib.reload_flag()


# ---------------------------------------------------------------------------
# 一、基线守门：关档下必须与旧实现逐位一致
# ---------------------------------------------------------------------------
def test_关档下时长估计与旧实现逐位一致():
    assert not calib.enabled(), "FT_CALIB 默认必须是关的（保护 OPTIMAL 40/40 基线）"
    for dev, pt, rt, want, bk in _BASE_CASES:
        st = _S(None, dev, pt, rt)
        assert estimate_duration(st) == want, f"{dev}/{rt} 兜底值被改了"
        assert bucket_of(st) == bk, f"{dev}/{rt} 档位键不对"


def test_关档下哪怕有足够样本也不生效():
    _seed({"stove": {"n": 20, "sum_planned": 100.0, "sum_actual": 300.0}})
    assert estimate_duration(_S(None, "灶具", "", "下锅翻炒")) == 8.0


def test_显式时长永远不被校准覆盖(monkeypatch):
    """原文写了「蒸 15 分钟」是事实 —— 再准的统计也不能盖掉它。"""
    monkeypatch.setenv("FT_CALIB", "on")
    calib.reload_flag()
    _seed({"steamer": {"n": 20, "sum_planned": 100.0, "sum_actual": 400.0}})  # 系数 4.0
    assert estimate_duration(_S(15.0, "蒸箱", "", "")) == 15.0
    # 同一档位的**无时长**步骤则确实被校准（限幅后取 2.0）
    assert estimate_duration(_S(None, "蒸箱", "", "")) == 16.0


# ---------------------------------------------------------------------------
# 二、学习的边界
# ---------------------------------------------------------------------------
def test_样本不足时不生效(monkeypatch):
    monkeypatch.setenv("FT_CALIB", "on")
    calib.reload_flag()
    _seed({"stove": {"n": 4, "sum_planned": 40.0, "sum_actual": 80.0}})   # 差一个样本
    assert estimate_duration(_S(None, "灶具", "", "下锅翻炒")) == 8.0


def test_样本够了按和算比值(monkeypatch):
    """ratio = Σ实际 / Σ预估 —— 不是"比值再平均"，否则 1 分钟与 30 分钟等权。"""
    monkeypatch.setenv("FT_CALIB", "on")
    calib.reload_flag()
    _seed({"stove": {"n": 6, "sum_planned": 60.0, "sum_actual": 90.0}})   # 1.5
    assert estimate_duration(_S(None, "灶具", "", "下锅翻炒")) == 12.0


@pytest.mark.parametrize("ratio,want", [(5.0, 16.0), (0.1, 4.0)])
def test_系数被限幅在_0_5_到_2_0(monkeypatch, ratio, want):
    """单次异常值（切菜时接了个电话）不该把整个档位抬走。"""
    monkeypatch.setenv("FT_CALIB", "on")
    calib.reload_flag()
    _seed({"stove": {"n": 6, "sum_planned": 60.0, "sum_actual": 60.0 * ratio}})
    assert estimate_duration(_S(None, "灶具", "", "下锅翻炒")) == want


def test_蒸箱与烤箱是两个档位互不污染(monkeypatch):
    """两者都兜底 8 分钟，但真实分布不同 —— 合在一起会互相带偏。"""
    monkeypatch.setenv("FT_CALIB", "on")
    calib.reload_flag()
    _seed({"steamer": {"n": 6, "sum_planned": 60.0, "sum_actual": 90.0}})  # 蒸箱 1.5
    assert estimate_duration(_S(None, "蒸箱", "", "")) == 12.0
    assert estimate_duration(_S(None, "烤箱", "", "")) == 8.0


def test_校准读不到样本时也不阻断排程(monkeypatch):
    """校准是增强不是依赖 —— 存储层炸了也必须照旧排程。"""
    monkeypatch.setenv("FT_CALIB", "on")
    calib.reload_flag()
    monkeypatch.setattr(exec_state, "samples", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert estimate_duration(_S(None, "蒸箱", "", "")) == 8.0


# ---------------------------------------------------------------------------
# 三、执行态与偏差判定
# ---------------------------------------------------------------------------
def test_偏差取最大单步而不是求和():
    """并行分支上的延迟**不叠加** —— 求和会把"3 道菜各晚 1 分钟"判成晚 3 分钟，
    触发一次毫无必要的重排。取最大值宁可低估（漏一次建议）也不要高估（噪音重排）。"""
    sid = "s1"
    exec_state.report(sid, "A#1", "delayed", planned=10.0, actual=13.0, bucket="stove")
    exec_state.report(sid, "A#2", "delayed", planned=10.0, actual=15.0, bucket="stove")
    dv = exec_state.deviation(sid)
    assert dv["delta_min"] == 5.0, f"应为 max(3,5)=5，实际 {dv['delta_min']}"
    assert dv["done"] == 0 and dv["total"] == 2


def test_没到门槛不建议重排():
    sid = "s1"
    exec_state.report(sid, "A#1", "delayed", planned=10.0, actual=12.0, bucket="stove")
    dv = exec_state.deviation(sid)
    assert dv["worth"] is False
    assert "重排门槛" in dv["reason"]


def test_重复上报不重复计数():
    """同一步点 5 次「完成」只应是 1 个样本 —— 否则一个人的手抖能带跑整档系数。"""
    sid = "s1"
    for _ in range(5):
        exec_state.report(sid, "A#1", "done", planned=10.0, actual=12.0, bucket="stove")
    assert exec_state.samples()["stove"]["n"] == 1


def test_没填实际时长按准时记而不是记零():
    """不填 ⇒ 按计划时长记。记 0 会把系数直接拉塌成"这步不要时间"。"""
    sid = "s1"
    exec_state.report(sid, "A#1", "done", planned=8.0, actual=None, bucket="stove")
    st = exec_state.get(sid)["steps"]["A#1"]
    assert st["actual"] == 8.0


def test_没有记录的_sid_返回空快照而不是报错():
    d = exec_state.get("不存在的sid")
    assert d["steps"] == {}
    assert exec_state.deviation("不存在的sid")["worth"] is False


def test_执行态文件损坏时当空历史且留证(monkeypatch, tmp_path):
    """坏文件 ⇒ 当空历史（不阻断启动），但必须**改名留证**而不是静默删掉。

    "我的记录全没了"这种事必须留下痕迹，否则连"到底坏过没有"都无从查起。
    """
    p = tmp_path / "exec_user.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    # 模块级 _PATH 是 import 期算的，环境变量这时候改没用 —— 直接换掉
    monkeypatch.setattr(exec_state, "_PATH", str(p))
    assert exec_state.get("x")["steps"] == {}
    assert (tmp_path / "exec_user.json.bad").exists(), "坏文件没有改名留证"


def test_原创档位不进发布包(sync_skip_names):
    """执行态记的是"这台机器上真实做菜花了多久"，发给别人没有意义；
    更糟的是 samples 会直接改兜底时长 —— 混进别人的数据等于悄悄改了排程结果。"""
    assert "exec_user.json" in sync_skip_names


def test_隔离开关存在():
    """没有 FT_EXEC_FILE，跑一次测试就会往开发机真文件里塞执行记录，
    进而改掉 estimate_duration 的返回值。"""
    p = (os.environ.get("FT_EXEC_FILE") or "").replace("\\", "/")
    assert p, "conftest 必须把 FT_EXEC_FILE 钉到临时目录"
    assert "/data/" not in p, f"FT_EXEC_FILE 指到了仓库的 data/ 下: {p}"


# ---------------------------------------------------------------------------
# 四、端点
# ---------------------------------------------------------------------------
def test_五个执行态端点都已注册():
    from app.api.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    for p in ("/api/exec/report", "/api/exec/{sid}", "/api/exec/suggest",
              "/api/exec", "/api/calib"):
        assert p in paths, f"端点没注册: {p}"


def test_非法执行状态被拒绝():
    from app.api.main import ExecReportIn, report_exec
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        report_exec(ExecReportIn(sid="s", task_id="A#1", state="bogus"))
    assert ei.value.status_code == 400


def test_缺_sid_或_task_id_被拒绝():
    from app.api.main import ExecReportIn, report_exec
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        report_exec(ExecReportIn(sid="", task_id="A#1", state="done"))


def test_重排建议只建议不改动计划():
    """`suggest` 必须只回评估结果 —— 自动换掉一份正在执行的方案比重排本身更糟。"""
    from app.api.main import ExecSidIn, suggest_replan
    sid = "s1"
    exec_state.report(sid, "A#1", "done", planned=10.0, actual=10.0, bucket="stove")
    exec_state.report(sid, "A#2", "delayed", planned=10.0, actual=20.0, bucket="stove")
    dv = suggest_replan(ExecSidIn(sid=sid))
    assert dv["worth"] is True
    # completed 给前端直接塞进 /reschedule，不用它自己再推断"哪些算做完"
    assert dv["completed"] == ["A#1"]
    assert "plan" not in dv, "suggest 不该返回新计划"
