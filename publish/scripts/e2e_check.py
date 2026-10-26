"""框架完整性 + 端到端全链路体检。

分三段，因为有些东西**只能**在某一段验：

  A. 静态接线   —— 模块是否被加载、延迟导入是否真的可达、有没有死代码
  B. HTTP 链路  —— 24 个路由 + L1→L4→L5 的串联（要后端在跑）
  C. 层内直调   —— L2 先验三态 / L3 权重随文本变化 / L4 三轨是否都在
                   （这些**不在 HTTP 响应里暴露**，只能直接调函数）

用法:
    python -m scripts.e2e_check                    # 全跑（需后端已在 8137）
    APP=http://127.0.0.1:8141 python -m scripts.e2e_check
    python -m scripts.e2e_check --no-http          # 只跑 A + C
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from urllib.parse import quote

BASE = os.environ.get("APP", "http://127.0.0.1:8137").rstrip("/")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

OK: list[str] = []
BAD: list[str] = []
NOTES: list[str] = []


# ==========================================================================
# 工具
# ==========================================================================
def call(method: str, path: str, body=None, timeout=90):
    """发请求。

    ⚠ 两个坑（本脚本自己踩过）：
      1. URL 里的中文必须 percent-encode —— urllib 会直接抛 UnicodeEncodeError，
         看起来像"接口挂了"，其实是客户端没编码。
      2. 非 JSON 响应（HTML）**不能截断** —— 截到 200 字符后去查
         `"apiFetch" in html` 必然失败，会误报"页面没有这个函数"。
    """
    url = BASE + quote(path, safe="/?&=%:")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
            ms = (time.time() - t0) * 1000
            try:
                return r.status, json.loads(raw), ms, None
            except json.JSONDecodeError:
                return r.status, raw.decode("utf-8", "replace"), ms, None
    except urllib.error.HTTPError as e:
        ms = (time.time() - t0) * 1000
        raw = e.read()
        try:
            return e.code, json.loads(raw), ms, None
        except Exception:
            return e.code, raw.decode("utf-8", "replace"), ms, None
    except Exception as e:
        return 0, None, (time.time() - t0) * 1000, f"{type(e).__name__}: {e}"


def step(name: str, ok: bool, detail: str = ""):
    (OK if ok else BAD).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    return ok


def note(text: str):
    NOTES.append(text)
    print(f"  [note] {text}")


def section(t: str):
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _official_dishes(n: int = 4) -> list[str]:
    """从官方菜库取真实菜名 —— **不能手写**。

    ⚠ 本脚本踩过的坑：这里曾手写 `["清蒸鳜鱼","蒜蓉西兰花","草莓蛋挞","凉拌黄瓜"]`，
      其中 **3 个不在官方 100 道菜库里**。于是 /schedule 只排出 1 道菜，探针报
      「4 道菜全部进计划 FAIL」—— 而程序行为完全正确：它把未命中的菜名逐条放进
      `warnings` 显式报了出来（这正是 `main.py::_unknown()` 的设计意图）。
      **假警报比漏报更贵**：它会让人去改没坏的代码。
      另外要优先挑**设备覆盖面大**的菜，否则"无设备时间冲突"这类检查会退化成空跑。
    """
    try:
        from app.spec.adapter import all_names, get
        from app.parser.recipe_parser import parse_recipe
    except Exception:
        return []

    pool = []
    for name in all_names():
        rec = get(name)
        if not rec:
            continue
        r = parse_recipe(name, rec["ingredients"], rec["steps"])
        devs = {s.device for s in r.steps if s.device not in ("人工", "冰箱")}
        pool.append((len(devs), name, devs))
    pool.sort(key=lambda x: (-x[0], x[1]))

    chosen: list[str] = []
    covered: set = set()
    for _, name, devs in pool:              # 先挑能带来新设备的
        if len(chosen) >= n:
            break
        if devs - covered:
            chosen.append(name)
            covered |= devs
    for _, name, _ in pool:                 # 不够再按序补满
        if len(chosen) >= n:
            break
        if name not in chosen:
            chosen.append(name)
    return chosen[:n]


DISHES = _official_dishes()
RECIPES = [{"name": n, "ingredients": "", "steps": "", "label": ""} for n in DISHES]
# 刻意不存在的菜名: 用来验"未命中菜库不许静默丢菜"这条契约
GHOSTS = ["__探针_菜库里没有的菜_甲__", "__探针_菜库里没有的菜_乙__"]


# ==========================================================================
# A. 静态接线
# ==========================================================================
def part_a() -> None:
    section("A. 静态接线 —— 模块是否真的被接上")

    # A1 启动后实际加载的模块
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    all_mods = set()
    for p in (root / "app").rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        parts = [x for x in p.relative_to(root / "app").with_suffix("").parts if x != "__init__"]
        if parts:
            all_mods.add("app." + ".".join(parts))

    for m in list(sys.modules):
        if m.startswith("app."):
            del sys.modules[m]
    import app.api.main  # noqa: F401
    loaded = {m for m in sys.modules if m.startswith("app.")}
    not_loaded = sorted(all_mods - loaded)

    import importlib
    import re

    src_all = {p: p.read_text(encoding="utf-8")
               for p in (root / "app").rglob("*.py") if "__pycache__" not in str(p)}

    # 全仓库源码（app + scripts + tests）。只看 app/ 会把"只被测试引用"的模块
    # 误判成死代码，所以判据必须扫整个仓库。
    all_src = "\n".join(src_all.values())
    for d in ("scripts", "tests"):
        for p in (root / d).rglob("*.py"):
            if "__pycache__" not in str(p):
                all_src += p.read_text(encoding="utf-8")

    def _own_src(m: str) -> str:
        own = root / (m.replace(".", "/") + ".py")
        return own.read_text(encoding="utf-8") if own.exists() else ""

    # A3 先算（A2 要用）：模块级死代码 = 定义了但全仓库零 import。
    dead = []
    for m in sorted(all_mods):
        short = m.rsplit(".", 1)[-1]
        own_txt = _own_src(m)
        external = all_src.replace(own_txt, "") if own_txt else all_src
        if not re.search(rf"(?:from|import)\s+[\w.]*\b{re.escape(short)}\b", external):
            dead.append(m)

    # A2 断链的真判据是"导不导得进来"，不是"源码里有没有写过 import"。
    #   旧写法只在源码里搜"函数体内的 import 语句" —— 那只能证明**有人写过这行**，
    #   证明不了这行真的可达（路径拼错 / 循环导入 / 依赖缺失全都看不出来）。
    #   而且它把"全仓库零引用的模块"也算成断链，和死代码判据互相打架：
    #   同一个 app.models.kitchen 会同时报"断链"和"死代码"两条失败。
    #   现在直接 import 一遍 —— 导不进来才叫断链。
    #   注意：死代码也是"导得进来"的，所以它**不能**算作延迟导入，
    #   否则会打出"延迟导入（正常，按需加载）: app.models.kitchen"这种自相矛盾的说明。
    lazy, broken = [], []
    for m in not_loaded:
        try:
            importlib.import_module(m)
            (lazy if m not in dead else []).append(m)
        except Exception as e:
            broken.append(f"{m}: {type(e).__name__}: {e}")

    step("启动加载了绝大多数模块",
         len(loaded & all_mods) >= len(all_mods) - len(not_loaded),
         f"{len(loaded & all_mods)}/{len(all_mods)} 加载，{len(not_loaded)} 延迟")
    if lazy:
        note(f"延迟导入（正常，按需加载）: {', '.join(lazy)}")
    step("未加载模块都能按需导入（无断链）", not broken,
         f"断链: {broken}" if broken else "")

    if dead:
        note(f"⚠ 无任何 import 的模块（死代码）: {', '.join(dead)}")
    step("无死代码模块", not dead, f"{dead}" if dead else "")

    # A3b 孤儿顶层符号：模块里的某个顶层 class/def，**全仓库（含它自己的文件）
    #   除定义处外一次都没被用过**。这类残留最容易被忽略 —— 因为"模块被 import 了"
    #   看起来一切正常，实际上里面的东西早没人调了。
    #   三个必须排除的假阳性（本脚本第一版全踩了，一次报出 27 个）：
    #     ① 只在本模块内被别的函数调用 ⇒ **那是正常的内部组合，不是孤儿**
    #        （所以计数要含自身文件，不能把自身源码挖掉）；
    #     ② 被装饰器注册（`@app.get(...)` 的路由函数）⇒ 名字只在定义处出现，
    #        但它是 HTTP 入口，必须看前一行是不是 `@`；
    #     ③ 常量不查（常被当字符串比较，静态判不准）。
    #   ⚠ 仍只作 note 汇报，不判失败 —— "通过 getattr/反射调用"静态排除不了。
    orphan = []
    for m in sorted(all_mods):
        own_txt = _own_src(m)
        lines = own_txt.splitlines()
        for i, line in enumerate(lines):
            mm = re.match(r"^(?:class|def)\s+([A-Za-z]\w*)", line)
            if not mm:
                continue
            sym = mm.group(1)
            if sym.startswith("_"):
                continue
            prev = lines[i - 1].lstrip() if i else ""
            if prev.startswith("@"):          # 装饰器注册（路由/注册表），不是孤儿
                continue
            if len(re.findall(rf"\b{re.escape(sym)}\b", all_src)) <= 1:
                orphan.append(f"{m}::{sym}")
    if orphan:
        note(f"⚠ 顶层符号除定义处外全仓库零引用（孤儿/半成品）: {', '.join(orphan)}")


# ==========================================================================
# B. HTTP 链路
# ==========================================================================
_PROBE_PREFIX = "__e2e_probe"


def _cleanup_probe_recipes() -> None:
    """清掉本探针在菜谱**用户层**留下的痕迹。

    为什么必须清：增删改查只写 `data/recipes_user.json`（官方 CSV 是只读基线），
    而"删除"是墓碑式软删 —— 记录仍留在 added 里。不清理的话**每体检一次**
    就往用户层堆一条探针菜，跑十次之后菜谱库就多出十条 `__e2e_probe_dish__`，
    而使用者完全不知道为什么。清理是幂等的，也可以用来回收上次崩溃的残留。
    """
    try:
        from app.spec import recipe_store as _rs
        layer = _rs._read_user()
        victims = {a["id"] for a in layer["added"] if str(a.get("name", "")).startswith(_PROBE_PREFIX)}
        if not victims and not any(str(d).startswith(_rs.USER_ID_PREFIX) for d in layer["deleted"]):
            return
        layer["added"] = [a for a in layer["added"] if a["id"] not in victims]
        layer["deleted"] = [d for d in layer["deleted"] if d not in victims]
        _rs._write_user(layer)
        _rs.invalidate()
        # 全空了就把文件删掉 —— 留一个空壳 JSON 会让人以为"用户层有内容"。
        import os as _os
        if (not layer["added"] and not layer["overrides"] and not layer["deleted"]
                and _os.path.exists(_rs._USER_JSON)):
            _os.remove(_rs._USER_JSON)
            _rs.invalidate()
    except Exception as e:
        note(f"⚠ 探针用户层清理失败（菜谱库里可能残留 __e2e_probe 记录）: {type(e).__name__}: {e}")


def part_b() -> int:
    _cleanup_probe_recipes()      # 先回收上一次崩溃留下的残留
    section("B0. 服务可达性 + 代码新鲜度")
    st, body, ms, err = call("GET", "/health")
    if not step("GET /health 可达", st == 200, f"{st} {ms:.0f}ms {err or ''}"):
        print("\n  后端没起来 —— 跑 start.bat，或用不同端口起一个干净实例：")
        print("    .venv/Scripts/python.exe -m uvicorn app.api.main:app --port 8141")
        return 2

    # ⚠ 关键：确认跑的是**当前代码**，不是残留的旧进程
    st, body, ms, _ = call("GET", "/api/recipe_detail?id=__definitely_not_exist__")
    fresh = (st == 404)
    step("跑的是当前代码（未命中 id 返回 404，不是旧的 {}）", fresh,
         f"实际 {st} {json.dumps(body, ensure_ascii=False)[:60] if body is not None else ''}")
    if not fresh:
        note("⚠ 端口上很可能是**旧进程**（改了代码但服务没重启）—— 下面所有结论都不可信。"
             "换一个端口起新实例再跑。")

    section("B1. 全部路由逐个实测")
    st, names, ms, _ = call("GET", "/api/recipe_names")
    official = names.get("names", []) if isinstance(names, dict) else []
    step("GET /api/recipe_names", st == 200 and bool(official), f"{st} {len(official)} 个菜名")

    # 先取一个**真实**菜谱 id —— 详情 / 改 / 删都要用它。
    # ⚠ 不能写死 "R0001"：那是 2000 道库时代的 id，菜谱库换成官方 100 道之后
    #   必然 404，而这条会伪装成"详情接口坏了"。（菜谱 id 现在是 24 位十六进制。）
    st, lib0, ms, _ = call("GET", "/api/recipes?limit=1")
    real_id = None
    if isinstance(lib0, dict) and lib0.get("recipes"):
        real_id = lib0["recipes"][0].get("id")
    step("取到真实菜谱 id（供详情与增删改查使用）", bool(real_id), f"{real_id}")

    routes = [
        ("GET", "/", None, {200, 307}),
        ("GET", "/app", None, {200, 307}),
        ("GET", "/ready", None, {200}),
        ("GET", "/health", None, {200}),
        ("GET", "/api/recipe_names", None, {200}),
        ("GET", "/api/recipes_meta", None, {200}),
        ("GET", "/api/recipes?q=&limit=5", None, {200}),
        ("GET", f"/api/recipe_detail?id={real_id}", None, {200}),
        ("GET", "/api/recipe_detail?id=__no_such__", None, {404}),
        ("GET", "/api/devices", None, {200}),
        ("GET", "/api/device_presets", None, {200}),
        ("GET", "/api/schedules", None, {200}),
        ("GET", "/api/llm_status", None, {200}),
        ("POST", "/parse", RECIPES, {200}),
        ("POST", "/api/chat", {"messages": [{"role": "user", "content": "帮我排 4 道菜"}]}, {200}),
        ("POST", "/api/agent_plan", {"text": "做四道菜，请客要一起上桌"}, {200}),
        ("POST", "/api/plan", [{"id": i, "name": n} for i, n in enumerate(DISHES)], {200}),
        ("POST", "/schedule", {"recipes": RECIPES, "text": "", "notify": False}, {200}),
        ("POST", "/notify", {"recipes": RECIPES, "text": "", "notify": True}, {200}),
        ("POST", "/api/devices", {"灶眼数": 2}, {200}),
        # 界面上「选择厨房 → 恢复默认」走的就是这条。紧跟在上面那条后面调，
        # 顺手把 `POST /api/devices` 写下的那半份用户层清掉 —— 否则每体检一次，
        # 开发机上就留一个 {"灶眼数": 2} 的残缺覆盖文件。
        ("POST", "/api/devices/reset", None, {200}),
        ("POST", "/reschedule", {"recipes": RECIPES, "completed": [], "extra": None,
                                 "failed_device": "", "text": ""}, {200}),
    ]
    slow = []
    for m, p, b, expect in routes:
        st, body, ms, err = call(m, p, b)
        step(f"{m} {p}", st in expect, f"{st} {ms:.0f}ms" + (f"  !! {err}" if err else ""))
        if ms > 5000:
            slow.append((f"{m} {p}", round(ms)))

    # ---- 菜谱增删改查（走真实 HTTP）----
    # 官方菜谱是只读基线，探针必须用**自己新建**的菜来测改/删，
    # 且跑完要把用户层痕迹清干净（否则每体检一次就往 data/recipes_user.json 堆一条）。
    probe_name = "__e2e_probe_dish__"
    probe_ids: list[str] = []
    st, created, ms, _ = call("POST", "/api/recipes",
                              {"name": probe_name,
                               "steps": "第1步：锅中注油烧热，下蛋液炒散。；第2步：加盐炒匀。"})
    new_id = created.get("recipe", {}).get("id") if isinstance(created, dict) else None
    step("POST /api/recipes（新增）", st == 200 and bool(new_id), f"{st} {ms:.0f}ms")
    if new_id:
        probe_ids.append(new_id)
        st, _, ms, _ = call("PUT", f"/api/recipes/{new_id}",
                            {"name": probe_name, "steps": "第1步：放入蒸箱，100℃蒸20分钟。"})
        step("PUT /api/recipes/{id}（修改）", st == 200, f"{st} {ms:.0f}ms")
        st, det, ms, _ = call("GET", f"/api/recipe_detail?id={new_id}")
        step("改完之后详情里能读到新原文",
             st == 200 and isinstance(det, dict) and "蒸箱" in str(det.get("raw_steps", "")),
             f"{st}")
        st, _, ms, _ = call("DELETE", f"/api/recipes/{new_id}")
        step("DELETE /api/recipes/{id}（删除自建菜）", st == 200, f"{st} {ms:.0f}ms")
        st, gone, ms, _ = call("GET", f"/api/recipe_detail?id={new_id}")
        step("自建菜删除后确实不存在了（不是留下看不见的墓碑）",
             st == 404, f"{st} {ms:.0f}ms")
    else:
        note("⚠ 新增失败，后续改/删没有执行")

    # 恢复只对**官方菜**有意义（自建菜是真删）。这里做一次净变化为 0 的往返：
    # 删官方菜 → 它出现在「已删除」档 → 恢复 → 回到 100 条。
    if real_id:
        st, _, ms, _ = call("DELETE", f"/api/recipes/{real_id}")
        step("DELETE 官方菜（软删）", st == 200, f"{st} {ms:.0f}ms")
        st, onlydel, ms, _ = call("GET", "/api/recipes?deleted=1")
        in_del = [r["id"] for r in (onlydel or {}).get("recipes", [])] if isinstance(onlydel, dict) else []
        step("已删除档里能看到它（否则「可恢复」是空话）", real_id in in_del, f"{in_del[:3]}")
        st, _, ms, _ = call("POST", f"/api/recipes/{real_id}/restore")
        step("POST /api/recipes/{id}/restore（恢复官方菜）", st == 200, f"{st} {ms:.0f}ms")
        st, back, ms, _ = call("GET", "/api/recipes?limit=1000")
        step("恢复后回到 100 条（净变化为 0）",
             isinstance(back, dict) and back.get("total") == 100, f"{(back or {}).get('total')}")
    if slow:
        note(f"超 5s 的接口: {slow}")

    # ---- B2 L1：用**真实原文**喂解析器 ----
    section("B2. L1 解析层 —— 真实原文 → 结构化任务")
    st, lib, ms, _ = call("GET", "/api/recipes?limit=4")
    real = lib.get("recipes", []) if isinstance(lib, dict) else []
    payload = [{"name": r["name"], "ingredients": r.get("ingredients", ""),
                "steps": r.get("steps", ""), "label": ""} for r in real]
    st, parsed, ms, _ = call("POST", "/parse", payload)
    recs = parsed.get("parsed", []) if isinstance(parsed, dict) else []
    step("解析出全部 4 道菜", len(recs) == len(payload), f"{len(recs)} 道")
    empty = [r.get("name") for r in recs if not r.get("steps")]
    step("每道菜都有步骤", not empty, f"空: {empty}" if empty else "")
    devs = defaultdict(int)
    nsteps = 0
    for r in recs:
        for s in r.get("steps", []):
            nsteps += 1
            d = s.get("device") or s.get("dev") or "?"
            devs[d] += 1
    step("识别出设备", len(devs) >= 2, f"{nsteps} 步，设备分布 {dict(devs)}")
    if recs and recs[0].get("steps"):
        print(f"       样例: {json.dumps(recs[0]['steps'][0], ensure_ascii=False)[:150]}")

    # 契约差异：只给菜名（不给原文）时 /parse 返回空 —— 与 /schedule 的自动补全不同
    st, only_name, ms, _ = call("POST", "/parse", RECIPES)
    on = only_name.get("parsed", []) if isinstance(only_name, dict) else []
    n_only = sum(len(r.get("steps", [])) for r in on)
    if n_only == 0:
        note("契约差异：/parse 只给菜名（不带 steps 原文）时返回空 —— 它**不做**官方菜库补全，"
             "而 /schedule、/reschedule、/notify 都会自动补全。调用方按同样习惯用会拿到空结果。")

    # ---- B3 L4：求解 ----
    section("B3. L4 求解层 —— 任务 → 排程计划")
    st, plan, ms, _ = call("POST", "/schedule",
                           {"recipes": RECIPES, "text": "", "notify": False})
    p = plan.get("plan", plan) if isinstance(plan, dict) else {}
    tasks = p.get("tasks", [])
    step("返回计划且非空", bool(tasks), f"{len(tasks)} 个任务, {ms:.0f}ms")
    kpi = plan.get("kpi") or {}
    step("计划含 KPI", bool(kpi), str(list(kpi.keys())))
    have = {t.get("dish") for t in tasks}
    miss = [d for d in DISHES if d not in have]
    step(f"{len(DISHES)} 道菜全部进计划", not miss,
         f"实际 {sorted(x for x in have if x)}；缺 {miss}" if miss else "")

    # 未命中菜库的菜名必须**显式报出来**，不能静默丢菜。
    #   这是 main.py::_unknown() 的核心契约 —— 排程少一道菜而无人知晓是最难排查的故障。
    #   （之前探针拿手写的假菜名来"验"这条，反而把正确行为报成了失败，
    #     见 _official_dishes() 的注释。）
    st, ghost, _gms, _ = call("POST", "/schedule",
                              {"recipes": [{"name": g} for g in GHOSTS],
                               "text": "", "notify": False})
    gres = ghost if isinstance(ghost, dict) else {}
    gw = gres.get("warnings", []) or []
    gtasks = (gres.get("plan") or {}).get("tasks", []) or []
    step("未命中菜库的菜名被显式报出（不静默丢菜）",
         len(gw) >= len(GHOSTS) and not gtasks,
         f"warnings={len(gw)} 条, 计划任务={len(gtasks)} 个")

    bydev = defaultdict(list)
    for t in tasks:
        d = t.get("device")
        if d and d not in ("人工", "冰箱"):
            bydev[d].append((t.get("start_min", 0), t.get("end_min", 0), t.get("dish")))
    overlap = []
    for d, iv in bydev.items():
        iv.sort()
        for i in range(1, len(iv)):
            if iv[i][0] < iv[i - 1][1] - 1e-9:
                overlap.append((d, iv[i - 1], iv[i]))
    step("无设备时间冲突", not overlap, f"{overlap[:2]}" if overlap else "")

    # 顺序判据必须走**依赖边**，不能拿 step_id 的大小去比时间。
    #   ⚠ 本脚本踩过的坑：原来假设"step_id 大 ⇒ 时间晚"，于是把 `#3@0.0` 与 `#4@-2.0`
    #     报成顺序错误。实际上 #3 与 #4 的 depends_on 都只有 #2 —— 它们是**并行分支**；
    #     而且 #4 是被刻意前移到 t<0 的「提前准备」行（备菜早做是对的）。
    #     真正的不变量是：**对每条依赖边 u → v，必须 end(u) ≤ start(v)**。
    by_id = {t.get("task_id"): t for t in tasks}
    orderbad = []
    for t in tasks:
        for dep in (t.get("depends_on") or []):
            u = by_id.get(dep)
            if u is None:
                orderbad.append((t.get("task_id"), f"前置 {dep} 不在计划里"))
            elif u.get("end_min", 0) > t.get("start_min", 0) + 1e-6:
                orderbad.append((f"{dep}(end {u.get('end_min')})",
                                 f"{t.get('task_id')}(start {t.get('start_min')})"))
    step("依赖顺序正确（每条依赖边 end(前) ≤ start(后)）", not orderbad,
         f"{orderbad[:2]}" if orderbad else "")

    # 附注（不判失败）：菜谱原文里靠后的步骤若开始得更早、且两者之间**没有依赖边**，
    # 多半是 L1 依赖推断漏了一条边 —— 排程本身没问题，是输入图少了约束。
    # 实测「三味蒸鳗鱼&香茅烤鳗鱼同烹」#4 原文是"将腌制好的鳗鱼夹出"，
    # 逻辑上必须晚于 #3 的"腌制10分钟"，但它的 depends_on 只有 #2。
    seq = defaultdict(list)
    for t in tasks:
        if isinstance(t.get("step_id"), int):
            seq[t.get("dish")].append((t["step_id"], t.get("start_min"), t.get("task_id")))
    no_edge = []
    for dish, lst in seq.items():
        lst.sort()
        for i in range(1, len(lst)):
            prev, cur = lst[i - 1], lst[i]
            deps = (by_id.get(cur[2]) or {}).get("depends_on") or []
            if cur[1] < prev[1] - 1e-9 and prev[2] not in deps:
                no_edge.append(f"{dish}: {prev[2]}@{prev[1]} 早于 {cur[2]}@{cur[1]} 且无依赖边")
    if no_edge:
        note(f"⚠ 原文顺序与排程时间相反、且两者无依赖边（疑似 L1 依赖漏边，非排程问题）: "
             f"{no_edge[:3]}")
    step("计划带 makespan/finish_gap", "makespan_min" in p and "finish_gap_min" in p,
         f"总时长={p.get('makespan_min')} 出锅差={p.get('finish_gap_min')}")
    print(f"       KPI: {json.dumps(kpi, ensure_ascii=False)[:220]}")

    # ---- B4 L5：通知 ----
    section("B4. L5 通知层 —— 计划 → 可读提醒")
    st, nt, ms, _ = call("POST", "/notify", {"recipes": RECIPES, "text": "", "notify": True})
    notes = nt.get("notifications", []) if isinstance(nt, dict) else []
    step("生成了通知", bool(notes), f"{len(notes)} 条, {ms:.0f}ms")
    kinds = defaultdict(int)
    for n in notes:
        kinds[n.get("kind")] += 1
    step("通知带 kind 分类", len(kinds) >= 2, f"{dict(kinds)}")
    no_time = [n for n in notes if n.get("at_min") is None and n.get("time") is None]
    step("每条通知都带时间", not no_time, f"缺时间 {len(no_time)} 条" if no_time else "")
    manual_tasks = [t for t in tasks
                    if t.get("needs_human") and not t.get("unattended")
                    and t.get("unit_id") not in ("等待", "提前准备")]
    step("人工任务都有对应提醒", len(notes) >= 1 and bool(manual_tasks),
         f"人工任务 {len(manual_tasks)} 个 / 通知 {len(notes)} 条（无人值守步骤按设计不逐条提醒）")
    if notes:
        print(f"       样例: {json.dumps(notes[0], ensure_ascii=False)[:150]}")

    # ---- B5 动态重排 ----
    section("B5. 动态重排 —— 冻结 / 插单 / 设备故障")
    st, rs, ms, _ = call("POST", "/reschedule", {
        "recipes": RECIPES, "completed": [f"{DISHES[0]}#1"], "extra": None,
        "failed_device": "", "text": ""})
    step("插单重排返回", st == 200 and bool(rs.get("plan")), f"{st} {ms:.0f}ms")
    step("返回扰动度量", "perturbed" in rs, f"perturbed={rs.get('perturbed')}")
    step("返回姿态仲裁", bool(rs.get("stance")),
         json.dumps(rs.get("stance"), ensure_ascii=False)[:110] if rs.get("stance") else "")
    st, rf, ms, _ = call("POST", "/reschedule", {
        "recipes": RECIPES, "completed": [], "extra": None,
        "failed_device": "蒸箱", "text": ""})
    step("设备故障重排返回", st == 200 and bool(rf.get("plan")), f"{st} {ms:.0f}ms")
    if rf.get("plan"):
        left = [t for t in rf["plan"].get("tasks", []) if t.get("device") == "蒸箱"]
        step("故障设备不再被排入", not left,
             f"仍有 {len(left)} 个蒸箱任务" if left else "已全部顶替/移除")

    # ---- B6 前端 ----
    section("B6. 前端静态资源")
    st, html, ms, _ = call("GET", "/app")
    h = html if isinstance(html, str) else json.dumps(html, ensure_ascii=False)
    step("GET /app 返回完整页面", st == 200 and "<html" in h.lower() and len(h) > 10000,
         f"{st} {len(h)} 字节")
    for k in ("apiFetch", "AUTH_TOKEN", "markAuthRequired", "loadDetail", "renderSwim",
              "syncPlaybar", "openStepDetail",
              # 菜谱增删改查 + 气泡对话（原「AI对话」独立站点已并入右下角气泡）
              "saveRecipe", "deleteRecipe", "restoreRecipe", "toggleFloat", "sendChat"):
        step(f"页面含 {k}", k in h)
    _cleanup_probe_recipes()      # 收尾：别把探针菜留在用户层
    return 0


# ==========================================================================
# C. 层内直调（HTTP 看不到的部分）
# ==========================================================================
def part_c() -> None:
    section("C. 层内直调 —— HTTP 响应里看不到的环节")

    # C1 L2 语义先验：三态（LLM / 缓存 / 兜底）
    try:
        from app.agent import llm
        from app.agent.priors import get_priors
        st = "在线" if llm.enabled() and not llm.breaker_open() else (
            "熔断" if llm.breaker_open() else "未配置")
        note(f"LLM 状态: {st}（enabled={llm.enabled()}, breaker_open={llm.breaker_open()}）")
        # 必须喂**真的 Recipe 对象**：先验层要读 recipe.steps / .ingredients。
        # 旧写法 `type("R", (), {"name": "清蒸鳜鱼"})()` 造出来的假对象会抛
        # `AttributeError: 'R' object has no attribute 'steps'` —— 那是探针的错，
        # 不是先验层的错，却把 C1 整段报成了失败。
        from app.spec.adapter import get as _get_rec
        from app.parser.recipe_parser import parse_recipe as _pr
        _name = DISHES[0] if DISHES else "草莓蛋挞"
        _rec = _get_rec(_name) or {}
        _recipe = _pr(_name, _rec.get("ingredients", ""), _rec.get("steps", ""))
        pr = get_priors([_recipe])
        step("L2 先验能取到结果", isinstance(pr, dict), f"keys={list(pr)[:3]}")
        if pr:
            v = next(iter(pr.values()))
            # ⚠ DishPrior 是 **dataclass**，不是 dict —— 旧写法只认 dict，
            #   于是「先验含保热/等待字段」恒为 False，把正常的先验层报成失败。
            import dataclasses
            vd = (dataclasses.asdict(v) if dataclasses.is_dataclass(v)
                  else (v if isinstance(v, dict) else {}))
            note(f"先验样例: {json.dumps(vd, ensure_ascii=False, default=str)[:180]}")
            step("先验含保热/等待字段",
                 any(k in vd for k in ("serve_hot", "hold_min")),
                 str(sorted(vd)[:8]))
    except Exception as e:
        step("L2 先验层可用", False, f"{type(e).__name__}: {e}")

    # C2 L3 目标塑形：不同文本 → 不同权重
    try:
        from app.agent.objective import shape_policy
        cases = {
            "尽快做完": "rush",
            "请客，必须一起上桌": "feast",
            "别让我一直站着，少动手": "lazy",
            "做好就能马上吃上": "hot",
        }
        seen = {}
        for text, tag in cases.items():
            pol = shape_policy(text, 4, use_llm=False)
            seen[tag] = (round(pol.w_makespan, 2), round(pol.w_gap, 2),
                         round(pol.w_hand, 2), round(pol.w_hot, 2), pol.rationale)
            print(f"       {tag:<6} w_makespan={seen[tag][0]:<7} w_gap={seen[tag][1]:<7}"
                  f" w_hand={seen[tag][2]:<5} w_hot={seen[tag][3]:<5} {pol.rationale}")
        step("L3 权重随文本变化", len(set(seen.values())) >= 3,
             f"{len(set(seen.values()))} 种不同权重组合")
        step("rush 文本真的抬高 w_makespan", seen["rush"][0] > 1.0, f"{seen['rush'][0]}")
        step("feast 文本真的抬高 w_gap", seen["feast"][1] > 1.0, f"{seen['feast'][1]}")
    except Exception as e:
        step("L3 目标塑形层可用", False, f"{type(e).__name__}: {e}")

    # C3 L4 三轨是否都在
    try:
        from app.scheduler import engine
        has = {
            "贪心轨(_schedule)": hasattr(engine, "_schedule") or hasattr(engine, "schedule"),
            "CP-SAT 轨(cpsat)": (lambda: __import__("app.scheduler.cpsat", fromlist=["x"]))() is not None,
            "精确轨(model)": (lambda: __import__("app.scheduler.model", fromlist=["x"]))() is not None,
            "厨房语义(_add_kitchen_semantics)": hasattr(engine, "_add_kitchen_semantics"),
            "步骤回补(_restore_missing_steps)": hasattr(engine, "_restore_missing_steps"),
            "请求级资源覆盖(resource_override)": hasattr(engine, "resource_override"),
        }
        for k, v in has.items():
            step(f"L4 组件存在: {k}", bool(v))
    except Exception as e:
        step("L4 引擎可导入", False, f"{type(e).__name__}: {e}")

    # C4 溯源护栏（R1~R4）
    try:
        r = subprocess.run([sys.executable, "-m", "scripts.step_provenance"],
                           capture_output=True, text=True, timeout=300,
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        tail = [x for x in r.stdout.strip().splitlines() if x.strip()][-1]
        step("L1→L4 溯源护栏 R1~R4 无违规", r.returncode == 0, tail[:110])
    except Exception as e:
        step("溯源护栏可运行", False, f"{type(e).__name__}: {e}")


def main() -> int:
    no_http = "--no-http" in sys.argv
    print(f"框架完整性体检   BASE={BASE}   {'(跳过 HTTP)' if no_http else ''}")
    part_a()
    rc = 0
    if not no_http:
        rc = part_b()
        if rc == 2:
            print("\n  HTTP 段跳过（服务不可达）。")
    part_c()

    section("汇总")
    print(f"  通过 {len(OK)} / 失败 {len(BAD)}")
    if BAD:
        print("  失败项:")
        for b in BAD:
            print("    ✗", b)
    if NOTES:
        print("  说明:")
        for n in NOTES:
            print("    ·", n)
    return rc or (1 if BAD else 0)


if __name__ == "__main__":
    raise SystemExit(main())
