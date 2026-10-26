"""列出中转站可用模型别名（GET /models，只验鉴权不碰推理）。"""
import json
import os
import urllib.request

BASE = "https://potatoya.cn/pro100x/v1"
KEY = (os.environ.get("FT_TEST_KEY") or "").strip()
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
req = urllib.request.Request(BASE + "/models",
                             headers={"Authorization": f"Bearer {KEY}"})
d = json.loads(op.open(req, timeout=30).read().decode())
rows = d.get("data", [])
print(f"共 {len(rows)} 个别名\n")
print(f"{'别名':<26} {'状态':<10} {'官方价':<12} {'实际路由'}")
print("-" * 74)
for r in sorted(rows, key=lambda x: x.get("id", "")):
    print(f"{r.get('id',''):<26} {r.get('status',''):<10} "
          f"{r.get('official_price',''):<12} {r.get('actual_model','')}")
cur = [r["id"] for r in rows if r.get("status") == "current"]
print(f"\nstatus=current 的 {len(cur)} 个: {', '.join(cur)}")
print(f"\n项目当前用的是 gpt-5.6-sol —— 在列表里 = "
      f"{any(r.get('id') == 'gpt-5.6-sol' for r in rows)}")
