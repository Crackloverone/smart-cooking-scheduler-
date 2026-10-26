"""P0-2 护栏变异自检：把新增能力逐条打坏，确认护栏会响。"""
import io
import os
import subprocess
import sys

NL = chr(10)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)
env = dict(os.environ)
env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"

MUTS = [
    ("app/parser/recipe_parser.py",
     "    if re.search(r\"半\\s*个?\\s*小时\", text):" + NL + "        return 30.0",
     "    if False and re.search(r\"半\\s*个?\\s*小时\", text):" + NL + "        return 30.0",
     "中文数字时长"),
    ("app/parser/recipe_parser.py",
     '    if os.environ.get("FT_DUR_TIERS", "on").lower() in ("off", "0", "false", "no"):' + NL + "        return default",
     '    if True:' + NL + "        return default",
     "兜底时长分档"),
    ("app/parser/recipe_parser.py",
     '    r"排骨|五花|猪|牛|羊|鸡(?!精|粉)|鸭|鹅|肉|蹄|肘|粽|糯米|南瓜|芋|山药|土豆|藕|海带|"',
     '    r"排骨|五花|猪|牛|羊|鸡|鸭|鹅|肉|蹄|肘|粽|糯米|南瓜|芋|山药|土豆|藕|海带|"',
     "鸡精误判"),
]


def run():
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_l1_gold.py", "-q"],
                       capture_output=True, env=env)
    return r.returncode, r.stdout.decode("utf-8", "replace").strip().splitlines()[-1]


orig = {p: io.open(p, encoding="utf-8").read() for p, _, _, _ in MUTS}
try:
    for p, a, b, label in MUTS:
        t = orig[p]
        if a not in t:
            print("变异(%s) -> 锚点没找到，跳过" % label)
            continue
        io.open(p, "w", encoding="utf-8", newline="").write(t.replace(a, b, 1))
        code, last = run()
        print("变异(%s) -> %s | %s" % (label, "FAIL ✓ 护栏响了" if code else "PASS ✗ 假绿", last))
        io.open(p, "w", encoding="utf-8", newline="").write(orig[p])
finally:
    for p, t in orig.items():
        io.open(p, "w", encoding="utf-8", newline="").write(t)
    print("已还原:", all(io.open(p, encoding="utf-8").read() == t for p, t in orig.items()))
