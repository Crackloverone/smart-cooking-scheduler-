"""CP-SAT 可复现性参数实验：workers × seed 对"稳定性"和"耗时"的影响。

组合：
  A  现状      workers=4, seed=0（改动前的实际配置）
  B  单线程    workers=1, seed=0（本轮默认）
  C  并行+种子 workers=4, seed=42

判据：
  1. 同一 12 菜组合连跑 6 次，makespan/gap 是否**逐位一致**；
  2. 耗时（mean / max）不能明显变差。
"""
import json
import os
import subprocess
import sys

ROOT = r"D:\Xuzj\ky\Project\ft"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

CHILD = r'''
import json, os, random, sys, time
sys.path.insert(0, r"{root}")
for k in ("FT_LLM","FT_AGENT_LLM","FT_WARM_LLM","FT_NOTIFY_LLM"): os.environ[k]="off"
os.environ["FT_DEVICES_FILE"] = r"{root}\.cache\_dev_cd.json"
os.environ["FT_SCHEDULES_FILE"] = r"{root}\.cache\_sched_cd.json"
os.environ["FT_EXEC_FILE"] = r"{root}\.cache\_exec_cd.json"
from app.parser.recipe_parser import parse_recipe
from app.spec.adapter import all_names, get, plan_recipes

names = all_names()
rnd = random.Random(20261009)
# ⚠ 必须用**与 repro_check 完全相同**的采样序列：不确定性只在特定难组合上发作
#   （换一批组合就测不到），所以这里要复刻当时那几组，尤其是已确认抖动的
#   sets[2]（12 菜，5 次里 1 次 makespan=138 其余 134）。
sets = [rnd.sample(names, n) for n in (5, 9, 12, 20)]
sets = [sets[1], sets[2], sets[3]]      # 9 / 12 / 20 菜三组
out = []
for sel in sets:
    recs = [parse_recipe(n, get(n)["ingredients"], get(n)["steps"]) for n in sel]
    plan_recipes(recs)
    rows = []
    for _ in range(6):
        t0 = time.perf_counter()
        p = plan_recipes(recs)
        rows.append([round(p.makespan_min,3), round(p.finish_gap_min,3),
                     round((time.perf_counter()-t0)*1000,1)])
    out.append({"n": len(sel), "rows": rows})
print("@@JSON@@" + json.dumps(out))
'''


def run(workers, seed):
    env = dict(os.environ)
    env["FT_CPSAT_WORKERS"] = str(workers)
    env["FT_CPSAT_SEED"] = str(seed)
    env["PYTHONUTF8"] = "1"
    r = subprocess.run([PY, "-c", CHILD.replace("{root}", ROOT)],
                       capture_output=True, env=env, cwd=ROOT)
    line = [l for l in r.stdout.decode("utf-8", "replace").splitlines()
            if l.startswith("@@JSON@@")]
    if not line:
        print(r.stdout.decode("utf-8", "replace")[-2000:])
        print(r.stderr.decode("utf-8", "replace")[-2000:])
        raise SystemExit("子进程失败")
    return json.loads(line[0][8:])


for tag, w, s in (("A 现状 workers=4 seed=0", 4, 0),
                  ("B 单线程 workers=1 seed=0", 1, 0),
                  ("C 并行+种子 workers=4 seed=42", 4, 42)):
    print("\n" + "=" * 70)
    print(" ", tag)
    print("=" * 70)
    data = run(w, s)
    tot = 0.0
    unstable = 0
    for g in data:
        rows = g["rows"]
        uniq = set((r[0], r[1]) for r in rows)
        ms = [r[2] for r in rows]
        tot += sum(ms) / len(ms)
        if len(uniq) > 1:
            unstable += 1
        print(f"  {g['n']:>3} 菜  {'稳定' if len(uniq)==1 else f'✗ 不稳定({len(uniq)}种)'}  "
              f"mean {sum(ms)/len(ms):>7.0f} ms  max {max(ms):>7.0f} ms  "
              f"makespan {sorted({r[0] for r in rows})}")
    print(f"  → 不稳定组合数 {unstable} / {len(data)}   合计 mean {tot:.0f} ms")
