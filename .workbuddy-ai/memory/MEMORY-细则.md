# 项目细则备忘 —— 界面层与 LLM 接入（ft）

> `MEMORY.md` 的配套文件。**改 `app/web/index.html` / `app/agent/llm.py` / `app/api/main.py` 前先读这里。**
> MEMORY.md 只留最高频的不变量（受注入长度限制），完整清单在本文件。

## A. 界面层（`app/web/index.html`）

护栏 `tests/test_web_ui.py`(31) + `tests/test_product_fixes.py`；探针 `scripts/ui_probe.mjs`(jsdom,140)
+ `scripts/ui_layout_probe.mjs`(真 Edge + CDP,114/112)；变异 `scripts/_mutate_web_guards.py`(71)。
逐条细则另见 `docs/界面交互体检.md`。

### A1 站点构成（5 个）

首页 / 排程 / 菜谱 / **设置** / 历史，外加右下角悬浮球「AI 对话」（`.fab` + `#floatPanel`）。
原「设备」站点已**并入设置**作为子页签，`#v-devices` 不存在。

⚠ **增删视图必须同步四处**，漏一处会出现"探针能过、点开是空白"的静默故障：

| 位置 | 内容 |
|---|---|
| `app/web/index.html` 的 `.menu` | `show('settings',this)` 等入口按钮 |
| `scripts/ui_probe.mjs` | `const views = [...]` + 「菜单 N 个入口」断言 |
| `scripts/ui_layout_probe.mjs` | `const VIEWS = [...]` |
| `tests/test_web_ui.py` | `test_settings_site_replaces_the_device_site` |

### A2 设置页

`.subtabs#setTabs` 内三个 `.stab[data-tab]`（模型接入 / 厨房设备 / 访问令牌）→
`.spanel#sp-model` / `#sp-devices` / `#sp-token`。

- 切换**只靠 `.active` 类**（`setTab()` 同时管 `.stab` 与 `.spanel`）。写 `style="display:..."` 的话，
  静态护栏与探针都只能"看见"默认那一屏，改坏了没人知道。
- **保存路径一律不许 `alert`**，只用 `_setLog(id, msg, cls)` 写页内 `.set-log`。
- 保存失败**判响应体里的 `error` 字段**，不判 HTTP 码 —— 后端非法配置返回 **200 + `error`**。
- `loadDevices()` 取 `/api/device_spec` 时 `catch{spec={}}` 兜底：容量在 `/api/devices`、
  能力在 `/api/device_spec`，是两个来源，能力拿不到也不能让容量编辑框失效。
- 设备卡必须同时给出**容量数字**与**能力行**（模式 chip），只给数字用户不知道"蒸箱 1 台"能干什么。

### A3 模型接入（前端侧）

- 必须显示 `MODEL_SOURCE`（`环境变量` / `设置文件` / `内置默认`）——"改了没生效"最常见的原因就是被环境变量钉死。
- `llm.model_locked()` 返回非空时禁用**模型输入框**，`llm.provider_locked()` 返回非空时禁用
  **接入方式下拉**（两者独立：钉了 provider 不该把"同一档内换模型"也一起废掉）。
- 保存成功后**从 `/api/model_settings` 重新拉一次**回填，不要只信本地状态。

**接入方式切换（2026-10-03 新增）**

- 控件：`#setProviderSel`（选项来自后端 `providers`，**前端不自己写一份**）、
  `#setRemoteUrl`、`#setRemoteKey`（`type=password`）、`#setModelSel`、`#setModelInput`。
  `#setProvider` / `#setBaseUrl` 是**只读的"当前生效"回显**（探针断言 `#setProvider` 值匹配 `/本地|远端/`）。
- 显隐**靠类**：`#sp-model.mode-local .remote-only{display:none}` /
  `#sp-model.mode-remote .local-only{display:none}`。
  ⚠ 选择器**必须带 `#sp-model`**（(1,2,0)）—— 只写 `.set-field.remote-only` 是 (0,2,0)，
  与 `.set-field{display:block}` 同权重，靠源序才压得住，挪一行就静默失效。
  两个远程字段**始终留在 DOM 里**（只 `display:none`）：探针按 id 取值，摘掉会让"取不到"和"值为空"变成同一个现象。
- ⚠ **密钥框永不回填**（`rk.value=''`），`saveModel()` 里 **空串不进 payload**
  （后端把空读作"保持原样"；无条件送空串 = 每保存一次抹一次配置）。
  **本地档不送远程字段**，否则切回本地会把远程配置一起改写。
- `saveModel()` 成功文案**按档分流**：`verified` 只对本地有意义，远程档报的是
  "主机名 · 密钥已配/未配"。不分档会把正常的远程切换显示成"⚠ 调用会返回 404"。
- `testModel()` 的失败提示也要分档 —— 远程档下让用户"确认 Ollama 已启动"是纯误导。
- 远程档不渲染"本机模型数"（`installed_models()` 本来就返回"当前 provider 不是 ollama"，
  当成 ⚠ 挂出来会把配置良好的远程接入显示成出错）。
- ⚠ **只拨下拉不改后端配置**（必须点保存才生效）——探针有专门一条守它。
- 相关护栏：`test_web_ui.py` 的 5 条 `provider_*` / `remote_api_key_*` / `blank_remote_*` /
  `save_result_wording_*`；变异锚点 7 条。

### A4 悬浮气泡尺寸

`--fab-size:60px` / `--fab-font:26px` / `--fab-inset*` / `--fab-gap` **只准在 `:root` 定义一次**。
基础 `.fab` 与所有断点（排程页 / 手机 / 超窄屏 / 矮横屏）**只准引用变量改位置**，
不许重写 `width` / `height` / `font-size`（曾 60/48/52/44 四档不一致）。
留白一律 `calc(var(--fab-size) + var(--fab-inset-plan) + …)`。
护栏：`test_fab_size_is_defined_exactly_once`。

### A5 选择态

- `#recipeList .item` 必须带 `data-name`。
- `agentPlan()` **直接从 `d.picked` 落选中态**，不能遍历 DOM 再 `click()` ——
  列表只渲染前 80 行，筛选态下识别到的菜可能不在其中 ⇒ 静默漏选。
- `selected.id` **一律规范成菜名**（`META` 原始对象**没有 `id` 字段**）。

### A6 请求与失败态

- 全文件只允许 **1 处** `fetch(`，在 `apiFetch` 内。
- **reject** = 后端不可达 ⇒ 弹 `#offlineBar` + 抛错。
- **HTTP 4xx/5xx** = 后端**是活的** ⇒ 原样返回 `r`（交给调用方判）。
- `apiFetch` 体内**不许有 `r.ok`**。
- **每个视图都要有失败态**。
- 详情未命中返 **404** ⇒ 专属文案 + 空对象兜底；模板**不许有裸 `${d.xxx}`**；写操作后 `META=[]`。
- 令牌只从 `?token=` / `localStorage` 取，**绝不硬编码**。`AUTH_TOKEN` 是 `localStorage` 的
  **内存镜像**，写完存储之后**必须从存储读回**（隐私模式下写失败但内存里是新的 ⇒ 刷新后行为不一致）；
  存储不可用则只留内存并提示。
- **401 走 `markAuthRequired()`，绝不能走 `markBackendDown()`**。

### A7 全局样式泄漏（最容易静默出事的一条）

全局有 `button{color:#fff; width:100%; box-shadow:橙色}`。任何新组件都必须**显式**写
`color` / `box-shadow` / `:hover` / `width:auto` / `min-width`。

特异性账（`.vbtn` 曾白字压白底，对比度 1.02）：

| 选择器 | 特异性 |
|---|---|
| `button:hover` | (0,1,1) |
| `.stab` / `.vbtn` | (0,1,0) |
| `.stab:hover` / `.stab.active` | (0,2,0) |
| `.stab.active:hover` | (0,3,0) |

⇒ `.stab.active` 与 `.stab:hover` **同为 (0,2,0)，靠源序压不住**，**必须补 `.stab.active:hover`**（`.vbtn` 同理）。

`<input type=range>` 必须显式换皮：`appearance:none` + **同时**自绘
`::-webkit-slider-runnable-track` 与 `::-webkit-slider-thumb`，再加 `-moz-*` 回退
（只给一个会剩半个控件）。播放条三者（滑块值 / `--p` 填充 / 读数）同步**只准走 `syncPlaybar(v)`**。

### A8 几何

- `.sbar` 自身 `padding` 必须为 0（宽度是按时间比例的百分比，极短任务仅 ~5px，带内边距会被浏览器兜底抬到 16px ⇒ 末端条越出轨道）；内边距与省略号放内部 `<span>`。
- 甘特条直接 `height:44px`，**不能用 `::after` 撑 —— `.sbar` 是 `overflow:hidden`**。
- ⚠ **`t._idx` 必须在画甘特图之前赋值**。
- `#v-plan.active{height:calc(100vh - var(--chrome))}`，`--chrome` 由 `fitChrome()` **实测**写入，
  且**必须挂 `ResizeObserver`**（按钮有 `transition`，采样会拿到过渡中间态）。
- `gap` 必须写 `#v-plan .grid3>div:not(.card)` —— 中列卡片本身就是 `.grid3` 的 `div` 子元素，
  漏掉会让卡片内部多出 7×14=98px 行距，吃光甘特图余量。
- **泳道按 `unit_id` 分组**；`.swim{overflow:clip}`（写 `hidden` 会让 `.swim` 自己变滚动容器，sticky 被锁死）；
  `nowline` 与泳道条必须同在 `.gantt-inner` 里。
- `seekT()` 空值早退（时间轴/播放按钮在计划页始终可见，用户可"先拖再排"）。
- HTML 内联 `on*` 里的每个函数都必须有定义（`onEventChange` 曾只被调用从未定义）。
- 拼 innerHTML 前**必须过 `esc()`**。

### A9 设置页的写操作：两套失败契约，必须同时判

后端有**两套**失败契约，缺一条就会把失败显示成成功：

| 场景 | HTTP | 响应体 |
|---|---|---|
| 业务校验失败（设备数量超范围、模型名非法…） | **200** | `{"error": "...", "details": [...]}` |
| 请求没被路由接受（Pydantic 参数校验） | **422** | `{"detail": [{"type","loc","msg"}, ...]}` |
| `HTTPException` | 400 | `{"detail": "中文原因"}` |

实测：给 `/api/devices` 发一个**数组体**，拿到 `422 {"detail":[...]}`，而旧代码只判
`d.error`（undefined）⇒ 一路走到"设备配置已保存，下次排程生效"。

**规则**：

1. `saveDevices` / `saveModel` 必须**同时**判 `r.ok` **和** `d.error`。
2. 错误文案统一走 `_httpMsg(d, r)` —— `detail` 可能是字符串，也可能是**对象数组**，
   直接 `'失败：'+(d.detail||...)` 遇到数组会拼成 `[object Object]`。
3. `saveDevices` 的数量框**留空必须拦住**：JS 里 `+'' === 0`，而 **0 在设备配置里
   是合法值**（= 该设备不可用）。不拦的话"手滑清空一格再点保存"会静默把整台设备
   下线，相关步骤全部改走顶替/人工，排程结果大变而界面只回一句"已保存"。
   （浏览器把 `1e`、`-` 这类半截输入也归成空串，所以这条拦得住真实手滑。）
4. **设备数量的上下限只有一个真源**：`app/api/main.py::_DEVICE_LIMITS`
   （灶眼数 0–8 / 蒸箱·烤箱·冰箱 0–4）。由 `/api/device_spec` 下发
   `qty_min` / `qty_max`，前端**不许**写死 —— 曾给所有设备写 `max="8"`，
   而蒸箱上限是 4：用户在界面上填 8 是合法操作，提交才被后端拒。

### A10 探针铁律

- 排版必须**真内核量**且**看截图** —— 光读 CSS 会得出错误结论；**数值全绿 ≠ 界面没问题**。
- **一步失败抛异常会吞掉后面所有检查**。
- **页面没真加载时探针会"全绿"**（量的空白页）⇒ 已加硬停 `exit(2)`。
  识别法：第 2 节 7 个视图全部"可见交互元素 1 个"且无差异 = 没量到真页面；**换第二档视口对照是发现它的手段**。
- `ui_layout_probe.mjs` **必须非沙箱**（沙箱内 `curl 127.0.0.1` 首次 200、随后 502；后台 uvicorn 会随 Bash 调用结束被回收）。
- 断言前**必须 `seekT(0)`**。
- **跑 ui 探针必须钉离线档**：否则本地 Ollama 介入，`/api/reschedule` 实测 **21039.8ms** ⇒ 探针大面积超时假失败。
- **起服务、跑探针、kill 必须在同一次 shell 调用里**（`(uvicorn &)` 起的进程不跨 shell 存活）。
- ESM `import "jsdom"` **不认 `NODE_PATH`** ⇒ 用 `createRequire(<workspace>/noop.js)` 指解析根。
- 页面 `<script>` 在 **JSDOM 构造期同步执行** ⇒ `fetch`/`alert`/`AudioContext`/`scrollTo` 必须在 **`beforeParse`** 补桩。
- 真浏览器（Windows 自带 Edge + Node 22 内置 `WebSocket` 手搓 CDP）**必须 `Network.setCacheDisabled`**，
  否则命中磁盘缓存，症状是"改了跟没改一样"。
- **有 `transition` 时读样式前必须 `sleep(500)`**（否则拿到过渡起始值 ⇒ 假通过）；hover 检查要额外断言
  `[...document.querySelectorAll(':hover')].includes(el)`。
- CSS 断言要查**声明级**（`(?:^|;)color:`）而不是子串（`"color:" in css` 会被 `transition:...,color .2s ease` 蒙混）。
- 半透明毛玻璃算对比度必须**逐层 alpha 合成**再比。
- 变异串过期会变成 0 次并打 `[跳过]` —— **跳过 = 那条护栏已经没人验了**。

**钉离线档的量化证据（v3.3 实测，同一份页面）**：
`ui_probe.mjs` 在 `FT_LLM` 默认档 **136 通过 / 11 失败**，`FT_LLM=off` **149 通过 / 0 失败**。
11 条失败全是「生成排程：指标卡渲染 0 张卡 / 甘特图 0 条泳道 / 任务明细 0 行 …」，
看着像"排程页彻底坏了"，实际是 **LLM 闭环抢走了本轮预算**（已知的"进程启动后第一次请求必中招"）。
直接 `curl /schedule` 复核：`plan` 里 30 个任务、`kpi`/`insight` 齐全 —— **后端是好的**。
> 教训：**探针的失败要先证伪"是探针环境还是产品缺陷"**。最省事的一步是换档复跑
> （这里 `FT_LLM=off` 一步就分清了），不要一上来就去改产品代码。

### A11 排程页的 flex 余量：新块必须 `flex:none` + `max-height`

`#v-plan #ganttWrap{flex:1;min-height:0;display:flex;flex-direction:column}`，
里面是 `tabs` / `#prepBanner` / `#gantt{flex:1}`（原 `.riskpanel` 已随诊断面板撤除）。
**`#gantt` 拿走"剩下的全部"** —— 任何新加的兄弟块只要参与抢余量，甘特图就会被压扁。

- **桌面断点没有兜底**：`#v-plan` 是固定高度 `calc(100vh - --chrome)`，
  而 `#gantt{min-height:300px/240px}` **只存在于 641~1024px 与 ≤640px 两个断点**。
  所以桌面下挤压是**静默**的：jsdom 探针全绿、pytest 全绿，只有**真 Edge 排版探针**会红。
- **实测**：风险面板有 5 条内容时甘特图 **35px**（`tasks` 是 104px），
  探针报 `时间轴高度显著大于任务明细 gantt=35 tasks=104`。
- **修法**：`flex:none`（不抢余量，让 `cardFill.slack` 仍为 0）+ `max-height` + `overflow-y:auto`。
- **判定性实验**（区分"上限在起作用"和"碰巧没超"）：`max-height` 110px → 10px，
  甘特图 152px → **252px**，正好差 100px。
- **护栏**（2026-10-08 已随面板删除）：`test_风险面板不抢甘特图的高度` 曾钉
  `flex:none`/`max-height`/`overflow-y:auto` 三条，并**先断言 `#riskPanel` 与 `#gantt`
  同处 `#ganttWrap`**（护栏前提变了就该重写，铁律⑤）。
  **将来把诊断面板接回来，这三条必须一并接回。**
- **A/B 取证法**：`.cache/pack-smoke/app/web/index.html` 里留着一份改动前的页面快照，
  把它拷进 `app/web/` 跑同一条探针再还原（sha256 核对）—— 用来回答
  "这条失败是我引入的还是本来就有的"。本轮正是靠它把 12 条里的 11 条判成既有、
  把 1 条判成本轮引入。

---

### A12 "模板不许有裸 `${d.xxx}`" 必须**按函数体**判

用 `src.index("}")` / `re.search(r"function f\(\)\{(.*?)\n\}")` 这类**只取第一段**的做法，
遇到函数体里有独立成行的 `}` 就会截断 ⇒ 后面的断言全部落空 ⇒ **护栏永远为真**（铁律②）。

- 合法写法（不该报）：`${d.locked||0}`、纯文本插值、插本地数组。
- 判据要落在**函数体内部**的模板字面量上，且必须先 `_strip_js_comments()`
  （注释里出现的 `${d.xxx}` 会造成**假绿**）。
- 新写的护栏用**大括号配平**取函数体，不要用 `\n}` 截断。

### A13 界面 v3.5 五项改动（2026-10-08）

用户原始指令：`把这个插单功能做好；排程诊断可以不用；耗时改成秒；`
`这里ai排输入后自动打开右下角的ai对话窗口然后输入；选择厨房功能完善；`

**① 移除排程诊断面板** —— 删 HTML `#riskPanel`、CSS `.riskpanel/.riskhead/.riskitem/.cpbar`、
JS `renderInsight()` 及其调用点与防御分支 `document.getElementById('riskPanel')`。
**后端 `insight` 字段刻意保留**：`/schedule` 仍回、`tests/test_insight.py` 仍钉
`test_schedule端点带诊断字段`；`/api/plan` 保持纯官方 schema。
`scripts/_mutate_web_guards.py` 里对应 6 条变异条目**必须同步删除** ——
忘了删的后果不是报错，而是 6 条全部 `[跳过] 原串在文件里出现 0 次`，
汇总从 101 变 95，**看起来像漏网**。

**② 耗时改秒** —— 后端各处回的 `elapsed_ms` 一律是**毫秒**（`round(x*1000,1)`）。
新增**唯一换算口径** `fmtSec(ms)`（`Number.isFinite` 兜底回 `—`，`(n/1000).toFixed(1)`），
两处调用点：顶部统计卡（`<span>耗时(秒)</span>`）与 `reschedSummary()`。
护栏 `test_耗时按秒显示且只有一个换算口径` 同时钉"有 `fmtSec`"与"**全文件无 `耗时(ms)`**"。

**③ 「AI排」交给右下角对话窗口** —— 原 `agentPlan()` 直连 `/api/agent_plan`，
用户要的是"输入后自动打开右下角 AI 对话窗口然后输入"。改为同步函数：
读 `#agentText` → 清空 → `toggleFloat(true)` → 写 `#chatInput2` → `sendChat('chatBox2','chatInput2')`。
⚠ **必须真的发送**：只填不发会让人以为按钮没生效。
⚠ `#chatInput2` 取不到时要 `alert` 而不是静默返回。
`/api/agent_plan` 端点**后端保留**（探针钉"不再直连"）。`agentPlan` 由 `async` 变同步 ⇒
`tests/test_web_ui.py` 里取函数体的辅助函数要传 `async_=False`。

**④ 插单（加一道菜）** —— 四件事：
- 新增 `#insertPick` 下拉（"随机挑一道没选的菜" + 候选菜），**留空才随机**；
  `loadInsertPool()` 拉一次缓存、`fillInsertPool()` 按 `selected` 过滤（已插的从候选消失），
  `updSel()` 末尾调 `fillInsertPool()` ⇒ 候选池永远跟着**唯一真源 `selected`** 走。
- `onEventChange()` 同步 `#insertPick` 显隐（只有 `insert` 档显示）。
- `triggerEvent()`：按钮**置忙**（`disabled` + `重排中…`），`finally` 里**必然恢复**
  （上面三条 `return` 都会跳过函数尾部，漏一个按钮就永远停在忙态）。
- **两条失败路径**都要判：`!r.ok`（HTTP）与 `d.error`（业务错误体）。
  原实现直接取 `d.kpi.conflict_count` ⇒ `kpi` 缺失时抛 `TypeError` 被 catch 吞成
  "无法连接后端服务"，把"后端回了畸形体"误报成"网络不通"。
- 成功后 `selected=selected.concat([...])` + `updSel()` + `loadRecipes()` + `fillInsertPool()`，
  **不套 5 道上限**（插单后 6 道也要如实显示）。
- `reschedSummary()` 用上后端已有的 `stance`（含 `by_llm` 区分"模型裁定/规则兜底"）、
  `pareto['保证']`（两项 ✓/✗）、`notice['新总时长']`。

**⑤ 选择厨房** —— 前端 + 后端两侧：
- 前端：选项带设备数；`loadPresets()` 反查**生效预设**用
  `DEVICE_KEYS.every(x=>(PRESETS[k][x]??null)===cur[x])`（**逐项全等**，不是只比灶眼数）；
  失败时**回退** `sel.value=KITCHEN_APPLIED`（下拉不能说谎：显示新预设、跑旧设备）；
  提交**只带 `DEVICE_KEYS`**（`Object.assign(cfg,p)` 会把预设名等无关键也发上去）。
