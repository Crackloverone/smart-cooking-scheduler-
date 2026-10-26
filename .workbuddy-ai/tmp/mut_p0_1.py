"""P0-1 护栏变异自检：把两处修复分别改回"静默丢菜"，确认护栏会响。

⚠ 用 Write 落盘再跑，不要走 heredoc —— Git Bash 的 heredoc 会把 `\n` 里的
   反斜杠改写成 `/`，导致锚点永远匹配不上（实测踩到）。
"""
import io
import os
import subprocess
import sys

NL = chr(10)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)

TARGETS = {
    "app/api/main.py": [("    if unresolved:" + NL, "    if False and unresolved:" + NL)],
    "app/spec/adapter.py": [
        ("            bad.append(raw)" + NL + "            continue" + NL, "            continue" + NL)
    ],
}

env = dict(os.environ)
env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"


def run():
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_plan_no_silent_drop.py", "-q"],
                       capture_output=True, env=env)
    return r.returncode, r.stdout.decode("utf-8", "replace").strip().splitlines()[-1]


orig = {p: io.open(p, encoding="utf-8").read() for p in TARGETS}
try:
    for i, (p, subs) in enumerate(TARGETS.items(), 1):
        t = orig[p]
        for a, b in subs:
            assert a in t, "找不到锚点 %r in %s" % (a, p)
            t = t.replace(a, b, 1)
        io.open(p, "w", encoding="utf-8", newline="").write(t)
        code, last = run()
        print("变异%d (%s) -> %s | %s" % (i, p, "FAIL ✓ 护栏响了" if code else "PASS ✗ 假绿", last))
        io.open(p, "w", encoding="utf-8", newline="").write(orig[p])
finally:
    for p, t in orig.items():
        io.open(p, "w", encoding="utf-8", newline="").write(t)
    print("已还原:", all(io.open(p, encoding="utf-8").read() == t for p, t in orig.items()))
