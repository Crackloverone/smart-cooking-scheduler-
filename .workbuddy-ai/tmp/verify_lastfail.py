"""验证 `last_failure` 能说清"为什么失败"，且不泄漏第三方域名。"""
import os
import sys

sys.path.insert(0, r"D:\Xuzj\ky\Project\ft")
for k in ("FT_LLM_API_KEY", "FT_LLM_BASE_URL", "FT_LLM_MODEL", "FT_LLM_PROVIDER"):
    os.environ.pop(k, None)
os.environ["FT_LLM"] = "on"

from app.agent import llm  # noqa: E402

print("调用前 last_failure =", llm.last_failure())
print("调用前熔断 open    =", llm.breaker_open(), "fails 阈值 =", llm.CB_THRESHOLD)

r = llm.chat([{"role": "user", "content": "回复：正常"}], timeout=20)
print(f"\nllm.chat() 返回 = {r!r}")
lf = llm.last_failure()
print("last_failure      =", lf)
print("熔断 open         =", llm.breaker_open(), "reason =", repr(llm.breaker_reason()))

print("\n== 断言 ==")
ok = True


def chk(name, cond, extra=""):
    global ok
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  | " + str(extra)) if extra else ""))
    ok &= bool(cond)


chk("失败被记录（有 code）", lf.get("code"), lf)
chk("给出了可操作的中文原因", bool(lf.get("hint")), lf.get("hint"))
chk("判为确定性失败", lf.get("deterministic") is True)
chk("确定性失败立刻熔断（不等 3 次）", llm.breaker_open() is True,
    f"reason={llm.breaker_reason()!r}")
# ⚠ 铁律：第三方域名只准出现在 llm_defaults.json
blob = str(lf)
for bad in ("potatoya", "stepfun", "http://", "https://"):
    chk(f"不泄漏 {bad}", bad not in blob, blob[:160])
chk("熔断原因可映射成文案（auth/quota）",
    llm.breaker_reason() in ("auth", "quota", "error", "warmup"), llm.breaker_reason())

print("\n结论:", "全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)
