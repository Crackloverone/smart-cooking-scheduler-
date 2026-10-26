# 智能烹饪调度 Agent API 接口文档（与公网部署一致）

| 文档项 | 内容 |
| --- | --- |
| 系统名称 | 智能烹饪调度 Agent（方太专项赛·赛道二 ZX-2026-0302） |
| 文档版本 | V2.0（按真实部署接口编写） |
| 服务 Base URL | `http://112.124.50.217:8766` |
| 交互协议 | HTTP / JSON，UTF-8 |
| 鉴权方式 | **无需访问令牌**（公开测评免登录、免令牌） |
| 接口文档（在线） | `http://112.124.50.217:8766/docs`（Swagger UI） |
| OpenAPI 契约 | `http://112.124.50.217:8766/openapi.json` |
| 更新日期 | 2026-10-09 |

> 说明：系统采用**分阶段 REST 接口**（解析 → 排程 → 重排 → 通知 → 甘特图 → 性能），而非单一聚合接口。本文全部路径、字段、枚举均来自服务端真实 OpenAPI 契约，示例数值为真实调用结果。

---

## 1. 通用约定

- 请求/响应 `Content-Type: application/json; charset=utf-8`。
- 成功响应直接返回业务 JSON，无 `code/message/data` 包装层。
- 参数校验失败返回 `422`，结构为：

```json
{ "detail": [ { "loc": ["body", "recipe_ids"], "msg": "field required", "type": "missing" } ] }
```

- 其他错误一般返回 `{ "detail": "错误原因" }`，且不回传明文密钥。

### 1.1 接口总览

| 层级 | 方法 | 路径 | 功能 |
| --- | --- | --- | --- |
| 通用 | GET | `/api/health` | 健康检查与配置概况 |
| 菜谱 | GET | `/api/recipes` | 菜谱库列表（含缓存命中标记） |
| 菜谱 | GET | `/api/recipes/{recipe_id}` | 单个菜谱详情 |
| 菜谱 | POST | `/api/recipes` | 新增菜谱 |
| 菜谱 | POST | `/api/recipes/import` | 导入 CSV/JSON |
| 菜谱 | GET | `/api/recipes/export` | 导出菜谱 |
| 设备 | GET / PUT | `/api/devices` | 读取 / 保存厨房设备配置 |
| L1 | POST | `/api/parse` | 菜谱结构化解析 |
| L2/L3 | POST | `/api/schedule` | 多菜并行调度与多目标优化 |
| L4 | POST | `/api/replan` | 动态重排（含突发事件） |
| L4 | POST | `/api/reschedule` | 等价于 `/api/replan`（别名） |
| L4 | POST | `/api/notify` | 关键节点通知生成 |
| 出图 | GET | `/api/gantt` | 甘特图（SVG/HTML） |
| L5 | GET | `/api/metrics` | 各阶段耗时统计 |
| L5 | POST | `/api/metrics/reset` | 重置性能统计 |
| 统计 | GET | `/api/stats` | 菜谱库 + 设备 + 性能综合统计 |
| 模型 | GET | `/api/models` | 本地模型清单 |
| 厂商 | GET | `/api/providers` | 厂商/模型/密钥掩码 |
| 厂商 | POST | `/api/providers/config` | 保存厂商 Key / Base URL |
| 厂商 | POST | `/api/providers/custom` | 新增自定义厂商 |
| 模型 | POST | `/api/llm/test` | 测试模型连通 |
| 模型 | POST | `/api/llm/select` | 记住选中模型 |
| 对话 | POST | `/api/chat` | AI 助手（可自主调用工具） |
| 对话 | GET | `/api/chat/tools` | 列出对话可用工具 |
| 对话 | POST | `/api/chat/reset` | 清空会话 |

---

## 2. 健康检查

`GET /api/health` —— 返回服务状态、模型连通性、云端厂商配置概况、菜谱库概况（密钥仅以掩码/是否配置体现）。

## 3. 菜谱库

### 3.1 菜谱列表

`GET /api/recipes?keyword=&limit=200`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `keyword` | string | 按菜名/食材模糊搜索，可空 |
| `limit` | integer | 1~2000，默认 200 |

返回菜谱数组，每项含 `id / name / ingredients / steps_text / source`，并带解析缓存命中标记。

### 3.2 单个菜谱

