"""
智能烹饪调度 Agent - FastAPI 接口
端点:
  POST /parse        菜谱结构化解析 (L1)
  POST /schedule     多菜并行排程 (L2/L3, 走 Agent 闭环)
  POST /reschedule   动态重排 (L4, 最小扰动 + 模型仲裁姿态)
  POST /notify       生成关键节点通知 (模板/LLM 语境化)
  POST /api/plan     官方评分接口 (与 /schedule 同一闭环)
  POST /api/agent_plan  自然语言 -> 选菜 -> 排程
  POST /api/chat     烹饪助手对话
  GET  /api/recipes  菜谱库列表 (官方 100 道 + 用户层)
  POST /api/recipes  新增菜谱
  PUT  /api/recipes/{id}      修改菜谱
  DELETE /api/recipes/{id}    删除菜谱 (软删, 可恢复)
  POST /api/recipes/{id}/restore  恢复菜谱
  GET  /api/devices  设备容量 / POST 修改
  GET  /api/device_spec    设备能力（合法模式 / 温度区间，只读）
  GET  /api/llm_status     模型链路诊断（熔断 / 闸门 / 统计）
  GET  /api/model_settings 当前模型配置 / POST 切换模型（设置页）
  GET  /api/ollama_models  本机已下载的 Ollama 模型列表
  GET  /app          Web 演示界面
  GET  /health       健康检查
"""
from __future__ import annotations
import hmac
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.parser.recipe_parser import parse_recipe
from app.scheduler.engine import schedule, reschedule, STRATEGIES, bucket_of
from app.scheduler import exec_state as _exec_state
from app.scheduler import calib as _calib
from app.scheduler.metrics import evaluate, perturbation, hand_streak
from app.notifier.notifications import generate_notifications, reschedule_notice
from app.chat.assistant import chat as ai_chat
from app.spec.adapter import (build_plan, plan_recipes, last_agent_result,
                              last_plan,
                              resolve_items,
                              normalize_name,
                              get as get_recipe,
                              all_names as all_recipe_names,
                              all_meta as all_recipe_meta)
from app.agent.orchestrator import run as agent_run
from app.agent.priors import get_priors
from app.agent.objective import shape_policy, rule_pick
from app.agent import llm

_LOG = logging.getLogger("ft.api")


def _warmup():
    """
    启动预热 —— 保证"第一个真实请求"就是稳态耗时。

    需要预热的三样东西, **顺序不能换**:
    1. OR-Tools 首次 import 约 2-3s (不预热会直接吃掉 <5s 的性能预算);
    2. LLM 可用性探测: 若模型端不可用(如 429 额度耗尽), 在这里就把熔断器落定,
       免得前几个真实请求各自白等一次超时。**同时它把本地模型焐热**
       (Ollama 首次调用要把权重读进显存, 实测 7B/Q4 ≈10s);
    3. plan_recipes 全链路 (规则表 / 先验缓存 / CP-SAT 建模), 而不是只跑求解器。

    ⚠ **为什么 2 必须排在 3 前面**(2026-10-04 实测踩到):
    `plan_recipes` 自己就会调模型 —— Agent 闭环的策略提议走的是
    `timeout=min(FT_LLM_TIMEOUT, FT_AGENT_BUDGET)` = min(30, 3.6) = **3.6s**。
    本地模型冷启动要 ~10s, 于是预热里的这两次调用**必然超时**并计入熔断器:
    实测启动后 `stats.failures=2 / calls=3` —— 再差一次就到阈值 3,
    服务还没接客就先把自己熔断了(而且那两次超时把启动时间白白拉长 7s)。
    把探测提到前面之后, 模型在"第一次赶时间的调用"之前就已经加载好。
    """
    try:
        from app.scheduler.cpsat import solve  # noqa: F401
    except Exception:
        pass
    # 2. 模型探测: 只在开关打开时做, 失败即熔断, 不影响后续请求。
    #    探测失败就直接把熔断打开 —— 否则"攒失败次数"的代价会落到第一个真实请求上。
    #
    #    三个必须守住的点(都是实测踩出来的):
    #    ① 探测要走**和 Agent 完全相同的调用路径**(chat_json + json_mode),
    #       不能只发一句纯文本 llm.chat —— 两者请求体不同, 可能一个通一个不通。
    #    ② 探测的判据是**"能否在预算内返回", 不是"连不连得上"**。
    #       远端中转站实测可连通但单次响应要 2.3~4.0s, 而远端档 FT_LLM_TIMEOUT 是 3.0s:
    #       于是调用被超时截断, 熔断器还得攒够失败次数才打开,
    #       第一个真实请求白等 1~2 次超时 —— 实测首个 /schedule 6.7~7.8s,
    #       而熔断打开后同一实例只要 0.15s。
    #       Agent 一次排程会发 1~2 次调用, 单次 >1.5s 就不可能落在 <5s 的评分线内,
    #       所以远端档用 1.5s 的紧预算探测: 答得快才算"在线可用"。
    #    ③ **预算默认值按 provider 分流**(见 llm.WARMUP_BUDGET):
    #       本地 Ollama 首次调用要把权重读进显存(实测 7B/Q4 ≈10s), 用 1.5s 探测
    #       必然失败 ⇒ 每次启动都自我熔断, 本地模型等于永远用不上。
    #       它给的是 60s —— 探测同时起到"焐热模型"的作用, 这笔冷启动成本
    #       花在启动阶段, 比让第一个用户请求去承担划算得多。
    #       实际预算取 min(FT_LLM_TIMEOUT, FT_WARM_LLM_BUDGET): 探测预算不该超过
    #       单次调用超时, 否则"探测通过"就是假话(真调用一样会超时)。
    #    cache_key 留 None: 探测结果不落盘, 免得污染语义先验缓存。
    if os.environ.get("FT_WARM_LLM", "on").lower() not in ("off", "0", "false", "no"):
        try:
            # 预算可配(FT_WARM_LLM_BUDGET): 远端部署 RTT 更高,
            # 用 1.5s 会把"其实能用、只是慢一点"的链路误判成不可用并直接熔断。
            probe_budget = min(llm.TIMEOUT, llm.WARMUP_BUDGET)
            if llm.chat_json("你是探针, 只返回 JSON 数组, 不要解释。", "ping",
                             temperature=0.0, timeout=probe_budget) is None:
                #    ④ **超时该不该熔断，取决于预算有多紧**（见 llm.probe_miss_is_fatal）：
                #       宽预算档（本地 60s）答不出来 = 模型/服务真有问题 ⇒ 熔断；
                #       紧预算档（远端 1.5s）超时只说明"比在线闭环慢" ⇒ 只落闸门。
                #       远端档若照旧 force_open，一个 2.6s 的中转站会在**每次启动**
                #       把 AI 对话一起关掉 120s —— 而那是用户第一次点它的时刻。
                if llm.probe_miss_is_fatal():
                    llm.force_open("warmup")
                    _LOG.warning("启动探测: 模型不可用或超出预算(%.1fs, provider=%s, 端点=%s), "
                                 "已预先熔断(%ss 内不再尝试), 在线排程全程走规则兜底",
                                 probe_budget, llm.PROVIDER, llm.BASE_URL, llm.CB_COOLDOWN)
                else:
                    llm.mark_online_too_slow(
                        f"启动探测未在 {probe_budget:.1f}s 预算内返回"
                        f"（provider={llm.PROVIDER}，模型={llm.MODEL}）")
                    _LOG.warning("启动探测: 模型比 %.1fs 的预算慢(provider=%s, 端点=%s); "
                                 "只关闭在线闭环（排程走规则，不再白等），"
                                 "AI 对话 / 通知仍走模型", probe_budget, llm.PROVIDER, llm.BASE_URL)
            else:
                _LOG.info("启动探测: 模型可用(%.1fs 预算内, provider=%s, 模型=%s)",
                          probe_budget, llm.PROVIDER, llm.MODEL)
        except Exception:
            llm.force_open("warmup")
    # 3. plan_recipes 全链路预热。放在探测之后: 这里会真的走一次 Agent 闭环,
    #    而闭环的模型预算只有 3.6s —— 冷启动的模型一定赶不上(见上面的说明)。
    try:
        names = all_recipe_names()[:2]
        _warm = []
        for n in names:
            meta = get_recipe(n)
            _warm.append(parse_recipe(n, meta["ingredients"], meta["steps"]))
        plan_recipes(_warm)
    except Exception:
        pass


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _warmup()
    yield


app = FastAPI(title="智能烹饪调度Agent", version="2.0", lifespan=_lifespan)


# ---------- 可选安全层（默认全部关闭 ⇒ 行为与"只有本机演示"时完全一致） ----------
# 为什么做成 opt-in 而不是默认开启：本项目默认是本机/内网演示，强制鉴权会让
# start.bat、ui_probe、ui_layout_probe 全部立刻连不上；而一旦挂到公网，这几项
# 又必须能靠一行环境变量打开。所以默认值刻意选"不改变现有行为"。
#
#   FT_AUTH_TOKEN=<随机串>      启用令牌鉴权（X-Auth-Token 或 Authorization: Bearer）
#   FT_RATE_LIMIT=120/60        每个 IP 每 60 秒最多 120 次（留空 = 不限流）
#   FT_TRUST_PROXY=1            信任 X-Forwarded-For（**仅当**前面确实有反向代理时开）
#   FT_CORS_ORIGINS=...         显式配置才挂 CORS 中间件；不配 = 同源（浏览器默认策略）
_AUTH_TOKEN = (os.environ.get("FT_AUTH_TOKEN") or "").strip()
# 豁免路径：静态页与健康检查。浏览器得先拿到页面才谈得上填令牌；
# 探针 / 负载均衡也要能探活。除此之外**一律**需要令牌。
_OPEN_PATHS = frozenset({"/", "/app", "/health", "/ready", "/favicon.ico"})
_TRUST_PROXY = os.environ.get("FT_TRUST_PROXY", "").lower() in ("1", "on", "true", "yes")


def _rate_limit_cfg():
    """解析 FT_RATE_LIMIT=<次数>/<秒>。返回 (次数, 秒)；0 表示不限流。"""
    raw = (os.environ.get("FT_RATE_LIMIT") or "").strip()
    if not raw:
        return 0, 0
    try:
        n, _, w = raw.partition("/")
        n, w = int(n), int(w or "60")
        return (max(1, n), max(1, w)) if n > 0 else (0, 0)
    except ValueError:
        _LOG.warning("FT_RATE_LIMIT=%r 解析失败, 已按不限流处理", raw)
        return 0, 0


_RL_MAX, _RL_WINDOW = _rate_limit_cfg()
_RL_HITS: dict = {}
_RL_MAX_KEYS = 4096          # 防止伪造 IP 撑爆内存


def _client_ip(request) -> str:
    if _TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "-"