- ⚠ **同名 JS 函数后定义覆盖先定义**：本轮 `applyKitchen` 曾同时存在两份（旧的
  `.catch(()=>{})` 吞失败、只提交 3 个键漏了冰箱），新护栏的 regex 匹配到**第一份（旧的）**
  ⇒ 2 条护栏假红。**改前先 `grep -c "function <name>"`。**
- 后端：`data/devices_user.json` 用户层（`FT_DEVICES_FILE` 可改路径，测试隔离用）。
  三纪律：① **原子写**（`os.replace`）；② 读回时过 `_coerce_device_cfg` **校验，非法值忽略**
  而不是灌进引擎；③ 损坏/缺失**不致命**（当空继续）。
  写盘后还要 `_apply_device_capacity(clean)` 把台数同步进 `engine.RESOURCE_UNITS`。
- `sync_publish.SKIP_NAMES = {"llm_settings.json", "devices_user.json"}`
  ⇒ 开发机的厨房配置**不进发布包**；护栏 `test_本机状态文件都不进发布包` 钉两条。
  ⚠ 与"出厂层"分清：`data/device_presets.json` 是**产品数据**、**必须随包走**。

### A14 两处「恢复默认」（2026-10-08 追加）

用户指令：`动态事件的重排模块添加一个恢复默认的按钮，点击后恢复默认；`
`选择厨房场景也同理，点击按钮恢复默认`。

两处语义**刻意不同**，都按各自模块"默认"的**唯一真源**来定：

**① 动态事件 · 重排 → `resetEvent()`（纯前端）**
- 复位 `#eventType` 到 `EVENT_DEFAULT='insert'`（**显式常量**，不读 `sel.options[0]`：
  读 DOM 的话将来调整选项顺序，"默认档"会**静默**跟着变，而文案/探针/后端的约定不会）。
- 清 `#insertPick` + 走 `onEventChange()`（顺带清掉上一次的结果摘要）。
- **刻意不自动重排**：`failed_device` 是 `/reschedule` 的**请求体字段**，按请求生效、
  不会粘在进程里 ⇒ 没有需要清的服务端状态；而重排要跑 5~20s，用户点"恢复默认"
  不会预期按钮转二十秒。计划仍是上一次重排的结果 ⇒ **文案必须写清**，否则用户
  以为计划也回去了。
- ⚠ 退回的档位必须是 `#eventType` 里**真实存在**的 value：给 `select.value` 赋一个
  不存在的值会**静默变成空串**，而后端 `{stove,steamer,oven}['']` 取到 `undefined`
  ⇒ "恢复默认"变成"用 undefined 设备做一次故障重排"。护栏钉了这层成员检查。

**② 选择厨房 → `resetKitchen()` → `POST /api/devices/reset`（后端）**
- 后端：`_DEVICE_CFG` / `engine.RESOURCE_UNITS` 拨回 `_DEFAULT_DEVICE_CFG`
  （**唯一真源**，与 `engine.RESOURCE_UNITS` 初值、`device_presets.json` 的
  「标准家庭厨房」三者必须同源），**并删掉用户层文件**。
  删除失败（占用/只读）**只告警不抛** —— 内存与引擎已经回到默认，对用户是成功的。
- 前端**不写死默认值**：抄一份的话后端改默认值前端不会跟着变，且没有任何报错。
- 前端**不能图省事走 `POST /api/devices`** 把默认值发上去：那会把"默认值"当成一次
  用户选择**落盘**，以后后端改默认，这台机器会顽固地停在旧默认上。
- 重置后必须重新 `loadPresets()` 反查生效预设，否则下拉停在已失效的那一项。

**布局坑（真 Edge 排版探针抓到，静态测试看不见）**：两处按钮都放在**右栏**，
667×375（iPhone 8 横屏、3 列布局）下右栏只有一百来像素。
`display:flex;gap:8px` 里放两个按钮**一定会**把后一个捅出视口
（实测 `#eventResetBtn@670`，视口宽 667）。
修法：容器加 `flex-wrap:wrap` + 第一个按钮 `min-width:0`，窄屏下自动落到第二行。
⚠ 光加 `min-width:0` 不够 —— 那只会把「应用重排」挤成一条缝。

---

### A15 顶栏「穿模」：sticky 的流内位置 ≠ 渲染位置（2026-10-08 第三轮）

用户原话：`智能排程界面的ui与其他站点不一致，上方的模块和导航栏穿模了`

#### A15.1 根因

`.nav` 是 `position:sticky`，而 **`top` 只挪渲染位置、不占文档流空间**：

| | 渲染位置 | 文档流里占的位置 |
|---|---|---|
| `.nav`（只写 `top:14px` 时） | `14..65` | **`0..51`** |

⇒ 紧跟其后的 `#v-plan` 从 **51** 开始，**首行模块顶部 14px 永远压在顶栏底下**。

**为什么只有排程页明显**（这是"碰巧"而不是"保证"）：
- `.view{padding-top:28px}` → `28 > 14` **侥幸**躲开；
- `#v-plan{padding:10px}` → `10 < 14` ⇒ 被压 4px + 顶栏投影 = 用户看到的"穿模"。

#### A15.2 修法：两个唯一真源变量

| 变量 | 值 | 用法 | 为什么必须这样 |
|---|---|---|---|
| `--nav-top` | `calc(14px + var(--sa-t))` | `.nav` 的 `top` **与** `margin-top` | `margin-top` 让**流内位置 = 渲染位置** ⇒ `--chrome`(= 顶栏下沿 65px) 与文档流自洽，谁都不可能再被压住 |
| `--page-top-gap` | `14px` | `.view` 与 `#v-plan.active` 的 `padding-top`（**全站唯一**） | 排程页原来自己写 10px = 第二个真源；统一后"内容永远在顶栏之下"由结构保证 |

⚠ 各断点（640 / 360 / 横屏）**只覆盖变量本身**，`top` / `margin-top` / `padding` 一起跟着走，
不会出现"半截改动"。改 `--nav-top` 时 `--page-top-gap` 必须一起调，否则各站点首行间距不一致
（实测手机端漏改 ⇒ 探针报 `plan=8 recipes=16` 假失败）。

#### A15.3 矮视口下三列仪表盘整体崩坏（同源缺陷）

仪表盘成立的前提是四项都塞得进视口：

```
右列最小内容 606 + 顶栏 65 + 底部留白 78 + 顶部间距 14  ⇒  视口高 ≳ 763px
```

再矮就**必然**同时出现三件事（实测）：

| 视口 | 症状 |
|---|---|
| `1084×570` | `ganttH=0`（时间轴白给）· `docH=667>570` |
| `1280×720` | `ganttH=132` · 右列溢出 39px · 悬浮球压住「生成排程」 |
| `1088×557`（用户截图） | 14 项失败，含球遮挡 `#kitchenResetBtn` |

修法：`@media (min-width:1025px) and (max-height:760px)` **换布局档**（不是继续压中列）：

```css
#v-plan.active{height:auto;min-height:calc(100vh - var(--chrome))}
#v-plan .grid3{flex:none;align-items:start}   /* ← 同时是探针判档的唯一信号 */
#v-plan #gantt{min-height:300px}
#v-plan #tasks{max-height:260px;overflow:auto} /* 长页档里明细仍收窄，主输出才保住主角 */
```

⚠ 阈值 **760 是算出来的不是拍的**：763 是临界，`1366×768` 这类常见笔记本
（768 > 760）仍留在仪表盘档。⚠ 首版把 `#tasks{max-height:none}` 写进去 ⇒ 页面被拉到
1912px（`docH=1912/720`），改成 260px 后收敛到 1088px。

另修 `.float-panel` 写死 `height:520px`：底部锚点 + 固定高度 = **顶部**溢出，
矮视口下用户连输入框都看不到（`1084×570` / `667×375` 实测）⇒
`height:min(520px, calc(100vh - var(--fab-inset) - var(--fab-size) - var(--fab-gap) - var(--sa-b) - 12px))`。

#### A15.4 探针侧：三档分支 + 判档信号

| 档 | 触发 | 硬要求 | 实测条数 |
|---|---|---|---|
| 桌面仪表盘 | 宽 >1024 且高 ≥761 | **页面不出滚动条** | 119 |
| 矮桌面长页 | 宽 >1024 且高 ≤760 | **容得下自己的内容**（可滚动） | 117 |
| 堆叠 | `colTracks !== 3` | 单列长页 | 117 |

⚠ **探针不许自己再抄一份断点阈值** —— 判档取**实测值** `#v-plan .grid3` 的
`align-items`（`stretch`=仪表盘、`start`=长页）。这个属性同时被 CSS 与探针使用，
是**唯一信号**；两边各写一份阈值迟早漂移。

⚠ `VP_LIST` **必须覆盖"宽 >1024 且高不到 800"**这一格。原表 11 个视口里有
`1024×768` / `844×390` / `667×375`，**恰好漏掉** `1280×720` / `1366×768` /
窗口未最大化的窄桌面 —— 上面那条崩坏一路没被发现就因为它落在扫描表空档里。
现为 **14 个**视口。

⚠ 逐视口扫描前必须 `window.scrollTo(0,0)`：3c 节局部截图用过 `scrollIntoView`，
桌面尺寸文档不够高（`scrollY` 恒 0）所以从没暴露，切到窄屏后残留生效 ⇒
`320×568` 量到"首行顶=-563"假失败。

⚠ `#v-home` 是满幅落地页（`style="padding:0"` + `.hero` 内容底对齐），
**首行间距 0 是有意的** ⇒ "各站点间距一致"的比对必须排除它。

#### A15.5 本轮踩的坑

1. **护栏断言被 CSS 注释兜住**（铁律②的 CSS 版）：长页档里
   `#v-plan .grid3{flex:none;align-items:start}` **上方**那段解释性注释同时写着
   `flex:none` 与 `align-items:start` ⇒ 删掉真声明后 `"align-items:start" in body`
   **照样为真**。变异自检实测报
   `[BAD] 长页档丢掉 align-items:start  变异后被捕获=False`。
   修法：新增 `tests/test_web_ui.py::_strip_css_comments()`，凡"从一块 CSS 里找某条
   声明"的断言都必须先过它。
2. **改 CSS 会批量撞掉既有变异锚点**：`padding:10px` → `var(--page-top-gap)`、
   `top:calc(14px + …)` → `var(--nav-top)` 一次性让 **4 条**旧锚点从 1 次变 0 次
   （全被标 `[跳过]`）。⇒ **改一条被锚点引用的声明，必须同一次改掉锚点**；
   改完跑"锚点唯一性预检"（一次性 `html.count(old)==1` × 全部条目）。
3. `_media_body()` 必须**按花括号配平**切块 —— `[^}]*` 会在第一条规则的 `}` 处停住，
   拿到半截内容（子串断言的老坑）。

#### A15.6 探针运行规矩（本轮定下）

jsdom 探针**不必**打断用户正在用的 8137 服务 —— `ui_probe.mjs` 支持 `BASE=` 覆盖，
起临时离线服务在**另一个端口**，起服务/跑探针/kill **同一次 shell**：

```bash
FT_AGENT_LLM=off FT_LLM=off FT_WARM_LLM=off FT_NOTIFY_LLM=off \
  FT_DEVICES_FILE="$PWD/.cache/_devices_probe.json" \
  ./.venv/Scripts/python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8138 --workers 1 &
BASE=http://127.0.0.1:8138 node scripts/ui_probe.mjs
```

⚠ 必须用 `FT_DEVICES_FILE` 把设备写操作引到 `.cache/`，否则探针会在开发机上留下
`data/devices_user.json`（实测隔离后 `data/` 无新文件、`data/recipes_user.json` 未变）。

---

### A16 逐控件可用性审计（2026-10-08 第四轮）—— "点下去有没有反应"

**规模盘点**：`index.html` 2743 行 · **47** 处 `onclick` · **77** 个函数 · **252** 个可交互控件。
静态侧已确认：每个 `onclick` 都有定义（0 未定义）· 0 个重复定义 · 0 个死函数 ·
0 个死按钮（每个处理器都有可观察副作用）· 8 个未被 JS 引用的 id 里 7 个是 CSS 锚点，
**只有 `#dinnerTime` 是真摆设**。

#### A16.1 新增探针 `scripts/ui_click_audit.mjs`（穷举层，与 `ui_probe.mjs` 分工不同）

- `ui_probe.mjs` = **场景化**，断言"内容对不对"；本脚本 = **穷举式**，只问"点下去之后
  DOM / 网络 / alert / 焦点 **有没有任何一处变化**"。
- 判定"有反应"的四选一口径 + `MutationObserver`：
  `mut>before.mut`（DOM）· `api>before.api`（请求）· `al>before.al`（alert）· `fo!==before.fo`（焦点）。
- **必须先跑 `ui_probe.mjs` 再跑本脚本**（穷举层会切「选择厨房」预设、真的落盘）。
- 收尾把设备配置写回进入时的样子（`POST /api/devices` 回 `devBefore`）。
- 退出码 `unknown.length || kbGap.length ? 1 : 0`。
- 实测 **241 有反应 / 0 待判断 / 5 合理无反应 / 6 跳过 · 回车缺口 0 · JS 异常 0**。

**5 条"合理无反应"单列（`knownNoop()`，不是白名单，每条写明理由）**：

| 控件 | 为什么无反应是合理的 |
| --- | --- |
| `#hero` `onmousemove` | 绑的是装饰性视差，用 `click` 探它本就不该有反应 |
| `#setModelSel` `onchange` | 只改 `.value` **属性**；MutationObserver 不观察 property |
| `✕` `toggleFloat(false)` | 面板已被前一个控件关掉，再关一次自然无变化 |
| 「发送」`sendChat(` | 输入框为空时早退，是**正确**行为（空消息不该发） |
| `#chatInput2` | 交互绑在 `onkeydown` 上，用 `click` 探它天然无反应（回车遍历已覆盖） |

#### A16.2 缺口 C1：回车缺口 13 → 0

**现象**：全站唯一带回车处理的输入框原来是 `#chatInput2`，其余十几个单行框
敲回车**毫无反应** —— 用户第一反应是"卡住了"。

**修法**：给 9 个"填完要按保存"的框补 `onkeydown="if(event.key==='Enter')<旁边那个按钮的动作>"`：
`#agentText`→`agentPlan()` · `#search`→`loadRecipes()` · `#libSearch`→`loadLib(this.value)` ·
`#setRemoteUrl`/`#setRemoteKey`/`#setModelInput`→`saveModel()` · `#setToken`→`saveToken()` ·
`.dev-num`→`saveDevices()` · `#rfName`→`saveRecipe(...)`。

⚠ **判缺口看"有没有绑 keydown"，不是"这次有没有反应"** —— 保存类空输入早退是**正确**行为，
按"有没有反应"判会让 `#chatInput2` 永远报红（探针没往框里填字）。改判后 13 → 0。
⚠ **回车接错动作比没接更危险**：`#setToken` 若接成 `saveModel()`，界面还回"已保存"，
但令牌根本没存。护栏 `test_关键输入框回车提交到正确的动作` 钉住映射表。

#### A16.3 缺口 C2：`#dinnerTime` 是摆设 → 墙上时钟锚点

**现象**：HTML 有、样式有、用户能改，但**全文件没有任何 JS 读它**、后端也不接受
`dinner` 字段 —— 设了 18:30 完全没作用。

**修法**：新增四函数 `dinnerAnchor()` / `fmtClock(mins)` / `clockOf(minutes)` / `updDinnerHint()`。
把排程结果的**相对分钟**（t=0 = 第一个占用资源的任务）锚到墙上时钟：
`开始下厨 = 开饭时间 − 总时长`。`render(d)` 两处（早退分支 + 正常分支尾部）调 `updDinnerHint()`；
通知区（`⏱ t=${n.at_min}分 · …`）与步骤详情时间行（`17 → 24 分（18:30 → 18:37）`）都带墙上时钟。

⚠ **它不是约束** —— 不会反过来逼求解器压缩总时长（那会让 Γ 口径被动摇）；真来不及就
如实说"⚠ 来不及"，而不是偷偷改计划。`start_min < 0` 的提前准备步骤不参与换算。

#### A16.4 ★ 右列（已选卡）"恒定单行"铁律 —— 一次差点误判的 A/B

**现象**：第一版「开饭提示」写成独立块 + 完整句（`🕐 按 18:30 开饭：需 17:30 开始下厨（总时长 60 分）`），
在右列窄栏里**折成 3 行**，把「生成排程」推进悬浮球 ⇒ 单视口 **1366×768** 报 `118/2`。

**差点误判**：我最初把原因归到"多了一行"，做了两次不完整的 A/B（先只把提示变独立块、
又只把文案变长）**都会绿** ⇒ 差点得出错误结论。
**真正的判据是"长文案 + 折行"**：最终 A/B 用"长文案版"（`innerHTML` + `<br>`）测得
单视口 `118/2` 红、扫描表报 `1366×768(:生成排程 →)` ⇒ 证明新检查会响。

**修法**：正文取最短说法（`17:30 下厨` / `⚠ 来不及`），全解放 `title`，
样式 `white-space:nowrap + text-overflow:ellipsis` 保行高恒定。改后 **120/0**。

⚠ **为什么只有 1366×768 发作**：1440×900 右列有余量，看不出折行。⇒ 排版扫描表必须
**逐视口**跑（`VP_LIST` 14 档），单视口默认值（1440×900）永远发现不了。
⚠ 判"三列仪表盘档"**必须**用 `matchMedia('(min-width:1025px) and (min-height:761px)')`，
**不能**用 `gridTemplateColumns` 轨道数 —— 手机断点下 `.grid3` 已改 `display:flex` 竖排，
但该属性仍解析出 3 条轨道（实测把 844×390 误判成桌面档）。
⚠ 这段代码整体在**模板字符串**里：注释里绝不能用反引号（实测 `SyntaxError: Unexpected identifier`，
整个探针起不来）。

#### A16.5 ★ 探针改了真实配置（严重，本轮修）

`data/llm_settings.json` 的路径写死在代码里 ⇒ 跑完 `ui_click_audit.mjs`，
开发机的 `provider` 从 `openai` 变成 `ollama`、`model` 变成 `qwen2.5:3b`；
密钥/地址因"留空=保持原值"**看起来什么都没坏**（用户下次开页面才发现模型被换了）。

**修法**：新增隔离开关 `FT_LLM_SETTINGS_FILE`（镜像既有的 `FT_DEVICES_FILE`），
在 `app/agent/llm.py` 的 `_SETTINGS_PATH` 上生效；`tests/conftest.py` 在 import 期
钉到不存在的路径。护栏 `test_settings_path_can_be_redirected_by_env`（重定向 + 还原 + reload）。

#### A16.6 ★ 三处数据卫生 + 两处探针可靠性

1. **`data/recipes_user.json` 被打进发布包**：它是菜谱库**用户层**，`overrides` 按 id
   **覆盖**官方菜且**回不去**（`restore()` 只恢复"被删除"的菜，不撤覆盖）。
   ⇒ 加入 `sync_publish.SKIP_NAMES`（**三份本机状态**）+ 2 处护栏 + 2 条变异条目。
2. **探针留副作用**：`ui_probe.mjs` 的「恢复默认[选择厨房]」假失败 —— 前置状态未归一。
   用例开头 `POST /api/devices/reset` 归一，末尾写回 `devOriginal`。
3. **扫描表判档用错信号**：见 A16.4 的 `gridTemplateColumns` 坑。
4. **`_mutate_web_guards.py` 跨文件变异崩**：`dict(os.environ, FT_WEB_HTML=…, **extra)`
   在两者同名时抛 `TypeError: dict() got multiple values for keyword argument 'FT_WEB_HTML'`
   ⇒ 整个套件断在第一条。修法：先铺默认再 `env.update(extra_env or {})`。
   `main()` 支持 **5 元组 = 变异另一个文件**（第 5 项是相对仓库根的路径），
   走各自 env 覆盖开关（`.mjs` → `FT_CLICK_AUDIT`）。

#### A16.7 ★★ 变异残留教训（本轮踩到）

`_mutate_llm_guards.py` 是**原地变异**。前台跑被 SIGTERM 后 `finally` 不执行 ⇒
`llm.py:476` 的 `"api_key_masked": mask_key(eff_key)` 被留成 `"api_key_masked": eff_key`
⇒ **设置接口会回传密钥原文**。

- **定位**：与 `.workbuddy-ai/tmp/mutate_llm_*.orig` 比 sha256（`aed05b12…` vs `d384c0c3…`），
  再 `diff` 找出唯一差异行。
- **修法**：从 `.orig` 写回 + 删除 `.orig`；**以后一律后台跑**。
- 判残留**必须比 sha256**（锚点全在、只换了一行，`grep` 看不出来）。

#### A16.8 本轮基线

`pytest` **505** · `test_web_ui` **61** · `test_llm_provider` **66** · `test_deploy_windows` **44** ·
`ui_probe` **165/0** · `ui_click_audit` **241/0/5/6 · 回车缺口 0** ·
`ui_layout_probe` **120/0**（1440×900 / 1366×768）· **118/0**（矮桌面长页 / 堆叠）·
**117/1**（320×568，既有未修）· 变异 **143/143 · 19/19 · 31/31** ·
发布包 **73 条目 / 473,284 B / sha256[:16] `8938fe51127a7509`**（**无三份用户层**）。

**既有未修（诚实清单）**：`320×568` 滑块只 125px（时间轴滑块未拿到行宽 1/2）·
长页档下球与 `#eventResetBtn` 的既有重叠（按"可滚动避开"豁免）· `667×375` 纵向溢出。

---

