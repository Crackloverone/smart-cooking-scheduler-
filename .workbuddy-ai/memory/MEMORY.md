# 项目长期备忘 —— 智能烹饪调度 Agent（ft）

> 须 <9.4k。只留**最高频不变量**；完整清单在 **`MEMORY-细则.md`**（§A 界面 / §B LLM / §C 工具链 / §D 部署）。

## 一、口径铁律
1. 出锅差 Γ = **原始跨度 `max F − min F`**（照抄评分表）。**不要**换成语义冷等待 `Σσ_d(T−F_d)`：冷菜可早出锅时两者**方向相反** ⇒"语义更对、评分更差"。
2. 硬约束（设备冲突/依赖）**绝不进目标函数**，只做可行性（利用率是仪表盘不是方向盘）。目标函数不得含**可被反向利用的代理量**；可博弈时用 **ε-约束堵口子**。
3. **"完成时刻" ≠ "出锅时刻"**：Γ 用"最后一步结束"；**保热惩罚**用"最后一次进加热设备结束"（`metrics.dish_cook_finish`）。混用 ⇒ 求解器**被奖励**"早做熟干等"（中位 **23min**）。
4. **保热项必须"以 Γ 计价"**：`min(hot, w_gap × FT_HOT_BUDGET)`，默认 **0.5 分钟 Γ**。用完成时刻 ⇒ 恒≈0；无界 ⇒ 一道菜等 170min 就**夺走 Γ 的权**。
5. **Γ 有独立下界**：`Γ_lb = Σd − max d`（d = 同一**容量 1** 资源上各菜**末步**时长；**排除**蒸箱/容量>1/`等待`）；**68% 实例 Γ 恰等于下界**。

## 二、模型（`scheduler/model.py`）—— 完整清单在 §C-补2/3/4/8
- 备菜判据**只准用 `prep.py::prep_keys`**（三轨唯一口径）。`PREP_AHEAD_MIN=30`（**别再降**）。**hard 反向前缀闭包拉祖先；soft 必须保守**。`slack=15`。优先级 **Γ ≫ T ≫ 语义冷等待**。
- ⚠ **`lower_bound_from_plan` 两个静默陷阱**：① 必须走 `_dep_key()`（`tuple(d.split("#"))` 给**字符串** step_id 而 `dur` 键是 **int** ⇒ 依赖边全丢、关键路退化成"最长单步"）；② **`等待`步必须计入关键路**。修后 gap 中位 **0.0%**、**68~72% makespan == 可证下界**。
- **主导工序先验 `priors._kind_of`** 按**上桌状态**判（冷食 > 烘焙 > 蒸 > 汤羹 > 炒炸；⚠ `蛋羹` 先于 `羹`）：旧判据把 **72/100** 判成 fry，现 **13**。**连续手动上限 (C5)** 见 §C-补8。
## 三、三轨求解（`engine.schedule`）
`贪心(6 策略,含 composite) → CP-SAT(不支持冻结) → 精确模型(支持 fixed)` → (Γ,T) 字典序取优 → `_add_kitchen_semantics()` → `_restore_missing_steps()` → `_rebase_to_first_resource()`。
- ✅ **可证最优率 ~98–100%**（3–5 菜 100/100、6–9 菜 60/60 OPTIMAL）；公平版预算探针 **dT=dG=0.0**。`composite` 必须**新增**而非替换原 5 条（收敛成一条掉 3~5 点）。
- ⚠ 调用方**显式传** `use_exact=` 会绕过"`is None` 才读 `FT_EXACT`"⇒ **`dispatch.solver_budgets` 也必须自己认 `FT_EXACT`**（否则消融/`verify_model` 对照组静默失效）。
- ⚠ **非备菜任务不得早于 t=0**：`dep_ready` 为负 ⇒ 步排到 t<0 ⇒ 被 `_to_plan` 的 `start>=0` **静默漏掉**（实测 8/100）。修法 `dep_ready=max(0.0,…)`。护栏 `test_no_resource_task_before_day_start`（**必须走 `adapter.plan_recipes`**）。
- ⚠ **t=0 锚点 = "第一个占用资源的任务"**（`FT_ANCHOR` on）；原定义"提前准备段结束"会在备菜后夹**纯等待** ⇒ t=0 后设备全空。⚠ **有 `hard_frozen` 时绝不平移**。
- ⚠ **预算必须"从后往前"分配**（`orchestrator._run_inner`）：LLM 吃光 `time_budget` ⇒ 精解轨全关 ⇒ **交付贪心解**；在线闸门**本轮结束后**才落 ⇒ **进程启动后第一次请求必中招**。修法 `dispatch.min_pool_for_tracks`（二分反解）+ `_solver_reserve`。护栏 `test_agent_budget.py`(24)。