@app.middleware("http")
async def _security_guard(request, call_next):
    """鉴权 + 限流。

    注册在 CORS 之前 ⇒ Starlette 的 `add_middleware` 是"后加的更外层"，
    于是 CORS 成为最外层，401/429 的响应也会带上跨域头。
    """
    path = request.url.path
    if path in _OPEN_PATHS or path.startswith("/app/"):
        return await call_next(request)

    if _AUTH_TOKEN:
        got = (request.headers.get("x-auth-token") or "").strip()
        if not got:
            auth = request.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
        # 定长比较：避免用响应时间逐字符试出令牌
        if not hmac.compare_digest(got, _AUTH_TOKEN):
            return JSONResponse(
                {"error": "unauthorized",
                 "hint": "缺少或错误的 X-Auth-Token / Authorization: Bearer"},
                status_code=401)

    if _RL_MAX:
        ip = _client_ip(request)
        now = time.monotonic()
        q = _RL_HITS.get(ip)
        if q is None:
            if len(_RL_HITS) >= _RL_MAX_KEYS:
                for k in [k for k, v in _RL_HITS.items()
                          if not v or now - v[-1] > _RL_WINDOW]:
                    _RL_HITS.pop(k, None)
                if len(_RL_HITS) >= _RL_MAX_KEYS:
                    _RL_HITS.clear()
            q = _RL_HITS[ip] = deque()
        while q and now - q[0] > _RL_WINDOW:
            q.popleft()
        if len(q) >= _RL_MAX:
            retry = max(1, int(_RL_WINDOW - (now - q[0])) + 1)
            return JSONResponse({"error": "rate_limited", "retry_after": retry},
                                status_code=429, headers={"Retry-After": str(retry)})
        q.append(now)

    return await call_next(request)


# CORS: 前端与后端**同源**部署（都挂在同一个域名下），正常使用不到跨域。
# 之前默认放开成 "*" 只是为了本地调试方便，但那等于把接口对任意网页开放
# （配合无鉴权 = 任何网站都能代表访客调你的后端）。现在改为**默认不挂中间件**
# —— 同源就是浏览器默认策略，什么都不用配；确实需要跨域再显式写 FT_CORS_ORIGINS。
_cors = (os.environ.get("FT_CORS_ORIGINS") or "").strip()
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _cors.split(",") if o.strip()],
        allow_methods=["*"], allow_headers=["*"],
    )


class RecipeIn(BaseModel):
    name: str
    ingredients: str = ""
    steps: str = ""
    label: str = ""


class ScheduleIn(BaseModel):
    recipes: list[RecipeIn]
    text: str = ""          # 用户自然语言偏好, 如"请客要一起上桌"
    notify: bool = True     # 是否生成通知


class RescheduleIn(BaseModel):
    recipes: list[RecipeIn]
    completed: list[str] = []   # 已完成 task_id 列表, e.g. ["清蒸鳜鱼#1","清蒸鳜鱼#2"]
    extra: RecipeIn | None = None  # 临时新增的菜 (如排骨汤)
    failed_device: str = ""       # 动态事件: 某设备故障, e.g. "蒸箱"
    text: str = ""                # 用户对本次重排的偏好/说明


def _env_on(name: str, default: str = "on") -> bool:
    return os.environ.get(name, default).lower() not in ("off", "0", "false", "no")


def _notify(recipes, plan):
    """通知生成: 默认尝试 LLM 语境化, 失败/超时自动回落模板。"""
    priors = get_priors(recipes)
    return generate_notifications(plan, priors=priors,
                                  use_llm=_env_on("FT_NOTIFY_LLM"),
                                  timeout=llm.TIMEOUT)


def _official(name: str, r: RecipeIn) -> RecipeIn:
    """菜名能匹配官方菜库时, 补全食材与步骤。

    查库前先做一次**保守归一**（空白/零宽/全角），与 `/api/plan` 同一口径 ——
    否则「 清蒸鲈鱼 」这种带空格的写法在 `/parse`、`/schedule` 上会静默落空。
    """
    if (not r.ingredients or not r.steps):
        rec = get_recipe(name) or get_recipe(normalize_name(name))
        if rec:
            r.ingredients, r.steps = rec["ingredients"], rec["steps"]
    return r


def _unknown(recipes) -> list[str]:
    """
    菜名既不在官方 100 道菜里、调用方也没给步骤 -> 解析出 0 步, 排程里会整个消失。
    这种情况必须显式报出来, 不能静默丢菜(排程少一道菜而无人知晓是最难排查的故障)。
    """
    return [r.name for r in recipes if not r.steps]


def _prepare(recipes_in: list[RecipeIn]) -> tuple[list, list[str]]:
    """把入参 RecipeIn 规整成可排程的 Recipe 列表。

    做两件事:
    1. 只给菜名的, 从官方菜库补全食材/步骤 (与 /api/plan 一致);
    2. **重名消歧**: 排程内部的任务键是 (菜名, 步骤号), 两份"鸡翅包饭"会塌成
       同一组任务 —— 响应里 recipeCount=2 但实际只排了 1 道菜, 是静默少菜。
       这里把第 n 份重名成 "鸡翅包饭(2)" / "鸡翅包饭(3)", 保证每份都真被排进去。

    返回 (Recipe 列表, 解析后仍为空的菜名列表)。
    """
    out, seen = [], {}
    for r in recipes_in:
        r = _official(r.name, r)
        name = r.name
        if name in seen:
            seen[name] += 1
            name = f"{name}({seen[name]})"
        else:
            seen[name] = 1
        out.append(parse_recipe(name, r.ingredients, r.steps, r.label))
    return out, _unknown(recipes_in)


def _duplicate_names(recipes_in: list[RecipeIn]) -> list[str]:
    """返回出现次数 >1 的菜名 (用于提示调用方"重名会被拆成多份")。"""
    cnt: dict = {}
    for r in recipes_in:
        cnt[r.name] = cnt.get(r.name, 0) + 1
    return [n for n, c in cnt.items() if c > 1]


@app.get("/")
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    """存活探针: 进程在就行, 不依赖任何外部资源。"""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """
    就绪探针: 确认"能真的排出一桌菜", 而不只是进程活着。
    容器启动阶段会做 OR-Tools 预热(数秒), 这期间不该被放进负载均衡接流量。
    """
    try:
        from app.spec.adapter import all_names
        names = all_names()
        return {"ready": True, "recipes": len(names)}
    except Exception as e:
        _LOG.warning("就绪检查失败: %s", e)
        return {"ready": False}


class ChatMsg(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    messages: list[ChatMsg]


@app.post("/api/chat")
def chat_endpoint(body: ChatIn):
    """AI 对话: 透传到模型服务（默认本地 Ollama）, 作为烹饪调度助手。回复里命中菜库的菜名自动提取, 供前端一键入排程。"""
    t0 = time.time()
    try:
        out = ai_chat([m.dict() for m in body.messages])
    except Exception as e:
        return {"reply": f"(AI 暂时没连上: {e})", "model": "", "elapsed_ms": 0}
    # 从回复文本里匹配菜库菜名 (越长越优先)
    names = sorted(all_recipe_names(), key=len, reverse=True)
    picked = [n for n in names if n and n in out.get("reply", "")]
    out["picked"] = picked[:5]
    out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    return out


class PlanItem(BaseModel):
    """官方计分请求项。

    `ingredients` / `steps` 是**可选扩展**（官方规范只要求 `id` + `name`，
    不传时行为与从前完全一致）：
      · 菜名命中官方库 -> 用库里的内容, 传了 steps 则以传入的为准;
      · 菜名**不在**库里 -> 用传入的 steps 在线解析, 支持评委的自有菜谱。

    为什么要加这两个字段：原实现 `if not rec: continue` 会把库外菜名**静默丢掉**，
    响应里 `recipeCount` 变小而调用方无从察觉 —— 违反《接口规范》第 8 条
    （cookingTimeline / recipeDetail 必须覆盖请求中的全部菜谱）。
    """
    id: object
    name: str
    ingredients: str = ""
    steps: str = ""


def _split_plan_payload(payload: Any) -> tuple[list[dict], dict]:
    """把 `/api/plan` 的请求体拆成 (菜谱条目, 设备容量覆盖)。

    支持**两种**形态，二者共用同一条求解闭环：

      ① 官方形态（默认，纯 schema）：裸数组
         `[{"id":1,"name":"清蒸鲈鱼"}, …]`

      ② 扩展形态：带可选 `devices` 的包装对象
         `{"recipes":[…], "devices":{"灶具":1,"蒸箱":0}}`

    为什么要加 ②：赛题原文是"根据用户选定的菜谱组合**及厨房设备条件**"。
    原实现只能靠改 `data/devices_user.json`（进程级全局状态）换厨房 ——
    评委无法在一次请求里指定"单灶眼 / 无蒸箱"这类配置，并发测评时两个请求
    还会互相踩。`devices` 走**请求级** ContextVar 覆盖，全局状态永不被改写。

    `devices` 的语义是**容量覆盖**（不是增量）：`{"蒸箱":0}` = 蒸箱不可用，
    相关任务按 `DEVICE_FALLBACK` 顶替到灶具；`{"灶具":1}` = 只有 1 个灶眼。
    """
    devices: dict = {}
    if isinstance(payload, dict):
        raw_items = payload.get("recipes")
        raw_dev = payload.get("devices") or {}
        if not isinstance(raw_dev, dict):
            raise HTTPException(status_code=400, detail={
                "error": "bad_devices",
                "message": "devices 必须是对象，如 {\"灶具\":1,\"蒸箱\":0}"})
        from app.scheduler.engine import RESOURCE_UNITS as _UNITS
        for k, v in raw_dev.items():
            if k not in _UNITS or k == "人工":
                raise HTTPException(status_code=400, detail={
                    "error": "unknown_device", "device": k,
                    "message": f"不认识设备「{k}」。可覆盖的设备："
                               f"{[d for d in _UNITS if d != '人工']}"})
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise HTTPException(status_code=400, detail={
                    "error": "bad_capacity", "device": k, "value": v,
                    "message": "容量必须是非负整数（0 = 该设备不可用）"})
            devices[k] = v
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raise HTTPException(status_code=400, detail={
            "error": "bad_payload",
            "message": "请求体必须是菜谱数组，或 {\"recipes\":[…],\"devices\":{…}} 对象"})

    if not isinstance(raw_items, list):
        raise HTTPException(status_code=400, detail={
            "error": "bad_recipes", "message": "recipes 必须是数组"})
    items: list[dict] = []
    for it in raw_items:
        if isinstance(it, dict):
            items.append(PlanItem(**it).model_dump())
        else:
            raise HTTPException(status_code=400, detail={
                "error": "bad_recipe_item", "message": "每个菜谱项必须是对象，至少含 id 与 name"})
    return items, devices


@app.post("/api/plan")
def plan_endpoint(payload: Any = Body(...)):
    """官方评分接口: 输入 `[{id,name}]`（或带 devices 的包装对象）, 返回完整多菜排程 JSON。

    **不静默丢菜**：任一菜名既不在官方库、又没随请求带 `steps` ⇒ HTTP 422 +
    明确的 error（列出是哪些菜名、以及怎么修），而不是返回一份残缺的计划。
    422 的判据放在排程之前，不做无谓的求解。
    """
    items, devices = _split_plan_payload(payload)
    req = [{"id": it["id"], "name": it["name"],
            "ingredients": it.get("ingredients", ""),
            "steps": it.get("steps", "")} for it in items]
    _ok, unresolved = resolve_items(req)
    if unresolved:
        _LOG.warning("菜名无法解析(既不在菜库、也未提供 steps): %s", unresolved)
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unresolved_recipes",
                "message": ("这些菜名既不在官方菜谱库中，请求里也没有提供 ingredients/steps，"
                            "无法生成计划。按《接口规范》第 8 条，本接口不会返回缺菜的计划。"),
                "unresolved": unresolved,
                "how_to_fix": ("在请求项里补上 steps（可选 ingredients）即可排程自有菜谱，例如 "
                               '{"id":1,"name":"佛跳墙","steps":"第1步：…；第2步：…"}；'
                               "或改用 GET /api/recipe_names 里的官方菜名。"),
                "recipe_names_endpoint": "/api/recipe_names",
            },
        )
    cnt: dict = {}
    for r in req:
        cnt[r["name"]] = cnt.get(r["name"], 0) + 1
    dupes = [n for n, c in cnt.items() if c > 1]
    if dupes:
        # 官方 schema 里没有"份数"字段, 且响应不能加自定义字段, 所以只能留痕。
        # 排程内部任务键是 (菜名, 步骤号), 重名会塌成一道菜 —— 这里显式告警,
        # 避免"输入 5 道菜、输出 4 道菜"这种静默丢菜被当成正常结果。
        _LOG.warning("请求含重名菜, 会被合并为同一道菜排程: %s", dupes)
    if devices:
        # 请求级设备容量覆盖（赛题原文的"及厨房设备条件"）。
        # ContextVar 只在本次请求可见 —— 全局 RESOURCE_UNITS 永不被改写，
        # 并发请求互不干扰，同一份 {菜名, devices} 可复现同一份计划。
        from app.scheduler.engine import resource_override
        with resource_override(devices):
            return build_plan(req)
    return build_plan(req)


