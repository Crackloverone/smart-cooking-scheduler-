"""
变异测试：给 app/web/index.html 注入缺陷，确认前端静态护栏**真的会失败**。

覆盖 `tests/test_web_ui.py`（排程页布局）与 `tests/test_product_fixes.py`
（P-01~P-07 产品修复）两个文件，按测试名自动判断该跑哪一个。

守卫写了不跑等于没写 —— 尤其是 CSS 断言，正则写歪了会永远为真。
本脚本逐条注入、逐条跑 pytest、最后打印命中矩阵。

⚠ **新增前端代码可能让既有变异的锚点从 1 次变 2 次** ⇒ 会打 `[跳过]`
（跳过 = 那条护栏已经没人验了）。改完前端**必须跑完整套件**，
单跑目标测试看不出来这个坑。

安全设计：**从不改动仓库里的真文件**。变异写进 .workbuddy-ai/tmp/ 下的副本，
用环境变量 FT_WEB_HTML 让护栏去读副本。这样即使进程被强杀，
页面文件也是干净的。

用法：python -m scripts._mutate_web_guards
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "app" / "web" / "index.html"
SCRATCH = ROOT / ".workbuddy-ai" / "tmp" / "web_mutated.html"
# 前端静态护栏分布在两个文件里（排程页布局 / 产品问题修复），
# 按测试名自动判断该跑哪一个 —— 避免"把测试名写进错文件里, 收集不到却当成通过"。
TEST_FILES = ("tests/test_web_ui.py", "tests/test_product_fixes.py", "tests/test_insight.py")

# (说明, 原串, 变异串, 应当失败的测试名)
MUTATIONS = [
    # 原串必须带上后面的 padding —— 只写 `height:calc(100vh - var(--chrome))` 的话，
    # 手机断点里的 `min-height:calc(100vh - var(--chrome))` 也会被命中，
    # 计数变成 2 直接 [跳过]（跳过 = 这条护栏已经没人验了）。
    # 加了 safe-area 之后 padding 变成 calc()，锚点改用"换行 + 缩进 + padding:10px"，
    # 手机断点那份是 9 空格缩进 + padding:8px，不会撞上。
    ("排程页高度改回按内容撑高",
     "#v-plan.active{display:flex;flex-direction:column;\n       height:calc(100vh - var(--chrome));",
     "#v-plan.active{display:flex;flex-direction:column;\n       height:auto;",
     "test_plan_view_height_comes_from_the_viewport"),
    ("把 ③ 动态事件 与 已选 的位置对调",
     "<h3>③ 动态事件 · 重排</h3>",
     "<h3>已选</h3>",
     "test_selected_card_sits_after_the_dynamic_event_card"),
    # 只改**基础规则**那一份（带 min-height:0;min-width:0;）。加适配断点后，
    # `#v-plan .grid3>div:not(.card){` 在 3 个媒体查询里又各出现一次，
    # 直接拿选择器当锚点会数到 4 次 → [跳过]。
    ("列间距规则去掉 :not(.card) 排除",
     "#v-plan .grid3>div:not(.card){min-height:0;min-width:0;",
     "#v-plan .grid3>div{min-height:0;min-width:0;",
     "test_column_gap_rule_excludes_the_middle_card"),
    ("时间轴容器不再可滚动",
     "#v-plan #gantt{flex:1;min-height:0;min-width:0;overflow:auto}",
     "#v-plan #gantt{flex:1;min-height:0;min-width:0;overflow:hidden}",
     "test_gantt_is_a_horizontal_scroll_container"),
    ("视图切换按钮丢掉 width:auto",
     "#v-plan .vbtn{font-size:12px;padding:6px 20px;min-width:92px;width:auto}",
     "#v-plan .vbtn{font-size:12px;padding:6px 20px;min-width:92px}",
     "test_view_switch_buttons_are_not_full_width"),
    # 气泡尺寸统一后，锚点从字面量 66px 换成变量表达式。⚠ 这条表达式在 5 个
    # 断点里各出现一次，必须带上 `padding:10px`（只有桌面那份是 10px）才唯一。
    ("排程页底部不再给悬浮球留白（留白写死 12px，脱离 --fab-size）",
     "padding:var(--page-top-gap) calc(16px + var(--sa-r))\n"
     "               calc(var(--fab-size) + var(--fab-inset-plan) + var(--sa-b))",
     "padding:var(--page-top-gap) calc(16px + var(--sa-r))\n"
     "               calc(12px + var(--sa-b))",
     "test_fab_clearance_is_reserved_in_the_plan_view"),
    # --- 悬浮 AI 球尺寸统一（2026-10-04）：防"某个断点又把球缩回去" ---
    ("手机断点重新把气泡缩到 52px（同一控件两种大小）",
     "    :root{--fab-inset:14px}",
     "    :root{--fab-inset:14px}\n    .fab{width:52px;height:52px}",
     "test_fab_size_is_defined_exactly_once"),
    ("基础 .fab 的宽高改回字面量（变量被架空，改一处不生效）",
     "width:var(--fab-size);height:var(--fab-size);border-radius:50%",
     "width:60px;height:60px;border-radius:50%",
     "test_fab_size_is_defined_exactly_once"),
    ("nowline 不再渲染在 .gantt-inner 定位基准层里",
     '<div class="nowline" id="nowline" data-t="t=0" style="left:0%;top:0;bottom:0;position:absolute"></div>',
     '<div class="nowline-moved"></div>',
     "test_gantt_is_a_horizontal_scroll_container"),
    # --- 播放条（第三轮）：换皮与"单一同步点"两条护栏 ---
    ("时间轴滑块退回 Chromium 原生皮肤（去掉 appearance:none）",
     "appearance:none;-webkit-appearance:none;cursor:pointer;border-radius:999px}",
     "appearance:auto;-webkit-appearance:auto;cursor:pointer;border-radius:999px}",
     "test_timeline_slider_is_themed_not_native"),
    ("时间轴滑块自身背景改回不透明（原生白底会盖住自绘轨道）",
     "height:32px;background:transparent;",
     "height:32px;background:#ffffff;",
     "test_timeline_slider_is_themed_not_native"),
    ("syncPlaybar 不再写 --p（已播放填充不跟着走）",
     "s.style.setProperty('--p',((v-lo)/Math.max(hi-lo,1e-9)*100).toFixed(2)+'%');",
     "s.style.setProperty('--x',((v-lo)/Math.max(hi-lo,1e-9)*100).toFixed(2)+'%');",
     "test_playbar_sync_has_a_single_source_of_truth"),
    ("seekT 绕开 syncPlaybar 自己改滑块（填充/读数不再同步）",
     "  CUR_T=+v;\n  syncPlaybar(v);",
     "  CUR_T=+v;\n  document.getElementById('tSlider').value=v;",
     "test_playbar_sync_has_a_single_source_of_truth"),
    # --- 视图切换按钮（第四轮）：可读性与尺寸 ---
    ("视图按钮丢掉显式 color（继承全局 button 的白字 ⇒ 白底白字看不见）",
     "       color:var(--fg);box-shadow:none;cursor:pointer;",
     "       box-shadow:none;cursor:pointer;",
     "test_view_switch_buttons_are_readable_in_every_state"),
    ("视图按钮 hover 改用主色（与选中态混淆）",
     "  .vbtn:hover{background:rgba(255,255,255,.92);color:var(--fg);",
     "  .vbtn:hover{background:rgba(224,90,26,.95);color:var(--fg);",
     "test_view_switch_buttons_are_readable_in_every_state"),
    ("选中按钮 hover 不再保持黑底",
     "  .vbtn.active:hover{background:#111;color:#fff;border-color:#111}",
     "  .vbtn.active:hover{background:rgba(255,255,255,.92);color:var(--fg);border-color:#111}",
     "test_view_switch_buttons_are_readable_in_every_state"),
    ("视图按钮丢掉 min-width（宽度参差、按钮过小）",
     "min-width:92px;width:auto}",
     "width:auto}",
     "test_view_switch_buttons_are_wide_enough"),

    # --- 产品问题修复（P-01 ~ P-04）：布局降级 / 触控 / 状态可观测 / 删除 / 步骤详情 ---
    ("手机断点里三栏/两栏不再降级为单列",
     ".grid3,.grid2{grid-template-columns:minmax(0,1fr)}",
     ".grid3,.grid2{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}",
     "test_grids_collapse_to_one_column_on_phone"),
    ("甘特条退回 mousedown（触屏拖不动也点不开）",
     "b.addEventListener('pointerdown',e=>{",
     "b.onmousedown=e=>{",
     "test_gantt_bars_use_pointer_events"),
    ("甘特条丢掉 touch-action（横向拖拽被页面滚动抢走）",
     "touch-action:pan-y}",
     "}",
     "test_gantt_bar_declares_touch_action"),
    # 锚点必须带上紧随其后的 .item —— 平板断点里现在也有一条 `button{min-height:44px}`，
    # 只写那一句会数到 2 次 → [跳过]。
    ("手机端按钮不再抬到 44px 点击目标",
     "    button{min-height:44px}\n    .item{padding:13px 12px;min-height:44px}",
     "    button{min-height:0}\n    .item{padding:13px 12px;min-height:44px}",
     "test_tap_targets_are_big_enough_on_phone"),
    ("顶栏模型状态徽标被改名（前端再也拿不到状态）",
     'id="llmBadge"',
     'id="llmBadgeX"',
     "test_frontend_shows_llm_state_badge"),
    ("降级提示丢掉一次性早退（每次排程都弹）",
     "if(LLM_NOTIFIED)return;",
     "if(LLM_NOTIFIED)LLM_NOTIFIED=false;",
     "test_frontend_notifies_degradation_only_once"),
    ("删除菜谱丢掉二次确认",
     "  if(!confirm('确定删除这道菜谱吗？\\n\\n'",
     "  if(!alert('确定删除这道菜谱吗？\\n\\n'",
     "test_frontend_has_confirm_and_restore_entry"),
    ("恢复菜谱的入口被去掉（删完就找不回来）",
     "const r=await apiFetch('/api/recipes/'+encodeURIComponent(id)+'/restore'",
     "const r=await apiFetch('/api/recipes/'+encodeURIComponent(id)",
     "test_frontend_has_confirm_and_restore_entry"),
    ("编辑也走 POST（点「保存」变成又建一条）",
     "id?'PUT':'POST'",
     "id?'POST':'POST'",
     "test_frontend_sends_the_right_http_verb"),
    ("菜谱库角标退回「只报 total」（搜索时假装被截断）",
     "const shownN=d.recipes.length;",
     "const shownN=d.total;",
     "test_library_count_distinguishes_total_from_shown"),
    ("任务明细表丢掉「操作步骤」列",
     "<th>操作步骤</th>",
     "<th>说明</th>",
     "test_task_table_shows_the_raw_step_text"),
    ("甘特条不再能打开步骤详情",
     "if(!wasMoved&&Number.isFinite(i))openStepDetail(i);",
     "if(!wasMoved&&Number.isFinite(i))void 0;",
     "test_step_detail_is_reachable_from_both_entry_points"),
    ("步骤详情不再处理合成行（dish 为空 ⇒ 空白）",
     "if(synth&&(t.dish_list||[]).length)",
     "if(false)",
     "test_step_detail_handles_synthetic_and_prep_rows"),
    ("明细表 raw_text 不再转义（原文里的尖括号会破坏结构）",
     'title="${esc(t.raw_text||\'\')}"',
     'title="${t.raw_text||\'\'}"',
     "test_raw_step_text_is_escaped_before_injection"),
    ("任务下标不再赋值（甘特条的 data-idx 变空串 ⇒ 点不开详情）",
     "p.tasks.forEach((t,i)=>{t._idx=i;});",
     "void 0;",
     "test_task_index_is_assigned_before_the_gantt_is_drawn"),
    # --- P-05 后端不可达：不许永远"加载中..." ---
    ("漏一个裸 fetch 绕过统一出口（该视图会永远转圈）",
     "const r=await apiFetch('/api/recipes?q='",
     "const r=await fetch('/api/recipes?q='",
     "test_all_api_calls_go_through_one_wrapper"),
    ("后端不可达时不再替换「加载中...」占位符",
     "if(el.textContent.trim()==='加载中...')el.textContent='后端未连接';",
     "void 0;",
     "test_backend_down_replaces_the_loading_placeholder"),
    # 锚点必须带上前一行 `if(t)t.innerHTML=backendHintHtml();` ——
    # Turn P 新增的 markAuthRequired() 里也有一句 `bar.classList.add('on');`，
    # 只写那一句会数到 2 次 ⇒ [跳过]（跳过 = 这条护栏已经没人验了）。
    ("横幅不再显示（失败态退回静默）",
     "    if(t)t.innerHTML=backendHintHtml();\n    bar.classList.add('on');",
     "    if(t)t.innerHTML=backendHintHtml();\n    void 0;",
     "test_backend_down_replaces_the_loading_placeholder"),
    ("菜谱列表加载失败不再给结论（退回永远转圈）",
     "'<div class=\"empty\">'+(BACKEND_DOWN?'后端未连接':'加载失败')+'</div>';\n      return;",
     "'<div class=\"empty\">加载中...</div>';\n      return;",
     "test_backend_down_replaces_the_loading_placeholder"),
    ("file:// 打开不再给专门的指引",
     "if(location.protocol==='file:')",
     "if(false)",
     "test_file_protocol_gets_its_own_hint"),
    ("横幅改用纯文本版渲染（地址变成不可点的死字）",
     "innerHTML=backendHintHtml()",
     "innerHTML=backendHint()",
     "test_file_protocol_gets_its_own_hint"),
    ("apiFetch 把 HTTP 4xx 也当成「后端挂了」（403 的真实原因被盖掉）",
     "  markBackendUp();\n  // 401",
     "  if(!r.ok){markBackendDown(new Error('HTTP '+r.status));}\n  markBackendUp();\n  // 401",
     "test_http_error_is_not_mistaken_for_backend_down"),
    ("模型状态失败时覆盖掉「后端未连接」徽标",
     "    if(BACKEND_DOWN)return;\n    el.textContent='○ 状态未知'",
     "    el.textContent='○ 状态未知'",
     "test_llm_badge_does_not_override_backend_down"),
    ("横幅的「重试」只关横幅、不重新拉数据",
     "  markBackendUp();\n  loadRecipes(); loadPresets(); refreshLlmStatus(true);",
     "  markBackendUp();",
     "test_retry_button_is_wired"),

    # ---- 屏幕尺寸适配（断点 / 安全区）----------------------------------------
    # 这一组防的是"改一行 CSS，某几种尺寸上悄悄坏掉" —— 桌面看不出、手机看不出，
    # 只有 iPad 竖屏或手机横屏才坏，最容易在改版时被漏掉。
    ("viewport 去掉 viewport-fit=cover（刘海屏安全区恒为 0）",
     'content="width=device-width, initial-scale=1.0, viewport-fit=cover"',
     'content="width=device-width, initial-scale=1.0"',
     "test_viewport_opts_into_full_bleed_for_notched_screens"),
    ("安全区 env() 去掉 0px 兜底（桌面浏览器上整条 padding 作废）",
     "--sa-t:env(safe-area-inset-top,0px);",
     "--sa-t:env(safe-area-inset-top);",
     "test_safe_area_insets_are_declared_with_fallback_and_used"),
    ("顶栏不再让出安全区（刘海挡住导航）",
     "padding:0 calc(16px + var(--sa-r)) 0 calc(16px + var(--sa-l))}",
     "padding:0 16px}",
     "test_safe_area_insets_are_declared_with_fallback_and_used"),
    ("平板竖屏断点被改窄（768×1024 掉回两栏）",
     "@media (min-width:641px) and (max-width:1024px){",
     "@media (min-width:641px) and (max-width:1023px){",
     "test_responsive_breakpoints_cover_the_whole_size_range"),
    ("折叠屏窄屏断点被放宽（320px 重新掉回 640 那套内边距）",
     "  @media (max-width:360px){",
     "  @media (max-width:600px){",
     "test_narrow_breakpoints_are_declared_after_wide_ones"),
    # 锚点必须带上换行 + padding:8px —— 平板竖屏断点里也有一模一样的
    # `height:auto;min-height:calc(100vh - var(--chrome));` 前缀，不带就数到 2 次。
    ("手机断点里排程页重新锁死视口高度（内容被截掉且滑不到）",
     "#v-plan.active{height:auto;min-height:calc(100vh - var(--chrome));\n"
     "         padding:var(--page-top-gap) calc(10px + var(--sa-r))",
     "#v-plan.active{height:calc(100vh - var(--chrome));min-height:calc(100vh - var(--chrome));\n"
     "         padding:var(--page-top-gap) calc(10px + var(--sa-r))",
     "test_plan_view_releases_its_viewport_height_on_small_screens"),
    ("去掉 iOS 横屏文字自动放大开关（横屏被撑出横向滚动条）",
     "html{-webkit-text-size-adjust:100%;text-size-adjust:100%}",
     "html{-webkit-text-size-adjust:auto;text-size-adjust:100%}",
     "test_ios_landscape_does_not_inflate_text"),
    ("手机端 .chip 不再抬到 44px（不是 button，吃不到全局规则）",
     ".chip{padding:9px 13px;min-height:44px;display:inline-flex;align-items:center}",
     ".chip{padding:9px 13px;display:inline-flex;align-items:center}",
     "test_touch_targets_cover_phones_and_tablets"),
    ("平板端 .chip 不再抬到 44px（iPad 被当成鼠标设备）",
     ".chip{min-height:44px;display:inline-flex;align-items:center}",
     ".chip{display:inline-flex;align-items:center}",
     "test_touch_targets_cover_phones_and_tablets"),
    ("平板断点整体丢掉触控目标（iPad 上按钮全偏小）",
     "    button{min-height:44px}\n    .item{min-height:44px}",
     "    .item{min-height:44px}",
     "test_touch_targets_cover_phones_and_tablets"),

    # ---- P-06 访问令牌（后端 FT_AUTH_TOKEN）----------------------------------
    # 这一组防的是"部署后页面全空、但看不出为什么"：后端一开鉴权，
    # 前端任何一处没跟上都表现为白屏或"后端未连接"。
    ("前端不再从 ?token= 取令牌（部署链接带令牌也没用）",
     "AUTH_TOKEN=new URLSearchParams(location.search).get('token')"
     "||localStorage.getItem('ft_token')||'';",
     "AUTH_TOKEN='';",
     "test_access_token_comes_from_url_and_is_remembered"),
    ("取到令牌后不持久化（刷新一次就掉）",
     "if(AUTH_TOKEN)localStorage.setItem('ft_token',AUTH_TOKEN);",
     "void 0;",
     "test_access_token_comes_from_url_and_is_remembered"),
    ("把令牌硬编码进源码（令牌随源码一起给出去）",
     "let AUTH_TOKEN='';",
     # 变异串刻意**不写成 sk- 形状** —— tests/test_security.py 的凭据扫描会扫
     # scripts/ 目录，写成像真凭据的字面量会让那条护栏把本文件也报出来。
     "let AUTH_TOKEN='hardcoded-token-value';",
     "test_token_is_never_hardcoded_in_the_frontend"),
    ("apiFetch 不再注入令牌头（所有接口都 401）",
     "  if(AUTH_TOKEN)opts.headers=Object.assign({},opts.headers,"
     "{'X-Auth-Token':AUTH_TOKEN});",
     "  void 0;",
     "test_api_fetch_attaches_the_token_header"),
    ("注入令牌时覆盖调用方 headers（POST 的 Content-Type 被冲掉）",
     "Object.assign({},opts.headers,{'X-Auth-Token':AUTH_TOKEN})",
     "{'X-Auth-Token':AUTH_TOKEN}",
     "test_api_fetch_attaches_the_token_header"),
    ("401 被当成「后端挂了」（真正原因永远看不到）",
     "  if(r.status===401)markAuthRequired();",
     "  if(r.status===401)markBackendDown(new Error('HTTP 401'));",
     "test_401_gets_its_own_message_not_backend_down"),
    ("令牌错误提示不再告诉用户怎么修",
     "?token=你的令牌",
     "?tok=你的令牌",
     "test_401_gets_its_own_message_not_backend_down"),
    ("「后端恢复了」顺手关掉令牌错误的横幅（提示一闪而过）",
     "if(bar&&!AUTH_FAILED)bar.classList.remove('on');",
     "if(bar)bar.classList.remove('on');",
     "test_auth_failure_survives_backend_recovery"),

    # ---- P-07 列表口径（界面条数必须等于真实渲染条数）-------------------------
    ("列表被截断却不说（用户以为能翻到全部）",
     "allRecipes.length>shown.length",
     "false",
     "test_recipe_list_discloses_truncation"),
    # ⚠ 原锚点 `d.recipes.length<d.total?` 在 2026-10-02 角标改造后失效（`[跳过]`
    #    = 那条护栏已经没人验了）。同一条护栏已有「角标退回只报 total」覆盖，
    #    这里不再保留重复用例，改为把锚点对准**当前**代码里的兜底写法。
    ("详情丢掉 404 专属文案（id 变更时只说「加载失败」）",
     "(r.status===404?'这道菜不在菜谱库里（id 可能已变更）'"
     ":'加载失败（HTTP '+r.status+'）')",
     "('加载失败（HTTP '+r.status+'）')",
     "test_recipe_detail_handles_404_and_empty_payload"),
    ("详情模板不再给字段兜底（屏幕上出现 undefined）",
     "${d.cook_time??'—'}",
     "${d.cook_time}",
     "test_recipe_detail_handles_404_and_empty_payload"),

    # ---- 设置页（2026-10-03：新增站点 + 模型接入 + 设备并入）-------------------
    # 子页签与排程页的 .vbtn 是同一类控件，踩的是同一个坑，所以护栏也照抄一份。
    ("设置页子页签丢掉显式 color（继承全局 button 白字 ⇒ 白底白字）",
     "       color:var(--muted);box-shadow:none;font-weight:500}",
     "       box-shadow:none;font-weight:500}",
     "test_settings_subtabs_are_readable_in_every_state"),
    ("设置页子页签丢掉 width:auto（被全局 button 撑成整行）",
     ".stab{width:auto;min-width:96px;",
     ".stab{min-width:96px;",
     "test_settings_subtabs_are_readable_in_every_state"),
    ("设置页子页签 hover 改用主色（与选中态混淆）",
     ".stab:hover{background:rgba(255,255,255,.95);color:var(--fg);box-shadow:none}",
     ".stab:hover{background:rgba(224,90,26,.95);color:var(--fg);box-shadow:none}",
     "test_settings_subtabs_are_readable_in_every_state"),
    ("子页签面板改用 inline style 切换（面板状态出现两份真源）",
     "  if(panel)panel.classList.add('active');",
     "  if(panel)panel.style.display='block';",
     "test_settings_panels_switch_by_class_not_inline_style"),
    ("模型面板不再说明「当前模型从哪来」",
     "  const srcTxt={env:'环境变量 FT_LLM_MODEL',settings:'界面选择（已保存）',default:'内置默认值',"
     "\n                factory:'出厂预置（随程序附带）'}[d.model_source]||d.model_source;",
     "  const srcTxt='内置默认值';",
     "test_model_panel_explains_where_the_model_came_from"),
    ("保存设备配置改回 alert（结果不留痕）",
     "  _setLog('setDevLog','设备配置已保存，下次排程生效。','ok');",
     "  alert('设备配置已保存');",
     "test_settings_page_has_no_alert_in_the_save_paths"),
    ("设备卡不再展示能力信息（只剩一个看不懂的数字）",
     "  try{ spec=((await(await apiFetch('/api/device_spec')).json()).devices)||{}; }",
     "  try{ spec={}; }",
     "test_device_cards_show_capability_alongside_the_number"),
    ("菜单改回独立的「设备」站点（站点数与探针不同步）",
     "    <button onclick=\"show('settings',this)\" title=\"设置（快捷键 4）\">设置</button>",
     "    <button onclick=\"show('devices',this)\" title=\"设备\">设备</button>",
     "test_settings_site_replaces_the_device_site"),
    # 设置页新增了「保存令牌」这条赋值路径 ⇒ 必须证明"不许硬编码"这条护栏
    # 在新形状下仍然有牙齿（旧断言只认白名单形状，换了写法就变成永远为真）。
    # ⚠ 变异串刻意不用 `sk-` 前缀 —— 那会被**另一条**凭据扫描护栏命中，
    #   于是"被捕获"了但捕获它的不是这条护栏，等于没验。
    ("令牌被硬编码进源码（字面量）",
     "  AUTH_TOKEN=localStorage.getItem('ft_token')||'';",
     "  AUTH_TOKEN='tok-abc123';",
     "test_token_is_never_hardcoded_in_the_frontend"),

    # ---- 设置页的写操作：两条失败路径（2026-10-03 实测 422 被显示成"已保存"）----
    # 锚点带上缩进，确保只命中 saveDevices() 里那一段（saveModel 里没有 HTTP 后缀）。
    ("设备保存不再判 HTTP 状态（422/500 被显示成已保存）",
     "  if(!r.ok){\n"
     "    _setLog('setDevLog','保存失败：'+_httpMsg(d,r)+'（HTTP '+r.status+'）','err');\n"
     "    return;\n"
     "  }\n",
     "",
     "test_settings_save_paths_check_the_http_status"),
    ("错误文案退回直接拼 detail（422 显示 [object Object]）",
     "  const det=d&&d.detail;\n"
     "  if(typeof det==='string'&&det)return det;\n"
     "  if(Array.isArray(det)){\n"
     "    const m=det.map(e=>e&&(e.msg||e.type)).filter(Boolean).join('；');\n"
     "    if(m)return m;\n"
     "  }\n",
     "",
     "test_http_error_detail_is_rendered_as_text_not_an_object"),
    ("设备数量框不再拦空值（`+''` 变成 0 ⇒ 静默把设备下线）",
     "  let blank='';\n"
     "  document.querySelectorAll('#devRows input[data-k]').forEach(i=>{\n"
     "    if(i.value.trim()===''){blank=i.dataset.k;return;}\n"
     "    cfg[i.dataset.k]=+i.value;\n"
     "  });\n",
     "  document.querySelectorAll('#devRows input[data-k]').forEach(i=>cfg[i.dataset.k]=+i.value);\n",
     "test_device_qty_input_refuses_blank_instead_of_silently_disabling"),
    ("设备数量上限写死回 8（与后端 _DEVICE_LIMITS 的两份真源）",
     'max="${qmax}"',
     'max="8"',
     "test_device_qty_limits_come_from_the_backend"),

    # ---- 接入方式切换（2026-10-03：本地 Ollama / 远程 OpenAI 兼容端点）----------
    # 这几条守的都是"静默失败"：不报错、不改红别的测试，只是功能悄悄不对。
    ("接入方式选项在前端写死（与后端 providers 成两份真源）",
     '<select class="inp" id="setProviderSel" onchange="onProviderPick()"></select>',
     '<select class="inp" id="setProviderSel" onchange="onProviderPick()">'
     '<option value="ollama">本地</option></select>',
     "test_provider_options_come_from_the_backend"),
    ("远程密钥框改成明文（肩窥 / 截图泄露）",
     'id="setRemoteKey" type="password" placeholder="留空 = 保持原值"',
     'id="setRemoteKey" type="text" placeholder="留空 = 保持原值"',
     "test_remote_api_key_is_never_rendered_back"),
    ("加载设置时不再清空密钥框（上一次的输入留在页面上）",
     "  rk.value='';\n  rk.placeholder=",
     "  rk.placeholder=",
     "test_remote_api_key_is_never_rendered_back"),
    ("保存时无条件带上 api_key（空串 ⇒ 每保存一次就抹掉一次密钥）",
     "    if(k)payload.api_key=k;",
     "    payload.api_key=k;",
     "test_blank_remote_fields_are_not_sent_so_saving_does_not_wipe_them"),
    ("保存时不再带 provider（接入方式永远切不过去）",
     "  if(provider)payload.provider=provider;\n",
     "",
     "test_blank_remote_fields_are_not_sent_so_saving_does_not_wipe_them"),
    ("显隐规则丢掉 #sp-model（特异性掉到 (0,2,0)，靠源序才压得住）",
     "  #sp-model.mode-local .remote-only{display:none}",
     "  .set-field.remote-only{display:none}",
     "test_provider_switch_hides_irrelevant_fields_by_class"),
    ("保存成功文案不再分档（远程切换被吓成「本机没有这个模型」）",
     "  if(!d.is_local){\n",
     "  if(false){\n",
     "test_save_result_wording_follows_the_provider"),
    ("测试连接失败时不再按真因分流（一律怪地址与密钥）",
     "    _setLog('setModelLog','测试失败（'+ms+'ms）：'+llmFailReason(d),'err');\n",
     "    _setLog('setModelLog','测试失败（'+ms+'ms）：请核对远程接口地址与 API Key。','err');\n",
     "test_test_connection_failure_names_the_real_cause"),
    ("失败原因分流不再识别 FT_LLM 总闸",
     "    if(llmKillSwitchOff(st))\n",
     "    if(false)\n",
     "test_test_connection_failure_names_the_real_cause"),
    ("设置页不再在动手前点出总闸（用户进来只会看到普通说明）",
     "  const killSwitch=llmKillSwitchOff(st);\n",
     "  const killSwitch=false;\n",
     "test_settings_page_warns_when_the_kill_switch_is_off"),
    ("失败原因分流不再按档位给提示（远程档叫用户去确认 Ollama）",
     "    +(cur.is_local?'请确认 Ollama 已启动、且该模型已下载。'\n",
     "    +(true?'请确认 Ollama 已启动、且该模型已下载。'\n",
     "test_save_result_wording_follows_the_provider"),
    # 「结构对、接线断」：状态不再落到 window._llmState 时，函数结构一字未改、
    # 其它断言全绿，但总闸**永远识别不出来**。
    ("模型状态不再落到 window._llmState（总闸永远识别不出）",
     "  window._llmState=d;\n",
     "  window._llmState=null;\n",
     "test_test_connection_failure_names_the_real_cause"),

    # ---- FT_LLM=off 是**全局总闸**（设置页改不动它）。部署时误加 setup.ps1 -Offline
    #      会让用户"切远程→保存成功→对话永远离线"，去翻模型配置白折腾。
    #      徽标 tooltip 和降级提示**两处都要**点出来，少一处就少一半人能看见。
    ("徽标 tooltip 不再点出 FT_LLM 总闸",
     "      bits.push(llmKillSwitchOff(d)\n",
     "      bits.push(true\n",
     "test_frontend_explains_the_ft_llm_kill_switch"),
    ("降级提示不再点出 FT_LLM 总闸",
     "    : (llmKillSwitchOff(d)?'总闸 FT_LLM=off",
     "    : (false?'总闸 FT_LLM=off",
     "test_frontend_explains_the_ft_llm_kill_switch"),

    # ---- 排程诊断面板（v3.3）已于 2026-10-08 整体移除 ---------------------------
    # 面板（HTML `#riskPanel` / CSS `.riskpanel` / `renderInsight()`）连同它的
    # 两条静态护栏 `test_前端有诊断面板且无裸模板`、`test_风险面板不抢甘特图的高度`
    # 一起下掉了 ⇒ 这一组 6 条变异没有护栏可守，**必须同步删除**。
    # ⚠ 忘了删的后果不是报错，而是 6 条全部 `[跳过] 原串在文件里出现 0 次`
    #   —— 汇总变成 95/101，看起来像"漏网"，实际是语料没了。
    # 后端 `insight` 字段仍在（`tests/test_insight.py::test_schedule端点带诊断字段`）。

    # --- 密钥打码（2026-10-06）：三处"写错了界面不报错、用户白折腾" ---
    ("密钥不再显示打码预览（用户认不出配的是哪一把钥匙）",
     "    rkn.textContent=rv.api_key_set?('已配置：'+(rv.api_key_masked||'')):'未配置';",
     "    rkn.textContent=rv.api_key_set?'已配置':'未配置';",
     "test_api_key_mask_is_shown_but_never_prefilled"),
    ("把打码预览回填进密钥输入框（用户保存一次就把它当真密钥提交）",
     "  rk.value='';\n"
     "  rk.placeholder=rv.api_key_set?'已配置（留空 = 不修改）':'粘贴 API Key（远程必填）';",
     "  rk.value=rv.api_key_masked||'';\n"
     "  rk.placeholder=rv.api_key_set?'已配置（留空 = 不修改）':'粘贴 API Key（远程必填）';",
     "test_api_key_mask_is_shown_but_never_prefilled"),
    ("提交前不再检查打码值（保存一次就把 `sk-pro1…ESb0` 写成真密钥）",
     "    if(k&&looksMaskedKey(k)){",
     "    if(false){",
     "test_masked_key_is_never_submitted"),
    ("前端判据只认星号、不认省略号（后端打出来的掩码漏过去）",
     "  return s.indexOf('…')>=0||s.indexOf('***')>=0;",
     "  return s.indexOf('***')>=0;",
     "test_masked_key_is_never_submitted"),
    ("界面不再提示「用的是出厂预置」（接收方以为配置丢了，再去乱填一遍）",
     "  if(d.factory_defaults&&d.factory_defaults.present&&!rv.base_url){",
     "  if(false){",
     "test_factory_defaults_are_surfaced_to_the_user"),
    ("模型来源文案丢掉 factory 一档（界面把英文枚举 `factory` 直接显示给用户）",
     "default:'内置默认',\n                factory:'出厂预置（随程序附带）'}",
     "default:'内置默认'}",
     "test_factory_defaults_are_surfaced_to_the_user"),

    # ---- 五项界面改动（2026-10-08）：插单 / 诊断面板下架 / 耗时改秒 / AI排交棒 / 选择厨房 ---
    # 这一组全是"写错了不报错、界面照常渲染，只是功能悄悄不对"的类型 ——
    # 静态护栏是唯一能挡住它们的东西，所以每条护栏都必须证明自己会响。
    ("耗时换算口径被抹掉（回常量 0）",
     "function fmtSec(ms){\n  const n=Number(ms);\n  return Number.isFinite(n)?(n/1000).toFixed(1):'—';\n}",
     "function fmtSec(ms){\n  return '0';\n}",
     "test_耗时按秒显示且只有一个换算口径"),
    ("统计卡片绕开 fmtSec 直接摆毫秒（单位标签也退回 ms）",
     "fmtSec(d.elapsed_ms)}</b><span>耗时(秒)</span>",
     "d.elapsed_ms}</b><span>耗时(ms)</span>",
     "test_耗时按秒显示且只有一个换算口径"),
    ("重排摘要绕开 fmtSec 直接摆毫秒",
     "耗时${fmtSec(d.elapsed_ms)}秒",
     "耗时${d.elapsed_ms}毫秒",
     "test_耗时按秒显示且只有一个换算口径"),
    ("插单不再读用户指定的菜（退回纯随机）",
     "      const sel=document.getElementById('insertPick');\n"
     "      const want=(sel&&sel.value)||'';\n"
     "      let pick=want;\n"
     "      if(!pick){",
     "      const want='';\n"
     "      let pick=want;\n"
     "      if(!pick){",
     "test_插单可以指定加哪一道菜"),
    ("候选池不再排除已选（同一道菜能插两遍）",
     "(INSERT_POOL||[]).filter(n=>!selected.some(s=>s.name===n));\n  sel.innerHTML=",
     "(INSERT_POOL||[]).filter(n=>n);\n  sel.innerHTML=",
     "test_插单可以指定加哪一道菜"),
    ("onEventChange 不再管插单下拉的显隐",
     "  const p=document.getElementById('insertPick');\n"
     "  if(p)p.style.display=(document.getElementById('eventType').value==='insert')?'':'none';",
     "  const p=null;",
     "test_插单可以指定加哪一道菜"),
    ("updSel 不再刷新候选池（已选变了、下拉还是旧的）",
     "  fillInsertPool();\n}\nfunction toggle(id){",
     "}\nfunction toggle(id){",
     "test_插单可以指定加哪一道菜"),
    ("重排期间不置忙（可反复点、响应互相覆盖）",
     "if(btn){btn.dataset.txt=btn.innerHTML;btn.disabled=true;btn.textContent='重排中…';}",
     "if(btn){btn.dataset.txt=btn.innerHTML;btn.textContent='重排中…';}",
     "test_重排期间按钮置忙且必然恢复"),
    ("finally 里不再恢复按钮（失败分支卡死在「重排中…」）",
     "if(btn){btn.disabled=false;btn.innerHTML=btn.dataset.txt||'应用重排';}",
     "if(btn){btn.innerHTML=btn.dataset.txt||'应用重排';}",
     "test_重排期间按钮置忙且必然恢复"),
    ("重排不再判 HTTP 状态（4xx 掉进成功分支）",
     "if(!r.ok){out.textContent=",
     "if(false){out.textContent=",
     "test_重排不再吞掉后端错误"),
    ("重排不再判业务错误体 {error}",
     "if(d&&d.error){out.textContent=`${desc}：重排失败 — ${d.error}`;return;}",
     "if(false){out.textContent=`${desc}：重排失败`;return;}",
     "test_重排不再吞掉后端错误"),
    ("插单成功不再并入 selected（甘特图 6 条、已选写 5 道）",
     "selected=selected.concat([{id:base.extra.name,name:base.extra.name}]);",
     "void 0;",
     "test_插单成功后已选集合跟着变"),
    ("插单成功不再刷新「已选」",
     "updSel(); loadRecipes(); fillInsertPool();",
     "loadRecipes(); fillInsertPool();",
     "test_插单成功后已选集合跟着变"),
    ("applyKitchen 不再判失败（下拉说谎：显示新预设、跑旧设备）",
     "  if(!r.ok||(d&&d.error)){\n"
     "    const why=(d&&d.error)?(d.error+'（'+((d.details||[]).join('；'))+'）'):('HTTP '+r.status);\n"
     "    if(note)note.textContent='切换失败：'+why;",
     "  if(false){\n"
     "    const why=(d&&d.error)?(d.error+'（'+((d.details||[]).join('；'))+'）'):('HTTP '+r.status);\n"
     "    if(note)note.textContent='切换失败：'+why;",
     "test_选择厨房_失败要报错并回退"),
    ("applyKitchen 请求抛错时不回退下拉",
     "    if(sel)sel.value=KITCHEN_APPLIED;      // 回退，别让下拉说谎",
     "    void 0;",
     "test_选择厨房_失败要报错并回退"),
    ("applyKitchen 失败不写 #kitchenNote（用户看不到反馈）",
     "  const note=document.getElementById('kitchenNote');\n  const p=PRESETS[name];",
     "  const note=null;\n  const p=PRESETS[name];",
     "test_选择厨房_失败要报错并回退"),
    ("loadPresets 不再读当前设备数（无法反查生效预设）",
     "cur=await(await apiFetch('/api/devices')).json();",
     "cur=null;",
     "test_选择厨房_按当前设备反查生效预设"),
    ("loadPresets 不再记录生效预设",
     "  KITCHEN_APPLIED=hit||'';",
     "  void 0;",
     "test_选择厨房_按当前设备反查生效预设"),
    ("loadPresets 不把反查结果写回下拉",
     "  sel.value=hit||'';",
     "  sel.value='';",
     "test_选择厨房_按当前设备反查生效预设"),
    ("反查只看灶眼数（不同厨房被判成同一个）",
     "cur && DEVICE_KEYS.every(x=>(PRESETS[k][x]??null)===cur[x]));",
     "cur && cur['灶眼数']===PRESETS[k]['灶眼数']);",
     "test_选择厨房_按当前设备反查生效预设"),
    ("applyKitchen 整份提交预设（说明/特征被当成未知设备）",
     "  DEVICE_KEYS.forEach(k=>{ if(p[k]!=null) cfg[k]=p[k]; });",
     "  Object.assign(cfg,p);",
     "test_选择厨房_只提交设备键"),
    ("AI排不再打开右下角对话窗口",
     "  toggleFloat(true);\n  const inp=document.getElementById('chatInput2');",
     "  const inp=document.getElementById('chatInput2');",
     "test_ai排_hands_the_text_to_the_chat_dock"),
    # 这一条把 chatInput2 从函数体里彻底摘掉（连 sendChat 一起换），
    # 于是「填进了 #chatInput2」这条断言是**第一个**失败的 —— 单独把它钉住。
    ("AI排把需求填进了别的元素（#chatInput2 被换掉）",
     "  const inp=document.getElementById('chatInput2');\n"
     "  if(!inp){alert('对话窗口没找到（页面结构变了？）');return;}\n"
     "  inp.value=text;\n"
     "  sendChat('chatBox2','chatInput2');   // 自动发送：按钮叫「AI排」，只填不发会让人以为没生效",
     "  const inp=document.getElementById('agentText');\n"
     "  if(!inp){alert('对话窗口没找到（页面结构变了？）');return;}\n"
     "  inp.value=text;",
     "test_ai排_hands_the_text_to_the_chat_dock"),
    ("AI排填完不发送（只填不发，看着像没生效）",
     "  sendChat('chatBox2','chatInput2');   // 自动发送：按钮叫「AI排」，只填不发会让人以为没生效",
     "  void 0;",
     "test_ai排_hands_the_text_to_the_chat_dock"),
    # ---------------------------------------------------- 恢复默认（2026-10-08）
    ("删掉动态事件模块的「恢复默认」按钮",
     '          <button class="ghost" id="eventResetBtn" onclick="resetEvent()"\n'
     '                  style="width:auto;flex:none;padding:0 14px;font-size:12px;white-space:nowrap">恢复默认</button>',
     "          <!-- 变异：按钮被删 -->",
     "test_动态事件有恢复默认按钮且真的重置"),
    ("恢复默认退回一个 #eventType 里不存在的档位",
     "const EVENT_DEFAULT='insert';",
     "const EVENT_DEFAULT='no_such_event';",
     "test_动态事件有恢复默认按钮且真的重置"),
    ("恢复默认不复位「插哪一道」（故障档的残留选择被带进插单）",
     "  if(ev)ev.value=EVENT_DEFAULT;\n  if(pick)pick.value='';",
     "  if(ev)ev.value=EVENT_DEFAULT;",
     "test_动态事件有恢复默认按钮且真的重置"),
    ("恢复默认不走 onEventChange()（旧结果摘要留着，与档位不符）",
     "  onEventChange();     // 清掉上一次的结果摘要 + 按档位显隐 #insertPick",
     "  void 0;",
     "test_动态事件有恢复默认按钮且真的重置"),
    ("恢复默认顺手发一次重排请求（点一下要等 20 秒）",
     "  if(ev)ev.value=EVENT_DEFAULT;",
     "  apiFetch('/reschedule',{method:'POST'});\n  if(ev)ev.value=EVENT_DEFAULT;",
     "test_动态事件有恢复默认按钮且真的重置"),
    ("删掉选择厨房的「恢复默认」按钮",
     '          <button class="ghost" id="kitchenResetBtn" onclick="resetKitchen()"\n'
     '                  style="width:auto;flex:none;padding:0 14px;font-size:12px;white-space:nowrap">恢复默认</button>',
     "          <!-- 变异：按钮被删 -->",
     "test_选择厨房有恢复默认按钮且走后端重置"),
    ("恢复默认改走 POST /api/devices（把默认值当成用户选择落盘）",
     "    r=await apiFetch('/api/devices/reset',{method:'POST'});",
     "    r=await apiFetch('/api/devices',{method:'POST'});",
     "test_选择厨房有恢复默认按钮且走后端重置"),
    ("恢复默认不判 HTTP 状态（4xx 掉进成功分支）",
     "  if(!r.ok||(d&&d.error)){\n"
     "    const why=(d&&d.error)?(d.error+'（'+((d.details||[]).join('；'))+'）'):('HTTP '+r.status);\n"
     "    if(note)note.textContent='恢复默认失败：'+why;",
     "  if(d&&d.error){\n"
     "    const why=(d&&d.error)?(d.error+'（'+((d.details||[]).join('；'))+'）'):('HTTP '+r.status);\n"
     "    if(note)note.textContent='恢复默认失败：'+why;",
     "test_选择厨房有恢复默认按钮且走后端重置"),
    ("恢复默认不判业务错误体 {error}",
     "  if(!r.ok||(d&&d.error)){\n"
     "    const why=(d&&d.error)?(d.error+'（'+((d.details||[]).join('；'))+'）'):('HTTP '+r.status);\n"
     "    if(note)note.textContent='恢复默认失败：'+why;",
     "  if(!r.ok){\n"
     "    const why=(d&&d.error)?(d.error+'（'+((d.details||[]).join('；'))+'）'):('HTTP '+r.status);\n"
     "    if(note)note.textContent='恢复默认失败：'+why;",
     "test_选择厨房有恢复默认按钮且走后端重置"),
    ("恢复默认的按钮卡在「恢复中…」不恢复",
     "    if(btn){btn.disabled=false;btn.innerHTML=btn.dataset.txt||'恢复默认';}",
     "    void 0;",
     "test_选择厨房有恢复默认按钮且走后端重置"),
    ("恢复默认后不重新反查生效预设（下拉停在已失效的预设上）",
     "  await loadPresets();\n  const txt=_devStr(d);",
     "  void 0;\n  const txt=_devStr(d);",
     "test_选择厨房有恢复默认按钮且走后端重置"),

    # ------ 顶栏穿模 / 站点顶部留白一致（2026-10-08）------
    # 用户反馈："智能排程界面上方的模块和导航栏穿模了"。
    # 根因是 sticky 的 top 只挪渲染位置、不占流内空间，必须靠 margin-top 补齐。
    ("顶栏只写 sticky top、不补 margin-top（首行模块被压 14px，穿模）",
     "  .nav{position:sticky;top:var(--nav-top);margin-top:var(--nav-top);z-index:30;",
     "  .nav{position:sticky;top:var(--nav-top);z-index:30;",
     "test_顶栏的流内位置必须等于它的_sticky_位置"),
    ("断点里又单独写死 .nav{top:...}（top 与 margin-top 各走一个值）",
     "    :root{--nav-top:calc(8px + var(--sa-t)); --page-top-gap:16px}\n"
     "    .nav{padding:0 calc(10px + var(--sa-r)) 0 calc(10px + var(--sa-l))}",
     "    :root{--page-top-gap:16px}\n"
     "    .nav{top:calc(8px + var(--sa-t));padding:0 calc(10px + var(--sa-r)) 0 calc(10px + var(--sa-l))}",
     "test_顶栏的流内位置必须等于它的_sticky_位置"),
    ("排程页又自己写小上内边距（10px < 顶栏偏移 14px ⇒ 又被压住）",
     "       padding:var(--page-top-gap) calc(16px + var(--sa-r))\n",
     "       padding:10px calc(16px + var(--sa-r))\n",
     "test_所有站点共用同一个顶部留白变量"),
    ("其他站点又写死 28px 上内边距（顶部留白出现第二个真源）",
     "       padding:var(--page-top-gap) calc(16px + var(--sa-r)) calc(48px + var(--sa-b)) calc(16px + var(--sa-l))}",
     "       padding:28px calc(16px + var(--sa-r)) calc(48px + var(--sa-b)) calc(16px + var(--sa-l))}",
     "test_所有站点共用同一个顶部留白变量"),
    ("长页档丢掉 align-items:start（探针会拿仪表盘口径去判长页）",
     "    #v-plan .grid3{flex:none;align-items:start}",
     "    #v-plan .grid3{flex:none}",
     "test_矮桌面摆不下仪表盘时降级成长页"),
    ("长页档去掉 #gantt 的高度下限（时间轴又被压成 0）",
     "    #v-plan #notes{max-height:38vh}\n"
     "    #v-plan #gantt{min-height:300px}",
     "    #v-plan #notes{max-height:38vh}\n"
     "    #v-plan #gantt{min-height:0}",
     "test_矮桌面摆不下仪表盘时降级成长页"),
    # ---- 控件可用性：回车提交 / 摆设控件（2026-10-08 第四轮）----
    # 这一组盯的是"用户以为界面坏了"的静默缺口：输入框敲回车没反应、
    # 或者某个控件根本没人读它。它们**不影响任何渲染结果**，静态看一切正常。
    ("AI 排菜输入框丢掉回车提交",
     '<input class="search" id="agentText" placeholder="🤖 一句话让AI排菜, 如:两荤一素快点" '
     'style="margin:0;flex:1"\n                 onkeydown="if(event.key===\'Enter\')agentPlan()">',
     '<input class="search" id="agentText" placeholder="🤖 一句话让AI排菜, 如:两荤一素快点" '
     'style="margin:0;flex:1">',
     "test_每个可编辑单行输入框都能回车提交"),
    # ⚠ 这一条**不是**"少了个回车"，而是"回车接错了动作" —— 用户以为存了令牌，
    #   实际改的是模型配置，界面还会回一句"已保存"。比没接更危险，所以单列一条。
    ("访问令牌输入框的回车接到了「保存模型」上",
     "onkeydown=\"if(event.key==='Enter')saveToken()\"",
     "onkeydown=\"if(event.key==='Enter')saveModel()\"",
     "test_关键输入框回车提交到正确的动作"),
    ("开饭时间又变回摆设（没人读那个输入框）",
     "function dinnerAnchor(){\n  const el=document.getElementById('dinnerTime');",
     "function dinnerAnchor(){\n  const el=null;",
     "test_开饭时间是墙上时钟锚点而不是摆设"),
    ("还没排程时不再刷新开饭提示（留着上一次的结论）",
     "    document.getElementById('taskCount').textContent='';\n"
     "    updGanttHint();\n"
     "    updDinnerHint();\n"
     "    updPlanTools(null);\n"
     "    showHints(null);\n"
     "    return;",
     "    document.getElementById('taskCount').textContent='';\n"
     "    updGanttHint();\n"
     "    updPlanTools(null);\n"
     "    showHints(null);\n"
     "    return;",
     "test_开饭时间是墙上时钟锚点而不是摆设"),
    # ⚠ 锚点必须落在**真的派发回车**那一行上，不能落在 `console.log("…回车键遍历…")`
    #   上：上面的注释块里同样写着"回车键遍历"，把日志行改掉后
    #   `"回车键遍历" in src` 照样为真 ⇒ 变异**不会被捕获**（假绿）。
    ("逐控件审计脚本不再真的派发回车事件",
     'new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })',
     'new window.KeyboardEvent("keydown", { key: "F13", bubbles: true, cancelable: true })',
     "test_遍历脚本仍在跑回车审计",
     "scripts/ui_click_audit.mjs"),
    # ---- 开饭提示必须恒定单行（真回归：长文案折行把「生成排程」推进悬浮球）----
    ("开饭提示不再强制单行（文案一长就折行）",
     "line-height:1.3;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap",
     "line-height:1.3;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:normal",
     "test_开饭提示必须恒定单行"),
    ("开饭提示改回 innerHTML（带 <br> 就必然多行）",
     "box.textContent=late?'⚠ 来不及':(fmtClock(start)+' 下厨');",
     "box.innerHTML=late?'⚠ 来不及':(fmtClock(start)+' 下厨');",
     "test_开饭提示必须恒定单行"),
    # ---- v3.7 计划工具与历史持久化（12 条）----
    ("历史行不再绑定还原动作（点下去没反应）",
     "onclick=\"restoreSchedule('${esc(h.sid)}',event)\"",
     'onclick=""',
     "test_历史行可点还原且保留cfg_row类"),
    ("还原历史时重新排了一次程（历史会被自己刷满）",
     "rec=await fetchSchedule(sid);",
     'rec=await (await apiFetch("/schedule",{method:"POST",headers:{'
     '"Content-Type":"application/json"},body:"{}"})).json();',
     "test_还原历史不会重新排程"),
    # ⚠ 这条钉的是**探针口径**而非业务：给复选框挂 onclick 会让穷举探针把它
    #   枚举成一个"点下去没反应"的控件，数字随历史条数漂移。
    ("历史行复选框自带 onclick（穷举探针把它扫成'没反应'）",
     'class="hist-pick" title="勾选后点「对比选中的 2 条」"',
     'class="hist-pick" onclick="event.stopPropagation()" '
     'title="勾选后点「对比选中的 2 条」"',
     "test_历史行复选框不许自带onclick"),
    ("勾选对比不设上限（勾第 3 条会把前两条都留下）",
     "while(CMP_SEL.length>2)CMP_SEL.shift();",
     "while(CMP_SEL.length>3)CMP_SEL.shift();",
     "test_对比最多两条且不给利用率标优劣"),
    ("给设备利用率标了优劣（它是仪表盘不是方向盘）",
     "cmpRow('设备利用率(%)',(A.kpi||{}).device_utilization??'-',"
     "(B.kpi||{}).device_utilization??'-',false)",
     "cmpRow('设备利用率(%)',(A.kpi||{}).device_utilization??'-',"
     "(B.kpi||{}).device_utilization??'-')",
     "test_对比最多两条且不给利用率标优劣"),
    ("CSV 不再加 UTF-8 BOM（Excel 打开中文乱码）",
     "const body=/csv/i.test(type)?'\\ufeff'+text:text;",
     "const body=text;",
     "test_导出CSV必须带UTF8_BOM"),
    ("作业单改用 window.open（拦截器会挡掉、无头环境报错）",
     "  if(!p||!p.tasks){alert('先生成一次排程再导作业单');return;}",
     "  window.open('about:blank');\n"
     "  if(!p||!p.tasks){alert('先生成一次排程再导作业单');return;}",
     "test_作业单不用window_open"),
    ("全局快捷键不再排除 <input>（在里面打字会误触发）",
     "if(tag==='input'||tag==='textarea'||tag==='select'||t.isContentEditable)return;",
     "if(tag==='textarea'||tag==='select'||t.isContentEditable)return;",
     "test_快捷键不抢输入框"),
    ("对话只在用户发言后保存（模型回复刷新即失）",
     "chatMsgs.push({role:'user',content:text});\n  chatSave();",
     "chatMsgs.push({role:'user',content:text});",
     "test_对话记录会持久化"),
    ("改菜单不再判 d.ok（失败被当成成功）",
     "if(!d.ok){",
     "if(false){",
     "test_改菜单必须按后端结论分流"),
    ("常驻诊断面板被加回 render()（用户明确要求删掉）",
     "function render(d){",
     "function render(d){ if(d.insight)document.getElementById('why').innerHTML="
     "String(d.insight);",
     "test_为什么这样排是按需弹层而不是常驻面板"),
    ("没有建议时不把容器清空（空容器会占高度挤甘特图）",
     "box.innerHTML=hs.length?",
     "box.innerHTML=true?",
     "test_不可行建议要能照着做"),
    ("计划工具栏按钮改回 disabled（点下去没反应）",
     '<button onclick="openWhy()" id="whyBtn"',
     '<button disabled onclick="openWhy()" id="whyBtn"',
     "test_工具栏按钮不许用disabled"),
    # ---- v3.8 C1：撤销条 + 二次确认 ----
    ("撤销条改成流内定位（会把甘特图挤变形）",
     ".undo-bar{position:fixed",
     ".undo-bar{position:relative",
     "test_撤销条必须是fixed且默认空容器"),
    ("撤销条默认不再隐藏（空壳永久占着底部）",
     "z-index:1002;display:none;",
     "z-index:1002;display:flex;",
     "test_撤销条必须是fixed且默认空容器"),
    ("撤销按钮没写 width:auto（会被全局 button 撑成整行）",
     ".undo-bar .ub-btn{width:auto;flex:none;",
     ".undo-bar .ub-btn{flex:none;",
     "test_撤销按钮要显式覆盖全局button样式"),
    ("关闭弹层时不清待执行的确认回调（下次确认会误触发它）",
     "  CONFIRM_FN=null;\n}",
     "}",
     "test_关闭弹层要丢掉待执行的确认回调"),
    ("runConfirm 先关弹层再取回调（拿到的永远是 null）",
     "const f=CONFIRM_FN; CONFIRM_FN=null;\n  closeInfo();",
     "closeInfo();\n  const f=CONFIRM_FN; CONFIRM_FN=null;",
     "test_关闭弹层要丢掉待执行的确认回调"),
    ("清空历史改成立刻删（撤销窗口是假的）",
     "setTimeout(commitClearHist,8000);",
     "commitClearHist();",
     "test_清空历史必须是延迟提交"),
    ("撤销清空时去重新拉列表（后端还没清，看着像撤销失败）",
     "renderHist();\n  setHistNote('已撤销清空');",
     "renderHist();\n  loadHist();\n  setHistNote('已撤销清空');",
     "test_清空历史必须是延迟提交"),
    ("删菜谱不再给撤销入口",
     "    undoToast('已删除「'",
     "    // undoToast('已删除「'",
     "test_删除菜谱留了撤销快照"),
    ("删菜谱快照改用展示数组（自建菜重建会丢格式）",
     "raw_ingredients||(Array.isArray(cur.ingredients)",
     "raw_steps||(Array.isArray(cur.ingredients)",
     "test_删除菜谱留了撤销快照"),
    ("恢复默认不留重置前快照（撤销点了没反应）",
     "const before=(window._dev&&typeof window._dev==='object')\n"
     "    ?JSON.parse(JSON.stringify(window._dev)):null;",
     "const before=null;",
     "test_恢复默认厨房可撤销"),
    ("撤销恢复默认时调了 /reset（变成再恢复一次默认）",
     "const r=await apiFetch('/api/devices',{method:'POST',\n"
     "      headers:{'Content-Type':'application/json'},body:JSON.stringify(before)});",
     "const r=await apiFetch('/api/devices/reset',{method:'POST',\n"
     "      headers:{'Content-Type':'application/json'},body:JSON.stringify(before)});",
     "test_恢复默认厨房可撤销"),
    ("清空对话撤销时没写回 localStorage（刷新一下又没了）",
     "    chatSave();\n    renderChatHistory();",
     "    renderChatHistory();",
     "test_清空对话与令牌可撤销"),
    ("清除令牌不再给撤销入口",
     "  undoToast('已清除访问令牌',()=>{\n"
     "    const i2=document.getElementById('setToken');\n"
     "    if(i2)i2.value=backup;\n"
     "    saveToken();\n"
     "    undoToast('已恢复访问令牌');\n"
     "  });",
     "  hideUndo();",
     "test_清空对话与令牌可撤销"),
    ("新确认入口退回 window.confirm",
     "askConfirm('清空对话',",
     "window.confirm('清空对话')&&askConfirm('清空对话',",
     "test_确认弹层不许用window_confirm"),

    # ---- v3.9 执行态回写 + 经验回流 ----
    # 盯的是三条容易在后续迭代里被悄悄破坏的取舍：
    #   ① 打点放弹层、不加明细表列（加列会动 table-layout:fixed 的几何）；
    #   ② 达到阈值只建议，绝不自动重排；
    #   ③ 报延迟不填实际用时必须拒绝，不许替用户编数。
    ("明细表加了第 7 列「执行」（会动 table-layout:fixed 的列宽分配）",
     "'<table><thead><tr><th>开始</th><th>结束</th><th>资源</th><th>菜/步</th>"
     "<th>操作步骤</th><th>值守</th></tr></thead><tbody>'",
     "'<table><thead><tr><th>开始</th><th>结束</th><th>资源</th><th>菜/步</th>"
     "<th>操作步骤</th><th>值守</th><th>执行</th></tr></thead><tbody>'",
     "test_执行打点不新增明细表列"),
    ("执行态标记挪到 hover 之前（鼠标一划过红色提示就没了）",
     "  #v-plan #tasks tbody tr:hover{background:rgba(242,101,34,.07)}\n"
     "  /* 执行态标记（v3.9）。⚠ 必须排在 hover **之后**：两者特异性相同\n"
     "     (2 id + 1 类/伪类 + 2 type)，后定义的赢；放前面会被 hover 盖掉，\n"
     "     鼠标一划过\"已延迟\"的红色提示就没了。\n"
     "     只改背景/透明度, **不新增列、不改宽度** ⇒ 布局探针零影响。 */\n"
     "  #v-plan #tasks tbody tr.exec-done{opacity:.5}\n"
     "  #v-plan #tasks tbody tr.exec-late{background:rgba(192,57,43,.10)}",
     "  /* 执行态标记（v3.9）。⚠ 必须排在 hover **之后**：两者特异性相同\n"
     "     (2 id + 1 类/伪类 + 2 type)，后定义的赢；放前面会被 hover 盖掉，\n"
     "     鼠标一划过\"已延迟\"的红色提示就没了。\n"
     "     只改背景/透明度, **不新增列、不改宽度** ⇒ 布局探针零影响。 */\n"
     "  #v-plan #tasks tbody tr.exec-done{opacity:.5}\n"
     "  #v-plan #tasks tbody tr.exec-late{background:rgba(192,57,43,.10)}\n"
     "  #v-plan #tasks tbody tr:hover{background:rgba(242,101,34,.07)}",
     "test_执行态标记_排在_hover_之后"),
    ("报延迟不填实际用时也放行（等于替用户编一个数喂进校准样本）",
     "  if(state==='delayed'&&!(actual>0)){",
     "  if(false){",
     "test_报延迟不填实际用时必须拒绝"),
    ("达到阈值直接自动重排（不再经过确认层）",
     "    if(d.deviation&&d.deviation.worth)askReplan(d.deviation);",
     "    if(d.deviation&&d.deviation.worth)deviationReplan(d.deviation);",
     "test_达到阈值只建议不自动重排"),
    ("重排的 completed 由前端自己推断（冻结集会算错）",
     "                           completed:(dv&&dv.completed)||[]})});",
     "                           completed:Object.keys(EXEC_MAP)}));",
     "test_重排请求带上已完成步骤"),
    ("render 里 await 了 loadExec（画甘特图要等一次网络往返）",
     "  loadExec(CUR_SID);",
     "  await loadExec(CUR_SID);",
     "test_拉执行态不阻塞渲染"),
    ("去掉「执行经验」入口（用户看不到自己报的数被拿去做什么）",
     "        <button onclick=\"openCalib()\" id=\"calibBtn\" "
     "title=\"经验回流：系统从你报过的实际用时里学到了什么\">执行经验</button>\n",
     "",
     "test_执行经验入口可见"),
    ("少了「报延迟」按钮（这条学习路径永远攒不到样本）",
     "<button onclick=\"reportExec(${i},'delayed')\" title=\"这一步比计划慢了\">报延迟</button>",
     "",
     "test_打点按钮都有真实动作"),
]


def _file_for(test_name: str) -> str | None:
    """找出定义了这个测试的文件。找不到返回 None —— 调用方必须当成失败。"""
    for f in TEST_FILES:
        if f"def {test_name}(" in (ROOT / f).read_text(encoding="utf-8"):
            return f
    return None


def run_one(test_name: str, extra_env: dict | None = None) -> bool:
    """跑单个测试，返回是否通过。

    找不到测试文件时**返回 False**（而不是悄悄跑另一个文件）—— 测试名写错却
    报"未变异时就不通过", 至少是显式失败, 不会被当成"变异没被捕获"糊过去。
    """
    tf = _file_for(test_name)
    if tf is None:
        return False
    env = dict(os.environ)
    # ⚠ 先铺默认、再让 extra_env 覆盖：跨文件变异（如审计脚本）要用自己的开关，
    #   直接 `dict(os.environ, FT_WEB_HTML=…, **extra)` 会在两者同名时抛
    #   `got multiple values for keyword argument` —— 实测把整个套件打断在第一条。
    env["FT_WEB_HTML"] = str(SCRATCH)
    env.update(extra_env or {})
    r = subprocess.run(
        [sys.executable, "-m", "pytest", f"{tf}::{test_name}", "-q", "--no-header"],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    return r.returncode == 0


def main() -> int:
    original = HTML.read_text(encoding="utf-8")
    SCRATCH.parent.mkdir(parents=True, exist_ok=True)
    bad = 0
    # 基线：未变异时必须通过，否则说明环境本身有问题。
    # ⚠ 按**测试名去重**再跑：一条护栏通常被好几个变异盯着，逐个重跑会把
    #   基线阶段的耗时放大成 O(变异数)，而这里要证明的只是"这些测试本身是绿的"。
    #   （2026-10-06 实测：101 条变异只有 ~40 个不同测试，去重后基线快一半以上。）
    SCRATCH.write_text(original, encoding="utf-8")
    for test_name in sorted({m[3] for m in MUTATIONS}):
        if not run_one(test_name):
            print(f"  [环境异常] 未变异时 {test_name} 就不通过")
            return 2

    for m in MUTATIONS:
        # 4 元组 = 变异页面本身；5 元组 = 变异**另一个文件**（第 5 项是相对仓库根的
        # 路径），走各自的 env 覆盖开关。跨文件条目是 2026-10-08 加的：护栏开始
        # 钉审计脚本这类"非页面"文件，没有这条通路就只能"写了但从没验证它会响"。
        desc, old, new, test_name = m[:4]
        if len(m) == 4:
            target, scratch, env_key = HTML, SCRATCH, "FT_WEB_HTML"
        else:
            target = ROOT / m[4]
            scratch = SCRATCH.parent / ("mutated_" + Path(m[4]).name)
            env_key = {".mjs": "FT_CLICK_AUDIT"}.get(target.suffix, "FT_WEB_HTML")
        src = target.read_text(encoding="utf-8")
        n = src.count(old)
        if n != 1:
            print(f"  [跳过] {desc}: 原串在 {target.name} 里出现 {n} 次（应为 1）")
            bad += 1
            continue
        extra = {env_key: str(scratch)}
        scratch.write_text(src.replace(old, new), encoding="utf-8")
        caught = not run_one(test_name, extra)
        scratch.write_text(src, encoding="utf-8")
        restored = run_one(test_name, extra)
        ok = caught and restored
        bad += 0 if ok else 1
        print(f"  [{'OK ' if ok else 'BAD'}] {desc}")
        print(f"         变异后被捕获={caught}  还原后通过={restored}  ({test_name})")

    print(f"\n==== 变异测试: {len(MUTATIONS) - bad}/{len(MUTATIONS)} 条被守卫捕获 ====")
    # ⚠ 清理必须放在**汇总之后**：沙箱/CI 的批量删除守卫可能直接中止进程，
    #   放在前面会把「结论」一起吞掉、退出码变成 1 —— 看起来像护栏失败，
    #   实际 60/60 全过（2026-10-03 实测踩到两次）。
    for f in (SCRATCH, *SCRATCH.parent.glob("mutated_*")):
        try:
            f.unlink(missing_ok=True)
        except BaseException as e:      # 清理失败绝不影响结论
            print(f"  [提示] 临时文件清理被拦（不影响结论）: {type(e).__name__}")
    return 1 if bad else 0

if __name__ == "__main__":
    raise SystemExit(main())
