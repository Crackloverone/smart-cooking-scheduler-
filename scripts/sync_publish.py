"""把源码同步到 `publish/` —— 线上发布的精简目录。

**为什么需要这个脚本**

`publish/` 是**拷贝**，不是软链接，也不是构建产物。改了源码不同步过去，
线上就还是旧代码，而且**从本地完全看不出来**（本地一切正常，只有外部链接是旧的）。

2026-10-01 正是这么踩的：P-01~P-05 五项修复全部落地、回归全绿，
但 `https://cooking-agent.app.workbuddy.host/` 仍然是 9/29 的版本 ——
因为 `publish/` 是当天上午生成的，之后再没重新生成过。
所以这个脚本存在的意义不是"少打几条 copy 命令"，而是**让"线上是不是旧的"变成一条命令就能回答**。

**用法**

    python -m scripts.sync_publish            # 同步，并打印本次改了什么
    python -m scripts.sync_publish --check    # 只检查不同步；有差异时退出码 1

`--check` 适合放在"发布前自检"里：退出码非 0 就说明线上会是旧代码。

**同步规则**（与首次发布时的精简口径一致）

| 范围 | 处理 |
|---|---|
| `app/` | 全量（运行时全部代码 + 前端页面） |
| `data/` | 运行期数据；排除 `*.png` 与 `*_report.json`（报告/截图，体积大且不需要） |
| `scripts/` | `*.py` 与 `*.mjs`（体检/复现脚本，便于线上自查） |
| `requirements.txt` / `requirements.lock.txt` | 有变化就同步（锁文件供服务器逐字复刻环境） |

一律排除：点开头的目录（`.venv` / `.cache` / `.workbuddy*` 等）、`__pycache__`、
`*.pyc` / `*.pyo` / `*.bak`。

**注意**：脚本只写 `publish/`，**不会**发布。发布是外部动作，
要用发布工具并确认"线上现有内容会被覆盖"。
"""
from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "publish"

# 顶层目录里"必须同步"的那几个（发布目录只含这些）
TOP_DIRS = ("app", "data", "scripts")
# ⚠ 锁文件**必须一起发布**：`deploy/windows/setup.ps1` 里有
#   「Python 3.14 + requirements.lock.txt ⇒ 逐字复刻开发环境」的分支，
#   锁文件不进包的话那段逻辑永远是死代码，服务器只能按 `requirements.txt`
#   的区间装最新版 —— 装出来的版本和本机验证过的不是一套。
TOP_FILES = ("requirements.txt", "requirements.lock.txt")

# 任何层级都排除的目录名
SKIP_DIR_NAMES = {"__pycache__", ".venv", ".git", ".cache", ".pytest_cache",
                  ".workbuddy", ".workbuddy-ai", ".mypy_cache", "node_modules"}

# 顶层文件/目录里排除的后缀（体积大、线上用不到；.tmp 是原子写的中间产物）
SKIP_SUFFIXES = (".pyc", ".pyo", ".bak", ".png", ".tmp", ".utf8", ".orig")

# 运行期"本机状态"而非源码，一律不同步。
# `llm_settings.json` 是设置页写下的**本机选的模型**：跟着进发布目录/镜像之后，
# 线上会带着开发机选的模型名启动 —— 而线上多半没 pull 那个模型，Ollama 返回 404，
# 所有调用**静默降级**，现场只剩"部署上去 AI 就不动了"。
# `devices_user.json` 同理，只是换成了**本机的厨房**：开发机上选过"专业级厨电套装"
# 之后它就会出现；进包的话，接收方一解压就是 3 灶眼 2 蒸箱 2 烤箱 —— 与他家厨房
# 完全不符，而界面上看不出这是"别人家的配置"。预设基线 `device_presets.json`
# 仍然照常随包走（那是产品数据，不是本机状态）。
# `recipes_user.json` 是同一条纪律的第三份：它是**菜谱库的用户层**
# （`{added, overrides, deleted}`，见 app/spec/recipe_store.py）。开发机上改过一道菜
# 就会生成它；进包的话，接收方的 100 道官方菜里会**静默混进别人的改稿** ——
# 而且 `overrides` 是按 id 覆盖官方菜，界面上完全看不出"这道菜被换过"，
# 也回不到官方版本（`restore()` 只恢复"被删除"的菜，不撤覆盖）。
# 官方基线 `recipes_official_100.csv` 仍照常随包走（产品数据）。
# `schedules_user.json` 是第四份：v3.7 起排程历史落盘（含**完整计划**）。
# 它是纯本机状态 —— 进包的话，接收方第一次打开"记录"页看到的全是**别人排过的菜**
# （而且是完整甘特图 + 任务明细，看起来完全像自己排的），还会占掉自己前 20 条
# 记录的位置。开发机每跑一次探针/演示就会新增一条。
# ⚠ 四份 → **五份**: v3.9 起 `exec_user.json`（执行态回写 + 经验回流样本）
# 也是本机状态 —— 它记的是"这台机器上真实做菜花了多久", 发给别人没有任何意义,
# 而且 `samples` 会直接改变兜底时长, 混进别人的数据等于悄悄改了对方的排程结果。
SKIP_NAMES = frozenset({"llm_settings.json", "devices_user.json",
                        "recipes_user.json", "schedules_user.json",
                        "exec_user.json"})