class AgentPlanIn(BaseModel):
    text: str


@app.post("/api/agent_plan")
def agent_plan(body: AgentPlanIn):
    """
    自然语言排程: 从菜库识别菜品, 直接生成排程。

    v2.2 修复
    ---------
    1. 原实现自己发 urllib 请求、timeout=30s, 绕过了统一 LLM 客户端 ——
       既不共享超时/熔断/缓存, 30s 更是远超 <5s 的性能预算。
    2. 原实现把异常原文(如 "HTTP Error 429: Too Many Requests")直接返回给客户端,
       内部细节外泄, 且模型不可用时整个端点直接废掉(返回空)。
       现在模型失败一律回落规则选菜, 客户端只看到规范化的 source/notice 字段。
    """
    t0 = time.time()
    names = all_recipe_names()
    metas = all_recipe_meta()
    picked, source, notice = [], "", ""

    if _env_on("FT_AGENT_LLM") and not llm.breaker_open():
        sys_p = ("你是烹饪调度助手。从菜库里挑出用户想吃的菜(2-5道), 只返回JSON数组, "
                 f"不要解释。菜库: {names}。例如用户说'两荤一素快点', 返回[\"菜名1\",\"菜名2\"]。")
        out = llm.chat_json(sys_p, body.text, temperature=0.3,
                            timeout=llm.TIMEOUT)
        cand = out
        if isinstance(cand, dict):
            cand = next(iter(cand.values()), None)
        if isinstance(cand, list):
            picked = [n for n in cand if isinstance(n, str) and n in names][:5]
        if picked:
            source = "llm"
        else:
            _LOG.warning("自然语言选菜未拿到模型结果, 回落规则选菜")
    if not picked:
        picked = rule_pick(body.text, metas)
        source = "rule"
        notice = ("模型不可用, 已按你说的关键词从菜库规则选菜"
                  if not llm.enabled() or llm.breaker_open()
                  else "按菜品分类规则选菜")
    if not picked:
        # 极端兜底: 菜库为空也能返回结构化响应, 绝不抛异常
        return {"picked": [], "plan": None, "source": "none",
                "notice": "未能识别菜品", "elapsed_ms": round((time.time() - t0) * 1000, 1)}
    plan = build_plan([{"id": i, "name": n} for i, n in enumerate(picked, 1)],
                      user_text=body.text)
    out = {"picked": picked, "plan": plan, "source": source,
           "notice": notice, "elapsed_ms": round((time.time() - t0) * 1000, 1)}
    # 前端画甘特图 / 指标卡用的是内部 SchedulePlan 形状 (与 /schedule 一致),
    # 而上面 plan 是纯官方 schema —— 没有 tasks/makespan_min, 前端直接渲染会报错。
    # 官方 schema 原样保留在 plan 里, 另给一份 schedule+kpi 供 UI 使用。
    internal = last_plan()
    if internal is not None:
        out["schedule"] = internal.to_dict()
        out["kpi"] = evaluate(internal).__dict__
    return out


@app.get("/api/recipe_names")
def recipe_names():
    return {"names": all_recipe_names()}


@app.get("/api/recipes_meta")
def recipes_meta():
    return {"items": all_recipe_meta()}


# ---- 自然语言「改计划」（v3.7）----------------------------------------------
# 修订指令的关键词表。**只用于离线兜底**（模型不可用时）：命中即生效，不命中就
# 如实回 `ok=false` —— 绝不猜。猜错的代价是"我说了一句话，计划悄悄变了"，
# 比"没听懂，请换个说法"严重得多。
_REVISE_DROP = ("去掉", "不要", "删掉", "删除", "取消", "别做", "不加", "换掉")
_REVISE_ADD = ("加上", "加个", "加一道", "来一个", "再来", "补一个", "多做")
_REVISE_FIRST = ("提前", "先做", "放到最前", "挪到前面", "排前面", "第一个")
_REVISE_LAST = ("推后", "最后", "放到最后", "挪到后面", "压轴", "往后")
_MAX_DISHES = 5


class ReviseIn(BaseModel):
    text: str
    dishes: list[str] = []


def _revise_by_rule(text: str, names: list[str], picked: list[str]):
    """离线兜底：**按小句**做增删与排序。返回 (order, add, remove, matched)。

    ⚠ 必须按小句（，。；、, 空格切开）判，不能拿整句去撞关键词 ——
      第一版就是整句判的，于是 "去掉鸡翅包饭，把苹果芒果派提前" 里
      「苹果芒果派」被**同一句里的"去掉"**连坐删掉（实测：
      order=["鸡翅包饭"]、remove=[]、还报成 source="llm"）。
      小句切分之后，"去掉" 只作用于它所在的那一句，语义才对得上。
    """
    import re as _re

    add: list[str] = []
    remove: list[str] = []
    first: list[str] = []
    last: list[str] = []
    # 小句之间要**传递增删意图**：中文列举菜名用顿号，"加上 A、B、C" 会被切成
    # 三个小句，只有第一句带"加上"。不传递的话 B、C 永远加不上（实测踩到：
    # `_revise_caps_dish_count` 直接红）。
    # ⚠ 只传递"增/删"这类**集合操作**，不传递"提前/推后" —— 排序意图一旦外溢，
    #   "把 A 提前，B" 会把没点名要动的 B 也挪到最前。
    carry = ""
    for clause in _re.split(r"[，。；;、,.!？?\s]+", text):
        if not clause:
            continue
        cnames = [n for n in sorted(names, key=len, reverse=True) if n and n in clause]
        if not cnames:
            continue
        if any(k in clause for k in _REVISE_DROP):
            carry = "drop"
        elif any(k in clause for k in _REVISE_ADD):
            carry = "add"
        elif any(k in clause for k in _REVISE_FIRST):
            carry = ""
            first += cnames
            continue
        elif any(k in clause for k in _REVISE_LAST):
            carry = ""
            last += cnames
            continue
        if carry == "drop":
            remove += [n for n in cnames if n in picked]
        elif carry == "add":
            add += [n for n in cnames if n not in picked]

    remove = list(dict.fromkeys(remove))
    add = list(dict.fromkeys(add))
    first = list(dict.fromkeys(first))
    last = list(dict.fromkeys(last))
    order = [n for n in picked if n not in remove] + [n for n in add if n not in picked]
    # 排序意图只作用在"被点到名的那道菜"上，不重排没提到的菜。
    if first and first[0] in order:
        n = first[0]
        order = [n] + [x for x in order if x != n]
    elif last and last[0] in order:
        n = last[0]
        order = [x for x in order if x != n] + [n]
    return order, add, remove, bool(add or remove or first or last)


@app.post("/api/revise_plan")
def revise_plan(body: ReviseIn):
    """自然语言**改计划**: "把汤挪到后面" / "去掉红烧肉" / "加个青菜"。

    只把一句话翻成**一份新的菜名列表**（顺序 + 增删），排程仍由 `/schedule` 做 ——
    这样"改计划"永远不会绕过求解器，也就不可能产出违反硬约束的计划。

    三条纪律
    --------
    1. **不许凭空造菜**：模型给的每个名字都必须能在菜库里找到，找不到直接丢弃。
    2. **不许静默丢菜**：模型返回的 `order` 若漏了最终集合里的菜，一律补到末尾 ——
       宁可顺序不合意，也不能让用户发现"我说了句话，一道菜没了"。
    3. **模型不可用时如实说**：走关键词兜底；兜底也认不出来就回 `ok=false` +
       可照做的说明，**绝不**返回一个"看起来成功"的假计划。
    """
    t0 = time.time()
    names = all_recipe_names()
    picked = [n for n in (body.dishes or []) if n in names]
    text = (body.text or "").strip()
    order: list[str] = []
    add: list[str] = []
    remove: list[str] = []
    source = "none"
    reason = ""

    # ⚠ 必须同时判 `llm.enabled()`，且**必须**用 `llm_ok` 这个独立标志决定
    #   source —— 踩过的坑：只看"order 非空"就认定模型成功，而模型返回空时
    #   `order` 会被下面"不许静默丢菜"的补全逻辑填成 `picked` 原样，于是
    #   `FT_LLM=off` 下也报 `source="llm"`，且把"一个菜都没改"当成成功返回。
    #   那是典型的静默无效：用户以为模型听懂了，其实什么都没发生。
    llm_ok = False
    if _env_on("FT_REVISE_LLM") and llm.enabled() and not llm.breaker_open():
        sys_p = (
            "你是烹饪调度助手，负责**修改**用户当前的菜单。只返回 JSON 对象，不要解释。"
            '格式：{"order":["菜名按新的做菜顺序"],"add":["新增菜名"],'
            '"remove":["去掉的菜名"],"reason":"一句话说明"}。'
            f"只能从菜库选菜，菜库：{names}。"
            "规则：order 必须包含最终所有菜名（不重不漏）；add/remove 只能是菜库里的名字；"
            f"最终菜数 1-{_MAX_DISHES} 道。"
        )
        user_p = f"当前菜单（顺序）：{picked or '（空）'}。用户要求：{text}"
        out = llm.chat_json(sys_p, user_p, temperature=0.2, timeout=llm.TIMEOUT)
        if isinstance(out, dict):
            src = out
        elif isinstance(out, list) and out and isinstance(out[0], dict):
            src = out[0]
        else:
            src = {}
        raw_rm = [n for n in (src.get("remove") or []) if isinstance(n, str)]
        raw_add = [n for n in (src.get("add") or []) if isinstance(n, str)]
        raw_order = [n for n in (src.get("order") or []) if isinstance(n, str)]
        if raw_rm or raw_add or raw_order:
            # ① 只保留菜库里真有的名字（防凭空造菜）
            rm = [n for n in raw_rm if n in picked]
            ad = [n for n in raw_add if n in names and n not in picked]
            final_set = [n for n in picked if n not in rm] + ad
            if final_set:
                order = [n for n in raw_order if n in final_set]
                # ② 不许静默丢菜：模型漏掉的补到末尾（去重）
                for n in final_set:
                    if n not in order:
                        order.append(n)
                add, remove, llm_ok = ad, rm, True
            else:
                _LOG.warning("改计划：模型结果落成空菜单，忽略并回落关键词兜底")
        else:
            _LOG.warning("改计划未拿到可用的模型结果，回落关键词兜底")

    if not llm_ok:
        order, add, remove, matched = _revise_by_rule(text, names, picked)
        source = "rule" if matched else "none"
        if not matched:
            order, add, remove = [], [], []
    else:
        source = "llm"

    if not order:
        # 如实说明为什么没改，并给出可照做的说法。
        if not picked and not text:
            reason = "empty"
            notice = "先选几道菜，或直接说要加/去掉什么。"
        elif not llm.enabled() or llm.breaker_open():
            reason = "llm_off"
            notice = ("模型当前不可用，只认关键词说法：如「去掉红烧肉」「加个青菜」"
                      "「把番茄炒蛋提前」。")
        else:
            reason = "unparsed"
            notice = "没听懂这句想改什么，换个说法试试（点名菜名 + 加/去掉/提前/推后）。"
        return {"ok": False, "reason": reason, "source": source, "notice": notice,
                "order": [], "add": [], "remove": [],
                "elapsed_ms": round((time.time() - t0) * 1000, 1)}

    truncated = len(order) > _MAX_DISHES
    order = order[:_MAX_DISHES]
    return {"ok": True, "reason": "", "source": source,
            "order": order, "add": [n for n in add if n in order],
            "remove": remove,
            "truncated": truncated,
            "notice": ("已按你的要求更新菜单" if source == "llm"
                       else "模型不可用，已按关键词更新菜单"),
            "elapsed_ms": round((time.time() - t0) * 1000, 1)}


