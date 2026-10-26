# -*- coding: utf-8 -*-
"""A/B: 逐条关掉 v3.1 的三处解析改动, 看指标到底是谁造成的。

只读 monkeypatch, 不改任何文件。
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("FT_AGENT_LLM", "off")
os.environ.setdefault("FT_LLM", "off")
os.environ.setdefault("FT_WARM_LLM", "off")
os.environ.setdefault("FT_NOTIFY_LLM", "off")

from app.parser import recipe_parser as RP  # noqa: E402
from app.spec import device_spec as DS  # noqa: E402
from app.spec.adapter import get, all_names, plan_recipes  # noqa: E402
from app.parser.recipe_parser import parse_recipe  # noqa: E402
from app.scheduler.metrics import evaluate  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 50
SEED = 42
names = all_names()

_real_strip = DS.strip_parts
_real_retr = RP.is_fridge_retrieval
_real_combo = RP.combo_device_of


def set_cfg(strip=True, fridge=True, combo=True):
    DS.strip_parts = _real_strip if strip else (lambda t: t or "")
    RP.is_fridge_retrieval = _real_retr if fridge else (lambda t: False)
    RP.combo_device_of = _real_combo if combo else (lambda t: None)


def run():
    random.seed(SEED)
    gaps, spans, hands, bad = [], [], [], 0
    for _ in range(N):
        pick = random.sample(names, random.randint(3, 5))
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        p = plan_recipes(recs, agent=True)
        k = evaluate(p)
        gaps.append(p.finish_gap_min)
        spans.append(p.makespan_min)
        hands.append(k.hand_streak_min)
        bad += k.conflict_count + k.depend_violation
    keep = [i for i, g in enumerate(gaps) if g < 300]
    g2 = [gaps[i] for i in keep]
    return dict(
        med=sorted(g2)[len(g2) // 2], mean=round(sum(g2) / len(g2), 2),
        ok=f"{sum(1 for g in g2 if g <= 5) / len(g2) * 100:.0f}%",
        span=round(sum(spans[i] for i in keep) / len(keep), 1),
        hand=round(sum(hands[i] for i in keep) / len(keep), 1),
        bad=bad)


CONFIGS = [
    ("全部开启(当前)", dict(strip=True, fridge=True, combo=True)),
    ("关 配件词剥离", dict(strip=False, fridge=True, combo=True)),
    ("关 冰箱取出", dict(strip=True, fridge=False, combo=True)),
    ("关 复合设备继承", dict(strip=True, fridge=True, combo=False)),
    ("三处全关(=v3.0)", dict(strip=False, fridge=False, combo=False)),
]

print(f"N={N} seed={SEED}")
print(f"{'配置':<22}{'出锅差中位':>10}{'均值':>8}{'<5min':>8}{'总时长':>9}{'连续手动':>10}{'冲突':>6}")
for label, cfg in CONFIGS:
    set_cfg(**cfg)
    r = run()
    print(f"{label:<22}{r['med']:>10}{r['mean']:>8}{r['ok']:>8}"
          f"{r['span']:>9}{r['hand']:>10}{r['bad']:>6}")

set_cfg()
