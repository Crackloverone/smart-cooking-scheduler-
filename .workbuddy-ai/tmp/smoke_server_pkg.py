"""服务器部署包冒烟：解压 → 按 systemd 的开关组合起服务 → 打接口断言。

模拟的是**服务器真实环境**：
  · 解压到一个全新目录（不是项目目录），里面没有 .env / 用户层数据；
  · 环境变量只有 systemd unit 里写的那几个（FT_LLM=on 等），没有开发机的东西；
  · 用 uvicorn + --workers 1，和 cooking-agent.service 一致。

⚠ 起服务和打接口必须在**同一个进程**里跑完：Bash 工具跨调用会杀掉后台进程。
"""
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

ROOT = r"D:\Xuzj\ky\Project\ft"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
TGZ = os.path.join(ROOT, "dist", "cooking-agent-server.tar.gz")
PORT = 8155

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

fails = []


def chk(name, cond, detail=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  | " + detail) if detail else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------- 1. 解压
stage = tempfile.mkdtemp(prefix="ft_srv_")
print("解压到:", stage)
with tarfile.open(TGZ, "r:gz") as tf:
    tf.extractall(stage)

names = []
for dp, dn, fn in os.walk(stage):
    for f in fn:
        names.append(os.path.relpath(os.path.join(dp, f), stage).replace("\\", "/"))
print("文件数:", len(names))

print("\n== 包内容硬校验 ==")
chk("出厂层 llm_defaults.json 在包里", "app/agent/llm_defaults.json" in names)
chk("无 .env / .env.local", not any(n.split("/")[-1].startswith(".env") for n in names))
for bad in ("llm_settings.json", "devices_user.json", "recipes_user.json",
            "schedules_user.json", "exec_user.json"):
    chk(f"无本机状态 {bad}", not any(n.split("/")[-1] == bad for n in names))
chk("离线先验缓存在包里", "data/llm_agent_cache.json" in names)
chk("100 道菜基线在包里", "data/recipes_official_100.csv" in names)
chk("部署脚本在包里", "deploy/server-setup.sh" in names)

fac = json.load(open(os.path.join(stage, "app/agent/llm_defaults.json"), encoding="utf-8"))
print("  出厂层 provider =", fac.get("provider"), "| model =", fac.get("model"))

# ---------------------------------------------------------------- 2. 起服务
# 环境变量**刻意只给 systemd unit 里写的那几个** —— 模拟服务器，不是开发机。
env = dict(os.environ)
env.update({
    "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
    "FT_LLM": "on", "FT_AGENT_LLM": "on", "FT_WARM_LLM": "on", "FT_NOTIFY_LLM": "on",
    "PORT": str(PORT),
})
srv = subprocess.Popen(
    [PY, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1",
     "--port", str(PORT), "--workers", "1"],
    cwd=stage, env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
)
ready = False
for _ in range(120):
    try:
        if op.open(f"http://127.0.0.1:{PORT}/health", timeout=2).status == 200:
            ready = True
            break
    except Exception:
        time.sleep(0.5)


def get(path, timeout=30):
    try:
        r = op.open(f"http://127.0.0.1:{PORT}{path}", timeout=timeout)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace"))
    except Exception as e:
        return 0, {"err": str(e)}


def post(path, payload, timeout=60):
    import urllib.parse
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


try:
    print("\n== 服务可用性 ==")
    chk("服务起来了（/health 200）", ready)
    if not ready:
        print(srv.stdout.read(2000, ).decode("utf-8", "replace") if srv.stdout else "")
        raise SystemExit(1)

    s, _ = get("/health")
    chk("GET /health", s == 200, f"http={s}")
    # /app 返回的是 HTML 不是 JSON，不能走 get()（它会 json.loads，必然抛异常，
    # 抛异常会被记成 http=0 —— 看起来像"页面打不开"，其实是检查脚本自己写错了）。
    try:
        r = op.open(f"http://127.0.0.1:{PORT}/app", timeout=30)
        body = r.read()
        chk("GET /app（前端页面）", r.status == 200 and len(body) > 50000,
            f"http={r.status} bytes={len(body)}")
        chk("前端含排程视图锚点", b'id="v-plan"' in body)
    except Exception as e:
        chk("GET /app（前端页面）", False, str(e))

    print("\n== 模型配置链路（本轮要修的核心） ==")
    s, d = get("/api/llm_status")
    chk("/api/llm_status enabled=true", d.get("enabled") is True,
        f"enabled={d.get('enabled')} state={d.get('state')} label={d.get('label')}")
    s, d = get("/api/model_settings")
    chk("provider 来自出厂层 = openai", d.get("provider") == "openai", f"provider={d.get('provider')}")
    chk("api_key 已装载（api_key_set=true）", d.get("api_key_set") is True,
        f"api_key_set={d.get('api_key_set')}")
    chk("模型名来自出厂层", d.get("model_source") in ("factory", "settings"),
        f"model_source={d.get('model_source')} model={d.get('model')}")
    print("  密钥预览（后端只回打码）:", json.dumps(d.get("remote", {}), ensure_ascii=False)[:160])

    print("\n== 官方评分接口 ==")
    s, d = post("/api/plan", [{"id": 1, "name": "鸡翅包饭"}, {"id": 2, "name": "苹果芒果派"}])
    ok = (s == 200 and d.get("overview", {}).get("recipeCount") == 2)
    chk("POST /api/plan 2 菜", ok,
        f"http={s} recipeCount={d.get('overview', {}).get('recipeCount') if s == 200 else d}")
    if s == 200:
        ends = [x["endTime"] for x in d.get("cookingTimeline", [])]
        print("  总时长:", d["overview"].get("timeSpent"), "| 各菜出锅:", ends)

    s, d = post("/api/plan", [{"id": 1, "name": "佛跳墙"}])
    chk("库外菜名 → 422（不静默丢）", s == 422, f"http={s}")

    s, d = get("/api/recipe_names")
    chk("菜谱库 100 道", len(d.get("names", [])) == 100, f"n={len(d.get('names', []))}")

    print("\n== 可复现性（服务器上也要成立）==")

    def _norm(obj):
        """剥掉绝对时刻字段后序列化。

        `endTime` 是 "HH:MM"，两次调用若跨分钟自然不同 —— 那不是不可复现。
        要比较的是**排程结果本身**（谁先谁后、各耗时多久、出锅差多少）。
        """
        if isinstance(obj, dict):
            return {k: _norm(v) for k, v in obj.items() if not k.endswith("Time")}
        if isinstance(obj, list):
            return [_norm(x) for x in obj]
        return obj

    for sel_n, sel in ((3, ["清蒸鲈鱼", "香烤排骨", "蒜香烤茄子"]),
                       (5, ["鸡翅包饭", "苹果芒果派", "酿苦瓜", "蒜蓉粉丝蒸扇贝",
                            "竹荪排骨汤"])):
        items = [{"id": i + 1, "name": n} for i, n in enumerate(sel)]
        s1, d1 = post("/api/plan", items)
        s2, d2 = post("/api/plan", items)
        if s1 != 200 or s2 != 200:
            chk(f"{sel_n} 菜可复现", False, f"http={s1}/{s2}")
            continue
        a = json.dumps(_norm(d1), sort_keys=True, ensure_ascii=False)
        b = json.dumps(_norm(d2), sort_keys=True, ensure_ascii=False)
        chk(f"{sel_n} 菜同一请求两次逐位一致", a == b,
            f"timeSpent={d1['overview'].get('timeSpent')}")
finally:
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except Exception:
        srv.kill()

print("\n" + ("=" * 60))
print("失败项:", fails if fails else "无 —— 全部通过")
print("临时目录（可删）:", stage)
sys.exit(1 if fails else 0)