### A17 计划工具与历史持久化（v3.7，2026-10-08 第五轮）

用户勾了 9 项（A1 历史持久化 / A2 计划对比 / A3 导出作业单 / A4 采购清单 / C2 快捷键 /
D1 多轮对话 / D2 为什么这样排 / D3 自然语言改计划 / D4 不可行兜底），B 组（多餐多日 /
库存 / 成本 / 营养）明确不做。9 项全部落地。

#### A17.1 历史落盘（本轮唯一的后端语义改动）

- `data/schedules_user.json` = **第四份本机状态** ⇒ `sync_publish.SKIP_NAMES`
  **三份→四份**。
- 隔离开关 `FT_SCHEDULES_FILE`。⚠ 与 `FT_DEVICES_FILE` / `FT_LLM_SETTINGS_FILE`
  **不同**：那两个只在"用户主动保存"时写，而**排程历史每跑一次 `/schedule` 就写一次**
  ⇒ conftest 必须指 `tempfile.mkdtemp()`，不能用 `tests/_no_such_*.json`
  （否则 tests/ 下很快出现一份几十条测试计划的 json）。
- 三端点：`GET /api/schedules`（**只回摘要**，绝不带 `plan`）/
  `GET /api/schedules/{sid}`（完整记录，找不到 ⇒ **404**）/ `DELETE /api/schedules`。
- 落库放在 `/schedule` 的**最后**：`insight`/`bound`/`model`/`agent` 都是后续
  追加进 `res` 的，提前落库会把它们漏掉。
- 记录页行整行可点还原（走 `/api/schedules/{sid}`，**不重调 `/schedule`** —— 否则
  用户点几次"看看以前的计划"，历史就被自己刷满了）。

#### A17.2 ★ 行内复选框不许挂 `onclick="event.stopPropagation()"`

语义没错（点勾选别还原计划），但**穷举探针按 `onclick` 属性枚举控件** ⇒ 复选框被当成
"点下去什么都没发生"归入待判断 ⇒ **每多一条历史就多一个待判断**，退出码跟着变 1，
基线数字漂移（实测 7 条历史 ⇒ 待判断 7）。改由**行自己**判事件源：
`restoreSchedule(sid,ev)` + `if(ev&&ev.target&&ev.target.closest('input'))return;`。
护栏 `test_历史行复选框不许自带onclick`。

#### A17.3 ★ 慢控件的判定窗口（探针时序）

`__apiLog` 在**响应回来之后**才记一笔（它要拿 `r.status`），而默认判定窗口只有 220ms ⇒
同步等后端的 `run()` **时红时绿**（服务热就绿、冷就红，同一份代码两种结论）。加
`SLOW` 集合（`run`/`agentPlan`/`triggerEvent`/`testModel`）放宽到 8s。
另：记录页段前先 `DELETE /api/schedules` 再**固定造一条**
（`POST /schedule` 两道菜 + `notify:false`），否则控件集合随开发机历史漂移。
修后连跑两次**逐行一致**（250 / 0 / 5 / 8）。

#### A17.4 其余 8 项要点

| 项 | 铁律 |
|---|---|
| A2 对比 | 最多 2 条；**设备利用率不标优劣**（仪表盘 ≠ 方向盘） |
| A3 CSV | **必须带 UTF-8 BOM**（否则 Excel 中文表头乱码） |
| A3 作业单 | 走**下载**，不用 `window.open`（拦截器挡 + 无头环境直接抛错） |
| C2 快捷键 | 让路三规则：不抢可编辑元素 / 不抢修饰键组合 / 数字键走 `show()`（绕过会漏 `loadLib` 等副作用） |
| D1 对话 | 用户消息与模型回复**两处都存**（只存一半会串不上） |
| D2 为什么 | **按需弹层**；`render()` 里**不许**再出现 `insight`（用户明确删掉了常驻面板） |
| D3 改菜单 | 不造菜 / 不丢菜 / 模型不可用**如实说**；离线兜底**按小句**判关键词（整句判会让"去掉 A，把 B 提前"里的 B 被连坐删掉） |
| D4 兜底 | 建议必须**可照做**；没建议时**空容器**（否则挤甘特图） |

#### A17.5 ★★ 两处"过期护栏"（本轮修）

**① 4 条前端变异锚点被本轮改动打破**（全部静默 `[跳过]`）：`confirm(` 现在出现 2 次
（新注释里也写了 `confirm()`）、设置菜单按钮加了 `title` 属性、`render()` 早退分支插入
`updPlanTools/showHints`、`white-space:nowrap` 现在 2 次（新 CSS 也用）⇒ 锚点全部加长到唯一。

**② `_mutate_deploy_guards.py` 两条 `recipes_user.json` 变异是假绿**：
- `_run` 把测试文件写死 `test_deploy_windows.py`，而盯 `sync_publish.py` 的护栏住在
  `test_device_persist.py` / `test_schedule_history.py` ⇒ pytest 以「no tests ran」退出
  （**exit 5**），与"断言失败"的 returncode 都非 0 ⇒ 被当成"护栏命中了"。修法：`_run`
  区分 exit 5 并退化成全仓库 `-k`。
- 更根本：那两条护栏写 `from scripts import sync_publish` ⇒ 读**真文件**，而变异只写
  **副本** ⇒ 永远不生效。修法：`conftest.sync_publish_skip_names()`（优先读
  `FT_WIN_DEPLOY_DIR` 下的副本）+ 同名 fixture，两处护栏改用 fixture。

**教训**：变异脚本的"锚点找不到"是**静默**的（`[跳过]`），而"测试跑不到"更隐蔽
（exit 5 被当成命中）。⇒ 改完被护栏盯的源码后**必须重跑对应变异**，并检查输出里
有没有 `[跳过]`；新增变异条目后也要确认 `len(MUTATIONS)` 涨了、且 `[跳过]` 为 0。

#### A17.6 本轮基线（2026-10-08 第五轮）

`pytest` **531**（`test_web_ui` 61→**73**、新增 `test_schedule_history` **14**）·
`ui_probe` **173/0** · `ui_click_audit` **250/0/5/8 · 回车缺口 0** ·
`ui_layout_probe` **120/0**（14 视口）· 变异 **156/156 · 33/33 · 19/19 · 17/17** ·
发布包 **73 条目 / 496,196 B / sha256[:16] `13128536e0f87b97`**（**无四份用户层**）·
解压冒烟 `/schedule` **47min / 出锅差 1 / 24 任务 / 0 冲突**，落盘 10,967 B，
列表不带 plan、详情全字段、不存在 sid ⇒ 404。

---

### A18 撤销与二次确认（v3.8，2026-10-08 第六轮）

用户补勾了上一轮清单里的 **C1（操作可撤销 / 二次确认）**。其余 6 项已在前一轮落地。

#### A18.1 分工铁律：按"能不能真的回退"决定给什么

| 操作 | 确认 | 撤销 |
|---|---|---|
| 删除菜谱 `deleteRecipe` | **保留 `confirm()`** | 官方菜 → `POST /api/recipes/{id}/restore`；自建菜 → 用 `raw_ingredients`/`raw_steps` **重建**（新 id） |
| 恢复默认厨房 `resetKitchen` | **不加** | 把重置前的 `window._dev` 写回 `POST /api/devices` |
| 清空对话 | 新增弹层 `askClearChat` | 写回内存 + `chatSave()` + `renderChatHistory()`（**三处缺一不可**） |
| 清除令牌 | 新增弹层 `askClearToken` | 填回原值 + `saveToken()` |
| 清空历史 | 保留弹层 | **延迟提交** 8 秒 + `undoClearHist` |

⚠ **`deleteRecipe` / `resetKitchen` 绝不能加确认层** —— `ui_probe.mjs` 会**直接调用**
它们（第 8e 节删菜谱、恢复默认用例），加确认会让探针红。这两个操作本来就可撤销，
撤销比确认更强。⚠ 撤销恢复默认**不能**再调 `/api/devices/reset`（那是"再恢复一次
默认"，不是"回到我原来的样子"）。

#### A18.2 清空历史 = 延迟提交（本项目唯一一处）

`doClearHist` **不调** `DELETE`；备份 `HIST` → 清 UI → `setTimeout(commitClearHist, 8000)`。
`undoClearHist` 只 `renderHist()`，**绝不能** `loadHist()`（后端还没清，拉回来是旧数据，
看着像撤销失败）。代价：窗口内刷新 ⇒ 删除没生效（**更安全**的方向）。
⇒ 因此 `loadHist` 的渲染抽成了 `renderHist()`（`loadHist` 与撤销共用）。

#### A18.3 撤销条与确认弹层的三条纪律

1. `.undo-bar` **必须 `position:fixed`**（底部**居中**，左下已被 `.llm-toast` 占）——
   进文档流会把甘特图挤到 35px（同 §A15）。
2. 撤销按钮必须显式 `width:auto` + `color` + `box-shadow:none`（全局 `button` 是
   `width:100%;color:#fff`）。
3. `closeInfo()` **必须清 `CONFIRM_FN`**；`runConfirm()` 必须**先取回调再关弹层**
   （`closeInfo` 会把它清成 null）—— 否则「取消」掉的回调会在**下一次确认**时误触发。
   撤销回调**只保留最近一次**（`undoToast` 每次 `clearTimeout` + 覆盖 `UNDO_FN`）。

#### A18.4 ★ 三处护栏判据被"同名的别处实现"兜住（变异 167/170 抓出来的）

| 变异 | 原判据 | 兜住它的东西 | 修法 |
|---|---|---|---|
| 删掉 `/restore` 入口 | `"/restore" in html` | `undoDeleteRecipe` 里也有一处 | 判 `restoreRecipe` 函数体 |
| 撤销清空改成 `loadHist()` | `"apiFetch" not in undo` | `apiFetch` 在 `loadHist` 内部 | 同时拦 `loadHist(` |
| 恢复默认不留快照 | `"_dev" in fn` | `_devStr(d)` 含 `_dev` 子串 | 判 `window._dev` |

⚠ 第 3 条的**变异本身**也有问题：`old` 只改了快照第一行，第二行还有 `window._dev`
⇒ 必须**整块**替换。修后 **170/170 · 0 跳过**。

**教训**：新加一处"同名调用"（哪怕语义完全不同）就会让老的子串判据失效 ——
`/restore`、`apiFetch`、`_dev` 三个都是这么被兜住的。**改完源码必须重跑变异自检**。

#### A18.5 本轮基线（2026-10-08 第六轮）

`pytest` **540**（`test_web_ui` 73→**82**）· `ui_probe` **189/0** ·
`ui_click_audit` **250/0/5/8 · 回车缺口 0** · `ui_layout_probe` **120/0**（14 视口）·
变异 **170/170 · 33/33 · 19/19 · 17/17**（0 跳过）·
发布包 **73 条目 / 503,766 B / sha256[:16] `aa3778bb92596285`**（无四份用户层）·
解压冒烟 `/schedule` **47min / 出锅差 1 / 24 任务 / 0 冲突**，落盘 10,968 B，
列表不带 plan、不存在 sid ⇒ 404。

---

## B. LLM 接入与安全

### B1 provider 分流（默认本地 Ollama）+ **运行期可切**

`FT_LLM_PROVIDER` ∈ `ollama`(默认) / `openai`。默认 `http://127.0.0.1:11434/v1` + `qwen2.5:7b`，**免密钥**；
只有 `openai` 档才要 `API_KEY`（`api_key_required()`）。`_cfg()` 把**空串当未设置**
（`.env` 里 `KEY=` 不再顶掉默认值）。

⚠ **2026-10-03 起 provider 是"运行期可切换状态"，不是只读常量**：设置页
「设置 → 模型接入」可在两档间切，落盘 `data/llm_settings.json` 的
`provider` / `models` / `remote` 三块。要点：

- `set_provider(provider, base_url, api_key, model)` 只覆盖**显式给了非空值**的字段；
  切档时把当前模型记进 `models[旧档]`，并恢复 `models[新档]`。切换后
  **重置熔断 + 在线闸门 + `_recompute_timeouts()`**。
- `_DEFAULTS` 里**不写死任何第三方中转站地址**（写死等于把别人的域名随源码/镜像发出去）。
- **env 永远最高优先**：`provider_locked()` 会禁用界面上的接入方式下拉，
  但**不**影响"同一档内换模型"（`model_locked()` 是另一条）。
- `providers_view()` 是"能选哪些接入方式"的**唯一真源**，前端只渲染、不自己写选项。
- `models_by_provider()` 供前端切档时回填"该档上次用的模型"。
- 护栏 `tests/test_llm_provider.py`(**50**) / `tests/test_warmup_probe.py`(**14**)。

### B2 模型名三层优先级

```
FT_LLM_MODEL（真实环境变量）  >  data/llm_settings.json（设置页写盘）  >  provider 内置默认
```

- ⚠ **`_resolve_model()` 必须优先读 `models[provider]`，而不是全局 `model`。**
  用户在界面切到远程（全局 `model = gpt-5.6-sol`）后，又在 `.env` 里用
  `FT_LLM_PROVIDER=ollama` 钉回本地 —— 只看全局 `model` 就会带着 `gpt-5.6-sol`
  打到本机 11434，Ollama 返回 404 `model not found`，而所有调用点**静默降级**：
  现象是"部署时钉了本地，AI 就再也不动了"，页面不报任何错。
  `models` 缺失（老文件）时才回落到全局 `model`。
- ⚠ **密钥只存不回传**：`/api/model_settings` 是**无鉴权**的（本机演示 + 发布出去的
  公开链接都能读），所以 `remote_view()` 只回 `api_key_set: bool`，**没有** `api_key` 字段，
  页面上也永不回填密钥框。对应地 **空串 = "保持原样"，不是"清空"** ——
  密钥框永远是空的，当清空会让用户每保存一次就抹掉一次自己的密钥。
- `llm.MODEL_SOURCE` ∈ `env` / `settings` / `default`，前端必须显示。
- 设置文件**缺失或损坏一律当 `{}`**（可选层不该让服务起不来）；写盘用**临时文件 + `os.replace`**。
- `set_model()` 写盘后**必须 `breaker_reset()` + `reset_online_gate()`** ——
  否则换到快模型后闭环仍被闸门挡着（熔断器/闸门记的是**上一个模型**的失败）。
- 非法模型名 `raise HTTPException(400, detail=...)`，**绝不静默成功**；返回的 `verified`
  表示该模型是否真在本机，`ok=False` 时给 `None` 而非 `False`；
  ⚠ **远程档必须给 `None`**（模型名在中转站那边，本机当然没有 ——
  照旧报 `False` 会把一次正常的远程切换显示成"⚠ 调用会返回 404"）。
- **缓存键不含模型名** ⇒ 换模型不会自动失效。
- 列模型走 Ollama **原生 API** `GET {root}/api/tags`（**不在 `/v1` 下**）；
  `BASE_URL` 形如 `.../v1`，直接拼会去请求 `/v1/api/tags` 拿 404 ⇒ `_native_root()` 专门去掉尾部 `/v1`。
  Ollama 没起是最常见状态，失败**不当异常抛**。

### B3 超时与启动探测

| 档 | `TIMEOUT` | `FT_WARM_LLM_BUDGET` |
|---|---|---|
| `ollama` | 30s | **60s** |
| `openai` | **20s**（2026-10-03 由 3.0 上调） | 1.5s |

- ⚠ **远端 3.0s 是"贴着线"的**：2026-10-03 接土豆中转站（`gpt-5.6-sol`）三次实测
  往返 **2.899 / 2.273 / 2.640 s**，非流式可用（返回里 `model` 被归一成站内结算模型）。
  贴线的后果是"聊天时好时坏"，而 llm 的失败**静默降级**、现场零报错。
- ⚠ **放大 TIMEOUT 不会拖慢排程**：`chat()` 取 `min(调用方传入的 timeout, TIMEOUT*3)`；
  闭环自己传 3.6s / 2.5s，与这里无关。反过来 TIMEOUT 变大后，闭环那几次超时会落进
  `deadline_race`（判"预算不够"、**不计熔断**）—— 这是安全方向。
- ⚠ **"稳态 131ms"是熔断器打开之后的数字，不是"第一个请求"的数字。**
- 探测必须走**和 Agent 完全相同的调用路径**（`chat_json`，`json_mode=True`），不能只用纯文本 `llm.chat`。
- 判据是**"能否在预算内返回"**而不是"连不连得上"。`cache_key` 留 `None`，探测结果不落盘。
- ⚠ **探测超时后是"熔断"还是"只关闸门"，取决于预算宽窄**（`llm.probe_miss_is_fatal()` = `WARMUP_BUDGET >= 10`）：
  - **宽预算档**（本地 60s）：连它都答不出来 = 模型没装/服务没起 ⇒ `force_open("warmup")`。
  - **紧预算档**（远端 1.5s）：超时只说明"比在线闭环慢"，**不说明服务坏了** ⇒
    只 `mark_online_too_slow()`。中转站实测 2.3~2.9s **必然**超 1.5s ——
    若照旧熔断，**每次重启都会把 AI 对话一起关掉 120s**，而那恰好是用户第一次点它的时刻。
  - 判据用**预算**而不是 provider 名字：规则只有一条，不会出现"provider=openai 但端点
    其实在局域网里"这类组合的例外。
- 本地 7B 冷启动 ≈ 10s ⇒ 1.5s 预算会把"能用但慢"误判成不可用并启动即熔断
  （实测首请求 **6735ms → 154ms**）。
- ⚠ **预热顺序必须 模型探测 → `plan_recipes` 预热**：反过来会让冷启动模型撞 3.6s 闭环预算、
  白慢 7s，且 `failures=2/3` 差一次到阈值 —— **服务没接客就自我熔断**。
- 回归：`tests/test_warmup_probe.py`（已变异测试）。

### B4 "预算不够" ≠ "服务坏了"

- `_post` 区分 `"timeout"` / `"err"`。
- `chat()` 里 `deadline_race` 的超时只记 `STATS["deadline_miss"]`，**不计熔断** ——
  否则闭环两次超时就把熔断打开，连 AI 对话一起关 120s。

### B5 在线闭环闸门（与熔断器分离）

熔断器管"服务坏了"，闸门管"服务好着但比闭环预算慢"。

- 判据 **`deadline_miss > 0`**（长提示词必落空、短提示词能成功，"有没有成功过"会被骗过去）。
- 只关**闭环**（orchestrator / replan），对话 / 通知 / 预热照常用模型。
- `FT_LLM_ONLINE_GATE=off` 可绕；`llm_status` 暴露 `online_gate_ok` / `reason`。
- 实测 qwen2.5:7b："策略提议" 7.6s、"目标塑形" ≈3.3s ⇒ 闸门落下后 `/api/plan` 0.14s。

### B6 测试必须钉离线档

默认 provider 免密钥后 `enabled()` 默认 True ⇒ 本机跑着 Ollama 时测试会**真发 HTTP**。

- `tests/conftest.py` **无条件**把 `FT_LLM` / `FT_AGENT_LLM` / `FT_WARM_LLM` / `FT_NOTIFY_LLM` 置 `"off"`。
- `scripts/_offline.py::force_offline_default()` 供基准脚本复用。
- `conftest` 覆盖 fixture 的 `FT_WARM_LLM=on` ⇒ 那里改用 `monkeypatch.setattr(llm, "chat_json", …)`。

### B7 "远程 LLM 不可用"排查

真因几乎总在前两条，**不是 CORS**：

1. `Dockerfile` 默认 `ENV FT_LLM=off FT_AGENT_LLM=off`；
2. `.env` 被 `.dockerignore` 排除 ⇒ `API_KEY` 空 ⇒ `enabled()` 恒 False 且**静默**。

容器用宿主机 Ollama 要 `host.docker.internal`（Linux 加 `--add-host`），并让 Ollama 监听 `0.0.0.0`。

### B8 安全层 / 凭据

全 opt-in（关闭时与修复前逐字一致），细则见 `docs/部署文档.md`，`tests/test_security.py` 29 项。

- ⚠ `add_middleware` 是"**后加的更外层**" ⇒ 先 `_security_guard` 后 CORS。
- **配置在导入时读取 ⇒ 测试必须 `importlib.reload`**。
- **评审机必须用 `requirements.lock.txt`**。

---

## §B-补1　模型配置五层分发 + 密钥打码（v3.4，2026-10-06）

> 触发：**"迁移程序时 api 调用的配置被抹掉了，别人拿到之后调不了 api"**。
> 需求原文三句：① 配置不能丢；② 前端对 key **打马赛克就好，不用删除**；③ **加默认自带配置，发出去第一时间就能用**。
> 完整对照文档：`docs/模型配置随包分发.md`。

### B-补1.1 五层优先级

```
真实环境变量
  > data/llm_settings.json        用户层（设置页写的；sync_publish 刻意排除）
    > app/agent/llm_defaults.json   出厂层（随源码 / 发布包走）← 本次新增
      > llm._DEFAULTS               代码常量（不带任何凭据）
        > 兜底常量
```

改造前只有"用户层 + 代码常量"两层 ⇒ **任何一层的凭据都不在包里**：
接收方拿到程序 → `provider=ollama` → 本机没装 Ollama → AI 静默降级成规则兜底，
页面上没有任何报错。这就是"迁移之后调不了 API"的成因。

关键实现点（`app/agent/llm.py`）：