@app.post("/parse")
def parse(recipes: list[RecipeIn]):
    """L1 结构化解析。返回 `{"parsed": [Recipe.to_dict(), …]}`。

    ⚠ 只给菜名（`ingredients` / `steps` 留空）时**必须回落到官方菜库**取内容。
      原实现直接拿空字符串去解析，于是 `POST /parse [{"name":"清蒸鲈鱼"}]`
      返回的是 `{"steps": []}` —— 一个**空壳**，而接口文档写的是"留空则自动从官方库取"。
      评委按文档调这个端点做 L1 评分，会拿到一份"解析出来什么都没有"的结果，
      看起来像解析器完全不工作。修法与 `/schedule`、`/reschedule` 一致：
      统一走 `_official()`（命中库就补全，命中不了就按传入内容解析，支持自有菜谱）。
    """
    out = []
    for r in recipes:
        r = _official(r.name, r)
        rec = parse_recipe(r.name, r.ingredients, r.steps, r.label)
        out.append(rec.to_dict())
    return {"parsed": out}


@app.post("/schedule")
def do_schedule(body: ScheduleIn):
    """多菜排程 —— 与 /api/plan 走同一条 Agent 闭环。"""
    t0 = time.time()
    recipes, miss = _prepare(body.recipes)
    dupes = _duplicate_names(body.recipes)
    plan = plan_recipes(recipes, user_text=body.text)
    if body.notify:
        plan.notifications = _notify(recipes, plan)
    kpi = evaluate(plan)
    cost = round((time.time() - t0) * 1000, 1)
    from app.scheduler.checker import check_plan
    chk = check_plan(plan)
    res = {"plan": plan.to_dict(), "kpi": kpi.__dict__,
           "elapsed_ms": cost, "check": chk}
    # 诊断层: 关键路径 + 风险提示 + 冲突归因 (建议文本《详细注意事项》§七.1 / §四.3)。
    # 只加在内部端点 /schedule; /api/plan 必须保持纯官方 schema, 不能加字段。
    # 纯只读分析, 不参与任何优化决策 —— 失败也绝不能影响排程返回。
    try:
        from app.scheduler.insight import analyze as _insight
        res["insight"] = _insight(plan, priors=get_priors(recipes))
    except Exception as exc:
        _LOG.debug("排程诊断失败(已忽略): %s", exc)
    # 理论下界与最优性间隙 —— 赛题「总耗时 vs 理论最优」这个观察点要看得见。
    # 用 lower_bound_from_plan(): 从**已产出的计划**反推, 构造上保证 LB <= makespan,
    # 绝不会出现 "makespan < 下界" 这种自相矛盾的汇报。
    # 注意: 只加在内部端点 /schedule; /api/plan 必须保持纯官方 schema, 不能加字段。
    try:
        from app.scheduler import model as _model
        lb = _model.lower_bound_from_plan(plan)
        live = [t for t in plan.tasks if t.start_min >= 0]
        mk = max(float(plan.makespan_min),
                 max((t.end_min for t in live), default=0.0))
        res["bound"] = {
            **{k: round(v, 1) for k, v in lb.items()},
            "makespan": round(mk, 1),
            "optimality_gap_pct": (round((mk - lb["lb"]) / lb["lb"] * 100, 1)
                                   if lb["lb"] > 0 else 0.0),
            "note": ("gap = (makespan - lb)/lb。lb 取「依赖关键路径 / 批次机产能 / "
                     "人工总工时」三者最大，构造上保证 lb <= makespan。其中人工项假设"
                     "单人无空闲连续作业，故 lb 偏松 —— 报告的 gap 是**真实最优间隙的"
                     "保守上界**（即真实间隙不会比它更差）。"),
        }
        # 精确轨的求解元信息: 时间轴规模 / 求解状态 / 连续手动上限到底生效没有。
        # 旧实现里 hand_cap 解不出来是**静默丢弃**的, 调用方拿到一份看起来正常、
        # 其实没受约束的计划。这里把"要了什么、生效没有、为什么没生效"显式回显。
        res["model"] = _model.last_meta()
    except Exception as exc:      # 下界只做汇报, 任何异常都不影响排程返回
        _LOG.debug("下界计算失败(已忽略): %s", exc)
    warnings = [f"菜名未命中官方菜库且未提供步骤，已跳过: {m}" for m in miss]
    if dupes:
        warnings.append(
            "检测到重名菜，已按份数拆开排程（如 " + dupes[0] + " → "
            + dupes[0] + "(2)）: " + "、".join(dupes))
    if warnings:
        res["warnings"] = warnings
    agent_res = last_agent_result()
    if agent_res is not None:
        res["agent"] = {
            "trace": [s.to_dict() for s in agent_res.trace],
            "policy": agent_res.policy.to_dict(),
            "explanation": agent_res.explanation,
            "llm": {
                "calls": agent_res.llm_calls,
                "ms": round(agent_res.llm_ms, 1),
                # 让调用方看得见"模型到底用上没有、为什么没用上":
                # enabled=全局开关, open=熔断中, reason=熔断原因(quota=额度耗尽/限流),
                # online_gate_ok=闭环闸门（模型好着但比闭环预算慢时落下，只关闭环）
                "enabled": llm.enabled(),
                "breaker_open": llm.breaker_open(),
                "breaker_reason": llm.breaker_reason(),
                "breaker_until": round(llm.breaker_until(), 1),
                "online_gate_ok": llm.online_usable(),
                "online_gate_reason": llm.online_gate_reason(),
                "stats": dict(llm.STATS),
            },
        }
    # ---- 落库: 存**完整**记录（含 plan / kpi / check / insight / bound）----
    # 旧版只存 `time/dishes/makespan/gap` 四个摘要字段 —— 即使不重启, 点开历史也
    # 看不到当时的完整计划（甘特图/任务明细/通知/诊断全都回不来）。
    # ⚠ 必须放在**最后**: insight / bound / model / agent 都是后续追加进 `res` 的,
    #   提前落库会把它们漏掉（还原出来的"为什么这样排"面板会是空的）。
    rec = {
        "sid": f"{int(time.time() * 1000)}-{_os.urandom(3).hex()}",
        "ts": int(time.time()),
        "time": time.strftime("%H:%M:%S"),
        "date": time.strftime("%Y-%m-%d"),
        "dishes": [r.name for r in recipes],
        "makespan": plan.makespan_min, "gap": plan.finish_gap_min,
        "tasks": len(plan.tasks),
        "warnings": warnings,
        "elapsed_ms": cost,
        "plan": res.get("plan"), "kpi": res.get("kpi"), "check": res.get("check"),
        "insight": res.get("insight"), "bound": res.get("bound"),
    }
    _SCHEDULE_HISTORY.appendleft(rec)
    _prune_schedules()
    _write_schedules()
    res["sid"] = rec["sid"]
    return res


@app.post("/reschedule")
def do_reschedule(body: RescheduleIn):
    """动态重排 (最小扰动 + 模型仲裁姿态)。"""
    t0 = time.time()
    # 与 /schedule 保持一致: 只给了菜名时, 从官方菜库补全食材与步骤。
    # 原实现直接用空步骤去解析, 只传菜名的调用会排出一张空计划。
    recipes, _miss = _prepare(body.recipes)
    # 设备故障: 临时把该设备容量置 0, 相关任务落到顶替设备/人工。
    # v2.4 修复: 不再改写全局 engine.RESOURCE_UNITS。
    #   旧写法 `eng.RESOURCE_UNITS[d] = 0 ... finally 恢复` 在线程池并发下会互相踩:
    #   请求 A 排程期间蒸箱=0, 请求 B 的 /schedule 读到的也是 0, 于是 B 的计划里
    #   蒸箱凭空消失(或顶替成灶具), 而 B 根本不知道厨房坏了设备。
    #   现在用请求级 ContextVar 覆盖, 全局字典始终不变。
    from app.scheduler.engine import resource_override
    from app.agent.replan import arbitrate, stance_to_strategy, stance_desc
    event = "device_fail" if body.failed_device else "insert"
    override = ({body.failed_device: 0} if body.failed_device else {})
    with resource_override(override):
        base = plan_recipes(recipes, user_text=body.text)
        fixed = {}
        id_map = {t.task_id: t for t in base.tasks}
        for cid in body.completed:
            if cid in id_map:
                dish, sid = cid.rsplit("#", 1)
                fixed[(dish, int(sid))] = id_map[cid]
        if body.extra:
            ex = _official(body.extra.name, body.extra)
            new_dish = parse_recipe(ex.name, ex.ingredients, ex.steps, ex.label)
        else:
            new_dish = None
        # ---- 模型仲裁: 决定本次重排的姿态 (最小扰动 / 重新对齐 / 保热优先) ----
        priors = get_priors(recipes + ([new_dish] if new_dish else []))
        stance, why, by_llm = arbitrate(
            base, priors=priors, completed_ids=body.completed, event=event,
            fresh_dish=(new_dish.name if new_dish else ""),
            use_llm=_env_on("FT_AGENT_LLM"), timeout=llm.TIMEOUT)
        # ---- 重排求解 ----
        # 与初始排程走同一条 Agent 闭环: 在冻结约束下对多策略择优, 而不是
        # 只用姿态映射到的那一条策略硬算 (实测同 3 道菜: 单策略 gap 64 → 闭环 gap 10)。
        # 姿态决定的策略排在最前面, 保证"最小扰动 / 保热优先"这类意图优先被尊重。
        stance_stg = stance_to_strategy(stance)
        ordered = [stance_stg] + [s for s in STRATEGIES if s != stance_stg]
        all_recipes = recipes + ([new_dish] if new_dish else [])
        budget = float(os.environ.get("FT_REPLAN_BUDGET", "2.5"))
        res = agent_run(all_recipes, user_text=body.text,
                        strategies=ordered, hard_frozen=fixed,
                        time_budget=budget,
                        # 设备故障时禁用 CP-SAT: 它的模型不读资源容量覆盖,
                        # 会照旧把坏掉的设备排进去
                        use_cpsat=not body.failed_device)
        new_plan = res.plan
    new_plan.notifications = _notify(recipes, new_plan)
    kpi = evaluate(new_plan)
    moved = perturbation(base, new_plan)
    cost = round((time.time() - t0) * 1000, 1)
    return {"plan": new_plan.to_dict(), "kpi": kpi.__dict__,
            "locked": len(fixed), "perturbed": moved, "elapsed_ms": cost,
            "event": body.failed_device or "插单",
            "stance": {"name": stance, "desc": stance_desc(stance),
                       "reason": why, "by_llm": by_llm},
            "pareto": _pareto_check(base, new_plan, body.completed,
                                    new_dish.name if new_dish else ""),
            "notice": reschedule_notice(base, new_plan, body.completed)}


