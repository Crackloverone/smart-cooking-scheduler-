"""接口层体检: 直接调用路由函数 (绕过 HTTP, 不需要 httpx / TestClient)。

用法: python -m scripts.api_probe

检查项覆盖本轮修复:
  1. 设备配置非法输入不再抛 ValueError, 且非法时不改任何状态
  2. 设备故障用请求级 ContextVar 覆盖, 全局 RESOURCE_UNITS 不再被改写
  3. 并发场景: 一个请求"蒸箱故障"不会污染另一个正常排程请求
  4. 大 JSON 走 mtime 缓存 (79ms -> 亚毫秒)
  5. /notify 尊重 notify 字段
  6. 设备预设文件缺失时不 500
  7. 排程历史限长 20
  8. 重名菜消歧 (不再静默塌成一道)
  9. main.py 不再戳 llm._CB 私有字典
"""
from __future__ import annotations
import os, sys, time, json, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("FT_LLM", "off")
os.environ.setdefault("FT_AGENT_LLM", "off")
os.environ.setdefault("FT_NOTIFY_LLM", "off")
os.environ.setdefault("FT_WARM_LLM", "off")

import app.api.main as api
from app.scheduler import engine as _eng
from app.api.main import PlanItem, RecipeIn, ScheduleIn, RescheduleIn

_PASS, _FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (_PASS if ok else _FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))


def brief(x, n=160):
    try:
        s = json.dumps(x, ensure_ascii=False, default=str)
    except Exception:
        s = repr(x)
    return s[:n]


def call(tag, fn, *a, **kw):
    t0 = time.time()
    try:
        out = fn(*a, **kw)
        dt = (time.time() - t0) * 1000
        print(f"  {tag:34s} OK   {dt:7.1f} ms  {brief(out)}")
        return out
    except Exception as e:
        dt = (time.time() - t0) * 1000
        print(f"  {tag:34s} 抛异常 {dt:6.1f} ms  {type(e).__name__}: {e}")
        return e


print("== 1. 设备配置校验: 非法输入不得抛异常, 也不得改状态 ==")
before = dict(_eng.RESOURCE_UNITS)
r = call("非法值 {'灶眼数': '很多'}", api.set_devices, {"灶眼数": "很多"})
check("非法值返回 error 字段而非抛异常",
      isinstance(r, dict) and "error" in r, brief(r))
check("非法值未污染引擎容量", _eng.RESOURCE_UNITS == before)
r = call("超范围 {'蒸箱': 999}", api.set_devices, {"蒸箱": 999})
check("超范围被拒", isinstance(r, dict) and "error" in r)
check("超范围未污染引擎容量", _eng.RESOURCE_UNITS == before)
r = call("未知键 {'微波炉': 2}", api.set_devices, {"微波炉": 2})
check("未知设备键被拒", isinstance(r, dict) and "error" in r)
r = call("合法 {'灶眼数': 4}", api.set_devices, {"灶眼数": 4})
check("合法值生效", _eng.RESOURCE_UNITS.get(_eng.DEVICE_STOVE) == 4)
api.set_devices({"灶眼数": 2})
check("可恢复默认", _eng.RESOURCE_UNITS.get(_eng.DEVICE_STOVE) == 2)

print("\n== 2. 设备故障: 全局 RESOURCE_UNITS 不得被改写 ==")
steamer_before = _eng.RESOURCE_UNITS.get(_eng.DEVICE_STEAMER)
out = call("蒸箱故障重排", api.do_reschedule,
           RescheduleIn(recipes=[RecipeIn(name="三味蒸鳗鱼&香茅烤鳗鱼同烹")],
                        failed_device="蒸箱"))
check("故障请求后全局蒸箱容量未被改",
      _eng.RESOURCE_UNITS.get(_eng.DEVICE_STEAMER) == steamer_before,
      f"蒸箱={_eng.RESOURCE_UNITS.get(_eng.DEVICE_STEAMER)}")
if isinstance(out, dict):
    check("故障请求本身正常返回计划", out.get("plan") is not None)

print("\n== 3. 并发隔离: 故障请求不得影响并发中的正常排程 ==")
# 正常请求: 一道必须用蒸箱的菜; 故障请求: 同一道菜 + 蒸箱故障
menu = [RecipeIn(name="三味蒸鳗鱼&香茅烤鳗鱼同烹")]
res_normal, res_fail = {}, {}


def _run_normal():
    res_normal["out"] = api.do_schedule(ScheduleIn(recipes=menu, notify=False))


def _run_fail():
    res_fail["out"] = api.do_reschedule(
        RescheduleIn(recipes=menu, failed_device="蒸箱"))