| 点 | 结论 |
|---|---|
| `_read_defaults()` | 缺失 / 坏形状 **一律当空** + 一条 warning，回落代码默认档。接收方**删掉这个文件**也必须能跑。 |
| `_layer(factory)` | `factory=None` ⇒ 用 import 时快照 `_FACTORY`。**缺省故意不是"空"**：缺省=空 ⇒ 以后新调用点忘传 `factory` ⇒ 出厂层整层失效（就是这次要修的问题）；缺省=真出厂层 ⇒ 最坏只是测试要显式传 `{}`（会立刻红）。 |
| 用户层优先 | 是"出厂层放 `app/`、用户层放 `data/`"的**结构结果**，不是额外判断逻辑 ⇒ 重新发版不会覆盖接收方的改动。 |
| `set_provider` 落盘模型 | 没显式给模型时用 `_resolve_model("", {}, p)[0]`（出厂层 > 代码默认）。**不能写 `_DEFAULTS[p][1]`**：会把出厂层配好的 `gpt-5.6-sol` 换成 `gpt-4o-mini`，中转站上没有 ⇒ 404 ⇒ 静默降级。 |
| `models_by_provider()` | 用户层没记录时回落**出厂层**（否则接收方第一次切到「远程」看到空模型框，会以为还得自己填）。 |

⚠ **出厂文件必须在 `app/` 下**：`sync_publish` 同步 `app/` 全量、排除 `data/llm_settings.json`。
放 `data/` 会踩同一个坑（本地正常、包里没有、无报错）。
⚠ **容器里 `Dockerfile` 的 `ENV FT_LLM=off` 是全局总闸**，出厂层**不绕过**它；
容器里要开 AI 得运行时传 `FT_LLM=on`。

### B-补1.2 密钥打码（"打马赛克，不要删除"）

| 位置 | 行为 |
|---|---|
| `llm.mask_key(k)` | 前 6 + `…` + 后 4；`len(k) < 12` ⇒ `"***"`（露头露尾 = 把整把钥匙给出去） |
| `llm.looks_masked(v)` | 含 `…` 或 `***` ⇒ 打码预览。真实密钥只含 `[A-Za-z0-9_-]`，不误伤 |
| `remote_view()` | 回 `api_key_masked`（**绝不回原文**）+ `base_url/model`（用户层保存值，供回填）+ `effective_*`（**生效值**） |
| `factory_view()` | 出厂层现状，同样只给打码预览；读的是 import 快照（保证"页面显示的"与"实际请求用的"同源） |
| `GET /api/llm_status` | 新增 `api_key_masked`（顶部徽标 tooltip） |
| `GET /api/model_settings` | `remote.api_key_masked` + 新增 `factory_defaults` 块 |
| `set_provider()` | 提交的 `api_key` 形如掩码 ⇒ **400**，且**拒绝时一个字节都不写盘、不改全局量** |

**为什么"打码预览"可以回传**：`/api/model_settings` 是**无鉴权公开**的。
前 6 + 后 4 只够辨认"是哪一把钥匙"（换过 key 后最需要这个），中间熵不外泄；
再加"后端拒绝把掩码写回去"这条，字段就是安全的。
**只回"已配置"不够**：用户没法确认配的是哪一把 ⇒ 反复重填 ⇒ 把好配置改坏。

前端（`app/web/index.html`）：

- 标签行 `#setRemoteKeyNote`（**inline 放在 `.set-label` 里**，不新开块 —— 新增块级元素会撑高卡片，
  那类"悄悄挤一下布局"只有真 Edge 排版探针看得见）显示 `已配置：sk-pro1…ESb0`；
- **输入框保持空**（`rk.value=''`）：把打码值填进 `value`，用户随手点一次「保存」就会提交它；
- 提交前 `looksMaskedKey(k)` 拦（与后端**同款判据**，两处口径必须一致，
  否则会出现"前端放行、后端 400"或反过来的错位）；
- 状态表 / 徽标 tooltip 都带打码预览；
- 用户层为空 + 出厂层存在 ⇒ 提示"远程那一栏现在用的是**随程序附带的出厂预置**，
  填了自己的并保存后以你的为准"（不说这句，接收方会以为是自己配好的）；
- `model_source == "factory"` 映射成「出厂预置（随程序附带）」。

### B-补1.3 分发通道与护栏

- **第三方域名只准出现在 `llm_defaults.json`**：护栏
  `test_third_party_relay_appears_only_in_the_factory_file` **从该文件读出域名**再扫
  `app/ scripts/ deploy/ .env.example Dockerfile`（换站时护栏自动跟着走，不用改测试）。
  `scripts/ui_probe_llm_off.mjs` 里的样例域名已改成 `relay.example.com`。
- **`pack.ps1` 反向检查**：包内出现 `.env` / `.env.local` ⇒ `Die`
  （`tests/test_deploy_windows.py::test_pack_refuses_to_ship_env_files`）。
- **变异自检**：新脚本 `scripts/_mutate_llm_guards.py`（19 条），
  `_mutate_web_guards.py` 追加 6 条。
- ⚠⚠ **Python 变异脚本被强杀 ⇒ 源码留在变异态**：`_mutate_llm_guards.py` 前台跑超时被 SIGTERM，
  `finally` 与信号处理器都没跑，`llm.py` 里的 `looks_masked()` 被留成 `return True`
  ⇒ 之后所有真密钥保存全被误拒。现在两个 Python 变异脚本都装了 SIGINT/SIGTERM 处理器
  + 开头自动从 `.orig` 还原。**判断"有没有残留"必须比 sha256**，不能只看几个锚点还在不在
  （那次锚点全在，只有一行 `return` 被换掉）。
- ⚠ `_mutate_web_guards.py` 的**基线阶段按测试名去重**（101 条变异只有 ~40 个不同测试，
  逐个重跑会把基线耗时放大成 O(变异数)）。
- ⚠ 出厂层被改坏之后跑一次 `sync_publish`，坏代码就进 `publish/` 了 ⇒
  改完源码 → 跑测试 → 再 sync。

### B-补1.4 已知取舍

密钥**明文随包分发**（用户明确要求）。缓解：环境变量整层覆盖 / 用户层整层覆盖 /
删 `remote` 块或改 `provider` 即可关掉。**轮换密钥 = 改这一个 json + 重新打包。**

---

## C. 工具链踩坑与已知未修（完整版）

### C1 `scripts/e2e_check.py` 的假警报

菜名**必须从官方库运行时取**；`DishPrior` 是 **dataclass 不是 dict**；
**顺序判据必须走依赖边**（`end(前) ≤ start(后)`），不能按原文序号。

`e2e_check` 的 `--no-http` 只跑 A（静态接线）+ C（层内直调）；B 段要后端在跑。

### C2 已知未修

- **多模式顶替未编码**（蒸箱 ⇄ 灶具蒸锅）：容量置 0 时精确轨判不可行、回落贪心。
  **CP-SAT 不读容量覆盖**。
- **组合下界偏松**：`scripts/lb_probe.py` 实测 gap 中位 **26.4%**、`makespan == LB` 仅 **18%**。
  根因是人工项假设"单人零空闲" ⇒ 报出的是**保守上界**，不是真下界。
  最优性证据是 **`OPTIMAL 60/60`**（`verify_model`）—— **两项都要写**，只报一个会误导。
- **`/parse` 不做官方菜库补全**（`/schedule`、`/reschedule`、`/notify` 都做）⇒
  只给菜名不带 `steps` 时它返回空。调用方按同样习惯用会拿到空结果。
- **`publish/` 是拷贝不是链接** ⇒ 改源码必须 `python -m scripts.sync_publish`
  （`--check` 只检查，退出码非 0 = 线上会是旧代码）。
- **必须单 worker**：进程内有状态（熔断器、缓存、请求级 ContextVar）。

### C3 界面残留（诚实清单）

- 极窄甘特条（0.5min 任务在 68min 计划里 ≈5px）**刻意保持时间轴比例诚实**，
  不为了可见性去挪 `left`（那会画错开始时刻）；精确时刻由「任务明细」表承担。
- 甘特条拖拽不持久：`bindDrag` 只改视觉位置，`render()` 一刷新就复位。
- 列表静默截断：计划页 chip 报「全部 100」但只渲染前 80 行（菜谱库 100 < 120 故当前不截断）。
- `data/ref_*.html` 是**厂商原始设计参考稿**，依赖 CDN、离线不可用 —— **不要**照它改界面。

### C4 评分口径的历史教训

**"配件词剥离"让 6/50 组出锅差变差 —— 已定性为修对了，不回滚。**
逐组核对：旧口径从「蒸烤盘/蒸烤架/油纸/锡纸」等**器具名**里凭空造出蒸箱/烤箱任务
（如「柠檬干」被造出蒸箱 15min，而它真正的烘烤步反而没识别），于是两道菜恰好收尾在
**不同设备**、能同时收尾 ⇒ gap=0；修对后两道菜都收尾在**同一台容量 1 的烤箱**，
8 分钟任务不可能同时收尾 ⇒ **gap ≥ 8 是硬下界**（模型报 `OPTIMAL`，`fixed` 实验证实）。
**旧口径的 94% 是假任务换来的。**

---

## D. Windows 服务器部署（`deploy/windows/`）

腾讯云轻量 **Windows Server + 宝塔面板**（用户实机：2核2G · 广州 · `111.230.129.109`）。
项目原有 `deploy/`（`server-setup.sh` / `cooking-agent.service` / `nginx-cooking-agent.conf` /
`upload.sh`）**全是 Linux/bash 路线，在这台机器上一条都用不了** ⇒ 新写一套。
教程（给用户看的逐步版）在 `docs/Windows服务器部署教程.md`。

### D1 三个脚本 + 两条护栏

| 文件 | 跑在哪 | 作用 |
|---|---|---|
| `deploy/windows/pack.ps1` | **开发机** | 强制 `sync_publish` → 打 `dist\cooking-agent-windows.zip`（67 文件 / 0.36MB） |
| `deploy/windows/setup.ps1` | **服务器（管理员）** | 六步全幂等一键部署 |
| `deploy/windows/uninstall.ps1` | **服务器（管理员）** | 默认保守卸载；`-Purge` 才删数据 |
| `tests/test_deploy_windows.py` | — | 静态护栏 **39** 条 |
| `scripts/_mutate_deploy_guards.py` | — | 变异测试 **26/26**（只动 `.workbuddy-ai/tmp/win_deploy/` 副本，靠 `FT_WIN_DEPLOY_DIR` 重定向，**从不碰真文件**） |

`setup.ps1` 六步：定位项目（解压多套一层会自动下探）→ 找 Python（`python` / `py -3.x` / 常见路径，要求 ≥3.10）
→ 建 `.venv` 装依赖（**只有 Python 恰好是 3.14 且锁文件存在才用 `requirements.lock.txt`**，
否则 `requirements.txt` —— 锁文件是在 CPython 3.14 / Windows 上 `pip freeze` 的）
→ 写 `.env.local`（标记块包裹 + 无 BOM）→ 生成 `run-server.cmd`（纯 ASCII/CRLF，`:loop` 崩溃自拉）
→ `New-NetFirewallRule` + `Register-ScheduledTask` → 自检 `/health` `/ready` `POST /api/plan`。

### D2 三条编码铁律，方向**各不相同**（最容易搞反）

| 文件 | 要求 | 为什么 |
|---|---|---|
| `*.ps1` | **必须带 UTF-8 BOM** | Windows Server 的 `powershell.exe` 是 **5.1**，读无 BOM 文件时按 **ANSI（中文系统 = GBK）** 解码。脚本里全是中文注释/提示 ⇒ 中文字节被解错，**连引号和花括号都会被吃掉**，解析阶段直接失败（实测报「语句块或类型定义中缺少右 }」「字符串缺少终止符」）。⚠ **阴在**：把同一文件按 UTF-8 显式读进来解析是**完全正常**的 ⇒ 开发机用编辑器打开、换工具检查，都看不出问题。 |
| `.env.local` | **必须无 BOM** | `llm.py::_parse_dotenv` 用 `encoding="utf-8"` 逐行 `split("=")`，带 BOM 时第一个键变成 `\uFEFFFT_AUTH_TOKEN` ⇒ **静默读不到**，鉴权失效。⇒ 写入用 `New-Object Text.UTF8Encoding $false`。 |
| `run-server.cmd` | **必须纯 ASCII + CRLF** | cmd.exe 按字节偏移分块读文件，多字节字符会让它错位、把半行当命令执行。⇒ 写入用 `[Text.Encoding]::ASCII`。 |

### D3 zip 条目名：`Compress-Archive` 和 `CreateFromDirectory` **都**产出反斜杠

ZIP 规范要求 `/`。实测（直接看 zip 原始字节）：
- Windows PowerShell 5.1 的 `Compress-Archive` → `app\api\main.py`
- .NET Framework 的 `ZipFile::CreateFromDirectory` → 同样反斜杠（该 bug 到 **.NET Core 3.0** 才修，
  而 PS 5.1 跑在 .NET Framework 上）

反斜杠包用资源管理器解开多半没事，但换 7-Zip / 宝塔 / Linux unzip 会解成一堆平铺怪文件，
**要到服务器上才发现**。⇒ `pack.ps1` 改成**手工逐条** `ZipFileExtensions::CreateEntryFromFile`，
条目名自己 `.Replace('\','/')`。
⚠ 两个程序集都要 `Add-Type`：`ZipFile`/`ZipFileExtensions` 在 `System.IO.Compression.FileSystem.dll`，
而 `ZipArchive`/`ZipArchiveMode`/`CompressionLevel` 在 `System.IO.Compression.dll` ——
少一个就报「找不到类型 `[IO.Compression.ZipArchiveMode]`」。

### D4 为什么用「计划任务」而不是 NSSM / WinSW

后两者都要先从网上下载 exe，国内服务器经常拉不动。计划任务是 Windows 自带的、零依赖。
配置：`SYSTEM` 身份 + `AtStartup` + `RunLevel Highest` + **`ExecutionTimeLimit=0`（默认 72 小时会被强行掐断）**
+ `MultipleInstances IgnoreNew`。行为与一个 Windows 服务等价。

### D5 其它硬约束

- **`--workers 1`**：设备配置 / 排程历史 / 熔断器 / 在线闸门全是**进程内状态**，
  多 worker 会出现「改了设备配置，下一次排程又变回去」。
- 服务器上**没有本地 Ollama** ⇒ 非离线档也要写 `FT_WARM_LLM=off`，
  否则每次开机白等 60s 探测、还会顺手熔断。
- **腾讯云控制台防火墙要单独放行**：只开 Windows 防火墙没用，外部照样连不上，
  而服务器上 `curl 127.0.0.1` 却是通的 —— 最容易卡住又最难想明白的一步。
- 加 Nginx 反代之后必须重跑 `setup.ps1 -TrustProxy`，否则限流把所有访客算成同一个 IP。
- 关闭鉴权用**显式开关 `-NoAuth`**，不要用 `-Token -`（PowerShell 参数绑定对单独的 `-` 行为不明确）。

### D6 变异测试连抓两次「子串空转护栏」

- `test_pack_syncs_publish_before_packaging` 原本只断言 `"sync_publish" in text` ——
  而**脚本顶部注释里就有这个词**，把真正的调用 `-m scripts.sync_publish` 改成 `-m scripts.noop` 照样全绿。
  ⇒ 改成锚定**命令行**：`re.search(r"-m\s+scripts\.sync_publish\s*$", text, re.M)`。
- `test_setup_cmd_has_restart_loop_and_single_worker` 断言 `"goto loop" in body` ——
  被 `rem goto loop` 蒙混。⇒ 改成整行锚点 `(?m)^goto\s+loop\s*$`。

**教训**：这跟 §A10 的「命中注释里的字样」是同一类病。静态护栏的锚点必须落在
**代码/命令行本身**上，不能落在"某个词出现过"上 —— 否则注释、字符串、日志文案都能让它假通过。

### D7 顺手修掉的既有缺陷（都不是部署这条线引入的）

1. **`audit_doc` 5.1 的跨零点假报警**：原判据 `ct["endTime"] > ov["finishTime"]` 比的是
   `"HH:MM"` **字符串**，`"23:44" > "00:04"` 字典序成立 ⇒ 排程跨零点时误判"菜品比上桌还晚"。
   触发窗口实测（seed=42 那 40 组）：21:00→0 条、**22:00→3 条**、23:00→1 条、23:30→0 条
   ⇒ **这个审计的结论取决于你几点跑它**。改成算「顺时针距离」
   `delta = (_hm_to_min(finish) - _hm_to_min(end)) % 1440; if delta > span`。
   新增 `tests/test_audit_doc.py`（4 条，含**负向对照**：证明 22:00 确实能让旧口径误报）。
2. **`test_llm_provider.py` 6 条红 —— 夹具只"用完还原"、没"开始前复位"**：
   `PROVIDER`/`MODEL`/`API_KEY`/`TIMEOUT` 是 **import 时**从 `data/llm_settings.json` 算出的
   **模块级全局量**，而那个文件**用户能直接改**。`tmp_settings` 只重定向 `_SETTINGS_PATH` 并
   在 `yield` 后还原 ⇒ 用例拿到的是开发者本机那份配置（上一轮端到端实测把它切成了 `openai`）。
   ⇒ 夹具在 `yield` **之前**补 `llm._apply({})` + `llm._recompute_timeouts()`
   （`_apply` 只算 5 个量，`TIMEOUT`/`WARMUP_BUDGET` 归 `_recompute_timeouts`，**两个都要调**），
   并新增 `test_tmp_settings_gives_every_case_a_clean_slate` 守这条。

### D8 迁移条件验证（2026-10-04）：怎么验、验出什么、两个环境假象

**验法（可复用）**：把 `dist/cooking-agent-windows.zip` 解到 `.cache/migrate-test/`，
**当成一台没有 Ollama 的服务器**真跑：起 uvicorn（`FT_WARM_LLM=off`）→ 打 `/health`、`/ready`、
`/api/plan` → 走设置页那条真实路径切远程 → `/api/chat`。**不要靠读代码判断"应该能跑"。**

**包自包含的判据**（每次打包后都该核）：解压条目数、**含反斜杠条目必须为 0**、
`data/recipes_user.json` 是空层、全树 grep 密钥关键字 **0 命中**、
不含 `.venv`/`.env*`/`llm_settings.json`/`__pycache__`/`tests`/`docs`。

**结论**：满足迁移条件。全新 venv 从 `requirements.txt` 装成 33 包（`ortools==9.15.6755`，
**Python 3.13 也装得上** ⇒ 区间约束跨版本可用）；启动 16.5s；`/ready` 100 道；
`/api/plan` 两道真菜 `recipeCount==2`；**远程对话成功**（`gpt-5.6-sol`，≈2.9s）。
无 Ollama 场景：启动 **7.2s**（不是 60s）、对话优雅降级（`offline=True`、`reason='error'`）。

⚠ **坑 A：开发机的 `http_proxy` 会把"429"伪装成"20s 卡死"。**
本机有 `http_proxy=http://127.0.0.1:50696`（沙箱代理），urllib 默认走它 ⇒ 20s 超时下
12/15 成功、失败的 3 次**精确卡到 20.4s**（双峰）；而裸测同一端点 8/8 全绿。
**禁用代理直连才露出真相：`HTTP 429`（限流）**，~480ms 返回。
⇒ 诊断任何"远端偶发超时"前，**先 `env | grep -i proxy`**，否则会误判成"中转站不可靠"。
⚠ 项目对 429 的处理是 `_trip("quota")` → `CB_QUOTA_COOLDOWN=1800s`（**熔断 30 分钟**）；
正常聊天不触发，猛点「测试连接」会撞上。填 `gpt-5.6-sol` 实际路由到 `step-3.7-flash` 是正常的。

⚠ **坑 B：`pip` 报 `from versions: none` 是"源被挡"，不是"包不存在"。**
`pip.ini` 配清华源时：`curl` 取镜像页 **200**、venv 的 `urllib` 也 **200（169KB）**，
**只有 pip 一个版本都找不到**（连 `Collecting` 都不打印）。
换 `-i https://pypi.org/simple/ --trusted-host pypi.org` **立刻装上**。
（同 `pip-install-forensics` 技能第 0 节铁律。）

### D9 `FT_LLM=off` 是**全局总闸** —— 设置页改不动它，`-Offline` 会写它

- **语义**：`FT_LLM=off` 不是"关掉本地 Ollama"，是**把大模型整体关掉**。
  全仓库**只有** `llm.enabled()` 读它（`llm.py:752`）；`set_provider` / 设置页 /
  设置文件**都不碰它** ⇒ 界面里怎么改都清不掉。
- **实测（2026-10-04）**：`FT_LLM=off` 下 `set_provider('openai', 带地址和Key)`
  返回 `ok:true`（界面显示"保存成功"），但 `enabled()=False` → `llm.chat()=None` →
  前端永远回"已切换为离线模式"（`offline=True`、`reason='disabled'`）。
  用户会去翻模型配置 / 网络 / 防火墙，**想不到是 `.env.local` 里那一行**。
- **成因**：`setup.ps1 -Offline` 会写 `FT_LLM=off` + `FT_AGENT_LLM=off` + `FT_NOTIFY_LLM=off`。
  而**不加 `-Offline`** 的 else 分支只写 `FT_WARM_LLM=off` —— 那才是
  "没有本地 Ollama、但有远程模型"这台机器的正确档位。
- **三处修复**（每处都有护栏，别只改一处）：
  1. 教程 4.2 改成**默认不加开关**，`-Offline` 降级为警示框；附 A 加前提；附 D 加排错行。
  2. `setup.ps1` 的 `-Offline` 分支加 `Warn`，明说"接远程模型也不会生效"。
     护栏 `test_setup_warns_that_offline_kills_remote_models` ——
     ⚠ 锚点必须取 `$lines.Add("FT_LLM=off")` 到 `} else {` **之间**；
     按全文首个 `FT_LLM=off` 切会落在 `.PARAMETER` 说明里（断言注释 ≠ 断言行为）。
  3. 前端 `llmKillSwitchOff(d)` 读 `/api/llm_status` 的 `switches.FT_LLM`，
     在**徽标 tooltip** 和**降级提示**两处都点出来（少一处 = 少一半人看得见）。