## 四、L1 解析与溯源 —— 完整清单在 §C / §C-补6/7/8
- **等待词双向守住**：`SOAK`（浸泡/泡发/解冻/过夜/静置/醒面/晾凉/放凉/冷却/沥水）命中即整句降级 `WAIT`。漏降级 ⇒"浸泡4小时"变蒸箱独占；乱降级 ⇒"烘烤结束，取出晾凉"抹掉整句真实烤箱作业。放行只限三类（`LOAD_START_VERBS` / 点名加热设备+显式时长 / 灶具+`ACTIVE_COOK`）。
- **溯源 R1–R4**：无凭空步骤 / 无 `dish` 的只准预热共享且 `raw_text` **精确等于** `蒸箱预热`（**不能 `endswith`**）/ 步骤不许消失 / 不许依赖违反；产出**恰好 5 条预热**。
- ⚠ **假设备两大来源**：① **名词里的烹饪动词**（`_DONE_STATE_RE` 只收 好/熟/透/沸/软/烂/过，**不收 开/至/成/干/香**）；② **守卫写宽了** —— 复合设备继承守卫**不能把灶具算进去**（含灶具 ⇒ 有任一灶具步就整段跳过 ⇒ 真烘烤步被当备菜、整道落 t<0）。`heat_steps` **193**。
- **验收判据是解析正确性不是分数**：有加热设备的菜 95/100 → **99/100**（优秀率 92%→90%）⇒ **指标变差也可能是修对了，不要按分数回滚**。**依赖图缺边（已知）**：回指型指代未识别，**只放宽不收紧**。
## 五、界面层（`app/web/index.html`）—— 完整清单在 §A
**站点 5 个**：首页/排程/菜谱/**设置**/历史 + 悬浮球「AI 对话」。⚠ 增删视图**必须同步四处**：`.menu`、两个 `.mjs` 探针、`test_web_ui.py`。⚠ 全文件只允许 **1 处** `fetch(`（在 `apiFetch` 内）；**reject**=不可达 ⇒ `#offlineBar`，**4xx/5xx**=后端活着 ⇒ 原样返回 `r`；**401 走 `markAuthRequired()`，绝不能走 `markBackendDown()`**。⚠ 探针**钉离线档**、服务**常驻**；**四份用户层引到 `.cache/`**：`FT_DEVICES_FILE`+`FT_LLM_SETTINGS_FILE`+`FT_SCHEDULES_FILE`+`FT_EXEC_FILE`；**设备上下限只准来自 `/api/device_spec`**。
- ⚠ **`#ganttWrap` 内的新块必须 `flex:none` + `max-height`**：`#gantt` 是 `flex:1` ⇒ 新面板一有内容就把甘特图挤到 **35px**；挤压**静默**，只有真 Edge 探针会红。
- ⚠ **每个可编辑单行输入框都必须绑回车**（= 旁边那个按钮的动作）；判缺口看**有没有绑 keydown**，不是"这次有没有反应"。⚠ 回车接错动作比没接更危险（`#setToken` 接成 `saveModel()` ⇒ 界面还回"已保存"）。见 §A16.2。
- ⚠ **右列（已选卡）任何新块都必须"恒定单行"**：折行会把「生成排程」推进悬浮球，**只在 1366×768 发作**。判档用 `matchMedia('(min-width:1025px) and (min-height:761px)')`（**不能**用轨道数）。见 §A16.4。
- ⚠ **顶栏穿模 = sticky 的 `top` 只挪渲染位置、不占流内空间**。修法：`--nav-top`（`top` 与 `margin-top` **同源**）+ `--page-top-gap`（**全站唯一**顶部留白）。⚠ 视口高 < **763px** 换长页档（`align-items:start` 是探针判档**唯一信号**）。见 §A15。
- **v3.7 计划工具**见 §A17。⚠ 行内复选框不许挂 `onclick`。
- **v3.8 C1 撤销/确认**（§A18）：**能回退的给撤销条**（删菜谱/恢复默认厨房/清空对话/清令牌）、**回不去的给确认层+延迟提交**（清空历史 8s）。⚠ `deleteRecipe`/`resetKitchen` **不能加确认层**（探针直接调）。
- **v3.9 执行态回写 + 时长校准**（§C-补10）：`FT_CALIB` **默认 off**（关档与旧行为逐位一致）；`exec_user.json` 第五份用户层 + `FT_EXEC_FILE`。⚠ **验证链未跑**（沙箱故障）。