def _skip(rel: Path) -> bool:
    """rel 是相对 ROOT 的路径。判断是否要排除。"""
    for part in rel.parts:
        if part in SKIP_DIR_NAMES or part.startswith("."):
            return True
    name = rel.name
    if name in SKIP_NAMES:
        return True
    if name.endswith(SKIP_SUFFIXES):
        return True
    # data 下的体检报告：小但没用，且每次跑探针都会变，同步过去只会让 diff 噪声很大
    if rel.parts[0] == "data" and name.endswith("_report.json"):
        return True
    return False


def _iter_sources() -> list[Path]:
    """列出应当出现在 publish/ 里的所有文件（相对 ROOT）。"""
    out: list[Path] = []
    for top in TOP_DIRS:
        base = ROOT / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")]
            for fn in filenames:
                rel = Path(dirpath, fn).relative_to(ROOT)
                if _skip(rel):
                    continue
                if top == "scripts" and Path(fn).suffix not in (".py", ".mjs"):
                    continue
                out.append(rel)
    for top in TOP_FILES:
        if (ROOT / top).is_file():
            out.append(Path(top))
    return sorted(out)


def _plan() -> tuple[list[Path], list[Path], list[Path]]:
    """返回 (新增, 更新, 多余)。多余 = publish 里有、但源码里已经没有的。"""
    add, upd = [], []
    wanted: set[Path] = set()
    for rel in _iter_sources():
        wanted.add(rel)
        dst = PUBLISH / rel
        if not dst.exists():
            add.append(rel)
        elif not filecmp.cmp(ROOT / rel, dst, shallow=False):
            upd.append(rel)
    stale: list[Path] = []
    for top in TOP_DIRS:
        base = PUBLISH / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                rel = Path(dirpath, fn).relative_to(PUBLISH)
                if rel not in wanted:
                    stale.append(rel)
    return add, upd, sorted(stale)


def _fmt(rels: list[Path], limit: int = 12) -> str:
    head = "\n".join(f"    {r}" for r in rels[:limit])
    if len(rels) > limit:
        head += f"\n    ... 另有 {len(rels) - limit} 个"
    return head


def main() -> int:
    ap = argparse.ArgumentParser(description="把源码同步到 publish/（线上发布目录）")
    ap.add_argument("--check", action="store_true",
                    help="只检查，不写入；有差异时退出码 1")
    args = ap.parse_args()

    if not PUBLISH.is_dir():
        print(f"[错误] 找不到发布目录 {PUBLISH}")
        return 2

    add, upd, stale = _plan()

    if args.check:
        total = len(add) + len(upd) + len(stale)
        if total == 0:
            print("publish/ 与源码一致 —— 发布出去的就是当前代码。")
            return 0
        print(f"publish/ 已过期，共 {total} 处差异（线上会是旧代码）：")
        if add:
            print(f"  新增 {len(add)} 个（线上缺这些文件）:\n{_fmt(add)}")
        if upd:
            print(f"  更新 {len(upd)} 个:\n{_fmt(upd)}")
        if stale:
            print(f"  多余 {len(stale)} 个（源码里已删除，发布目录还留着）:\n{_fmt(stale)}")
        print("\n修复: python -m scripts.sync_publish")
        return 1

    for rel in add + upd:
        dst = PUBLISH / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    for rel in stale:
        (PUBLISH / rel).unlink()

    print(f"同步完成：新增 {len(add)} · 更新 {len(upd)} · 删除 {len(stale)}")
    if add:
        print(f"  新增:\n{_fmt(add)}")
    if upd:
        print(f"  更新:\n{_fmt(upd)}")
    if stale:
        print(f"  删除:\n{_fmt(stale)}")

    # 同步完顺手复核一次，避免"以为同步了其实没写进去"
    a2, u2, s2 = _plan()
    if a2 or u2 or s2:
        print(f"[错误] 同步后仍有 {len(a2) + len(u2) + len(s2)} 处差异，请检查权限")
        return 2
    print("复核通过：publish/ 与源码一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