## §C-补：假设备的三/四/五类来源 + 备菜前置两处口径（2026-10-04 指令 F）

### 假设备（`parser.need_fb` 探针文本上做减法，官方 100 道占加热设备步数 230→210）

探针：`_fb_probe = _DONE_STATE_RE.sub("", _SEASONING_RE.sub("", _ds.strip_parts(_LABEL_RE.sub("", stext))))`

| 类 | 判据 | 例子 | 旧判 |
|---|---|---|---|
| ① 完成态引用 | `_DONE_STATE_RE` = `(?:烤\|蒸\|炒\|煎\|炸\|煮\|炖\|焖\|烧\|卤\|焯\|煸\|焗\|煲)(?:好\|熟\|透\|沸\|软\|烂\|过)[^，。；、\s]{0,2}?(?:的\|了\|后)` | 小碗蒸肉#10「**炖好的**猪肉，放到碗里，淋肉汁」 | 灶具 45min |
| ① 变体 | 同上（熟/沸/过/软烂） | 芋头蛋卷#7「**煎好的**蛋饼…**碾压好的**芋泥」 | 烤箱 25min |
| ② 器皿名 | `PART_WORDS` 补 烤碗/烤串/烤网/蒸碟 | 香烤鲫鱼#3「**烤串**放在烤架上」 | 烤箱 25min |
| ③ 调料名 | `_SEASONING_RE` = 蒸鱼豉油/蒸鱼酱油/蒸鱼油/蒸肉粉 | 金银蒜蒸鸡胸肉#3「加入**蒸鱼豉油**…腌制1小时」 | 蒸箱 60min |
| ③ 子菜名标签 | `_LABEL_RE` = `^[^，。；、\s：]{2,12}[：:]` | 轻松一锅蒸#4「**腊味合蒸**：处理食材…」 | 蒸箱 15min |
| ④ 量词 | `_infer_from` 炖煮分支 `汤` → `汤(?!匙\|勺)` | 麻辣对虾(第二条)#4 占位句（食材「食用油3**汤**匙」） | 灶具 45min |

⚠ 状态词表**只收 好/熟/透/沸/软/烂/过**，且必须带 的/了/后：
**不收 开/至/成/干/香** ⇒「烧开后」「炒至微黄后」「水分炒干」「炒香后的」全部放行。
摘了这些会把整道菜的真实灶具作业一起摘掉（`test_completion_state_guard_keeps_real_cooking` 钉住）。

⚠ **试过但必须记住的失败方向**：给 `need_fb` 再加"设备装载信号"
（`水箱|加水|第N层|中间层`）+ 把 `香` 收进状态词表 ⇒ 加热步数**顶回 213**，新增 6 条**全是错的**：
「豆沙松糕#3 **边加水边翻拌**成半湿状态」→ 蒸箱 15min；「美式薯条#1 铺在蒸烤架上放入机器中间层」→ 蒸箱；
柠檬干/广式月饼同理。唯一收益（麻辣对虾#3 的蒸箱）**不加也能拿到**（"炒香后的"里"炒"放行）。
⇒ **只摘完成态引用，绝不另外放宽 `need_fb` 的触发面。**

### 备菜前置（`scheduler/prep.py`）

1. **判据必须只有一份**：`engine._build_tasks` 曾**内联复制**过一份旧判据，已删，改调 `prep_keys`。
   护栏 `test_greedy_track_shares_the_prep_rule`（源码断言：`prep_keys(` 必须在、旧表达式必须不在）。
2. **hard 种子做反向前缀闭包**（把祖先拉进来）：
   旧判据 `(hard or soft) and all(前置已在 prep 里)` ⇒ 超长等待只要前面挂着一个普通短步骤就永远进不了 prep。
   · 血糯米仙草奶茶#6（240min 冷却）前置 #5 是 2min 人工 ⇒ makespan 284 / LB 42 / **gap 576%**
   · 抹茶红豆白玉卷 #2(180min)+#9(120min) ⇒ `prep_keys` **整个为空**，300min 冷藏全留当天
3. **soft 种子必须保守**（不做闭包）：对 soft 也闭包 ⇒ **25/100 道菜整道推进 t<0**、
   平均 prep 占比 44%→**70%**，`makespan`（只统计 `start>=0`）从 79 **崩到 36** = 刷指标。
4. **`PREP_AHEAD_MIN` 90 → 30**：90 把官方 7 条 40~60min 的腌制/冷藏/泡发（腌制1小时×3、
   冷藏1小时×2、泡发40分钟、常温静置1小时）全挡在当天，而它们恰恰是 `is_hard_prep` 的语义，
   没有一条是设备烹饪步。最差用例 lb_probe#4 就是被「金银蒜蒸鸡胸肉#3 腌制1小时」顶出来的。
   ⚠ **别再降到 20**：会把「冷藏30分钟即可食用」这类**末步收尾**也推进 t<0，
   整道菜消失的从 2 道涨到 5 道（雪媚娘/黑森林蛋糕/皂角银耳羹…）。
   30 安全是因为解析器给"无显式时长冰箱步"的默认值**正好 30**，而判据是**严格大于**。
   阈值扫描（三种子 42/11·7/3·202/77，`.cache/_thresh.py`）：
   90 → 总时长 68.3/81.6/84.0；45 → 66.0/77.3/82.1；**30 → 63.7/76.6/81.3**；20 → 整道进 prep 5/100。

### 净效果

| 指标 | 改前 | 改后 |
|---|---|---|
| bench 总时长 | 82.0 | **63.7** |
| bench 连续手动 | 27.2 | **26.3** |
| bench 出锅差中位 | 2.0 | 2.0 |
| lb_probe gap 中位 | 38.6% | **28.6%** |
| lb_probe gap 均值 | 55.0% | **32.8%** |
| lb_probe gap p90 | 100% | **87.5%** |
| lb_probe **gap max** | **576.2%** | **100.0%** |
| 占加热设备步数 | 230 | **210** |
| 有加热设备的菜 | 99/100 | 99/100 |

护栏 `pytest 370 → 381`；`step_provenance` 0 违规；`audit_doc` 9/9；`_mutate_web_guards` 89/89。
新增 13 条护栏（8 条解析 + 5 条备菜/调度）。

---

## §C-补2　下界口径与保热基准（2026-10-04 晚，指令 F 续）

### C2.1 `lower_bound_from_plan` 的两个**静默**缺陷

**缺陷 ①（键类型错配 → 依赖边全丢）**
```python
# 旧
"deps": [tuple(d.split("#")) for d in (t.depends_on or [])]
```
`ScheduledTask.step_id` 是 `int`，`str.split("#")` 产出**字符串** ⇒ 键 `("菜","3")` vs `("菜",3)`
⇒ `_critical_path` 内 `[d for d in deps if d in dur]` **恒为空** ⇒ 关键路退化成"最长单步时长"。
实测 5 菜实例（三味蒸鳗鱼+油醋汁剁肉饼蒸和乐蟹+香辣烤小龙虾+红豆糯米糕+金银蒜蒸鸡胸肉）：
真实活跃关键路 **54**（=makespan）报成 **18** ⇒ LB 27 ⇒ **gap 0% 虚报成 100%**。
修复：`model._dep_key()`，`int()` 化 step_id，异常不抛。

**缺陷 ②（`等待`步被排除出关键路）**
`t.unit_id not in ("等待","提前准备")` 一刀切。理由（不占机器/人手）对 **批次机产能/人工总工时**
成立，对**关键路径不成立**：腌制/浸泡/放凉是**真实流逝时间**且是依赖链节点。
三味蒸鳗鱼真实活跃关键路 54，摘掉等待只剩 25。
修复：拆成 `live`（含等待 → `_critical_path`）与 `live 去掉等待/提前准备`（→ energy/hand）。

**为什么长期潜伏**：三条旧护栏**结构上**抓不到 ——
| 护栏 | 为什么抓不到 |
|---|---|
| `test_critical_path_parallel_branches_not_sum` | 自造 dict，键类型**本来就匹配**，走不到键转换那行 |
| `test_lower_bound_from_plan_never_exceeds_makespan` | 只断言 `lb <= makespan`，下界被低估时**恒真** |
| `lb_probe` | 只报数，从不与"手算关键路"对照 |

**修后真实最优性**（`lb_probe 60 seed=11`）：gap 中位 **0.0%** / 均值 **2.2%** / p90 8.7% / max **34.8%**，
**43/60（72%）makespan 恰好等于可证下界**，0 违反。剩余 gap 来源：
① 下界忽略机器"最早可开工时刻"（#15：烤箱容量 1，饼干最早 45 分进炉 ⇒ 45+48=93 才是最优，LB 只算 48）；
② 目标以 Γ 为第一优先，会主动牺牲 makespan。

### C2.2 保热/冷等待基准：`完成时刻` vs `出锅时刻`

`dish_finish[dish] = 该菜所有 t≥0 任务的 max end`，同时喂给 ① Γ（评分口径）② 冷等待/保热惩罚。
把 1 分钟收尾步（"尽情品尝吧"/"撒葱花"）停在末端 ⇒ `F_d == T` ⇒ `cold = 0` ⇒ **惩罚被完全掩掉**，
求解器**被奖励**"早做熟、干等到最后"。实测 60 组：报告 Γ 与真实烹饪跨度差额 **中位 23.0 / 均值 30.2 / max 175**，
49/60 组 >5min。典型 #55（酿苦瓜+手工鸡蛋豆腐+原味墨西哥面包）：两道蒸菜真实 8/16min 出锅，
两条"尽情品尝吧"停在 190-192 ⇒ 报 **Γ=2.0，菜实际凉了 177min**。

**两处结论相反，都量过：**

| 位置 | 改法 | 效果 |
|---|---|---|
| `model.solve_exact` 的 `cold/over` | 基准改 `F_cook` | 差额中位 **23.0 → 7.5**，**零指标代价**（<5min 90.3% 不变，总时长 63.6） |
| `objective.score_plan` 的保热项 | **刻意不改** | 改了 <5min **90.3% → 80.0%**（6 seed 全部下降） |

原因：`model` 里 cold 是**字典序最低层**（排在 Γ、T 之后），改基准不跟 Γ 抢；
`score_plan` 里它是**无界加权项**，等 170min ⇒ `30×0.9×170 ≈ 4590` ≫ `w_gap·Γ = 180`，
**策略择优被整个改写**。要做"保热方向盘"只能给 `over` 加**上界**，不是换基准。

### C2.3 本轮踩的"静默降级"（自己造成的，被 pytest 抓住）

给 `model.py` 加 `FT_COLD_BASE` 开关时写了 `os.environ`，但**该文件没有 `import os`**
⇒ `solve_exact` 抛 `NameError` ⇒ 被 `engine.schedule` 的 `except Exception: pass` **静默吞掉**
⇒ 精确轨整个失效（bench 总时长 63.9 → 72.5，看起来只是"变差一点"）。
**是 pytest 抓到的**（`test_model.py` 里有直接调 `solve_exact` 的用例）。
⇒ **纪律：给"被 try/except 包住"的模块加新全局依赖时，必须有一条直接调用它的测试。**

### C2.4 新增护栏（8 条，全部**真的去调被测代码**）

`tests/test_model.py` §5–§6：
`test_dep_key_normalizes_step_id_to_int` ·
`test_lower_bound_critical_path_follows_real_edges`（合成链 5→7→2，断言 lb_cp==14 而非 10）·
`test_lower_bound_counts_wait_steps_in_critical_path`（断言 lb_cp==46 且 lb_hand 不含等待）·
`test_lower_bound_gap_is_meaningful_on_real_recipes`（真实菜谱 gap 中位 ≤60%）·
`test_dish_cook_finish_excludes_trailing_steps` · `test_dish_cook_finish_falls_back_for_cold_dishes` ·
`test_cold_penalty_in_score_plan_stays_subordinate` · `test_exact_model_uses_cook_finish_for_cold`。

⚠ 最后两条是**源码级**断言。写的时候踩了一次"子串断言"坑：注释里写了 `dish_cook_finish` 这个名字，
直接对全文断言会被**自己的注释**误伤 ⇒ 必须**先剥掉 `#` 注释**再断言（同 §C 的 `_strip_js_comments`）。

### C2.5 已知未修（下一步候选）

1. **`priors._kind_of` 只看菜名 + 加热步的 mode/原文**，被"炒熟糯米粉"这类**辅料处理**带偏：
   `雪媚娘`（冰皮冷甜品）被判 `主导工序=fry` ⇒ `serve_hot=0.95 / hold_min=6`。影响保热惩罚。
2. **`objective.score_plan` 的保热项无界**：`w_hot·serve_hot·over` 可以压过 `w_gap·Γ`。
   正确修法是给 `over` 加上界（如 `min(over, hold_cap)`），让保热成为"方向盘但不夺权"。
3. `dist/cooking-agent-windows.zip` 尚未重打包（本轮解析/调度改动未进包）。

### C2.6 跨午夜 HH:mm 回绕 → "偶发红一次"的护栏（2026-10-04）

`adapter.hhmm(offset) = (now + timedelta(minutes=offset)).strftime("%H:%M")`。
`now=23:10` 排 50 分钟 ⇒ **"00:00"**。而 `test_official_api_spec_conformance` 的"要求3"裸比分钟值：
`max_end = max(mm(endTime)) = 1432` vs `mm(finishTime) = 0` ⇒ 假报违规。

- **复现窗口** = "now 到午夜"短于计划跨度时：`23:10` 时 8 组里 **1 组**中招；
  `22:50`/`23:00`/`23:55` 为 0 ⇒ 只在临近午夜出现 ⇒ 表现为**随机红一次**。
- **修法**：环形差 —— `dish_end <= makespan` ⇒ `(mm(finish) − mm(end)) % 1440 == makespan − end <= makespan`。
  对回绕免疫，且**不改口径**（HH:mm 表达不了"次日"，语义上 00:00 确实晚于 23:52，错的是比较方式）。
- **新护栏** `test_api_times_are_rollover_safe`：`monkeypatch.setattr(adapter, "datetime", _FakeDT)` 钉在 23:10，让场景**必现**。
- **验证**：修复后全量在 **23:11 本地时间**跑过 `390 passed` —— 正是原来会红的时刻。

⚠ 这条把 §C 的"三条铁律"扩成**四条**：**依赖墙上时钟的护栏 = 定时炸弹**。
"偶发红一次"比"一直红"更危险 —— 它会训练人忽略红灯。

## §C-补3　语义先验 v2.3 与保热预算（2026-10-04 深夜，指令 F 收尾）

> 本轮修掉了 §C2.5 记的两个候选（先验主导工序误判、保热项无界），
> 并把"审计文档假红"一起治了。**全部先量后改**，三个结论都反直觉。

### C3.1 `priors._kind_of` 重写：按"上桌状态"判，不按"有没有炒"

**旧实现把 72/100 道菜判成 `fry`**（趁热 0.95 / 可放 6 分）。两条根因，都不报错、只让先验静默失真：

| 根因 | 表现 |
|---|---|
| `_FRY` 收了「烤\|酥」 | **任何**烤箱烘焙落进 fry ⇒ 黑森林蛋糕/糖霜饼干/广式月饼/红豆糯米糕 全被判"6 分钟内必须吃掉" |
| `has_fry` 对**任意一个**灶具/烤箱步生效 | 辅料处理/收尾淋油也能定调 |

受害者（都是"末步灶具动作盖过真实主导设备"）：
- `雪媚娘` #1「准备熟糯米粉：炒锅慢火炒至微黄色」⇒ 一道**冰皮冷甜品**被判爆炒；
- `清蒸鲈鱼` #5「烧热油淋在鱼身上」⇒ **蒸菜**被判爆炒（蒸箱被一个淋油步盖过）；
- `竹荪排骨汤` ⇒ 汤被判爆炒（hold 45 → 6）。

**新判据：三个信号，依次为**

```python
# ① 上桌状态: 末道**设备**工序是冷藏/冷冻 ⇒ cold
if _served_cold(steps): return "cold"
# ② 菜名强信号, 顺序即优先级: 冷 > 烘焙 > 蒸 > 汤羹 > 炒炸
for pat, kind in ((_COLD_NAME,"cold"), (_BAKE_NAME,"bake"),
                  (_STEAM_NAME,"steam"), (_SOUP_NAME,"soup"), (_FRY_NAME,"fry")):
    if re.search(pat, name): return kind
# ③ 菜名无信号 -> 加热步的**主导设备** (平票按 蒸箱 > 烤箱 > 灶具)
votes = {...}; return _DEV_KIND[max(_HEAT_DEVS, key=lambda d: votes[d])]
```

⚠ 三个**顺序陷阱**（都是实测踩出来的）：
1. **`蛋羹` 必须先于 `羹` 判** —— 鸡蛋羹是**蒸**出来的，归汤羹会把 hold 从 10 分放宽到 45 分。
2. **`bake` 必须排在 `steam`/`fry` 之前** —— 否则"烤"字进不了烘焙档。
3. **`cold` 排最前** —— 冰皮月饼名字里有"月饼"（bake 词），但它上桌是冷的。

**`_served_cold` 的两个严判据**（都要满足，缺一即漏）：
```python
if last_i is None or steps[last_i].device != DEVICE_FRIDGE: return False   # 末设备步必须是冰箱
if "趁热" in (steps[last_i].raw_text or ""): return False                  # 原文说趁热 ⇒ 不是冷食
return all(s.prep_type == PLATE for s in steps[last_i + 1:])               # 冰箱后只剩上桌
```
- 少第一条 ⇒「玛格丽特 #2 冷藏面团」后面还有烘烤，被误判 cold；
- 少第二条 ⇒「皂角银耳羹 #4 蒸好后…趁热或冷藏后食用」一句里同时给了冷热两种吃法；
- 少第三条 ⇒「冷藏醒面/冷冻定型」这类**过程**冷藏被当成上桌状态。

**效果**：`fry 72 / steam 18 / cold 3 / other 7` → `fry 13 / bake 36 / steam 39 / cold 6 / soup 6`（新增 `bake` 档 0.75/hold20）。
⚠ **诚实量化：对排程指标几乎无影响**（`<5min` 90.3%→90.3%、总时长 68.75→68.82、连续手动 25.40→25.72，全在噪声内）。
**价值在语义正确**（`reason` 字段、`notifications` 的趁热提醒、答辩可解释性），不在分数。

顺带删掉旧实现里 **4 个重复、永远走不到**的 `return`（死代码）。

### C3.2 保热项：从"没用"和"夺权"两个坏形态，改成"以 Γ 计价的方向盘"

`objective.score_plan` 的保热项两个口径决策，**都是量过才定的**：

**① 基准用 `dish_cook_finish`（出锅时刻），不用 `plan.dish_finish`（完成时刻）**
用完成时刻时这一项**恒 ≈ 0**（等于从来没参与择优）—— 把 1 分钟收尾步「尽情品尝吧」停在末端，
完成时刻就等于 T，惩罚被完全掩掉，求解器**被奖励**"早做熟、干等到最后"。60 组随机实例中
报告 Γ 与真实烹饪跨度差 >5min 的有 **49 组**（中位 23，最坏 175）。典型 #55：两道蒸菜真实
8/16min 出锅，两条"尽情品尝吧"停在 190-192 ⇒ 报 Γ=2.0，菜实际凉了 177min。
（`FT_HOT_BASE=finish` 可退回旧口径，供 A/B。）

**② 但必须同时给它总预算**，否则从"没用"直接变"夺权"：它是个无界加权项，一道菜等 170min
⇒ `30×0.9×170 ≈ 4590`，而 `w_gap·Γ` 只有 `60×3 = 180` ⇒ 策略择优被整个改写。
预算**以"分钟的 Γ"计价**：`min(hot, w_gap × FT_HOT_BUDGET)` —— 语义清晰："保热最多花掉 N 分钟的 Γ"。

**默认 0.5 是扫出来的**（4 seed × 60 组 lb_probe）：

| budget | makespan==可证下界 | gap 均值 | 保热违规 | `<5min` |
|---|---|---|---|---|
| 旧口径（完成时刻，无界） | 65.0% | 3.43 | 3.68 | 90.3% |
| 0 | 65.8% | 3.18 | 3.68 | 90.3% |
| **0.5 ← 采用** | **65.0%** | **3.23** | **~3.5** | **90.3%** |
| 1 | 64.5% | 3.43 | 3.38 | 90.3% |
| 2 | 62.8% | 3.60 | 3.40 | 90.3% |
| 4 | 62.0% | 3.75 | 3.17 | 90.3% |
| off（无界） | — | — | 1.04 | 80.7% |

**读法：出锅差 Γ 对 `[0, 4]` 整个区间免疫**（评分表主项，必须是平的）；但预算一大就开始用
makespan 换保热 —— 2 已经掉了 2pp 的"makespan==可证下界"。**0.5 = 三条轴上都不劣于旧口径的最小值。**

⚠ **第一版修法（per-dish `over` 上界 `FT_HOT_CAP`）不够**：实测 CAP=5/10/20 只把 `<5min`
救回 89.3/88.3/86.7%，**仍低于旧口径 90.3%** —— per-dish 上界管不住"多道菜累加"。改成按 Γ 计价的总预算才成立。

