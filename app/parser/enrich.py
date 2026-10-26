"""
L1 大模型补全: 对规则解析拿不到设备/温度/时长的占位步骤,
调用 LLM 推断烹饪参数。结果按菜名持久化缓存, 第二次起毫秒级。

v2.1 修复
---------
原实现自带一套 urllib 调用与 12s 超时, **绕过了统一 LLM 客户端**:
既不受 FT_LLM 离线开关控制, 也没有熔断, 于是在离线/评审环境下
一次排程会凭空多等 10 秒以上 —— 直接吃掉性能分。
现在统一走 app.agent.llm: 共享开关 / 超时 / 熔断 / 磁盘缓存。
"""
from __future__ import annotations
import os

from app.agent import llm

_OLD_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data",
                               "llm_enrich_cache.json")
_MIGRATED = False


def _migrate():
    """把 v2.0 时期的 enrich 缓存迁到统一缓存文件, 避免重复调用模型。"""
    global _MIGRATED
    if _MIGRATED or not os.path.exists(_OLD_CACHE_PATH):
        _MIGRATED = True
        return
    _MIGRATED = True
    try:
        import json
        with open(_OLD_CACHE_PATH, encoding="utf-8") as f:
            old = json.load(f)
        dirty = False
        for name, val in (old or {}).items():
            k = f"enrich::{name}"
            if llm.cache_get(k) is None:
                llm.cache_put(k, val)
                dirty = True
        if dirty:
            llm.cache_flush()
    except Exception:
        pass


_SYS = "你是厨电参数推断器, 只输出 JSON。"


def enrich_recipe(name: str, steps_text: str, timeout: float | None = None) -> dict:
    """返回 {step_index: {device, temperature, duration_min, mode}} 或 {}。带缓存。"""
    _migrate()
    key = f"enrich::{name}"
    hit = llm.cache_get(key)
    if isinstance(hit, dict):
        return hit
    # 离线开关 / 熔断: 任何一项不允许就直接返回空, 由规则兜底
    if not llm.enabled() or llm.breaker_open():
        return {}

    prompt = f"""这道菜叫「{name}」。下面是它的烹饪步骤。
请为每一步中真正用到厨房设备加热的步骤, 推断: 用哪个设备(只能是 灶具/蒸箱/烤箱/冰箱/洗碗机/净水机 之一), 温度(数字℃, 灶具可不填用火力), 时长(分钟), 模式。
只输出 JSON, 不要解释。格式: {{"steps":[{{"i":步骤序号,"device":"蒸箱","temperature":100,"duration_min":15,"mode":"普通蒸"}}]}}
人工切洗步骤不用输出。

步骤:
{steps_text}"""

    out = llm.chat_json(_SYS, prompt, cache_key=key, temperature=0.1,
                        timeout=timeout or llm.TIMEOUT)
    if not isinstance(out, dict):
        # 失败也缓存空, 避免每道菜每次都等超时
        llm.cache_put(key, {})
        llm.cache_flush()
        return {}
    try:
        parsed = {int(s["i"]): {"device": s.get("device"),
                                "temperature": s.get("temperature"),
                                "duration_min": s.get("duration_min"),
                                "mode": s.get("mode", "")}
                  for s in (out.get("steps") or []) if "i" in s}
    except Exception:
        parsed = {}
    llm.cache_put(key, parsed)
    llm.cache_flush()
    return parsed
