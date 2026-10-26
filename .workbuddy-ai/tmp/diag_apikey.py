"""诊断 API Key 调用失败：直接打一次中转站，看真实返回。

为什么必须直连实测
------------------
界面徽标的「AI在线」只反映**熔断器状态**（开关开着 + 凭据非空），
它**不反映调用是否成功**。所以"徽标在线 + 助手降级"这种组合下，
只看界面永远查不出原因 —— 必须把请求真的打出去看状态码。

⚠ 不打印 key 原文，只打印打码预览与长度。
"""
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

ROOT = r"D:\Xuzj\ky\Project\ft"
sys.path.insert(0, ROOT)

# 先把环境变量清干净，让配置只来自出厂层/用户层（模拟服务器解包后的状态）
for k in ("FT_LLM", "FT_AGENT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM",
          "FT_LLM_API_KEY", "FT_LLM_BASE_URL", "FT_LLM_MODEL", "FT_LLM_PROVIDER"):
    os.environ.pop(k, None)
os.environ["FT_LLM"] = "on"

from app.agent import llm  # noqa: E402

print("=" * 74)
print(" 配置解析结果（不打印 key 原文）")
print("=" * 74)
print(f"  provider      = {llm.PROVIDER}")
print(f"  base_url      = {llm.BASE_URL}")
print(f"  model         = {llm.MODEL}")
print(f"  model_source  = {llm.MODEL_SOURCE}")
print(f"  key 已装载    = {bool(llm.API_KEY)}   长度={len(llm.API_KEY or '')}")
if llm.API_KEY:
    print(f"  key 预览      = {llm.API_KEY[:7]}…{llm.API_KEY[-4:]}")
print(f"  enabled()     = {llm.enabled()}")
print(f"  api_key_required = {llm.api_key_required()}")
print(f"  timeout       = {llm.TIMEOUT}s")
print(f"  熔断器        = open={llm.breaker_open()} reason={llm.breaker_reason()!r}")

print()
print("=" * 74)
print(" 直连中转站（绕过代理）")
print("=" * 74)
url = llm.BASE_URL.rstrip("/") + "/chat/completions"
payload = {
    "model": llm.MODEL,
    "messages": [{"role": "user", "content": "回复两个字：正常"}],
    "max_tokens": 16,
    "stream": False,
}
body = json.dumps(payload).encode()
req = urllib.request.Request(
    url, data=body,
    headers={"Content-Type": "application/json",
             "Authorization": f"Bearer {llm.API_KEY}"},
    method="POST")

# ① 直连（不走任何代理）
op_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
t0 = time.time()
try:
    r = op_direct.open(req, timeout=30)
    txt = r.read().decode("utf-8", "replace")
    print(f"  [直连] HTTP {r.status}  {round((time.time()-t0)*1000)}ms")
    print(f"  {txt[:600]}")
except urllib.error.HTTPError as e:
    txt = e.read().decode("utf-8", "replace")
    print(f"  [直连] HTTP {e.code}  {round((time.time()-t0)*1000)}ms")
    print(f"  {txt[:900]}")
except Exception as e:
    print(f"  [直连] 失败: {type(e).__name__}: {e}")

# ② 走环境里的代理（如果有）
proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or ""
print()
print(f"  环境代理 = {proxy or '（无）'}")
if proxy:
    op_proxy = urllib.request.build_opener()
    t0 = time.time()
    try:
        r = op_proxy.open(req, timeout=30)
        txt = r.read().decode("utf-8", "replace")
        print(f"  [代理] HTTP {r.status}  {round((time.time()-t0)*1000)}ms")
        print(f"  {txt[:300]}")
    except urllib.error.HTTPError as e:
        print(f"  [代理] HTTP {e.code}  {round((time.time()-t0)*1000)}ms")
        print(f"  {e.read().decode('utf-8','replace')[:300]}")
    except Exception as e:
        print(f"  [代理] 失败: {type(e).__name__}: {e}")

# ③ 走系统自己的 llm.chat（与助手实际路径一致），开日志看它怎么失败
print()
print("=" * 74)
print(" 走 llm.chat()（助手实际用的那条路）")
print("=" * 74)
import logging  # noqa: E402
logging.basicConfig(level=logging.DEBUG, format="    [%(levelname)s] %(name)s: %(message)s")
t0 = time.time()
reply = llm.chat([{"role": "user", "content": "回复两个字：正常"}],
                 temperature=0.2, timeout=30)
print(f"  返回 = {reply!r}   耗时 {round((time.time()-t0)*1000)}ms")
print(f"  熔断器 = open={llm.breaker_open()} reason={llm.breaker_reason()!r}")
