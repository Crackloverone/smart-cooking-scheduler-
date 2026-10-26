"""变异测试：给**降温缓冲 + 排程诊断**的 Python 护栏注入缺陷，确认它们真的会失败。

覆盖 `tests/test_insight.py`（v3.3 新增）。守卫写了不跑等于没写 —— 这里的坑
尤其隐蔽：`cooldown_minutes` 是一个纯函数，测试断言 `> 0` / `== 0`，
一旦哪天有人把"降温"改成"只记不罚"，**所有断言照样全绿**。

安全设计：`engine.py` / `metrics.py` / `insight.py` 是**被 import 的真模块**，
没法像前端那样用环境变量重定向到副本，所以这里采用
**「原地变异 → 立刻还原」**，并且：
  · 每条变异前后都核对 sha256，还原不成功直接中止（绝不留下被改过的源码）；
  · 脚本开头先检查有没有上次异常退出留下的残留（`.orig` 备份），有就先还原；
  · `finally` 里无条件还原，哪怕 pytest 崩了 / 键盘中断；
  · 装了 SIGINT/SIGTERM 处理器，收到信号也先还原再退出。

⚠ **被强杀（Windows 的 TerminateProcess / 超时 kill）时 `finally` 与信号处理器都不会跑**，
  源码会**留在被改过的状态**。2026-10-06 在姊妹脚本 `_mutate_llm_guards.py` 上真实踩过：
  前台跑超时被 SIGTERM，一行 `return` 被留在变异态，之后一片用例开始莫名其妙地红。
  恢复办法：**重跑一次本脚本**（开头会自动还原），或从 `.workbuddy-ai/tmp/*.orig` 手工还原。
  ⚠ **判断"有没有残留"必须比 sha256**，不能只看几个锚点还在不在。

用法：python -m scripts._mutate_insight_guards
"""
from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / ".workbuddy-ai" / "tmp"

TARGETS = {
    "engine": ROOT / "app" / "scheduler" / "engine.py",
    "metrics": ROOT / "app" / "scheduler" / "metrics.py",
    "insight": ROOT / "app" / "scheduler" / "insight.py",
}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _to_lf(raw: bytes) -> str:
    """解码并**把换行统一成 LF**，供锚点匹配用（见 main() 里的说明）。"""
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _backup_path(key: str) -> Path:
    return BACKUP / f"mutate_insight_{key}.py.orig"


def _restore_from_backup() -> None:
    """还原上次异常退出留下的残留（幂等）。"""
    for key, path in TARGETS.items():
        bp = _backup_path(key)
        if bp.exists():
            path.write_bytes(bp.read_bytes())
            bp.unlink()
            print(f"  [还原] 发现残留备份，已还原 {path.name}")


