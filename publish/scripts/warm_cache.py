"""
离线预热: 把"模型在环"的产物提前算好落盘。

为什么要预热
------------
中转站一次往返约 2.5s, 而赛题要求初始排程 <5s。因此本项目的模型参与方式是:
    **离线把模型的判断算好, 在线只读结果** —— 模型参与的是决策本身, 不是在线算力。

预热内容
--------
1. 每道菜的调度语义先验 (趁热程度/可放时长/可提前备菜/合炉组) -> llm_agent_cache.json
2. 规则解析拿不到设备参数的菜, 用模型补全 (L1 兜底)           -> 同上
3. 常见菜组合的策略择优结论 (可选, --combos N)

用法:
    python -m scripts.warm_cache               # 先验 + 参数补全 (100 道菜)
    python -m scripts.warm_cache --combos 30   # 额外预热 30 组常见组合
    python -m scripts.warm_cache --force       # 忽略已有缓存, 重新问模型
"""
from __future__ import annotations
import argparse
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 必须在 import app.agent.llm **之前** 设置: TIMEOUT 在模块导入时就固定了。
# 预热是离线动作, 不赶时间, 给模型 20s; 在线路径仍是 3s。
os.environ.setdefault("FT_LLM_TIMEOUT", "20")
os.environ.setdefault("FT_LLM_CB_THRESHOLD", "999")   # 预热阶段不要触发熔断

from app.parser.recipe_parser import parse_recipe
from app.spec.adapter import get, all_names
from app.agent import llm
from app.agent.priors import extract_prior
from app.parser.enrich import enrich_recipe


def warm_priors(names, force=False):
    ok = cached = fail = 0
    for i, n in enumerate(names, 1):
        rec = get(n)
        r = parse_recipe(n, rec["ingredients"], rec["steps"])
        key = f"prior::{n}"
        if not force and isinstance(llm.cache_get(key), dict):
            cached += 1
            continue
        p = extract_prior(r, use_llm=True, timeout=20.0, allow_cache=False)
        if p is not None and p.source.startswith("llm"):
            ok += 1
        else:
            fail += 1
        if i % 20 == 0:
            llm.cache_flush()
            print(f"  ...{i}/{len(names)} 已算 {ok} 缓存命中 {cached} 失败 {fail}")
    llm.cache_flush()
    return ok, cached, fail


def warm_enrich(names, force=False):
    """规则解析没拿到设备参数的菜, 让模型补全。"""
    from app.models.schema import DEVICE_NONE
    COOK = ("蒸", "烤", "炖", "煮", "炒", "煎", "炸", "焯", "焗", "焖", "煲")
    PH = ("方太智能烹饪设备", "开始烹饪", "烹饪结束", "按屏幕提示")
    todo = []
    for n in names:
        rec = get(n)
        r = parse_recipe(n, rec["ingredients"], rec["steps"])
        if any(s.device == DEVICE_NONE and any(v in s.raw_text for v in COOK + PH)
               for s in r.steps):
            todo.append(n)
    done = 0
    for n in todo:
        if not force and isinstance(llm.cache_get(f"enrich::{n}"), dict):
            continue
        enrich_recipe(n, get(n)["steps"], timeout=20.0)
        done += 1
    return len(todo), done


def warm_combos(names, n_combo=30, seed=7):
    """预热常见组合的"策略择优"结论, 写进缓存供在线直接取用。"""
    from app.agent.orchestrator import run as agent_run
    random.seed(seed)
    hit = 0
    for _ in range(n_combo):
        pick = random.sample(names, random.randint(3, 5))
        recs = [parse_recipe(x, get(x)["ingredients"], get(x)["steps"]) for x in pick]
        agent_run(recs, use_llm=True, warm_priors=True, time_budget=25.0)
        hit += 1
    return hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--combos", type=int, default=0, help="额外预热的菜组合数")
    ap.add_argument("--force", action="store_true", help="忽略已有缓存重新问模型")
    args = ap.parse_args()

    names = all_names()
    print(f"官方菜谱 {len(names)} 道, 开始预热 ...")
    t0 = time.time()

    print("[1/3] 语义先验 ...")
    ok, cached, fail = warm_priors(names, force=args.force)
    print(f"      新算 {ok} / 缓存命中 {cached} / 退化规则 {fail}")

    print("[2/3] L1 参数补全 ...")
    todo, done = warm_enrich(names, force=args.force)
    print(f"      需要补全 {todo} 道, 本次调用 {done}")

    if args.combos:
        print(f"[3/3] 组合策略择优 {args.combos} 组 ...")
        print(f"      完成 {warm_combos(names, args.combos)} 组")

    print(f"\n预热完成, 用时 {time.time() - t0:.1f}s")
    print(f"缓存文件: {os.path.join(ROOT, 'data', 'llm_agent_cache.json')}")
    print("预热后在线排程只读缓存, 模型决策 0 延迟、0 网络依赖。")


if __name__ == "__main__":
    main()
