"""服务器包护栏的变异自检 —— 每条护栏都要证明"它真的会响"。

写法沿用本项目惯例：原地改源码 → 跑护栏 → 必须**失败** → finally 还原 → 比 sha256。
"""
import hashlib
import io
import json
import os
import subprocess
import sys

ROOT = r"D:\Xuzj\ky\Project\ft"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
TEST = "tests/test_pack_server.py"

FILES = [
    "deploy/cooking-agent.service",
    "app/agent/llm_defaults.json",
    "deploy/server-setup.sh",
    "scripts/pack_server.py",
]


def sha(p):
    return hashlib.sha256(open(os.path.join(ROOT, p), "rb").read()).hexdigest()


orig = {p: open(os.path.join(ROOT, p), "rb").read() for p in FILES}
orig_sha = {p: sha(p) for p in FILES}

env = dict(os.environ)
env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"


def run():
    r = subprocess.run([PY, "-m", "pytest", TEST, "-q", "--no-header", "-x"],
                       cwd=ROOT, capture_output=True, env=env)
    out = r.stdout.decode("utf-8", "replace")
    last = [l for l in out.strip().splitlines() if l.strip()][-1] if out.strip() else ""
    return r.returncode, last


def mutate(rel, fn):
    p = os.path.join(ROOT, rel)
    data = orig[rel]
    if rel.endswith(".json"):
        d = json.loads(data.decode("utf-8"))
        fn(d)
        new = json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8")
    else:
        new = fn(data.decode("utf-8")).encode("utf-8")
    open(p, "wb").write(new)


CASES = []


def case(name, expect_test):
    def deco(fn):
        CASES.append((name, fn, expect_test))
        return fn
    return deco


# 1) systemd 把模型开关改回 off —— 最容易被改回去、也最难发现的一条
@case("systemd 的 FT_LLM 改回 off", "test_systemd默认开启模型")
def m1():
    mutate("deploy/cooking-agent.service",
           lambda t: t.replace("Environment=FT_LLM=on", "Environment=FT_LLM=off", 1))


# 2) 出厂层把 key 清空 —— 包能解包能启动，只是 AI 全程不动
@case("出厂层 api_key 清空", "test_出厂层带着可用的凭据")
def m2():
    def f(d):
        d["remote"]["api_key"] = ""
    mutate("app/agent/llm_defaults.json", f)


# 3) 部署脚本被 Windows 编辑器改成 CRLF
@case("server-setup.sh 改成 CRLF", "test_部署脚本必须是LF")
def m3():
    mutate("deploy/server-setup.sh",
           lambda t: t.replace("\n", "\r\n"))


# 4) 排除清单抄成另一份常量（漂移）
@case("pack_server 排除清单抄成独立常量", "test_排除清单与sync_publish同源")
def m4():
    mutate("scripts/pack_server.py",
           lambda t: t.replace("FORBIDDEN_NAMES = SKIP_NAMES",
                               'FORBIDDEN_NAMES = frozenset({"llm_settings.json", '
                               '"devices_user.json", "recipes_user.json", '
                               '"schedules_user.json"})'))


# 5) 校验从"中止"退化成"打印一行警告" —— 包照打，问题照发
@case("_verify 失败时只警告不中止", "test_打包脚本会在校验失败时中止")
def m5():
    mutate("scripts/pack_server.py",
           lambda t: t.replace('    print(f"[错误] {msg}", file=sys.stderr)\n'
                               '    raise SystemExit(1)',
                               '    print(f"[警告] {msg}", file=sys.stderr)\n'
                               '    return'))


print("=" * 72)
hit = 0
try:
    for name, fn, expect in CASES:
        fn()
        code, last = run()
        # 护栏必须**失败**（退出码非 0）
        caught = code != 0
        hit += caught
        print(f"{'✅ 捕获' if caught else '❌ 假绿'}  {name}")
        print(f"          {last}")
finally:
    for p, data in orig.items():
        open(os.path.join(ROOT, p), "wb").write(data)

print("=" * 72)
bad = [p for p in FILES if sha(p) != orig_sha[p]]
print(f"变异捕获: {hit}/{len(CASES)}")
print("还原校验:", "全部逐字节一致" if not bad else f"❌ 未还原: {bad}")
sys.exit(0 if hit == len(CASES) and not bad else 1)