# (说明, 目标, 应当失败的测试 id, 变异函数 str -> str)
#
# 每条都对应一个**真实会犯的错**，不是假想的。
MUTATIONS: list[tuple[str, str, str, object]] = [
    # ---- 一、降温口径 --------------------------------------------------------
    ("cooldown_minutes 恒为 0（降温只记不罚，等于没做）",
     "engine", "tests/test_insight.py::test_cooldown_只罚降温",
     lambda s: s.replace("    return round(drop / 100.0 * cooldown_rate(), 4)",
                         "    return 0.0")),
    ("cooldown_enabled 忽略环境变量（FT_COOLDOWN=off 的总闸失效，A/B 对照变成自欺）",
     "engine", "tests/test_insight.py::test_cooldown_开关可关闭",
     lambda s: s.replace(
         '    return os.environ.get(COOLDOWN_ENV, "on").strip().lower() not in ("off", "0", "false", "no")',
         "    return True")),
    ("cooldown_rate 忽略 FT_COOLDOWN_RATE（速率写死，调参调不动）",
     "engine", "tests/test_insight.py::test_cooldown_速率可覆盖",
     lambda s: s.replace("    raw = os.environ.get(COOLDOWN_RATE_ENV)",
                         "    raw = None")),
    ("温度未知时不再早退（None 参与减法 ⇒ 排程直接崩）",
     "engine", "tests/test_insight.py::test_cooldown_温度未知为0",
     lambda s: s.replace("    if prev_temp is None or next_temp is None:\n        return 0.0\n", "")),

    # ---- 二、校验器 ----------------------------------------------------------
    ("device_switches 只看「是否相邻」、不看「留够没有」（合规解被误报成违规）",
     "metrics", "tests/test_insight.py::test_device_switches_留够时间不报",
     lambda s: s.replace("            if gap < need - 1e-6:", "            if True:")),
    # ⚠ 不能把锚点对准"相邻"——同温紧邻时 `gap == 0`，去掉早退后 `0 < -1e-6` 仍为假，
    #   测不出差别。必须用**同温重叠**（gap 为负）那条测试来钉（见 test_insight.py）。
    ("device_switches 去掉 need<=0 早退（同温合炉的重叠被误报成降温违规）",
     "metrics", "tests/test_insight.py::test_device_switches_同温重叠不报",
     lambda s: s.replace("            if need <= 0:\n                continue\n", "")),

    # ---- 三、贪心轨真的把缓冲排进去（两条路径各一条变异）--------------------
    # ⚠ 降温缓冲有**两份实现**：烤箱独占 ⇒ `_ResourceTimeline._fit`（12 空格缩进）；
    #   蒸箱同温合炉 ⇒ `_temp_share_fit`（8 空格缩进）。两条必须各有一个变异，
    #   否则删掉其中一份，另一条测试照样全绿（实测漏网过一次）。
    ("_ResourceTimeline._fit 不加降温缓冲（烤箱异温背靠背照样排出）",
     "engine", "tests/test_insight.py::test_贪心轨插入降温缓冲",
     lambda s: s.replace(
         "            if e <= cur:\n"
         "                # 这段已经过去, 但余温可能还没散完 (紧邻时才有影响)\n"
         "                cd = cooldown_minutes(t2, temp)\n"
         "                if cd and e + cd > cur:\n"
         "                    cur = e + cd\n"
         "                continue\n",
         "            if e <= cur:\n                continue\n")),
    ("_temp_share_fit 不加降温缓冲（蒸箱异温背靠背照样排出）",
     "engine", "tests/test_insight.py::test_蒸箱轨插入降温缓冲",
     lambda s: s.replace(
         "        if e <= cur:\n"
         "            # 这段已经过去, 但余温可能还没散完 (紧邻时才有影响)\n"
         "            cd = cooldown_minutes(t2, temp)\n"
         "            if cd and e + cd > cur:\n"
         "                cur = e + cd\n"
         "            continue\n",
         "        if e <= cur:\n            continue\n")),

    # ---- 四、择优门（两条，一条管降温、一条管"不能合成一个布尔"）-------------
    ("择优门 _cd_ok 恒 True（降温不再是可行性，CP-SAT/精确轨可以交违规解）",
     "engine", "tests/test_insight.py::test_择优门消除降温违规",
     lambda s: s.replace("        if not cd_on or p is None:\n            return True",
                         "        if True:\n            return True")),
    # ε-约束（`prefer_cd`）的两条边界：无界合规优先、以及"预算内也不换"。
    # 前者是实测踩过的坑（Γ 2.0 → 191.4），后者是把它改坏的常见方式。
    ("prefer_cd 去掉预算（无界合规优先 ⇒ 拿 Γ 换物理，实测 95 倍）",
     "engine", "tests/test_insight.py::test_降温门_超预算不拿分数换物理",
     lambda s: s.replace("    if float(best_cd.finish_gap_min) <= float(best.finish_gap_min) + budget:",
                         "    if True:")),
    ("prefer_cd 恒返回更优解（预算内也不再换合规解，等于没做）",
     "engine", "tests/test_insight.py::test_prefer_cd_预算语义",
     lambda s: s.replace("    if float(best_cd.finish_gap_min) <= float(best.finish_gap_min) + budget:\n"
                         "        return best_cd\n    return best",
                         "    return best")),
    # ⚠ `best_cd` 只判降温不判 hand_cap ⇒ ε-约束会把 hand_cap 的保证换掉
    #   （实测 hand_streak 99 > 30）。这是"两道门不能互相覆盖"的等价形态。
    ("best_cd 只判降温不判 hand_cap（ε-约束把 hand_cap 的保证换掉）",
     "engine", "tests/test_insight.py::test_降温ε约束不能换掉满足hand_cap的解",
     lambda s: s.replace("    best_cd = greedy if (cd_on and _feas_ok(greedy)) else None",
                         "    best_cd = greedy if (cd_on and _cd_ok(greedy)) else None")),
    # hand_cap 那道门**必须还在**：降温改成 ε-约束后，它是 `_better` 里唯一的可行性过滤。
    # 一旦被抹掉，"满足连续手动上限"的保证就没人守了（实测 hand_streak 99 > 30）。
    ("`_better` 不再过滤 hand_cap（连续手动上限的保证被抹掉）",
     "engine", "tests/test_model.py::test_engine_keeps_hand_cap_satisfying_plan",
     lambda s: s.replace("        if hand_cap is not None:\n"
                         "            c_ok, b_ok = _cap_ok(cand), _cap_ok(cur)\n"
                         "            if c_ok != b_ok:\n"
                         "                return c_ok\n",
                         "        if False:\n"
                         "            pass\n")),

    # ---- 五、诊断模块的环守卫与兜底口径 --------------------------------------
    ("零余量链去掉成环守卫（畸形依赖把关键路径算成死循环）",
     "insight", "tests/test_insight.py::test_insight_零余量链成环立刻停",
     lambda s: s.replace(
         "        if cur in chain:                     # 成环: 立刻停, 绝不死循环\n"
         "            break\n", "")),
    ("最长时长链去掉 onstack 环检测（成环 ⇒ RecursionError）",
     "insight", "tests/test_insight.py::test_insight_最长时长链成环不死循环",
     lambda s: s.replace('            return 0.0                        # 成环: 退化为 0, 绝不死循环\n', "")),
    ("零余量链退化时不再退回最长链（关键路径只剩 1 步，等于没给）",
     "insight", "tests/test_insight.py::test_insight_零余量链退化时退回最长链",
     lambda s: s.replace("    if len(chain) < 2:", "    if False:")),
    ("risks 丢掉降温归因（红色预警里没有「为什么」）",
     "insight", "tests/test_insight.py::test_insight_降温违规报成high",
     lambda s: s.replace('                "level": "high", "kind": "cooldown", "task_id": b,',
                         '                "level": "mid", "kind": "cooldown", "task_id": b,')),
]


