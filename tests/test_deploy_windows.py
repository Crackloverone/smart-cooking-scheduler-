"""Windows 服务器部署脚本护栏（`deploy/windows/*.ps1`）。

为什么需要它
------------
这套脚本是**在别的机器上**跑的：开发机打包、服务器部署。本地跑得通不代表
服务器上跑得通，而失败现场往往看不到（服务器上只有一段看不懂的报错）。
本文件守的每一条，都是**真实踩过的坑**，不是假想：

1. **`.ps1` 必须带 UTF-8 BOM。**
   Windows Server 上的 `powershell.exe` 是 **5.1**，它读无 BOM 的 `.ps1`
   时按 **ANSI（中文系统 = GBK）** 解码。脚本里全是中文注释和中文提示，
   于是中文字节被解错，连**引号/花括号都会被吃掉** ⇒ 解析直接失败
   （实测：报「语句块或类型定义中缺少右 }」「字符串缺少终止符」）。
   更阴的是：把同一个文件按 UTF-8 显式读进来解析是**完全正常**的，
   所以在开发机上用编辑器打开、或换个工具检查，都看不出问题。
   ⚠ 注意这和项目里另外两条**方向相反**的规矩：
     · `.env.local` 必须**没有** BOM（`llm.py::_parse_dotenv` 逐行 split("=")，
       带 BOM 会让第一个键变成 "\\uFEFFFT_AUTH_TOKEN"，静默读不到）；
     · `run-server.cmd` 必须**纯 ASCII**（cmd.exe 按字节偏移读文件）。

2. **调用的辅助函数必须在同一个文件里定义过。**
   实测漏定义 `Warn`（只写了 `Say/Ok/Die`），PowerShell 不在解析期报错，
   而是在**跑到那一行**才抛 `CommandNotFoundException` —— 于是脚本前面
   几步都成功了、到复核阶段突然 `exit 1`，看起来像"打包失败"，
   实际是脚本自己写漏了。

3. **自检用的菜名必须真在官方菜库里。**
   实测写成「番茄炒蛋」——菜库里根本没有这道菜（只有「芝士番茄土豆泥」）。
   `/api/plan` 对未命中菜名**只在服务端日志留痕**，响应里看不出来，
   于是自检报 `recipeCount=1`（请求 2 道只排 1 道），
   部署看起来像失败了，实际是自检自己写错了菜名。

4. **zip 条目名必须是正斜杠。**
   ZIP 规范要求 `/`。实测两个"看起来最正规"的做法都产出**反斜杠**：
     · Windows PowerShell 5.1 的 `Compress-Archive` → `app\\api\\main.py`
     · .NET Framework 的 `ZipFile::CreateFromDirectory` → 同样反斜杠
       （该 bug 到 .NET Core 3.0 才修，而 5.1 跑在 .NET Framework 上）
   反斜杠包用资源管理器解开多半没事，但换 7-Zip / 宝塔 / Linux unzip
   会解成一堆平铺怪文件 —— 而且**要到服务器上才发现**。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
# 允许把被测目录重定向到一份**副本**（变异测试用）。
# 这样 `scripts/_mutate_deploy_guards.py` 注入缺陷时不必碰仓库里的真文件 ——
# 即使进程被强杀，deploy/windows/ 下的脚本也始终是干净的。
_WIN = Path(os.environ.get("FT_WIN_DEPLOY_DIR") or (_ROOT / "deploy" / "windows"))

_SCRIPTS = ("pack.ps1", "setup.ps1", "uninstall.ps1")

# PowerShell 脚本里自定义的辅助函数（用于「调用前必须已定义」那条）
_HELPERS = ("Say", "Ok", "Warn", "Die")

_BOM = b"\xef\xbb\xbf"


def _sync_publish_text() -> str:
    """读 `scripts/sync_publish.py`。

    优先读重定向目录里的副本 —— 变异测试会把一份带缺陷的副本放进 `_WIN`，
    这样"锁文件不再随包发布"这条变异才打得穿。副本不存在时读仓库真文件。
    """
    p = _WIN / "sync_publish.py"
    if not p.exists():
        p = _ROOT / "scripts" / "sync_publish.py"
    return p.read_text(encoding="utf-8")


def _raw(name: str) -> bytes:
    return (_WIN / name).read_bytes()


def _text(name: str) -> str:
    """按 utf-8-sig 读 —— 这样 BOM 被剥掉，断言里不用到处写 \\ufeff。"""
    return (_WIN / name).read_text(encoding="utf-8-sig")


@pytest.fixture(scope="module")
def setup_text() -> str:
    return _text("setup.ps1")


@pytest.fixture(scope="module")
def pack_text() -> str:
    return _text("pack.ps1")


# --------------------------------------------------------------- 存在性
@pytest.mark.parametrize("name", _SCRIPTS)
def test_script_exists_and_is_not_empty(name: str) -> None:
    p = _WIN / name
    assert p.is_file(), f"{name} 不存在 —— 部署教程会引用它"
    assert p.stat().st_size > 500, f"{name} 小得不像一个完整脚本"


# ------------------------------------------------- 1. BOM（最容易翻车的一条）
@pytest.mark.parametrize("name", _SCRIPTS)
def test_ps1_starts_with_utf8_bom(name: str) -> None:
    raw = _raw(name)
    assert raw.startswith(_BOM), (
        f"{name} 没有 UTF-8 BOM。Windows Server 上的 powershell.exe 5.1 会按 "
        f"GBK 解码它，中文注释/提示会被解错，连引号和花括号都会被吃掉，"
        f"脚本**解析阶段就失败**。请在文件开头补 EF BB BF。"
    )


@pytest.mark.parametrize("name", _SCRIPTS)
def test_ps1_has_exactly_one_bom(name: str) -> None:
    """BOM 只能在文件最开头出现一次。"""
    raw = _raw(name)
    assert raw.count(_BOM) == 1, f"{name} 里出现了 {raw.count(_BOM)} 个 BOM 字节序列"


@pytest.mark.parametrize("name", _SCRIPTS)
def test_ps1_decodes_as_utf8_and_keeps_chinese(name: str) -> None:
    """BOM 之后必须仍是合法 UTF-8，且中文没被写成乱码。"""
    raw = _raw(name)
    body = raw[len(_BOM):] if raw.startswith(_BOM) else raw
    text = body.decode("utf-8")           # 解不开会直接抛 UnicodeDecodeError
    assert re.search(r"[\u4e00-\u9fff]", text), f"{name} 里找不到任何中文 —— 提示信息被写没了？"
    # 典型的"UTF-8 被当 GBK 再存回 UTF-8"的乱码特征
    assert "锛" not in text and "鈥" not in text, f"{name} 里疑似有编码乱码（如「锛」「鈥」）"


@pytest.mark.parametrize("name", _SCRIPTS)
def test_ps1_has_no_lone_cr_or_lf_oddities(name: str) -> None:
    """PowerShell 对换行不挑，但混用会让 diff 变得不可读；统一 CRLF 或 LF 即可。"""
    raw = _raw(name)
    assert b"\r\n" in raw or b"\n" in raw
    # 不允许出现"孤立 CR"（\r 后面不是 \n）—— 那是编辑器换行风格混用的痕迹
    assert not re.search(rb"\r(?!\n)", raw), f"{name} 里有孤立的 \\r 换行符"


# ------------------------------------------------- 2. 辅助函数必须先定义
@pytest.mark.parametrize("name", _SCRIPTS)
def test_every_helper_call_has_a_definition(name: str) -> None:
    """`Warn "..."` 这种调用若没定义，PowerShell 不在解析期报错，
    而是跑到那一行才 CommandNotFoundException —— 前面步骤都成功了、
    突然 exit 1，看起来像"部署失败"，实际是脚本自己写漏了一个函数。"""
    text = _text(name)
    missing = [
        h for h in _HELPERS
        # 行首的 `Warn "..."` / `Die @'` 之类 = 调用；`function Warn (...)` 不算
        if re.search(rf"(?m)^\s*{h}\s", text) and not re.search(rf"(?m)^\s*function\s+{h}\b", text)
    ]
    assert not missing, (
        f"{name} 调用了未定义的辅助函数 {missing} —— 请在文件开头补 "
        f"`function X ($m) {{ Write-Host ... }}`"
    )


@pytest.mark.parametrize("name", _SCRIPTS)
def test_admin_required_only_on_server_scripts(name: str) -> None:
    """服务器上跑的两个必须要求管理员；开发机打包的不需要（也不需要 UAC 弹窗）。"""
    text = _text(name)
    if name == "pack.ps1":
        assert "#Requires -RunAsAdministrator" not in text, (
            "pack.ps1 在开发机上跑，不该要求管理员（会平白弹 UAC）"
        )
    else:
        assert "#Requires -RunAsAdministrator" in text, (
            f"{name} 要在服务器上改防火墙 / 注册计划任务，必须声明 #Requires -RunAsAdministrator"
        )


# ------------------------------------------------- 3. setup.ps1 的行为约束
def test_setup_writes_env_local_without_bom(setup_text: str) -> None:
    """`.env.local` 必须**不带 BOM** —— 与 .ps1 自己的要求正好相反。

    llm.py::_parse_dotenv 用 encoding="utf-8" 逐行 split("=")，
    带 BOM 时第一个键会变成 "\\uFEFFFT_AUTH_TOKEN"，**静默**读不到 ⇒ 鉴权失效。
    """
    assert "UTF8Encoding $false" in setup_text, (
        "setup.ps1 写 .env.local 时必须用 `New-Object Text.UTF8Encoding $false`（不带 BOM）"
    )
    assert "UTF8Encoding $true" not in setup_text, (
        "setup.ps1 里出现了 UTF8Encoding $true（带 BOM）—— .env.local 会被写成带 BOM，"
        "导致 FT_AUTH_TOKEN 静默失效"
    )


def test_setup_env_local_write_is_atomic_enough(setup_text: str) -> None:
    """用 [IO.File]::WriteAllText 整份写出，而不是逐行追加 / 用 Set-Content。

    逐行追加在中断时会留下半截文件；`Set-Content` 的默认编码随 PowerShell 版本变
    （5.1 是 ANSI，7 是 UTF-8 无 BOM），会让 `.env.local` 的中文变成乱码。
    """
    assert "[IO.File]::WriteAllText($envLocal" in setup_text, (
        "写 .env.local 必须用 [IO.File]::WriteAllText（整份写出 + 显式编码）"
    )
    # 只检查"写 $envLocal 这件事"没有走 Set-Content / Out-File
    assert not re.search(r"Set-Content\s+(-Path\s+)?\$envLocal", setup_text), (
        "不要用 Set-Content 写 .env.local —— 默认编码随版本变，中文会变乱码"
    )
    assert not re.search(r"Out-File\s+(-FilePath\s+)?\$envLocal", setup_text), (
        "不要用 Out-File 写 .env.local —— 默认编码随版本变"
    )


def test_setup_preserves_handwritten_env_local(setup_text: str) -> None:
    """重跑脚本不能把用户手写的内容冲掉 —— 必须用标记块包裹。"""
    assert "# >>> cooking-agent" in setup_text and "# <<< cooking-agent" in setup_text, (
        "setup.ps1 必须用 '# >>> cooking-agent ... >>>' / '# <<< ... <<<' 标记块包裹自动生成段，"
        "否则重跑会把用户手写的配置整段覆盖掉"
    )


def test_setup_selfcheck_recipe_names_exist_in_the_official_library(setup_text: str) -> None:
    """自检请求的菜名必须真在菜库里。

    实测写成「番茄炒蛋」——菜库里没有（只有「芝士番茄土豆泥」），
    于是 /api/plan 静默少菜、recipeCount 报 1，看起来像部署失败。
    这里直接拿**被测代码**（recipe_store）来核对，而不是查字符串。
    """
    m = re.search(r"\$probeBody\s*=\s*'(\[.*?\])'", setup_text)
    assert m, "setup.ps1 里找不到 $probeBody = '[...]' 形式的自检请求体"
    items = json.loads(m.group(1))
    assert len(items) >= 2, "自检至少要请求 2 道菜，否则测不出'静默少菜'"

    from app.spec.recipe_store import all_names

    known = set(all_names())
    unknown = [it["name"] for it in items if it["name"] not in known]
    assert not unknown, (
        f"setup.ps1 自检用了菜库里没有的菜名 {unknown} —— "
        f"/api/plan 会静默少菜，自检会误报部署失败。请改用真实菜名。"
    )


def test_setup_selfcheck_guards_against_silent_recipe_loss(setup_text: str) -> None:
    """自检必须显式比对"请求了几道 vs 排了几道"。"""
    assert "recipeCount" in setup_text, "自检要读 overview.recipeCount"
    assert re.search(r"recipeCount\s*-lt\s*\d", setup_text), (
        "自检必须把 recipeCount 与期望值比较 —— 否则菜名没命中时不会报警"
    )


def test_setup_cmd_is_pure_ascii(setup_text: str) -> None:
    """生成的 run-server.cmd 必须**纯 ASCII**。

    cmd.exe 按字节偏移分块读 .cmd，多字节字符会让它错位、把半行当命令执行。
    """
    m = re.search(r'\$cmdText\s*=\s*@"\r?\n(.*?)\r?\n"@', setup_text, re.S)
    assert m, "找不到 $cmdText 的 here-string —— 生成 run-server.cmd 的逻辑被改动了？"
    body = m.group(1)
    bad = sorted({c for c in body if ord(c) > 127})
    assert not bad, (
        f"run-server.cmd 的模板里出现了非 ASCII 字符 {bad} —— "
        f"cmd.exe 会因此错位解析。注释和提示请一律用英文。"
    )
    assert "[Text.Encoding]::ASCII" in setup_text, "写出 .cmd 时必须显式用 ASCII 编码"


def test_setup_cmd_has_restart_loop_and_single_worker(setup_text: str) -> None:
    m = re.search(r'\$cmdText\s*=\s*@"\r?\n(.*?)\r?\n"@', setup_text, re.S)
    body = m.group(1)
    assert "--workers 1" in body, (
        "必须单 worker：设备配置 / 排程历史 / 熔断器 / 在线闸门全是**进程内状态**，"
        "多 worker 会出现『改了设备配置，下一次排程又变回去』"
    )
    # ⚠ 锚点必须钉在**整行**上。写成 `"goto loop" in body` 会被
    #   `rem goto loop`（注释掉）蒙混过去 —— 变异检验实测漏网过一次。
    assert re.search(r"(?m)^:loop\s*$", body), "启动器要有 :loop 标号"
    assert re.search(r"(?m)^goto\s+loop\s*$", body), "启动器要真的跳回 :loop（被注释掉不算）"
    assert "--host 0.0.0.0" in body, "必须监听 0.0.0.0，否则外部访问不到"


def test_setup_registers_boot_autostart(setup_text: str) -> None:
    assert "Register-ScheduledTask" in setup_text and "-AtStartup" in setup_text, (
        "必须注册开机自启的计划任务，否则服务器重启后服务就没了"
    )
    assert "ExecutionTimeLimit" in setup_text, (
        "计划任务必须显式设无限时长（ExecutionTimeLimit=0），"
        "默认 72 小时会被强行掐断"
    )


def test_setup_opens_firewall_and_reminds_about_cloud_console(setup_text: str) -> None:
    assert "New-NetFirewallRule" in setup_text, "要放行 Windows 防火墙"
    assert "腾讯云" in setup_text, (
        "必须提醒用户去腾讯云控制台也放行端口 —— 只开 Windows 防火墙，"
        "外部照样连不上，这是最容易漏的一步"
    )


def test_setup_does_not_force_local_ollama_probe(setup_text: str) -> None:
    """服务器上不需要启动探测：出厂层已带可用的远程配置，而远端档预算只有 1.5s。

    冷启动要先建 TLS 连接（实测常落在 1.5s 上下）—— 一旦超时，闸门只会把
    「在线闭环」关掉、排程退回规则兜底，等于白少一路 AI 策略；而关掉探测时
    闭环每轮都真试一次，代价只是首个请求多等一次冷启动。
    """
    assert "FT_WARM_LLM=off" in setup_text, (
        "非离线档也要写 FT_WARM_LLM=off —— 否则开机白等一次远端探测，"
        "超时还会把在线闭环关掉（排程退回规则）"
    )


def test_setup_warns_that_offline_kills_remote_models(setup_text: str) -> None:
    """`-Offline` 写的是 `FT_LLM=off` —— 那是**全局总闸**，设置页改不动它。

    不警告的话：用户在设置页把「接入方式」切成远程、填好地址和 Key、界面显示
    "保存成功"，然后 AI 对话**永远**回"已切换为离线模式" —— 他会去翻模型配置、
    翻网络，怎么都想不到是 `.env.local` 里这一行。2026-10-04 迁移验收时实测过这个坑。
    """
    assert "FT_LLM=off" in setup_text, "-Offline 档要写 FT_LLM=off"
    # ⚠ 必须**锚定在 -Offline 分支内部**（从 `$lines.Add("FT_LLM=off")` 到它后面的
    #   `} else {`）。按全文第一个 "FT_LLM=off" 去切会落在 .PARAMETER 说明里 ——
    #   那样断言的是注释，不是行为（这类"子串空转护栏"本项目已经踩过两次）。
    seg = setup_text.split('$lines.Add("FT_LLM=off")', 1)[1].split("} else {", 1)[0]
    assert "Warn" in seg, (
        "-Offline 分支必须警告：FT_LLM=off 是全局总闸，不是「关掉本地模型」"
    )
    assert "接远程模型也不会生效" in seg, (
        "警告必须点明「接远程模型也不会生效」，否则用户会去设置页白折腾"
    )


def test_setup_token_has_a_safe_default(setup_text: str) -> None:
    """不给 -Token 时要自动生成随机令牌，而不是留空（留空 = 无鉴权）。"""
    assert "[guid]::NewGuid().ToString" in setup_text, (
        "默认必须自动生成随机令牌；留空等于把无鉴权的服务暴露到公网"
    )
    # 关闭鉴权必须是**显式开关**。曾经写成 `-Token -`（拿减号当哨兵值），
    # 但 PowerShell 的参数绑定对单独的 `-` 行为不明确，教程里教错就麻烦了。
    assert re.search(r"\[switch\]\$NoAuth", setup_text), (
        "要有一个显式的 -NoAuth 开关来关闭鉴权"
    )
    assert re.search(r"\$NoAuth\s+-or", setup_text), "-NoAuth 必须真的参与判断"
    assert "Warn" in setup_text.split("$NoAuth -or")[1][:300], (
        "关闭鉴权必须给出警告（公网暴露会被任意改配置）"
    )


def test_pack_verifies_the_dependency_lock_is_inside_the_archive(pack_text: str) -> None:
    """锁文件必须随包发布，而且打包后要**复核它真的在包里**。

    `setup.ps1` 里有「Python 3.14 + requirements.lock.txt ⇒ 逐字复刻开发环境」的分支。
    锁文件不随包走，那段逻辑就**永远是死代码** —— 曾经就是这样：`pack.ps1` 只打
    `publish/*`，而 `sync_publish.py` 的 `TOP_FILES` 里没有锁文件。
    服务器于是按 `requirements.txt` 的区间装最新版，装出来的跟本机验证过的不是一套。
    """
    assert "requirements.lock.txt" in pack_text, (
        "pack.ps1 的复核清单必须含 requirements.lock.txt —— "
        "否则锁文件悄悄没进包，要到服务器上才发现依赖版本不对"
    )


def test_sync_publish_ships_the_dependency_lock() -> None:
    """`sync_publish.py` 的同步清单必须含锁文件 —— 否则它永远到不了 `publish/`。"""
    src = _sync_publish_text()
    m = re.search(r"TOP_FILES\s*=\s*\(([^)]*)\)", src)
    assert m, "sync_publish.py 里找不到 TOP_FILES"
    assert "requirements.lock.txt" in m.group(1), (
        "TOP_FILES 必须含 requirements.lock.txt，否则 setup.ps1 的锁文件分支是死代码"
    )


def test_sync_publish_excludes_machine_local_state() -> None:
    """三份"本机状态"文件都不许进发布包。

    `llm_settings.json` 是**用户层**的模型配置（含打码前的密钥原文与 base_url）；
    `devices_user.json` 是**开发机自己**的厨房设备数（3 灶眼 2 蒸箱 2 烤箱…）；
    `recipes_user.json` 是**菜谱库的用户层**（本机新增/改过/删过的菜）。
    这几份一旦随包走：接收方一解压就是别人家的厨房 + 别人的账号配置 + 别人的改稿，
    而界面上完全看不出这是"别人的"，只会当成"程序自带的默认值"。
    ⚠ `recipes_user.json` 里的 `overrides` 尤其阴：它按 id **覆盖**官方菜，
      界面上没有任何标记，也回不到官方版本（`restore()` 只恢复"被删除"的菜）。
    ⚠ 与"出厂层 / 产品数据"要区分开：`app/agent/llm_defaults.json`、
      `data/device_presets.json`、`data/recipes_official_100.csv` 都必须随包走。
    """
    src = _sync_publish_text()
    m = re.search(r"SKIP_NAMES\s*=\s*frozenset\(\{([^}]*)\}\)", src)
    assert m, "sync_publish.py 里找不到 SKIP_NAMES"
    names = m.group(1)
    for n in ("llm_settings.json", "devices_user.json", "recipes_user.json"):
        assert f'"{n}"' in names, (
            f"SKIP_NAMES 必须含 {n}，否则开发机的本机状态会随包发给别人"
        )


# ------------------------------------------------- 4. pack.ps1 的行为约束
def test_pack_syncs_publish_before_packaging(pack_text: str) -> None:
    """`publish/` 是**拷贝**不是链接 —— 不同步就会把旧代码发上去。

    ⚠ 断言必须落在**命令行调用**上，不能只查 `"sync_publish" in text`：
    脚本顶部的注释里也写着 sync_publish，把真正的调用改成 `scripts.noop`
    时子串断言照样通过 —— 变异检验实测漏网过一次。
    """
    assert re.search(r"-m\s+scripts\.sync_publish\s*$", pack_text, re.M), (
        "pack.ps1 必须先真的执行 `-m scripts.sync_publish`（注释里出现不算）"
    )
    assert re.search(r"-m\s+scripts\.sync_publish\s+--check", pack_text), (
        "同步之后要再用 `-m scripts.sync_publish --check` 复核一次"
    )
    # 顺序：同步在前、复核在后，否则复核的是旧状态
    assert pack_text.index("-m scripts.sync_publish\n") < pack_text.index("--check"), (
        "必须先同步再复核"
    )


def test_pack_normalizes_zip_entry_separators(pack_text: str) -> None:
    """条目名必须显式转成正斜杠。

    实测 Compress-Archive 与 .NET Framework 的 CreateFromDirectory **都**产出反斜杠。
    """
    assert re.search(r"\.Replace\('\\',\s*'/'\)", pack_text), (
        "pack.ps1 必须显式把条目名的 '\\' 换成 '/' —— "
        "PS 5.1 的 Compress-Archive 和 .NET Framework 的 CreateFromDirectory 都产出反斜杠"
    )


def test_pack_does_not_rely_on_compress_archive_as_primary(pack_text: str) -> None:
    """Compress-Archive 只能作为兜底，不能是主路径。"""
    idx_main = pack_text.find("ZipFile]::Open($Out")
    idx_fallback = pack_text.find("Compress-Archive -Path")
    assert idx_main > 0, "主路径应该是手工逐条写 zip（可控制条目名）"
    assert idx_fallback > idx_main, (
        "Compress-Archive 只能出现在主路径之后的兜底分支里"
    )


def test_pack_verifies_key_files_inside_the_archive(pack_text: str) -> None:
    for need in ("app/api/main.py", "app/web/index.html", "data/recipes_official_100.csv",
                 # 出厂默认配置：少了它，接收方解压后只会拿到代码兜底档
                 # （本地 Ollama），必须自己配一遍才能用上模型，而且**不会报任何错**。
                 "app/agent/llm_defaults.json",
                 "requirements.txt", "deploy/windows/setup.ps1"):
        assert need in pack_text, f"pack.ps1 打完包后应核对 {need} 真的在包里"


def test_pack_refuses_to_ship_env_files(pack_text: str) -> None:
    """`.env` / `.env.local` **不许进包** —— 反向确认，不是靠"记得排除"。

    开箱配置走 `app/agent/llm_defaults.json`（随包走，是设计）；而 `.env.local`
    是"本机凭据、单独轮换"的那一份。它一旦进包：开发机的环境跟着发出去，
    之后接收方改了设置页、又看到 `.env.local` 里还有一份，就再也说不清
    到底是哪一份在生效（真实环境变量 > .env.local > 出厂层）。
    """
    assert re.search(r"\$forbidden\s*=.*\$names", pack_text), (
        "pack.ps1 没有对包内条目做「凭据文件」反向检查"
    )
    assert re.search(r"\.env", pack_text), "反向检查没有匹配 .env / .env.local"
    assert re.search(r"Die\s*\(", pack_text), "查到凭据文件却没有中止打包"


def test_pack_excludes_heavy_dirs(pack_text: str) -> None:
    """打包内容 = publish/ + deploy/，绝不能把 .venv / .cache 打进去。"""
    assert "publish\\*" in pack_text or "publish/*" in pack_text
    for heavy in (".venv", "pack-windows"):
        # 允许出现在"清理/暂存目录"的语境里，但不允许作为打包来源
        assert f'Join-Path $RootDir "{heavy}"' not in pack_text, (
            f"pack.ps1 不该把 {heavy} 当作打包来源"
        )


# ------------------------------------------------- 5. uninstall.ps1 的安全约束
def test_uninstall_is_conservative_by_default() -> None:
    """默认只摘服务、不碰数据 —— 删代码目录必须挂在 -Purge 后面。"""
    text = _text("uninstall.ps1")
    assert "Unregister-ScheduledTask" in text, "要注销计划任务"
    assert "Remove-NetFirewallRule" in text or "advfirewall firewall delete rule" in text, (
        "要删掉防火墙规则"
    )
    assert "-Purge" in text, "要有显式的 -Purge 开关才会删数据"
    # 删目录必须用 -LiteralPath（不展开通配符），且必须二次确认
    assert "Remove-Item -LiteralPath $root -Recurse -Force" in text, (
        "删项目目录必须用 -LiteralPath 且只删这一个确切路径，不能用通配符"
    )
    assert "Read-Host" in text, "-Purge 必须二次确认，不能一跑就删"


def test_uninstall_warns_about_cloud_firewall() -> None:
    text = _text("uninstall.ps1")
    assert "腾讯云" in text, (
        "要提醒用户云控制台的防火墙规则本机删不掉，需要去控制台手动删"
    )