## 六、LLM 接入与安全 —— 完整清单在 §B
**两档接入，运行期可切**：`FT_LLM_PROVIDER` ∈ `ollama`(免密钥,`127.0.0.1:11434/v1`)/`openai`(需密钥)。**env 最高优先**；`_cfg()` 把**空串当未设置**。护栏 `test_llm_provider.py`(**66**)。**★ 模型失败可观测性（§G，2026-10-09）**：徽标**只看熔断器**、`_post` 曾**丢掉响应体** ⇒ "AI在线 + 助手回离线模式"这种组合**完全查不出原因**。修：`_record_failure(code, body)` + `last_failure()` + `_hint_of()`（**剥 URL**，守"域名只在出厂层"铁律）；**确定性失败立刻熔断**（401/402/403/429 + 实名/额度/凭据关键词 ⇒ `_trip("auth")`，⚠ **必须同时改 `_trip` 内部的立刻拉满条件**，否则 `last_failure` 记上了但 `open=False`）；助手降级文案带原因；`/api/llm_status` 暴露 `last_failure`、徽标转警示态。护栏 `test_llm_diagnostics.py`(**17**)+变异(**7/7**)。⚠ 变异两次"判据被兜住"：① **替身把被测路径自己实现了** ⇒ 关键路径必须有一条走**真实实现**；② 判"分支接上没有"只判了**产出文案** ⇒ 必须判**定义式/输入源**。
⚠ 用户层路径有**隔离开关** `FT_LLM_SETTINGS_FILE`（镜像 `FT_DEVICES_FILE`，conftest 钉到不存在路径）—— 没有它，探针切一下「接入方式」+回车就会**改掉开发机真配置**。见 §A16.5。
- ⚠ **`_resolve_model` 必须优先读 `models[provider]`**（否则拿远程模型名打 11434 ⇒ 404 且**静默降级**）。**密钥只回打码预览、原文永不出后端**；**空串=保持原样**；`llm_settings.json` **不进镜像也不进发布目录**。写盘后**必须 `breaker_reset()`+`reset_online_gate()`+`_recompute_timeouts()`**。
- ⚠ 超时按档分流（ollama 30 / **openai 20**）· 落盘**必须原子写**（半截 json ⇒ 下次启动整份丢弃且无报错）· `add_middleware` **后加的更外层** ⇒ 先 `_security_guard` 后 CORS。**"远程不可用"（不是 CORS）**：`Dockerfile` 默认 `FT_LLM=off` + `.env` 被排除 ⇒ `enabled()` 恒 False。

## 七、设备容量与顶替（改 `engine._schedule_once` / `agent/orchestrator.py` 前必读）
**三个真实位置**：容量 = `engine.RESOURCE_UNITS` + 请求级 `resource_override()`（ContextVar，**全局字典永不被改写**）；能力 = `spec/device_spec.py::DEVICE_SPEC`；顶替 = `engine.DEVICE_FALLBACK`。
- ⚠ **顶替/降级必须同步改 `device`，不能只改 `res`**：只改 `res` ⇒ `device="蒸箱"`+`unit_id="灶具-1"` 的矛盾任务；`checker.overlap_allowed()` 按 `device` 判"蒸箱同温可共享"豁免 ⇒ **把真实灶具冲突判成合法重叠**（静默漏报）。护栏 `test_device_substitution.py`(13)。
- ⚠ **`use_cpsat=False`（设备故障）只许关 CP-SAT**，**不能**写成 `if use_cpsat:` 把整个精解块跳过（**精确轨认容量覆盖**）。**不能写 `max(1, cap)`**。⚠ `device_spec.is_legal()/legal_modes()` 是**闭包性质**：`clamp_params`/`fit_mode` 输出必须**永远** `is_legal`（别名子串匹配）。

## 八、菜谱库两层数据纪律（`app/spec/recipe_store.py`，改前必读，详见 §C）
**官方当只读基线，用户改动只写用户层**：`data/recipes_official_100.csv`（**永不被改写**，SHA-256 护栏）+ `data/recipes_user.json`（`{added,overrides,deleted}`，原子写）。**唯一真源**是 `recipe_store.py`。**删除语义按来源分**：官方菜 → 墓碑软删（可恢复）；**自建菜 → 真删**；`restore()` 抛 `KeyError` ⇒ 调用方转 **404**。⚠ `recipes_user.json` **进 `SKIP_NAMES`**（`overrides` 按 id **覆盖**官方菜且**回不去**）。
- ⚠ **`adapter._load()` 缓存必须按 `recipe_store.version()` 失效**：原 `if _DB: return` 硬缓存 ⇒ **用户新增的菜永远排不出来**。写开关 `FT_ALLOW_RECIPE_WRITE`（默认 on）。