`GET /api/recipes/{recipe_id}` —— 返回菜谱原始对象（`Recipe`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 菜谱 id |
| `name` | string | 名称 |
| `ingredients` | string | 食材清单原始文本 |
| `steps_text` | string | 烹饪步骤原始自然语言文本 |
| `source` | string | 来源：csv / manual / import |

## 4. 厨房设备配置

`GET /api/devices` 读取、`PUT /api/devices` 保存。请求体：

```json
{ "devices": { "<设备key>": { "key": "steamer", "name": "蒸箱", "resource_kind": "exclusive_temp" } } }
```

`DeviceConfig` 主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `key` / `name` | string | 设备键 / 名称 |
| `resource_kind` | enum | `slots` / `exclusive_temp` / `exclusive_mode` / `capacity` / `shared` / `virtual` |
| `slots` | integer | 槽位/层数容量，默认 1 |
| `enabled` | boolean | 是否启用，默认 true |
| `modes` / `levels` | string[] | 可用模式 / 层级 |
| `temp_range` | number[] | 温度范围 |
| `same_temp_required` | boolean | 是否要求同温（蒸/烤箱同温并行、异温互斥） |
| `same_mode_required` | boolean | 是否要求同模式 |
| `linked_with` | string | 联动设备（如烟机随灶具） |
| `zones` / `power_levels` | string[] / integer | 灶眼分区 / 火力档数 |

---

## 5. L1 菜谱结构化解析

`POST /api/parse`

**请求体 `ParseRequest`：**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `recipe_ids` | string[] | 是 | 待解析菜谱 id |
| `model` | string/null | 否 | 指定模型 |
| `use_llm` | boolean | 否 | 是否启用 LLM 增强，默认 true |
| `force` | boolean | 否 | 忽略缓存强制重解析，默认 false |
| `manual_overrides` | object | 否 | 人工修正步骤 `{recipe_id: StructuredStep[]}`，优先于自动解析 |

**响应 `ParseResponse`：**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `results` | `ParseResult[]` | 每道菜的解析结果 |
| `elapsed_ms` | number | 总耗时（毫秒） |
| `cache_hits` | integer | 缓存命中数 |
| `llm_calls` | integer | 实际 LLM 调用数 |

`ParseResult` 字段：`recipe_id`、`recipe_name`、`steps[]`（`StructuredStep`）、`device_summary[]`、`total_duration`（关键路径时长）、`parser`（rule/llm/merged）、`llm_model`、`low_confidence_steps[]`、`elapsed_ms`、`warnings[]`。

### 5.1 StructuredStep（结构化步骤，L1 核心产物）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `step_no` | integer | 序号（从 1 开始） |
| `raw_text` | string | 原始自然语言文本 |
| `title` | string | 动作摘要（用于甘特图/通知） |
| `device` | string | 烟机/灶具/蒸箱/烤箱/冰箱/洗碗机/净水机/料理机/备置区/人工 |
| `mode` | string/null | 设备模式，如 普通蒸 / 常规烘焙 |
| `temperature` | number/null | 温度（℃） |
| `duration` | number | 持续时间（分钟），默认 3 |
| `power_level` | string/null | 火力/档位，如 大火、9档 |
| `depends_on` | integer[] | 前置依赖步骤序号 |
| `needs_human` | boolean | 是否需人工介入，默认 true |
| `unattended` | boolean | 是否无人值守 |
| `human_minutes` | number | 占用双手时长 |
| `resource_slot` | string | 资源槽位类名（hob/steamer.cavity/oven.cavity/fridge/human/prep） |
| `preheat_minutes` | number | 隐式预热前置占用（并入本步骤区间，0=无） |
| `is_preheat` | boolean | 是否原文显式预热步骤 |
| `confidence` | number | 置信度 0~1 |
| `source` | string | rule / llm / merged |
| `note` | string | 备注（如“时长按经验估算”） |

---

## 6. L2/L3 多菜并行调度

`POST /api/schedule`

