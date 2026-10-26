"""可观测性护栏的变异自检：每条护栏都要证明"它真的会响"。

变异点就是"把本轮修的东西改回去"，看护栏是否立刻变红。
"""
import hashlib
import os
import subprocess
import sys

ROOT = r"D:\Xuzj\ky\Project\ft"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
TEST = "tests/test_llm_diagnostics.py"

FILES = ["app/agent/llm.py", "app/chat/assistant.py", "app/api/main.py",
         "app/web/index.html"]
orig = {p: open(os.path.join(ROOT, p), "rb").read() for p in FILES}
orig_sha = {p: hashlib.sha256(orig[p]).hexdigest() for p in FILES}
env = dict(os.environ)
env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"


def run():
    r = subprocess.run([PY, "-m", "pytest", TEST, "-q", "--no-header"],
                       cwd=ROOT, capture_output=True, env=env)
    out = r.stdout.decode("utf-8", "replace")
    last = [l for l in out.strip().splitlines() if l.strip()]
    return r.returncode, (last[-1] if last else "")


def mutate(rel, old, new):
    p = os.path.join(ROOT, rel)
    t = orig[rel].decode("utf-8")
    assert old in t, f"{rel} 找不到锚点: {old[:70]!r}"
    open(p, "wb").write(t.replace(old, new, 1).encode("utf-8"))


CASES = [
    ("_post 不再记录失败（丢掉响应体）",
     lambda: mutate("app/agent/llm.py",
                    "        _record_failure(e.code, body)\n",
                    "")),
    ("_hint_of 直接回上游原文（泄漏域名）",
     lambda: mutate("app/agent/llm.py",
                    '    b = _URL_RE.sub("", body or "")\n',
                    '    return (body or "")[:80]\n    b = _URL_RE.sub("", body or "")\n')),
    ("确定性失败不再立刻熔断",
     lambda: mutate("app/agent/llm.py",
                    '    if reason in ("quota", "auth"):',
                    '    if reason in ("quota",):')),
    ("成功后不清空失败记录",
     lambda: mutate("app/agent/llm.py",
                    "        _LAST_FAIL.clear()          # 链路已恢复 ⇒ 界面上的失败提示该消失\n",
                    "")),
    ("助手降级文案不带原因",
     lambda: mutate("app/chat/assistant.py",
                    'return {"reply": _UNAVAILABLE + _why_line(),',
                    'return {"reply": _UNAVAILABLE,')),
    ("状态端点去掉 last_failure",
     lambda: mutate("app/api/main.py",
                    '        "last_failure": _llm.last_failure(),',
                    '        "last_failure": {},')),
    ("前端徽标去掉失败分支",
     lambda: mutate("app/web/index.html",
                    "    const failing=d.state==='online'&&!!lf.code;",
                    "    const failing=false;")),
]

print("=" * 72)
hit = 0
try:
    for name, fn in CASES:
        fn()
        code, last = run()
        caught = code != 0
        hit += caught
        print(f"{'✅ 捕获' if caught else '❌ 假绿'}  {name}")
        print(f"          {last}")
        for p, data in orig.items():
            open(os.path.join(ROOT, p), "wb").write(data)
finally:
    for p, data in orig.items():
        open(os.path.join(ROOT, p), "wb").write(data)

print("=" * 72)
bad = [p for p in FILES
       if hashlib.sha256(open(os.path.join(ROOT, p), "rb").read()).hexdigest() != orig_sha[p]]
print(f"变异捕获: {hit}/{len(CASES)}")
print("还原校验:", "全部逐字节一致" if not bad else f"❌ 未还原: {bad}")
sys.exit(0 if hit == len(CASES) and not bad else 1)
