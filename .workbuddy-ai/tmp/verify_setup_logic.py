"""验证 server-setup.sh 里本轮新增的两段逻辑真的能工作。

1) `jget` —— 自检里取 JSON 字段的那个内联 python。
   它用 sys.argv 收 (PORT, 端点, 字段)，少传一个参数就会 IndexError，
   而 shell 里 `|| echo unknown` 会把 IndexError 也吞成 unknown ⇒
   自检永远显示 unknown、永远不判失败 —— 典型的"假绿"。
   所以必须实测它能取出值。

2) WITH_LLM 的 sed —— 开关要真能改成 on/off 两种，且改完 grep 复核能通过。
   sed 没匹配上是**静默**的，光看退出码分不出来。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = r"D:\Xuzj\ky\Project\ft"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PORT = 8156
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
fails = []


def chk(name, cond, detail=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  | " + detail) if detail else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------- 1. jget 逻辑（要真服务）
# 与 server-setup.sh 里的内联代码**逐字一致**
JGET = """import json, sys, urllib.request
try:
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:%s%s" % (sys.argv[1], sys.argv[2]),
                                          timeout=15).read().decode())
    print(str(d.get(sys.argv[3], "unknown")).lower())
except Exception:
    print("unknown")
"""

env = dict(os.environ)
env.update({"FT_LLM": "on", "FT_AGENT_LLM": "on", "FT_WARM_LLM": "on",
            "FT_NOTIFY_LLM": "on", "PYTHONUTF8": "1"})
srv = subprocess.Popen([PY, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1",
                        "--port", str(PORT), "--workers", "1"],
                       cwd=ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(120):
    try:
        if op.open(f"http://127.0.0.1:{PORT}/health", timeout=2).status == 200:
            break
    except Exception:
        time.sleep(0.5)


def jget(port, endpoint, field):
    r = subprocess.run([PY, "-", str(port), endpoint, field], input=JGET,
                       capture_output=True, text=True)
    return (r.stdout or "").strip()


try:
    print("== jget 取值（server-setup.sh 内联逻辑） ==")
    v = jget(PORT, "/api/llm_status", "enabled")
    chk("enabled 能取到 true", v == "true", f"得到 {v!r}")
    v = jget(PORT, "/api/llm_status", "state")
    chk("state 能取到（非 unknown）", v not in ("unknown", ""), f"得到 {v!r}")
    v = jget(PORT, "/api/model_settings", "api_key_set")
    chk("api_key_set 能取到 true", v == "true", f"得到 {v!r}")
    v = jget(PORT, "/api/model_settings", "provider")
    chk("provider 能取到 openai", v == "openai", f"得到 {v!r}")
    v = jget(PORT, "/api/llm_status", "不存在的字段")
    chk("取不到的字段回 unknown（不是崩溃）", v == "unknown", f"得到 {v!r}")
finally:
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except Exception:
        srv.kill()

# ---------------------------------------------------- 2. WITH_LLM 的 sed
print("\n== WITH_LLM 的 sed 替换（在副本上跑，不动源文件） ==")
# ⚠ 本机 Git Bash 的 `sed -i` 在 Windows 临时目录上报
#   "cannot rename: Invalid cross-device link"（Linux 服务器没有这个问题）。
#   而且它失败时**源文件保持原样** —— 源文件本来是 on，于是"改成 on"的检查
#   会照常通过 ⇒ 典型的**假绿**，正是脚本里那段 grep 复核存在的理由。
#   所以这里改用 `sed` 输出到新文件（不 -i，无 rename），只看替换结果本身。
tmp = tempfile.mkdtemp(prefix="ft_svc_")
src = os.path.join(ROOT, "deploy", "cooking-agent.service")
for want in ("off", "on"):
    cur = src
    for k in ("FT_LLM", "FT_AGENT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM"):
        out = os.path.join(tmp, f"{want}_{k}.out")
        with open(out, "w", encoding="utf-8") as fh:
            subprocess.run(["sed", f"s|^Environment={k}=.*|Environment={k}={want}|", cur],
                           stdout=fh, check=True)
        cur = out
    got = [l.strip() for l in open(cur, encoding="utf-8") if l.startswith("Environment=FT_")]
    ok = len(got) == 4 and all(g == f"Environment={k}={want}"
                               for g, k in zip(sorted(got),
                                               ["FT_AGENT_LLM", "FT_LLM",
                                                "FT_NOTIFY_LLM", "FT_WARM_LLM"]))
    chk(f"WITH_LLM={'1' if want == 'on' else '0'} → 四行都变成 {want}", ok, str(sorted(got)))
    g = subprocess.run(["grep", "-q", f"^Environment=FT_LLM={want}$", cur])
    chk(f"  复核 grep 能通过（{want}）", g.returncode == 0, f"exit={g.returncode}")
    if want == "off":
        wrong = subprocess.run(["grep", "-q", "^Environment=FT_LLM=on$", cur])
        chk("  关掉后不应再匹配 on（复核能抓到没改成功）", wrong.returncode != 0)

print("\n失败项:", fails if fails else "无 —— 全部通过")
sys.exit(1 if fails else 0)