**请求体 `ScheduleRequest`：**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `recipe_ids` | string[] | 是 | 待排菜谱 id |
| `recipes` | Recipe[] | 否 | 临时菜谱（如临时加菜） |
| `devices` | object | 否 | 自定义设备配置 |
| `model` | string/null | 否 | 模型（仅用于解析新菜谱） |
| `use_llm` | boolean | 否 | 默认 false；排程本身不依赖 LLM |
| `target_spread` | number | 否 | 目标出锅差（分钟），默认 5 |
| `align_finish` | boolean | 否 | 是否对齐出锅，默认 true |
| `use_cpsat` | boolean | 否 | 启用 CP-SAT 第二轨，默认 true，超时回退启发式 |
| `cpsat_time_limit` | number | 否 | CP-SAT 预算（秒），0.5~300 |
| `cpsat_makespan_slack_min` | number | 否 | Phase2 冻结总耗时的放宽量；0=严格最优 |
| `time_budget_ms` | number | 否 | SGS 多起点启发式预算（毫秒），50~60000 |
| `weights` | object | 否 | 多目标权重 |
| `manual_overrides` | object | 否 | 人工修正步骤，优先于自动解析 |

**响应 `ScheduleResponse`：**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schedule_id` | string | 排程 id（供甘特图/重排引用） |
| `tasks` | `ScheduleTask[]` | 排程后的任务实例 |
| `metrics` | `ScheduleMetrics` | 多目标指标与性能 |
| `recipes` | object[] | 参与菜谱 |
| `devices` | object | 实际使用的设备配置 |
| `notifications` | `Notification[]` | 通知 |
| `generated_at` | string | 生成时间 |

### 6.1 ScheduleTask（任务实例，甘特图色块）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string | 形如 `<recipe_id>::<step_no>` |
| `recipe_id` / `recipe_name` | string | 所属菜 |
| `step_no` | integer | 步骤序号 |
| `title` | string | 步骤标题 |
| `device` / `resource` | string | 设备 / 资源名 |
| `resource_slot` | string | 槽位类名 |
| `slot` | integer | 占用槽位编号 |
| `is_preheat` / `preheat_minutes` | boolean / number | 预热标记与时长 |
| `start` / `end` / `duration` | number | 开始 / 结束 / 时长（相对 0 点，分钟） |
| `temperature` / `mode` | number/string | 温度 / 模式 |
| `needs_human` / `human_minutes` | boolean / number | 需人工 / 人工时长 |
| `unattended` | boolean | 是否无人值守 |
| `deps` | string[] | 前置任务 id |
| `is_human_slot` | boolean | 是否人工槽位 |
| `color` | string | 甘特色块颜色 |
| `status` | enum | `pending` / `running` / `done` / `frozen` |

### 6.2 ScheduleMetrics（L3 指标 + L5 性能）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `makespan` | number | 总工期 |
| `first_finish` / `last_finish` | number | 最早 / 最晚出锅 |
| `spread` | number | 出锅时间差 |
| `spread_grade` | string | 出锅差评级 |
| `parallel_degree` | number | 并行度 |
| `total_workload` | number | 总工作量 |
| `manual_total` | number | 人工总时长 |
| `manual_max_continuous` | number | 最长连续人工时长 |
| `manual_segments` | integer | 人工段数 |
| `human_utilization` | number | 人工利用率 |
| `cooling_penalty` | number | 变凉惩罚 |
| `lower_bound` | number | 理论下界 |
| `optimality_gap` | number | 相对下界 gap |
| `compression_ratio` | number | 并行压缩比 |
| `conflicts` | integer | 设备冲突数（须为 0） |
| `dependency_violations` | integer | 依赖违反数（须为 0） |
| `solver` | string | `sgs` / `cpsat` |
| `cpsat_status` | string | CP-SAT 状态（OPTIMAL/FEASIBLE…） |
| `cpsat_makespan_optimal` | boolean | 总工期是否证明最优 |
| `cpsat_objective` / `cpsat_bound` | number | CP-SAT 目标值 / 界 |
| `cpsat_wall_ms` | number | CP-SAT 求解耗时 |
| `sgs_score` / `cpsat_score` | number | 两轨统一评分（取高者） |
| `device_utilization` | object | 各设备利用率 |
| `device_timeline` | object | 各设备时间线 |
| `solve_ms` | number | 总求解耗时 |
| `iterations` | integer | 迭代次数 |

### 6.3 真实调用示例（3 道菜）

请求（`use_llm=false, use_cpsat=true`）后，服务端真实返回（schedule_id `sch-10512bd218`，16 个任务）核心指标：

| 指标 | 真实值 |
| --- | ---: |
| makespan 总工期 | 43.0 |
| 最早出锅 / 最晚出锅 | 41.0 / 43.0 |
| 出锅差 spread | 2.0（优秀） |
| 理论下界 lower_bound | 43.0 |
| optimality_gap | 0.0 |
| 冲突 / 依赖违反 | 0 / 0 |
| 求解器 / 状态 | cpsat / OPTIMAL |
| 总工作量 / 人工总时长 | 79.0 / 34.0 |
| 最长连续人工 | 19.1 |
| 求解耗时 solve_ms | 554.5 |

任务对象示例（结构真实）：

```json
{
  "task_id": "58e70ae1a3fd4a750f4b75af::1",
  "recipe_id": "58e70ae1a3fd4a750f4b75af",
  "recipe_name": "双椒鳙鱼头",
  "step_no": 1,
  "title": "洗净并沥干水分",
  "device": "人工",
  "resource": "人工(操作台)",
  "resource_slot": "human",
  "start": 5.0, "end": 7.0, "duration": 2.0,
  "needs_human": true, "human_minutes": 2.0,
  "unattended": false, "deps": [], "status": "pending"
}
```

---

## 7. L4 动态重排

`POST /api/replan`（`/api/reschedule` 为等价别名）。

**请求体 `ReplanRequest`：**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `tasks` | ScheduleTask[] | 是 | 当前排程任务 |
| `event` | `ReplanEvent` | 是 | 突发事件 |
| `devices` | object | 否 | 设备配置 |
| `model` | string/null | 否 | 模型 |
| `use_llm` | boolean | 否 | 默认 false |
| `align_finish` | boolean | 否 | 默认 true |
| `target_spread` | number | 否 | 默认 5 |
| `use_cpsat` | boolean | 否 | **false=最小扰动（默认，已有步骤尽量不动）；true=全局重优化（冻结已完成/进行中，其余 CP-SAT 求优）** |
| `cpsat_time_limit` | number | 否 | 重优化预算（秒），默认 5 |

`ReplanEvent` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | enum | `add_recipe` / `delay_task` / `device_failure` / `change_duration` / `advance_time` |
| `now` | number | 当前已执行到的时间点（分钟） |
| `recipe` | Recipe/null | 新增菜谱（add_recipe） |
| `recipe_ids` | string[] | 相关菜谱 |
| `task_id` | string/null | 受影响任务 |
| `device` | string/null | 故障设备（device_failure） |
| `delta` | number | 延迟/变动量（分钟） |
| `duration` | number/null | 新时长（change_duration） |
| `description` | string | 事件说明 |

**响应 `ReplanResponse`：**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schedule_id` | string | 新排程 id |
| `tasks` | ScheduleTask[] | 重排后任务 |
| `metrics` | ScheduleMetrics | 指标 |
| `recipes` | object[] | 菜谱 |
| `notifications` | Notification[] | 通知 |
| `diff` | `ReplanDiffItem[]` | 变更明细 |
| `frozen_count` | integer | 被冻结（已完成/进行中）任务数 |
| `added_tasks` | integer | 新增任务数 |
| `disturbance` | number | 扰动度（既有任务位移总量，越小越好） |
| `message` | string | 结果说明 |
| `elapsed_ms` | number | 重排耗时 |

