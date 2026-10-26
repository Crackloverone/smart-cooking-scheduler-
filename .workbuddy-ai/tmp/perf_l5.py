"""L5 性能实测：**双档**（FT_LLM=off 离线档 / FT_LLM=on 开模型档）。

为什么必须测两档
----------------
此前所有性能数字（121ms 等）都是 `FT_LLM=off` 跑出来的，而服务器部署包现在
**默认 FT_LLM=on**（出厂层凭据生效）。开模型后耗时会走完全不同的路径
（策略择优 / 目标塑形 / 通知润色都是在线调用），拿离线档的数当结论会误判。

评分口径（赛题 L5）：
    初始排程  合格 <10s  优秀 <5s
    动态重排  合格 <8s   优秀 <3s
"""
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = r"D:\Xuzj\ky\Project\ft"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def start(port, llm_on):
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "FT_LLM": "on" if llm_on else "off",
        "FT_AGENT_LLM": "on" if llm_on else "off",
        "FT_WARM_LLM": "off",       # 不预热，才看得到"第一个请求"的真实代价
        "FT_NOTIFY_LLM": "on" if llm_on else "off",
        "FT_DEVICES_FILE": os.path.join(ROOT, ".cache", "_dev_perf.json"),
        "FT_LLM_SETTINGS_FILE": os.path.join(ROOT, ".cache", "_llm_perf.json"),
        "FT_SCHEDULES_FILE": os.path.join(ROOT, ".cache", "_sched_perf.json"),
        "FT_EXEC_FILE": os.path.join(ROOT, ".cache", "_exec_perf.json"),
    })
    p = subprocess.Popen([PY, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1",
                          "--port", str(port), "--workers", "1"],
                         cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        try:
            if op.open(f"http://127.0.0.1:{port}/health", timeout=2).status == 200:
                return p
        except Exception:
            time.sleep(0.4)
    return p


def post(port, path, payload, timeout=180):
    t0 = time.time()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        r = op.open(req, timeout=timeout)
        body = r.read()
        return r.status, json.loads(body.decode()), (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, {}, (time.time() - t0) * 1000
    except Exception as e:
        return 0, {"err": str(e)}, (time.time() - t0) * 1000


def get(port, path, timeout=60):
    t0 = time.time()
    try:
        r = op.open(f"http://127.0.0.1:{port}{path}", timeout=timeout)
        r.read()
        return r.status, (time.time() - t0) * 1000
    except Exception:
        return 0, (time.time() - t0) * 1000


def stats(xs):
    xs = sorted(xs)
    n = len(xs)
    return {
        "n": n, "min": round(xs[0], 1), "mean": round(statistics.mean(xs), 1),
        "p50": round(xs[n // 2], 1),
        "p95": round(xs[min(n - 1, int(n * 0.95))], 1), "max": round(xs[-1], 1),
    }


def run(llm_on, port):
    tag = "FT_LLM=on " if llm_on else "FT_LLM=off"
    print("\n" + "=" * 74)
    print(f"  {tag}  （不预热，第一个请求即冷启动）")
    print("=" * 74)
    srv = start(port, llm_on)
    try:
        s, names_d = post(port, "/api/recipe_names", None) if False else (None, None)
        st, names_b = get(port, "/api/recipe_names")
        names = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/recipe_names", timeout=30).read().decode())["names"]
        print(f"  菜谱库 {len(names)} 道")

        # ---- 冷启动第一个请求（关键：评分脚本第一次调用就是这个）
        items = [{"id": i + 1, "name": names[i]} for i in range(5)]
        s, d, ms = post(port, "/api/plan", items)
        print(f"  [冷启动首请求] 5 菜  http={s}  {ms:.0f} ms")

        # ---- 稳态：不同菜数
        print(f"  {'菜数':>4}  {'n':>3}  {'min':>8} {'mean':>8} {'p95':>8} {'max':>8}")
        for n in (3, 5, 9, 12, 20):
            xs = []
            for k in range(5):
                it = [{"id": i + 1, "name": names[(i + k * 7) % len(names)]} for i in range(n)]
                s, d, ms = post(port, "/api/plan", it)
                if s == 200:
                    xs.append(ms)
            if xs:
                st_ = stats(xs)
                print(f"  {n:>4}  {st_['n']:>3}  {st_['min']:>8.0f} {st_['mean']:>8.0f} "
                      f"{st_['p95']:>8.0f} {st_['max']:>8.0f}")

        # ---- /schedule（带 KPI / 通知）
        xs = []
        for k in range(5):
            it = [{"name": names[(i + k * 3) % len(names)]} for i in range(5)]
            s, d, ms = post(port, "/schedule", {"recipes": it, "notify": True})
            if s == 200:
                xs.append(ms)
        if xs:
            st_ = stats(xs)
            print(f"  /schedule 5 菜(带通知)      mean {st_['mean']:.0f}  max {st_['max']:.0f} ms")

        # ---- /reschedule（L5 另一半：优秀 <3s）
        xs = []
        for k in range(5):
            it = [{"name": names[(i + k * 5) % len(names)]} for i in range(4)]
            s, d0, _ = post(port, "/schedule", {"recipes": it, "notify": False})
            done = [t["task_id"] for t in (d0.get("plan", {}).get("tasks") or [])[:3]]
            s, d, ms = post(port, "/reschedule", {"recipes": it, "completed": done,
                                                  "extra": {"name": names[(k + 40) % len(names)]}})
            if s == 200:
                xs.append(ms)
        if xs:
            st_ = stats(xs)
            print(f"  /reschedule 4菜+加菜        mean {st_['mean']:.0f}  max {st_['max']:.0f} ms")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()


print("L5 性能实测 —— 评分线：初始 <5s(优秀)/<10s(合格)；重排 <3s(优秀)/<8s(合格)")
run(False, 8161)
run(True, 8162)
print("\n完成")