def _pareto_check(base, new, completed_ids, fresh_dish: str = "") -> dict:
    """重排的**可验证**不劣化保证。

    为什么需要一个"可验证"的版本
    ----------------------------
    "重排后 Pareto 不劣化"这句话如果不写清楚基准, 就是空话:
      · 新增一道菜**必然**增加总工作量, 所以"总时长不增加"根本不可能成立;
      · 设备故障时旧计划本身已经不可行, 拿它当基准也不成立。
    所以这里把保证拆成三条**都能当场判定真假**的断言:

      ① 已完成步骤零变动  —— 硬保证, 逐任务比对开始时刻
      ② 不劣于串行基线    —— 基线 = 保持原计划不动 + 新增菜整体串行接在最后。
                             这是一个**必然可行**的朴素解, 任何像样的重排都该赢它。
      ③ 三项指标前后对比  —— 出锅差 / 总时长 / 连续手动, 显式列出涨跌, 不粉饰。
    """
    before = {"出锅差": base.finish_gap_min, "总时长": base.makespan_min,
              "连续手动": hand_streak(base)}
    after = {"出锅差": new.finish_gap_min, "总时长": new.makespan_min,
             "连续手动": hand_streak(new)}

    # ① 已完成步骤零变动
    new_by_id = {t.task_id: t for t in new.tasks}
    moved_locked = [cid for cid in (completed_ids or [])
                    if cid in new_by_id and cid in {t.task_id for t in base.tasks}
                    and abs(new_by_id[cid].start_min
                            - next(t.start_min for t in base.tasks
                                   if t.task_id == cid)) > 0.5]
    completed_unchanged = not moved_locked

    # ② 串行基线: 原计划不动 + 新增菜整体串行接在末尾 (必然可行的最坏解)
    nd = [t for t in new.tasks if fresh_dish and t.dish == fresh_dish
          and t.start_min >= 0]
    serial_span = sum(float(t.duration_min) for t in nd)
    t_base = float(base.makespan_min)
    t_serial = t_base + serial_span
    fins = list((base.dish_finish or {}).values()) + ([t_serial] if nd else [])
    gap_serial = (max(fins) - min(fins)) if fins else 0.0
    no_worse_than_serial = (new.makespan_min <= t_serial + 1e-6
                            and (not nd or new.finish_gap_min <= gap_serial + 1e-6))

    degraded = [k for k in before if after[k] > before[k] + 1e-6]
    improved = [k for k in before if after[k] < before[k] - 1e-6]
    return {
        "保证": {
            "已完成步骤零变动": completed_unchanged,
            "不劣于串行基线": bool(no_worse_than_serial),
        },
        "违反明细": moved_locked[:5],
        "串行基线": {"总时长": round(t_serial, 1), "出锅差": round(gap_serial, 1)},
        "重排前": {k: round(v, 1) for k, v in before.items()},
        "重排后": {k: round(v, 1) for k, v in after.items()},
        "改善": improved,
        "劣化": degraded,
        "说明": ("新增菜品必然增加总工作量, 所以「总时长不增」不成立; 这里保证的是"
                 "「不推翻已完成步骤」与「不劣于串行追加基线」两条可判定断言, "
                 "并把三项指标的涨跌如实列出。"),
    }


@app.post("/notify")
def do_notify(body: ScheduleIn):
    recipes, _miss = _prepare(body.recipes)
    plan = plan_recipes(recipes, user_text=body.text)
    if not body.notify:
        # 与 /schedule 保持一致: notify=False 时不要生成通知。
        # (原实现忽略该字段 —— 调用方显式关掉通知仍会拿到一份, 且白花一次
        #  可能的 LLM 语境化调用。)
        return {"notifications": []}
    return {"notifications": _notify(recipes, plan)}


# ---- 菜谱库列表 (供前端选菜) ----
import json as _json
import os as _os

from app.spec import recipe_store as _rs

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

# 大 JSON 懒加载缓存: (路径 -> (mtime, 解析结果))
# 原来 /api/recipes 与 /api/recipe_detail 每次请求都重新读盘 + json.load 全量,
# 实测 3.88 MB / 79~81 ms —— 比一次完整排程(45~75 ms)还贵, 而且是纯浪费。
# 按 mtime 失效, 所以热更新数据文件仍然生效。
_JSON_CACHE: dict = {}


def _load_json_cached(path: str):
    try:
        mtime = _os.path.getmtime(path)
    except OSError:
        return None
    hit = _JSON_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
    except Exception as e:
        _LOG.warning("数据文件读取失败 %s: %s", path, e)
        return None
    _JSON_CACHE[path] = (mtime, data)
    return data


@app.get("/api/recipes")
def list_recipes(q: str = "", limit: int = 200, deleted: str = "0"):
    """菜谱库列表 —— 官方 100 道 + 用户层增删改后的结果。

    deleted 三档: "0"=只看未删除(默认) / "1"=只看已删除 / "all"=全部。

    返回三个计数，各有各的用途，**不能合并**：
      · `total`    库中该档位的总条数（**不受搜索词影响** —— 否则边打字徽标数字乱跳）
      · `matched`  命中搜索词的条数（搜索态下前端显示"匹配 N 道"）
      · `recipes`  本次真正返回的行（受 limit 截断）
    只报 total 会让搜索时显示"共 100 道 · 显示前 3"，读起来像是被截断了。
    """
    recs = _rs.all_records(include_deleted=True)
    mode = (deleted or "0").strip().lower()
    keep = []
    for r in recs:
        is_del = bool(r["deleted"])
        if mode in ("0", "false", "no", "") and is_del:
            continue
        if mode in ("1", "true", "yes") and not is_del:
            continue
        keep.append(r)
    total = len(keep)
    if q:
        keep = [r for r in keep if q in r["name"]]
    matched = len(keep)
    lim = max(1, min(int(limit or 200), 1000))
    out = []
    for r in keep[:lim]:
        d = _rs.display_item(r)
        # `steps` / `ingredients` 保留**原文文本**（老契约）：
        # 调用方（含体检脚本）会把它们直接喂给 /parse，换成结构化数组会静默失效。
        out.append({"id": d["id"], "name": d["name"],
                    "steps": d["raw_steps"], "ingredients": d["raw_ingredients"],
                    "method": d["cooking_method"], "time": d["cook_time"],
                    "source": d["source"], "edited": d["edited"], "deleted": d["deleted"]})
    return {"recipes": out, "total": total, "matched": matched,
            "writable": _allow_recipe_write(), **_rs.stats()}


@app.get("/api/recipe_detail")
def recipe_detail(id: str):
    """按 id 取菜谱详情。

    找不到时返回 **404**，不是空对象 —— 返回 `{}` 会让前端模板把每个字段都拼成
    字符串 "undefined"，屏幕上出现一整片 "undefined · undefined · undefined分"，
    比明确报错更难排查。（菜谱库里有重名，按 id 取不会串，但 id 打错很常见。）

    **已删除的菜谱仍然返回 200**，只是带 `deleted: true` —— 前端要据此显示
    "恢复"按钮。若这里返回 404，"删除后可恢复"就成了一句空话。
    """
    rec = _rs.get_by_id(id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"菜谱不存在: {id}")
    return _rs.display_item(rec)


# ---- 菜谱增删改查 ----
# 官方 CSV 是赛题下发的评测数据，**永不被改写**：新增/修改/删除全部只写
# data/recipes_user.json（见 app/spec/recipe_store.py 的模块说明）。
#
# ⚠ 类名**不能**叫 `RecipeIn` —— 上面已经有一个同名的调度入参模型（带 `label`），
#   重名会把它覆盖掉，于是 `ScheduleIn.recipes: list[RecipeIn]` 引用的还是旧类，
#   而外部 import 到的是新类，pydantic 校验直接报 "not an instance of RecipeIn"。
#   这个错误只在**跨模块 import** 时暴露，本文件内自测完全正常。
class RecipeEditIn(BaseModel):
    name: str
    ingredients: str = ""
    steps: str = ""


def _allow_recipe_write() -> bool:
    """菜谱写入是否放行。

    默认 **开启**（改动只作用于用户层、官方数据不被改写、删除可恢复）。
    ⚠ 公开部署时请设 FT_ALLOW_RECIPE_WRITE=off 收紧 —— 当前没有登录鉴权，
    否则任何能打开页面的人都能改菜谱。
    """
    return os.environ.get("FT_ALLOW_RECIPE_WRITE", "on").lower() not in (
        "off", "0", "false", "no")


def _guard_recipe_write() -> None:
    if not _allow_recipe_write():
        raise HTTPException(status_code=403,
                            detail="菜谱写入已关闭（FT_ALLOW_RECIPE_WRITE=off）")


@app.post("/api/recipes")
def create_recipe(body: RecipeEditIn):
    _guard_recipe_write()
    try:
        rec = _rs.create(body.name, body.ingredients, body.steps)
    except ValueError as e:
        # 校验失败是**用户的输入问题**，必须是 400 而不是 500。
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "recipe": _rs.display_item(rec), **_rs.stats()}


@app.put("/api/recipes/{rid}")
def update_recipe(rid: str, body: RecipeEditIn):
    _guard_recipe_write()
    try:
        rec = _rs.update(rid, body.name, body.ingredients, body.steps)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"菜谱不存在: {rid}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "recipe": _rs.display_item(rec), **_rs.stats()}


@app.delete("/api/recipes/{rid}")
def delete_recipe(rid: str):
    """删除菜谱。

    官方菜 → 软删可恢复（官方 CSV 不动）；自建菜 → 真删。
    返回值里的 `recoverable` 明确告诉前端该显示"恢复"还是"已彻底删除"。
    """
    _guard_recipe_write()
    try:
        res = _rs.delete(rid)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"菜谱不存在: {rid}")
    return {"ok": True, **res}


