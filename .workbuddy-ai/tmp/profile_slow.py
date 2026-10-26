"""定位"为什么 9 菜的 max 比 12 菜还高"——耗时到底花在哪。

实测里反常的一点：
    9 菜  max 1787ms
    12 菜 max 1232ms
菜数更多反而更快 ⇒ 耗时**不只由菜数决定**，而是由组合的求解难度决定。
方差（9 菜 min 230 / max 1787 = 7.8 倍）才是真正的风险：
开发机余量 4 倍，换到弱 CPU 云服务器（通常慢 2–3 倍）可能只剩 1.3 倍。

所以目标是**削尾**，不是削均值。先找出最慢的组合，再 profile 它。
"""
import cProfile
import io as _io
import os
import pstats
import random
import sys
import time

for k in ("FT_LLM", "FT_AGENT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
    os.environ[k] = "off"
os.environ.setdefault("FT_DEVICES_FILE", os.path.join(os.getcwd(), ".cache", "_dev_prof.json"))
os.environ.setdefault("FT_LLM_SETTINGS_FILE", os.path.join(os.getcwd(), ".cache", "_llm_prof.json"))
os.environ.setdefault("FT_SCHEDULES_FILE", os.path.join(os.getcwd(), ".cache", "_sched_prof.json"))
os.environ.setdefault("FT_EXEC_FILE", os.path.join(os.getcwd(), ".cache", "_exec_prof.json"))

sys.path.insert(0, os.getcwd())

from app.parser.recipe_parser import parse_recipe          # noqa: E402
from app.spec.adapter import all_names, get, plan_recipes  # noqa: E402

names = all_names()
print("菜谱库", len(names), "道")

rnd = random.Random(20261009)


def build(sel):
    out = []
    for n in sel:
        rec = get(n)
        out.append(parse_recipe(n, rec["ingredients"], rec["steps"]))
    return out


# ---------------------------------------------------------------- 1. 扫慢组合
rows = []
for i in range(24):
    n = 9
    sel = rnd.sample(names, n)
    recs = build(sel)
    t0 = time.perf_counter()
    p = plan_recipes(recs)
    ms = (time.perf_counter() - t0) * 1000
    rows.append((ms, sel, p.makespan_min, p.finish_gap_min))

rows.sort(key=lambda r: -r[0])
print("\n== 9 菜 × 24 组，最慢 5 组 ==")
for ms, sel, mk, gap in rows[:5]:
    print(f"  {ms:8.1f} ms   总时长 {mk:6.1f}  出锅差 {gap:5.1f}   {'+'.join(sel[:4])}…")
print("\n== 最快 3 组 ==")
for ms, sel, mk, gap in rows[-3:]:
    print(f"  {ms:8.1f} ms   总时长 {mk:6.1f}  出锅差 {gap:5.1f}   {'+'.join(sel[:4])}…")

xs = [r[0] for r in rows]
xs.sort()
print(f"\n  9 菜耗时: min {xs[0]:.0f}  p50 {xs[len(xs)//2]:.0f}  "
      f"p95 {xs[int(len(xs)*0.95)]:.0f}  max {xs[-1]:.0f} ms")

# ---------------------------------------------------------------- 2. profile 最慢组
worst = rows[0][1]
print("\n== 对最慢组做 cProfile ==")
print("  ", " + ".join(worst))
recs = build(worst)

# 先跑一次预热（解析缓存 / 导入开销不算）
plan_recipes(recs)

pr = cProfile.Profile()
pr.enable()
for _ in range(3):
    plan_recipes(recs)
pr.disable()
s = _io.StringIO()
ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
ps.print_stats(22)
print(s.getvalue())

print("== 只看 tottime（自身耗时，排除子调用）==")
s2 = _io.StringIO()
pstats.Stats(pr, stream=s2).sort_stats("tottime").print_stats(18)
print(s2.getvalue())
