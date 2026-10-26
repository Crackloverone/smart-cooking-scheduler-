"""变异测试：给**模型接入配置层**的 Python 护栏注入缺陷，确认它们真的会失败。

覆盖 `tests/test_llm_provider.py`（2026-10-06 新增的出厂层 + 密钥打码一组）。

为什么这一层特别需要变异自检
----------------------------
这次改动有三处"写错了也不报错"的失败模式：

1. **出厂层没被真的用上** —— 解析函数少传一个 `factory` 参数，代码照样跑、
   测试照样绿（只要断言写的是"函数返回了个东西"），但接收方拿到的还是本地 Ollama 档，
   也就是**问题根本没修**。
2. **打码值被当成密钥写回** —— `looks_masked()` 判据写歪一点（比如只判 `*`），
   测试若只断言"掩码不含原文"，照样全绿，而用户保存一次就把配置写坏。
3. **打码预览漏出原文** —— 打码函数少切几位（比如 `k[:20]`），
   只要断言写的是"blob 里不含密钥"，而密钥恰好很短，也可能全绿。

这三条都不会让别的测试变红，所以逐条注入、逐条确认护栏会响。

安全设计：`llm.py` / `main.py` 是**被 import 的真模块**，没法像前端那样用环境变量
重定向到副本，所以采用 **「原地变异 → 立刻还原」**：
  · 每条变异前后都核对 sha256，还原不成功直接中止（绝不留下被改过的源码）；
  · 脚本开头先检查有没有上次异常退出留下的残留（`.orig` 备份），有就先还原；
  · `finally` 里无条件还原，哪怕 pytest 崩了 / 键盘中断；
  · 装了 SIGINT/SIGTERM 处理器，收到信号也先还原再退出。

⚠ **被强杀（Windows 的 TerminateProcess / 超时 kill）时 `finally` 与信号处理器都不会跑**，
  源码会**留在被改过的状态**。真实踩过：前台跑超时被 SIGTERM，`llm.py` 里的
  `looks_masked()` 被留成了 `return True`，之后所有带真密钥的保存全部被误拒。
  恢复办法：**重跑一次本脚本**（开头会自动还原），或直接从
  `.workbuddy-ai/tmp/mutate_llm_*.orig` 手工还原。
  ⚠ **判断"有没有残留"必须比 sha256**，不能只看几个锚点是否还在 ——
  那次踩坑时锚点全在，只有一行 `return` 被换掉了。

用法：python -m scripts._mutate_llm_guards
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
    "llm": ROOT / "app" / "agent" / "llm.py",
    "main": ROOT / "app" / "api" / "main.py",
    "defaults": ROOT / "app" / "agent" / "llm_defaults.json",
}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _to_lf(raw: bytes) -> str:
    """解码并把换行统一成 LF，供锚点匹配用。

    `llm.py` 在仓库里可能是 CRLF（Windows 上被编辑器翻过），而本文件里的锚点
    字面量是 LF ⇒ 直接 decode 会让 `str.replace` 一条都匹配不上，
    全部误报成"锚点找不到"。写回时用**原始字节**，换行风格不会被动到。
    """
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _backup_path(key: str) -> Path:
    return BACKUP / f"mutate_llm_{key}.orig"


def _restore_from_backup() -> None:
    """还原上次异常退出留下的残留（幂等）。"""
    for key, path in TARGETS.items():
        bp = _backup_path(key)
        if bp.exists():
            path.write_bytes(bp.read_bytes())
            bp.unlink()
            print(f"  [还原] 发现残留备份，已还原 {path.name}")


# (说明, 目标, 应当失败的测试 id, 变异函数 str -> str)
MUTATIONS: list[tuple[str, str, str, object]] = [
    # ---------- ① 出厂层必须真的参与解析 ----------
    ("解析层忽略出厂层（`_layer()` 直接返回空 ⇒ 接收方拿不到随包配置）",
     "llm", "tests/test_llm_provider.py::test_factory_defaults_file_gives_a_working_config_out_of_the_box",
     lambda s: s.replace("    return _FACTORY if factory is None else factory",
                         "    return {}")),
    ("出厂层的 provider 被无视（永远按本地 Ollama 解析）",
     "llm", "tests/test_llm_provider.py::test_factory_defaults_file_gives_a_working_config_out_of_the_box",
     lambda s: s.replace(
         '    return _norm_provider(saved.get("provider") or _layer(factory).get("provider") or "")',
         '    return _norm_provider(saved.get("provider") or "")')),
    ("出厂层里的密钥不被采用（开箱即用落空：enabled() 恒 False）",
     "llm", "tests/test_llm_provider.py::test_factory_defaults_file_gives_a_working_config_out_of_the_box",
     lambda s: s.replace(
         '        for src in (saved, _layer(factory)):\n'
         '            k = str(_remote_cfg(src).get("api_key") or "").strip()\n'
         '            if k:\n'
         '                return k\n',
         '        for src in (saved,):\n'
         '            k = str(_remote_cfg(src).get("api_key") or "").strip()\n'
         '            if k:\n'
         '                return k\n')),
    ("用户层不再压过出厂层（重新发版会把接收方的改动盖回去）",
     "llm", "tests/test_llm_provider.py::test_user_settings_win_over_the_factory_defaults",
     lambda s: s.replace(
         '        for src in (saved, _layer(factory)):\n'
         '            u = str(_remote_cfg(src).get("base_url") or "").strip()\n'
         '            if u:\n'
         '                return u.rstrip("/")\n',
         '        for src in (_layer(factory), saved):\n'
         '            u = str(_remote_cfg(src).get("base_url") or "").strip()\n'
         '            if u:\n'
         '                return u.rstrip("/")\n')),
    ("切档时没给模型 ⇒ 落盘代码默认值（把出厂层配好的模型名换掉 ⇒ 404 静默降级）",
     "llm", "tests/test_llm_provider.py::test_switching_provider_without_a_model_keeps_the_factory_model",
     lambda s: s.replace('or _resolve_model("", {}, p)[0]', 'or _DEFAULTS[p][1]')),
    ("出厂层坏形状不再被当空（`_read_defaults` 把异常抛出去 ⇒ 服务起不来）",
     "llm", "tests/test_llm_provider.py::test_broken_factory_file_is_ignored_not_fatal",
     lambda s: s.replace(
         '    except Exception as e:\n'
         '        _LOG.warning("读取出厂默认配置失败(%s)，已忽略 %s（按代码内置默认档运行）",\n'
         '                     e, _DEFAULTS_PATH)\n'
         '        return {}\n',
         '    except FileNotFoundError:\n'
         '        return {}\n')),
    # ---------- ② 打码本身 ----------
    ("打码露太多（前 20 + 后 4 ⇒ 密钥基本等于没打）",
     "llm", "tests/test_llm_provider.py::test_mask_key_keeps_the_ends_and_hides_the_middle",
     lambda s: s.replace("    return k[:6] + _MASK_ELLIPSIS + k[-4:]",
                         "    return k[:20] + _MASK_ELLIPSIS + k[-4:]")),
    ("打码不回传（退回只有布尔值的旧行为 ⇒ 用户认不出是哪把钥匙）",
     "llm", "tests/test_llm_provider.py::test_remote_view_exposes_a_masked_preview_but_never_the_key",
     lambda s: s.replace('        "api_key_masked": mask_key(eff_key),',
                         '        "api_key_masked": "",')),
    ("`remote_view` 直接回传密钥原文（凭据挂公网）",
     "llm", "tests/test_llm_provider.py::test_remote_view_never_leaks_the_api_key",
     lambda s: s.replace('        "api_key_masked": mask_key(eff_key),',
                         '        "api_key_masked": eff_key,')),
    ("`factory_view` 直接回传出厂密钥原文",
     "llm", "tests/test_llm_provider.py::test_factory_view_never_leaks_the_key",
     lambda s: s.replace('        "api_key_masked": mask_key(key),',
                         '        "api_key_masked": key,')),
    ("`remote_view` 只报「已保存」值（接收方看到「未配置」，实际却在正常跑）",
     "llm", "tests/test_llm_provider.py::test_remote_view_exposes_a_masked_preview_but_never_the_key",
     lambda s: s.replace('        "effective_base_url": BASE_URL if PROVIDER == "openai" else eff_url,\n'
                         '        "effective_model": MODEL if PROVIDER == "openai" else eff_model,\n',
                         '        "effective_base_url": stored_url,\n'
                         '        "effective_model": stored_model,\n')),
    # ---------- ③ 打码值不许被写回 ----------
    ("不再拒绝打码值（用户保存一次就把 `sk-pro1…ESb0` 写成真密钥）",
     "llm", "tests/test_llm_provider.py::test_masked_key_is_rejected_on_write",
     lambda s: s.replace("        if looks_masked(k):", "        if False:")),
    ("判据只认星号、不认省略号（我们自己打出来的掩码漏过去）",
     "llm", "tests/test_llm_provider.py::test_looks_masked_recognises_previews_but_not_real_keys",
     lambda s: s.replace('    return _MASK_ELLIPSIS in v or "***" in v', '    return "***" in v')),
    ("判据把真密钥也误判成掩码（用户永远存不上钥匙）",
     "llm", "tests/test_llm_provider.py::test_looks_masked_recognises_previews_but_not_real_keys",
     lambda s: s.replace('    return _MASK_ELLIPSIS in v or "***" in v', '    return True')),
    # ---------- ④ 接口层 ----------
    ("`/api/llm_status` 不回打码预览（徽标 tooltip 认不出是哪把钥匙）",
     "main", "tests/test_llm_provider.py::test_status_endpoint_reports_the_masked_key",
     lambda s: s.replace('        "api_key_masked": _llm.mask_key(_llm.API_KEY),',
                         '        "api_key_masked": "",')),
    ("`/api/model_settings` 不回出厂层现状（界面说不出「用的是出厂预置」）",
     "main", "tests/test_llm_provider.py::test_model_settings_endpoint_never_returns_the_key",
     lambda s: s.replace('        "factory_defaults": llm.factory_view(),', "")),
    # ---------- ⑤ 出厂文件本身 ----------
    ("出厂文件丢掉密钥（「发出去就能用」直接落空）",
     "defaults", "tests/test_llm_provider.py::test_factory_defaults_file_gives_a_working_config_out_of_the_box",
     lambda s: s.replace('    "api_key": "', '    "_api_key": "')),
    ("出厂文件里写了个非法模型名（每次调用 404 ⇒ 静默降级）",
     "defaults", "tests/test_llm_provider.py::test_factory_defaults_file_gives_a_working_config_out_of_the_box",
     lambda s: s.replace('"openai": "gpt-5.6-sol"', '"openai": "带空格的 模型名"')),
    # ---------- ⑥ 用户层路径的隔离开关（2026-10-08）----------
    # 这一条防的是"探针把开发机上的真配置改掉"：没有 env 覆盖，界面探针切一下
    # 接入方式 + 按个回车就会改写 data/llm_settings.json，而且因为密钥/地址是
    # "留空=保持原值"，**看起来什么都没坏**。
    ("用户层路径不再能被 env 重定向（探针会改到真配置）",
     "llm", "tests/test_llm_provider.py::test_settings_path_can_be_redirected_by_env",
     lambda s: s.replace(
         '_SETTINGS_PATH = (os.environ.get("FT_LLM_SETTINGS_FILE")\n'
         '                  or os.path.join(_ROOT, "data", "llm_settings.json"))',
         '_SETTINGS_PATH = os.path.join(_ROOT, "data", "llm_settings.json")')),
]


def _install_signal_guard(originals: dict, shas: dict) -> None:
    """收到 SIGINT / SIGTERM 时先还原源码再退出。

    ⚠ 这只是**尽力而为**：Windows 上被 TerminateProcess 强杀时不会有机会执行。
    真正兜底的是脚本开头的 `_restore_from_backup()` + `.orig` 备份。
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