**更本质的发现（下一步的方向）**：预算从 0 加到 4，**出锅差完全不动** ⇒ 6 条策略 + 3 轨给出的
候选**彼此差得很远**，几乎没有"Γ 接近"的候选对可供挑选 ⇒ **语义先验在这一层只能微调；
要让它真正参与决策，方向是精解轨做局部搜索（邻域再生成候选），不是继续调系数。**

### C3.3 审计文档假红：**前提会随被测代码变好而失效**

`tests/test_audit_doc.py::test_the_false_alarm_is_reachable` 把"能造出跨零点场景"的时刻
**钉死 22:00**。本轮保热口径一改、排程变短，22:00 从"3 条旧口径误报"退化成"0 个跨零点场景"
⇒ 测试失败（`assert 0 > 0`）。**不是审计坏了，是排程变好了。**

**修法**：`_TRIGGER` → `23:15`（重扫实测最优：跨零点 10 组 / 旧口径误报 4 条）
\+ `_TRIGGER_CANDIDATES`（23:15/23:30/23:45/23:00/22:30/22:00/21:00）+ `_trigger()` **先试首选、
失败自动扫候选池**，最终仍断言前提可达（**前提消失仍报错，不静默跳过**）。

⚠ **这是第五条铁律**：**断言"某个前提成立"的测试，前提本身会随被测代码变好而失效。**
把前提写成"在候选集里搜一个满足的"，而不是"钉死一个"，否则每次优化都要来修它。
（与第四条"依赖墙上时钟 = 定时炸弹"同源：都是**把易变量写成了常量**。）

### C3.4 净效果（本轮）

- `pytest 390 → 398`（+7 主导工序 +2 保热预算 −1 替换）。
- `bench 50`(seed42)：出锅差 **2.0** / 时长 **63.2** / 手动 24.6 / 冲突 0。
- `lb_probe 60/11`：gap 中位 **0.0%** / 均值 **3.1%** / max 41.7% / **41-60（68%）== 下界** / 违反 0。
- `step_provenance` 0 违规 · `audit_doc` 9/9 · `test_deploy_windows` 42 · `_mutate_deploy_guards` 29/29。
- `dist/cooking-agent-windows.zip` 重打包：**69 条目 / 401374 字节 / sha256 `0f27d806838e28f1`**，
  0 反斜杠条目、无密钥、含 `deploy/windows/*.ps1`。

## §C-补4　最优性证书与三个"看起来像漏洞"的东西（2026-10-04 第三轮，指令 F）

### C4.1 可证最优率 ~98–100%（这是最强的判据）

`model.py:751` `plan.solver_status = "OPTIMAL" if st == OPTIMAL else "FEASIBLE"` 是**真状态**。
生产 1.5s 预算实测：**3–5 菜 100/100 OPTIMAL**、**6–9 菜 60/60 OPTIMAL**。
证书可信：模型自报 (Γ,T) 与独立 `evaluate()` **逐位一致**、`check_plan` 全可行（`_cert_verify.py`）。
⚠ 探针里 `random.seed` 必须在**预算循环之前**每次重设，否则两个预算跑的是不同实例集
（曾误得"30s 比 1.5s 差"的假象）。

### C4.2 公平版预算探针（`.cache/_opt_fair.py`）

旧 `_opt_probe.py` 的"足预算解"用**默认单策略**，生产是"6 策略 + 三轨" ⇒ 候选集不同，
测出的是**策略选择**收益。公平版把两边钉在**同一策略**只放大预算：
**20 组 dT=dG=0.0 逐位相同，0/20 有改进** ⇒ 预算不是瓶颈。

### C4.3 Γ 的独立下界（新增 `model.gamma_lb_from_plan`）

> **Γ_lb = Σd − max d**，d 取同一**容量 1** 资源上各菜**末步**时长。

**三条必须排除**（漏一条就造出"下界 > 真实 Γ"的假下界）：
① **蒸箱** —— `unit_id` 是**腔体槽位**不是机器（同温无限共享）；
② **容量 > 1**（灶具 = 2 灶眼）；③ **`等待`/`提前准备`**（不占资源）。
已接进 `scripts/lb_probe.py` + 4 条护栏（`tests/test_model.py` §6）。
实测 60 组：**68% 的实例 Γ 恰好等于下界**，`Γ−Γ_lb` 中位 0.0 / 均值 0.5 / max 7 / 违反 0。

### C4.4 三条"看着像漏洞、量完不是"的

- **ε 约束方向反了？**（先压 T、再在 `T≤T*+15` 内压 Γ，评分表却是 Γ 主序）→ **不成立**：
  `FT_EXACT_SLACK` 15→999 最终指标**逐项完全相同**。多轨 + `score_plan`(60:1) 已补掉：
  `solve_exact` 在 #15 单独给 Γ=24，而**贪心给 Γ=1**，择优自然选贪心。
- **灶具平票永远选 `灶具-1`？**（`_ResourceTimeline.earliest_slot` 平票取 `u=0`）→ **无害**：
  平票 = 两个灶眼同时空闲 ⇒ 放哪个都不影响后续可达性。
- **择优用加权和而非 (Γ,T) 字典序？** → **20/20 一致**（`_pick_gap.py`）。

### C4.5 ⚠ 肉眼看甘特图最容易漏的耦合：**人工单资源**

我手工分析 #10 断言"把 `清蒸带鱼` 淋油 35-38 挪到 41-44（用空闲 `灶具-2`）能把 Γ 从 7 降到 4"
—— **错的**。灶具步**全都 `needs_human=Y`**，而**人工容量 = 1**，三个 3 分钟灶具步只能串行，
挪动会挤掉别的菜并顺延收尾。模型报 OPTIMAL、slack 0→60 都不变 ⇒ **Γ=7 就是真最优**。
**任何"把某步挪到另一个设备就能更好"的手工推断，必须先查那一步是否占人工。**

### C4.6 三个潜伏问题（当前规模不触发）

1. **蒸箱容量两套口径**：`engine.RESOURCE_UNITS[蒸箱] = 1`（一个腔体）vs
   `model.capacities()[蒸箱] = BATCH_Q[蒸箱] = 4`。引擎 `_temp_share_fit` 对同温任务
   **永不阻挡**（并行无上限），模型上限 4，`checker.overlap_allowed` 只查温度**不查数量**。
   实测 60 组最大同温并行 **4 盘 / 0 超限 / 0 异温并行** ⇒ 未触发。
2. **`solve_exact` 在 ε 卡紧时可能不如贪心**（#15：exact Γ=24 / 贪心 Γ=1）—— 被多轨掩盖。
3. **`plan.solver_status` 未对外暴露**（只在 `model.py` 内部 meta 与 `verify_model.py`）。
   "已证明最优"这个最强声明 API 看不到。

### C4.7 已量化但**未启用**的开关：`FT_HAND_CAP`

"连续手动操作时长"是 25 分项之一，但 `FT_HAND_CAP` 默认未设 ⇒ 精确模型**不优化它**。
实测 `FT_HAND_CAP=30`：hand 均值 **26.2 → 22.5（−14%）**、max **86 → 75**，**Γ/T 完全不动**。
代价：模型变大（60 组×5 cap 跑 5m36s），且 **8/40 组最终仍超 cap**（降级或别的轨胜出）。
生产延迟 131ms/p95 274ms（优秀线 5s）⇒ 延迟不是障碍。**本轮不动默认**，留档待决。

## §C-补5　精解轨预算保底：一个"只在进程启动后第一次出现"的真 bug（2026-10-05，指令 F 第四轮）

### C5.1 现象：同 4 菜，第 1 次 Γ=32/T=83，第 2 次 Γ=3/T=57

**不是算法次优，是整条质量最好的轨被静默跳过。**

`orchestrator._run_inner` 的顺序：LLM 塑形 → LLM 提议 → 贪心枚举 → 精解轨。
精解轨预算来自"剩余时间"：

```python
left = max(0.0, time_budget - (time.time() - t0))
bud  = solver_budgets(len(recipes), left * 0.75)
```

在线时 LLM 两次调用实测吃光 `time_budget=3.6s` ⇒ `left≈0` ⇒ `solver_budgets(4, 0)`
两条精解轨全 `use_*=False` ⇒ **精解段整块跳过，交付贪心解**。

⚠ **在线闸门（`FT_LLM_ONLINE_GATE`）是本轮结束后才落的** ⇒ 每次进程启动后的
**第一次**请求都中招，之后才恢复 —— 这就是"第 1 次差、第 2 次好"的来源。
⚠ 量级最大：第三轨净收益 **T −21.6% / Γ −32.0%**。"LLM 慢"不该升级成"丢掉最好的轨"。

### C5.2 修复：预算从**后往前**分配（不是先到先得）

| 环节 | 预算 | 默认 3.6s 下 |
| --- | --- | --- |
| 精解轨（CP-SAT + 精确模型） | `_solver_reserve(n, use_cpsat)` | **0.761s** |
| LLM（塑形 + 提议） | `llm_quota = time_budget − reserve − _ENUM_RESERVE` | 2.239s |
| 策略枚举（6 条贪心快评） | `_ENUM_RESERVE` | 0.600s |

1. `dispatch.min_pool_for_tracks(n, want_cpsat=, want_exact=)` —— **二分调用
   `solver_budgets` 本身**反解最小池，**不要照抄 0.45/0.55 公式**（两处各写一份必然漂移；
   本项目前科：备菜判据三轨各写各的 ⇒ 精解轨刷 +54% 虚高）。**逐轨分别求再取 max**，
   所以 `FT_EXACT=off` 不会连带丢掉 CP-SAT 的预留。返回 **0**（不是 `inf`）表示该 N 下
   该轨本就不开（N=40：`1.2×6/40=0.18 < MIN_USEFUL`）⇒ 无需预留。
2. `orchestrator._solver_reserve()` = `min_pool / 0.75 + 0.02`。
   ⚠ **必须加 2% 余量**：`solver_budgets` 判据是 `>=`，浮点差 1ulp 就整轨关。
3. `_run_inner` 三处各守其位：LLM 的 timeout/剩余 → `llm_quota`；枚举 break → `llm_budget`；
   **精解段的 `left` 仍用 `time_budget`** ⇒ 走到那里一定 `≥ reserve`。

**连带缺陷**：`propose_strategies` 的 `max(0.5, …)` 下限会让 LLM **超支**
（剩余 −0.001s 也照样给 0.5s）⇒ "预留 0.761s"实际只剩 0.26s。
改为"剩余 < `_LLM_MIN_ROUNDTRIP`(0.5) 就**跳过**提议、直接枚举全策略"。

### C5.3 量化（4 菜 seed=11，`time_budget=3.6s`）

| | Γ | T | 耗时 | 精解轨 |
| --- | --- | --- | --- | --- |
| 离线基准 | 1.0 | 37.0 | — | 跑 |
| 修复前·慢模型首请求 | **8.0** | **52.0** | 4.10s | **跳过** |
| 修复后·慢模型首请求 | **1.0** | **37.0** | **2.29s** | 跑（`+cpsat`） |

修复后**逐位等于离线基准**，而且**更快**（跳过了注定落空的提议往返）。
反向回归：模型快（200ms）时塑形仍命中（`policy.source=="llm"`）、提议仍生效
（trace 只 3 条候选）、精解轨仍跑，耗时 0.51s。

### C5.4 护栏与变异（`tests/test_agent_budget.py`，**24 条**）

反解口径必须**紧**（给足就开、少 1% 就关）· N=40 返回 0 而非 inf · `FT_EXACT=off`
时仍为 CP-SAT 预留 · N≤20 全部生产规模 `reserve×0.75` 后两轨都开 · **端到端核心回归**
（精解轨在跑 + 枚举跑完 6 条 + Γ/T 不劣于同预算离线）· 结构性算术断言
（LLM 与枚举都用满时精解池仍够）。

变异（临时改回缺陷，看护栏是否红）**三条全中**：
① `_solver_reserve` 恒 0 → 11 红（含核心回归）；
② 精解段 `left` 误用 `llm_budget` → 核心回归红；
③ `_ENUM_RESERVE=0` → 核心回归红（枚举断言）。

### C5.5 教训

**"从后往前分配预算"适用于一切"关键阶段 + 可选前置阶段"的组合。**
本项目的形态是：精解轨（关键，质量杠杆最大）在最后，LLM（可选，且会静默超时）
在前面。任何"先到先得"的预算都会让前面的可选阶段把后面的关键阶段饿死 ——
而且**不报错**，只体现在"结果差了一点"，正是最难发现的一类回归。

---

## §C-补6　甘特图随机抽查查出的两个真缺陷（2026-10-06，指令"抽查甘特图是否准确"）

### C6.0 方法：先证明"审计会响"，再拿它查

新建 `.cache/_gantt_audit.py`（**独立**校验 10 项，不复用被测代码）：
时长/区间自洽 · 单位重叠（含蒸箱同温 ±15℃ / 预热共享 ±10℃ 豁免）· 依赖边 ·
锚点与 t<0 纯净性 · **独立重算 T/Γ 对账 KPI** · 步骤连续号 · 声明步数 vs 解析步数
（**必须排除合成预热节点**，`insert_preheat_steps` 本就重编号）· 凭空任务 ·
device/unit 自洽 · 应用自检与独立结论是否打架。

**先做变异自检**：注入 10 类已知缺陷（重叠/依赖倒挂/锚点漂移/t<0 资源任务/时长不符/
步骤缺号/删末步/凭空任务/device-unit 矛盾/KPI 时长口径），**10/10 必须命中**才用它查。
> 其中一条变异抓出"护栏挂错入口"：`test_no_resource_task_before_day_start` 初版
> 裸调 `engine.schedule()` **复现不出**缺陷（走的是另一组策略集）⇒ 必须走
> `adapter.plan_recipes`（Agent 闭环 = 线上真实路径）。

配套：`.cache/_gantt_scan100.py`（全量 100 道扫 [A] 资源任务 t<0 / [B] 整道无资源任务 /
[C] 首个资源任务 != 0）· `.cache/_parser_scan.py`（解析器体检）。

### C6.1 缺陷 A：**占设备/占人工的步骤被排到 t<0**（8/100 道）

- **现象**：`低温牛排#2` 蒸箱 −2→6、`芋头蛋卷#2` 蒸箱 −2→6、`血糯米仙草奶茶#8` 蒸箱 −2→6、
  `麻辣对虾#4` 双手 −15、`金龙吐瑞#4` 双手 −15、`轻松一锅蒸#3` 双手 −10、
  `金银蒜蒸鸡胸肉#4` 双手 −60、`油醋汁剁肉饼蒸和乐蟹#6` 蒸箱 −2→3。
- **根因**：备菜步在 `_build_tasks` 里整体**右对齐到 0**（`cur = -sum(dur)`）⇒ 其 `end_min`
  是**负数**。贪心的 `dep_ready = max(done[d].end_min for d in depends_on)` 若前置恰好是
  一个结束在 −2 的备菜步，`dep_ready` 就是 −2 ⇒ 把这条**占设备/占人工**的步排到 t<0。
- **后果（静默）**：`_to_plan` 的 `cooking = [t for t in tasks if t.start_min >= 0]` ⇒
  这些任务**既不计入 T/Γ、也不进 `check_plan`** —— 甘特图上"设备昨天就在干活"，
  指标却看不见。
- **修复**：`dep_ready = max(0.0, max((done[d].end_min for d in t["depends_on"]), default=0.0))`。
  影响 **8/100 → 0**。
- **护栏**：`test_no_resource_task_before_day_start`（用例 `["低温牛排","金银蒜蒸鸡胸肉","芋头蛋卷"]`，
  **必须走 `adapter.plan_recipes`**）。

### C6.2 缺陷 B：**复合设备继承被灶具步挡住**（`玛格丽特`）

- **现象**：`玛格丽特` 当天只剩 3 分钟"晾凉"，T=3.0、利用率 **0%**，
  **烘烤段在甘特图上根本不存在**。
- **根因**：`app/parser/recipe_parser.py` 复合设备继承的守卫写的是
  `if not any(s.device in HEATING_DEVICES ...)`，而 `HEATING_DEVICES = (灶具, 蒸箱, 烤箱)`
  **含灶具** ⇒ 菜里**只要有一个灶具步**
  （哪怕只是"煮鸡蛋 15 分钟"），蒸烤一体机的继承就整段跳过，真正的烘烤步停在"人工" ⇒
  被 `rule_prior`（`device==DEVICE_NONE and not unattended` 一律算备菜）当备菜 ⇒
  整道 4 步落进 t<0。**判据自相矛盾**：同一句在**没有灶具步**的菜里却能判成烤箱。
- **修复**：守卫改为只看 `(DEVICE_STEAMER, DEVICE_OVEN)`。影响 **+2 步**
  （`玛格丽特#4`、`芝士番茄土豆泥#12`，均 `device_source='(复合设备推断)'`，
  原文可印证）⇒ `heat_steps` 指纹 **210 → 212**；修复后 `玛格丽特` T=3.0→**11.0**、
  利用率 0→**18.2%**。
- **护栏**：`test_combo_device_inheritance_survives_stove_step`（用例
  `{"玛格丽特":"烤箱","芝士番茄土豆泥":"烤箱"}`，断言该菜含灶具步**且**烤箱步没被挡掉）。

### C6.3 排除的假警报

`原味墨西哥面包` T=190min 且**无独立烘烤步** —— 核对原文确认菜谱本身**只有 7 步**、
190min 来自两次发酵（120+65），排程**忠实**。这类"看起来不合理"要回原文核，不能凭直觉报缺陷。

### C6.4 环境：本机 `http_proxy` **劫持 127.0.0.1**

本机设了 `http_proxy=http://127.0.0.1:64582`（沙箱代理）⇒ urllib/curl 默认走它，
**服务不在时返回 502**（不是 ConnectionRefused），把真实原因掩盖成"服务挂了"。
排查本地超时/502 前先 `env | grep -i proxy`。处置：探针脚本用
`urllib.request.build_opener(urllib.request.ProxyHandler({}))`，curl 加 `--noproxy '*'`。

### C6.5 教训：**改源码后必须重启 uvicorn**

用户报"还是前八分钟都空着"，根因是服务进程 **09:27:56** 启动、`engine.py` **10:30** 才改
⇒ 界面跑的是**旧代码**。判据：`Get-CimInstance Win32_Process` 看 uvicorn 的启动时间
（`wmic.exe` 被本机黑名单拦截）。**改完必须 kill + 重启再验收**，否则"修复没生效"的
假象会浪费一整轮。

---

## §C-补7　菜谱"第N层"的建模口径（2026-10-06 问答）

**问题**：菜谱里"放入烤箱第3层""蒸烤架放第1层、蒸烤盘放第3层"这类步骤，
**会不会在甘特图上表现出来**？

**答**：**不会成为独立的行/块**。甘特图的行维度是 `unit_id`（设备资源单元），
**没有"层"这一维**。三条链路逐一核实：

1. **解析层**：`层` 只被当作**判断这台机器是蒸还是烤**的信号
   （`_COMBO_HINT` 含 `第N层|中间层`，见 `recipe_parser.combo_device_of`），
   **不生成独立步骤、不生成独立工位**。而且曾试过把 `水箱|加水|第N层|中间层`
   加进 `need_fb` 触发面，**净负收益**（"美式薯条 铺在蒸烤架上，放入机器中间层"
   被判成蒸箱 15min，其实是烤）⇒ 已回退，**别再放宽**。
2. **调度层**：`engine.RESOURCE_UNITS = {灶具:2, 蒸箱:1, 烤箱:1, 冰箱:1, 人工:1}`
   —— "层"不是资源。精确模型另有批次容量 `model.BATCH_Q = {蒸箱:4, 烤箱:1}`。
3. **渲染层**：`index.html::renderSwim()` 按 `unit_id` 分组建 `.lane`；
   三种视图 = `device`(默认,按 unit_id) / `dish`(按菜) / `hand`(人工·无人值守)。
   **都没有"层"**。

**实测（`三味蒸鳗鱼&香茅烤鳗鱼同烹` #12、`轻松一锅蒸` #11、`鸡仔饼` #5）**：

| 菜 | 原文 | 解析结果 |
| --- | --- | --- |
| 三味蒸鳗鱼&香茅烤鳗鱼同烹 #12 | 蒸烤架放**第1层**，蒸烤盘放**第3层** | **1 个任务** `蒸箱-1 35→43` |
| 轻松一锅蒸 #11 | 蒸烤架放**第1层**，蒸烤盘放**第3层** | **1 个任务** `蒸箱-1 42→57` |
| 鸡仔饼 #5 | 把烤盘放入**第3层** | **1 个任务** `烤箱-1 0→8` |

⇒ 一句话里两个层被**合并成一个块**；`raw_text` 仍保留原文，所以**悬停提示
（`title`）与"任务明细"的「操作步骤」列**里还能看到"第3层"——**层信息只是"原文可见"，
不参与调度、不成行**。

**两个容易误读的点**：

- **烤箱恒为 1 行**：`BATCH_Q[烤箱]=1` 且生产路径不传 `oven_q` ⇒ 永远只有 `烤箱-1`。
- **蒸箱最多 4 行（`蒸箱-1..4`）是"腔体槽位"，不是菜谱层号**：
  `model.py` 注释原话——"蒸箱：其 `unit_id` 是**腔体槽位**而非机器（同温可无限共享腔体）"。
  四道蒸菜（酿苦瓜/麻辣对虾/葱香土豆泥/豆豉蒸腐竹）实测 ⇒ `蒸箱-1..4` 各一块，
  **槽位号由求解器分配，与菜谱写的层号无关**。