`ReplanDiffItem`：`task_id`、`recipe_name`、`title`、`old_start`、`new_start`、`delta`、`reason`。

> 最小扰动模式保证已完成/进行中步骤不被推翻；全局重优化在冻结这些步骤后对剩余问题求优，OPTIMAL 即证明剩余部分最优。

## 8. L4 关键节点通知

`POST /api/notify`

**请求 `NotifyRequest`：** `tasks`（ScheduleTask[]）、`schedule_id`、`now`（默认 0）、`model`、`use_llm`（是否用 LLM 润色，默认 false）。

**响应 `NotifyResponse`：** `notifications[]`、`elapsed_ms`、`generated_by`。

`Notification` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 通知 id |
| `time` | number | 触发时刻（分钟，相对 0 点） |
| `kind` | enum | `prep` / `start` / `check` / `finish` / `warn` |
| `priority` | enum | `high` / `medium` / `low` |
| `recipe_id` / `recipe_name` | string | 关联菜 |
| `task_id` | string | 关联任务 |
| `title` / `message` | string | 标题 / 正文 |
| `action` | string | 建议操作 |
| `lead_minutes` | number | 提前量（分钟） |
| `generated_by` | string | rule / llm |

## 9. 甘特图

`GET /api/gantt`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `recipe_ids` | string | 逗号分隔菜谱 id |
| `schedule_id` | string | 或引用已生成排程 id |
| `view` | string | `device`=设备视图 / `dish`=菜品视图，默认 device |
| `format` | string | `svg` / `html`，默认 svg |
| `target_spread` | number | 默认 5 |
| `use_cpsat` | boolean | 默认 true |

