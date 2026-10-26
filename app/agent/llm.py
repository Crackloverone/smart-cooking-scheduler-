"""
统一 LLM 客户端

接入方式（provider）**运行期可切**：本地 Ollama（免密钥）或任意 OpenAI 兼容端点
（中转站 / 云厂商，需要 API_KEY）。两种 provider 共用同一份代码路径 ——
差别只在"要不要密钥"和"超时/探测预算的默认值"。

配置按**五层**解析，从上到下第一个"给了值"的层胜出：

    真实环境变量 > data/llm_settings.json（用户层） > app/agent/llm_defaults.json（出厂层）
                 > 代码里的 _DEFAULTS > 兜底常量

为什么要单独有一层"出厂层"（`llm_defaults.json`，2026-10-06）
------------------------------------------------------------
产品要求是"**发给别人，别人第一时间就能用**"。原来只有"用户层 + 代码默认"两层，
代码默认刻意只写了 `http://127.0.0.1:11434/v1`（本地 Ollama，免密钥）——
好处是源码里没有凭据，代价是接收方**必须自己配一遍**才能用上模型。

于是新增这一层：它**随源码 / 发布包一起走**（在 `app/` 里，`sync_publish` 会自动带上），
里面放着开箱可用的接入配置。它和用户层是**两件事**：
  · 出厂层 = 我发出去的默认值，重新打包会覆盖；
  · 用户层 = 接收方在设置页改出来的结果，`sync_publish` 刻意排除，
    永远压过出厂层 ⇒ 重新发一版程序**不会**抹掉接收方自己的改动。

设计原则
--------
1. **单一配置源**: PROVIDER / BASE_URL / API_KEY / MODEL 只在此处定义, 其余模块统一 import,
   避免配置散落在 enrich.py / assistant.py / main.py 三处。
2. **结构化输出**: chat_json() 强制 JSON, 并做容错解析 (剥 ```json 包裹 / 截取首尾括号)。
   不支持 response_format 的服务会返回 HTTP 400, 此时自动去掉该字段重试一次。
3. **离线预计算 + 磁盘缓存**: 同一 cache_key 直接命中缓存。
   这是本方案的关键工程约束 —— 在线路径不做"必答题"式的模型调用:
   语义先验一律离线算好落盘, 在线只读缓存。实测价值: 远端中转站"今日 token
   额度用完"返回 429 后, 系统仍以规则+缓存全速运行 (50 组基准质量与耗时不变),
   排程链路对模型可用性零依赖。
4. **全链路降级**: 任何异常/超时都返回 None, 调用方走规则兜底, 绝不阻塞主流程。
   环境变量 FT_LLM=off 可全局关闭 LLM (用于离线评测 / 无网环境)。
5. **分级熔断**: 普通失败(超时/5xx)冷却 CB_COOLDOWN 秒后重试;
   额度类失败(HTTP 429)直接冷却 FT_LLM_CB_QUOTA 秒(默认 1800s)。
   本地 Ollama 永远不会返回 429, 那条分支对它等于死代码 —— 保留是为了
   provider=openai 时行为与改造前逐字一致。
6. **本地模型的冷启动是"一次性成本", 必须由启动探测买单**:
   Ollama 首次调用要把权重读进显存(7B/Q4 实测 ~10s), 之后热态只要 0.5s。
   所以 provider=ollama 时探测预算给到 60s(不是 1.5s), 并且探测走 chat_json ——
   它同时把模型"焐热", 第一个真实请求才不会撞上冷启动。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.request

_LOG = logging.getLogger("ft.llm")

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _parse_dotenv(path: str) -> dict:
    """读一个 .env 文件 → dict；文件不存在返回 {}。"""
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k:
                    out[k] = v
    except FileNotFoundError:
        return {}
    except Exception as e:
        # 原来是静默 pass。但"配置读不进来 ⇒ API_KEY 为空 ⇒ enabled() 恒 False"
        # 正是「远程访问时 AI 完全不工作」最经典的成因，静默会让人查半天。
        _LOG.warning("读取 %s 失败(%s)，该文件里的配置已全部忽略", path, e)
    return out


def _load_dotenv(path: str | None = None) -> None:
    """把项目根目录的 .env / .env.local 读进 os.environ (已存在的环境变量优先, 不覆盖)。

    为什么不直接用 python-dotenv: 它不在 requirements.txt 里 —— 依赖必须与
    声明一致, 否则 Docker 构建出来的镜像会 ImportError。这里手写十几行即可。
    为什么需要它: 密钥原来硬编码在源码里 (llm.py 与 chat/assistant.py 各一份),
    会随 Docker 镜像、提交 zip、答辩 PPT 一起发出去。外置到 .env 后源码里没有
    任何凭据, .env 与 .env.local 都被 .dockerignore 排除。

    两个文件的分工:
      · `.env`       非敏感配置, 可以随项目一起走;
      · `.env.local` 凭据, 单独排除 / 单独轮换。
    合并时**后者覆盖前者** —— 否则"把密钥挪到 .env.local"反而会读不到。
    真实环境变量永远优先（`k not in os.environ`），所以容器里注入的凭据
    不会被仓库里的文件盖掉。
    """
    if path:
        files = [path]
    else:
        files = [os.path.join(_ROOT, ".env"), os.path.join(_ROOT, ".env.local")]
    merged: dict = {}
    for p in files:
        merged.update(_parse_dotenv(p))
    applied = [k for k in merged if k not in os.environ]
    for k in applied:
        os.environ[k] = merged[k]
    if applied:
        # 只记键名, 绝不记值
        _LOG.debug("从 .env/.env.local 载入 %d 项配置: %s", len(applied), ", ".join(sorted(applied)))


_load_dotenv()


def _cfg(name: str, default: str = "") -> str:
    """读环境变量；空串按"未设置"处理，回落到默认值。

    ⚠ 必须显式处理空串：`.env` / `.env.local` 里写成 `FT_LLM_BASE_URL=`（留空表示
    "用默认值"）会让 `os.environ.get(k, default)` 返回**空串**而不是 default ——
    于是默认端点被一个空字符串顶掉，请求发到 `/chat/completions` 这种相对路径上。
    改造前的默认值是硬编码在 get() 里的，正好被 `.env.local` 的空值躲过去了；
    现在默认值按 provider 分流，这个坑就会真的踩到。
    """
    v = (os.environ.get(name) or "").strip()
    return v or default


# ---- 运行时设置文件（设置页可改；env 永远优先）----
# ⚠ `FT_LLM_SETTINGS_FILE` 是给**测试与探针**用的隔离开关，与 `FT_DEVICES_FILE`
#   同一套纪律（见 app/api/main.py）。没有它的话：
#   · pytest 会带着开发机上那份真配置启动 —— "默认接入方式"类断言静默变味；
#   · 更糟的是**探针会改到真配置**：`scripts/ui_click_audit.mjs` 遍历控件时会切
#     「接入方式」下拉、按回车触发 saveModel()，于是 `provider` 从 openai 变成
#     ollama、`model` 被换成本机下拉里的那个 —— 而密钥/地址是"留空=保持原值"，
#     所以**看起来什么都没坏**，用户下次开页面才发现模型被换了。
#     （2026-10-08 实测踩到：跑完探针 data/llm_settings.json 被改写。）
#   ⚠ 路径由 env 决定 ⇒ 必须在**读取时**算，不能 import 时就写死常量……
#     但下面这几个函数都是模块级缓存的，所以这里仍然是 import 期求值 —— 与
#     FT_DEVICES_FILE 一样，调用方必须在 import 之前设好（tests/conftest.py 就是
#     这么做的）。
_SETTINGS_PATH = (os.environ.get("FT_LLM_SETTINGS_FILE")
                  or os.path.join(_ROOT, "data", "llm_settings.json"))

# ---- 出厂默认配置文件（随源码 / 发布包一起走；**用户层永远压过它**）----
# 放在 app/agent/ 下（不是 data/）是有意的：`scripts/sync_publish.py` 会全量同步
# `app/`，但把 `data/llm_settings.json` 排除掉 —— 出厂层要跟着包走，用户层不能。
_DEFAULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_defaults.json")


def _read_settings() -> dict:
    """读运行时设置文件。缺失/损坏一律当空 —— 它是可选层，不该让服务起不来。"""
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _LOG.warning("读取模型设置失败(%s)，已忽略 %s", e, _SETTINGS_PATH)
        return {}


def _read_defaults() -> dict:
    """读**出厂默认配置**（`app/agent/llm_defaults.json`）。

    缺失/损坏一律当空并回落代码默认 —— 与 `_read_settings()` 同款纪律。
    这条纪律在这里尤其重要：接收方可能手工删掉这个文件（不想用随包附带的凭据），
    此时必须安静地跑本地 Ollama 档，而不是让服务起不来。
    """
    try:
        with open(_DEFAULTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _LOG.warning("读取出厂默认配置失败(%s)，已忽略 %s（按代码内置默认档运行）",
                     e, _DEFAULTS_PATH)
        return {}


def _write_settings(data: dict) -> None:
    """原子写设置文件：先写临时文件再 os.replace。

    直接 `open(path,"w")` 有真实风险：写到一半被 kill（或磁盘满）会留下截断的
    json，下次启动读不动 —— 用户会觉得"我设的模型自己变回去了"。
    """
    os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
    tmp = _SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _SETTINGS_PATH)


# 模型名的合法字符集。挡住的是"粘贴时带上了空格/换行/整段说明文字"这类输入 ——
# 它们本身不报错，只会让请求带着一个不存在的模型名打到服务上，返回 404 后静默降级，
# 现场只剩"选了模型但 AI 毫无反应"。
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-/]{0,127}$")

# ---- 单一配置源 (全部可由环境变量 / .env / 设置页 覆盖) ----
# 模型服务提供方：
#   "ollama" 本地 Ollama —— **默认**。免密钥，端点默认 http://127.0.0.1:11434/v1。
#   "openai" 任意 OpenAI 兼容端点（中转站 / 云厂商）—— 必须配 FT_LLM_API_KEY。
# 为什么要把这件事显式建模：旧实现拿 `bool(API_KEY)` 当总开关，而 Ollama 本来
# 就没有密钥 —— 不区分 provider 的话，本地模型会被永远判成"未配置"，
# 全链路静默走规则兜底，表现是"接了 Ollama 但 AI 毫无反应"且页面不报任何错。
#
# ⚠ **provider 是"运行期可切换状态"，不是只读常量**（2026-10-03 起）：
# 设置页可在「本地 Ollama / 远程 OpenAI 兼容」之间切换，落盘到
# data/llm_settings.json 的 provider / models / remote 字段。
# 环境变量仍然**最高优先** —— 容器/CI 里注入的 FT_LLM_PROVIDER 必须能压过用户在
# 页面上点的选择，否则"部署时钉死接入方式"会被 data/ 下一个 json 悄悄推翻。
_PROVIDERS = ("ollama", "openai")
# 设置页下拉的文案。放后端是为了让"能选哪些接入方式"只有**一份真源** ——
# 前端只负责渲染，不再自己写一份选项列表（那种重复早晚会有一边漏改）。
_PROVIDER_LABELS = {
    "ollama": "本地 Ollama（免密钥）",
    "openai": "远程 OpenAI 兼容端点（需密钥）",
}


def providers_view() -> list[dict]:
    """设置页「接入方式」下拉的选项（顺序即展示顺序）。"""
    return [{"id": p, "label": _PROVIDER_LABELS[p]} for p in _PROVIDERS]

# 每个 provider 的 (默认端点, 默认模型) —— **最后一层兜底**，不带任何凭据。
#
# ⚠ 这里仍然刻意**不写死任何第三方中转站地址**。原因不是"域名见不得人"，而是
#   "开箱可用的配置"这件事必须**可替换、可轮换、可整份删掉**：把它写在代码里，
#   换站/换钥匙就得改代码、重跑测试；写进 `app/agent/llm_defaults.json`
#   （出厂层）则是改一个数据文件就行，而且接收方可以整份删掉回落到本地档。
#   护栏 `test_third_party_relay_appears_only_in_the_factory_file` 钉住这条分工：
#   第三方域名只允许出现在那**一个**文件里。
_DEFAULTS = {
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5:7b"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}

# 出厂层快照（import 时读一次；`_apply` 与 `_resolve_*` 的缺省来源）。
_FACTORY = _read_defaults()

# ---- 五层配置：真实环境变量 > 用户层 > 出厂层 > _DEFAULTS ----
_ENV_PROVIDER = _cfg("FT_LLM_PROVIDER", "")
_ENV_BASE_URL = _cfg("FT_LLM_BASE_URL", "")
_ENV_API_KEY = _cfg("FT_LLM_API_KEY", "")
_ENV_MODEL = _cfg("FT_LLM_MODEL", "")
_ENV_TIMEOUT = _cfg("FT_LLM_TIMEOUT", "")
_ENV_WARMUP = _cfg("FT_WARM_LLM_BUDGET", "")


def _norm_provider(v: str) -> str:
    v = (v or "").strip().lower()
    return v if v in _PROVIDERS else "ollama"


def _remote_cfg(saved: dict) -> dict:
    """设置文件里的远程配置块（`remote`）。坏形状一律当空。"""
    r = saved.get("remote")
    return r if isinstance(r, dict) else {}


def _models_map(saved: dict) -> dict:
    """设置文件里的「各 provider 各自记住的模型」映射。"""
    m = saved.get("models")
    return m if isinstance(m, dict) else {}


def _layer(factory: dict | None) -> dict:
    """`factory=None` ⇒ 用 import 时读到的出厂层。

    为什么缺省不是"空"而是"真出厂层"：这两个方向都会静默出错，但**错的后果不对称**。
      · 缺省=空 ⇒ 以后谁新加一个调用点忘了传 factory，出厂配置就整层失效，
        接收方拿到的是本地 Ollama 默认档 —— 正是"发出去别人用不了"这个待修的问题；
      · 缺省=真出厂层 ⇒ 最坏情况只是测试想验"代码兜底档"时得显式传 `{}`（会立刻红）。
    """
    return _FACTORY if factory is None else factory


def _resolve_provider(env_provider: str, saved: dict, factory: dict | None = None) -> str:
    if env_provider:
        p = _norm_provider(env_provider)
        if p != env_provider.strip().lower():
            _LOG.warning("FT_LLM_PROVIDER=%r 不认识, 已按 %s 处理", env_provider, p)
        return p
    return _norm_provider(saved.get("provider") or _layer(factory).get("provider") or "")


def _resolve_base_url(env_url: str, saved: dict, provider: str,
                      factory: dict | None = None) -> str:
    """env > 用户层 remote.base_url > 出厂层 remote.base_url > 代码默认。"""
    if env_url:
        return env_url.rstrip("/")
    if provider == "openai":
        for src in (saved, _layer(factory)):
            u = str(_remote_cfg(src).get("base_url") or "").strip()
            if u:
                return u.rstrip("/")
    return _DEFAULTS[provider][0]


def _resolve_api_key(env_key: str, saved: dict, provider: str,
                     factory: dict | None = None) -> str:
    """env > 用户层 > 出厂层；**只在 openai 档**取，取不到就是空串。

    Ollama 下空串是**正常状态**（它不需要密钥）；provider=openai 下空串才意味着
    "没配凭据 -> 直接离线"。密钥只在 openai 档取 —— 否则切回本地后会把远程密钥
    继续挂在 Authorization 上发给 Ollama（有的实现会当成"错误凭据"直接 401）。

    出厂层里的密钥让"发出去的包开箱可用"；但用户层**整层优先**，
    所以接收方在设置页填了自己的钥匙之后，重新发一版程序也不会把它盖掉。
    """
    if env_key:
        return env_key
    if provider == "openai":
        for src in (saved, _layer(factory)):
            k = str(_remote_cfg(src).get("api_key") or "").strip()
            if k:
                return k
    return ""


def _resolve_model(env_model: str, saved: dict, provider: str,
                   factory: dict | None = None) -> tuple[str, str]:
    """五层优先级 env > 用户层 > 出厂层 > 代码默认。返回 (模型名, 来源)。

    来源取值：`env` / `settings` / `factory` / `default` —— 前端按它显示
    "当前模型来自哪里"（`factory` = 随程序附带的出厂预置）。

    ⚠ **必须优先读 `models[provider]`，而不是全局的 `model`**。设置文件里同时
    存着两个东西：`models` 是"每种接入方式各自记住的模型"，`model` 是"当前生效的
    那一个"。只看 `model` 会出真事故：用户在设置页切到远程（`model=gpt-5.6-sol`），
    之后在 `.env` 里用 `FT_LLM_PROVIDER=ollama` 把接入方式钉回本地 —— 此时
    全局 `model` 还是远程那个名字，于是请求带着 `gpt-5.6-sol` 打到本机 11434，
    Ollama 返回 404 `model not found`，而所有调用点**静默降级**：
    现象是"部署时钉了本地，AI 就再也不动了"，页面上没有任何报错。
    `models` 缺失（老版本写的文件）时才回落到全局 `model`。

    **用户层与出厂层都要过 `_MODEL_RE`**。这两层都是能直接编辑的 json：
    手改出一个带空格或整段中文说明的值时，旧实现会原样采用，之后所有调用都带着
    这个不存在的模型名打到服务上拿 404 —— 而 llm 的失败是**静默降级**，
    现场只剩"选了模型但 AI 毫无反应"，人会去怀疑模型服务装坏了。

    环境变量非法时**不覆盖**（部署者写了就是他的意图，代码不该悄悄改掉），
    只留一条 warning，让"持续 404"有一条能查到的线索。
    """
    if env_model:
        if not _MODEL_RE.match(env_model):
            _LOG.warning("环境变量 FT_LLM_MODEL=%r 不是合法模型名，仍按原样使用"
                         "（若模型调用持续 404，先检查这里）", env_model)
        return env_model, "env"
    for src, tag, path in ((saved, "settings", _SETTINGS_PATH),
                           (_layer(factory), "factory", _DEFAULTS_PATH)):
        m = _models_map(src).get(provider)
        name = str(m or src.get("model") or "").strip()
        if not name:
            continue
        if _MODEL_RE.match(name):
            return name, tag
        _LOG.warning("%s 里的模型名 %r 不合法，已忽略并回落下一层默认模型",
                     path, name)
    return _DEFAULTS[provider][1], "default"


_SETTINGS = _read_settings()
PROVIDER = _resolve_provider(_ENV_PROVIDER, _SETTINGS)
BASE_URL = _resolve_base_url(_ENV_BASE_URL, _SETTINGS, PROVIDER)
API_KEY = _resolve_api_key(_ENV_API_KEY, _SETTINGS, PROVIDER)
MODEL, MODEL_SOURCE = _resolve_model(_ENV_MODEL, _SETTINGS, PROVIDER)


def api_key_required() -> bool:
    """当前 provider 是否必须配密钥。本地 Ollama 免密钥 ⇒ False。

    对外暴露成函数（而不是让调用方自己判断 PROVIDER）：前端徽标 / 状态端点 /
    测试都要用它，散落三处写 `PROVIDER!='ollama'` 早晚会有一处漏改。
    """
    return PROVIDER != "ollama"


def model_locked() -> str:
    """模型名是否被环境变量钉死。返回原因（空串 = 界面可以改）。

    界面要据此把输入框置灰**并说明原因** —— 否则用户改完、保存、发现没生效，
    会以为界面坏了。`.env` 里写了 FT_LLM_MODEL 也算钉死（它同样会进 os.environ）。
    """
    if _ENV_MODEL:
        return ("环境变量 FT_LLM_MODEL 已指定模型，界面改动不会生效"
                "（请改 .env / .env.local 后重启服务）")
    return ""


def provider_locked() -> str:
    """接入方式是否被环境变量钉死。返回原因（空串 = 界面可以改）。"""
    if _ENV_PROVIDER:
        return ("环境变量 FT_LLM_PROVIDER 已指定接入方式，界面改动不会生效"
                "（请改 .env / .env.local 后重启服务）")
    return ""


def base_url_locked() -> str:
    """远程接口地址是否被环境变量钉死。"""
    if _ENV_BASE_URL:
        return ("环境变量 FT_LLM_BASE_URL 已指定接口地址，界面改动不会生效"
                "（请改 .env / .env.local 后重启服务）")
    return ""


# 打码用的省略号。**只用这一个字符**（不用 `*`）：前端要凭它认出
# "这是打码预览、不是真密钥"，见 looks_masked()。
_MASK_ELLIPSIS = "\u2026"


def mask_key(key: str) -> str:
    """把密钥打成"认得出来、但用不了"的预览：前 6 + 省略号 + 后 4。

    为什么不是"干脆不回传"：完全不回传时，界面只能说"已配置" ——
    用户没法确认配的是**哪一把**钥匙（换过 key 之后尤其明显），
    于是会反复重填、把本来好着的配置改坏。前 6 + 后 4 足够辨认，
    中间那一段（密钥的熵几乎全在这里）不出去。

    太短的密钥（< 12）一律打成 `***`：露头露尾就等于把整把钥匙给出去。
    """
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) < 12:
        return "***"
    return k[:6] + _MASK_ELLIPSIS + k[-4:]


def looks_masked(value: str) -> bool:
    """这个值是不是**我们打码后的预览**（而不是一把真密钥）。

    必须挡住它被写回去：界面把打码值显示出来了，用户随手点一次「保存」就会把
    `sk-pro1…ESb0` 当成真密钥落盘 —— 之后所有调用 401/403，而 llm 的失败是
    **静默降级**，现场只剩"AI 突然不工作了"，根本联想不到是保存那一下干的。

    判据取"含省略号或三个以上星号"：这两样都不会出现在任何真实密钥里
    （OpenAI 风格的 key 只含 `[A-Za-z0-9_-]`）。
    """
    v = (value or "").strip()
    if not v:
        return False
    return _MASK_ELLIPSIS in v or "***" in v


def remote_view() -> dict:
    """设置页「远程」那一栏的现状。

    ⚠ **绝不回传密钥本身**，只回传"配没配"（`api_key_set`）与**打码预览**
    （`api_key_masked`）。这几个端点是**无鉴权公开**的 —— 回传密钥等于把用户的
    凭据挂在公网上；打码预览只够辨认是哪一把，不足以拿去调用（后端也会**拒绝**
    把形如掩码的值写回去，见 `set_provider`）。

    `base_url` / `model` 回的是**用户层保存的值**（空 = 从没在界面存过），
    供输入框回填；`effective_*` 是**当前真正生效的值**（可能来自出厂层或环境变量）
    —— 界面要把两者分开显示，否则接收方看到"远程地址：空"会以为没配，
    而实际上随包的出厂配置正在生效。
    """
    saved = _read_settings()
    stored_url = str(_remote_cfg(saved).get("base_url") or "").strip()
    stored_model = str(_models_map(saved).get("openai") or "").strip()
    # 生效值按 openai 档单独解析一次（与当前 provider 无关）：
    # 界面在本地档下也要能显示"远程那一栏配成了什么"。
    eff_url = _resolve_base_url(_ENV_BASE_URL, saved, "openai")
    eff_key = _resolve_api_key(_ENV_API_KEY, saved, "openai")
    eff_model = _resolve_model(_ENV_MODEL, saved, "openai")[0]
    return {
        "base_url": stored_url,
        "api_key_set": bool(eff_key),
        "api_key_masked": mask_key(eff_key),
        "model": stored_model,
        # 当前就是 openai 档时直接用全局量 —— 它已经被 _apply/_recompute 算好了
        "effective_base_url": BASE_URL if PROVIDER == "openai" else eff_url,
        "effective_model": MODEL if PROVIDER == "openai" else eff_model,
    }


def factory_view() -> dict:
    """出厂默认配置层（`app/agent/llm_defaults.json`）的现状。

    供设置页 / 诊断说明"开箱配置来自哪里"：`present=False` 表示接收方把随包的
    那个文件删掉了（不想用附带的凭据），此时一切回落到代码内置默认档（本地 Ollama）。

    **不含密钥原文**，只给打码预览 —— 这个端点是无鉴权公开的。
    读的是 import 时的快照（与真正生效的那份是同一份），不是每次去读盘：
    否则"页面上显示的值"和"实际请求用的值"可能来自不同版本的文件。
    """
    fac = _layer(None)
    key = _resolve_api_key("", {}, "openai", fac)
    return {
        "present": bool(fac),
        "provider": _norm_provider(fac.get("provider") or ""),
        "base_url": _resolve_base_url("", {}, "openai", fac),
        "model": _resolve_model("", {}, "openai", fac)[0],
        "api_key_set": bool(key),
        "api_key_masked": mask_key(key),
    }


def models_by_provider() -> dict:
    """每种接入方式各自记住的模型名（设置页切换接入方式时用来回填）。

    没有它，用户在设置页从「本地」切到「远程」时会看到一个**空的**模型输入框，
    只能凭记忆重填一遍 gpt-5.6-sol；切回去又要再填一遍 qwen2.5:7b。
    这只是"回填建议" —— 真正生效的模型仍由 set_provider 落盘后决定。

    用户层没有记录时**回落到出厂层**：接收方第一次打开设置页、把接入方式切到
    「远程」，输入框里就该是随包配好的那个模型名，而不是空白 —— 空白会让人以为
    "还得自己填"，而填错的名字**不报错**，只会在调用时静默 404。
    """
    m = _models_map(_read_settings())
    fac = _models_map(_layer(None))
    return {p: str(m.get(p) or fac.get(p) or "").strip() for p in _PROVIDERS}


def _apply(data: dict) -> None:
    """把设置文件里的内容同步到模块级配置（不写盘）。"""
    global PROVIDER, BASE_URL, API_KEY, MODEL, MODEL_SOURCE
    PROVIDER = _resolve_provider(_ENV_PROVIDER, data)
    BASE_URL = _resolve_base_url(_ENV_BASE_URL, data, PROVIDER)
    API_KEY = _resolve_api_key(_ENV_API_KEY, data, PROVIDER)
    MODEL, MODEL_SOURCE = _resolve_model(_ENV_MODEL, data, PROVIDER)


def _save_or_error(data: dict) -> dict | None:
    """落盘；失败返回错误 dict，成功返回 None（省掉每处重复的 try/except）。"""
    try:
        _write_settings(data)
        return None
    except Exception as e:
        _LOG.warning("保存模型设置失败: %s", e)
        return {"ok": False, "error": f"写入设置文件失败: {type(e).__name__}"}


def set_model(name: str) -> dict:
    """切换**当前 provider 的**模型并落盘。返回 {"ok": bool, ...}。

    切换后**必须重置熔断与在线闸门**：这两个状态记的都是"上一个模型"的结论 ——
    旧模型慢/坏不代表新模型慢/坏。不重置的话，用户刚换到一个快模型，闭环却仍被
    闸门挡着，表现为"换了模型还是不走模型"，而且怎么看状态都是 online。
    """
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "模型名不能为空"}
    if not _MODEL_RE.match(name):
        return {"ok": False,
                "error": "模型名含非法字符（只允许字母/数字/._:-/，且以字母或数字开头）"}
    lock = model_locked()
    if lock:
        return {"ok": False, "error": lock}
    data = _read_settings()
    data["model"] = name
    models = dict(_models_map(data))
    models[PROVIDER] = name          # 按 provider 各记一份，切回来时自动恢复
    data["models"] = models
    data.setdefault("provider", PROVIDER)
    err = _save_or_error(data)
    if err:
        return err
    old = MODEL
    _apply(data)
    if MODEL != old:
        breaker_reset()
        reset_online_gate()
        _LOG.info("模型已切换: %s -> %s（熔断与在线闸门已重置）", old, MODEL)
    return {"ok": True, "model": MODEL, "source": MODEL_SOURCE, "provider": PROVIDER,
            "base_url": BASE_URL, "api_key_set": bool(API_KEY)}


def set_provider(provider: str, base_url: str | None = None,
                 api_key: str | None = None, model: str | None = None) -> dict:
    """切换接入方式（本地 Ollama / 远程 OpenAI 兼容）并落盘。

    只覆盖**显式给了非空值**的字段：
      · `base_url` / `api_key` 传空串或 None = "保持原样"，**不是"清空"**。
        界面上的密钥框永远是空的（我们从不回传密钥），把空串当"清空"会让用户
        每保存一次就把自己的密钥抹掉一次。
      · 切 provider 时把当前模型记到 `models[旧 provider]`，并恢复
        `models[新 provider]` 的模型；新 provider 没有记录就用内置默认。

    切换后重置熔断与在线闸门，并**重算超时默认值**：远端档默认 20s，沿用旧的
    3s 会把实测 2.3~2.9s 的中转站调用直接截断（贴着线，时好时坏）。
    """
    p = (provider or "").strip().lower()
    if p not in _PROVIDERS:
        return {"ok": False,
                "error": f"不认识的接入方式 {provider!r}（只支持 {' / '.join(_PROVIDERS)}）"}
    if p != PROVIDER:
        lock = provider_locked()
        if lock:
            return {"ok": False, "error": lock}

    data = _read_settings()
    remote = dict(_remote_cfg(data))

    if base_url is not None and str(base_url).strip():
        u = str(base_url).strip().rstrip("/")
        if not re.match(r"^https?://", u):
            return {"ok": False, "error": "接口地址必须以 http:// 或 https:// 开头"}
        lock = base_url_locked()
        if lock:
            return {"ok": False, "error": lock}
        remote["base_url"] = u
    if api_key is not None and str(api_key).strip():
        k = str(api_key).strip()
        # ⚠ 界面会把密钥的**打码预览**（`sk-pro1…ESb0`）显示出来。用户随手点一次
        #   「保存」就会把它当密钥提交上来 —— 落盘之后所有调用 401/403，而 llm 的
        #   失败是**静默降级**，现场只剩"AI 突然不工作了"。所以这里**必须显式 400**，
        #   而且文案要说清"留空 = 保持原样"，否则用户会以为非得重填不可。
        if looks_masked(k):
            return {"ok": False,
                    "error": f"这看起来是打码后的预览值（{k}），不是真实密钥。"
                             "请粘贴完整密钥；**留空表示保持原样**，不用重填。"}
        remote["api_key"] = k
    data["remote"] = remote

    models = dict(_models_map(data))
    if p != PROVIDER and MODEL:
        models[PROVIDER] = MODEL
    if model is not None and str(model).strip():
        m = str(model).strip()
        if not _MODEL_RE.match(m):
            return {"ok": False,
                    "error": "模型名含非法字符（只允许字母/数字/._:-/，且以字母或数字开头）"}
        models[p] = m
    data["models"] = models
    data["provider"] = p
    # 落盘的 `model` 是"当前生效的那一个"。没显式给模型时**不能**直接落到
    # `_DEFAULTS[p][1]` —— 那会把出厂层配好的模型名（如 gpt-5.6-sol）换成代码默认的
    # gpt-4o-mini，而中转站上并没有这个模型 ⇒ 404 ⇒ 静默降级。
    # 直接复用 `_resolve_model`（env 传空、用户层传空）拿到"出厂层 > 代码默认"的结果，
    # 不另造一套口径。
    data["model"] = str(models.get(p) or "").strip() or _resolve_model("", {}, p)[0]

    err = _save_or_error(data)
    if err:
        return err

    changed = (p != PROVIDER)
    old_model = MODEL
    _apply(data)
    _recompute_timeouts()
    if changed:
        breaker_reset()
        reset_online_gate()
        _warn_endpoint_mismatch()
        _LOG.info("接入方式已切换: -> %s（端点=%s, 模型=%s, 超时=%.1fs；熔断与在线闸门已重置）",
                  PROVIDER, BASE_URL, MODEL, TIMEOUT)
    return {"ok": True, "provider": PROVIDER, "model": MODEL, "source": MODEL_SOURCE,
            "base_url": BASE_URL, "api_key_set": bool(API_KEY),
            "changed": changed or (MODEL != old_model)}


def _native_root() -> str:
    """Ollama **原生** API 根地址（`/api/tags` 挂在根上，不在 `/v1` 下面）。

    BASE_URL 是 OpenAI 兼容层（`.../v1`），而列模型要走原生接口。不做这层换算
    就会去请求 `/v1/api/tags`，拿到 404 后报"本地模型服务不可用" ——
    可服务其实好着，用户会去重装 Ollama。
    """
    u = BASE_URL.rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def installed_models(timeout: float = 3.0) -> tuple[bool, list[dict], str]:
    """列出本机 Ollama 已下载的模型。返回 (ok, models, error)。

    失败**不当异常抛**：Ollama 没启动是本地部署最常见的状态，界面要能照常渲染
    并给出可执行的提示（"先 ollama serve / ollama pull xxx"）。
    """
    if PROVIDER != "ollama":
        return False, [], "当前 provider 不是 ollama，无需选择本地模型"
    url = _native_root() + "/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, [], f"无法访问本地模型服务（{url}）：{type(e).__name__}"
    out: list[dict] = []
    for m in (data.get("models") or []):
        nm = str(m.get("name") or m.get("model") or "").strip()
        if not nm:
            continue
        det = m.get("details") or {}
        out.append({
            "name": nm,
            "size": m.get("size"),
            "modified_at": m.get("modified_at") or "",
            "params": det.get("parameter_size") or "",
            "family": det.get("family") or "",
        })
    out.sort(key=lambda x: x["name"])
    return True, out, ""


def _looks_local(url: str) -> bool:
    """端点是否指向本机/本机可达的地址（回环、私网、容器访问宿主机）。"""
    host = url.split("//", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1].split(":")[0].lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"):
        return True
    if host.startswith("192.168.") or host.startswith("10.") or host.startswith("172."):
        return True
    return False


# provider=ollama 却配了远端端点，是"改了模型却没改干净"的典型症状：请求会带着
# 远端模型名(gpt-5.6-sol 之类)打到本地 Ollama 上，返回 404 "model not found"，
# 而所有调用点都静默降级 —— 现象是"换了 Ollama 之后 AI 完全不工作"且没有任何报错。
# 这里只警告不拦截：确实有人把 Ollama 装在局域网另一台机器上。
#
# 写成函数（而不是导入时判一次）是因为 provider / 端点现在**运行期可切**：
# 在设置页切回本地、却留着远程的 FT_LLM_BASE_URL，同样要能留下这条线索。
def _warn_endpoint_mismatch() -> None:
    if PROVIDER == "ollama" and not _looks_local(BASE_URL):
        _LOG.warning("FT_LLM_PROVIDER=ollama 但端点 %s 不像本地地址；"
                     "若你要用远端中转站请在设置页切到「远程」"
                     "（或设 FT_LLM_PROVIDER=openai）", BASE_URL)


_warn_endpoint_mismatch()


# 单次调用超时。默认值按 provider 分流：
#   · ollama 本地：热态 0.5s，但**权重被换出显存后重新加载要 ~10s**。
#     给 3.0s 会把"模型其实好着、只是刚被换出"误判成故障并触发熔断，
#     于是本地模型用着用着就永久降级成规则模式。给 30s 是为了"宁可慢，不能错"。
#   · openai 远端：实测中转站单次往返 **2.3~2.9s**（2026-10-03 三次实测），
#     旧默认 3.0s 是贴着线的 —— 稍慢一点就被截断，表现成"聊天时好时坏"。
#     ⚠ 把默认放大到 20s **不会**拖慢排程：真正在意延迟的调用方
#     （orchestrator / replan）自己传更小的预算，`chat()` 取
#     `min(传入值, TIMEOUT*3)` —— 闭环仍是 3.6s / 2.5s，与这里无关。
#     反过来，TIMEOUT 变大后闭环那几次超时会落进 `deadline_race` 分支
#     （判成"预算不够"而不是"服务坏了"），不会再把熔断器打上去 —— 这正是要的。
_DEFAULT_TIMEOUT = {"ollama": 30.0, "openai": 20.0}

# 启动探测预算(秒)。探测的判据是"能否在预算内返回", 不是"连不连得上" ——
#   · openai: 1.5s —— 单次 >1.5s 本来就落不进 <5s 的评分线，答得快才算"在线可用"。
#     ⚠ 探测超时**不再直接熔断**（见 main._warmup）：慢 ≠ 坏，只关在线闭环，
#     AI 对话照常用模型。否则一个 2.5s 的中转站会在启动时把对话也一起关掉 120s。
#   · ollama: 首次调用要加载权重(~10s), 1.5s 必然失败 ⇒ 每次启动都自熔断,
#     本地模型等于永远用不上。这里给 60s, 探测顺便把模型焐热 —— 这笔钱
#     花在启动阶段, 比花在第一个真实请求上划算得多。
# 两者都可用 FT_WARM_LLM=off 完全跳过探测。
_DEFAULT_WARMUP = {"ollama": 60.0, "openai": 1.5}


def _recompute_timeouts() -> None:
    """provider 变了 ⇒ 超时/探测预算的默认值跟着换（被环境变量钉死的除外）。

    不重算的后果很具体：远端档沿用 3.0s 的旧默认，会把实测 2.3~2.9s 的中转站
    调用随机截断，表现成"聊天时好时坏"，而现场没有任何报错。
    """
    global TIMEOUT, WARMUP_BUDGET
    try:
        TIMEOUT = float(_ENV_TIMEOUT) if _ENV_TIMEOUT else _DEFAULT_TIMEOUT[PROVIDER]
    except ValueError:
        TIMEOUT = _DEFAULT_TIMEOUT[PROVIDER]
    try:
        WARMUP_BUDGET = float(_ENV_WARMUP) if _ENV_WARMUP else _DEFAULT_WARMUP[PROVIDER]
    except ValueError:
        WARMUP_BUDGET = _DEFAULT_WARMUP[PROVIDER]


def probe_miss_is_fatal() -> bool:
    """启动探测超时，是否等价于"模型服务真的坏了"（True ⇒ 熔断；False ⇒ 只落闸门）。

    ⚠ 判据是**探测预算有多紧**，不是 provider 叫什么名字：

    · **宽预算档**（本地 Ollama 给 60s，为了容下 ~10s 的权重冷加载）：
      连 60s 都答不出来，那就是模型没装 / 服务没起 ⇒ 直接熔断是对的，
      免得第一个真实请求再白等一次超时（实测首个 /schedule 会从 0.15s 变成 7.8s）。

    · **紧预算档**（远端 1.5s，为了对齐 <5s 的性能线）：超时只说明"比在线闭环
      那几秒的预算慢"，**不说明服务坏了**。实测中转站单次往返 2.3~2.9s，
      必然超这个预算 —— 若照旧 force_open，每次重启都会把 **AI 对话**
      一起关掉 `FT_LLM_CB_COOLDOWN`（默认 120s），而那恰好是用户第一次点它的时刻。
      所以只落在线闭环闸门：排程立刻改走规则（省掉白等），对话照常用模型。

    阈值取 10s 与 `_DEFAULT_WARMUP` 的钉法一致（见 test_provider_default_budgets_are_pinned：
    `ollama >= 10.0 > openai`）—— 预算被调到 10s 以下就不再算"宽"。
    """
    return WARMUP_BUDGET >= 10.0


_recompute_timeouts()

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "llm_agent_cache.json")

# 统计: 便于在演示界面展示"本次排程调用了几次模型、耗时多少"
#   deadline_miss = 调用方给的预算太紧、模型没跑完的那几次。
#   它**不计入熔断**（见 chat()），单独计数是为了让它可见 ——
#   "排程一直走规则"和"排程一直走规则、而且每次还白等了 3.6s"是两回事。
STATS = {"calls": 0, "hits": 0, "failures": 0, "ms": 0.0, "skipped": 0,
         "rate_limited": 0, "deadline_miss": 0}

# ---- 熔断器 ----
# 评审机/离线环境常连不上中转站。若每次请求都傻等超时, 会把 5s 性能预算白白吃掉。
# 连续失败达阈值后熔断一段时间, 期间所有调用直接返回 None 走规则兜底 (零延迟)。
_CB = {"fails": 0, "open_until": 0.0, "reason": ""}
CB_THRESHOLD = int(os.environ.get("FT_LLM_CB_THRESHOLD", "3"))
CB_COOLDOWN = float(os.environ.get("FT_LLM_CB_COOLDOWN", "120"))
# 额度类失败(429)的冷却时间: 远长于普通失败, 默认半小时
CB_QUOTA_COOLDOWN = float(os.environ.get("FT_LLM_CB_QUOTA", "1800"))


def breaker_open() -> bool:
    """熔断是否打开 (打开=不再尝试调用模型, 直接走规则兜底)。"""
    return _CB["fails"] >= CB_THRESHOLD and time.time() < _CB["open_until"]


def breaker_reason() -> str:
    """熔断原因 ("quota"=额度耗尽/限流, "error"=网络/5xx, "warmup"=启动探测失败)。

    对外暴露成公开函数, 免得调用方(如 api/main.py)去戳私有的 _CB 字典 ——
    那样一旦熔断器内部结构改了, 调用方会静默拿到空串而不是报错。
    未熔断时返回空串。
    """
    return _CB.get("reason", "") if breaker_open() else ""


def breaker_until() -> float:
    """熔断解除的时间戳 (未熔断返回 0.0)。供前端展示"还要等多久重试"。"""
    return _CB["open_until"] if breaker_open() else 0.0


def force_open(reason: str = "warmup") -> None:
    """
    直接打开熔断, 不等失败次数累积。

    用途: 启动预热阶段已经验证过"模型链路不通", 就没必要让真实请求再去试。
    否则首个请求要自己把失败次数攒到阈值 —— 实测这会让第一个 /api/plan
    从 0.07s 变成 3.5s (一次 3s 超时), 在 <5s 的评分线上吃掉 70% 预算。
    """
    _CB["fails"] = max(_CB["fails"], CB_THRESHOLD)
    _CB["open_until"] = max(_CB["open_until"], time.time() + CB_COOLDOWN)
    _CB["reason"] = reason


def breaker_reset() -> None:
    _CB["fails"] = 0
    _CB["open_until"] = 0.0
    _CB["reason"] = ""


def _trip(reason: str = "") -> None:
    """记录一次失败并根据失败类型决定冷却时长。"""
    was_open = breaker_open()
    _CB["fails"] += 1
    if reason in ("quota", "auth"):
        # **确定性**失败（额度耗尽 / 凭据被拒 / 上游要求实名）不必等满阈值 ——
        # 立刻熔断。重试注定一样，而每多试一次用户就白等一个超时；
        # 更要紧的是熔断打开后界面才会显示「模型熔断中」，否则徽标一直挂着
        # 「在线」，用户对着一个绿色的"在线"完全不知道从哪查。
        _CB["fails"] = max(_CB["fails"], CB_THRESHOLD)
    if _CB["fails"] >= CB_THRESHOLD:
        cool = CB_QUOTA_COOLDOWN if reason in ("quota", "auth") else CB_COOLDOWN
        _CB["open_until"] = max(_CB["open_until"], time.time() + cool)
        _CB["reason"] = reason
        if not was_open:
            # 只在"刚刚打开"时记一条 warning。之后每次失败都记会刷屏,
            # 而排查"接了 Ollama 但 AI 没反应"时, 这条是唯一的线索 ——
            # 之前这里是全静默的, 现场只剩"AI 变笨了"这种无法定位的现象。
            _LOG.warning("模型链路熔断 %.0fs (原因=%s, provider=%s, 端点=%s, 模型=%s); "
                         "期间全部调用零延迟走规则兜底",
                         cool, reason or "error", PROVIDER, BASE_URL, MODEL)


def reset_stats() -> None:
    STATS.update({"calls": 0, "hits": 0, "failures": 0, "ms": 0.0, "skipped": 0,
                  "rate_limited": 0, "deadline_miss": 0})


# ---------- 在线闭环闸门 ----------
# 与熔断器是**两件事**，别混：
#   · 熔断器管"服务坏了"（连不上 / 5xx / 额度耗尽）→ 关掉一切，等冷却；
#   · 这个闸门管"服务好着，只是装不进在线闭环那几秒的预算"→ 只关掉**闭环**，
#     AI 对话 / 通知 / 离线预热照常用模型。
#
# 为什么必须有它（2026-10-04 实测）：本地 qwen2.5:7b 跑一次真实的"策略提议"
# 提示词要 **7.6s**，而 /api/plan 的闭环预算只有 3.6s。没有闸门的话：
#   ① 每次排程都先白等 3.6s 才回落规则 —— 实测中位耗时从 184ms 拉到 ~3.7s，
#      直接顶穿 <5s 评分线；
#   ② 那两次超时还会记进熔断器（阈值 3），**两次排程之后熔断打开**，
#      连"AI 对话"一起被关掉 120s —— 用户看到的是"接了本地模型，AI 用两次就变笨了"。
# 闸门一旦落下就只在进程内生效（重启服务即复位）；想无视它、每次都硬试：
# `FT_LLM_ONLINE_GATE=off`。
_ONLINE = {"ok": True, "reason": ""}


def online_usable() -> bool:
    """在线闭环现在值不值得调模型（False = 直接走规则，别再白等一次超时）。"""
    if os.environ.get("FT_LLM_ONLINE_GATE", "on").lower() in ("off", "0", "false", "no"):
        return True                      # 闸门被显式关掉 = 无论如何都试
    return _ONLINE["ok"]


def mark_online_too_slow(reason: str = "") -> None:
    """记下"模型比在线闭环的预算慢"，此后闭环不再调模型。

    只影响闭环：对话、通知、`scripts/warm_cache.py` 都照常用模型。
    """
    if not _ONLINE["ok"]:
        return
    _ONLINE["ok"] = False
    _ONLINE["reason"] = reason
    _LOG.warning("在线闭环已停用模型调用：%s。排程改走规则兜底（毫秒级），"
                 "AI 对话 / 通知 / 离线预热不受影响；"
                 "想无视闸门每次硬试请设 FT_LLM_ONLINE_GATE=off", reason)


def online_gate_reason() -> str:
    """闸门落下的原因（未落下返回空串）。"""
    return "" if _ONLINE["ok"] else _ONLINE["reason"]


def reset_online_gate() -> None:
    _ONLINE.update({"ok": True, "reason": ""})


def enabled() -> bool:
    """全局开关 + 凭据齐备才启用。

    FT_LLM=off 时全局关闭 (离线评测)。
    凭据要求按 provider 分流:
      · ollama 本地: **不需要密钥** —— 它本来就没有。要求密钥等于"本地模型永远
        用不上", 而且失败方式很隐蔽(页面不报错, 只是 AI 静默降级成规则)。
      · openai 兼容端点: 没配 API_KEY 直接关闭 —— 否则会发一次注定 401 的请求,
        攒够失败次数才熔断, 白吃掉性能预算 (实测首个请求多等 1.4~3.5s)。
    """
    if os.environ.get("FT_LLM", "on").lower() in ("off", "0", "false", "no"):
        return False
    if api_key_required() and not API_KEY:
        return False
    return True


# ---------- 磁盘缓存 ----------
_CACHE: dict = {}
_CACHE_DIRTY = False


def _cache_load() -> None:
    global _CACHE
    if _CACHE:
        return
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            _CACHE = json.load(f)
    except Exception:
        _CACHE = {}


def cache_get(key: str):
    _cache_load()
    return _CACHE.get(key)


def cache_put(key: str, value) -> None:
    global _CACHE_DIRTY
    _cache_load()
    _CACHE[key] = value
    _CACHE_DIRTY = True


def cache_flush() -> None:
    global _CACHE_DIRTY
    if not _CACHE_DIRTY:
        return
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        # 原子写（与 _write_settings 同款）。直接 open(path,"w") 会先把文件截断，
        # 中途被 kill / 磁盘满就留下半截 json —— 下次启动 _cache_load() 解析失败，
        # 只能整体丢弃（`except: _CACHE = {}`），111 条语义先验一夜归零，
        # 表现是"每道菜都重新调模型、系统莫名变慢"，而现场没有任何报错。
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _CACHE_PATH)
        _CACHE_DIRTY = False
    except Exception as e:
        # 缓存写不进去不该影响主流程，但要留痕 —— 否则"每次都重新调模型"
        # 会表现成"系统莫名其妙变慢"，而现场没有任何线索。
        _LOG.debug("语义先验缓存写盘失败(%s)，本次不落盘", e)


def _key_of(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


# ---------- 最近一次失败（界面诊断用）----------
#
# 为什么需要它
# ------------
# 徽标上的「AI在线」只看**熔断器**（开关开着 + 凭据非空），它**不反映调用是否
# 成功**。于是会出现这种最难查的组合：徽标写着「AI在线」、助手却回「已切换到
# 离线模式」。用户完全没有线索 —— 而真正的原因（上游返回 502、账号要实名）
# 只存在于服务端日志里，且响应体在 `_post` 里被直接丢掉了。
#
# 所以这里把"最近一次失败"留成可读的状态，供 `/api/llm_status` 报出来。
#
# ⚠ **不许把上游原文或 URL 透出去**：项目有一条铁律 —— 第三方域名只准出现在
#   `app/agent/llm_defaults.json`（护栏 `test_third_party_relay_appears_only_in_
#   the_factory_file`）。上游报错里常常带自己的文档链接，所以下面统一把 URL
#   抹掉，只留一句归纳后的中文建议。
_LAST_FAIL: dict = {}

_URL_RE = re.compile(r"https?://\S+")
_VERIFY_HINT = re.compile(r"real[\s-]?name|verif|实名|认证", re.I)
_QUOTA_HINT = re.compile(r"quota|balance|insufficient|额度|欠费|rate[\s-]?limit", re.I)
_CRED_HINT = re.compile(r"invalid.{0,12}(api)?[\s_-]?key|unauthor|authenticat|"
                        r"forbidden|permission|凭据|密钥", re.I)


def _hint_of(code: int, body: str) -> str:
    """把上游错误归纳成一句**可操作**的中文（URL 已剥离）。"""
    b = _URL_RE.sub("", body or "")
    if _VERIFY_HINT.search(b):
        return "上游要求账号完成实名认证"
    if _QUOTA_HINT.search(b):
        return "额度不足或被限流"
    if _CRED_HINT.search(b):
        return "凭据被拒（密钥无效或无权限）"
    if code in (401, 403):
        return "凭据被拒（密钥无效或无权限）"
    if code == 402:
        return "账户欠费或额度不足"
    if code in (502, 503, 504):
        return "上游网关错误（中转站或模型服务异常）"
    return ""


def _record_failure(code: int, body: str = "") -> None:
    """记下最近一次 HTTP 失败。

    `deterministic` = "重试没有意义"：凭据被拒、欠费、限流、以及上游明确要求
    实名认证这类。它们不该等满 3 次阈值才熔断 —— 每多试一次，用户就白等一次
    超时，而结果注定一样。
    """
    hint = _hint_of(code, body)
    det = bool(_VERIFY_HINT.search(body or "")) or code in (401, 402, 403, 429)
    _LAST_FAIL.clear()
    _LAST_FAIL.update({"code": int(code), "hint": hint,
                       "deterministic": det, "at": time.time()})


def last_failure() -> dict:
    """最近一次失败的摘要；还没失败过就是空 dict。

    调用成功会清空它 —— 所以"有值"就意味着**当前链路有问题**，界面可以据此
    把徽标从「在线」改成警示态，而不是让用户对着一个绿色的"在线"发愁。
    """
    return dict(_LAST_FAIL)


# ---------- 底层调用 ----------
def _post(payload: dict, timeout: float) -> tuple[str, str | None, int]:
    """发一次 /chat/completions。返回 (status, content, http_code)。

    status ∈ {"ok", "timeout", "http", "err"}。
    **"timeout" 与 "err" 必须分开**：前者是"这次没来得及返回"（模型可能只是慢），
    后者是"链路/服务有问题"。熔断器只该因为后者打开 —— 详见 chat()。
    **不抛异常、不改熔断器状态**：熔断的账统一由 chat() 记。
    """
    headers = {"Content-Type": "application/json"}
    # 免密钥的端点（本地 Ollama）**不能**带 `Authorization: Bearer `（空 token）——
    # 有的实现会把它当成"提供了错误凭据"直接 401，而不是"匿名访问"。
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(f"{BASE_URL}/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        return "ok", data["choices"][0]["message"]["content"], 200
    except urllib.error.HTTPError as e:
        # ⚠ 响应体必须读出来：它才是"为什么失败"的唯一来源。
        #   原来这里直接丢掉，于是 502 与 401 在系统里长得一模一样（都只有一个
        #   状态码），用户只能看到"AI 不可用"四个字，无从下手。
        try:
            body = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            body = ""
        _record_failure(e.code, body)
        _LOG.debug("模型返回 HTTP %s (%s, model=%s)", e.code, BASE_URL, MODEL)
        return "http", None, e.code
    except (socket.timeout, TimeoutError) as e:
        _LOG.debug("模型调用读超时(%.1fs): %s (%s, model=%s)", timeout, e, BASE_URL, MODEL)
        return "timeout", None, 0
    except urllib.error.URLError as e:
        # urlopen 会把底层 socket 错误包成 URLError；超时藏在 .reason 里。
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            _LOG.debug("模型调用读超时(%.1fs, URLError): %s", timeout, e.reason)
            return "timeout", None, 0
        # 原来是静默 pass。连不上是最常见的故障，不留痕就只能靠猜。
        _LOG.debug("模型连接失败 %s (%s, model=%s)", e.reason, BASE_URL, MODEL)
        return "err", None, 0
    except Exception as e:
        _LOG.debug("模型调用失败 %s: %s (%s, model=%s)",
                   type(e).__name__, e, BASE_URL, MODEL)
        return "err", None, 0


def chat(messages: list[dict], temperature: float = 0.2,
         timeout: float | None = None, json_mode: bool = False) -> str | None:
    """返回模型文本; 失败返回 None (不抛异常)。
    timeout 缺省用全局 TIMEOUT; 熔断打开时零延迟返回 None。"""
    if not enabled():
        return None
    if breaker_open():
        STATS["skipped"] += 1
        return None
    requested = timeout
    timeout = TIMEOUT if timeout is None else min(timeout, TIMEOUT * 3)
    # 调用方是否在"抢时间"：显式给了一个比模块默认更紧的预算。
    # 抢时间失败 ≠ 服务坏了 —— 见下面 timeout 分支的说明。
    deadline_race = requested is not None and requested < TIMEOUT
    payload = {"model": MODEL, "messages": messages,
               "stream": False, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    t0 = time.time()
    STATS["calls"] += 1
    status, content, code = _post(payload, timeout)
    # 不支持结构化输出的模型/服务会直接 400 —— 这与"模型不可用"完全是两回事，
    # 不该让它把熔断器打上去。去掉 response_format 原样重试一次，让
    # _parse_json 的容错解析兜住（提示词里本来就写了"只输出 JSON"）。
    # 实测：qwen2.5 系列支持该字段；这条是换模型时的安全网。
    if status == "http" and code == 400 and json_mode:
        _LOG.info("模型不接受 response_format(HTTP 400)，去掉该字段重试一次")
        payload.pop("response_format", None)
        STATS["calls"] += 1
        status, content, code = _post(payload, timeout)
    if status == "ok":
        STATS["ms"] += (time.time() - t0) * 1000
        _CB["fails"] = 0            # 成功即复位熔断计数
        _LAST_FAIL.clear()          # 链路已恢复 ⇒ 界面上的失败提示该消失
        return content
    # 调用方自己给了一个比模块默认更紧的预算（如 Agent 闭环的 3.6s），
    # 而模型只是没跑完 —— **这是"预算不够"，不是"服务坏了"**：
    # 把账记到熔断器上，会让"模型慢"升级成"模型死了"，连不赶时间的
    # AI 对话 / 通知一起被关掉。单独计数，保持可见，但不触发熔断。
    if status == "timeout" and deadline_race:
        STATS["deadline_miss"] += 1
        _LOG.debug("模型未在调用方给的 %.1fs 预算内返回（模块默认 %.1fs），"
                   "按'预算不足'处理，不计入熔断", timeout, TIMEOUT)
        return None
    STATS["failures"] += 1
    # 429 = 额度/限流: 短期重试没有意义, 冷却拉长到 CB_QUOTA_COOLDOWN。
    # 本地 Ollama 不会返回 429, 这条分支只在 provider=openai 时生效。
    if status == "http" and code == 429:
        STATS["rate_limited"] += 1
    # 确定性失败立刻熔断（不等 3 次阈值）：凭据被拒 / 欠费 / 限流 /
    # 上游明确要求实名认证。这类失败重试注定一样，而每多试一次用户就白等
    # 一个超时 —— 实测上游 502 每次约 0.9s，攒够 3 次要白等近 3 秒，
    # 期间徽标还一直显示「在线」，用户完全无从判断。
    if _LAST_FAIL.get("deterministic"):
        _trip("quota" if code == 429 else "auth")
    else:
        _trip("error")
    return None


_JSON_BLOCK = re.compile(r"\{.*\}|\[.*\]", re.S)


def _parse_json(text: str):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    try:
        return json.loads(text)
    except Exception:
        m = _JSON_BLOCK.search(text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def chat_json(system: str, user: str, cache_key=None, temperature: float = 0.1,
              timeout: float = 20.0):
    """
    结构化调用。cache_key 非空时读写磁盘缓存 (命中则 0 延迟、0 调用)。
    返回 dict / list / None。
    """
    key = _key_of({"model": MODEL, "system": system, "user": user}) if cache_key is None else cache_key
    if cache_key is not None:
        hit = cache_get(key)
        if hit is not None:
            STATS["hits"] += 1
            return hit
    text = chat([{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                temperature=temperature, timeout=timeout, json_mode=True)
    out = _parse_json(text)
    if out is not None and cache_key is not None:
        cache_put(key, out)
        cache_flush()
    return out
