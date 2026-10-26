"""把源码 + 服务器部署脚本打成**可直接上传**的部署包（`dist/`）。

为什么单独有这个脚本
----------------------

`deploy/upload.sh` 里原本内联了一段打包逻辑，它有两个真问题：

1. `cp deploy/* "$STAGE/deploy/"` —— `deploy/` 下有 `windows/` 子目录，`cp` 没有 `-r`
   会报 `omitting directory` 且退出码非 0，在 `set -e` 下**整个上传流程必然中止**。
2. 打出来的包没有任何校验 —— "出厂层的 key 到底有没有进去"只能靠人肉看，
   而它没有进去时**界面上完全正常**（排程照跑，只是 AI 不动），没有任何报错。

所以打包这件事必须有**单一真源**且 **fail-closed**：
`upload.sh` 改成调用本脚本拿包，两边不会再漂移；本脚本在打包前逐条断言，
任一条不过就直接中止、不产出包。

校验项（不过就不打包）
----------------------

必须存在且可用：

- `app/agent/llm_defaults.json` —— 出厂层接入配置。它决定"解包能不能直接用"。
  除了存在，还要真能解析出 `provider`；provider=openai 时 `remote.api_key` 必须非空
  （`llm.enabled()` 在 openai 档下没 key 会直接返回 False，而界面只显示
  「模型已关闭」，不看这个文件根本查不出原因）。
- `data/llm_agent_cache.json` —— 离线语义先验缓存。**服务器断网也要排得好**，
  靠的就是它；少了它排程仍能跑但质量会掉。
- `data/recipes_official_100.csv` / `data/device_presets.json` —— 产品数据基线。
- `deploy/server-setup.sh` / `deploy/cooking-agent.service` —— 部署入口。

必须**不**存在：

- `.env` / `.env.local` / 任何 `.env*` —— 本机凭据，绝不上服务器。
- 五份本机状态（`llm_settings.json` / `devices_user.json` / `recipes_user.json` /
  `schedules_user.json` / `exec_user.json`）—— 进包等于把开发机的模型选择、
  厨房配置、菜谱改稿、排程历史、执行样本**静默**塞给服务器（详见
  `scripts/sync_publish.py` 的 SKIP_NAMES 注释）。
- `tests/` / `__pycache__` / 点开头的目录。

另外所有 `.sh` / `.service` / `.conf` 必须**无 CR**（CRLF 在 Linux 上会让
`#!/bin/bash` 变成 `#!/bin/bash^M`，报错是 `bad interpreter`，很容易误判成
"环境没装 bash"），且 `.sh` 在包内权限为 0755。

用法
----

    python -m scripts.pack_server              # 打 tar.gz + zip 到 dist/
    python -m scripts.pack_server --only tar   # 只打 tar.gz
    python -m scripts.pack_server --check      # 只校验不打包
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

# 本机状态清单**直接复用** sync_publish 的（单一真源）。
# 抄一份过来迟早会漂移：那时 publish/ 里确实没有这些文件（所以"看起来没问题"），
# 但换一条路径打的包就可能把它们带上去，而两份清单对不上是**静默**的。
from scripts.sync_publish import SKIP_NAMES

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PUBLISH = ROOT / "publish"

# deploy/ 里**服务器用得上**的文件（windows/ 是 Windows 一键安装包专用，不上服务器）
DEPLOY_FILES = (
    "server-setup.sh",
    "upload.sh",
    "pack-server.sh",
    "cooking-agent.service",
    "nginx-cooking-agent.conf",
)

# 随包带上的文档（验收/排障要查的）
DOC_FILES = (
    "智能烹饪调度Agent接口规范.md",
    "部署指南.md",
    "docs/测评接口清单.md",
    "docs/部署文档.md",
)

# 任何层级都不许出现（清单本身来自 sync_publish.SKIP_NAMES，见文件头）
FORBIDDEN_NAMES = SKIP_NAMES
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".bak", ".orig", ".png", ".tmp")

MUST_HAVE = (
    "app/api/main.py",
    "app/web/index.html",
    "app/agent/llm_defaults.json",
    "data/recipes_official_100.csv",
    "data/device_presets.json",
    "data/llm_agent_cache.json",
    "requirements.txt",
    "requirements.lock.txt",
    "deploy/server-setup.sh",
    "deploy/cooking-agent.service",
)

_SERVER_README = """# 智能烹饪调度 Agent —— 服务器部署包

