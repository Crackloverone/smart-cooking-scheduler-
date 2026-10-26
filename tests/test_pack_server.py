"""服务器部署包的护栏。

钉的是三件"错了不会报错、只会让对方悄悄用不上"的事：

1. **systemd 默认必须开着模型**。写 off 的话，出厂层 `llm_defaults.json` 里的
   凭据一个都用不上 —— 界面显示"模型已关闭"，但排程照跑、耗时照旧，
   日志里也没有任何错误。看界面完全查不出原因。
2. **出厂层必须真带着凭据**。`llm.enabled()` 在 openai 档下没 key 直接返回 False。
3. **部署脚本必须 LF**。CRLF 在 Linux 上会让 `#!/bin/bash` 变成 `#!/bin/bash^M`，
   报错是 `bad interpreter`，很容易误判成"服务器没装 bash"。

外加两条"防漂移"：排除清单必须与 sync_publish 同源；publish/ 必须与源码一致。
"""
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"

# 模型开关的四个环境变量 —— 少一个就会有一路 AI 静默失效
LLM_FLAGS = ("FT_LLM", "FT_AGENT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM")


def _service_text() -> str:
    return (DEPLOY / "cooking-agent.service").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- 1
@pytest.mark.parametrize("flag", LLM_FLAGS)
def test_systemd默认开启模型(flag):
    """四个开关默认都必须是 on，否则"解包即可用"是假的。

    历史上这里写的是四个 off —— 排程与耗时完全正常（语义先验走离线缓存），
    于是"部署成功了但 AI 不动"这件事**没有任何报错能指向它**。
    """
    m = re.search(rf"^Environment={flag}=(\S+)$", _service_text(), re.M)
    assert m, f"systemd 里找不到 Environment={flag} 这一行"
    assert m.group(1) == "on", (
        f"{flag} 默认应为 on（出厂层凭据要生效），实际 {m.group(1)}。"
        f"想关掉请用 server-setup.sh 的 WITH_LLM=0，不要改默认值")


def test_systemd里没有遗留的off行():
    """防止"加了一行新的 off"或"改了一半"。"""
    offs = [l.strip() for l in _service_text().splitlines()
            if re.match(r"^Environment=FT_(AGENT_|WARM_|NOTIFY_)?LLM=off$", l.strip())]
    assert not offs, f"systemd 里还有写死的 off，会让模型整路失效：{offs}"


# --------------------------------------------------------------------------- 2
def test_出厂层带着可用的凭据():
    """provider=openai 时必须有 api_key —— 否则 enabled() 直接 False。"""
    p = ROOT / "app" / "agent" / "llm_defaults.json"
    assert p.is_file(), "缺少出厂层配置 app/agent/llm_defaults.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    provider = str(d.get("provider") or "").strip()
    assert provider, "出厂层没有 provider —— 等于空配置"
    if provider == "openai":
        key = str((d.get("remote") or {}).get("api_key") or "").strip()
        assert key, ("provider=openai 但 remote.api_key 为空 —— "
                     "llm.enabled() 会直接返回 False，界面只显示「模型已关闭」")
        assert str((d.get("remote") or {}).get("base_url") or "").strip(), \
            "provider=openai 但 remote.base_url 为空"


def test_出厂层在发布目录里():
    """sync_publish 会带上 app/ 全量；但这条把"必须在"钉成可查的事实。"""
    assert (ROOT / "publish" / "app" / "agent" / "llm_defaults.json").is_file(), \
        "publish/ 里没有出厂层配置 —— 发出去的包会用不上模型"


# --------------------------------------------------------------------------- 3
@pytest.mark.parametrize("rel", ["server-setup.sh", "upload.sh", "pack-server.sh",
                                 "cooking-agent.service", "nginx-cooking-agent.conf"])
def test_部署脚本必须是LF(rel):
    """CRLF 在 Linux 上 = bad interpreter。Windows 上编辑最容易引入。"""
    raw = (DEPLOY / rel).read_bytes()
    assert b"\r" not in raw, f"deploy/{rel} 含 CR —— 在 Linux 上会跑不起来，必须转 LF"


# --------------------------------------------------------------------------- 4
def test_排除清单与sync_publish同源():
    """两份清单抄来抄去必然漂移；必须 import 同一份。"""
    from scripts.pack_server import FORBIDDEN_NAMES
    from scripts.sync_publish import SKIP_NAMES
    assert FORBIDDEN_NAMES == SKIP_NAMES, \
        "pack_server 的排除清单与 sync_publish 不一致 —— 会出现'publish 排除了、包却带上了'"


def test_排除清单覆盖五份本机状态():
    for n in ("llm_settings.json", "devices_user.json", "recipes_user.json",
              "schedules_user.json", "exec_user.json"):
        assert n in __import__("scripts.sync_publish", fromlist=["SKIP_NAMES"]).SKIP_NAMES, \
            f"{n} 不在排除清单里 —— 开发机的本机状态会被打进服务器包"


# --------------------------------------------------------------------------- 5
def test_打包脚本会在校验失败时中止():
    """fail-closed：校验不过就不许产出包。

    判据落在**函数**上：`_verify` 里任一条不满足都抛 SystemExit。
    这里钉"它确实会 raise"而不是"它打印了一行字"。
    """
    from scripts import pack_server
    import tempfile
    # 造一个缺了出厂层的 staging 目录
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        (stage / "app").mkdir()
        # 只放一个无关文件，必要文件全缺
        (stage / "app" / "x.py").write_text("# nothing", encoding="utf-8")
        with pytest.raises(SystemExit) as e:
            pack_server._verify(stage)
        assert e.value.code != 0


def test_出厂层凭据缺失也会被拦下():
    """provider=openai 但没 key —— 这种包能正常解包启动，只是 AI 全程不动。"""
    from scripts import pack_server
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        for rel in pack_server.MUST_HAVE:
            p = stage / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if rel.endswith("llm_defaults.json"):
                p.write_text(json.dumps({"provider": "openai", "remote": {"api_key": ""}}),
                             encoding="utf-8")
            else:
                p.write_text("x", encoding="utf-8")
        with pytest.raises(SystemExit):
            pack_server._verify(stage)
