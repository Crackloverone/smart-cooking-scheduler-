"""
菜谱库 —— 全项目**唯一**的菜谱真源。

分两层
------
· **官方层** `data/recipes_official_100.csv` —— 赛题下发的 100 道菜。**只读，永不被改写。**
· **用户层** `data/recipes_user.json`     —— 用户的新增 / 修改 / 删除。可恢复。

为什么不让"增删改"直接写官方 CSV
--------------------------------
1. 官方 CSV 是赛题评测数据，硬删/改写**不可逆**；
2. `adapter.all_names()` / `get()` 直接读它决定"哪些菜能排"，改写它会**静默改变排程行为**
   （而且改完只有重跑基准才看得出来）；
3. 删除做成 tombstone（墓碑）⇒ "恢复"就是从 deleted 里移掉，换机器/重新部署自动回到基线。

这条纪律和档案层当初的做法一致：**官方数据当只读基线，用户改动只写用户层。**

对上层暴露的口径
----------------
`all_names()` 返回**含重名**的全部条数（官方 100 道里有 1 组重名 ⇒ 99 个不同菜名，
但必须返回 100 条 —— 赛题口径就是 100）。去重会让"条数与赛题一致"这条断言失败。
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_OFFICIAL_CSV = os.path.join(_ROOT, "data", "recipes_official_100.csv")
_USER_JSON = os.path.join(_ROOT, "data", "recipes_user.json")

# 用户新增菜谱的 id 前缀。官方 id 是 24 位十六进制，加前缀后**天然不会撞**，
# 而且一眼能看出这条是用户加的（排查线上数据时很省事）。
USER_ID_PREFIX = "u_"

_MAX_NAME = 80
_MAX_TEXT = 20000

_lock = threading.Lock()
_cache: dict = {}          # {"official": (mtime, rows), "user": (mtime, layer)}


# ---------------------------------------------------------------- 读取

def _read_official() -> list[dict]:
    """读官方 CSV。文件缺失时返回空表而不是抛异常 —— 页面该给结论，不该 500。"""
    try:
        mtime = os.path.getmtime(_OFFICIAL_CSV)
    except OSError:
        return []
    hit = _cache.get("official")
    if hit and hit[0] == mtime:
        return hit[1]
    rows: list[dict] = []
    try:
        with io.open(_OFFICIAL_CSV, encoding="utf-8-sig", newline="") as f:
            for i, row in enumerate(csv.DictReader(f)):
                name = (row.get("名称") or "").strip()
                if not name:
                    continue
                rid = (row.get("菜谱id") or "").strip() or f"official_{i}"
                rows.append({
                    "id": rid,
                    "name": name,
                    "ingredients": row.get("食材清单") or "",
                    "steps": row.get("烹饪步骤") or "",
                    "source": "official",
                })
    except Exception:
        return []
    _cache["official"] = (mtime, rows)
    return rows


def _empty_layer() -> dict:
    return {"added": [], "overrides": {}, "deleted": []}


def _read_user() -> dict:
    """读用户层。结构不合法时**退化为空层**，不让一个坏文件废掉整个菜谱库。"""
    try:
        mtime = os.path.getmtime(_USER_JSON)
    except OSError:
        return _empty_layer()
    hit = _cache.get("user")
    if hit and hit[0] == mtime:
        return hit[1]
    layer = _empty_layer()
    try:
        with io.open(_USER_JSON, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return layer
    if not isinstance(raw, dict):
        return layer
    added = raw.get("added")
    if isinstance(added, list):
        for a in added:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "").strip()
            if not name:
                continue
            layer["added"].append({
                "id": str(a.get("id") or "").strip() or _new_id(),
                "name": name,
                "ingredients": str(a.get("ingredients") or ""),
                "steps": str(a.get("steps") or ""),
            })
    ov = raw.get("overrides")
    if isinstance(ov, dict):
        for k, v in ov.items():
            if not isinstance(v, dict):
                continue
            layer["overrides"][str(k)] = {
                "name": str(v.get("name") or "").strip(),
                "ingredients": str(v.get("ingredients") or ""),
                "steps": str(v.get("steps") or ""),
            }
    dele = raw.get("deleted")
    if isinstance(dele, list):
        layer["deleted"] = [str(x) for x in dele if str(x).strip()]
    _cache["user"] = (mtime, layer)
    return layer


def _write_user(layer: dict) -> None:
    """原子落盘：先写临时文件再 os.replace。

    直接 `open(path,"w")` 全量重写有两个问题：写到一半崩溃会留下半个 JSON
    （下次读取整层消失）；并发请求之间会互相截断。os.replace 在同一文件系统内
    是原子的 —— 读到的永远是"完整的旧版"或"完整的新版"。
    """
    os.makedirs(os.path.dirname(_USER_JSON), exist_ok=True)
    tmp = _USER_JSON + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(layer, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _USER_JSON)
    # 显式失效：mtime 精度在"同一秒内连续写两次"时不够用，会读到上一次的内容。
    _cache.pop("user", None)


def _new_id() -> str:
    return USER_ID_PREFIX + uuid.uuid4().hex[:16]


# ---------------------------------------------------------------- 合并视图

def _merged() -> list[dict]:
    """官方 + 用户层合并后的**全部**记录（含已删除，带 deleted 标记）。"""
    layer = _read_user()
    gone = set(layer["deleted"])
    ov = layer["overrides"]
    out: list[dict] = []
    for r in _read_official():
        rid = r["id"]
        rec = dict(r)
        o = ov.get(rid)
        if o:
            if o.get("name"):
                rec["name"] = o["name"]
            if o.get("ingredients"):
                rec["ingredients"] = o["ingredients"]
            if o.get("steps"):
                rec["steps"] = o["steps"]
            rec["edited"] = True
        else:
            rec["edited"] = False
        rec["deleted"] = rid in gone
        out.append(rec)
    for a in layer["added"]:
        rec = dict(a, source="user", edited=False)
        rec["deleted"] = a["id"] in gone
        out.append(rec)
    return out


def all_records(include_deleted: bool = False) -> list[dict]:
    recs = _merged()
    return recs if include_deleted else [r for r in recs if not r["deleted"]]


def all_names() -> list[str]:
    """全部可用菜名，**含重名**（官方 100 道里有 1 组重名，必须保持 100 条）。"""
    return [r["name"] for r in all_records()]


def get_by_id(rid: str):
    """按 id 取记录。**包含已删除的** —— 恢复功能需要能查到它。

    调用方（`/api/recipe_detail`）必须自己判断 `deleted`：查得到不等于还能用。
    """
    for r in _merged():
        if str(r["id"]) == str(rid):
            return r
    return None


def stats() -> dict:
    layer = _read_user()
    recs = _merged()
    return {
        "total": len([r for r in recs if not r["deleted"]]),
        "official_total": len([r for r in recs if r["source"] == "official"]),
        "added_total": len(layer["added"]),
        "edited_total": len(layer["overrides"]),
        "deleted_total": len([r for r in recs if r["deleted"]]),
    }


# ---------------------------------------------------------------- 写

def _clean_text(v, field: str, required: bool = True) -> str:
    s = "" if v is None else str(v).strip()
    if required and not s:
        raise ValueError(f"{field}不能为空")
    if len(s) > _MAX_TEXT:
        raise ValueError(f"{field}过长（上限 {_MAX_TEXT} 字）")
    return s


def _clean_name(v) -> str:
    s = "" if v is None else str(v).strip()
    if not s:
        raise ValueError("菜名不能为空")
    if len(s) > _MAX_NAME:
        raise ValueError(f"菜名过长（上限 {_MAX_NAME} 字）")
    return s


def create(name, ingredients, steps) -> dict:
    """新增菜谱。返回新记录。

    ⚠ 允许重名：官方库本身就有 1 组重名（100 条 / 99 个菜名）。这里**不**做唯一性
    校验，否则会出现"官方重名能共存、用户重名被拒"这种自相矛盾的规则。
    排程按 `name` 取第一条 —— 与官方重名的行为保持一致。
    """
    nm = _clean_name(name)
    ing = _clean_text(ingredients, "食材清单", required=False)
    stp = _clean_text(steps, "烹饪步骤", required=False)
    if not ing and not stp:
        raise ValueError("食材清单与烹饪步骤至少要填一项")
    with _lock:
        layer = _read_user()
        rec = {"id": _new_id(), "name": nm, "ingredients": ing, "steps": stp}
        layer["added"].append(rec)
        _write_user(layer)
    return dict(rec, source="user", edited=False, deleted=False)


def update(rid: str, name=None, ingredients=None, steps=None) -> dict:
    """修改菜谱。官方菜谱的修改只写进用户层 overrides，**官方 CSV 不动**。"""
    rid = str(rid)
    cur = get_by_id(rid)
    if cur is None:
        raise KeyError(f"菜谱不存在: {rid}")
    nm = _clean_name(name) if name is not None else cur["name"]
    ing = _clean_text(ingredients, "食材清单", required=False) if ingredients is not None \
        else cur["ingredients"]
    stp = _clean_text(steps, "烹饪步骤", required=False) if steps is not None else cur["steps"]
    if not ing and not stp:
        raise ValueError("食材清单与烹饪步骤至少要留一项")
    with _lock:
        layer = _read_user()
        if cur["source"] == "official":
            layer["overrides"][rid] = {"name": nm, "ingredients": ing, "steps": stp}
        else:
            for a in layer["added"]:
                if a["id"] == rid:
                    a.update({"name": nm, "ingredients": ing, "steps": stp})
                    break
            else:
                raise KeyError(f"菜谱不存在: {rid}")
        _write_user(layer)
    return dict(get_by_id(rid) or {})


def delete(rid: str) -> dict:
    """删除菜谱。

    两种语义，按**数据来源**分（这条规则要在界面上说清楚）：

    · **官方菜** → 墓碑式软删（`deleted` 列表），可恢复。官方 CSV 一行不动。
      这是为了保护赛题下发的评测数据 —— 它同时是排程的数据源，硬删不可逆。
    · **自建菜** → **真删**（从 added 里移除，连同它的墓碑）。理由：自建记录没有
      "只读基线"要保护；若也做墓碑，删掉的自建菜会永久留一条**谁也看不到、
      也恢复不了**的垃圾（`deleted_total` 里算着、列表里搜不到），跑几次就成了
      噪声。界面的二次确认会明说这一点。

    返回值里 `purged` 标记走的是哪条路。
    """
    rid = str(rid)
    cur = get_by_id(rid)
    if cur is None:
        raise KeyError(f"菜谱不存在: {rid}")
    with _lock:
        layer = _read_user()
        if cur["source"] == "user":
            layer["added"] = [a for a in layer["added"] if a["id"] != rid]
            layer["deleted"] = [d for d in layer["deleted"] if d != rid]
            layer["overrides"].pop(rid, None)
            _write_user(layer)
            return {"deleted": 1, "purged": 1, "id": rid, "recoverable": False, **stats()}
        newly = rid not in layer["deleted"]
        if newly:
            layer["deleted"].append(rid)
            _write_user(layer)
        return {"deleted": 1 if newly else 0, "purged": 0, "id": rid,
                "recoverable": True, **stats()}


def restore(rid: str) -> dict:
    """恢复被删除的**官方**菜谱。

    自建菜是真删，记录已不存在 ⇒ 这里抛 KeyError（调用方转 404）。
    返回 `restored: 0` 而不是报错只用于"本来就还在"的幂等场景。
    """
    rid = str(rid)
    cur = get_by_id(rid)
    if cur is None:
        raise KeyError(f"菜谱不存在: {rid}")
    with _lock:
        layer = _read_user()
        if rid not in layer["deleted"]:
            return {"restored": 0, "id": rid, **stats()}
        layer["deleted"].remove(rid)
        _write_user(layer)
    return {"restored": 1, "id": rid, **stats()}


def invalidate() -> None:
    """丢掉缓存（测试/热更新用）。"""
    _cache.clear()


def version() -> tuple:
    """缓存戳：官方层与用户层的 mtime。

    调用方（`adapter._load`）用它决定"要不要重建派生缓存"。**不能**用
    "`if _DB: return`" 这种一次性硬缓存 —— 那样用户新增一道菜之后，
    排程侧永远看不到它（库里有、排不出来，是最难查的一类不一致）。
    """
    def _mt(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    return (_mt(_OFFICIAL_CSV), _mt(_USER_JSON))


# ---------------------------------------------------------------- 展示派生
# 菜谱库详情要显示的"分类 / 时长"，官方 CSV 里没有这两列 —— 只能从原文派生。
# 派生规则与 `adapter._category_of` / `all_meta` **完全一致**，绝不另写一套：
# 库里的分类和排程时的分类必须是同一个东西，否则筛选出来的"蒸箱料理"里会混进派。

def display_item(rec: dict) -> dict:
    """把一条记录变成菜谱库列表/详情用的展示结构。"""
    from app.spec.adapter import _category_of, _split_ingredients

    name = rec["name"]
    steps_text = rec.get("steps") or ""
    ing_text = rec.get("ingredients") or ""
    # 时长：累加原文里的分钟/小时。这是**文本口径**的估计，不是排程时长 ——
    # 排程时长由 L1 解析 + 求解器决定，两者本来就不该相等，前端文案里已注明。
    import re as _re
    mins = sum(float(x) for x in _re.findall(r"(\d+(?:\.\d+)?)\s*分钟", steps_text))
    hours = sum(float(x) * 60 for x in _re.findall(r"(\d+(?:\.\d+)?)\s*个?小时", steps_text))
    majors, minors = _split_ingredients(ing_text) if ing_text else ([], [])
    return {
        "id": rec["id"],
        "name": name,
        "category": _category_of(name, steps_text),
        "cooking_method": _category_of(name, steps_text),
        "cook_time": int(mins + hours),
        "steps_count": len(_re.findall(r"第\s*\d+\s*步", steps_text)),
        "ingredients": [{"name": i["name"], "amount": i["unit"]} for i in majors + minors],
        # 原文用"；"分隔步骤，切完每段尾巴上会留一个分号，展示时去掉。
        "steps": [s.strip().rstrip("；;") for s in _re.split(r"第\s*\d+\s*步[：:]", steps_text)
                  if s.strip()],
        "raw_ingredients": ing_text,
        "raw_steps": steps_text,
        "source": rec["source"],
        "edited": bool(rec.get("edited")),
        "deleted": bool(rec.get("deleted")),
    }