解包即可用：模型接入配置与凭据**已随包附带**（`app/agent/llm_defaults.json`，
出厂层），不需要再手工填任何 API Key。排程本身离线也能跑（语义先验走
`data/llm_agent_cache.json` 缓存）。

## 三步跑起来

```bash
# 1) 解包到 /opt
sudo mkdir -p /opt/cooking-agent
sudo tar xzf cooking-agent-server.tar.gz -C /opt/cooking-agent

# 2) 一键部署（装依赖 + 注册 systemd + 自检），约 2–5 分钟
sudo bash /opt/cooking-agent/deploy/server-setup.sh

# 3) 打开
#    http://<服务器IP>:8000/app
```

想让链接不带端口：`WITH_NGINX=1 sudo bash /opt/cooking-agent/deploy/server-setup.sh`
（会顺带装 nginx 把 80 反代到应用）。

只想离线跑（不开模型）：`WITH_LLM=0 sudo bash /opt/cooking-agent/deploy/server-setup.sh`。
排程质量与耗时不变，只是没有在线 AI 行为。

## ⚠ 打不开时先查这个

**服务器本机 `curl http://127.0.0.1:8000/health` 正常、外部打不开**
⇒ 几乎一定是云厂商控制台的【安全组 / 防火墙】没放行 TCP 端口。
这是最常见的一条，本机 curl 一切正常，很容易误判成"部署没生效"。

## 常用命令

```bash
sudo systemctl status cooking-agent        # 看状态
sudo journalctl -u cooking-agent -f        # 跟日志
sudo systemctl restart cooking-agent       # 改了代码/配置后重启
```

## 模型配置怎么改

- **推荐**：Web 界面「设置 → 模型接入」，填完保存即生效，落 `data/llm_settings.json`
  （用户层，**整层压过**出厂层 ⇒ 以后重新发一版程序也不会盖掉你填的）。
- 服务器环境变量优先级最高（在 systemd unit 里加 `Environment=FT_LLM_API_KEY=...`），
  但一旦写了 `FT_LLM_PROVIDER`，界面上的下拉会被禁用。

## 接口

- 官方评分接口 `POST /api/plan`，契约见 `智能烹饪调度Agent接口规范.md`。
- 其余端点（`/parse` `/schedule` `/reschedule` `/notify` `/api/revise_plan`）
  见 `docs/测评接口清单.md`。
