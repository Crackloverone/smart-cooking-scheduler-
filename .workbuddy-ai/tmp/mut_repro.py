"""可复现性护栏的变异自检 —— 每条护栏都要证明"它真的会响"。

变异点就是"把本轮修的东西改回去"，看护栏是否立刻变红。
"""
import hashlib
import json
import os
import subprocess
import sys

ROOT = r"D:\Xuzj\ky\Project\ft"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
TEST = "tests/test_reproducible.py"

FILES = ["app/scheduler/cpsat.py", "app/scheduler/model.py", "app/scheduler/engine.py"]
orig = {p: open(os.path.join(ROOT, p), "rb").read() for p in FILES}
orig_sha = {p: hashlib.sha256(orig[p]).hexdigest() for p in FILES}
env = dict(os.environ)
env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"


def run():
    r = subprocess.run([PY, "-m", "pytest", TEST, "-q", "--no-header"],
                       cwd=ROOT, capture_output=True, env=env)
    out = r.stdout.decode("utf-8", "replace")
    last = [l for l in out.strip().splitlines() if l.strip()]
    return r.returncode, (last[-1] if last else "")


def mutate(rel, old, new):
    p = os.path.join(ROOT, rel)
    t = orig[rel].decode("utf-8")
    assert old in t, f"{rel} 找不到锚点: {old[:60]!r}"
    open(p, "wb").write(t.replace(old, new, 1).encode("utf-8"))


CASES = [
    ("CP-SAT 改回多 worker",
     lambda: mutate("app/scheduler/cpsat.py",
                    'os.environ.get("FT_CPSAT_WORKERS", "1")',
                    'os.environ.get("FT_CPSAT_WORKERS", "4")')),
    ("精确模型改回多 worker",
     lambda: mutate("app/scheduler/model.py",
                    'os.environ.get("FT_EXACT_WORKERS", "1")',
                    'os.environ.get("FT_EXACT_WORKERS", "4")')),
    ("短路方案被重新引入",
     lambda: mutate("app/scheduler/engine.py",
                    "def schedule(recipes: list[Recipe], policy=None, priors: dict | None = None,",
                    "def provably_optimal(plan):\n    return False\n\n\n"
                    "def schedule(recipes: list[Recipe], policy=None, priors: dict | None = None,")),
]

print("=" * 72)
hit = 0
try:
    for name, fn in CASES:
        fn()
        code, last = run()
        caught = code != 0
        hit += caught
        print(f"{'✅ 捕获' if caught else '❌ 假绿'}  {name}")
        print(f"          {last}")
        for p, data in orig.items():
            open(os.path.join(ROOT, p), "wb").write(data)
finally:
    for p, data in orig.items():
        open(os.path.join(ROOT, p), "wb").write(data)

print("=" * 72)
bad = [p for p in FILES if hashlib.sha256(open(os.path.join(ROOT, p), "rb").read()).hexdigest() != orig_sha[p]]
print(f"变异捕获: {hit}/{len(CASES)}")
print("还原校验:", "全部逐字节一致" if not bad else f"❌ 未还原: {bad}")
sys.exit(0 if hit == len(CASES) and not bad else 1)