@app.post("/api/recipes/{rid}/restore")
def restore_recipe(rid: str):
    _guard_recipe_write()
    try:
        return {"ok": True, **_rs.restore(rid)}
    except KeyError:
        # 自建菜是真删，记录已经不存在 —— 恢复一个不存在的东西是 404，
        # 不是"静默成功 0 条"（后者会让用户以为恢复过了）。
        raise HTTPException(status_code=404, detail=f"菜谱不存在: {rid}")


# 设备配置 (可由前端修改)
#
# ⚠ **出厂默认**必须与 `engine.RESOURCE_UNITS` 的初值一致（2 灶眼 / 1 蒸箱 /
#   1 烤箱 / 2 冰箱温区）—— 它就是 `data/device_presets.json` 里的「标准家庭厨房」。
#   ⚠ 冰箱记的是**温区数**（冷藏 + 冷冻），不是"几台冰箱"：两者可同时使用，
#     记 1 会把长时腌制/冷藏串行化，白白抬高总时长。
#   「恢复默认」按钮（`POST /api/devices/reset`）回到的就是这三个字面量；
#   三者一旦不同源，点了恢复默认之后界面、内存、引擎会各说各话。
_DEFAULT_DEVICE_CFG = {"灶眼数": 2, "蒸箱": 1, "烤箱": 1, "冰箱": 2}
_DEVICE_CFG = dict(_DEFAULT_DEVICE_CFG)
# 排程历史: 只保留最近 20 条。原来是普通 list + insert(0, ...), 无上限增长 ——
# 长跑/压测下内存单调上涨, 且是纯诊断数据, 没有任何保留全量的理由。
#
# v3.7 起**落盘**到 `data/schedules_user.json`（本机状态, 与 devices_user.json /
# llm_settings.json 同一套两层纪律, 见 sync_publish.SKIP_NAMES）。
# 落盘前这份历史是**纯内存**的: 重启一次就全没了, 而且每条只存
# `time/dishes/makespan/gap` 四个字段 —— 即使不重启, 点开历史也看不到当时的
# 完整计划（甘特图/任务明细/通知/诊断全都回不来）。用户的原话是
# "计划做出来留不住也拿不走", 这一条就是"留得住"。
#
# ⚠ `FT_SCHEDULES_FILE` 是给**测试与探针**用的隔离开关, 与 `FT_DEVICES_FILE`
#   完全同一套纪律。没有它的话: ① pytest 会带着开发机上的历史启动, 计数类断言
#   静默变味; ② 更糟的是**探针会写进真仓库** —— `ui_probe.mjs` 每次跑都会生成
#   一次排程, 于是 `data/schedules_user.json` 里混进一堆 `2-0-0` 之类的测试数据,
#   用户打开"记录"页看到的全是自己没排过的计划。
_SCHEDULES_PATH = _os.environ.get("FT_SCHEDULES_FILE") or _os.path.join(
    _ROOT, "data", "schedules_user.json")

# 单条记录里 `plan` 是完整计划(几十个任务), 20 条满打满算也就几百 KB —— 但仍然
# 加一道总量保护: 落盘文件超过此值就丢最旧的, 避免某次异常输入把文件撑爆。
_SCHEDULES_MAX_BYTES = 4 * 1024 * 1024


def _schedule_record(d: dict) -> dict:
    """把落盘记录裁成**列表用**的摘要（详情走 `/api/schedules/{sid}`）。

    ⚠ 列表绝不能带 `plan` —— 前端 `loadHist()` 只渲染 4 个字段, 但 20 条完整
      计划一起塞进响应会让"打开记录页"变成一次几百 KB 的传输。
    """
    return {k: d.get(k) for k in ("sid", "ts", "time", "date", "dishes",
                                  "makespan", "gap", "tasks")}


def _write_schedules() -> None:
    """原子写排程历史。半截 JSON 会让下次启动**静默丢弃整份历史**（同 llm_settings）。"""
    try:
        _os.makedirs(_os.path.dirname(_SCHEDULES_PATH), exist_ok=True)
        tmp = _SCHEDULES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(list(_SCHEDULE_HISTORY), f, ensure_ascii=False)
        _os.replace(tmp, _SCHEDULES_PATH)
    except Exception as e:      # 落盘失败不该让"排程"这个动作失败
        _LOG.warning("排程历史落盘失败（本次记录仍在本进程内可见）: %s", e)


def _load_schedules() -> None:
    """启动时读回历史。文件不存在/损坏 ⇒ **当空历史**（不报错、不阻断启动）。

    损坏时把坏文件改名留证, 而不是静默删除 —— "我的记录全没了"这种事必须留下痕迹。
    """
    if not _os.path.exists(_SCHEDULES_PATH):
        return
    try:
        with open(_SCHEDULES_PATH, encoding="utf-8") as f:
            data = _json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"顶层不是 list: {type(data).__name__}")
        keep = [d for d in data if isinstance(d, dict) and d.get("sid")]
        for d in keep[: _SCHEDULE_HISTORY.maxlen or len(keep)]:
            _SCHEDULE_HISTORY.append(d)
        _LOG.info("已读回 %d 条排程历史（%s）", len(_SCHEDULE_HISTORY), _SCHEDULES_PATH)
    except Exception as e:
        bad = _SCHEDULES_PATH + ".bad"
        try:
            _os.replace(_SCHEDULES_PATH, bad)
        except Exception:
            pass
        _LOG.warning("排程历史损坏，已忽略并另存为 %s: %s", bad, e)


_SCHEDULE_HISTORY: deque = deque(maxlen=20)
_load_schedules()


def _prune_schedules() -> None:
    """总量保护: 落盘内容超过上限就丢最旧的。

    `plan` 是完整计划（几十个任务 + 通知），正常 20 条也就几百 KB；但接口没有
    输入长度上限，一次异常输入就能把文件撑到几十 MB —— 那是下次启动读 20 秒的
    代价，而用户只是想看一眼记录。丢最旧的比"整个文件读不动"好。
    """
    try:
        while len(_SCHEDULE_HISTORY) > 1:
            blob = _json.dumps(list(_SCHEDULE_HISTORY), ensure_ascii=False)
            if len(blob.encode("utf-8")) <= _SCHEDULES_MAX_BYTES:
                break
            _SCHEDULE_HISTORY.pop()
    except Exception:
        pass


@app.get("/api/devices")
def get_devices():
    return _DEVICE_CFG


@app.get("/api/device_presets")
def get_presets():
    p = _os.path.join(_ROOT, "data", "device_presets.json")
    data = _load_json_cached(p)
    if data is None:
        # 原实现直接 open() —— 文件缺失/损坏时抛 FileNotFoundError 变成 500。
        # 预设是纯展示数据, 缺了不该让整个页面挂掉, 返回空列表即可。
        _LOG.warning("设备预设文件不可用: %s", p)
        return {"presets": []}
    return {"presets": data}


# 设备容量的合法范围: 0 表示该设备不可用(故障/未购置), 上限给一个物理常识值,
# 防止前端手滑传 9999 把 CP-SAT 的模型规模撑爆。
_DEVICE_LIMITS = {"灶眼数": (0, 8), "蒸箱": (0, 4), "烤箱": (0, 4), "冰箱": (0, 4)}


def _coerce_device_cfg(cfg: dict) -> tuple[dict, list[str]]:
    """把外部传入的设备配置规整成 {名称: 非负整数}。

    返回 (规整后的配置, 错误信息列表)。
    原实现直接 `int(cfg["灶眼数"])` —— 传 "很多" 会抛未捕获的 ValueError,
    客户端拿到的是 500 而不是"参数不对"。这里改成逐项校验, 并且**只接受已知键**,
    避免调用方塞进任意键污染全局 _DEVICE_CFG。
    """
    clean, errors = {}, []
    for k, v in (cfg or {}).items():
        if k not in _DEVICE_LIMITS:
            errors.append(f"未知设备: {k}")
            continue
        lo, hi = _DEVICE_LIMITS[k]
        try:
            n = int(v)
        except (TypeError, ValueError):
            errors.append(f"{k} 必须是整数, 收到 {v!r}")
            continue
        if n < lo or n > hi:
            errors.append(f"{k} 超出范围 [{lo}, {hi}], 收到 {n}")
            continue
        clean[k] = n
    return clean, errors


@app.post("/api/devices")
def set_devices(cfg: dict):
    """更新设备容量。非法输入返回 400 语义的 {"error": ...}, 且**不做任何修改**。

    注意: 只对合法项生效。原实现是 `_DEVICE_CFG.update(cfg)` 先改内存、
    再逐项 int() 转换 —— 一旦中途抛异常, _DEVICE_CFG 已被写脏, 且引擎容量
    只更新了一半, 内存配置与引擎容量不一致。
    """
    clean, errors = _coerce_device_cfg(cfg)
    if errors:
        return {"error": "设备配置无效", "details": errors, "applied": {}}
    _DEVICE_CFG.update(clean)
    _apply_device_capacity(clean)
    # 落盘到**用户层**。只改内存的话，服务一重启就回到 2/1/1 —— 用户选完
    # "专业级厨电套装"，重启后计划按标准厨房排，而界面上看不出任何异常。
    _write_devices_user(clean)
    return dict(_DEVICE_CFG)


# ---- 设备容量的**用户层**持久化 ---------------------------------------------
# 与菜谱库同一套两层纪律：`data/device_presets.json` 是只读基线（预设），
# 用户改动只写 `data/devices_user.json`。后者**不进发布包**（`sync_publish`
# 的 SKIP_NAMES，与 llm_settings.json 同理），所以重新发版不会把接收方的
# 厨房配置盖回去。
#
# ⚠ `FT_DEVICES_FILE` 是给**测试**用的隔离开关。没有它的话，只要开发机上
#   有人从界面改过一次设备（文件就被创建了），此后每次 pytest 都会带着这份
#   配置启动 —— 所有"默认 2 灶眼"的断言静默变味，而报错信息完全指不到这里。
#   tests/conftest.py 在 import 期把它钉到一个不存在的路径。
_DEVICES_PATH = _os.environ.get("FT_DEVICES_FILE") or _os.path.join(
    _ROOT, "data", "devices_user.json")

# 设备名 → 引擎资源键。**唯一映射**：加载路径与保存路径共用，免得两边各写一份。
_DEVICE_KEYMAP = {"灶眼数": "DEVICE_STOVE", "蒸箱": "DEVICE_STEAMER",
                  "烤箱": "DEVICE_OVEN", "冰箱": "DEVICE_FRIDGE"}


def _apply_device_capacity(clean: dict) -> None:
    """把设备数量同步进调度引擎的资源容量（引擎侧读的就是这个全局字典）。"""
    from app.scheduler import engine
    for k, v in clean.items():
        attr = _DEVICE_KEYMAP.get(k)
        if attr:
            engine.RESOURCE_UNITS[getattr(engine, attr)] = v


