"""端到端验证：起真服务，看界面能拿到什么。

复刻用户现场：模型链路是坏的（上游 502），然后检查
  ① /api/chat 的降级文案有没有带出原因
  ② /api/llm_status 有没有暴露 last_failure（徽标靠它才能变警示态）
  ③ 熔断有没有在**第一次**失败后就打开
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = r"D:\Xuzj\ky\Project\ft"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PORT = 8171
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

env = dict(os.environ)
# 清掉可能存在的环境变量覆盖，让它只用出厂层配置（= 服务器解包后的状态）
for k in ("FT_LLM_API_KEY", "FT_LLM_BASE_URL", "FT_LLM_MODEL", "FT_LLM_PROVIDER"):
    env.pop(k, None)
env.update({"FT_LLM": "on", "FT_AGENT_LLM": "on", "FT_WARM_LLM": "off",
            "FT_NOTIFY_LLM": "on", "PYTHONUTF8": "1",
            "FT_DEVICES_FILE": os.path.join(ROOT, ".cache", "_dev_diag.json"),
            "FT_LLM_SETTINGS_FILE": os.path.join(ROOT, ".cache", "_llm_diag.json"),
            "FT_SCHEDULES_FILE": os.path.join(ROOT, ".cache", "_sched_diag.json"),
            "FT_EXEC_FILE": os.path.join(ROOT, ".cache", "_exec_diag.json")})

srv = subprocess.Popen([PY, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1",
                        "--port", str(PORT), "--workers", "1"],
                       cwd=ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(150):
    try:
        if op.open(f"http://127.0.0.1:{PORT}/health", timeout=2).status == 200:
            break
    except Exception:
        time.sleep(0.4)

fails = []


def chk(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  | " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


def post(path, payload, timeout=60):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        r = op.open(req, timeout=timeout)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace"))
    except Exception as e:
        return 0, {"err": str(e)}


def get(path, timeout=30):
    try:
        r = op.open(f"http://127.0.0.1:{PORT}{path}", timeout=timeout)
        return r.status, json.loads(r.read().decode())
    except Exception as e:
        return 0, {"err": str(e)}


try:
    print("== 复刻用户现场：模型链路坏掉（上游拒绝），看界面能拿到什么 ==")
    s, d = post("/api/chat", {"messages": [{"role": "user", "content": "."}]})
    reply = d.get("reply", "")
    print("  助手回复:")
    for line in reply.splitlines():
        print("    " + line)
    chk("助手降级文案带出原因", "HTTP" in reply, reply[:80])
    chk("助手文案不泄漏第三方域名",
        not any(b in reply for b in ("http://", "https://", "potatoya", "stepfun")))

    s, d = get("/api/llm_status")
    lf = d.get("last_failure") or {}
    chk("/api/llm_status 暴露 last_failure", bool(lf.get("code")), lf)
    chk("last_failure 带可操作原因", bool(lf.get("hint")), lf.get("hint"))
    chk("确定性失败已立刻熔断", d.get("state") == "breaker",
        f"state={d.get('state')} reason={d.get('breaker_reason')}")
    chk("熔断文案可读", bool(d.get("breaker_reason_text")), d.get("breaker_reason_text"))

    # 第二次请求：熔断后应当零延迟（不再白等一个上游超时）
    t0 = time.time()
    post("/api/chat", {"messages": [{"role": "user", "content": "再试一次"}]})
    dt = (time.time() - t0) * 1000
    chk("熔断后第二次请求零延迟（<300ms）", dt < 300, f"{dt:.0f} ms")

    # 排程不受影响
    s, d = post("/api/plan", [{"id": 1, "name": "鸡翅包饭"}, {"id": 2, "name": "苹果芒果派"}])
    chk("模型坏了排程照常可用", s == 200 and d.get("overview", {}).get("recipeCount") == 2,
        f"http={s}")
finally:
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except Exception:
        srv.kill()

print("\n失败项:", fails if fails else "无 —— 全部通过")
sys.exit(1 if fails else 0)