"""


# --------------------------------------------------------------------------- 工具
def _fail(msg: str) -> "None":
    print(f"[错误] {msg}", file=sys.stderr)
    raise SystemExit(1)


def _find_python() -> str:
    for cand in (ROOT / ".venv/Scripts/python.exe", ROOT / ".venv/bin/python"):
        if cand.exists():
            return str(cand)
    return sys.executable


def _sync_publish(py: str) -> None:
    """打包前**强制**同步 publish/ —— 不同步就会发出旧代码，而本地完全看不出来。"""
    print("==> 同步 publish/ 与源码")
    r = subprocess.run([py, "-m", "scripts.sync_publish"], cwd=str(ROOT))
    if r.returncode != 0:
        _fail("sync_publish 失败")
    r = subprocess.run([py, "-m", "scripts.sync_publish", "--check"],
                       cwd=str(ROOT), capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, file=sys.stderr)
        _fail("publish/ 与源码仍不一致，已中止（线上会是旧代码）")
    print("    publish/ 与源码一致")


def _copy_publish(stage: Path) -> None:
    if not PUBLISH.is_dir():
        _fail(f"找不到发布目录 {PUBLISH}")
    shutil.copytree(PUBLISH, stage, dirs_exist_ok=True)


def _copy_deploy_and_docs(stage: Path) -> None:
    d = stage / "deploy"
    d.mkdir(parents=True, exist_ok=True)
    for fn in DEPLOY_FILES:
        src = ROOT / "deploy" / fn
        if not src.is_file():
            _fail(f"缺少部署文件 deploy/{fn}")
        shutil.copy2(src, d / fn)
    for rel in DOC_FILES:
        src = ROOT / rel
        if not src.is_file():
            print(f"[警告] 文档缺失，跳过：{rel}")
            continue
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    (stage / "README-SERVER.md").write_text(_SERVER_README, encoding="utf-8")


# --------------------------------------------------------------------------- 校验
def _verify(stage: Path) -> list[str]:
    """返回通过信息列表；任一条不过直接 SystemExit。"""
    ok: list[str] = []
    rels = [str(p.relative_to(stage)).replace("\\", "/")
            for p in stage.rglob("*") if p.is_file()]

    # 1) 必须存在
    for rel in MUST_HAVE:
        if not (stage / rel).is_file():
            _fail(f"包内缺少必要文件：{rel}")
    ok.append(f"必要文件齐全（{len(MUST_HAVE)} 项）")

    # 2) 出厂层真的可用 —— 不只是"存在"
    fac_p = stage / "app/agent/llm_defaults.json"
    try:
        fac = json.loads(fac_p.read_text(encoding="utf-8"))
    except Exception as e:
        _fail(f"llm_defaults.json 解析失败：{e}")
    provider = str(fac.get("provider") or "").strip()
    if not provider:
        _fail("llm_defaults.json 里没有 provider —— 出厂层等于空，解包后模型不会启用")
    remote = fac.get("remote") or {}
    key = str(remote.get("api_key") or "").strip()
    if provider == "openai" and not key:
        _fail("provider=openai 但 remote.api_key 为空 —— llm.enabled() 会直接返回 "
              "False，界面只显示「模型已关闭」，看不出是凭据没带")
    if provider == "openai":
        ok.append(f"出厂层可用：provider=openai，api_key 已随包（…{key[-4:]}）")
    else:
        ok.append(f"出厂层可用：provider={provider}（本地档，免密钥）")

    # 3) 不许出现的本机状态 / 凭据
    bad = []
    for r in rels:
        name = r.rsplit("/", 1)[-1]
        if name in FORBIDDEN_NAMES:
            bad.append(f"{r}（本机状态）")
        elif name.startswith(".env"):
            bad.append(f"{r}（本机凭据）")
        elif name.endswith(FORBIDDEN_SUFFIXES):
            bad.append(f"{r}（中间产物/大文件）")
        elif r.startswith("tests/") or "/tests/" in r:
            bad.append(f"{r}（测试代码）")
        elif any(part.startswith(".") for part in r.split("/")):
            bad.append(f"{r}（点开头目录）")
        elif "__pycache__" in r:
            bad.append(f"{r}（字节码缓存）")
    if bad:
        _fail("包内混进了不该出现的文件：\n    " + "\n    ".join(bad[:12]))
    ok.append(f"无本机状态 / 凭据 / 测试代码（共 {len(rels)} 个文件）")

    # 4) 脚本必须 LF 且可执行 —— CRLF 在 Linux 上会让 #!/bin/bash 变成 #!/bin/bash^M
    for r in rels:
        if not r.endswith((".sh", ".service", ".conf")):
            continue
        raw = (stage / r).read_bytes()
        if b"\r" in raw:
            _fail(f"{r} 含 CR —— 在 Linux 上会报 bad interpreter，必须转 LF")
    n_sh = sum(1 for r in rels if r.endswith(".sh"))
    ok.append(f"部署脚本均为 LF（{n_sh} 个 .sh）")

    # 5) 离线兜底必须在 —— 服务器断网也要排得好
    ok.append("离线先验缓存 data/llm_agent_cache.json 已随包")
    return ok


# --------------------------------------------------------------------------- 打包
def _mode_of(rel: str, is_dir: bool) -> int:
    if is_dir:
        return 0o755
    return 0o755 if rel.endswith(".sh") else 0o644


def _make_tar(stage: Path, out: Path) -> None:
    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.mode = _mode_of(ti.name, ti.isdir())
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        return ti

    with tarfile.open(out, "w:gz", format=tarfile.GNU_FORMAT) as tf:
        # ⚠ 目录必须 `recursive=False`：tarfile.add() 默认递归，遍历 rglob 时
        #   目录被 add 一遍、目录下的文件又被逐个 add 一遍 ⇒ 每个文件进包两次。
        #   现象只是"包大了一倍"，解压出来完全正常 —— 不对比体积根本发现不了。
        for p in sorted(stage.rglob("*"), key=lambda x: str(x)):
            rel = "./" + str(p.relative_to(stage)).replace("\\", "/")
            if p.is_dir():
                ti = tf.gettarinfo(str(p), arcname=rel)
                ti.mode, ti.uid, ti.gid = 0o755, 0, 0
                ti.uname = ti.gname = "root"
                tf.addfile(ti)
            else:
                tf.add(p, arcname=rel, recursive=False, filter=_filter)


def _make_zip(stage: Path, out: Path) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(stage.rglob("*"), key=lambda x: str(x)):
            rel = str(p.relative_to(stage)).replace("\\", "/")
            if p.is_dir():
                continue
            zi = zipfile.ZipInfo(rel, date_time=(2026, 10, 9, 0, 0, 0))
            zi.external_attr = (_mode_of(rel, False) << 16) | 0o100000
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, p.read_bytes())


def main() -> int:
    ap = argparse.ArgumentParser(description="打服务器部署包到 dist/")
    ap.add_argument("--only", choices=("tar", "zip", "both"), default="both",
                    help="只打某一种（默认两种都打）")
    ap.add_argument("--check", action="store_true", help="只校验，不产出包")
    ap.add_argument("--no-sync", action="store_true",
                    help="跳过 sync_publish（仅调试用，线上会发旧代码）")
    args = ap.parse_args()

    py = _find_python()
    if not args.no_sync:
        _sync_publish(py)

    stage = Path(tempfile.mkdtemp(prefix="ft_stage_"))
    try:
        _copy_publish(stage)
        _copy_deploy_and_docs(stage)
        print("==> 校验包内容")
        for line in _verify(stage):
            print(f"    [OK] {line}")
        if args.check:
            print("\n校验通过（--check，未产出包）")
            return 0

        DIST.mkdir(parents=True, exist_ok=True)
        made = []
        if args.only in ("tar", "both"):
            t = DIST / "cooking-agent-server.tar.gz"
            _make_tar(stage, t)
            made.append(t)
        if args.only in ("zip", "both"):
            z = DIST / "cooking-agent-server.zip"
            _make_zip(stage, z)
            made.append(z)

        print("\n==> 产出")
        for m in made:
            print(f"    {m.name}  {m.stat().st_size:,} B ({m.stat().st_size/1048576:.2f} MB)")
        print(f"\n上传到服务器： HOST=<IP> bash deploy/upload.sh")
        print("或手动：scp cooking-agent-server.tar.gz root@<IP>:/tmp/ 后解包执行 "
              "deploy/server-setup.sh")
        return 0
    finally:
        # 清理失败不影响产物 —— 只警告
        try:
            shutil.rmtree(stage)
        except Exception as e:
            print(f"[警告] 临时目录未清理（可手动删）：{stage} —— {e}")


if __name__ == "__main__":
    sys.exit(main())
