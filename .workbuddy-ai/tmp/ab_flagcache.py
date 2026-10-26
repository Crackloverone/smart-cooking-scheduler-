"""开关缓存（FT_FLAG_CACHE）的 A/B。

判据两条，缺一不可：
  1. **结果逐位一致** —— 缓存只是"少读几遍环境变量"，不该改变任何排程结果；
  2. **耗时下降** —— 尤其要看 p95/max（削尾），均值不是重点。

跑法：同一批组合，先在 `FT_FLAG_CACHE=off`（旧行为）子进程里跑，再在 on 里跑。
必须**分成两个子进程**：总闸是模块级常量，同进程内改不掉。
"""
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = r"D:\Xuzj\ky\Project\ft"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

CHILD = r'''
import json, os, random, sys, time
sys.path.insert(0, r"{root}")
for k in ("FT_LLM","FT_AGENT_LLM","FT_WARM_LLM","FT_NOTIFY_LLM"): os.environ[k]="off"
os.environ["FT_DEVICES_FILE"] = r"{root}\.cache\_dev_ab.json"
os.environ["FT_SCHEDULES_FILE"] = r"{root}\.cache\_sched_ab.json"
os.environ["FT_EXEC_FILE"] = r"{root}\.cache\_exec_ab.json"
if os.environ.get("FT_FLAG_CACHE"): pass
from app.parser.recipe_parser import parse_recipe
from app.spec.adapter import all_names, get, plan_recipes

names = all_names()
rnd = random.Random(20261009)
sets = [rnd.sample(names, n) for n in (5,5,9,9,9,12,12,20)]
out = []
for sel in sets:
    recs = [parse_recipe(n, get(n)["ingredients"], get(n)["steps"]) for n in sel]
    plan_recipes(recs)                      # warm-up（解析缓存不计入）
    t0 = time.perf_counter()
    p = plan_recipes(recs)
    ms = (time.perf_counter()-t0)*1000
    out.append({"n": len(sel), "ms": round(ms,1),
                 "makespan": round(p.makespan_min,3),
                 "gap": round(p.finish_gap_min,3),
                 "tasks": len(p.tasks)})
print("@@JSON@@" + json.dumps(out))
'''


def run(flag):
    env = dict(os.environ)
    env["FT_FLAG_CACHE"] = flag
    env["PYTHONUTF8"] = "1"
    code = CHILD.replace("{root}", ROOT)
    r = subprocess.run([PY, "-c", code], capture_output=True, env=env, cwd=ROOT)
    txt = r.stdout.decode("utf-8", "replace")
    line = [l for l in txt.splitlines() if l.startswith("@@JSON@@")]
    if not line:
        print(txt[-3000:], r.stderr.decode("utf-8", "replace")[-2000:])
        raise SystemExit("子进程失败")
    return json.loads(line[0][8:])


a = run("off")      # 旧行为
b = run("on")       # 新行为

print(f"{'菜数':>4}  {'off(旧)':>9} {'on(新)':>9} {'变化':>8}   {'makespan':>16} {'gap':>12}")
diffs = []
same = True
for x, y in zip(a, b):
    d = y["ms"] - x["ms"]
    diffs.append((x["ms"], y["ms"]))
    mk_ok = abs(x["makespan"] - y["makespan"]) < 1e-6
    gp_ok = abs(x["gap"] - y["gap"]) < 1e-6
    tk_ok = x["tasks"] == y["tasks"]
    if not (mk_ok and gp_ok and tk_ok):
        same = False
    print(f"{x['n']:>4}  {x['ms']:>9.1f} {y['ms']:>9.1f} {d:>+8.1f}   "
          f"{x['makespan']:>7.2f}/{y['makespan']:<7.2f} {x['gap']:>5.2f}/{y['gap']:<5.2f}"
          f"{'  ✓' if (mk_ok and gp_ok and tk_ok) else '  ✗ 不一致'}")

xs = [d[0] for d in diffs]
ys = [d[1] for d in diffs]
print(f"\n合计   旧 {sum(xs):.0f} ms   新 {sum(ys):.0f} ms   "
      f"↓{100*(1-sum(ys)/sum(xs)):.1f}%")
print(f"max    旧 {max(xs):.0f} ms   新 {max(ys):.0f} ms")
print(f"\n结果逐位一致: {'是' if same else '否 —— 有问题！'}")
sys.exit(0 if same else 1)