默认返回可直接嵌入报告的 SVG。

## 10. L5 性能与统计

- `GET /api/metrics`：返回 `timings`（各阶段耗时）、`counters`（计数）、`last_schedule`。
- `POST /api/metrics/reset`：重置统计。
- `GET /api/stats`：菜谱库 + 设备 + 性能综合统计。

## 11. 模型与厂商（简要）

- `GET /api/models`：本地模型清单（`ModelsResponse`）。
- `GET /api/providers`：厂商、可选模型、密钥配置状态（**仅掩码**）。
- `POST /api/providers/config`：保存 `{provider, api_key, base_url}`；省略字段不改动，`""` 清除；响应仅掩码。
- `POST /api/providers/custom`：新增/更新自定义 OpenAI 兼容或 Anthropic 网关（`label/protocol/base_url/api_key/models/pid`）。
- `POST /api/llm/test`：`{model}` 极短请求验证连通。
- `POST /api/llm/select`：记住选中模型。
- 另含厂商自定义模型增删与 `/models/refresh` 在线拉取（见第 1.1 节）。

## 12. AI 对话（简要）

`POST /api/chat`，请求 `{message, session_id?, model?}`，响应：

```json
{ "session_id": "...", "reply": "...", "steps": [ { "tool": "...", "args": {}, "ok": true, "summary": "..." } ], "model": "...", "rounds": 2, "elapsed_ms": 1200 }
```

模型可自主调用搜菜谱、排程、重排、设备、指标等工具；`GET /api/chat/tools` 列出工具，`POST /api/chat/reset` 清空会话。

---

## 13. 端到端调用流程

1. `GET /api/recipes` 选定菜，记录 id；
2. `POST /api/parse` 结构化解析（命中缓存则毫秒返回、不调模型）；
3. `POST /api/schedule` 生成并行排程，查看 `metrics`（出锅差、下界、冲突）；
4. 执行中遇突发事件 → `POST /api/replan` 增量重排；
5. `POST /api/notify` 获取节点通知；`GET /api/gantt` 出图；
6. `GET /api/metrics` 查看 L5 性能。

## 14. cURL / Python 调用示例

```bash
# 排程
curl -X POST "http://112.124.50.217:8766/api/schedule" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"recipe_ids":["58e70ae1a3fd4a750f4b75af","58e70b0e9f6429675ec805d8"],"use_llm":false,"use_cpsat":true}'
```

```python
import requests
base = "http://112.124.50.217:8766"
s = requests.post(base + "/api/schedule", json={
    "recipe_ids": ["58e70ae1a3fd4a750f4b75af", "58e70b0e9f6429675ec805d8"],
    "use_llm": False, "use_cpsat": True}, timeout=30).json()
m = s["metrics"]
print(m["makespan"], m["spread"], m["solver"], m["cpsat_status"], m["conflicts"])
```

## 15. 对接验收检查单

- [ ] `/api/health` 正常；接口免令牌可直接访问。
- [ ] `/api/parse` 对已解析菜返回缓存命中、`llm_calls=0`。
- [ ] `/api/schedule` 返回 `schedule_id/tasks/metrics`，`conflicts=0`、`dependency_violations=0`。
- [ ] 出锅差 `spread` 与评级正确；`optimality_gap`、下界口径一致。
- [ ] `/api/replan` 已完成步骤被冻结（`frozen_count`），`disturbance` 合理、不推翻历史。
- [ ] `/api/notify` 通知含时间、菜名、操作三要素，提前量合理。
- [ ] `/api/gantt` 正常出图；`/api/metrics` 耗时统计可查。
- [ ] 全部响应为合法 JSON，无 Markdown、无明文密钥。

---

*本文档路径、字段、枚举均取自 `http://112.124.50.217:8766/openapi.json`，示例数值为 2026-10-09 真实调用结果；如服务更新，以在线 `/docs` 与 `/openapi.json` 为准。*