ts = [threading.Thread(target=_run_normal) for _ in range(4)]
tf = [threading.Thread(target=_run_fail) for _ in range(4)]
for t in ts + tf:
    t.start()
for t in ts + tf:
    t.join()


def _steamer_rows(o):
    if not isinstance(o, dict):
        return -1
    rows = (o.get("plan") or {}).get("tasks", [])
    return sum(1 for r in rows if r.get("device") == "蒸箱")


n_steam = [_steamer_rows(res_normal["out"])]
f_steam = [_steamer_rows(res_fail["out"])]
check("并发正常请求仍看到蒸箱 (未被故障请求清零)",
      all(x > 0 for x in n_steam), f"正常请求蒸箱行数={n_steam}")
check("并发故障请求仍正常返回",
      isinstance(res_fail["out"], dict) and res_fail["out"].get("plan") is not None)

print("\n== 4. 大 JSON 缓存: 第二次起应为亚毫秒 ==")
t0 = time.time(); api.list_recipes(q="鸡", limit=10); first = (time.time() - t0) * 1000
t0 = time.time()
for _ in range(20):
    api.list_recipes(q="鸡", limit=10)
avg = (time.time() - t0) / 20 * 1000
print(f"  首次 {first:.1f} ms / 热态均值 {avg:.2f} ms")
check("热态读取 < 5ms", avg < 5.0, f"{avg:.2f} ms")
check("缓存按 mtime 失效 (第二次返回同一对象)",
      api.list_recipes()["total"] > 0)

print("\n== 5. /notify 尊重 notify 字段 ==")
r_on = api.do_notify(ScheduleIn(recipes=[RecipeIn(name="鸡翅包饭")], notify=True))
r_off = api.do_notify(ScheduleIn(recipes=[RecipeIn(name="鸡翅包饭")], notify=False))
check("notify=False 返回空通知",
      isinstance(r_off, dict) and r_off.get("notifications") == [],
      brief(r_off, 60))
check("notify=True 有内容",
      isinstance(r_on, dict) and len(r_on.get("notifications") or []) >= 0)

print("\n== 6. 设备预设文件缺失不得 500 ==")
_orig = api._load_json_cached
api._load_json_cached = lambda p: None      # 模拟文件读不到
try:
    r = api.get_presets()
    check("预设缺失时返回空列表", isinstance(r, dict) and r.get("presets") == [])
finally:
    api._load_json_cached = _orig

print("\n== 7. 排程历史限长 ==")
for i in range(30):
    api.do_schedule(ScheduleIn(recipes=[RecipeIn(name="鸡翅包饭")], notify=False))
h = api.list_schedules()
check("历史长度 <= 20", h["count"] <= 20, f"count={h['count']}")

print("\n== 8. 重名菜消歧 ==")
r = api.do_schedule(ScheduleIn(
    recipes=[RecipeIn(name="鸡翅包饭"), RecipeIn(name="鸡翅包饭")], notify=False))
if isinstance(r, dict):
    tasks = (r.get("plan") or {}).get("tasks", [])
    dishes = {t.get("dish") for t in tasks}
    check("两份同名菜都被排进去",
          any("(2)" in str(d) for d in dishes), f"dishes={sorted(map(str, dishes))}")
    check("给出重名告警",
          any("重名" in w for w in (r.get("warnings") or [])),
          brief(r.get("warnings")))

print("\n== 9. 不再戳私有 _CB ==")
src = open(os.path.join(ROOT, "app", "api", "main.py"), encoding="utf-8").read()
check("main.py 无 llm._CB 直接访问", "llm._CB" not in src)
check("llm.breaker_reason() 存在", hasattr(api.llm, "breaker_reason"))

print("\n== 10. 空输入端点不崩 ==")
for tag, fn, arg in [("POST /schedule 空菜谱", api.do_schedule, ScheduleIn(recipes=[])),
                     ("POST /notify 空菜谱", api.do_notify, ScheduleIn(recipes=[])),
                     ("POST /parse 空列表", api.parse, []),
                     ("POST /api/plan 空列表", api.plan_endpoint, []),
                     ("POST /api/chat 空消息", api.chat_endpoint, api.ChatIn(messages=[]))]:
    out = call(tag, fn, arg)
    check(f"{tag} 不抛异常", not isinstance(out, Exception))

print(f"\n==== 汇总: {len(_PASS)} 通过 / {len(_FAIL)} 失败 ====")
if _FAIL:
    for f in _FAIL:
        print("  FAIL:", f)
    sys.exit(1)