## 九、降温缓冲 + 排程诊断（v3.3）—— 完整清单在 §C-补9
**降温是"可行性"不是目标项**（`engine.cooldown_minutes`，`FT_COOLDOWN` on / `FT_COOLDOWN_RATE` 2.0）：**只罚降温不罚升温** · 容差与同温合炉**共用** `STEAM_TEMP_TOL` · **绝不进 `(Γ,T)` 字典序**；四处接入**同源**，三档 A/B 一致 ⇒ **净代价 0**。附加精化 ⇒ **ε-约束 `FT_CD_BUDGET=5.0`**（**无界版把 Γ 从 2.0 抬到 191.4**）。
- ⚠⚠ **两道可行性门不能合成一个布尔**（`_better`）：合成 ⇒ 两类"各违一条"都判不可行 ⇒ 退化成按 `(Γ,T)` 比 ⇒ **超上限的解赢了**。**按优先级顺序判**（hand_cap 先、降温后）。
- `insight.py`（只读，与 `metrics` **分家**）：零余量链优先，`len<2` 退回最长链并回 `method`；异常只回 `{"error":…}`。**`/api/plan` 保持纯官方 schema**。

## 十、模型配置五层分发 + 密钥打码（v3.4）—— 完整清单在 §B-补1
**五层**：`env > data/llm_settings.json`（用户层，**`sync_publish` 排除**）`> app/agent/llm_defaults.json`（**出厂层，随包走，必须在 `app/` 下**）`> _DEFAULTS > 兜底`；出厂层坏 ⇒ **静默当空**；用户层**整层优先**。⚠ 切档没给模型 ⇒ 落盘取出厂层模型名（回落 `_DEFAULTS` ⇒ 404+静默降级）。`looks_masked` 值 ⇒ **`set_provider` 400 拒且不写盘**；前端**输入框必须留空**。⚠ **第三方域名只准出现在 `llm_defaults.json`**。

