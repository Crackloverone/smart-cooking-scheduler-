"""可证最优短路（FT_PROOF_SKIP）的 A/B。

判据（顺序即优先级）：
  1. **结果不许变差** —— 短路的合法性来自"下界是放松"，所以贪心解达到下界时
     精解不可能更优。若实测出现"on 更差"，说明下界算错了（那是真 bug，不是调参）。
  2. **命中率** —— 有多少实例真的被短掉了。
  3. **耗时** —— 均值与尾部（p95/max）。

跑法：两个子进程（开关是模块级常量）。
"""
import json
import os
import random
import subprocess
import statistics
import sys

ROOT = r"D:\Xuzj\ky\Project\ft"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

CHILD = r'''
import json, os, random, sys, time
sys.path.insert(0, r"{root}")
for k in ("FT_LLM","FT_AGENT_LLM","FT_WARM_LLM","FT_NOTIFY_LLM"): os.environ[k]="off"
os.environ["FT_DEVICES_FILE"] = r"{root}\.cache\_dev_ps.json"
os.environ["FT_SCHEDULES_FILE"] = r"{root}\.cache\_sched_ps.json"
os.environ["FT_EXEC_FILE"] = r"{root}\.cache\_exec_ps.json"
from app.parser.recipe_parser import parse_recipe
from app.scheduler import engine
from app.spec.adapter import all_names, get, plan_recipes

names = all_names()
rnd = random.Random(42)
sets = []
for _ in range(40):                       # 赛题范围 3–5 菜，40 组
    sets.append(rnd.sample(names, rnd.randint(3, 5)))
for n in (9, 9, 12, 12, 20):              # 超出范围的大组合
    sets.append(rnd.sample(names, n))

out = []
for sel in sets:
    recs = [parse_recipe(n, get(n)["ingredients"], get(n)["steps"]) for n in sel]
    plan_recipes(recs)                    # warm-up
    # 先拿贪心解，判"是否可证最优"（短路的命中条件）
    greedy = engine.schedule(recs, use_cpsat=False)
    hit = engine.provably_optimal(greedy)
    t0 = time.perf_counter()
    p = plan_recipes(recs)
    ms = (time.perf_counter()-t0)*1000
    out.append({"n": len(sel), "hit": bool(hit), "ms": round(ms,1),
                "makespan": round(p.makespan_min,3), "gap": round(p.finish_gap_min,3),
                "tasks": len(p.tasks)})
print("@@JSON@@" + json.dumps(out))
'''


def run(flag):
    env = dict(os.environ)
    env["FT_PROOF_SKIP"] = flag
    env["PYTHONUTF8"] = "1"
    r = subprocess.run([PY, "-c", CHILD.replace("{root}", ROOT)],
                       capture_output=True, env=env, cwd=ROOT)
    txt = r.stdout.decode("utf-8", "replace")
    line = [l for l in txt.splitlines() if l.startswith("@@JSON@@")]
    if not line:
        print(txt[-2500:], r.stderr.decode("utf-8", "replace")[-2000:])
        raise SystemExit("子进程失败")
    return json.loads(line[0][8:])


off = run("off")
on = run("on")

print(f"共 {len(off)} 组；其中**可证最优**（能短路）{sum(1 for x in off if x['hit'])} 组\n")
print(f"{'菜数':>4} {'短路':>4} {'off(旧)':>9} {'on(新)':>9} {'变化':>8}  "
      f"{'makespan off/on':>18} {'gap off/on':>14}")
worse, better, same = [], 0, 0
so = sy = 0.0
for x, y in zip(off, on):
    so += x["ms"]; sy += y["ms"]
    mk_same = abs(x["makespan"] - y["makespan"]) < 1e-6
    gp_same = abs(x["gap"] - y["gap"]) < 1e-6
    tk_same = x["tasks"] == y["tasks"]
    # 判"更差"：Γ 变大，或 Γ 相同但 T 变大（字典序）
    worse_off = (y["gap"] > x["gap"] + 1e-6) or (
        abs(y["gap"] - x["gap"]) < 1e-6 and y["makespan"] > x["makespan"] + 1e-6)
    better_off = (y["gap"] < x["gap"] - 1e-6) or (
        abs(y["gap"] - x["gap"]) < 1e-6 and y["makespan"] < x["makespan"] - 1e-6)
    if worse_off:
        worse.append((x, y))
    elif better_off:
        better += 1
    elif mk_same and gp_same and tk_same:
        same += 1
    print(f"{x['n']:>4} {'✓' if x['hit'] else ' ':>4} {x['ms']:>9.1f} {y['ms']:>9.1f} "
          f"{y['ms']-x['ms']:>+8.1f}  {x['makespan']:>7.2f}/{y['makespan']:<7.2f} "
          f"{x['gap']:>5.2f}/{y['gap']:<5.2f}"
          f"{'  ✗更差' if worse_off else ('  ↑更优' if better_off else '')}")

print(f"\n耗时合计  旧 {so:.0f} ms   新 {sy:.0f} ms   ↓{100*(1-sy/so):.1f}%")
print(f"逐位一致 {same} 组 · 新更优 {better} 组 · **新更差 {len(worse)} 组**")
for x, y in worse[:5]:
    print(f"   ✗ {x['n']} 菜: gap {x['gap']}→{y['gap']}  makespan {x['makespan']}→{y['makespan']}")
sys.exit(1 if worse else 0)