⚠ **顺带发现一处渲染不一致（未修，待定）**：同温合炉（`checker.overlap_allowed`
蒸箱同温 ±15℃ 豁免）在两条轨上**画法不同**：
- **纯贪心轨 `_schedule()`**：全放 `蒸箱-1` 一行、**区间重叠**（实测 20-35 / 30-45 / 32-47，同为 100℃）；
- **精确模型轨**（`(Γ,T)` 更优、通常胜出）：拆成 `蒸箱-1..4` **四行**。
两者都合法（重叠有豁免），但**同一份输入在甘特图上长得不一样**；贪心那种画法
容易被用户误读成"蒸箱冲突"。若要改，方向是让贪心轨也用槽位（或在 UI 上把
同温共享的块做视觉标记），**属产品决策，未动**。

---

## §C-补8　连续手动上限（C5）重写 + 纯装载句摘设备（2026-10-06 晚，自评 88→92）

> 本节的**短版**在主文件 `MEMORY.md` §二/§四；这里放完整细节。

### 8.1 (C5) 连续手动上限：O(n·H) → O(m) 行程编码

**旧缺陷**：`model.py` (C5) 用**时间索引** `omega_t / cover`（每分钟一个布尔），
规模 `O(|J_hand| · H)`。5 菜实例 = 4725 布尔变量、`#kLinearN: 450 (#terms: 13959)`。
3 s 预算下 **7/8 组解不出** ⇒ 阶段 1 `UNKNOWN` ⇒ `_give_up()` **静默丢弃**该约束
（报出来的"连续手动 54 min"其实**根本没受约束**）。

**新编码的关键观察**：**人工容量 = 1 ⇒ 人工任务天然全序。**
于是不需要时间轴指示变量：
1. 按**参考开工时间**定一个序（`_ord_key`：先验 `hint` 优先，否则 `_est()`）；
2. `s[k+1] >= e[k]`（全序）；
3. 对每个起点 `i` 与每个 `j > i`：段 `[i, j]` 的**总工时 + 空隙 <= B**，
   除非中间存在一次 `> tol` 的**断点**（`brk` 布尔，big-M 线性化）。
   一旦 `段工时 > B` 就 `break`（该起点后面更不可能）。

规模与时间轴 `H` **解耦**，1.5 s 内 OPTIMAL。

⚠ **两个实现坑**：
- **CP-SAT 线性约束只接受整数系数/常数** ⇒ 时长必须 `int(round(t["dur"]))`、
  big-M 也必须 `int(...)`；否则报 `Linear constraints only accept integer values`。
- `B` 的口径必须与 `metrics.hand_streak` **同源** ⇒ 直接
  `from app.scheduler.metrics import HAND_GAP_TOL`，`tol = max(0, int(round(HAND_GAP_TOL))) = 1`。

### 8.2 择优丢弃：约束生效了但结果被换掉

`engine.schedule()` 三轨择优原先**只按 `(Γ, T)` 字典序比较** ⇒
**满足 `hand_cap` 的精确解会被不满足 cap 的贪心解换掉**。
**修法**：`_cap_ok(p)` 可行性优先 + `_better(cand, cur)`：

```python
def _better(cand, cur):
    if hand_cap is not None:
        c_ok, b_ok = _cap_ok(cand), _cap_ok(cur)
        if c_ok != b_ok:
            return c_ok          # 可行优先
    return (cand.finish_gap_min, cand.makespan_min) < (cur.finish_gap_min, cur.makespan_min)
```

⚠ **CP-SAT 轨必须逐字保留原语义**：原逻辑是"Γ 不差过贪心 +1 就采用"
（**不是**字典序，**不比 T**）。`hand_cap is None` 时必须原样走，否则 `verify_model`
从 −21.3% 变 −22.1%（实测踩过）。只有 `hand_cap` 开启时才用 `_better`。

**实测**：`FT_HAND_CAP=30` 超限 **8/40 → 0/40**（max 30.0，修复前 max 97.0）。

### 8.3 纯装载/取放句不占设备

**缺陷**：`"将烤盘放入烤箱中" / "放入烤箱中" / "将装有饼坯的烤盘放入烤箱，关闭好"`
的动作是**取放**，设备并没有开始工作 —— 真正的工作在**相邻的程序句**里
（`"选择'常规烘焙'模式，以170℃烤13分钟"`）。旧实现把它认成烤箱步，而它没写时长 ⇒
`estimate_duration` 补 8 分钟 ⇒ **凭空多占烤箱 8 分钟**（烤箱容量 1，最稀缺资源）。
典型：`黄油小饼干#9/#10 + #11` 三句其实只是**一次 13 分钟烘烤** ⇒ 虚增 10 分钟烤箱占用。

**判据（模块级 `_LOAD_ONLY` / `_PREHEAT_DONE`，五条全成立才摘）**：
① `_LOAD_ONLY` 取放动词（放入/放进/推入/装入/摆入/置于）② 有加热设备
③ **无** `LOAD_START_VERBS`（开启电源/开始烹饪/屏幕提示…）
④ **无** `ACTIVE_COOK`（加热/翻炒/烘烤/煮沸…）⑤ 无显式时长。

⚠ **③④ 必须留**：`"放入方太智能烹饪设备第2层，开启电源，选择开始烹饪按键，按屏幕提示操作"`
同样是"放入"，但设备**真的要跑起来**（只是原文没给时长）—— 摘掉它整道菜会丢主烹饪步。
⚠ **判④前必须先摘 `预热好的` 这类完成态定语**（`_PREHEAT_DONE`），否则 `ACTIVE_COOK`
命中"预热"而误放行（实测 2 条：`半熟芝士蛋糕#10` / `蔬菜塔#3`）。
⚠ **位置**：`is_load_only` 必须在 `need_fb` **之前**用原始 device 算，
但**在 `need_fb` 之后**才清设备 —— 先清则 `device=人工` ⇒ `need_fb` 触发 ⇒
兜底推断又把设备推回来（判据自相矛盾）。

**实测**：官方 100 道菜 **19 条**中招（15 条同菜同设备另有真时长步）。
- 设备工作步 **251 → 232**、时长覆盖率 **50.2% → 54.3%**
- `heat_steps` 指纹 **212 → 193**（含预热节点）、`heat_dishes` 仍 **99/100**
- `bench 50` 总时长 **63.4 → 59.4（−6.3%）**、连续手动 **25.4 → 21.7**、出锅差中位 **2.0 不变**、冲突 0

### 8.4 连带回归：预热节点会跟着消失（自查发现）

摘掉 `"放入预热好的烤箱中"` 的设备后，`insert_preheat_steps` 的条件
`st.device in (DEVICE_OVEN, DEVICE_STEAMER)` 不再成立 ⇒ **预热节点一起消失**
（官方 5 → 2 条）⇒ `audit_doc` 的 **3.1.4（预热为独立任务节点）由 ok 变缺陷**。

**修法**：加 `_preheat_device(st)` —— 若 `st.device` 已是"人工"，
就**从原文 `find_device(st.raw_text)` 把设备取回来**（装载句里写着"烤箱"/"蒸箱"）；
预热节点仍插在该步**紧前面**（物理顺序：预热 → 装载 → 烘烤）。
`dur` 判据也要改用 `ph_dev`（用 `st.device` 会拿到 `0.0` 而漏掉预热）；
`temperature_c` 在 host 为人工步时是 `None` ⇒ 回落 `_ds.default_temp(dev)`。
**9/9 恢复**，预热节点回到 5 条。

### 8.5 护栏与变异自检（4 条新护栏，全部做过变异）

| 新护栏 | 注入的变异 | 结果 |
| --- | --- | --- |
| `test_hand_cap_solvable_at_production_budget` | 行程编码跨段约束 `for i in range(0)` | 双红 |
| `test_engine_keeps_hand_cap_satisfying_plan` | 精确轨择优改回纯 `(Γ,T)` | 红（105.0） |
| `test_pure_load_sentence_is_not_a_device_step` | 装载句判据禁用 | 红（"放入烤箱中"仍是烤箱） |
| `test_load_sentence_with_program_start_keeps_device` | 去掉 `not LOAD_START_VERBS` | 红（屏幕提示句掉设备） |
| 预热节点断言（在上一条 test 内） | `_preheat_device` 的原文回取禁用 | 红 + `audit_doc` 变缺陷 |

⚠ **一条重要的护栏教训（别再走回头路）**：
**被"别的机制兜住"的判据，用官方语料做变异自检永远测不出来。**
实例：`not LOAD_START_VERBS` 在官方语料上**完全冗余** —— 唯一"只被它拦住"的句子
是 `低温牛排#2`「将牛排放入蒸烤架上，放入蒸烤箱第二层，蒸烤箱取出水箱加满水，
根据屏幕提示操作」，但删掉该判据后它**仍会被复合设备继承救回来**
（该菜清空设备后不再有任何蒸箱/烤箱步 ⇒ `combo_device_of` 按"加满水"判回蒸箱）。
**实测：删/留该判据，官方 100 道菜的解析指纹 sha256 完全一致（`50e7fecd57db2899`）。**
⇒ 这类判据**必须用合成句直接钉**（`test_load_sentence_with_program_start_keeps_device` 就是）。

### 8.6 本轮最终基线（2026-10-06 晚，全部实测）

| 探针 | 结果 |
| --- | --- |
| `pytest` | **433 passed**（429 → +2 hand_cap +2 L1） |
| `audit_doc` | **9/9** |
| `step_provenance` | 单菜 100 + 多菜 9 组，**违规 0** |
| `e2e_check --no-http` | **15 / 0** |
| `verify_model 40 11` | **OPTIMAL 40/40**、21 优 / 19 平 / **0 更差**、−19.2% / −24.0% / −38.4% |
| `bench 50`(seed42) | 出锅差 **2.0** / <5min **82%** / 总时长 **59.4** / 手动 **21.7** / 冲突 **0** |
| `lb_probe 40 11` | gap 中位 **0.0%** / makespan==LB **65%** / Γ==LB **70%** |
| `l1_audit` | 设备工作步 **232** / 覆盖率 **54.3%** / 原文显式 **105/767 = 13.7%** / 依赖 **87.0%** |
| `_hand_degrade.py 40 11 30` | **0/40 超限**（修复前 8/40） |
| `heat_steps` 指纹 | **193**（含预热节点）、`heat_dishes` **99/100**、预热节点 **5** |

⚠ **第三轨"净收益百分比变小 ≠ 回归"**：基线自己变好（摘 19 处假设备后"关轨"贪心
从 68.3 → 64.3 分钟）⇒ 第三轨的**边际**收益自然收窄。判据永远是 **0 更差**。

**自评 ≈88 → ≈92**（多目标 +3：连续手动真生效；菜谱解析 +1：摘 19 处假设备）。
余 8 分：① 上游数据上限（原文只有 13.7% 的步骤写了时长）② 下界不够紧（P2）
③ `hand_cap` 仍默认关闭（口径决策：连续手动作约束而非目标，避免与 Γ/T 纠缠交换率）。

**遗留**：Task #82（收紧下界）、#83（多模式顶替）未做 —— 本轮已过 90，且这两项
改的是"汇报好看"与"故障场景"，动了会牵动已稳定的基线，留作后续。
临时探针已从 `scripts/` 移到 `.cache/`：`_l1_missing / _l1_show / _l1_bucket /
_l1_hand_phantom / _l1_prose / _l1_loadonly`。

---

## §C-补9　设备降温缓冲 + 排程诊断 insight（v3.3，2026-10-06，指令"参考建议文本优化系统"）

来源：用户给的两份建议文本（7 节 + 9 节）。**逐条差距分析**见
`docs/建议文本对照与优化.md`（含第一块 7 节 + 第二块 9 节 + **明确不做的 6 项及理由**）。
真缺的只有三类：① 设备切换成本（降温缓冲）② 输出关键路径 + 风险提示 ③ 前端风险面板。

### C9.1 降温缓冲（`FT_COOLDOWN`）

**原缺陷**：全代码库 `cooldown` **0 处实现**。L1 只把"晾凉"这类词降级成 `等待`，
排程层完全不知道"200℃ 刚出炉不能立刻 180℃ 进炉"。实测 50 组 seed42 有 **5~6 处**
同腔体异温背靠背，**全部**是高温→低温。

```python
COOLDOWN_MIN_PER_100C = 2.0     # 每降 100℃ 需 2 分钟
COOLDOWN_ENV = "FT_COOLDOWN"    # on/off; off 时行为与旧版逐字一致（供 A/B）
COOLDOWN_RATE_ENV = "FT_COOLDOWN_RATE"

def cooldown_minutes(prev_temp, next_temp) -> float:
    if not cooldown_enabled():                 return 0.0
    if prev_temp is None or next_temp is None: return 0.0
    drop = float(prev_temp) - float(next_temp)
    if drop <= STEAM_TEMP_TOL:                 return 0.0   # 同温合炉不受影响
    return round(drop / 100.0 * cooldown_rate(), 4)
```

**三条纪律**：① **只罚降温不罚升温**（升温成本已由 L1 的 `insert_preheat_steps` 预热步
显式建模，再罚一次是双重计价）；② 容差与同温合炉**共用** `STEAM_TEMP_TOL`（否则"加降温"
会把 180/182℃ 的同温合炉一起干掉）；③ 降温是**可行性**不是目标项，**不进 `(Γ,T)` 字典序**
（任何能被优化的东西都会被反向利用 —— 求解器会学会"让菜一直高温"来消灭降温）。

**接入四处**（缺一不可）：

| 位置 | 作用 |
|---|---|
| `_ResourceTimeline._fit` | 容量 1 设备（灶具/人工）排下一段时按余温顺延 |
| `_temp_share_fit` | 蒸箱/烤箱串行段之间插入缓冲 |
| `engine.schedule` 的 `_cd_ok` + `_better` | **交付前**的可行性门：CP-SAT 与精确轨的批次机里没有"降温"，可能给出"更优但做不出来"的解 |
| `metrics.device_switches` | 校验器，返回 `[(腔体, 前, 后, 需求分钟)]`，判据与 `cooldown_minutes` **同源** |

**⚠⚠ 最重要的教训：两道可行性门不能合成一个布尔。**

第一版写成 `c_ok = _cap_ok(cand) and _cd_ok(cand)`。后果：hand_cap 那份解满足上限但违反降温、
贪心那份满足降温但超上限，两边都"不可行" ⇒ 退化成按 `(Γ,T)` 比 ⇒ **超上限的解赢了**，
`hand_cap` 的保证被静默抹掉（回归立刻报 `test_engine_keeps_hand_cap_satisfying_plan` 失败，
hand_streak 99 > 30）。正确写法是**按优先级顺序判**（先 hand_cap，后 cooldown），
优先级 = **显式约束优先**：hand_cap 是用户/赛题给的硬指标，降温是本系统的物理精化；
冲突时保住前者，后者由 `insight` **如实报出来**。

**A/B 实测**（`.cache/_ab_cooldown.py`，同进程翻转开关，50 组 seed42）：

| 指标 | `off`（旧基线） | `on`（本轮） |
|---|---|---|
| 同腔体异温背靠背违规 | **5~6 处** | **0** |
| 总时长中位 | 59.4 min | 59.6 min（**+0.2**） |
| 出锅差 < 5 min 占比 | 82% | 82% |
| 逐组 | — | 46 持平 / 4 略差 / **0 更优** |

`FT_COOLDOWN=off` 时**逐字**保留旧基线 —— **没有 off 档就没法证明 on 档真的改变了什么**。

### C9.2 排程诊断 `app/scheduler/insight.py`（新建，约 310 行，纯只读）

对应建议文本 §七.1（风险提示/红色预警）· §三.3（关键路径）· §四.3（冲突归因）。

**刻意与 `metrics` 分家**：`metrics` 是**评分口径**（每个数字都可能进目标函数或评分表，
动它等于动基线）；本模块是**诊断口径**（只给人看，**不参与任何优化决策**）。

**关键路径用"零余量链"而不是"最长时长链"**：教材式的"按任务时长累加找最长链"在这里会
**指错方向** —— 本系统会刻意给短菜加空等让它和长菜同时出锅（同出锅对齐），于是那条
"时长最长"的链往往只到 makespan 的一半左右，真正的瓶颈在别处。本模块改用**零余量链**：
从决定出餐时刻的任务往回走，每一步要求"前一步结束 == 后一步开始"（`ZERO_SLACK_EPS=0.01`）。
语义非常干净 —— **链上任何一步延误 X 分钟，出餐就晚 X 分钟**。

⚠ 但它在"到处是对齐空等"的计划里会**退化成长度 1**（实测某 5 菜实例
`span_min=1.0, steps=1`）⇒ `len(chain) < 2` 时退回 `_longest_duration_chain`
（记忆化递归 + `onstack` 环检测），并返回 `method` 字段**显式说明口径** ——
两种口径的 `span_min` 完全不同，不标注就会有人拿"最长工序链"去对"零余量链"的数字。

**风险清单刻意"不吵"**：第一版做"逐任务余量为 0 就报警"，报出 **14 项、13 项噪声**
（紧凑排程里背靠背**本来就是优化目标**）。改成只报**集合级**结论：

| kind | level | 判据 |
|---|---|---|
| `cooldown` | **high** | 同腔体异温背靠背且留的时间不够 |
| `conflict` | **high** | `check_plan()` 的 details 非空 |
| `critical` | mid | 关键路径 ≥ 2 步全程无缓冲 |
| `bottleneck` | mid | 单设备占用率 ≥ `BOTTLENECK_UTIL=85%` |
| `hot_wait` | mid | 必须趁热的菜（`serve_hot ≥ 0.85`）出锅后等待 > `hold_min` |

`analyze()` 任何异常都返回 `{"error": …}` —— **诊断失败绝不能拖垮排程**。
接口：`/schedule` 加 `insight` 字段（`try/except` 包住，失败只记 debug 日志）；
**`/api/plan` 保持纯官方 schema，不加字段**。

### C9.3 前端风险面板 + 一处"护栏误伤"的修法

CSS `.riskpanel/.riskhead/.riskitem(.high/.mid/.ok)/.rk/.cpbar` **全部显式给色**
（遵守"全局 `button` 的 color/width/box-shadow 会泄漏"铁律）；`#riskPanel` 插在
`prepBanner` 与 `gantt` 之间；`renderInsight(d)` 容忍 `insight` 缺失，
`plan:null` 早退分支里清空面板。

⚠ `test_前端有诊断面板且无裸模板` 第一版**全文件正则扫裸 `${d.xxx}` ⇒ 误伤既有代码**：

1. `${d.locked||0}`、`${d.cook_time??'—'}` —— 有兜底，合法；
2. `bits.push(\`状态: ${d.label}\`)` —— **纯文本插值**（进 `title`，不进 innerHTML），受 API 契约保证，合法；
3. `${ids.length}` —— **本地数组**长度，恒为数字，合法。

最终判据收紧成三件事：① 按**大括号配平**取出 `renderInsight` 的**函数体**
（不能用 `src[idx:src.index("}")]` —— 只取到第一行结尾，护栏就变成**永远为真**）；
② 只看**后端载荷派生**的对象 `d|ins|cp|r|task`；③ 属性访问**直接**被 `}` 闭合
（中间没有 `||` / `??` / 函数包装）。再补一条**正向断言**（`box.innerHTML` 与
`risks.map` 必须出现）—— 否则**一个空函数也能过**"无裸模板"。
为此该测试改为读 `FT_WEB_HTML`（与 `test_web_ui.py` / `test_product_fixes.py` 同约定）。

### C9.4 护栏与变异自检

- `tests/test_insight.py`：**26 项**（降温口径 4 / 校验器 5 / 贪心两轨+择优门+ε-约束 6 /
  诊断模块 9 / 接口与前端 2）。
- `scripts/_mutate_insight_guards.py`（**新建**）：**17 条变异**。覆盖降温恒 0、开关失效、
  速率写死、温度未知不早退、校验器丢判据、**校验器丢 `need<=0` 早退**、
  **`_fit` / `_temp_share_fit` 两份实现各一条**、`_cd_ok` 恒 True、
  **`prefer_cd` 去掉预算 / 恒返回更优解**、**`best_cd` 只判降温不判 hand_cap**、
  **`_better` 不再过滤 hand_cap**、两个环守卫、兜底阈值、降温归因降级。
  ⚠ 该脚本用**「原地变异 → 立刻还原 + sha256 核对」**（Python 模块没法像前端那样用环境变量
  重定向副本），开头会还原上次异常退出的残留，`finally` 无条件还原。
- `scripts/_mutate_web_guards.py`：`TEST_FILES` 加 `tests/test_insight.py`，
  新增 4 条前端变异（丢 innerHTML 写入 / 丢 esc / `render()` 不再调用 / 容器改名）。

**⚠ 一个重要的护栏教训**：`_zero_slack_chain` 的成环守卫**靠 `critical_path` 是测不出来的** ——
只要时长 > 0，"A.end == B.start 且 B.end == A.start"就退化成零时长，正常排程永远碰不到
这个分支。必须**直接调内部函数 + 手工造零时长互依赖**才能钉住它
（新增 `test_insight_零余量链成环立刻停` / `test_insight_最长时长链成环不死循环`）。
同类的还有"零余量链退化时退回最长链" —— 也必须手工造一个带 1 分钟对齐空等的两任务计划。

### C9.5 收口：ε-约束 + 一次真回归（2026-10-06 晚，本轮最后一轮）

**(1) 为什么降温不能当"无界可行性门"。**
把降温做成 `_cd_ok` 之后，CP-SAT / 精确轨会为了"合规"把 Γ 卖得很便宜：
seed42 的 **#23 Γ 2.0 → 191.4**（95 倍）、#25 8.0 → 15.4、#33 2.0 → 25.0。
`.cache/_ab_cd_gate.py` 把**两份代价**分开量（`M.device_switches = _noop_switches` 打桩，
让择优门"瞎掉"、只留插入缓冲），实测 **`off` 与 `insert_only` 逐项完全一致** ⇒
代价 **100% 来自择优门**，不是插入缓冲。