## 十一、基线、铁律、部署
**基线（2026-10-09 第八轮）**：`pytest` **618** · `audit_doc` **9/9** · `step_provenance` 0 违规 · `e2e_check --no-http` **15/0** · `ui_probe` **189/0** · `ui_click_audit` **250/0/5/8 · 回车缺口 0** · `ui_layout_probe` **120/0**（探针钉 `FT_LLM=off`）· `verify_model 40` **OPTIMAL 40/40**（0 更差）· `bench 50` 出锅差 **2.0**/均值 2.9/总时长 **62.0**/耗时均值 **163ms** · `lb_probe` gap 中位 **0.0%** · `heat_steps` **193** · L5 实测 5 菜 **207ms**、重排 max **1238ms**。
**★ 可复现性（L5，2026-10-09）**：`cpsat.py`+`model.py` 曾写死 `num_search_workers=4` ⇒ **同一输入两次排程给不同计划**（12 菜 134/135/139/143；20 菜 194 vs 373）。评分要核对"总耗时 vs 理论最优" ⇒ 已改**默认 workers=1**（`FT_CPSAT_WORKERS`/`FT_EXACT_WORKERS` 可换吞吐，代价是不可复现）。护栏 `test_reproducible.py`(**6**)。⚠ 实测否决过：①"贪心达下界就跳过精解"——`lower_bound_from_plan` 是**从计划反推**的下界，只在**同一任务集口径**内成立，精解轨会换口径（丢备菜步）⇒ **假下界**，45 组 3 组变差（Γ 0→10、T 30→74）；②降 `FT_EXACT_BUDGET` 无效（池子会重分配）；③关 CP-SAT 反而更慢（3624 vs 1503ms）且质量掉。开关缓存 `FT_FLAG_CACHE` 必须用 `@_flag_scope` 限制在函数执行期内（入口 reset 一次会让 monkeypatch 失效，实测红 4 条）。
**变异自检**：`insight`(17) · `llm`(**19**) · `web`(**170**) · `deploy`(**33**) · `repro`(**3**) —— 全命中 0 跳过。⚠ Python 侧**原地变异**：锚点须先把源码**换行统一成 LF**（`engine.py`/`model.py` 是 **CRLF**）；**一律后台跑**（被强杀 ⇒ **源码留变异态**）。判残留**必须比 sha256**，还原用 `.workbuddy-ai/tmp/mutate_*.orig`。见 §A16.7。
**打包/发布**：`pack.ps1` = `sync_publish` 闸门 + 包内复核 + 反向禁 `.env*`。产物 `dist/cooking-agent-windows.zip`（**0.48MB/73 文件**，sha256[:16] `aa3778bb92596285`）。⚠ **PS 侧 safe-delete 垫片拦 `Remove-Item` 且 fail-closed**（`CODEBUDDY_SAFE_DELETE_ENABLED=0` **无效**）⇒ 用 `[IO.Directory]/[IO.File]::Delete` **预清理**。⚠ **两个 `.wbapp_*.genie` 不是同一应用** ⇒ 重发布前确认 appId/URL。⚠ `SKIP_NAMES` = **五份本机状态**（v3.9 加 `exec_user.json`）；产品数据（`llm_defaults.json`/`device_presets.json`/`recipes_official_100.csv`）**必须随包走**。
**服务器部署**（§E）：`scripts/pack_server.py` 是**打包单一真源**（`deploy/pack-server.sh` 薄封装，`upload.sh` 也调它）⇒ 产出 `dist/cooking-agent-server.tar.gz`(**0.48MB/81 文件**)。⚠ **模型开关默认值两种形态不同**：`Dockerfile`=`FT_LLM=off`（容器无稳定出口）；`deploy/cooking-agent.service`=**四个 `on`** —— 写 off 会让出厂层 key **完全用不上**，而排程/耗时/日志**全无异常**，只有 `/api/llm_status` 的 `enabled` 能看出来。关掉用 `WITH_LLM=0`。⚠ `tarfile.add()` 默认 `recursive=True`，遍历 `rglob` 会让每个文件**进包两次**（体积翻倍但解压正常）⇒ 必须 `recursive=False`。⚠ 部署脚本 `.sh/.service/.conf` 必须 **LF**（CRLF ⇒ `bad interpreter`）。护栏 `test_pack_server.py`(**16**)+变异(**5/5**)。
**Windows 部署**（`deploy/windows/`，§D）：`pack.ps1`→`setup.ps1`→`uninstall.ps1`；护栏 `test_deploy_windows.py`(**44**)+变异(**31/31**)。⚠ 编码铁律方向各异：`.ps1` **必须带 BOM** · `.env.local` **必须无 BOM** · `run-server.cmd` **必须纯 ASCII+CRLF**。用**计划任务**、`--workers 1`。⚠ `-Offline` 的 `FT_LLM=off` 是**全局总闸**。

## 十二、界面 v3.5/v3.6（2026-10-08）—— 完整清单见 §A13/§A14/§A16
诊断面板前端已删（后端 `insight` 保留）· `fmtSec()` 唯一耗时口径 · 「AI排」走对话窗口 · 选择厨房落 `devices_user.json` · 两处「恢复默认」。⚠ 右栏并排按钮**必须** `flex-wrap:wrap`+`min-width:0`。

> ⚠ **第三轨"净收益百分比变小 ≠ 回归"**：基线自己变好 ⇒ 边际收益自然收窄。判据永远是 **0 更差**。
> ⚠ `bench.run(out_name=...)` 写进 `data/` ⇒ 被 `sync_publish` 同步进 `publish/`。A/B 跑完即删。
> ⚠ **改源码后必须重启 uvicorn**；判据是 `/api/recipes?limit=1` 的 id 形状，不是 `/health`。
> ⚠ 本机 `http_proxy=127.0.0.1:64582` **会劫持 127.0.0.1**（服务不在时报 **502**）⇒ 探针一律 `curl --noproxy '*'`。

**五条铁律**：① **过期的审计脚本比没有更危险**。② **护栏一假报警，真回归也会被无视**（先 `_strip_js_comments()`）。③ **子串断言是重灾区** ⇒ 锚点落在**命令行/整行**。④ **依赖墙上时钟的护栏 = 定时炸弹**（用环形差 `(a−b)%1440`）。⑤ **断言"前提成立"的测试会随代码变好而失效** ⇒ 先试首选、失败自动扫候选池。
> **护栏必须证明它会响**：新检查先做**变异自检**。⚠ **被别的机制兜住的判据，语料变异测不出来** ⇒ 用**合成句**钉（§C-补8.5）。

**唯一"不要回滚"**：配件词剥离让 6/50 组出锅差变差 —— 修对了（§C4）。**文档索引**：`技术方案.md` · `docs/部署文档.md` · `docs/建议文本对照与优化.md` · `docs/最优性审计_2026-10-04.md` · `docs/调度算法建模.md` · `docs/框架完整性检查报告.md`。