def _write_devices_user(clean: dict) -> None:
    """原子写用户层设备配置。

    半截 JSON 比没有更糟：下次启动会**静默丢弃整份配置**，用户看到的只是
    "我选的厨房又没了"，没有任何报错可查（llm_settings.json 踩过同一个坑）。
    """
    try:
        _os.makedirs(_os.path.dirname(_DEVICES_PATH), exist_ok=True)
        tmp = _DEVICES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(clean, f, ensure_ascii=False, indent=2)
        _os.replace(tmp, _DEVICES_PATH)
    except Exception as e:      # 落盘失败不该让"保存"这个动作失败
        _LOG.warning("设备配置落盘失败（本次改动仍在本进程内生效）: %s", e)


def _load_devices_user() -> None:
    """启动时把用户层设备配置套用到 `_DEVICE_CFG` 与引擎容量。

    读到的每一项都过 `_coerce_device_cfg`（范围校验 + 只认已知键）：
    手改过的文件可能带 `{"灶眼数": 9999}`，直接灌进 RESOURCE_UNITS 会把
    CP-SAT 的模型规模撑爆（`_DEVICE_LIMITS` 的存在就是为了拦这个）。
    """
    try:
        with open(_DEVICES_PATH, encoding="utf-8") as f:
            raw = _json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        _LOG.warning("设备用户配置读取失败，按默认设备启动: %s", e)
        return
    clean, errors = _coerce_device_cfg(raw if isinstance(raw, dict) else {})
    if errors:
        _LOG.warning("设备用户配置含非法项，已忽略: %s", "；".join(errors))
    if clean:
        _DEVICE_CFG.update(clean)
        _apply_device_capacity(clean)
        _LOG.info("已套用用户层设备配置 %s（来源 %s）", clean, _DEVICES_PATH)


_load_devices_user()


@app.post("/api/devices/reset")
def reset_devices():
    """恢复**出厂默认**设备配置（2 灶眼 / 1 蒸箱 / 1 烤箱 / 1 冰箱）。

    与 `set_devices` 的两点不同，都是刻意的：
    1. **同时删掉用户层文件** —— "恢复默认"的语义是"回到出厂状态"，不是
       "把默认值当成一次用户选择存下来"。留着文件的话，将来改了
       `_DEFAULT_DEVICE_CFG`，这台机器会顽固地停在旧默认值上。
    2. **不做 `_coerce_device_cfg` 校验** —— 默认值是代码常量，不是外部输入。

    ⚠ 删除失败（文件被占用 / 只读）**只告警不抛**：内存与引擎已经回到默认，
    这次操作对用户是成功的；下一次改设备会把它覆盖掉。
    """
    _DEVICE_CFG.clear()
    _DEVICE_CFG.update(_DEFAULT_DEVICE_CFG)
    _apply_device_capacity(_DEFAULT_DEVICE_CFG)
    try:
        _os.remove(_DEVICES_PATH)
    except FileNotFoundError:
        pass                      # 本来就没存过用户层，正是默认状态
    except OSError as e:
        _LOG.warning("恢复默认时未能删除用户层设备配置 %s: %s", _DEVICES_PATH, e)
    return dict(_DEVICE_CFG)


# 设备"能做什么"是**只读**信息（来自官方《设备参数清单参考》，见 device_spec.py），
# 与"有几台"（可配置的容量）是两回事。设置页把两者并排展示：用户改数量时能顺手看到
# 这台设备支持哪些模式、温度区间多少 —— 否则填完数字也不知道这个数字意味着什么。
@app.get("/api/device_spec")
def get_device_spec():
    from app.spec import device_spec as _ds
    from app.scheduler import engine
    pairs = (("灶眼数", engine.DEVICE_STOVE), ("蒸箱", engine.DEVICE_STEAMER),
             ("烤箱", engine.DEVICE_OVEN), ("冰箱", engine.DEVICE_FRIDGE))
    out = {}
    for label, dev in pairs:
        spec = _ds.DEVICE_SPEC.get(dev, {}) or {}
        modes = spec.get("modes", {}) or {}
        # 温度区间取该设备**所有模式**的包络 —— 单看某个模式(如"空气炸"180~230)
        # 会让人以为烤箱最低只能到 180℃。
        los = [v[0] for v in modes.values() if v and v[0] is not None]
        his = [v[1] for v in modes.values() if v and v[1] is not None]
        out[label] = {
            "device": dev,
            "default_mode": spec.get("default_mode", ""),
            "modes": list(modes.keys()),
            "temp_min": min(los) if los else None,
            "temp_max": max(his) if his else None,
            # 数量上限**必须由后端给**：前端曾把 `max="8"` 写死，而 _DEVICE_LIMITS 里
            # 蒸箱/烤箱/冰箱是 4 —— 用户在界面上填 8 会被后端判非法。同一件事有
            # 两份真源，改一处必然漏另一处。
            "qty_min": _DEVICE_LIMITS.get(label, (0, 8))[0],
            "qty_max": _DEVICE_LIMITS.get(label, (0, 8))[1],
        }
    return {"devices": out}


@app.get("/api/schedules")
def list_schedules():
    """历史列表 —— **只回摘要**, 不回完整计划。

    ⚠ 这是刻意的: 20 条完整计划（每条含几十个任务 + 通知 + 诊断）一起塞进响应,
      会让"打开记录页"变成一次几百 KB 的传输, 而列表上只显示 4 个字段。
      要看完整计划走 `GET /api/schedules/{sid}`。
    """
    # deque 不支持切片, 且本身已限长 20, 直接转 list 即可 (新→旧)
    items = [_schedule_record(d) for d in _SCHEDULE_HISTORY]
    return {"history": items, "count": len(items)}


@app.get("/api/schedules/{sid}")
def get_schedule(sid: str):
    """取一条历史排程的**完整**记录 —— 前端用它还原当时的甘特图 / 任务明细 / 诊断。

    找不到 ⇒ 404（不返回空对象）。返回 `{}` 会让前端把每个字段都拼成 "undefined"，
    屏幕上出现一整片 "undefined · undefined分"，比明确报错更难排查。
    """
    for d in _SCHEDULE_HISTORY:
        if d.get("sid") == sid:
            return d
    raise HTTPException(status_code=404, detail=f"排程记录不存在: {sid}")


@app.delete("/api/schedules")
def clear_schedules():
    """清空历史（含落盘文件）。

    删不掉文件时**如实报错**而不是回 `{"ok": true}` —— 否则用户下次启动发现
    "记录又回来了"，会以为清空按钮是假的。
    """
    n = len(_SCHEDULE_HISTORY)
    _SCHEDULE_HISTORY.clear()
    err = None
    if _os.path.exists(_SCHEDULES_PATH):
        try:
            _os.remove(_SCHEDULES_PATH)
        except Exception as e:
            err = str(e)
            _LOG.warning("排程历史文件删除失败: %s", e)
    return {"ok": err is None, "cleared": n, "error": err}


# ---- 执行态回写 + 偏差重排建议 (v3.9) ----
#
# ★ 补的是 Agent 闭环里唯一缺失的那一跳: 排程历史 (`schedules_user.json`) 存的是
#   **计划**, 不是**执行结果**。系统排完程、发完通知就失明了 —— 它不知道用户第 3 步
#   做了没有、有没有晚。于是「动态事件·重排」只能靠用户在下拉里**手动**选。
#   设计取舍见 `app/scheduler/exec_state.py` 文件头。
#
# ⚠ 四条纪律:
#   1. **只回写、只建议** —— 达到阈值也不擅自改计划, 由前端弹确认层让用户决定
#      (与 v3.8 的确认纪律同源: 自动换掉一份正在执行的方案, 比重排本身更破坏信任)。
#   2. `planned` 取**计划里真实排定的** `duration_min`, 不是重新估计一遍 ——
#      否则对比的是两个估计值, 校准系数永远趋向 1.0, 等于白学。
#   3. 只有"原文没写时长"的步骤才打样本 —— 校准不覆盖显式时长(见 calib 纪律 2)。
#   4. 上报后必须 `calib.invalidate()`: 否则本次进程读的还是旧缓存, 刚攒的样本
#      在重启前永远不生效, 用户会看到"我报了十次, 系数还是 1.0"。

class ExecReportIn(BaseModel):
    sid: str
    task_id: str
    state: str                      # done | delayed | skip
    actual: float | None = None     # 实际耗时(分钟); 留空 = 与计划一致
    dishes: list[str] = []
    makespan: float | None = None


class ExecSidIn(BaseModel):
    sid: str = ""


def _lookup_step(sid: str, task_id: str):
    """反查这一步的 `(计划时长, 经验档位)`。

    返回 `(planned, bucket)`。两者都可能是空 ——
    `planned` 空 = 这份计划不在历史里(如重排产生的、或历史被清了);
    `bucket` 空 = 原文写了时长 / 菜谱查不到, **这一步不参与校准**(纪律 3)。
    """
    planned = None
    for d in _SCHEDULE_HISTORY:
        if d.get("sid") != sid:
            continue
        for t in ((d.get("plan") or {}).get("tasks") or []):
            if t.get("task_id") == task_id:
                planned = t.get("duration_min")
                break
        break

    bucket = ""
    dish, sep, sno = task_id.rpartition("#")
    if not sep:
        return planned, bucket          # 预热/烟机联动类合成任务, 没有菜名
    try:
        rec = get_recipe(dish)
        if not rec:
            return planned, bucket
        # ⚠ 用 `rec.get("name") or dish` 而不是 `rec["name"]`: 菜谱记录的键由
        #   recipe_store 决定, 少一个 name 就会 KeyError ⇒ 整段落进 except ⇒
        #   静默返回空 bucket ⇒ 这一步永远攒不到样本, 而界面上什么异常都没有。
        r = parse_recipe(rec.get("name") or dish, rec["ingredients"],
                         rec["steps"], rec.get("label", ""))
        for st in r.steps:
            if str(st.step_id) != str(sno).replace("step", ""):
                continue
            # 原文写了时长 ⇒ 这是事实不是估计, 不校准、不打样本
            bucket = "" if st.duration_min is not None else bucket_of(st)
            break
    except Exception:
        # 反查失败只影响"能不能学到东西", 不该让"记录一步执行"失败
        pass
    return planned, bucket


@app.post("/api/exec/report")
def report_exec(body: ExecReportIn):
    """回写一步执行态。

    返回该次排程的**完整**执行态 + 偏差评估, 前端据此决定要不要弹「建议重排」。
    """
    if body.state not in ("done", "delayed", "skip"):
        raise HTTPException(status_code=400,
                            detail=f"state 必须是 done/delayed/skip: {body.state}")
    if not body.sid or not body.task_id:
        raise HTTPException(status_code=400, detail="sid 与 task_id 不能为空")

    planned, bucket = _lookup_step(body.sid, body.task_id)
    run = _exec_state.report(
        body.sid, body.task_id, body.state,
        planned=planned, actual=body.actual, bucket=bucket,
        dishes=body.dishes or None, makespan=body.makespan)
    _calib.invalidate()                      # 纪律 4
    return {"ok": True, "run": run,
            "deviation": _exec_state.deviation(body.sid),
            "sample": {"planned": planned, "bucket": bucket or None}}


@app.get("/api/exec/{sid}")
def get_exec(sid: str):
    """取一次排程的执行态。没记录 ⇒ 空快照(不是 404) —— 还没开始执行是正常状态。"""
    return {"run": _exec_state.get(sid),
            "deviation": _exec_state.deviation(sid)}