**(2) 修法：ε-约束 `prefer_cd(best, best_cd, budget)`。**
降温是**附加**物理精化 ⇒ 有界牺牲：预算内（`FT_CD_BUDGET`，默认 **5.0 分钟 Γ**）换合规解；
预算外保留更优解，并把违规交给 `insight` 报红色预警。分数与物理都不撒谎。

**(3) ⚠⚠ 真回归：ε-约束把 hand_cap 的保证换掉了。**
第一版 `best_cd` **只判降温**，于是"降温合规但超连续手动上限"的贪心解，
把 hand_cap 达标的精确解按 Γ 换掉了 —— 全量回归当场红：`test_engine_keeps_hand_cap_satisfying_plan`
里 **hand_streak 99.0 > 30**。修法 = 新增

```python
def _feas_ok(p) -> bool:
    """hand_cap **与** 降温同时满足。"""
    if hand_cap is not None and not _cap_ok(p):
        return False
    return _cd_ok(p)
```

并把 `best_cd` 的登记与两处登记条件从 `_cd_ok` 全改成 `_feas_ok`。
**教训**：两道可行性门**不能合成一个布尔**（否则"满足 A 违反 B"和"违反 A 满足 B"都判不可行 ⇒
退化成按 `(Γ,T)` 比 ⇒ 超上限的解赢了）。**按优先级顺序判** = 显式约束优先。
补护栏 `test_降温ε约束不能换掉满足hand_cap的解`（`FT_HAND_CAP=30` + `FT_CD_BUDGET=999`）。

**(4) 修后净代价 0。** 三档 A/B（50 组 seed42）逐项完全一致：

| 档位 | Γ 中位 | Γ 均值 | <5min | T 均值 |
|---|---|---|---|---|
| `off` | 1.5 | 3.40 | 82% | 59.42 |
| `insert_only` | 1.5 | 3.40 | 82% | 59.42 |
| `on` | 1.5 | 3.40 | 82% | 59.42 |

**Γ 变差的组：0**（由择优门造成 0 / 由插入缓冲造成 0）。

**(5) ⚠ 变异自检踩到的环境坑：CRLF/LF 锚点失配。**
`app/scheduler/engine.py` 与 `app/scheduler/model.py` 在仓库里是 **CRLF**（`crlf=1449/929, lf=0`），
而 `_mutate_insight_guards.py` 里的锚点字面量是 LF ⇒ `originals[key].decode("utf-8")` 之后
`str.replace` **一条都匹配不上**，6 条变异全部误报成
`[跳过] …锚点在当前代码里找不到（护栏可能已失效）`。
定位手法：`.cache/_diag_anchor2.py` 逐字符比对，报
`首个不同 @46: 文件='\r'(13) 锚点='\n'(10)`；`.cache/_diag_eol.py` 统计全仓库换行风格。
**修法**（`_to_lf()`）：变异前把源码换行规范化成 LF 再施加变异。
写回时 `Path.write_text(newline=None)` 会把 LF 翻回 `os.linesep`(CRLF)，**缺陷照样注入**；
`finally` 用的是**原始字节**，所以仓库换行风格不受影响。修后 **17/17 全命中**。
> 教训归类：这是"**测试基础设施自己坏了，却被读成护栏失效**"的又一例 ——
> 与"过期审计脚本"、"假阳性命中注释"同族。变异脚本必须**先把基线跑绿**（脚本已有此闸门），
> 且 `[跳过]` 与 `[漏网]` **必须分开报**、返回码区分（跳过错 → 3，漏网 → 1）。

### C9.6 真浏览器探针抓到的布局回归（同日晚，详见 §A11）

前端加面板后 `ui_layout_probe.mjs` 红 1 条：`时间轴高度显著大于任务明细 gantt=35 tasks=104`。
根因 = `.riskpanel` 是 `#ganttWrap`(flex 列) 的兄弟、`#gantt` 是 `flex:1` ⇒ 面板抢走甘特图余量；
**桌面断点 `#v-plan` 固定高度且没有 `#gantt{min-height}` 兜底** ⇒ 挤压静默，
jsdom 探针与 pytest **全绿**，只有真 Edge 排版探针看得见。
修法 `flex:none;max-height:110px;overflow-y:auto`；判定性实验 110px→10px ⇒ 甘特 152→252（正好差 100）。
**完整的方法论（A/B 取证法 / 判定性实验 / 钉离线档的量化）记在 §A11 与 §A10**，
并已沉淀进 `web-ui-layout-probe` skill 的坑 16~20。

**同批查清的一件事**：`ui_probe.mjs` 那 11 条失败（生成排程渲染 0 张卡）**不是**产品缺陷 ——
`FT_LLM=off` 后 **149/0**，且直接 `curl /schedule` 回带 30 个任务 + `kpi` + `insight`。
**探针失败先证伪"环境还是产品"**，换档复跑一步就能分清。

---

## §C-补10　执行态回写 + 经验回流（v3.9，2026-10-08 第九轮）

补的是 Agent 闭环里**唯一缺失的那一跳**。两项**串起来**：历史里只有计划时长、
没有实际时长 ⇒ 经验回流必须建立在执行态回写之上，顺序不能倒。

### C10.1 缺口取证（grep 出来的，不是印象）

| # | 缺口 | 证据 |
|---|---|---|
| 1 | 无执行态回写 | 32 个路由里搜 `task/step/exec/tick/state/progress`，**0 命中**。排完、通知完就失明；重排只能靠用户手选「动态事件」 |
| 2 | 不从历史学习 | `app/scheduler/` + `app/agent/` 搜 `schedules_user\|history`，**0 命中**。A1 的历史只供看/比/还原，不回流决策 |
| 3 | 在线决策面窄 | 9 个 Prompt 里 **P1 语义先验 / P7 参数补全**离线落盘、在线只读缓存；真在线的只有 P2/P3/P4/P5/P6 |

### C10.2 ★ 时长校准（`app/scheduler/calib.py`）四条硬纪律

1. **`FT_CALIB=off` 默认关。** 校准改时长 ⇒ 直接动 (Γ, T) 权衡。开档必须先跑 A/B 证明 **0 组更差**。
   关档时 `apply()` 第一行 `return base` ⇒ 与旧行为**逐位一致**，基线零风险。
2. **只校准 `duration_min is None` 的兜底步。** 原文写了"蒸 15 分钟"是事实不是估计。
   这条由 `engine.estimate_duration` 的**结构**保证（校准只在无显式时长分支被调用）。
3. **clamp [0.5, 2.0] + 样本门限 `FT_CALIB_MIN_N`=5。** 单次异常值不该抬走整个档位。
4. **按和算比值** `Σ实际/Σ预估`，**不是**"比值再平均" —— 后者让 1 分钟与 30 分钟等权。

**接入手法（关键）**：`estimate_duration` 拆成 `_base_estimate(step) -> (值, 档位键)`，
四处调用点（engine×3 / model.py:161 / cpsat.py:57 / prep.py:129）**自动生效且签名不变**。
⚠ **蒸箱与烤箱刻意分成两个 bucket** —— 都兜底 8 分钟但真实分布不同，合并会互相污染系数。

### C10.3 执行态（`app/scheduler/exec_state.py`）

- 落盘 `data/exec_user.json` = **第五份用户层**（已进 `SKIP_NAMES`）；
  新增隔离开关 **`FT_EXEC_FILE`**，conftest 钉 `tempfile.mkdtemp()`。
  ⚠ 比另四份**更危险一档**：`samples` 会直接改兜底时长 ⇒ 一整批排程断言集体变味，
  而报错指向排程结果，没人会想到去看 data/ 下多出来的 json。
- **偏差取「最大单步超出」而不是求和** —— 并行分支上的延迟**不叠加**；
  求和会把"3 道菜各晚 1 分钟"判成晚 3 分钟 ⇒ 噪音重排。宁可低估（漏建议）不要高估。
  "重排到底有没有更好"交给 `/reschedule` 已有的 `pareto` 实测，**本模块不做预测**。
- **重复上报先扣旧样本再加新的** —— 同一步点 5 次「完成」只应是 1 个样本。
- **不填实际用时 ⇒ 按 `planned` 记（准时）**，绝不记 0（记 0 会把系数拉塌成"这步不要时间"）。
- **只建议不自动重排** —— 达到阈值弹确认层，用户点头才调 `/reschedule`。

### C10.4 前端三条取舍

1. **打点放步骤详情弹层，不加明细表列** —— 加列会动 `table-layout:fixed` 的列宽分配，
   而 14 视口排版探针盯的就是这套几何。护栏判据是**整条表头字符串**（有人加 `<th>` 立刻红）。
2. **`.exec-done` / `.exec-late` 必须排在 `tbody tr:hover` 之后** —— 两者特异性完全相同
   （2 id + 1 类/伪类 + 2 type），后定义赢；放前面会被 hover 盖掉。
3. **`loadExec()` 在 `render()` 里不能 await** —— 画甘特图不该等一次网络往返。

### C10.5 ⚠ 本轮未完成的验证（沙箱故障）

跑命令的沙箱组件（`tsbx.dll`）持续故障，Bash 与 PowerShell **两个工具全部不可用**。
**验证链一步都没跑**。恢复后必须全跑：pytest / ui_probe / ui_click_audit /
ui_layout_probe / 四套变异 / 打包 / 解压冒烟。
另缺：`ui_probe.mjs` 与 `ui_click_audit.mjs` 的 v3.9 用例；后端 17 条护栏的变异脚本
（`_mutate_web_guards.py` 只变异 index.html 副本，机制上不适用于后端 py ——
参考 `_mutate_insight_guards.py` 的**原地变异 + sha256 还原**做法，且**必须后台跑**）。
⚠ 探针启动命令要补第四个隔离变量 `FT_EXEC_FILE=$PWD/.cache/_exec_probe.json`。

---

## §E　服务器部署包（2026-10-09，指令"打包移植到服务器，key 保留，马上可用"）

### E1 ★ 模型开关：两种部署形态的默认值**不同**（别照抄）

| 形态 | 默认值 | 理由 |
|---|---|---|
| `Dockerfile` | `FT_LLM=off` | 容器通常没有稳定模型出口，出口不通会白吃超时预算 |
| `deploy/cooking-agent.service`（systemd） | **四个 `on`** | 出厂层自带凭据，解包即用 |

**为什么 systemd 那份必须写 on**：2026-10-09 之前它是四个 `off` ⇒ `llm_defaults.json`
里的 key 一个都用不上。故障现象是**静默**的——排程照跑、耗时照旧（语义先验走
`data/llm_agent_cache.json`）、日志无 error，只有界面上一句「模型已关闭」。
`/health` 与 `/api/plan` **判不出这个故障**，所以自检必须单独断言
`/api/llm_status` 的 `enabled=true`。

关掉的方式：`WITH_LLM=0 bash deploy/server-setup.sh`（sed 改四行 + grep 复核）。

### E2 `scripts/pack_server.py` —— 打包单一真源，fail-closed

原来 `deploy/upload.sh` 内联打包，有两个真 bug：

1. `cp deploy/* "$STAGE/deploy/"` —— `deploy/windows/` 是目录，`cp` 无 `-r`
   ⇒ `omitting directory` + 退出码非 0 ⇒ `set -e` 下**整个上传必然中止**。
2. 打出来的包**没有任何校验** —— "key 有没有进去"只能人肉看。

现在 `upload.sh` 与 `deploy/pack-server.sh` 都调 `pack_server.py`，不会漂移。
校验不过就 `SystemExit(1)`，**不产出包**。

**必须存在且可用**：`llm_defaults.json`（不只是存在——要能解析出 provider，
openai 档 `remote.api_key` 必须非空，否则 `enabled()` 直接 False）；
`data/llm_agent_cache.json`（断网也要排得好）；100 道菜基线；部署脚本。
**必须不出现**：`.env*`、五份本机状态、`tests/`、点开头目录、`.pyc`、`.png`。
所有 `.sh/.service/.conf` **无 CR**；`.sh` 在包内 0755。

⚠ 排除清单**直接 `from scripts.sync_publish import SKIP_NAMES`**，抄一份必漂移，
且漂移是**静默**的（publish/ 里确实没有这些文件，看起来没问题）。

### E3 三个踩到的坑

1. **`tarfile.add()` 默认 `recursive=True`** ⇒ 遍历 `rglob` 时目录 add 一遍、
   目录下文件又逐个 add 一遍 ⇒ 每个文件进包两次（1.25MB → 0.48MB）。
   现象只是"包大了一倍"，**解压完全正常**，不对比体积根本发现不了。
   修：`recursive=False`（目录项用 `gettarinfo` + `addfile` 单独加）。
2. **本机 Git Bash 的 `sed -i` 在 Windows 临时目录报 `cannot rename: Invalid cross-device link`**，
   且失败时**源文件保持原样**。危险在于：源文件本来是 `on`，于是"改成 on"的
   检查**假绿通过** —— 只有"改成 off"那次才暴露。Linux 服务器无此问题。
   ⇒ 验证 sed 替换时**别用"改成当前值"当通过判据**。
3. **冒烟脚本对 `/app` 做了 `json.loads`** —— `/app` 返回 HTML，必然抛异常，
   而 `except` 把它记成 `http=0`，看起来像"页面打不开"，其实是检查脚本自己写错。

### E4 冒烟方式（可复用）

`.workbuddy-ai/tmp/smoke_server_pkg.py`：**解压到干净临时目录** + 只给 systemd unit 里
那几个环境变量起服务（模拟服务器，不带开发机的 `.env` 与用户层）⇒
断言 `enabled=true` / `provider=openai` / `api_key_set=true` / `model_source=factory`。
⚠ 起服务 + 打接口必须在**同一个 Python 进程**里跑完（Bash 工具跨调用会杀后台进程）。

### E5 本轮基线

`pytest` **612 passed**（`test_pack_server` 新增 16）· 变异 **5/5 捕获** ·
`dist/cooking-agent-server.tar.gz` **0.48 MB / 81 文件** · 冒烟全绿。

---

## §F　L5 性能与可复现性（2026-10-09，指令"优化排程生成速度"）

### F1 ★ 先分清"快"和"可复核"——后者才是硬要求

实测性能早已达标（3 菜 48ms / 5 菜 207ms，优秀线 5000ms，余量 15–68 倍），
但发现**同一输入两次排程给不同计划**：

| 菜数 | 连跑 6 次的 makespan |
|---|---|
| 12 菜 | **134 / 135 / 139 / 143**（4 种） |
| 20 菜 | **194 与 373.4**（差近 2 倍）；4 次跑出 4 种 `dish_finish` |

根因（**两个**求解器都有）：`num_search_workers = 4`（多线程并行搜索）
+ 墙钟超时 + 接受 `FEASIBLE` 次优解 ⇒ 哪个 worker 先撞出好解取决于线程调度。
**只设 `random_seed` 没用**（实测 C 档 workers=4+seed=42 仍有 2/3 组合抖动）。

修：`cpsat.py` / `model.py` 默认 **workers=1** + 固定 seed。
`FT_CPSAT_WORKERS` / `FT_EXACT_WORKERS` 可换吞吐（**代价是不可复现**）。

### F2 单线程的代价与反例

- bench 耗时均值 121 → **163 ms**；max ~0.5s → **~1.5s**（仍 ≪ 5s）
- ⚠ **大问题上单线程反而更快**：12 菜 1698 vs 1789ms、20 菜 2014 vs 2027ms
  （多 worker 的协调开销 + 预算被切成多份）
- 关 CP-SAT（`FT_CPSAT=off`）**更慢**（max 3624 vs 1503ms）且 Γ 2.9→3.5
  ⇒ **CP-SAT 是加速器不是累赘**，别想着砍掉它换速度

### F3 ❌ 被实测否决的三个方案（别再捡）

1. **"贪心解已达下界 ⇒ 跳过精解轨"**（45 组 3 组变差：Γ 0→10、T 30→74、T 55→76）
   - `lower_bound_from_plan` 是**从计划反推**的下界，只在**同一任务集口径**内成立；
     精解轨会换口径（`slim` 丢掉提前准备步）⇒ 拿 A 口径的下界判 B 口径的解 = **假下界**
   - 这正是 `model.py` 注释里警告过的事，被用在了它明确不适用的地方
   - 副作用：下界本身要算，45 组只命中 2 组 ⇒ 净效果**慢 3%**
2. **降 `FT_EXACT_BUDGET`**（1.5→0.4）：max 1503→1574ms，**无效**
   （orchestrator 会把固定池重新分配，总量不变）
3. **关 CP-SAT**：见 F2

### F4 开关缓存 `FT_FLAG_CACHE` 的正确写法

`cooldown_enabled()` 一次排程被调 **116,895 次**，每次读 `os.environ`
（含 `strip/lower/encodekey`）⇒ 0.85s/6.93s = **12%**。

⚠ **不能用模块级变量，也不能"入口 reset 一次然后一直缓存"**：
后者会让同一个测试内的 `setenv("off")` → `setenv("on")` 读不到新值
（实测 `test_insight` 3 条 + `test_core` 1 条当场变红）。

正确写法（`@_flag_scope` 装饰器）：
- 进入 `_schedule_once` / `schedule` 时 `_FLAG_CACHE.set({})`（空 dict = 在作用域内）
- 退出时 `set(None)`
- `_flag()` 在 **`cache is None` 时直接读环境、不建缓存**
  （否则任何零散调用都会留下残留值）

### F5 最终 L5 实测（3–5 菜 = 赛题范围）

| 场景 | 均值 | p95 | max | 余量 |
|---|---|---|---|---|
| 3 菜 | 48 ms | 73 ms | 73 ms | 68× |
| 5 菜 | 207 ms | 329 ms | 329 ms | 15× |
| /reschedule（4 菜+加菜） | 296 ms | — | 1238 ms | 2.4× |

冷启动首请求（刻意 `FT_WARM_LLM=off`）2472ms；服务器 systemd 是 `FT_WARM_LLM=on`，
启动即预热 ⇒ 用户首个请求付不到这个代价。

### F6 本轮基线

`pytest` **618**（`test_reproducible.py` 新增 6）· 变异 **3/3** ·
bench 指标未变（Γ 中位 2.0 / 均值 2.9 / <5min 82% / 总时长 62.0 / 冲突 0）。

---

## §G　模型调用失败的**可观测性**（2026-10-09，用户截图"apikey 调用有问题"）

### G1 ★ 故障现场：徽标「AI在线」+ 助手「已切换到离线模式」

真因是**上游账号未实名**（StepFun 中转站一律 502），不是代码问题。
但这个故障**在界面上完全看不出来**，因为两个根因：

1. **徽标只看熔断器**（`enabled()` = 开关 + 凭据非空），**不反映调用是否成功**。
   熔断阈值 3 次，用户只试 1–2 次 ⇒ 徽标一直绿色「AI在线」。
2. **响应体在 `_post` 里被直接丢掉**（`return "http", None, e.code`）⇒
   系统里**根本不存在**"为什么失败"这个信息，502 与 401 长得一模一样。

### G2 修法（四处，缺一不可）

| 位置 | 改动 |
|---|---|
| `llm.py` | `_record_failure(code, body)` / `last_failure()` / `_hint_of()`；`_post` 读响应体记账 |
| `llm.py` | **确定性失败立刻熔断**（401/402/403/429 + 上游含 实名/额度/凭据 关键词）⇒ `_trip("auth")` |
| `assistant.py` | 降级文案追加原因行（用户就是在对话框里看到问题的，那里最该说明） |
| `main.py`+`index.html` | `/api/llm_status` 暴露 `last_failure`；徽标 `online 但 last_failure 有值` ⇒ `⚠ 模型调用失败` |

`_LLM_REASON_TEXT` 新增 `"auth"` 键；`_trip` 的"立刻拉满阈值"从只认 `quota`
扩到 `("quota", "auth")` —— **只加 `_trip("auth")` 调用而不改 `_trip` 内部，
熔断不会打开**（本轮实际踩到：`last_failure` 记上了，但 `open=False`）。

⚠ `_hint_of` 必须剥 URL（上游报错自带文档链接）⇒ 守住铁律
「第三方域名只准出现在 `llm_defaults.json`」。

### G3 ★★ 判据被"兜住"的两种新形态（本轮两次，都要记）

1. **替身把被测路径自己实现了** —— `_fake_post` 替身内部就调了 `_record_failure`，
   于是"把真实 `_post` 里那行删掉"这个变异**测不出来**（16 项全绿但功能已废）。
   ⇒ 关键路径**必须有一条走真实实现的用例**（这里 mock `urlopen` 抛 `HTTPError`）。
2. **判"分支接上没有"却只判了它的产出** —— `assert "模型调用失败" in html`，
   而变异把 `failing` 写死成 `false` 后**文案仍在页面上**，照样通过。
   ⇒ 必须判**定义式/输入源**（`const failing=…` 里含 `lf.code`），
   不能判"页面里有没有这几个字"。

**通用规律**：判"某分支是否真的生效" ⇒ 判**它的输入**；判"某函数是否真的被调用" ⇒
必须走**真实实现**（替身不能替被测的那一段）。

### G4 本轮基线

`pytest` **635**（`test_llm_diagnostics.py` 新增 17）· 变异 **7/7 捕获** ·
端到端全绿（助手文案带原因 · 状态端点暴露 · 立即熔断 · 二次请求 **5ms**（原 ~900ms）·
`/api/plan` 不受影响）。
