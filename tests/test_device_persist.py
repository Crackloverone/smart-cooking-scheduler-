"""设备容量的**用户层**持久化（`data/devices_user.json`）。

为什么值得单独立护栏
--------------------
`_DEVICE_CFG` 是进程内存，重启就回到默认 2/1/1。而"选择厨房预设"是用户对
**这台机器上有什么设备**的陈述 —— 重启后偷偷变回去，排出来的计划与他刚选的
厨房不符，界面上却看不出任何异常（只有"怎么又不对了"）。

两层数据纪律与菜谱库一致：`data/device_presets.json` 是只读基线（预设），
用户改动只写 `data/devices_user.json`；后者不进发布包。

⚠ 隔离：`tests/conftest.py` 在 import 期把 `FT_DEVICES_FILE` 钉到一个不存在的
   路径，所以整个套件默认永远读不到开发机上的真实配置。本文件每条用例再各自
   把 `main._DEVICES_PATH` monkeypatch 到 `tmp_path`。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_CFG = {"灶眼数": 2, "蒸箱": 1, "烤箱": 1, "冰箱": 2}   # 冰箱 = 温区数（冷藏+冷冻）


@pytest.fixture
def dev_env(tmp_path, monkeypatch):
    """把用户层文件指到 tmp_path，并在用例结束后复原全局设备状态。

    ⚠ `_DEVICE_CFG` 与 `engine.RESOURCE_UNITS` 都是**模块级可变字典**。用例改完
      不复原的话，后面所有排程用例都跑在被改过的厨房上 —— 报错会指向"某个排出来
      的结果不对"，没人会想到是这里漏了复原。
    """
    from app.api import main
    from app.scheduler import engine

    monkeypatch.setattr(main, "_DEVICES_PATH", str(tmp_path / "devices_user.json"))
    cfg0 = dict(main._DEVICE_CFG)
    res0 = dict(engine.RESOURCE_UNITS)
    try:
        yield main, engine
    finally:
        main._DEVICE_CFG.clear()
        main._DEVICE_CFG.update(cfg0)
        engine.RESOURCE_UNITS.clear()
        engine.RESOURCE_UNITS.update(res0)


def test_设备配置落盘后能被重新加载(dev_env):
    main, engine = dev_env
    out = main.set_devices({"灶眼数": 3, "蒸箱": 2, "烤箱": 2, "冰箱": 2})
    assert out["灶眼数"] == 3

    path = main._DEVICES_PATH
    assert os.path.exists(path), "set_devices 没有落盘 —— 重启后用户选的厨房就没了"
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"灶眼数": 3, "蒸箱": 2, "烤箱": 2, "冰箱": 2}

    # 模拟重启：先把内存与引擎都恢复成默认，再走一次加载
    main._DEVICE_CFG.update(DEFAULT_CFG)
    engine.RESOURCE_UNITS[engine.DEVICE_STOVE] = 2
    main._load_devices_user()

    assert main._DEVICE_CFG["灶眼数"] == 3
    assert engine.RESOURCE_UNITS[engine.DEVICE_STOVE] == 3, (
        "只改了 _DEVICE_CFG 没同步引擎容量 —— 界面显示 3 灶眼、排程还按 2 个算"
    )
    assert engine.RESOURCE_UNITS[engine.DEVICE_STEAMER] == 2


def test_用户层里的非法值被忽略而不是灌进引擎(dev_env):
    """手改过的文件可能带超范围的值；直接灌进 RESOURCE_UNITS 会撑爆 CP-SAT 规模。"""
    main, engine = dev_env
    with open(main._DEVICES_PATH, "w", encoding="utf-8") as f:
        json.dump({"灶眼数": 9999, "蒸箱": 2, "说明": "手改的"}, f)
    main._load_devices_user()

    assert main._DEVICE_CFG["灶眼数"] == 2, "超范围的值被灌进了配置"
    assert main._DEVICE_CFG["蒸箱"] == 2, "合法项应当照常生效"
    assert engine.RESOURCE_UNITS[engine.DEVICE_STOVE] == 2


def test_损坏的用户层不致命(dev_env):
    """半截 JSON / 形状不对：一律按"没有用户配置"处理，绝不能把服务拖起来不了。"""
    main, _ = dev_env
    with open(main._DEVICES_PATH, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    main._load_devices_user()                       # 不许抛
    assert main._DEVICE_CFG["灶眼数"] == 2

    with open(main._DEVICES_PATH, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)                     # 不是 dict
    main._load_devices_user()
    assert main._DEVICE_CFG["灶眼数"] == 2


def test_落盘不留临时文件(dev_env):
    """原子写：`.tmp` 必须被 `os.replace` 吃掉，不能留在 data/ 里。"""
    main, _ = dev_env
    main.set_devices({"灶眼数": 2})
    leftovers = [p for p in os.listdir(os.path.dirname(main._DEVICES_PATH))
                 if p.endswith(".tmp")]
    assert leftovers == [], f"原子写留下了临时文件: {leftovers}"


def test_设备用户层不进发布目录(sync_skip_names):
    """`devices_user.json` 是**本机状态**（这台机器上有什么设备），不是产品数据。

    跟着进包的话，接收方一解压就是开发机上的厨房 —— 与他家完全不符，而界面上
    看不出这是"别人家的配置"。预设基线 `device_presets.json` 仍照常随包走。

    ⚠ 这里**不能**写 `from scripts import sync_publish`：`_mutate_deploy_guards.py`
      把 `sync_publish.py` 拷到副本再变异，import 读的是真文件 ⇒ 变异永远不生效
      ⇒ 护栏空转（假绿）。`sync_skip_names` fixture 会优先读副本。
    """
    SKIP = sync_skip_names

    assert "devices_user.json" in SKIP
    assert "llm_settings.json" in SKIP, "旧的那条不能被顶掉"
    assert "device_presets.json" not in SKIP, (
        "预设基线是产品数据，必须随包走"
    )
    # 菜谱库是**同一套两层纪律**的第三份：官方 CSV 随包走，用户层不随包走。
    # 进包的话接收方的 100 道官方菜里会静默混进别人的改稿 —— `overrides` 按 id
    # 覆盖官方菜，界面上看不出被换过，也回不到官方版本（`restore()` 只恢复"被删除"的菜）。
    assert "recipes_user.json" in SKIP, (
        "菜谱库的用户层是本机状态，不能进发布包"
    )
    assert "recipes_official_100.csv" not in SKIP, (
        "官方菜谱基线是产品数据，必须随包走"
    )


def test_测试环境不会读到开发机的厨房配置():
    """conftest 的隔离开关必须真的生效。

    没有它的话：只要开发机上有人从界面改过一次设备，`data/devices_user.json`
    就被创建；此后每次 pytest 都带着那份配置启动，所有"默认 2 灶眼"的断言
    **静默**变味，而报错完全指不到这里。
    """
    from app.api import main

    assert os.environ.get("FT_DEVICES_FILE"), "conftest 没有设 FT_DEVICES_FILE"
    assert not os.path.exists(main._DEVICES_PATH), (
        f"测试期读到了真实设备配置: {main._DEVICES_PATH} —— 隔离失效"
    )


# ------------------------------------------------------- 恢复默认（界面上的按钮）
def test_恢复默认回到出厂值并删掉用户层(dev_env):
    """「恢复默认」= 内存 + 引擎 + 用户层文件**三者一起**回到出厂状态。

    只改内存不删文件：重启后 `_load_devices_user()` 又读回旧配置，用户会以为
    按钮没生效（而且界面上看不出为什么）。
    只删文件不改内存：当前这次进程里排程仍按旧设备算，得重启才对 —— 同样离谱。
    """
    main, engine = dev_env
    assert main._DEFAULT_DEVICE_CFG == DEFAULT_CFG, (
        "出厂默认被改过了；本文件的 DEFAULT_CFG 与 main._DEFAULT_DEVICE_CFG 必须一致"
    )
    main.set_devices({"灶眼数": 3, "蒸箱": 2, "烤箱": 2, "冰箱": 2})
    assert os.path.exists(main._DEVICES_PATH)

    out = main.reset_devices()

    assert out == DEFAULT_CFG, f"返回的配置不是出厂默认: {out}"
    assert main._DEVICE_CFG == DEFAULT_CFG, "内存里的设备配置没回到默认"
    assert engine.RESOURCE_UNITS[engine.DEVICE_STOVE] == DEFAULT_CFG["灶眼数"]
    assert engine.RESOURCE_UNITS[engine.DEVICE_STEAMER] == DEFAULT_CFG["蒸箱"]
    assert engine.RESOURCE_UNITS[engine.DEVICE_OVEN] == DEFAULT_CFG["烤箱"]
    assert engine.RESOURCE_UNITS[engine.DEVICE_FRIDGE] == DEFAULT_CFG["冰箱"]
    assert not os.path.exists(main._DEVICES_PATH), (
        "用户层文件还在 —— 重启后又读回旧配置，「恢复默认」等于没做"
    )


def test_恢复默认没有用户层时也不报错(dev_env):
    """从没改过厨房的机器上点「恢复默认」，不该抛 FileNotFoundError 变成 500。"""
    main, _ = dev_env
    assert not os.path.exists(main._DEVICES_PATH)
    assert main.reset_devices() == DEFAULT_CFG          # 不许抛


def test_恢复默认端点已注册():
    """前端点「恢复默认」调的是 `POST /api/devices/reset`，路由必须真的存在。

    ⚠ 只断言函数存在是不够的：漏了 `@app.post(...)` 装饰器时函数照样在，
      而按钮点下去是 404 —— 界面上只表现为"点了没反应"。
    """
    from app.api.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/devices/reset" in paths, "重置端点没注册 —— 前端按钮点了会 404"