@app.post("/api/exec/suggest")
def suggest_replan(body: ExecSidIn):
    """偏差评估 + 重排建议。**只建议, 不改动任何计划。**

    返回里带上 `completed`（已标记为完成的步骤 id）—— 前端点「重排」时直接把它
    塞进 `/reschedule` 的 `completed` 字段即可, 不用自己再算一遍"哪些已冻结"。
    """
    dv = _exec_state.deviation(body.sid)
    run = _exec_state.get(body.sid)
    dv["completed"] = [k for k, v in (run.get("steps") or {}).items()
                       if v.get("state") == "done"]
    return dv


@app.delete("/api/exec")
def clear_exec(body: ExecSidIn | None = None):
    """清空执行态(不影响排程历史)。`sid` 为空 ⇒ 全清。"""
    n = _exec_state.clear((body.sid if body else "") or "")
    _calib.invalidate()
    return {"ok": True, "cleared": n}


@app.get("/api/calib")
def get_calib():
    """经验回流快照: 每个档位攒了多少样本、算出多少系数、**是否真的生效**。

    ⚠ 关档(`FT_CALIB=off`, 缺省)时 `enabled=false` 且每个 bucket 的 `active`
      都是 false —— 界面必须如实显示"未启用", 不能让用户以为已经学会了。
    """
    return _calib.snapshot()


# ---- 模型链路状态 ----
# 为什么需要这个端点:
#   排程链路对模型可用性**零依赖**(不可用时静默走规则兜底), 这本身是优点,
#   但副作用是"部署到服务器之后 AI 变笨了"完全无从判断 —— 用户只能看到
#   AI 点菜退化成规则选菜, 既不知道是没配密钥、还是被容器默认关掉、还是
#   额度耗尽触发了熔断。这里把开关/熔断/统计一次性暴露出来。
#   注意: 只暴露"密钥是否已配置"的布尔值, 绝不回显密钥本身。
_LLM_REASON_TEXT = {
    "quota": "额度耗尽或被限流",
    "auth": "凭据被拒（密钥无效 / 无权限 / 账号未实名）",
    "error": "网络或模型服务异常",
    "warmup": "启动探测未在预算内通过",
}
# 本地 provider 的探测失败几乎只有一个原因：Ollama 没起、或模型还没下载。
# 通用文案（"启动探测未在预算内通过"）对本地部署毫无指导性 —— 用户看着这句话
# 不知道该去查什么，所以这里按 provider 换成可执行的说法。
_LLM_REASON_TEXT_LOCAL = {
    "quota": "额度耗尽或被限流",
    "auth": "凭据被拒（本地档通常不需要密钥，请检查是否误配了 provider）",
    "error": "本地模型服务无响应",
    "warmup": "本地模型服务未就绪（检查 Ollama 是否已启动、模型是否已下载）",
}


@app.get("/api/llm_status")
def llm_status():
    from app.agent import llm as _llm
    enabled = _llm.enabled()
    is_open = _llm.breaker_open()
    reason = _llm.breaker_reason()
    retry = int(max(0.0, _llm.breaker_until() - time.time()))
    # 文案必须按 provider 分流。本地 Ollama **不需要密钥**, 此时再说
    # "密钥未配置"会把用户引向一个根本不存在的配置项 —— 他明明按文档装好了
    # Ollama 且模型能跑, 界面上却写着"未配置"，只会去翻 .env 白折腾。
    key_required = _llm.api_key_required()
    local = _llm.PROVIDER == "ollama"
    if not enabled:
        state = "off"
        if key_required and not _llm.API_KEY:
            label = "未配置模型（离线模式）"
        else:
            label = "模型已关闭"
    elif is_open:
        state = "breaker"
        label = "模型熔断中"
    else:
        state = "online"
        label = "本地模型在线" if local else "模型在线"
    return {
        "state": state, "label": label,
        "enabled": enabled,
        "breaker_open": is_open,
        "breaker_reason": reason,
        "breaker_reason_text": (_LLM_REASON_TEXT_LOCAL if local else _LLM_REASON_TEXT)
                               .get(reason, reason or ""),
        "retry_after_s": retry,
        "provider": _llm.PROVIDER,
        "is_local": local,
        "api_key_required": key_required,
        # 闭环闸门：模型"好着但太慢"时落下。它只关闭环，不关对话/通知，
        # 所以必须和熔断分开报 —— 否则用户看到 state=online 却"排程没走模型"，
        # 无从判断到底哪里被关了。
        "online_gate_ok": _llm.online_usable(),
        "online_gate_reason": _llm.online_gate_reason(),
        "model": _llm.MODEL,
        "api_key_set": bool(_llm.API_KEY),
        # 密钥的**打码预览**（前 6 + 后 4），供界面显示"配的是哪一把"。
        # 只够辨认，不足以拿去调用；而且后端会拒绝把形如掩码的值写回去
        # （见 llm.set_provider），所以把它挂在无鉴权端点上没有泄露风险。
        "api_key_masked": _llm.mask_key(_llm.API_KEY),
        "base_url": _llm.BASE_URL,
        "timeout_s": _llm.TIMEOUT,
        "warmup_budget_s": _llm.WARMUP_BUDGET,
        "stats": dict(_llm.STATS),
        # 最近一次调用失败（code / 归纳后的中文原因 / 是否确定性失败）。
        # ⚠ 为什么必须有这个字段：徽标只看熔断器，会出现「AI在线」但助手回
        #   「已切换到离线模式」的组合 —— 那时用户没有任何线索。上游原文与
        #   链接已在 llm._hint_of 里剥掉，这里不会有第三方域名。
        "last_failure": _llm.last_failure(),
        "switches": {
            "FT_LLM": os.environ.get("FT_LLM", "on"),
            "FT_AGENT_LLM": os.environ.get("FT_AGENT_LLM", "on"),
            "FT_NOTIFY_LLM": os.environ.get("FT_NOTIFY_LLM", "on"),
        },
    }


# ---- 模型设置（设置页「模型接入」）----
# 与 /api/llm_status 的分工：
#   · /api/llm_status  是**诊断**（熔断没熔断、闸门落没落、调了几次）——只读；
#   · 这三个端点是**配置**（本机有哪些模型、当前选了哪个、换一个）——可写。
# 混在一起会出问题：状态端点被前端轮询，里面塞写操作等于把"刷新"变成"改配置"。
class ModelSettingIn(BaseModel):
    # 字段全部可选，是为了**向后兼容**：老的调用方只发 {"model": "..."}，
    # 语义是"只换当前接入方式的模型"。要切换接入方式才需要带上 provider。
    # 不设必填还有个实际好处：Pydantic 的 422 与业务 400 是两套失败契约，
    # 字段可选能少一类"形状不对"的失败路径。
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None


@app.get("/api/ollama_models")
def list_ollama_models():
    """列出本机 Ollama 已下载的模型，供设置页下拉选择。

    Ollama 没启动时 ok=false + 可执行文案 —— 界面照常渲染，而不是让设置页变 500。
    """
    ok, models, err = llm.installed_models()
    return {"ok": ok, "models": models, "error": err, "selected": llm.MODEL,
            "provider": llm.PROVIDER, "is_local": llm.PROVIDER == "ollama"}


@app.get("/api/model_settings")
def get_model_settings():
    """设置页「模型接入」要的全部信息（一次取齐，免得前端打三个请求）。"""
    ok, models, err = llm.installed_models()
    return {
        "provider": llm.PROVIDER,
        "is_local": llm.PROVIDER == "ollama",
        "model": llm.MODEL,
        # 模型名从哪来：env=环境变量钉死 / settings=界面选的 / default=内置默认
        "model_source": llm.MODEL_SOURCE,
        "locked": llm.model_locked(),
        # 接入方式也能被环境变量钉死（容器里钉死 provider 是常见做法）
        "provider_locked": llm.provider_locked(),
        "base_url_locked": llm.base_url_locked(),
        # 下拉选项来自后端：能选哪些接入方式只有一份真源
        "providers": llm.providers_view(),
        # 远程那一栏的现状。**不含密钥原文**，只有"配没配" + 打码预览
        # （见 llm.remote_view）。打码预览是为了让用户能确认"配的是哪一把钥匙"，
        # 换过 key 之后尤其有用 —— 只说"已配置"时人会反复重填、把好配置改坏。
        "remote": llm.remote_view(),
        # 出厂默认配置层（app/agent/llm_defaults.json，随源码/发布包走）。
        # 报出来是为了让"发出去的包开箱能不能用"变成一条可查的事实：
        # 接收方看到的 remote.effective_* 到底来自出厂层还是他自己存的，
        # 靠这个字段 + 用户层是否为空就能判断。
        "factory_defaults": llm.factory_view(),
        # 各接入方式各自记住的模型 —— 设置页切换接入方式时用来回填输入框，
        # 否则用户每切一次都要凭记忆重填一遍模型名。
        "models_by_provider": llm.models_by_provider(),
        "base_url": llm.BASE_URL,
        "api_key_required": llm.api_key_required(),
        "api_key_set": bool(llm.API_KEY),
        "timeout_s": llm.TIMEOUT,
        "reachable": ok,
        "error": err,
        "models": models,
    }


@app.post("/api/model_settings")
def set_model_settings(body: ModelSettingIn):
    """切换模型 / 接入方式。非法输入或被环境变量锁定时返回 400 —— **绝不静默成功**。

    静默成功是最坏的结果：界面显示"已保存"而配置其实没变，用户会带着错误的
    认知去排查别的地方（多半会去怀疑模型服务装坏了）。

    两条分支：
      · 带了 provider  → 切换接入方式（可同时改 base_url / api_key / 模型）；
      · 没带 provider  → 只换当前接入方式的模型（向后兼容老前端）。
    """
    if body.provider is not None:
        res = llm.set_provider(body.provider, base_url=body.base_url,
                               api_key=body.api_key, model=body.model)
    else:
        res = llm.set_model(body.model or "")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "模型设置失败")
    ok, models, err = llm.installed_models()
    names = {m["name"] for m in models}
    is_local = llm.PROVIDER == "ollama"
    return {
        "ok": True,
        "provider": llm.PROVIDER,
        "is_local": is_local,
        "model": res["model"],
        "source": res["source"],
        "base_url": llm.BASE_URL,
        "api_key_set": bool(llm.API_KEY),
        # verified = 该模型是否确实已在本机下载。
        # ⚠ 只对本地档有意义 —— 远程档的模型名在中转站那边，本机 Ollama 当然没有，
        #   照旧报 False 会让界面显示"⚠ 本机没有这个模型，调用会返回 404"，
        #   把一次正常的远程切换吓成失败。ok=False（Ollama 没起）时给 None ——
        #   不能把"问不到"当成"不存在"。
        "verified": (res["model"] in names) if (ok and is_local) else None,
        "installed": sorted(names),
        "error": err,
    }


# ---- 托管前端页面 ----
from fastapi.responses import FileResponse
_WEB = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__)))), "app", "web", "index.html")


@app.get("/app")
def web_ui():
    if _os.path.exists(_WEB):
        return FileResponse(_WEB)
    return {"msg": "前端页面未找到, 请访问 /docs"}