def _install_signal_guard(originals: dict, shas: dict) -> None:
    """收到 SIGINT / SIGTERM 时先还原源码再退出。

    ⚠ 尽力而为：Windows 上被 TerminateProcess 强杀时不会有机会执行。
    真正兜底的是开头的 `_restore_from_backup()` + `.orig` 备份。
    """
    def _handler(signum, _frame):
        print(f"\n[中断] 收到信号 {signum} —— 先还原源码再退出")
        _restore(originals, shas)
        raise SystemExit(130)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, AttributeError):
            pass


def _run(test_id: str) -> bool:
    """跑一条测试，返回是否**通过**。"""
    env = dict(os.environ, CODEBUDDY_SAFE_DELETE_ENABLED="0")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", test_id, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    return r.returncode == 0


def main() -> int:
    BACKUP.mkdir(parents=True, exist_ok=True)
    _restore_from_backup()

    originals = {k: p.read_bytes() for k, p in TARGETS.items()}
    shas = {k: _sha(b) for k, b in originals.items()}
    for k, p in TARGETS.items():
        _backup_path(k).write_bytes(originals[k])
    _install_signal_guard(originals, shas)

    # 基线：未变异时必须全绿，否则后面每条变异都会"失败"，看起来 100% 命中，其实是假的
    for test_id in sorted({m[2] for m in MUTATIONS}):
        if not _run(test_id):
            print(f"[X] 基线就没过: {test_id} —— 变异结果无意义")
            _restore(originals, shas)
            return 2

    missed: list[str] = []
    skipped: list[str] = []
    restore_failed = False
    print(f"逐条注入 {len(MUTATIONS)} 个变异（原地改 + 立刻还原，每步核对 sha256）")
    print()
    try:
        for label, key, test_id, mutate in MUTATIONS:
            path = TARGETS[key]
            # ⚠ 换行必须先规范化成 LF 再变异：`engine.py` / `model.py` 在仓库里是
            #   **CRLF**，而本文件里的锚点字面量是 LF ⇒ 直接 decode 会让
            #   `str.replace` 一条都匹配不上，全部误报成"锚点找不到 / 护栏已失效"。
            #   写回时 `write_text(newline=None)` 会把 LF 翻回 `os.linesep`(CRLF)，
            #   所以注入进去的缺陷对 CRLF 源文件依然有效；而 `finally` 用的是
            #   **原始字节**，仓库换行风格不会被动到。
            src = _to_lf(originals[key])
            mutated = mutate(src)                       # type: ignore[operator]
            if mutated == src:
                skipped.append(label)
                print(f"  [跳过] {label}  —— 锚点在当前代码里找不到（护栏可能已失效）")
                continue
            path.write_text(mutated, encoding="utf-8")
            try:
                passed = _run(test_id)
            finally:
                path.write_bytes(originals[key])
                if _sha(path.read_bytes()) != shas[key]:
                    restore_failed = True
                    print(f"[X] 还原失败: {path} —— 立即中止")
                    raise RuntimeError("restore failed")
            if passed:
                missed.append(label)
                print(f"  [漏网] {label}  ->  {test_id}")
            else:
                print(f"  [命中] {label}")
    except RuntimeError:
        pass
    finally:
        _restore(originals, shas)

    if restore_failed:
        return 3

    print()
    if skipped:
        print(f"⚠ 有 {len(skipped)} 条变异锚点找不到 —— 跳过 = 那条护栏已经没人验了：")
        for s in skipped:
            print("   -", s)
    if missed:
        print(f"✗ 有 {len(missed)} 条变异**没被护栏拦住**（护栏空转）：")
        for m in missed:
            print("   -", m)
        return 1
    if skipped:
        return 3
    print(f"✓ 全部 {len(MUTATIONS)} 条变异都被对应护栏捕获。")
    return 0


def _restore(originals: dict, shas: dict) -> None:
    """无条件还原并核对；顺手清掉备份。"""
    for k, p in TARGETS.items():
        try:
            p.write_bytes(originals[k])
        except BaseException as e:
            print(f"  [警告] 还原 {p.name} 失败: {type(e).__name__}")
    for k, p in TARGETS.items():
        ok = _sha(p.read_bytes()) == shas[k]
        print(f"  [{'OK ' if ok else 'BAD'}] 源码已还原: {p.relative_to(ROOT)}")
    try:
        for k in TARGETS:
            _backup_path(k).unlink(missing_ok=True)
    except BaseException as e:
        print(f"  [提示] 备份清理被拦（不影响结论）: {type(e).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
