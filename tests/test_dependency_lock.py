"""依赖锁文件护栏。

为什么需要它: `requirements.txt` 里全是 `>=`, 上游发新版就可能改变行为
(ortools 的求解器默认参数、starlette 的中间件语义都会随小版本变)。
`requirements.lock.txt` 是评审机复现的唯一依据, 一旦它与真实环境漂移,
"在我机器上是好的" 就会在评审机上重现 —— 而且是最难查的那类故障。

本文件守四件事:
  1. 锁文件存在、格式合法(全 `==` 精确钉死, 无区间/通配/重复);
  2. 锁文件覆盖 `requirements.txt` 的**每一个**直接依赖;
  3. 锁文件里的每个版本都与**当前环境实际安装的版本一致** —— 这条是防漂移的关键,
     只要有人升级了包却没重新生成锁文件, 这里立刻红;
  4. 锁文件按字母序, 便于 diff。
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_LOCK = _ROOT / "requirements.lock.txt"
_REQ = _ROOT / "requirements.txt"

# 允许 `name==1.2.3` 或 `name[extra]==1.2.3`; 版本号里允许 PEP 440 的常见字符
_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?==(?P<ver>[A-Za-z0-9._!+*-]+)$")

# 直接依赖 → 期望出现在锁文件里的规范名
_DIRECT = ("fastapi", "uvicorn", "pydantic", "ortools", "pytest")


def _norm(name: str) -> str:
    """PEP 503 规范名: 小写 + 把连续的 -_. 折叠成单个 -。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


_NAME_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?(?P<rest>.*)$")


def _parse(text: str) -> list[tuple[str, str, str]]:
    """解析 requirements 文本 → [(PEP503 规范名, 版本, 排序键)]，忽略空行与注释。

    版本取的是 `==` / `>=` 后面的部分（去掉比较运算符）。
    排序键用**原始小写名**而不是规范名 —— pip freeze 就是这么排的，
    两者在 `typing-inspection` vs `typing_extensions` 上顺序相反
    （`-`=0x2d < `_`=0x5f），用规范名比会误报"未排序"。
    """
    out: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):          # -r/-e/--index-url 之类, 本文件不用
            continue
        m = _NAME_RE.match(line)
        assert m, f"无法解析的依赖行: {raw!r}"
        name = m.group("name")
        ver = (m.group("rest") or "").strip().lstrip("=<>!~ ")
        out.append((_norm(name), ver, name.lower()))
    return out


def test_lock_file_exists() -> None:
    assert _LOCK.exists(), (
        "requirements.lock.txt 缺失 —— 评审机无法复现依赖树。"
        "用 `.venv/Scripts/python.exe -m pip freeze > requirements.lock.txt` 重新生成。"
    )


def test_lock_is_fully_pinned() -> None:
    """每一行都必须是 name==version，不允许区间/通配/环境标记。"""
    bad: list[str] = []
    for raw in _LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if not _PIN_RE.match(line):
            bad.append(raw)
    assert not bad, "锁文件里存在非精确钉死的行(必须形如 name==1.2.3):\n  " + "\n  ".join(bad)


def test_lock_has_no_duplicates() -> None:
    names = [n for n, _, _ in _parse(_LOCK.read_text(encoding="utf-8"))]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"锁文件里有重复包: {dupes}"


def test_lock_is_sorted() -> None:
    """pip freeze 本来就按原始小写名字母序, 保持它可以让 diff 只显示真正的变化。"""
    keys = [k for _, _, k in _parse(_LOCK.read_text(encoding="utf-8"))]
    assert keys == sorted(keys), "锁文件未按字母序排列，请重新生成"


def test_lock_covers_every_direct_dependency() -> None:
    locked = {n for n, _, _ in _parse(_LOCK.read_text(encoding="utf-8"))}
    direct = {n for n, _, _ in _parse(_REQ.read_text(encoding="utf-8"))}
    missing = sorted(d for d in direct if d not in locked)
    assert not missing, (
        f"requirements.txt 里的直接依赖 {missing} 没有出现在锁文件里 —— "
        "改了直接依赖就必须重新生成 requirements.lock.txt"
    )
    # 顺带守住"直接依赖没有漏改 requirements.txt"这一侧
    not_declared = sorted(d for d in _DIRECT if d not in direct)
    assert not not_declared, f"项目实际使用的直接依赖 {not_declared} 未在 requirements.txt 声明"


def test_lock_matches_installed_environment() -> None:
    """锁文件里的每个版本都必须等于当前环境装的版本 —— 防"升了包却没重生成锁"。"""
    drift: list[str] = []
    for name, ver, _ in _parse(_LOCK.read_text(encoding="utf-8")):
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            drift.append(f"{name}: 锁文件要求 {ver}，但当前环境未安装")
            continue
        if actual != ver:
            drift.append(f"{name}: 锁文件 {ver} ≠ 实际 {actual}")
    assert not drift, (
        "锁文件与当前环境已漂移，请重新 `pip freeze > requirements.lock.txt`:\n  "
        + "\n  ".join(drift)
    )


def test_lock_declares_its_platform() -> None:
    """锁文件必须写明生成平台 —— 否则换平台后 uvloop/colorama 的差异无从判断。"""
    head = _LOCK.read_text(encoding="utf-8")[:2000]
    assert "pip freeze" in head, "锁文件头部应说明它是 pip freeze 的产物"
    assert "Windows" in head or "Linux" in head or "Darwin" in head, (
        "锁文件头部应写明生成平台(Windows/Linux/Darwin)"
    )
