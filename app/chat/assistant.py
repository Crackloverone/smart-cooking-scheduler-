"""
AI 对话模块 (L4 自然语言交互)
让用户用自然语言选菜/咨询, AI 作为烹饪调度助手。

v2.3 修复 (本文件曾是一个"孤岛"实现)
------------------------------------
原实现自己写 BASE_URL / API_KEY / MODEL, 自己发 urllib 请求, 后果:

  1. **密钥硬编码**: 与 app/agent/llm.py 各存一份明文 key, 会随镜像/提交包外泄。
  2. **绕过开关**: 不读 FT_LLM。实测 FT_LLM=off 时 /api/chat 仍发真实网络请求,
     白等 1.4s 才返回错误 —— 离线评审机上每次对话都浪费 1.4s。
  3. **绕过熔断**: 不走 llm.py 的熔断器, 模型不可用时每次都重新超时。
  4. **超时 60s**: 是全局 TIMEOUT(3s) 的 20 倍, 远超赛题 <5s 的性能预算。
  5. **异常原文外泄**: 直接把 "HTTP Error 502: Bad Gateway" 回给客户端。

现在统一走 app/agent/llm.py —— 共享开关 / 超时 / 熔断 / 统计, 失败一律
规范化文案, 内部细节不外泄。
"""
from __future__ import annotations

from app.agent import llm

SYSTEM_PROMPT = """你是「方太智能烹饪调度Agent」的对话助手。你在用户的厨房里帮忙选菜和安排做饭顺序。

你的能力:
1. 根据用户人数、口味、忌口、现有食材推荐菜品
2. 解释调度结果: 为什么这道菜先做、哪台设备在什么时间用
3. 用户临时加菜/改菜时, 说明会怎么重排
4. 提醒关键节点: 什么时候该翻炒、什么时候该取出

对话规则:
- 简短口语化, 像朋友聊天, 不要长篇大论
- 推荐菜品时给出菜名和一句话理由
- 不确定用户口味时主动问一句 (几人吃/辣不辣/有没有忌口)
- 不要编造设备参数; 提到本系统能力时说"我来帮你排"
"""

_UNAVAILABLE = "（AI 助手当前不可用，已切换为离线模式：我可以直接帮你按菜名排程，或用「自然语言排程」按口味选菜。）"


def _why_line() -> str:
    """把"为什么不可用"附在降级文案后面。

    信息来自 `llm.last_failure()`（HTTP 状态码 + 归纳后的中文原因），
    上游原文与链接已在 `llm._hint_of` 里剥掉 —— 这里不会出现第三方域名。
    拿不到失败记录时（例如只是开关关着）返回空串，保持原文案不变。
    """
    try:
        lf = llm.last_failure()
    except Exception:
        return ""
    code = lf.get("code")
    if not code:
        return ""
    why = f"最近一次调用失败：HTTP {code}"
    if lf.get("hint"):
        why += f"（{lf['hint']}）"
    return f"\n\n> 原因：{why}。可到「设置 → 模型接入」核对接口地址与 API Key。"


def chat(messages: list[dict]) -> dict:
    """messages: [{role:user/assistant, content:str}]. 返回 {reply, model, usage}。

    模型不可用 (未配置凭据 / 开关关闭 / 熔断中 / 调用失败) 时返回规范化的
    离线提示, **不抛异常、不外泄内部错误原文**。
    """
    try:
        from app.spec.adapter import all_names
        menu = "、".join(all_names())
    except Exception:
        menu = ""
    sys_prompt = (SYSTEM_PROMPT
                  + f"\n\n【可用菜库, 推荐时只能从这里面选, 必须用完全一致的菜名】\n{menu}")
    msgs = [{"role": "system", "content": sys_prompt}] + messages

    reply = llm.chat(msgs, temperature=0.7, timeout=llm.TIMEOUT)
    if not reply:
        # 降级文案必须**带上原因**。
        # 原来只有一句"AI 助手当前不可用"，而界面上的徽标又显示「AI在线」
        # （徽标只看熔断器，不看调用结果）—— 两句话互相矛盾，用户完全不知道
        # 该去查什么。实测遇到的真实情形是：上游返回 502「账号需实名认证」，
        # 而用户在界面上只能看到"不可用"，白折腾半天。
        return {"reply": _UNAVAILABLE + _why_line(), "model": "", "usage": {},
                "offline": True,
                "reason": "breaker" if llm.breaker_open()
                          else ("disabled" if not llm.enabled() else "error")}
    return {"reply": reply, "model": llm.MODEL, "usage": {}, "offline": False}
