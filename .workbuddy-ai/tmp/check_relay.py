"""按接入文档核对配置 + 用给定 key 实测中转站。

key 由环境变量 FT_TEST_KEY 传入（不落盘、不打印原文）。

文档要点（用户截图）：
  BASE URL  https://potatoya.cn/pro100x/v1
  端点      POST /chat/completions · GET /models
  限额      20 RPM / KEY · 2 CONCURRENT · 200K TOKENS / DAY / KEY
  别名      "所有别名使用同一套 step-3.7-flash 价格与余额结算规则"
            （cURL 示例用 claude-opus-5，SDK 示例用 gpt-5.6-sol）
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://potatoya.cn/pro100x/v1"
KEY = (os.environ.get("FT_TEST_KEY") or "").strip()
ROOT = r"D:\Xuzj\ky\Project\ft"

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

if not KEY:
    print("缺少 FT_TEST_KEY 环境变量")
    sys.exit(2)

print("=" * 74)
print(" ① 配置核对（项目里存的 vs 文档写的）")
print("=" * 74)
cfg = json.load(open(os.path.join(ROOT, "app", "agent", "llm_defaults.json"), encoding="utf-8"))
user = {}
up = os.path.join(ROOT, "data", "llm_settings.json")
if os.path.exists(up):
    user = json.load(open(up, encoding="utf-8"))


def effective(key_path):
    """用户层优先（整层压过出厂层）。"""
    for src in (user, cfg):
        cur = src
        for k in key_path:
            cur = (cur or {}).get(k) if isinstance(cur, dict) else None
        if cur:
            return cur
    return ""


cfg_url = effective(["remote", "base_url"])
cfg_model = effective(["model"]) or effective(["models", "openai"])
cfg_key = effective(["remote", "api_key"])

print(f"  BASE URL   项目 = {cfg_url}")
print(f"             文档 = {BASE}")
print(f"             一致 = {cfg_url.rstrip('/') == BASE}")
print(f"  模型        项目 = {cfg_model}   （文档 SDK 示例也用这个）")
print(f"  provider    项目 = {effective(['provider'])}")
print(f"  限额        20 RPM / 2 并发 / 200K tokens/天（文档）")

same_key = bool(cfg_key) and cfg_key == KEY
print(f"\n  你给的 key  长度={len(KEY)}  sha256[:12]={hashlib.sha256(KEY.encode()).hexdigest()[:12]}")
print(f"  配置里的 key 长度={len(cfg_key)}  sha256[:12]={hashlib.sha256(str(cfg_key).encode()).hexdigest()[:12]}")
print(f"  **两者相同 = {same_key}**")
if not same_key:
    print("  ⚠ 不一样 —— 说明你要换 key，或者配置里存的是另一把")


def call(path, method="GET", payload=None, timeout=30):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t0 = time.time()
    try:
        r = op.open(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace"), round((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), round((time.time() - t0) * 1000)
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", round((time.time() - t0) * 1000)


print()
print("=" * 74)
print(" ② GET /models —— 这一步只验 key 有没有效，不碰模型推理")
print("=" * 74)
s, b, ms = call("/models")
print(f"  HTTP {s}  {ms}ms")
print("  " + b[:900].replace("\n", "\n  "))

print()
print("=" * 74)
print(" ③ POST /chat/completions —— 按文档逐个试别名")
print("=" * 74)
for model in ("gpt-5.6-sol", "claude-opus-5"):
    s, b, ms = call("/chat/completions", "POST", {
        "model": model,
        "messages": [{"role": "user", "content": "回复两个字：正常"}],
        "max_tokens": 16, "stream": False}, timeout=40)
    print(f"\n  [{model}] HTTP {s}  {ms}ms")
    try:
        d = json.loads(b)
        if s == 200:
            print(f"    回复 = {d['choices'][0]['message']['content']!r}")
            print(f"    usage = {d.get('usage')}")
        else:
            print(f"    {json.dumps(d, ensure_ascii=False)[:500]}")
    except Exception:
        print("    " + b[:500])

print()
print("=" * 74)
print(" ④ 项目自己那条路（llm.chat）")
print("=" * 74)
sys.path.insert(0, ROOT)
os.environ["FT_LLM"] = "on"
os.environ["FT_LLM_API_KEY"] = KEY        # 环境变量优先级最高，临时覆盖
from app.agent import llm  # noqa: E402
llm.breaker_reset()
t0 = time.time()
reply = llm.chat([{"role": "user", "content": "回复两个字：正常"}], timeout=30)
print(f"  llm.chat() = {reply!r}   {round((time.time()-t0)*1000)}ms")
print(f"  last_failure = {llm.last_failure()}")
print(f"  熔断 open = {llm.breaker_open()} reason = {llm.breaker_reason()!r}")
