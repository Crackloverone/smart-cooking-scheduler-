"""
前端（app/web/index.html）的静态回归守卫。

为什么要有这一层
----------------
界面 bug 有两类，pytest 一行都测不到：

1. **引用错**：HTML 里写 `onclick="foo()"`，而 `foo` 根本没定义 —— 点下去只抛
   ReferenceError，用户看到的是"点了没反应"。实测踩过一次：`onEventChange`
   只被调用、从未定义，改一次下拉就报一次错。
2. **契约错**：JS 靠 `el.dataset.name` 认菜名，而模板里没渲染 `data-name` ——
   匹配永远失败，界面不报错、只是静默地什么都不选。（这是历史上真实踩过的形态；
   现在 `data-name` 已不再被 JS 读取，但属性仍随行渲染，见 `test_recipe_rows_carry_data_name`。）

这两类都**不需要跑起来就能静态查出来**，而且都是"改一行就悄悄回归"的类型，
所以放在 pytest 里当护栏最划算（scripts/ui_probe.mjs 才是真正的动态体检，
但它需要后端在跑，不适合进单测）。

注意：这里只锁**不变量**，不锁样式细节 —— 断言写得越像"实现"越容易假报警。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# 默认测真实页面；FT_WEB_HTML 可指向副本，供 scripts/_mutate_web_guards.py
# 做变异测试（往副本里注入缺陷），这样变异测试永远不会碰到仓库里的真文件。
HTML_PATH = Path(os.environ.get("FT_WEB_HTML")
                 or (Path(__file__).resolve().parents[1] / "app" / "web" / "index.html"))

# 这些是页面脚本在运行时**动态创建**的元素 id，静态 HTML 里当然找不到。
DYNAMIC_IDS = {"pending", "nowline"}

# 内联处理器里允许出现的浏览器/JS 内置函数（不是页面自定义函数）。
BUILTINS = {
    "if", "else", "for", "while", "return", "alert", "confirm", "prompt", "this", "event",
    "JSON", "Math", "String", "Number", "Array", "Object", "Boolean", "Date", "RegExp",
    "parseInt", "parseFloat", "isNaN", "setTimeout", "setInterval", "clearTimeout",
    "encodeURIComponent", "decodeURIComponent", "document", "window", "fetch", "console",
    "void", "typeof", "new", "function", "delete", "true", "false", "null", "undefined",
    "stopPropagation", "preventDefault",
}


@pytest.fixture(scope="module")
def html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _inline_handlers(html: str) -> list[str]:
    """抽出所有内联事件处理器属性值，如 onclick="foo();bar()"。"""
    return [m.group(1) for m in re.finditer(r'\son[a-z]+\s*=\s*"([^"]*)"', html)]


def _called_names(handler_body: str) -> set[str]:
    """取出处理器里被调用的**裸标识符**函数名。

    前面带 `.` 的是方法调用（JSON.stringify / x.replace），不可能"未定义"。
    """
    return {m.group(1) for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", handler_body)}


def _defined_names(html: str) -> set[str]:
    """页面里定义过的函数名：function foo( / async function foo( / const foo= / window.foo="""
    names = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", html))
    names |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", html))
    names |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", html))
    return names


def test_no_external_resources_so_page_works_offline(html: str):
    """页面不能依赖任何外链 —— 离线/内网演示时 CDN 拿不到就是白屏。"""
    offenders = re.findall(r'(?:src|href)\s*=\s*"https?://[^"]*"', html)
    offenders += re.findall(r"@import\s+url\(\s*['\"]?https?://", html)
    assert offenders == [], f"页面出现外链资源，离线会挂: {offenders}"


def test_every_inline_handler_is_defined(html: str):
    """内联 on* 里调用的每个函数都必须在页面里定义过。

    这是"点了没反应 + 控制台 ReferenceError"这一类 bug 的根因，必须静态堵死。
    """
    defined = _defined_names(html)
    called: set[str] = set()
    for body in _inline_handlers(html):
        called |= _called_names(body)
    missing = sorted(called - BUILTINS - defined)
    assert missing == [], f"内联处理器引用了未定义的函数: {missing}"


def test_every_getelementbyid_target_exists(html: str):
    """getElementById('x') 里的 x 必须在 HTML 里存在（动态创建的除外）。"""
    referenced = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", html))
    existing = set(re.findall(r'id\s*=\s*"([^"]+)"', html))
    missing = sorted(referenced - existing - DYNAMIC_IDS)
    assert missing == [], f"脚本引用了不存在的元素 id: {missing}"


def test_recipe_rows_carry_data_name(html: str):
    """菜谱行必须渲染 data-name。

    历史上 AI 排菜靠它把"识别到的菜名"对回列表行；没有这个属性时旧实现退回
    textContent 前 8 个字，而那是"菜名+分类+步数"的拼接，永远匹配不上 ——
    AI 排菜后「已选」会一直停在 0 道。
    2026-10-08 起 `agentPlan()` 改为把需求交给对话窗口，不再自己落选中状态，
    所以 `data-name` 目前没有 JS 读取方；保留它 + 这条护栏，是因为它是
    「行 ↔ 菜谱」唯一稳定的机器可读锚点（探针/未来的行级操作都要靠它），
    且删掉容易、想再加回来时往往已经忘了当年为什么必须有。

    用 rindex 而不是 index：loadRecipes() 现在**先**有一个失败分支也会写
    `getElementById('recipeList').innerHTML=`（后端不可达时给出明确结论而不是
    永远转圈）。取 index 会命中那个失败分支，于是模板里明明有 data-name 也报缺失。
    """
    idx = html.rindex("getElementById('recipeList').innerHTML")
    segment = html[idx: idx + 1200]
    assert "data-name=" in segment, "菜谱行模板缺少 data-name 属性"


def test_ai排_hands_the_text_to_the_chat_dock(html: str):
    """「AI排」必须把输入**交给右下角的对话窗口**并自动发送（2026-10-08 改）。

    为什么换掉原来那条直连 /api/agent_plan 的链路：那条只认菜名，认不出就弹
    「没识别到菜, 换个说法」，用户既不知道怎么换、也没法追问；而对话链路能解释
    为什么这么配，还会附「加入排程」按钮。两条链路并存时，"解释"和"最终排出来的
    计划"会出自不同上下文 —— 所以这里同时钉住"新链路要做"和"老链路不许回"。

    比对前先去掉注释：上面这段说明里正当地提到了 /api/agent_plan，不该被算成代码命中。
    """
    i = html.index("function agentPlan(")
    # 按大括号配平取函数体。⚠ 不能用 index("\n}\n") —— 函数体里就有独立成行的 `}`，
    # 那样只会截到中间，后面的断言全部落空、护栏变成永远为真（项目铁律②）。
    j = html.index("{", i)
    depth = 0
    for k in range(j, len(html)):
        if html[k] == "{":
            depth += 1
        elif html[k] == "}":
            depth -= 1
            if depth == 0:
                body = html[j:k + 1]
                break
    else:
        raise AssertionError("agentPlan 函数体大括号不闭合")
    code = re.sub(r"//[^\n]*", "", body)
    assert "toggleFloat(true)" in code, (
        "agentPlan 没有强制打开右下角对话窗口（toggleFloat(true)）"
    )
    assert re.search(r"chatInput2", code), "agentPlan 没有把需求填进对话输入框 #chatInput2"
    assert re.search(r"sendChat\(\s*['\"]chatBox2['\"]\s*,\s*['\"]chatInput2['\"]\s*\)", code), (
        "agentPlan 填完没有发送（要调用 sendChat('chatBox2','chatInput2')）"
    )
    assert "/api/agent_plan" not in code, (
        "agentPlan 又直连 /api/agent_plan 了 —— 两条链路并存会让解释与计划来自不同上下文"
    )


def test_gantt_bar_padding_stays_out_of_width(html: str):
    """.sbar 自身 padding 必须为 0。

    全局 box-sizing:border-box 下，宽度是按时间比例算出来的百分比：极短任务算出来
    只有 ~5px，一旦条自身带左右内边距，浏览器会把 border-box 宽度**兜底抬到
    内边距之和(16px)**，末端任务于是被顶出轨道右边界。内边距必须放在内部 span 上。
    """
    m = re.search(r"\.sbar\{([^}]*)\}", html)
    assert m, "找不到 .sbar 规则"
    # 必须严格是 padding:0（后面直接跟 ; 或结尾）—— 不能写成 padding:0 8px。
    # 注意别用 `padding\s*:\s*0(?:\s|;)`：`0 8px` 里 0 后面就是个空格，会误放行。
    assert re.search(r"padding\s*:\s*0\s*(?:;|$)", m.group(1)), (
        f".sbar 的 padding 不是 0，窄条会被顶出轨道: {m.group(1).strip()}"
    )
    assert re.search(r"\.sbar>span\{[^}]*text-overflow\s*:\s*ellipsis", html), (
        ".sbar>span 缺少 text-overflow:ellipsis，窄条文字会从字中间硬切"
    )


def test_seekT_guards_missing_plan(html: str):
    """seekT 必须在 LAST_PLAN 为空时早退。

    时间轴/播放按钮在「计划」视图里始终可见，用户可以"先拖再排"；
    旧实现直接读 LAST_PLAN.tasks 会抛 TypeError，播放态下每 120ms 抛一次。
    """
    body = html[html.index("function seekT(v){"):]
    body = body[: body.index("\n}\n") + 3]
    assert "if(!LAST_PLAN)return;" in body, "seekT 缺少 LAST_PLAN 空值保护"


# ---------------------------------------------------------------------------
# 排程页改版（视口内自洽布局）的不变量
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    """去掉所有空白，让 CSS 断言不受换行/缩进影响。"""
    return re.sub(r"\s+", "", text)


def _root_var(html: str, name: str) -> str | None:
    """取 `:root{...}` 里某个自定义属性的值；没有则返回 None。

    只看**第一个** :root 块 —— 媒体查询里也可能再写一条 `:root{--fab-inset:14px}`
    做断点覆盖，那是合法的；"全局唯一"指的是顶层那份。
    先剥注释再压缩空白：注释里出现的变量名不该被当成定义，而声明之间隔着
    换行与缩进，不压掉就匹配不到 `;--fab-size:`。
    """
    body = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
    m = re.search(r":root\{([^}]*)\}", body)
    if not m:
        return None
    v = re.search(rf"(?:^|;){re.escape(name)}:([^;}}]*)", _norm(m.group(1)))
    return v.group(1) if v else None


def _split_shorthand(value: str) -> list[str]:
    """按**顶层**空白切分 CSS 简写值，不切 calc() 内部的空格。

    `10px calc(16px + var(--sa-r)) calc(66px + var(--sa-b))` 直接 `.split()`
    会得到 10 段（`calc(16px` / `+` / `var(--sa-r))` …），按四值简写取下标必然取错。
    """
    parts, depth, cur = [], 0, ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                parts.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


def _media_body(html: str, condition_re: str) -> str:
    """取出 `@media <条件>{...}` 的块体，条件部分用正则给。

    ⚠ 不能写成 `@media ...\\{([^}]*)\\}`：块体里每一条规则都自带一对花括号，
    `[^}]*` 会在第一条规则的 `}` 处停住，拿到的是**半截内容** —— 断言看起来
    匹配上了、其实只覆盖了第一行（"子串断言/半截匹配"是本项目的老坑）。
    这里按花括号配平来切。
    """
    m = re.search(r"@media\s*" + condition_re + r"\s*\{", html)
    if not m:
        return ""
    i, depth = m.end(), 1
    while i < len(html) and depth:
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
        i += 1
    return html[m.end(): i - 1]


def _strip_css_comments(src: str) -> str:
    """去掉 CSS 注释，避免断言被注释里的同名文字兜住。

    ⚠ 这是本项目铁律②（"假阳性命中注释"）的 **CSS 版**。实例：长页档里
    `#v-plan .grid3{flex:none;align-items:start}` 的**上方**有一段解释性注释写着
    "flex:none 让三列各拿自然高度；align-items:start 让列之间不再互相拉平"，
    于是把真正的 `align-items:start` 声明删掉后，`"align-items:start" in body`
    **照样为真** —— 变异自检实测报
    `[BAD] 长页档丢掉 align-items:start  变异后被捕获=False`。
    凡是"从一块 CSS 里找某条声明"的断言，都必须先过这一层。
    """
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def _strip_html_comments(src: str) -> str:
    """去掉 HTML 注释 —— 与 `_strip_css_comments` / `_strip_js_comments` 同一套纪律。

    ⚠ 2026-10-08 实测踩到：`.menu` 上方新加的注释里写了
    "键盘处理里用 `document.querySelectorAll('.menu button')[i]` 取按钮再调 `show()`"，
    于是 `test_settings_site_replaces_the_device_site` 的
    `body.count("show(") == 5` 数成了 6 —— **菜单本身没错**，是护栏把注释算进去了。
    假报警的代价是真回归也会被无视（铁律②），所以这里补上 HTML 版剥离。
    """
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


def test_plan_view_height_comes_from_the_viewport(html: str):
    """排程页高度必须由视口推出，而不是被内容顶高。

    要求是"不超出浏览器界面从而出现滚动条"。做法：#v-plan 取
    (100vh − 顶栏实测高)，三列等高，滚动只发生在模块内部。
    顶栏高必须实测（--chrome 由 fitChrome() 写入），否则换字体/缩放会错位。
    """
    m = re.search(r"#v-plan\.active\{([^}]*)\}", html)
    assert m, "找不到 #v-plan.active 规则"
    assert "height:calc(100vh-var(--chrome))" in _norm(m.group(1)), (
        "#v-plan.active 的高度不再是 (100vh - var(--chrome))，页面会被内容顶出滚动条"
    )
    assert re.search(r"--chrome\s*:", html), ":root 缺少 --chrome 兜底值"
    assert "function fitChrome(" in html, "缺少 fitChrome()：顶栏高没有实测"
    assert "fitChrome();" in html, "fitChrome() 定义了却没调用"


def test_selected_card_sits_after_the_dynamic_event_card(html: str):
    """「已选」卡必须在右列、③ 动态事件·重排之后。

    它原先在左列 ① 选择菜品 下面；「已选」里放着开饭时间/厨房预设/生成排程
    主按钮，位置一改整个右下角就换了主人（还引出了悬浮球遮挡主按钮的问题）。
    """
    after = html.index("<h3>③ 动态事件 · 重排</h3>")
    picked = html.index("<h3>已选</h3>")
    assert picked > after, "「已选」卡没有移到 ③ 动态事件·重排下面"
    assert html.index("<h3>① 选择菜品") < after, "① 选择菜品 的位置被改动了"


def test_column_gap_rule_excludes_the_middle_card(html: str):
    """给三列加 gap 的规则必须排除中列卡片本身。

    中列那张卡片也是 `.grid3` 的 div 子元素 —— 选择器写成 `#v-plan .grid3>div`
    就会连带命中它，卡片内部 8 个子块之间凭空多出 7×14=98px 行距，正好把甘特图
    那 97px 的可分配余量吃光，表现为"flex:1 的甘特区怎么都不长高"。排查了很久。
    """
    assert re.search(r"#v-plan \.grid3>div\{", html) is None, (
        "#v-plan .grid3>div 会连带命中中列卡片，必须写成 :not(.card)"
    )
    m = re.search(r"#v-plan \.grid3>div:not\(\.card\)\{([^}]*)\}", html)
    assert m, "找不到排除中列卡片的列容器规则"
    assert "gap" in _norm(m.group(1)), "列间距不在 :not(.card) 规则里，卡片内部会被塞进行距"


def test_gantt_is_a_horizontal_scroll_container(html: str):
    """时间轴必须能左右滚，且 nowline 与泳道条同处一个定位基准。

    seekT() 用 `nl.parentElement.clientWidth` 换算 nowline 的像素位置，
    泳道条却按百分比定位 —— 两者必须以 .gantt-inner（可滚内容）为同一基准，
    否则滚动/拉宽后指针与条会错位。
    """
    m = re.search(r"#v-plan #gantt\{([^}]*)\}", html)
    assert m, "找不到 #v-plan #gantt 规则"
    assert "overflow:auto" in _norm(m.group(1)), "时间轴容器不再是可滚动容器"
    assert re.search(r"\.gantt-inner\{", html), "缺少 .gantt-inner 定位基准层"
    seg = html[html.index('class="gantt-inner"'):]
    seg = seg[: seg.index("</div>") + 6]
    # 必须匹配 id="nowline" 这个精确标记 —— 只写 "nowline" 的话，
    # 任何带 nowline 字样的类名都会把它蒙混过去（变异测试实测踩到过）。
    assert 'id="nowline"' in seg, (
        "nowline 没渲染在 .gantt-inner 里，px 基准会与泳道轨道不一致"
    )
    assert "min-width:max(100%," in html, "画布没有按计划时长设最小宽度，横向滚动条不会出现"


def test_view_switch_buttons_are_not_full_width(html: str):
    """视图切换按钮必须显式 width:auto。

    全局 `button{width:100%}` 是给 CTA 用的；视图切换按钮在 flex 行里会被撑成
    整行（实测「设备视图」拉成一条 430px 的黑条），三个按钮完全错位。
    """
    m = re.search(r"#v-plan \.vbtn\{([^}]*)\}", html)
    assert m, "找不到 #v-plan .vbtn 规则"
    assert "width:auto" in _norm(m.group(1)), (
        ".vbtn 没写 width:auto，会被全局 button{width:100%} 撑成整行"
    )


def test_view_switch_buttons_are_wide_enough(html: str):
    """视图切换按钮要有足够宽度。

    实测改前 66×26px —— 三个 4 字标签挤得很局促（用户反馈"太小了，拉长点宽度"）。
    `min-width` 同时保证三个按钮等宽：文本长度不同时不会参差。
    """
    m = re.search(r"^[ \t]*#v-plan \.vbtn\{([^}]*)\}", html, re.M)
    assert m, "找不到 #v-plan .vbtn 规则"
    css = _norm(m.group(1))
    mm = re.search(r"min-width:(\d+)px", css)
    assert mm and int(mm.group(1)) >= 80, (
        f"#v-plan .vbtn 的 min-width 不足 80px，按钮会太小：{css}"
    )


def test_view_switch_buttons_are_readable_in_every_state(html: str):
    """视图切换按钮三个状态都必须看得清：未选中 / 选中 / hover。

    两个真 bug，都是"控件在、但看不清"这一类 —— 尺寸断言永远抓不到：

    1. **白字压白底**：全局 `button{color:#fff}`，而 `.vbtn` 若没显式写 `color`，
       就会继承白色；底色是 `rgba(255,255,255,.6)`。实测对比度 **1.02**，
       字完全消失（用户报的"字体和背景融合了看不出来"）。所以 `.vbtn` 必须
       显式声明 `color`，并显式清掉全局 button 的橙色 `box-shadow`。
    2. **hover 抢成主色**：全局 `button:hover` 特异性 (0,1,1) 高于 `.vbtn` (0,1,0)，
       `.vbtn` 不写自己的 `:hover` 的话，鼠标划过未选中的按钮会变橙底，
       界面上同时出现两个"看起来是选中"的按钮。
    """
    m = re.search(r"^[ \t]*\.vbtn\{([^}]*)\}", html, re.M)
    assert m, "找不到基础 .vbtn 规则"
    base = _norm(m.group(1))
    # 必须查"声明级"的 color，不能查子串 `color:` —— `transition:...,color .2s ease`
    # 和 `border-color:` 里都含 `color:`，会把"根本没写 color"的版本蒙混过去
    # （变异测试实测踩到过：去掉 color 声明后断言仍为真）。
    assert re.search(r"(?:^|;)color:", base), (
        ".vbtn 没显式写 color —— 会继承全局 button{color:#fff}，白字压白底完全看不见"
    )
    assert "color:#fff" not in base, ".vbtn 的 color 不能是白色（它自己的底色是浅色）"
    assert "box-shadow:none" in base, ".vbtn 没清掉全局 button 的橙色光晕"
    hv = re.search(r"^[ \t]*\.vbtn:hover\{([^}]*)\}", html, re.M)
    assert hv, "缺少 .vbtn:hover —— 全局 button:hover 会把未选中的按钮抢成主色橙底"
    h = _norm(hv.group(1))
    assert "rgba(255,255,255" in h, ".vbtn:hover 的背景必须是浅色，不能变主色"
    assert "var(--accent)" not in h and "242,101,34" not in h, (
        ".vbtn:hover 用了主色，未选中的按钮划过就与选中态混淆"
    )
    act = re.search(r"^[ \t]*\.vbtn\.active\{([^}]*)\}", html, re.M)
    assert act, "找不到 .vbtn.active 规则"
    assert "color:#fff" in _norm(act.group(1)), ".vbtn.active 必须是白字（黑底）"
    a_hv = re.search(r"^[ \t]*\.vbtn\.active:hover\{([^}]*)\}", html, re.M)
    assert a_hv, "缺少 .vbtn.active:hover —— 选中的按钮 hover 时不能变色"
    assert "background:#111" in _norm(a_hv.group(1)), ".vbtn.active:hover 必须保持黑底"


def test_fab_clearance_is_reserved_in_the_plan_view(html: str):
    """排程页必须给右下角悬浮球留出空间。

    三列都顶到视口底，悬浮球（fixed 右下角）会压住右列「生成排程」主按钮 ——
    把「已选」卡搬到右列之后才暴露出来的遮挡。

    ⚠ 这个值**必须由 --fab-size 推导**，不能写死像素。写死之后一旦改球的大小，
    留白不会跟着走，"球又压住按钮了"就会原地复现 —— 2026-10-04 统一气泡尺寸时
    就是这么发现的：四个断点各写一遍球径（60/48/52/44），留白也各写一遍
    （100/78/84/52），两串数字彼此耦合却谁也不知道谁。
    """
    m = re.search(r"#v-plan\.active\{([^}]*)\}", html)
    pad = re.search(r"padding:([^;}]*)", m.group(1))
    assert pad, "#v-plan.active 没设 padding"
    # 底部值现在是 `calc(var(--fab-size) + var(--fab-inset-plan) + var(--sa-b))`，
    # 所以**不能取最后一个 px 数字**（那会取到安全区那项的 16px），
    # 必须按四值简写取第 3 段（top / right / **bottom** / left）。
    parts = _split_shorthand(pad.group(1))
    assert len(parts) == 4, f"#v-plan.active 的 padding 不是四值简写: {pad.group(1)}"
    bottom = _norm(parts[2])
    assert "var(--fab-size)" in bottom, (
        f"#v-plan.active 的底部内边距没跟着 --fab-size 走，改球径时会重新压住主按钮: {parts[2]}"
    )
    assert "var(--fab-inset-plan)" in bottom, (
        f"底部内边距没算上排程页的贴角距离（球顶边会越出留白区）: {parts[2]}"
    )
    assert "var(--sa-b)" in bottom, (
        f"底部内边距丢了安全区 —— 刘海屏的 Home 横条会压住内容: {parts[2]}"
    )
    assert "#v-plan.active ~ .fab{" in html, "缺少排程页悬浮球的位置覆盖"
    # 排程页的覆盖**只准改位置**：尺寸再出现一次就等于"同一个球两个大小"。
    plan_fab = re.search(r"#v-plan\.active ~ \.fab\{([^}]*)\}", html)
    assert plan_fab, "找不到排程页悬浮球的覆盖规则"
    assert not re.search(r"(?:^|;)(width|height|font-size):", _norm(plan_fab.group(1))), (
        "#v-plan.active ~ .fab 又写死了尺寸 —— 尺寸只允许在 :root 的 --fab-size 定义一次"
    )


def test_fab_size_is_defined_exactly_once(html: str):
    """悬浮 AI 球的尺寸必须全站唯一：只准在 :root 用 --fab-* 变量定义。

    起因（2026-10-04 用户反馈）：同一个 AI 气泡在不同界面大小不一 ——
    基础 .fab 60px、排程页 48px、手机断点 52px、超窄屏/矮横屏 44px。
    球是**同一个控件**，四个尺寸只会让人以为它是四个东西。

    这条护栏锁的不是"60px"这个数值（那是产品选择，可以改），而是
    **"只能有一处定义"**：任何断点里再写一次字面量宽高/字号都算漂移。
    位置（right/bottom）仍然允许逐断点覆盖 —— 那是合理的自适应。
    """
    size = _root_var(html, "--fab-size")
    assert size, ":root 里没有 --fab-size —— 悬浮球尺寸必须有唯一出处"
    m = re.fullmatch(r"(\d+)px", size.strip())
    assert m, f"--fab-size={size!r} 必须是像素值（calc() 会让护栏无法核对下限）"
    assert int(m.group(1)) >= 44, (
        f"--fab-size={size} 小于 44px —— 低于触控目标下限，手指点不准"
    )
    font = _root_var(html, "--fab-font")
    assert font and re.fullmatch(r"\d+px", font.strip()), (
        f"--fab-font={font!r} 必须是像素值，否则球内 emoji 与球径会各走各的"
    )

    base = re.search(r"^[ \t]*\.fab\{([^}]*)\}", html, re.M)
    assert base, "找不到基础 .fab 规则"
    b = _norm(base.group(1))
    assert "width:var(--fab-size)" in b and "height:var(--fab-size)" in b, (
        ".fab 的宽高必须引用 --fab-size，不能写字面量"
    )
    assert "font-size:var(--fab-font)" in b, ".fab 的字号必须引用 --fab-font"

    # 任何一条 .fab 规则里的宽高/字号都必须是变量，不能是字面量 ——
    # 这条同时覆盖"基础规则被改回字面量"和"断点里又插了一条覆盖"。
    rules = re.findall(r"\.fab(?:[:.][\w-]+)?\s*\{([^}]*)\}", html)
    assert rules, "一条 .fab 规则都没找到"
    for decl in rules:
        d = _norm(decl)
        for prop in ("width", "height", "font-size"):
            v = re.search(rf"(?:^|;){prop}:([^;}}]*)", d)
            if v:
                assert v.group(1).startswith("var(--fab-"), (
                    f".fab 的 {prop} 出现了字面量 {v.group(1)!r} —— "
                    "气泡尺寸只允许在 :root 的 --fab-* 定义一次，断点里只准改位置"
                )


def test_顶栏的流内位置必须等于它的_sticky_位置(html: str):
    """顶栏 sticky 之后**渲染位置**变了，但**流内位置**没变 —— 必须靠 margin-top 补上。

    2026-10-08 用户反馈："智能排程界面上方的模块和导航栏穿模了"。
    根因：`.nav{position:sticky;top:calc(14px + var(--sa-t))}` —— sticky 的 top
    只把元素**往下挪 14px 渲染**，文档流里它仍然只占 0..51。于是紧跟其后的
    `#v-plan`（从流内 51 开始）首行模块顶部一截永远压在顶栏底下。
    实测（真 Edge 排版探针，改动前）：1440×900 首行顶=80 / 顶栏下沿=84，
    1920×1080、2560×1440、1084×570 全部为负 —— **每个桌面尺寸都中招**。
    其他站点只是**碰巧**没露出来（`.view` 的 28px 上内边距 > 14px）。

    修法：top 与 margin-top 同源（都用 --nav-top），流内位置 = 渲染位置。
    这条护栏锁的是"同源"，不是某个具体像素值 —— 断点里改 --nav-top 是允许的
    （手机端顶栏离顶更近），但**不准**再单独写 `.nav{top:...}`。
    """
    nav_top = _root_var(html, "--nav-top")
    assert nav_top, (
        ":root 里没有 --nav-top —— 顶栏的 sticky 偏移必须有唯一出处，"
        "否则 top 与 margin-top 迟早各写一个值"
    )
    assert "var(--sa-t)" in nav_top, (
        f"--nav-top={nav_top!r} 没算上安全区，刘海屏上顶栏会被听筒挡住"
    )
    base = re.search(r"^[ \t]*\.nav\{([^}]*)\}", html, re.M)
    assert base, "找不到基础 .nav 规则"
    b = _norm(base.group(1))
    assert "position:sticky" in b, "顶栏不再是 sticky 了？这条护栏的前提变了"
    assert "top:var(--nav-top)" in b, (
        f"顶栏的 top 不是 var(--nav-top)：{base.group(1).strip()}"
    )
    assert "margin-top:var(--nav-top)" in b, (
        "顶栏缺少 margin-top:var(--nav-top) —— sticky 的 top 不占流内空间，"
        "不补 margin-top 的话顶栏会浮在内容上（首行模块被压，就是用户说的「穿模」）"
    )
    # 断点只准覆盖变量本身，不准再写一条 .nav{top:...} / {margin-top:...}
    for m in re.finditer(r"\.nav\{([^}]*)\}", html):
        d = _norm(m.group(1))
        for prop in ("top", "margin-top"):
            v = re.search(rf"(?:^|;){prop}:([^;}}]*)", d)
            if v:
                assert v.group(1) == "var(--nav-top)", (
                    f"断点里的 .nav 又写死了 {prop}:{v.group(1)} —— "
                    "顶栏偏移只允许在 --nav-top 定义一次（断点请覆盖变量本身）"
                )


def test_所有站点共用同一个顶部留白变量(html: str):
    """每个站点的首行内容离顶栏下沿的距离必须由**同一个变量**决定。

    用户反馈的另一半是"UI 与其他站点不一致"：排程页自己写 `padding-top:10px`，
    其他站点写 28px。10px 比顶栏的 sticky 偏移(14px)还小 ⇒ 排程页首行模块被压住，
    其他站点只是侥幸躲开。把"间距"收敛到 --page-top-gap 之后，
    "内容永远在顶栏之下"从"碰巧"变成结构保证，跨站点也不可能再各自漂移。

    ⚠ 这条同时管住**所有断点**：手机/超窄屏/矮横屏各自会把间距调小（16/12/8px），
      但必须通过覆盖 --page-top-gap 实现 —— 只改 .view 不改 #v-plan 就又变成
      "排程页比别的站点少几个像素"，正是用户看到的那种不一致。
    """
    gap = _root_var(html, "--page-top-gap")
    assert gap, ":root 里没有 --page-top-gap —— 页面顶部留白必须有唯一出处"
    assert re.fullmatch(r"\d+px", gap.strip()), (
        f"--page-top-gap={gap!r} 必须是像素值，calc() 会让跨站点一致性无法核对"
    )

    def top_pad(body: str):
        pad = re.search(r"padding:([^;}]*)", body)
        if not pad:
            return None
        parts = _split_shorthand(pad.group(1))
        return _norm(parts[0]) if parts else None

    checked = 0
    for pattern in (r"(?<![\w-])\.view\{([^}]*)\}", r"#v-plan\.active\{([^}]*)\}"):
        for m in re.finditer(pattern, html):
            t = top_pad(m.group(1))
            if t is None:
                continue
            checked += 1
            assert t == "var(--page-top-gap)", (
                f"顶部内边距不是 var(--page-top-gap) 而是 {t!r} —— "
                "各站点首行间距必须同源，否则排程页会比其他站点贴得更近（顶栏穿模）"
            )
    assert checked >= 2, (
        f"只检查到 {checked} 处 padding —— 规则没匹配上，护栏自己失效了"
    )


def test_矮桌面摆不下仪表盘时降级成长页(html: str):
    """宽 >1024 但矮的视口（1280×720 / 1084×570）必须降级成"长页"，不能硬锁视口高。

    仪表盘成立的前提是"右列最小内容(606px) + 顶栏(65) + 底部留白(78) + 顶部间距(14)
    都塞得进视口"，即视口高 ≳ 763px。再矮就必然同时出现三件事（实测）：
      ① 甘特图被压成 0~130px —— 时间轴是本页主输出，等于白给（1084×570 实测 gantt=0）；
      ② 右列溢出到底部留白里 —— 悬浮球压住「生成排程」主按钮（探针 hits 实测命中）；
      ③ 内容把 <body> 顶高 ⇒ 页面出纵向滚动条，sticky 顶栏浮在内容上（"穿模"）。
    这是与平板/手机同一套降级策略：列数不变，只是不再锁高、滚动交给页面。

    ⚠ `align-items:start` 同时是 scripts/ui_layout_probe.mjs 判定"当前处于长页档"的
      **唯一信号**（仪表盘档是 stretch）—— 探针刻意不自己再抄一份断点阈值，
      否则两边迟早不一致。所以这条要**跨文件**一起钉。
    """
    body = _media_body(html, r"\(min-width:1025px\)\s*and\s*\(max-height:760px\)")
    assert body, "缺少矮桌面长页档的媒体查询 (min-width:1025px) and (max-height:760px)"
    # ⚠ 必须先剥掉注释再断言：块体里那段解释性注释同时写着 `flex:none` 与
    #   `align-items:start`，不剥的话删掉真声明断言**照样为真**（变异自检实测 [BAD]）。
    nb = _norm(_strip_css_comments(body))
    assert "height:auto" in nb, "长页档没有解除 #v-plan 的固定高度"
    assert "min-height:calc(100vh-var(--chrome))" in nb, (
        "长页档丢了 min-height —— 内容不足一屏时排程页会缩成一条"
    )
    assert "align-items:start" in nb, (
        "长页档没给 #v-plan .grid3 设 align-items:start —— "
        "它同时是排版探针判定布局档位的唯一信号，缺了探针会拿仪表盘口径去判长页"
    )
    assert "flex:none" in nb, "长页档没让 .grid3 取自然高度（flex:none）"
    gm = re.search(r"#v-plan#gantt\{min-height:(\d+)px\}", nb)
    assert gm, "长页档没给 #gantt 设 min-height —— 时间轴又会被压成 0"
    assert int(gm.group(1)) >= 200, (
        f"长页档的 #gantt min-height 只有 {gm.group(1)}px，时间轴没法看"
    )
    # 跨文件：探针必须按同一个信号选分支，而不是自己再写一份阈值
    probe = Path(__file__).resolve().parents[1] / "scripts" / "ui_layout_probe.mjs"
    src = probe.read_text(encoding="utf-8")
    assert "alignItems" in src and "gridAlign" in src, (
        "排版探针没有读取 .grid3 的 align-items —— 它必须用实测值选布局档位"
    )
    assert 'gridAlign === "start"' in src, (
        "排版探针没按 align-items:start 识别长页档，会和 CSS 的断点阈值各写一份"
    )


def test_timeline_slider_is_themed_not_native(html: str):
    """时间轴滑块必须是主题化皮肤，不能留 Chromium 原生控件样式。

    原生 `<input type=range>` 的默认皮肤是「白底矩形轨道 + 蓝色圆点 +
    border-radius:0 + 无阴影」，和整站的毛玻璃 + 橙色圆角语言完全割裂
    （实测截图里那条白色横杠就是它）。换皮必须同时满足三件事：
      1. `appearance:none` 复位原生皮肤；
      2. 自身背景透明 —— 轨道改由 `::-webkit-slider-runnable-track` 画；
      3. 拇指自绘 `::-webkit-slider-thumb`，用主色描边。
    另外 `.playbar>*` 必须统一行高，否则三个控件高度参差（实测 33/16/20px）。
    """
    m = re.search(r"#tSlider\{([^}]*)\}", html)
    assert m, "找不到 #tSlider 规则"
    css = _norm(m.group(1))
    assert "appearance:none" in css, "#tSlider 没有 appearance:none，会退回 Chromium 原生皮肤"
    assert "background:transparent" in css, "#tSlider 背景不透明，原生白底会盖住自绘轨道"
    assert "height:32px" in css, "#tSlider 没有固定行高，与播放按钮高度会参差"
    assert "::-webkit-slider-runnable-track" in html, "缺少自绘轨道伪元素"
    assert "::-webkit-slider-thumb" in html, "缺少自绘拇指伪元素"
    assert "-moz-range-thumb" in html, "缺少 Firefox 拇指伪元素，跨浏览器会掉回原生样式"
    # 轨道/拇指都要吃到主色，否则换皮换了个寂寞
    for pseudo in ("::-webkit-slider-runnable-track", "::-webkit-slider-thumb"):
        seg = re.search(re.escape(pseudo) + r"\{([^}]*)\}", html)
        assert seg, f"{pseudo} 规则为空"
        assert "var(--accent)" in seg.group(1), f"{pseudo} 没用主色，换皮不彻底"
    # 已播放填充：纯 CSS 渐变 + JS 写入的 --p
    assert "var(--p" in html, "轨道渐变没引用 --p，已播放段不会变橙"
    # 行高统一 + 读数等宽（否则 t=0分 → t=100分 会把整行推来推去）
    bar = re.search(r"\.playbar>\*\{([^}]*)\}", html)
    assert bar and "height:32px" in _norm(bar.group(1)), ".playbar>* 没有统一行高"
    lbl = re.search(r"#tLabel\{([^}]*)\}", html)
    assert lbl and "tabular-nums" in _norm(lbl.group(1)), (
        "#tLabel 没写 font-variant-numeric:tabular-nums，读数变化时行宽会抖动"
    )


def test_playbar_sync_has_a_single_source_of_truth(html: str):
    """滑块值 / 已播放填充 / 读数必须由同一个函数同步。

    三者散在三处写，必然出现「滑块动了填充不动」或「重排后读数停在上一轮的
    t 上」这类不同步（旧实现就是 render() 只改了 max、label 没复位）。
    """
    m = re.search(r"function syncPlaybar\(v\)\{(.*?)\n\}", html, re.S)
    assert m, "找不到 syncPlaybar() —— 播放条同步逻辑又散回各处了"
    body = m.group(1)
    assert "--p" in body, "syncPlaybar() 没写 --p，已播放填充不会跟着走"
    assert "tLabel" in body, "syncPlaybar() 没更新读数标签"
    assert "s.value=v" in _norm(body), "syncPlaybar() 没同步滑块值"
    # seekT（拖动/播放）与 render（重排复位）都必须走它
    for caller, why in (("seekT", "拖动/播放时不同步"), ("render", "重排后读数不复位")):
        seg = re.search(r"function " + caller + r"\([^)]*\)\{(.*?)\n\}", html, re.S)
        assert seg, f"找不到 {caller}()"
        assert "syncPlaybar(" in seg.group(1), f"{caller}() 没调用 syncPlaybar()，{why}"


# ---------------------------------------------------------------------------
# 屏幕尺寸适配（断点 / 安全区）的不变量
# ---------------------------------------------------------------------------
# 这一组锁的是"适配度"：页面要在 320px 手机 → 2560px 超宽屏、以及手机横屏
# 这些真实尺寸上都不出横向滚动条、控件不被压扁、导航不被刘海挡住。
#
# 护栏只锁**结构性不变量**（断点在不在、顺序对不对、安全区有没有接上），
# 像素级结论交给真浏览器探针 scripts/ui_layout_probe.mjs 的「3d. 多视口适配扫描」。
# 两者缺一不可：静态护栏快但不量排版，探针量排版但需要真内核。


def _css_rules(html: str, selector: str) -> list[str]:
    """取出某个选择器的所有规则体（同一选择器可能在多个 @media 里各有一份）。"""
    return re.findall(re.escape(selector) + r"\{([^}]*)\}", html)


def _media_conds(html: str) -> list[str]:
    """按**出现顺序**取出所有 @media 的条件串。"""
    return [m.group(1).strip() for m in re.finditer(r"@media([^{]*)\{", html)]


def test_viewport_opts_into_full_bleed_for_notched_screens(html: str):
    """viewport 必须带 viewport-fit=cover，否则安全区拿不到真实值。

    不开 cover 时浏览器会把页面缩进"安全区"里，`env(safe-area-inset-*)` 恒为 0；
    开了 cover 才铺满整屏 —— 代价是必须自己把刘海和 Home 横条那两条让出来。
    所以这条和下面的安全区断言是一对，缺一不可：只开 cover 不写 env()，
    顶部导航会被听筒挡住、底部按钮会被 Home 横条压住。
    """
    m = re.search(r'<meta name="viewport" content="([^"]*)"', html)
    assert m, "找不到 viewport meta"
    content = m.group(1)
    assert "width=device-width" in content, "viewport 丢了 width=device-width"
    assert "viewport-fit=cover" in content, (
        "viewport 没写 viewport-fit=cover —— 刘海屏上 env(safe-area-inset-*) 恒为 0，"
        "顶部导航会被听筒挡住、底部按钮会被 Home 横条压住"
    )


def test_safe_area_insets_are_declared_with_fallback_and_used(html: str):
    """安全区必须：① 用 env() 声明成变量且带 0px 兜底；② 真的被关键容器用上。

    只声明不用 = 白声明。
    用而不给兜底更糟：桌面浏览器上 `env()` 没有值，`--sa-r` 变成空值，
    `padding:calc(16px + var(--sa-r))` **整条声明作废**，桌面布局当场塌掉。
    """
    root = re.search(r":root\{(.*?)\}", html, re.S)
    assert root, "找不到 :root"
    root_css = _norm(root.group(1))
    for side in ("t", "r", "b", "l"):
        assert f"--sa-{side}:env(safe-area-inset-" in root_css, f"缺少 --sa-{side} 声明"
    assert root_css.count("env(safe-area-inset-") == 4, (
        ":root 里的 safe-area 变量数量不对（应恰好 t/r/b/l 四条）"
    )
    # env() 必须带 0px 兜底 —— 否则桌面浏览器上整条 calc() 作废
    fallbacks = re.findall(r"env\(safe-area-inset-[a-z]+,\s*0px\)", html)
    assert len(fallbacks) == 4, (
        f"safe-area 的 env() 没写 0px 兜底值（找到 {len(fallbacks)} 条，应为 4 条）—— "
        "桌面浏览器上 env() 无值，整条 padding 会作废"
    )
    # 关键容器必须真的用上，否则声明了也是白声明。
    # 只看**基础规则**（rules[0]）—— 媒体查询里也写了 var(--sa-)，只查 any()
    # 的话，把基础规则的 safe-area 全删掉仍然会通过（变异测试会漏掉这条）。
    for selector, why in (
        (".nav", "顶部胶囊导航会被刘海/状态栏挡住"),
        (".view", "正文左右会被圆角裁掉、底部会被 Home 横条压住"),
    ):
        rules = _css_rules(html, selector)
        assert rules, f"找不到 {selector} 规则"
        assert "var(--sa-" in rules[0], f"{selector} 的基础规则没用 safe-area 变量，{why}"
    plan = re.search(r"#v-plan\.active\{([^}]*)\}", html)
    assert plan, "找不到 #v-plan.active 规则"
    assert "var(--sa-" in plan.group(1), (
        "#v-plan.active 没用 safe-area 变量 —— 横屏时左列会被圆角裁掉、底部会被 Home 横条压住"
    )


def test_responsive_breakpoints_cover_the_whole_size_range(html: str):
    """断点必须覆盖：超宽屏 / 桌面 / 平板竖屏 / 手机 / 折叠屏窄屏 / 手机横屏。

    只有 1024 与 640 两条时的实际后果（逐条实测过）：
      · 768×1024（iPad 竖屏）掉进"两栏"，排程页三列塞进两行，每格只剩 ~100px；
      · 1920/2560 两侧空出大片，正文被限制在 1440 里；
      · 320px 小屏（iPhone SE / 折叠外屏）左右内边距吃掉 60px 后横向溢出；
      · 375px 高的手机横屏完全没处理，hero 占满一屏 ⇒ 首屏什么都看不到。
    这些是真实设备，不是边角情况。
    """
    joined = " || ".join(_media_conds(html))
    for pat, why in (
        (r"max-width:\s*1024px", "缺少平板断点"),
        (r"max-width:\s*640px", "缺少手机断点"),
        (r"max-width:\s*360px", "缺少折叠屏外屏/小屏断点（320px 会横向溢出）"),
        (r"min-width:\s*1800px", "缺少超宽屏断点（1920/2560 会空出大片）"),
        (r"orientation:\s*landscape", "缺少手机横屏断点"),
        (r"max-height:\s*500px", "横屏断点没按高度触发，矮窗口不受保护"),
    ):
        assert re.search(pat, joined), why

    # 平板竖屏必须单独把排程页收成单列 —— 只靠 max-width:1024px 的两栏是不够的
    tab = re.search(
        r"@media\s*\(min-width:\s*641px\)\s*and\s*\(max-width:\s*1024px\)\s*\{(.*?)\n  \}",
        html, re.S)
    assert tab, "找不到平板竖屏专用断点"
    tab_css = _norm(tab.group(1))
    # 注意 _norm 会把 `#v-plan .grid3` 里的空格也吃掉，所以期望串也要先规范化
    assert _norm("#v-plan .grid3{grid-template-columns:minmax(0,1fr)") in tab_css, (
        "平板竖屏没把排程页收成单列 —— 三列塞进两栏后每格只剩 ~100px，甘特图没法看"
    )


def test_narrow_breakpoints_are_declared_after_wide_ones(html: str):
    """媒体查询的顺序必须是"越窄/越矮越靠后"。

    一条属性被多个查询同时命中时特异性相同 ⇒ 靠**源序**决胜：
      · 640 写在 360 后面的话，360px 屏幕拿到的还是 640 那套（更宽的内边距）；
      · 横屏规则写在 640 前面，会被 640 的 `#v-plan.active{height:auto}` 盖掉，
        横屏恢复"视口内自洽"的努力全部落空。
    顺序错了不会有任何报错，只是在那几种尺寸上悄悄用错规则 —— 所以必须锁。
    """
    conds = _media_conds(html)

    def idx(pat: str) -> int:
        for i, c in enumerate(conds):
            if re.search(pat, c):
                return i
        raise AssertionError(f"找不到断点: {pat}")

    i1024, i640, i360 = (idx(r"max-width:\s*1024px"),
                         idx(r"max-width:\s*640px"),
                         idx(r"max-width:\s*360px"))
    assert i1024 < i640 < i360, (
        f"断点顺序错了：1024@{i1024} → 640@{i640} → 360@{i360}，窄的必须排在宽的后面"
    )
    iland = idx(r"orientation:\s*landscape")
    assert iland > i640, (
        f"横屏断点(第 {iland} 条)必须排在手机断点(第 {i640} 条)之后，"
        "否则它的 #v-plan.active 覆盖会被 640 的 height:auto 盖掉"
    )


def test_ios_landscape_does_not_inflate_text(html: str):
    """必须显式关掉 iOS 横屏的文字自动放大。

    iOS Safari 横屏时会自作主张放大正文（它认为字太小），整页布局被撑爆、
    出现横向滚动条 —— 而媒体查询本身完全正确，排查时极易误判成"断点写错了"。
    `-webkit-text-size-adjust:100%` 是唯一能关掉它的开关。
    """
    assert re.search(r"-webkit-text-size-adjust\s*:\s*100%", html), (
        "缺少 -webkit-text-size-adjust:100% —— iOS 横屏会自动放大文字并撑出横向滚动条"
    )
    assert re.search(r"(?<!-webkit-)text-size-adjust\s*:\s*100%", html), (
        "缺少标准属性 text-size-adjust:100%（部分浏览器只认标准名）"
    )


def test_touch_targets_cover_phones_and_tablets(html: str):
    """触控目标必须在**手机与平板两个断点**都抬到 ≥44px，且必须包含 .chip。

    两个实测缺口（真浏览器探针在多视口扫描里报出来的）：
      1. `.chip`（菜系筛选）不是 `button`，吃不到全局 `button{min-height:44px}` ——
         手机上实测只有 38px 高（12px 字 × 1.5 行高 + 18px 内边距 + 2px 边框）。
         它又是高频点击项，必须单独抬。
      2. 触控尺寸原先只写在 640 断点里，iPad（768×1024）拿不到 ——
         探针在平板上报出 8 个不足 44px 的控件。**iPad 是触摸屏，不是鼠标设备**，
         只判手机等于把平板用户当鼠标用户对待。
    """
    cases = (
        ("手机", r"@media\s*\(max-width:\s*640px\)\s*\{(.*?)\n  \}"),
        ("平板", r"@media\s*\(min-width:\s*641px\)\s*and\s*\(max-width:\s*1024px\)\s*\{(.*?)\n  \}"),
    )
    for name, pat in cases:
        m = re.search(pat, html, re.S)
        assert m, f"找不到{name}断点"
        css = _norm(m.group(1))
        assert "button{min-height:44px}" in css, f"{name}断点没把按钮抬到 44px"
        assert re.search(r"\.chip\{[^}]*min-height:44px", css), (
            f"{name}断点的 .chip 没有 min-height:44px —— .chip 不是 button，"
            "吃不到全局规则，实测只有 38px 高，低于 44px 触控下限"
        )
        assert re.search(r"\.sbar\{[^}]*height:44px", css), f"{name}断点的甘特条不足 44px"


def test_plan_view_releases_its_viewport_height_on_small_screens(html: str):
    """窄屏上排程页必须从"锁死视口高度"改回"整页滚动"。

    桌面那套 `height:calc(100vh - var(--chrome))` 是为了"页面不出现滚动条"；
    手机上三列挤在一屏里必然什么都看不清，所以 640 断点要显式把 height 改回 auto。
    忘了改的话症状很隐蔽：页面不报错，只是内容被截掉一半、且怎么滑都滑不到。
    """
    m = re.search(r"@media\s*\(max-width:\s*640px\)\s*\{(.*?)\n  \}", html, re.S)
    assert m, "找不到手机断点"
    css = _norm(m.group(1))
    plan = re.search(r"#v-plan\.active\{([^}]*)\}", m.group(1))
    assert plan, "手机断点里没有覆盖 #v-plan.active 的高度"
    body = _norm(plan.group(1))
    assert "height:auto" in body, (
        "手机断点里 #v-plan.active 没改回 height:auto —— 三列锁在一屏里必然看不清"
    )
    assert "grid-template-columns:minmax(0,1fr)" in css, "手机断点没把三列收成单列"


# ---------------------------------------------------------------- 设置页
def test_settings_site_replaces_the_device_site(html: str):
    """「设备」必须已并入「设置」，且站点数仍是 5。

    站点清单是**跨四处**的契约（.menu、ui_probe.mjs 的 views、ui_layout_probe.mjs
    的 VIEWS、以及本文件），少同步一处探针就会静默量到一个不存在的视图 ——
    它会"全绿"，因为量的是空白页。这里锁住最容易漏的那一半：菜单里不能再有
    `show('devices')`，同时必须有 `show('settings')`。
    """
    menu = re.search(r'<div class="menu">(.*?)</div>', html, re.S)
    assert menu, "找不到 .menu"
    # ⚠ 先剥 HTML 注释：菜单上方那段"索引顺序就是快捷键 1–5"的说明里提到了
    #   `show()`，不剥的话 `count("show(")` 会多算一个（实测踩到）。
    body = _strip_html_comments(menu.group(1))
    assert body.count("show(") == 5, f"菜单入口数应为 5，实为 {body.count('show(')}"
    assert "show('settings'" in body, "菜单里没有「设置」入口"
    assert "show('devices'" not in body, "菜单里仍有「设备」入口 —— 它已并入设置页"
    assert 'id="v-settings"' in html, "找不到 #v-settings 视图"
    assert 'id="v-devices"' not in html, "#v-devices 残留 —— 已并入 #v-settings"


def test_settings_subtabs_are_readable_in_every_state(html: str):
    """设置页子页签与排程页视图按钮是同一类控件，踩的是同一个坑。

    全局 `button{color:#fff;width:100%;box-shadow:橙}` 会漏进 `.stab`：
    不显式写 color 就是白字压白底（对比度 1.02）；不写 width:auto 三个页签各占
    一整行；不写自己的 :hover，全局 `button:hover`(0,1,1) 会把未选中的页签
    抢成橙底，看起来像同时选中了两个。
    """
    m = re.search(r"^[ \t]*\.stab\{([^}]*)\}", html, re.M)
    assert m, "找不到基础 .stab 规则"
    base = _norm(m.group(1))
    # 必须查"声明级"的 color —— 子串匹配会被 `border-color:` 蒙混过去
    assert re.search(r"(?:^|;)color:", base), ".stab 没显式写 color，会继承白色"
    assert "color:#fff" not in base, ".stab 未选中态不能是白字（底色是浅色）"
    assert "width:auto" in base, ".stab 没写 width:auto，会被全局 button 撑成整行"
    assert "box-shadow:none" in base, ".stab 没清掉全局 button 的橙色光晕"
    hv = re.search(r"^[ \t]*\.stab:hover\{([^}]*)\}", html, re.M)
    assert hv, "缺少 .stab:hover —— 全局 button:hover 会把未选中的页签抢成主色"
    h = _norm(hv.group(1))
    # 判据是"**底色必须是浅色**"，不是"不含 accent"：变异测试实测过，
    # 把 hover 换成 `rgba(224,90,26,.95)`（同一个橙色的另一种写法）能溜过去。
    assert "rgba(255,255,255" in h or "background:#fff" in h, (
        ".stab:hover 的底色必须是浅色，否则未选中的页签划过就与选中态混淆"
    )
    assert "var(--accent)" not in h, ".stab:hover 用了主色"
    act = re.search(r"^[ \t]*\.stab\.active\{([^}]*)\}", html, re.M)
    assert act, "找不到 .stab.active 规则"
    assert "color:#fff" in _norm(act.group(1)), ".stab.active 必须是白字（黑底）"
    a_hv = re.search(r"^[ \t]*\.stab\.active:hover\{([^}]*)\}", html, re.M)
    assert a_hv, (
        "缺少 .stab.active:hover —— .stab.active 与 .stab:hover 同为 (0,2,0)，"
        "靠源序压不住，选中页签 hover 时会掉色"
    )


def test_settings_panels_switch_by_class_not_inline_style(html: str):
    """子页签面板必须靠 `.spanel.active` 显隐，不能写 inline style。

    用 inline `style.display` 切面板时，"当前在哪个页签"这份状态就同时存在于
    DOM 的 style 属性和 JS 变量里 —— 探针/护栏读不到，用户切页签后刷新更是直接
    回到第一页。类名切换只有一个真源。
    """
    css = _norm(html)
    assert ".spanel{display:none}" in css, "缺少 .spanel 的默认隐藏规则"
    assert ".spanel.active{display:block}" in css, "缺少 .spanel.active 的显示规则"
    fn = re.search(r"function setTab\(name,btn\)\{(.*?)\n\}", html, re.S)
    assert fn, "找不到 setTab()"
    body = fn.group(1)
    assert "classList.remove('active')" in body and "classList.add('active')" in body, (
        "setTab() 没有用 classList 切换 —— 面板状态必须只有一个真源"
    )
    assert "style.display" not in body, "setTab() 里出现了 inline style，状态会有两份"


def test_model_panel_explains_where_the_model_came_from(html: str):
    """模型接入面板必须说明"当前模型是从哪来的"以及"为什么改不了"。

    模型名有三层来源（环境变量 > 界面设置 > 内置默认）。不显示来源的话，
    用户在界面上改完、发现没生效，会以为界面坏了 —— 实际是 `.env` 里的
    FT_LLM_MODEL 把它钉死了。所以两件事都要有：
      · 展示 model_source；· 被锁定时置灰输入框并给出原因。
    """
    fn = re.search(r"async function loadModelSettings\(\)\{(.*?)\n\}", html, re.S)
    assert fn, "找不到 loadModelSettings()"
    body = fn.group(1)
    assert "model_source" in body, "没有展示模型来源（env / settings / default）"
    assert "locked" in body, "没有处理 locked —— 被环境变量钉死时界面不给任何提示"
    assert "readOnly" in body, "锁定时没有把输入框置灰，用户会白填一遍再收到报错"


def test_settings_page_has_no_alert_in_the_save_paths(html: str):
    """设置页的保存结果必须落在那行内联日志里，不能只弹 alert。

    alert 是阻塞对话框：它在探针里会被 stub 掉（`window.__alerts`），在真实
    浏览器里则打断操作流；更关键的是它**不留痕** —— 关掉之后用户无从回看
    "刚才到底存成了没有、报的什么错"。
    """
    for fn_name in ("saveDevices", "saveModel", "saveToken"):
        m = re.search(rf"(?:async )?function {fn_name}\(\)\{{(.*?)\n\}}", html, re.S)
        assert m, f"找不到 {fn_name}()"
        body = m.group(1)
        assert "alert(" not in body, f"{fn_name}() 仍在用 alert，结果不会留在页面上"
        assert "_setLog(" in body or "setLog(" in body, f"{fn_name}() 没有写内联日志"


def test_device_cards_show_capability_alongside_the_number(html: str):
    """设备卡必须同时给出「数量」和「能力」，并且能力是只读的。

    只给一个数字输入框时，用户不知道"蒸箱 1"意味着什么；把官方设备参数
    （合法模式 / 温度区间）并排展示，这个数字才有意义。能力来自
    /api/device_spec（只读），数量才提交给 /api/devices —— 两者不能混。
    """
    fn = re.search(r"async function loadDevices\(\)\{(.*?)\n\}", html, re.S)
    assert fn, "找不到 loadDevices()"
    body = fn.group(1)
    # ⚠ 必须查**调用点**而不是字符串 "/api/device_spec"：函数里有一句注释提到
    # "上下限来自后端（/api/device_spec 的 qty_min/qty_max）"，只查字符串的话
    # 注释会把变异"喂饱" —— 把真正的 apiFetch 调用删掉，护栏照样绿。
    # （2026-10-03 实测：变异「设备卡不再展示能力信息」捕获=False，就是这么漏的。）
    assert "apiFetch('/api/device_spec')" in body, (
        "设备卡没有真的去取能力信息（注释里提到接口不算，必须是调用点）"
    )
    assert "dev-num" in body and "data-k=" in body, "设备卡没有可编辑的数量输入框"
    assert "mode-chip" in body, "设备卡没有渲染支持模式"
    # 能力拉不到时不能把整个面板变成空 —— 数量是调度真正要用的东西
    assert "spec={}" in _norm(body) or "spec = {}" in body, (
        "device_spec 请求失败时没有兜底，会让数量输入框一起消失"
    )


def test_settings_save_paths_check_the_http_status(html: str):
    """设置页的写操作必须**同时**判 HTTP 状态与业务 error 字段。

    后端有两套失败契约，缺一条就会把失败显示成成功：
      · 业务校验失败 → **200** + `{"error": ...}`（`/api/devices` 的历史契约）
      · 请求没被路由接受 → **422/500** + `{"detail": ...}`（FastAPI 参数校验）

    实测：给 `/api/devices` 发一个数组体，拿到 `422 {"detail":[...]}`，
    而旧代码只判 `d.error`（undefined）⇒ 一路走到"设备配置已保存，下次排程生效"。
    """
    for fn_name in ("saveDevices", "saveModel"):
        m = re.search(rf"(?:async )?function {fn_name}\(\)\{{(.*?)\n\}}", html, re.S)
        assert m, f"找不到 {fn_name}()"
        body = m.group(1)
        assert re.search(r"\br\.ok\b", body), (
            f"{fn_name}() 没有判 r.ok —— 422/500 会被显示成保存成功"
        )


def test_http_error_detail_is_rendered_as_text_not_an_object(html: str):
    """错误文案必须经 `_httpMsg()` 归一，不能直接拼 `detail`。

    FastAPI 的 `detail` 有两种形状：字符串（HTTPException）与**对象数组**（422 校验）。
    直接 `'失败：'+(d.detail||...)` 遇到数组会拼成 `[object Object]` —— 用户看到
    等于没看到，还会把"参数不对"误判成"后端坏了"。
    """
    m = re.search(r"function _httpMsg\([^)]*\)\{(.*?)\n\}", html, re.S)
    assert m, "缺少 _httpMsg() —— 没有地方把两种 detail 形状归一"
    body = m.group(1)
    assert "Array.isArray" in body, "_httpMsg() 没有处理数组形状的 detail"
    assert re.search(r"typeof\s+det\s*===?\s*['\"]string['\"]", body), (
        "_httpMsg() 没有处理字符串形状的 detail"
    )
    for fn_name in ("saveDevices", "saveModel"):
        mm = re.search(rf"(?:async )?function {fn_name}\(\)\{{(.*?)\n\}}", html, re.S)
        assert "_httpMsg(" in mm.group(1), (
            f"{fn_name}() 没有走 _httpMsg() 渲染错误，422 会显示 [object Object]"
        )



def test_device_qty_input_refuses_blank_instead_of_silently_disabling(html: str):
    """数量框留空必须拦住，不能当成 0 静默提交。

    JS 里 `+'' === 0`，而 **0 在设备配置里是合法值**（表示该设备不可用）。
    不拦的话，"手滑清空一格再点保存"会静默把整台设备下线：相关步骤全部改走
    顶替/人工，排程结果大变，而界面只回一句"已保存"。
    浏览器还会把 `1e`、`-` 这类半截输入也归成空串，所以这条拦得住真实手滑。
    """
    m = re.search(r"async function saveDevices\(\)\{(.*?)\n\}", html, re.S)
    assert m, "找不到 saveDevices()"
    body = m.group(1)
    assert re.search(r"value\.trim\(\)\s*===?\s*''", body), (
        "saveDevices() 没有检查数量框是否为空 —— 空框会被 `+''` 变成 0（= 设备下线）"
    )
    assert re.search(r"if\s*\(\s*blank\s*\)", body), (
        "saveDevices() 检测到空值后没有中止保存"
    )


def test_device_qty_limits_come_from_the_backend(html: str):
    """数量上下限必须由 `/api/device_spec` 下发，不能在前端写死。

    后端 `_DEVICE_LIMITS` 是 灶眼数(0,8) / 蒸箱(0,4) / 烤箱(0,4) / 冰箱(0,4)，
    而旧前端给**所有**设备都写 `max="8"` —— 蒸箱填 8 会被后端判非法。
    同一件事有两份真源，改一处必然漏另一处。
    """
    # 只查输入框那一段 —— 直接对全文查 `max="8"` 会被**注释里**举例的
    # 「写死 max="8"」命中，变成一条自己撞自己的假阳性护栏。
    tag = re.search(r"<input class=\"dev-num\"[^>]*>", html)
    assert tag, "找不到设备数量输入框"
    assert not re.search(r'max="\d+"', tag.group(0)), (
        "数量框的 max 被写死成数字了 —— 后端蒸箱/烤箱/冰箱上限是 4，"
        "应改绑 /api/device_spec 下发的 qty_max"
    )
    m = re.search(r"async function loadDevices\(\)\{(.*?)\n\}", html, re.S)
    assert m, "找不到 loadDevices()"
    body = m.group(1)
    assert "qty_max" in body and "qty_min" in body, "设备卡没有用后端给的 qty_min/qty_max"
    assert re.search(r'max="\$\{qmax\}"', body), "max 属性没有绑定后端下发的上限"


# ---- 接入方式切换（本地 Ollama / 远程 OpenAI 兼容端点）----
def _fn_body(html: str, name: str, *, async_: bool = True) -> str:
    """抽出某个顶层函数的函数体（到第一个顶格 `}` 为止）。

    ⚠ 两个形态都要允许**带参数**：`async function f(a,b){` 与 `function f(a,b){`。
      旧版 async 分支写死了 `\\(\\)`，于是 `sendChat(boxId,inputId)` 这类
      带参异步函数直接"找不到"—— 报错信息指向测试，而不是"这个函数真的没了"。
    """
    pat = (rf"async function {name}\([^)]*\)\{{(.*?)\n\}}" if async_
           else rf"function {name}\([^)]*\)\{{(.*?)\n\}}")
    m = re.search(pat, html, re.S)
    assert m, f"找不到 {name}()"
    return m.group(1)


def _strip_js_comments(src: str) -> str:
    """去掉 JS 注释，只留可执行代码。

    ⚠ 本文件有若干 `assert "d.error" in body` 这类**子串断言**，而源码注释里
      恰恰在解释"只判 d.error 会让 4xx 掉进成功分支" —— 注释命中会让断言
      **永远为真**（变异注入后照样绿）。护栏假绿比漏报更危险：真回归会被无视。
    （`tests/test_product_fixes.py` 有一份同名实现，两边各自独立、互不依赖。）
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def test_provider_options_come_from_the_backend(html: str):
    """「接入方式」下拉的选项必须由后端下发，前端不许再写一份。

    两处各写一份选项列表的下场很具体：以后加第三种接入方式（自建 vLLM、
    局域网 Ollama……）时只改了后端，用户的下拉里根本没有它 —— 而且**没有任何
    报错**，只是"这个功能好像没做"。
    """
    tag = re.search(r'<select[^>]*id="setProviderSel"[^>]*>(.*?)</select>', html, re.S)
    assert tag, "找不到 #setProviderSel 下拉"
    assert not tag.group(1).strip(), (
        "下拉里写死了选项 —— 选项必须来自 /api/model_settings 的 providers 字段"
    )
    body = _fn_body(html, "loadModelSettings")
    assert "d.providers" in body, "loadModelSettings() 没有用后端下发的 providers 渲染下拉"
    assert re.search(r"psel\.disabled\s*=", body), (
        "被 FT_LLM_PROVIDER 钉死时没有把下拉置灰 —— 用户会白改一遍再收到报错"
    )


def test_remote_api_key_is_never_rendered_back(html: str):
    """远程 API Key 只能"往上送"，**绝不能回填到页面上**。

    这个页面会被发布成公开链接（本机演示也是无鉴权的）。把密钥写进 DOM 等于
    把凭据挂在公网上 —— 任何一次"顺手回填一下方便用户"的改动都是事故。
    三重保险：① 输入框是 password；② HTML 里没有 value；③ 加载路径把它清空，
    且**从不读**响应里的 api_key 字段。
    """
    tag = re.search(r'<input[^>]*id="setRemoteKey"[^>]*>', html)
    assert tag, "找不到 #setRemoteKey 输入框"
    assert 'type="password"' in tag.group(0), "密钥框必须是 password，否则会被肩窥/截图泄露"
    assert not re.search(r'\svalue="', tag.group(0)), "密钥框在 HTML 里带了初始值"

    body = _fn_body(html, "loadModelSettings")
    assert re.search(r"setRemoteKey[\s\S]{0,160}?\.value\s*=\s*['\"]['\"]", body), (
        "加载设置时没有清空密钥框 —— 上一次的输入会留在页面上"
    )
    assert not re.search(r"\b(?:d|rv)\.api_key\b(?!_)", body), (
        "加载路径里读了响应体的 api_key 字段 —— 后端本来就不回传它，"
        "这里出现说明有人在往回填密钥"
    )


def test_api_key_mask_is_shown_but_never_prefilled(html: str):
    """密钥要显示**打码预览**（前 6 + 后 4），但**不许回填进输入框**。

    这两件事看起来矛盾，其实必须同时成立：
      · 只显示"已配置"是不够的 —— 用户没法确认配的是**哪一把**钥匙（换过 key 之后
        尤其明显），于是会反复重填，把本来好着的配置改坏；
      · 但把打码值填进 `value` 是另一个坑：用户随手点一次「保存」就会把它当密钥
        提交上去（后端会 400 挡住，但那是补救，不是设计）。

    所以：标签行里显示，输入框留空。
    """
    assert 'id="setRemoteKeyNote"' in html, (
        "密钥标签行里没有打码预览的位置 —— 用户只能看到「已配置」，无法确认是哪一把"
    )
    body = _fn_body(html, "loadModelSettings")
    assert re.search(r"rv\.api_key_masked", body), (
        "loadModelSettings() 没有把后端下发的 api_key_masked 显示出来"
    )
    assert re.search(r"setRemoteKeyNote", body), "取到了打码值却没往 #setRemoteKeyNote 里写"
    # 输入框必须保持空：既没被 rv.* 赋值，也仍然显式清空
    assert not re.search(r"(?:rk|setRemoteKey)[^\n]*\.value\s*=\s*(?:rv|d)\.", body), (
        "把后端下发的值回填进密钥框了 —— 用户保存一次就会把打码值当真密钥提交"
    )
    assert re.search(r"(?:rk|setRemoteKey)[\s\S]{0,160}?\.value\s*=\s*['\"]['\"]", body), (
        "加载设置时没有清空密钥框"
    )
    # 状态表与徽标 tooltip 也要带上打码预览 —— 那是用户最常看的两处
    assert re.search(r"api_key_masked", _fn_body(html, "renderModelState", async_=False)), (
        "状态表里「密钥：已配置」没有带打码预览"
    )
    assert re.search(r"api_key_masked", _fn_body(html, "refreshLlmStatus")), (
        "顶部徽标的 tooltip 没有带打码预览"
    )


def test_masked_key_is_never_submitted(html: str):
    """打码预览值**不许**被当成密钥提交 —— 前端拦一次，后端再拦一次。

    这是整套改动里最容易踩的一脚：标签行里就显示着 `sk-pro1…ESb0`，用户很可能
    顺手复制进输入框。真写进去之后所有调用 401/403，而 llm 的失败是**静默降级**，
    现场只剩"AI 突然不工作了"，根本联想不到是保存那一下干的。

    判据必须与后端 `llm.looks_masked()` **一致**（省略号 / 三个以上星号）：
    两处口径不一致会出现"前端放行、后端 400"（用户白填一遍）或反过来的错位。
    """
    fn = _fn_body(html, "looksMaskedKey", async_=False)
    assert "…" in fn, "前端判据里没有省略号 —— 后端打码用的就是它"
    assert "***" in fn, "前端判据里没有星号 —— 手写的 `sk-****abcd` 这类值会漏过去"

    body = _fn_body(html, "saveModel")
    m = re.search(r"if\s*\([^;{]*looksMaskedKey\s*\([^)]*\)\s*\)\s*\{", body)
    assert m, "saveModel() 没有在提交前检查「这是不是打码预览」"
    guard_at = m.start()
    assign = re.search(r"payload\.api_key\s*=", body)
    assert assign, "saveModel() 里找不到 payload.api_key 的赋值"
    assert guard_at < assign.start(), (
        "打码检查落在了 payload.api_key 赋值之后 —— 拦不住，值已经进请求体了"
    )
    assert re.search(r"\breturn\b", body[guard_at:assign.start()]), (
        "发现是打码值却没有 return —— 会继续往下走把请求发出去"
    )


def test_factory_defaults_are_surfaced_to_the_user(html: str):
    """随程序附带的出厂预置必须**说出来**，否则接收方不知道配置是谁配的。

    接收方的用户层是空的，真正在跑的是随包的默认配置。界面若只说"远程地址：空"，
    他会以为配置丢了、再去乱填一遍；而正确的心智模型是"这是程序自带的，我改了就以
    我的为准"。所以两处都要认这个字段：
      · 加载路径：用户层为空 + 出厂层存在 ⇒ 明确提示"用的是出厂预置"；
      · 模型来源文案：`factory` 要有一句人话（否则会直接把英文枚举显示出来）。
    """
    body = _fn_body(html, "loadModelSettings")
    assert re.search(r"d\.factory_defaults", body), (
        "loadModelSettings() 没有读 factory_defaults —— 界面说不出「现在用的是出厂预置」"
    )
    assert re.search(r"factory_defaults[\s\S]{0,120}?present", body), (
        "读了 factory_defaults 却没有看 present（文件被删掉时也会照样提示）"
    )
    # 模型来源文案：model_source='factory' 必须映射成人话
    for fn, async_ in (("loadModelSettings", True), ("renderModelState", False)):
        src = _fn_body(html, fn, async_=async_)
        m = re.search(r"srcTxt\s*=\s*\{(.*?)\}", src, re.S)
        assert m, f"{fn}() 里找不到 srcTxt 映射"
        assert re.search(r"factory\s*:", m.group(1)), (
            f"{fn}() 的 srcTxt 没有 factory 这一档 —— 界面会把英文枚举 `factory` 直接显示给用户"
        )


def test_blank_remote_fields_are_not_sent_so_saving_does_not_wipe_them(html: str):
    """空白的远程地址 / 密钥**不许进请求体**。

    密钥框永远是空的（我们从不回填）。若把空串也送上去，后端只能把它理解成
    "清空" —— 用户每点一次保存就抹掉一次自己的密钥，而且是静默的：
    界面上写着"已保存"，下一次对话才发现没配密钥。
    """
    body = _fn_body(html, "saveModel")
    assert re.search(r"payload\.provider\s*=", body), (
        "保存时没带上 provider —— 后端会走「只换模型」的旧分支，接入方式永远切不过去"
    )
    assert re.search(r"if\s*\([^)]*\)\s*payload\.base_url\s*=", body), (
        "base_url 没有做非空判断，空串会被当成「清空地址」"
    )
    assert re.search(r"if\s*\([^)]*\)\s*payload\.api_key\s*=", body), (
        "api_key 没有做非空判断，空串会被当成「清空密钥」"
    )
    assert re.search(r"provider\s*!==\s*'ollama'", body), (
        "本地档也在送远程字段 —— 切回本地时会把远程配置一起改写"
    )


def test_provider_switch_hides_irrelevant_fields_by_class(html: str):
    """按档位显隐字段必须靠**类**，不能靠 inline style。

    与子页签同一个理由：inline `style.display` 会把"现在是哪一档"变成 DOM 属性
    和 JS 变量两份状态，探针/护栏都读不到。另外这两条规则必须是 `#sp-model` 打头的
    (1,2,0) 选择器 —— 只写 `.set-field.remote-only` 是 (0,2,0)，与 `.set-field`
    同权重，靠源序压住，以后谁把 `.set-field` 挪到后面就静默失效。
    """
    css = _norm(html)
    assert "#sp-model.mode-local.remote-only{display:none}" in css, (
        "缺少「本地档隐藏远程字段」的规则（且必须用 id 打头保证特异性）"
    )
    assert "#sp-model.mode-remote.local-only{display:none}" in css, (
        "缺少「远程档隐藏本机模型下拉」的规则"
    )
    body = _fn_body(html, "onProviderPick", async_=False)
    assert "className=" in body, "onProviderPick() 没有切换 mode-* 类"
    assert "style.display" not in body, "onProviderPick() 用了 inline style，状态会有两份"


# ===========================================================================
# v3.9 执行态回写 + 经验回流（前端侧）
#
# 这些护栏盯的不是"功能有没有"，而是**三条容易在后续迭代里被悄悄破坏的取舍**：
#   ① 打点放在步骤详情弹层，不新增明细表列（加列会动 table-layout:fixed 的几何）；
#   ② 达到阈值只建议，绝不自动重排；
#   ③ 报延迟不填实际用时必须拒绝，不许替用户编数。
# ===========================================================================

def test_执行打点不新增明细表列(html: str):
    """明细表必须还是 6 列 —— 打点走步骤详情弹层。

    `#v-plan #tasks table{table-layout:fixed}` 下加一列就要重新分配列宽，
    而 14 视口排版探针盯的正是这套几何。为了两个按钮去动它不划算。
    判据取**整条表头字符串**：只要有人加了 `<th>`，这一条立刻红。
    """
    assert ("<th>开始</th><th>结束</th><th>资源</th><th>菜/步</th>"
            "<th>操作步骤</th><th>值守</th>") in html, (
        "任务明细的表头变了 —— 执行打点应该放在步骤详情弹层里，不要加列")
    assert "nth-child(7)" not in _norm(html), "出现了第 7 列的宽度规则（说明加了列）"
    # 打点确实挂进了步骤详情
    detail = _fn_body(html, "openStepDetail", async_=False)
    assert "execBar(" in detail, "openStepDetail() 里没有执行打点区"


def test_执行态标记_排在_hover_之后(html: str):
    """`.exec-done` / `.exec-late` 必须排在 `tbody tr:hover` **之后**。

    两者特异性完全相同（2 id + 1 类/伪类 + 2 type），后定义的赢。放前面会被
    hover 盖掉 —— 鼠标一划过，"已延迟"的红色提示就没了。

    ⚠⚠ 锚点必须写成 `_norm()` 之后的样子：**不能带空格**。
    `_norm()` 会把所有空白压掉，`#v-plan #tasks tbody tr:hover` 会变成
    `#v-plan#taskstbodytr:hover`，此时再 `find("tbody tr:hover")` 恒为 -1 ——
    护栏退化成一条"永远找不到锚点"的假红，真正的顺序回归反而没人看（铁律①）。
    """
    css = _norm(html)
    hover = css.find("tbodytr:hover")
    assert hover > 0, "找不到 tbody tr:hover 规则（锚点失效——是不是又写了带空格的锚点？）"
    assert css.find("tbodytr.exec-done") > hover, ".exec-done 排在 hover 之前，会被盖掉"
    assert css.find("tbodytr.exec-late") > hover, ".exec-late 排在 hover 之前，会被盖掉"


def test_报延迟不填实际用时必须拒绝(html: str):
    """报延迟却不填「实际分钟」⇒ 明确拒绝。

    编一个数喂进校准样本比没有样本更糟 —— 后端 `exec_state.report` 是同一口径
    （不填按"准时"记，绝不记 0）。这里必须在前端就拦住，别让用户以为记上了。
    """
    body = _fn_body(html, "reportExec")
    m = re.search(r"state===['\"]delayed['\"][\s\S]{0,200}?return", body)
    assert m, "reportExec() 没有在「报延迟但没填实际用时」时 return"
    assert "actual>0" in body[m.start():m.end()], "判据不是「实际用时不大于 0」"


def test_达到阈值只建议不自动重排(html: str):
    """偏差到阈值只弹确认层 —— 自动换掉一份正在执行的方案，比不重排更破坏信任。

    ⚠ 判据必须同时看两处：reportExec 里出现 `askReplan`，且**没有**直接调
    `deviationReplan`。只判前者的话，有人顺手在下面加一行自动重排也照样绿。
    """
    body = _fn_body(html, "reportExec")
    assert "askReplan(" in body, "reportExec() 达到阈值后没有走确认层"
    assert "deviationReplan(" not in body, (
        "reportExec() 直接调了 deviationReplan —— 变成自动重排了")


def test_重排请求带上已完成步骤(html: str):
    """`/reschedule` 的 completed 必须来自后端返回的列表。

    前端不自己推断"哪些算做完" —— 冻结集算错会把已经下锅的步骤挪走。
    """
    body = _fn_body(html, "deviationReplan")
    assert re.search(r"completed\s*:", body), "重排请求里没有 completed 字段"
    assert re.search(r"completed\s*:\s*\(dv", body), (
        "completed 不是取自后端返回的 dv.completed（前端自己推断会算错冻结集）")


def test_拉执行态不阻塞渲染(html: str):
    """`loadExec()` 在 render() 里**不能 await** —— 画甘特图不该等一次网络往返。"""
    body = _fn_body(html, "render", async_=False)
    assert "loadExec(" in body, "render() 里没有刷新执行态"
    assert not re.search(r"await\s+loadExec\(", body), (
        "render() 里 await 了 loadExec —— 弱网下整张甘特图要等一次请求")


def test_执行经验入口可见(html: str):
    """经验回流必须**看得见** —— 否则用户不知道自己报的数被拿去做什么了。

    关档时界面要如实显示"未启用"，不能让人以为已经学会了。
    """
    assert re.search(r'id="calibBtn"[^>]*onclick="openCalib\(\)"', html) or \
        re.search(r'onclick="openCalib\(\)"[^>]*id="calibBtn"', html), (
        "计划工具栏里没有「执行经验」入口")
    body = _fn_body(html, "openCalib")
    assert "/api/calib" in body, "openCalib() 没有读 /api/calib"
    assert "未启用" in body, "关档时没有如实说明「未启用」"


def test_打点按钮都有真实动作(html: str):
    """四个打点控件必须都绑到 reportExec —— 只做一个（比如只有「完成」）
    会让"报延迟"这条学习路径永远攒不到样本。"""
    for st in ("done", "delayed", "skip"):
        assert f"reportExec(${{i}},'{st}')" in html, f"缺少 state={st} 的打点按钮"
    # 两个远程字段的元素必须**留在 DOM 里**（只是 display:none）：
    # 探针按 id 取值，直接从 DOM 摘掉会让"取不到"和"值为空"变成同一个现象。
    for eid in ("setRemoteUrl", "setRemoteKey"):
        assert f'id="{eid}"' in html, f"{eid} 被从 DOM 里删掉了"


def test_save_result_wording_follows_the_provider(html: str):
    """保存成功的文案必须按档位分开。

    `verified` 只对**本地**有意义（远程的模型名在中转站那边，本机 Ollama 当然没有）。
    不分档的话，一次正常的远程切换会显示"⚠ 本机没有这个模型，调用会返回 404" ——
    把成功吓成失败，用户会去改一个本来正确的配置。

    ⚠ 断言必须落在**分支条件**上，不能只查 `"d.is_local" in body`：
    变异测试实测过 —— saveModel() 里 `d.is_local` 还出现在最后那行日志文案里
    （`已保存：本地/远程`），所以把 `if(!d.is_local){` 换成 `if(false){` 时，
    只查子串的护栏照样绿，而分档已经没了。**子串命中 ≠ 分支存在。**
    """
    body = _fn_body(html, "saveModel")
    assert re.search(r"if\s*\(\s*!?\s*d\.is_local\s*\)", body), (
        "saveModel() 没有按档位分支 —— 远程档会把「本机没有这个模型，调用会返回 404」"
        "当成保存结果，把一次正常的远程切换吓成失败"
    )
    assert re.search(r"verified", body), "本地档仍要报「本机是否真的有这个模型」"

    # ⚠ 「按档位分流」这段代码 2026-10-04 从 testModel() **搬进了 llmFailReason()**
    #   （失败原因要先按 disabled / breaker / error 分流，档位分流只是最后一层）。
    #   锚点必须跟着搬：留在 testModel 里会变成"锚点消失"，而锚点消失 = 这条护栏
    #   再也不会红（变异脚本会直接报「环境异常」）。
    fr = _fn_body(html, "llmFailReason", async_=False)
    assert re.search(r"is_local\s*\?", fr), (
        "失败提示没有按档位分流 —— 远程档下会让用户去「确认 Ollama 已启动」"
    )
    tm = _fn_body(html, "testModel")
    assert "llmFailReason(" in tm, "testModel() 必须把失败原因交给 llmFailReason() 分流"


# ============================================================ 2026-10-08 五项改动

def test_耗时按秒显示且只有一个换算口径(html: str):
    """耗时统一以**秒**呈现。

    后端各处回的 `elapsed_ms` 一律是毫秒（`round(x*1000,1)`，见 main.py / model.py）。
    原实现把毫秒数直接摆进卡片（实测 "19005.2 耗时(ms)"）—— 五位数既占宽、
    也读不出量级。这条同时钉住"唯一口径"：换算只许有一处（`fmtSec`），
    别处再自己 `/1000` 的话，以后改口径必然漏掉一两个地方。
    """
    fn = _fn_body(html, "fmtSec", async_=False)
    assert re.search(r"/\s*1000", fn), "fmtSec() 没有把毫秒换成秒"
    assert "Number.isFinite" in fn, "fmtSec() 没有对 undefined/NaN 兜底（会显示 NaN）"
    r = _fn_body(html, "render", async_=False)
    assert "fmtSec(d.elapsed_ms)" in r, "统计卡片没有走 fmtSec()"
    assert "耗时(秒)" in r, "统计卡片的单位标签没改"
    rs = _fn_body(html, "reschedSummary", async_=False)
    assert "fmtSec(d.elapsed_ms)" in rs, "重排摘要没有走 fmtSec()"
    assert "耗时(ms)" not in html, "页面里仍残留「耗时(ms)」字样"


def test_插单可以指定加哪一道菜(html: str):
    """插单必须能**指定菜名**，不能只有随机。

    原实现是 `pool[Math.floor(Math.random()*pool.length)]` —— 用户既没法选，
    也复现不了上一次的结果（同一句"插单"两次加的菜不同），演示时讲不清发生了什么。
    """
    assert 'id="insertPick"' in html, "缺少插单菜品下拉 #insertPick"
    fn = _fn_body(html, "triggerEvent")
    assert "insertPick" in fn, "triggerEvent() 没有读 #insertPick —— 插单又退回纯随机"
    assert re.search(r"if\s*\(\s*!pick\s*\)", fn), (
        "没有把「用户指定」与「随机兜底」分开：应当只有留空才随机"
    )
    fp = _fn_body(html, "fillInsertPool", async_=False)
    assert re.search(r"selected\.some\(", fp), "候选池没有排除已选菜品（会把同一道插第二遍）"
    oc = _fn_body(html, "onEventChange", async_=False)
    assert "insertPick" in oc, "onEventChange() 没有同步 #insertPick 的显隐"
    # 候选池必须跟着「已选」走 —— 挂在 updSel() 里才不会漏掉新出现的改选入口
    us = _fn_body(html, "updSel", async_=False)
    assert "fillInsertPool()" in us, "updSel() 没有刷新候选池（已选变了、下拉还是旧的）"


def test_重排期间按钮置忙且必然恢复(html: str):
    """重排要跑完整闭环（实测 5~20s），期间必须置忙，且恢复必须放 `finally`。

    不置忙：用户以为没点上、反复点 ⇒ 同一份输入把后端打爆，后回来的响应还会
    覆盖先回来的结果。
    恢复漏在 `return` 之后：几条失败分支（无菜可插 / 非 2xx / `d.error`）都会跳过
    函数尾部，按钮永远停在"重排中…"且 disabled —— 界面看起来就是卡死。
    """
    fn = _fn_body(html, "triggerEvent")
    assert re.search(r"btn\.disabled\s*=\s*true", fn), "重排期间没有置忙"
    assert re.search(r"finally\s*\{", fn), "没有 finally —— 失败分支会跳过按钮恢复"
    tail = fn[fn.index("finally"):]
    assert re.search(r"btn\.disabled\s*=\s*false", tail), "finally 里没有恢复按钮"
    assert re.search(r"btn\.innerHTML\s*=", tail), "finally 里没有恢复按钮文案"


def test_重排不再吞掉后端错误(html: str):
    """重排失败必须分两种路径判定，且都要说出来。

    业务失败是 200 + `{"error":...}`（`/reschedule` 的历史契约），路由/校验失败是
    4xx/5xx 且响应体形状不定。只判 `d.error` 的话 4xx 会掉进"成功"分支 ——
    界面上照旧渲染一个空计划，看起来像"重排成功但什么都没变"。
    """
    fn = _strip_js_comments(_fn_body(html, "triggerEvent"))
    assert re.search(r"if\s*\(\s*!r\.ok\s*\)", fn), "没有判 HTTP 状态"
    assert re.search(r"d\.error", fn), "没有判业务错误体 {error}"
    # 原实现直接取 d.kpi.conflict_count —— kpi 缺失时抛 TypeError，被 catch 吞成
    # "无法连接后端服务"，把"后端回了个畸形体"误报成"网络不通"。
    assert not re.search(r"d\.kpi\.conflict_count", fn), (
        "仍在裸取 d.kpi.conflict_count —— kpi 缺失会抛 TypeError 并被误报成网络错误"
    )


def test_插单成功后已选集合跟着变(html: str):
    """插单成功 ⇒ 左侧「已选」和顶部「N道」必须一起变。

    原实现只重画甘特图，`selected` 还是旧集合 —— 同一屏上甘特图有 6 条、
    「已选」写着 5 道，两个数字互相打架；再点「生成排程」还会把刚插的菜丢掉。
    """
    fn = _fn_body(html, "triggerEvent")
    assert re.search(r"selected\s*=\s*selected\.concat", fn), "插单后没有把新菜并进 selected"
    assert re.search(r"updSel\(\)", fn), "插单后没有刷新「已选」"


def test_选择厨房_失败要报错并回退(html: str):
    """`applyKitchen` 失败必须**说出来**，并把下拉退回上一个生效值。

    原实现是 `.catch(()=>{})` + 不看 HTTP 状态：请求失败时下拉停在刚选的那一项、
    设备却一点没变 —— 界面看起来"切好了"，实际排程还按老设备算。
    """
    fn = _strip_js_comments(_fn_body(html, "applyKitchen", async_=False))
    # 见 `test_选择厨房有恢复默认按钮且走后端重置` 里的注释：必须判**同一条条件**，
    # 且取条件要用非贪婪 `[^\n]*?`（条件里带嵌套括号）。
    conds = re.findall(r"if\s*\(([^\n]*?)\)\s*\{", fn)
    assert any("!r.ok" in c and "d.error" in c for c in conds), (
        "没有一条 if 条件**同时**判了 HTTP 状态与业务错误体"
    )
    assert fn.count("sel.value=KITCHEN_APPLIED") >= 2, (
        "两条失败路径（请求抛错 / 非 2xx 或 error 体）都要把下拉退回上一个生效值，"
        "否则下拉会说谎：显示的是新预设，跑的还是旧设备"
    )
    assert "kitchenNote" in fn, "失败没有写到 #kitchenNote —— 用户看不到任何反馈"


def test_选择厨房_按当前设备反查生效预设(html: str):
    """刷新后下拉要停在**当前实际生效**的预设上。

    不反查的话，刷新一次就退回"选择厨房预设…"，而设备其实早被改过 ——
    用户看到的是一个和自己厨房不符的界面，只能靠记忆猜现在是哪一套。
    """
    fn = _fn_body(html, "loadPresets")
    assert "/api/devices" in fn, "loadPresets() 没有读当前设备数，无法反查"
    assert re.search(r"KITCHEN_APPLIED\s*=", fn), "没有记录当前生效的预设"
    assert re.search(r"sel\.value\s*=\s*hit", fn), "没有把反查结果写回下拉"
    # 反查必须是**逐项全等**，不能只看灶眼数
    assert re.search(r"DEVICE_KEYS\.every", fn), (
        "反查没有逐项比对全部设备键 —— 只看灶眼数会把不同的厨房判成同一个"
    )


def test_选择厨房_只提交设备键(html: str):
    """预设对象里含 `说明` / `特征` 两个**非设备**键。

    整份 POST 过去会被后端 `_coerce_device_cfg` 判成"未知设备"，返回
    200 + `{"error":"设备配置无效"}` —— 一次正常的预设切换会变成"切换失败"。
    """
    fn = _fn_body(html, "applyKitchen", async_=False)
    assert re.search(r"DEVICE_KEYS\.forEach", fn), (
        "没有按 DEVICE_KEYS 过滤要提交的键（说明/特征会被当成未知设备）"
    )


# ---------------------------------------------- 动态事件 / 选择厨房 的「恢复默认」
def test_动态事件有恢复默认按钮且真的重置(html: str):
    """「恢复默认」必须把事件档位退回一个**真实存在**的选项，并清掉旧结果。

    ⚠ 最容易踩的坑是退回一个 `#eventType` 里没有的 value —— 给 `select.value`
      赋一个不存在的值会**静默变成空串**，下拉显示为空白；而后端拿到 `ev=''`
      会走 `else` 分支，`{stove,steamer,oven}['']` 取到 `undefined`，
      于是"恢复默认"变成了"用 undefined 设备做一次故障重排"。
    """
    assert re.search(r'id="eventResetBtn"[^>]*onclick="resetEvent\(\)"', html, re.S), (
        "找不到「恢复默认」按钮（#eventResetBtn → resetEvent()）"
    )
    m = re.search(r"const EVENT_DEFAULT\s*=\s*'([^']*)'", html)
    assert m, "找不到 EVENT_DEFAULT 常量"
    tag = html.split('id="eventType"', 1)[1].split("</select>", 1)[0]
    values = re.findall(r'<option value="([^"]*)"', tag)
    assert m.group(1) in values, (
        f"EVENT_DEFAULT={m.group(1)!r} 不在 #eventType 的选项 {values} 里 —— "
        "赋值会被静默吞成空串"
    )
    fn = _strip_js_comments(_fn_body(html, "resetEvent", async_=False))
    assert "EVENT_DEFAULT" in fn, "resetEvent() 没有用 EVENT_DEFAULT 复位"
    # ⚠ 必须钉**赋值**而不是 `"insertPick" in fn`：`const pick=getElementById('insertPick')`
    #   已经把那个词带进来了，只查标识符的话"删掉复位那一行"照样绿（实测漏网一次）。
    assert re.search(r"\bpick\.value\s*=", fn), "没有把「插哪一道」一并复位"
    assert re.search(r"onEventChange\(\)", fn), (
        "没有走 onEventChange() —— 上一次的结果摘要会留着，与当前档位不符"
    )
    assert "reschedule" not in fn, (
        "resetEvent() 不该自己发重排请求：设备故障是按请求生效的（没有需要清的服务端"
        "状态），而重排要跑 5~20s，用户点「恢复默认」不会预期按钮转二十秒"
    )


def test_选择厨房有恢复默认按钮且走后端重置(html: str):
    """「恢复默认」走 `POST /api/devices/reset`，不在前端写死默认值。

    两条纪律：
    ① 默认值的**唯一真源在后端**（`_DEFAULT_DEVICE_CFG`）。前端再抄一份的话，
       后端改了默认值而前端没改 —— 点「恢复默认」得到的是旧默认，且没有任何报错。
    ② 不能图省事走 `POST /api/devices` 把默认值发上去：那会把"默认值"当成一次
       用户选择**落盘**，以后后端改了默认，这台机器会顽固地停在旧默认上。
    """
    assert re.search(r'id="kitchenResetBtn"[^>]*onclick="resetKitchen\(\)"', html, re.S), (
        "找不到「恢复默认」按钮（#kitchenResetBtn → resetKitchen()）"
    )
    fn = _strip_js_comments(_fn_body(html, "resetKitchen"))
    assert "'/api/devices/reset'" in fn, "没有调用后端重置端点"
    assert "'/api/devices'" not in fn, (
        "走的是 POST /api/devices —— 会把默认值当成用户选择落盘，"
        "以后后端改默认值这台机器也回不去"
    )
    assert re.search(r"finally\s*\{", fn), "没有 finally —— 失败分支会跳过按钮恢复"
    tail = fn[fn.index("finally"):]
    assert re.search(r"btn\.disabled\s*=\s*false", tail), "finally 里没有恢复按钮"
    assert "loadPresets()" in fn, (
        "重置后没有重新反查生效预设 —— 下拉会停在一个已不再生效的预设上"
    )
    # ⚠ 必须判**同一个 if 条件里同时有这两项**：分开断言的话，`if(!r.ok){` 后面那句
    #   `const why=(d&&d.error)?…` 仍然带着 `d.error` ⇒ "去掉 error 判定"照样绿
    #   （实测漏网一次）。判据要落在**条件**上，不是函数体里出现过这个词。
    #   ⚠ 取条件不能用 `\(([^)]*)\)`：条件里有嵌套括号（`(d&&d.error)`），
    #     `[^)]*` 会停在**内层**的 `)` 上，随后要求 `{` 而实际是 `)` ⇒ 一条都匹配不到，
    #     断言恒假（"还原后也红"）。非贪婪 + 禁止跨行才对。
    conds = re.findall(r"if\s*\(([^\n]*?)\)\s*\{", fn)
    assert any("!r.ok" in c and "d.error" in c for c in conds), (
        "没有一条 if 条件**同时**判了 HTTP 状态与业务错误体 —— "
        "只判一条会让另一类失败掉进成功分支"
    )


# ---------------------------------------------- 控件可用性：回车提交 / 摆设控件
# 本节来自一次"逐控件遍历"审计（`scripts/ui_click_audit.mjs`，穷举全部 252 个可交互
# 控件 + 全部单行输入框派发一次事件，看 DOM/网络/alert/焦点有没有任何一处变化）。
#
# 静态接线其实是**完整**的：0 个未定义处理器、0 个重复函数、0 个死函数、0 个 JS 异常。
# 但**动态**层面查出两类"用户以为界面坏了"的缺口：
#   ① 单行输入框敲回车毫无反应 —— 全站原来只有 `#chatInput2` 一个绑了 keydown；
#   ② 「开饭时间」是纯摆设 —— HTML 有、样式有、能改，但全文件没有一行 JS 读它。
# 下面三组护栏分别钉住它们，防止以后改版再退化。

# 回车由**浏览器自己**承担提交语义的输入类型（滑杆、勾选、时间、文件…），
# 以及 readonly/disabled（用户根本敲不进去）—— 这些不该要求 onkeydown。
_ENTER_EXEMPT_TYPES = {"range", "checkbox", "time", "radio", "file", "submit", "button", "hidden"}

# 关键输入框 → 回车应当触发的那个动作（与它旁边按钮的 onclick 完全一致）。
_ENTER_TARGETS = {
    "agentText": "agentPlan()",
    "search": "loadRecipes()",
    "libSearch": "loadLib(this.value)",
    "setRemoteUrl": "saveModel()",
    "setRemoteKey": "saveModel()",
    "setModelInput": "saveModel()",
    "setToken": "saveToken()",
    "dev-${esc(k)}": "saveDevices()",
    "rfName": "saveRecipe(",
    # v3.9 执行态打点：回车 = 左边那个「✓ 完成」。
    # 不能接「报延迟」—— 那条路径要求 actual>0，空着按回车会被拒绝，
    # 而界面只回一句"报延迟要填实际分钟"，用户会以为输入框坏了。
    # ⚠ 所以这里钉到**完整实参**，只钉函数名的话 `reportExec(${i},'delayed')`
    #   也能混过去 —— 那正是"接错了比没接更危险"的那一类。
    "execActual": "reportExec(${i},'done')",
}


def _input_tags(src: str) -> list:
    """取出所有 `<input …>` 标签原文（跨行也算一个）。

    ⚠ 先剥 CSS 注释：样式表里有一段解释 `<input type=range>` 原生皮肤的注释，
      不剥的话它会被当成一个"没有 onkeydown 的真实控件" ⇒ 假红。
    """
    return re.findall(r"<input\b[^>]*>", _strip_css_comments(src), re.S)


def test_每个可编辑单行输入框都能回车提交(html: str):
    """单行输入框按回车必须等于"按下旁边那个按钮"。

    ⚠ 判据落在**标签属性**上，不是"文件里出现过 Enter" —— 后者会被 `#chatInput2`
      这一处实现（或注释里的说明文字）兜住，属于本项目铁律②的假绿。
    """
    missing = []
    for tag in _input_tags(html):
        m = re.search(r'id="([^"]+)"', tag)
        if not m:                      # 没有 id 的多半是注释/文案里的示例
            continue
        t = re.search(r'type="([a-z]+)"', tag)
        if t and t.group(1) in _ENTER_EXEMPT_TYPES:
            continue
        if re.search(r"\breadonly\b", tag) or re.search(r"\bdisabled\b", tag):
            continue
        if "onkeydown" not in tag or "Enter" not in tag:
            missing.append(m.group(1))
    assert not missing, (
        "这些单行输入框敲回车没有任何反应（用户会以为界面卡住）：" + "、".join(missing)
    )


def test_关键输入框回车提交到正确的动作(html: str):
    """回车必须接到**同一个**动作上，不能接错（接错了比没接更危险）。

    例：`#setToken` 接成 `saveModel()` ⇒ 用户以为存了令牌，实际改的是模型配置，
    而且界面还会回一句"已保存"。
    """
    bad = []
    for i, want in _ENTER_TARGETS.items():
        tag = next((t for t in _input_tags(html) if f'id="{i}"' in t), None)
        assert tag, f"找不到输入框 #{i}（id 改了？记得同步本表）"
        if "onkeydown" not in tag or "Enter" not in tag or want not in tag:
            bad.append(f"#{i} 期望回车 → {want}")
    assert not bad, "回车没有接到对应动作：" + "；".join(bad)


def test_开饭时间是墙上时钟锚点而不是摆设(html: str):
    """`#dinnerTime` 曾经是**纯摆设**：HTML 有、样式有、用户能改，但全文件没有一行
    JS 读它、后端也不接受 dinner 字段 —— 设成 18:30 完全无效，且用户无从察觉。

    现在它承担唯一职责：把排程结果的**相对分钟**（t=0 = 第一个占用资源的任务）
    翻译成墙上时钟（`开始下厨 = 开饭时间 − 总时长`）。
    ⚠ 它**不是约束** —— 不会反过来逼求解器压缩总时长（那会动摇 Γ 口径）；
      真来不及就如实提示"来不及"，而不是偷偷改计划。
    """
    assert re.search(r'id="dinnerTime"[^>]*onchange="updDinnerHint\(\)"', html, re.S), (
        "改了开饭时间却不重算提示"
    )
    # ① 真的有人读它 —— 这一条就是"摆设 vs 真控件"的分水岭
    anchor = _strip_js_comments(_fn_body(html, "dinnerAnchor", async_=False))
    assert "dinnerTime" in anchor, "dinnerAnchor() 没有读 #dinnerTime —— 摆设又回来了"
    # ② 相对→绝对 的换算只许有一个口径
    clock = _strip_js_comments(_fn_body(html, "clockOf", async_=False))
    assert "dinnerAnchor()" in clock and "fmtClock(" in clock, (
        "clockOf() 没有走 dinnerAnchor()+fmtClock() —— 出现第二套时间换算口径"
    )
    # ③ 排程渲染后必须刷新提示，**两条分支都要**（无 plan 的早退分支同样会留着旧值）
    body = _strip_js_comments(_fn_body(html, "render", async_=False))
    assert body.count("updDinnerHint()") >= 2, (
        "render() 只在一处刷新开饭提示 —— 早退分支（还没排程）会显示上一次的结论"
    )
    # ④ 不能只在提示条里算：通知与步骤详情都要带上墙上时钟，否则用户还得自己倒推
    assert "clockOf(" in body, "通知区没有把相对时间翻成墙上时钟"
    assert "clockOf(" in _strip_js_comments(_fn_body(html, "openStepDetail", async_=False)), (
        "步骤详情的时间行没有带上墙上时钟"
    )


def test_开饭提示必须恒定单行(html: str):
    """提示文字一旦在窄列里折成两三行，右列就被撑高，「生成排程」主按钮会被推进
    悬浮球里 —— 而且**只在 1366×768 这一档发作**（1440×900 有余量，看不出来）。

    实测 A/B（2026-10-08）：
    · 长文案（"🕐 按 18:30 开饭：需 17:30 开始下厨（总时长 60 分）"）→ 折 3 行，
      单视口 1366×768 **118/2 红**，多视口扫描也报 `1366×768(:生成排程 →)`；
    · 短文案（"17:30 下厨"，完整解释放 title）→ 120/0 全绿。

    ⚠ 精确高度是排版探针的活（扫描表里那条「三列仪表盘档：悬浮球不遮挡排程页
      交互元素」就是为它加的）。这里只钉住"保持单行"的**机制**，不锁像素。
    """
    tag = next((t for t in re.findall(r"<[^>]*id=\"dinnerHint\"[^>]*>", html, re.S)), None)
    assert tag, "找不到 #dinnerHint"
    assert "white-space:nowrap" in tag, (
        "#dinnerHint 没有 nowrap —— 文案一长就折行，右列被撑高后主按钮会进悬浮球"
    )
    assert "text-overflow:ellipsis" in tag, (
        "#dinnerHint 没有 ellipsis —— 文案一长会溢出或被换行，行高不再恒定"
    )
    fn = _strip_js_comments(_fn_body(html, "updDinnerHint", async_=False))
    assert "innerHTML" not in fn, (
        "updDinnerHint 用 innerHTML 写提示 —— 一旦带上 <br> 就必然多行（实测那次就是这么坏的）"
    )
    assert "textContent" in fn, "提示没有走 textContent"
    assert "title" in fn, "完整解释没有落到 title —— 正文取短说法就丢信息了"


def test_遍历脚本仍在跑回车审计(html: str):
    """跨文件钉住审计脚本本身：它才是发现"回车没反应"的那双眼睛。

    ⚠ 护栏和探针都会随代码变好而失效（本项目铁律⑤）—— 如果哪天有人把回车遍历
      删掉，静态护栏仍会全绿，但"用户敲回车没反应"这类缺陷就再也没人报了。
    ⚠ 路径可被 `FT_CLICK_AUDIT` 覆盖，好让 `scripts/_mutate_web_guards.py` 把
      缺陷注入**副本**（同一个"从不碰真文件"的纪律）。
    """
    probe = Path(os.environ.get("FT_CLICK_AUDIT")
                 or (Path(__file__).resolve().parents[1] / "scripts" / "ui_click_audit.mjs"))
    assert probe.exists(), "逐控件审计脚本不见了：scripts/ui_click_audit.mjs"
    src = probe.read_text(encoding="utf-8")
    assert "回车键遍历" in src and 'key: "Enter"' in src, "回车遍历被删掉了"
    assert "回车缺口" in src, "回车遍历的结果没有被统计/报出来"
    assert "readOnly" in src and "disabled" in src, (
        "没有排除 readonly/disabled 输入框 —— 统计里会全是假阳性"
    )


# ===========================================================================
# v3.7 计划工具：历史还原 / 对比 / 导出 / 采购清单 / 为什么这样排 / 快捷键
# ===========================================================================
def test_历史行可点还原且保留cfg_row类(html: str):
    """历史记录行必须整行可点还原，且**保留 `cfg-row` 类**。

    ⚠ `cfg-row` 不是样式需要，而是**探针契约**：`ui_probe.mjs` 有一条
      「`#histList .cfg-row` 行数 == 后端 count」。换掉类名会让那条断言红，
      报出来像渲染 bug，其实是契约被单方面改了。

    ⚠ 渲染逻辑 v3.8 起抽到了 `renderHist()`（"撤销清空"要**只重绘、不重新请求**
      —— 那时后端根本没被调用过，重拉只会拿到一份还没清空的旧列表）。所以这里
      判 `renderHist` 的函数体，并另外钉住 `loadHist` **确实走的是它** ——
      只判 `loadHist` 会误报"丢了类名"，其实是位置变了。
    """
    load = _strip_js_comments(_fn_body(html, "loadHist", async_=True))
    assert "renderHist()" in load, "loadHist 没走 renderHist —— 渲染被拆成了两份"
    fn = _strip_js_comments(_fn_body(html, "renderHist", async_=False))
    assert "cfg-row" in fn and "hist-row" in fn, "历史行丢了 cfg-row/hist-row 类"
    assert "restoreSchedule(" in fn, "历史行没有绑定还原动作（点下去没反应）"
    assert "data-sid=" in fn, "行上没有 sid —— 还原/对比都拿不到句柄"


def test_还原历史不会重新排程(html: str):
    """还原必须走 `/api/schedules/{sid}`，**不能**重新调 `/schedule`。

    重新排一次会凭空多出一条历史记录：用户点几次"看看以前的计划"，
    历史就被自己刷满了，还会把真正想留的那条挤出去。
    """
    fn = _strip_js_comments(_fn_body(html, "restoreSchedule", async_=True))
    # 详情接口只允许有一个出口：`fetchSchedule()`。它内部才是 `/api/schedules/{sid}`。
    assert "fetchSchedule(" in fn, "还原没有走详情读取（fetchSchedule）"
    assert "/schedule'" not in fn and '"/schedule"' not in fn, \
        "还原里又调了一次 /schedule —— 会凭空多出一条历史记录"
    assert "render(" in fn, "还原完没有重绘"
    detail = _strip_js_comments(_fn_body(html, "fetchSchedule", async_=True))
    assert "/api/schedules/" in detail, "fetchSchedule 没有打详情端点"


def test_历史行复选框不许自带onclick(html: str):
    """行内复选框的"让路"必须由**行自己**判事件源，而不是给复选框挂
    `onclick="event.stopPropagation()"`。

    ⚠ 两者**语义等价**（都是"点勾选别还原计划"），但**探针按 `onclick` 属性枚举
      控件**：给复选框挂 onclick 会让「全控件反应遍历」把它当成一个"点下去什么都
      没发生"的控件归入待判断，于是**每多一条历史就多一个待判断**，退出码跟着变 1，
      基线数字再也不稳定。实测：7 条历史时待判断 = 7（全是 hist-pick）。
    """
    src = _strip_html_comments(html)
    picks = re.findall(r'<input[^>]*class="hist-pick"[^>]*>', src)
    assert picks, "找不到 hist-pick 复选框（对比功能靠它勾选）"
    for tag in picks:
        assert "onclick" not in tag, \
            "hist-pick 挂了 onclick —— 会被穷举探针扫成'没反应'，数字随历史条数漂移"
        assert "onchange" in tag, "hist-pick 没有绑定 pickCmp"
    fn = _strip_js_comments(_fn_body(html, "restoreSchedule", async_=True))
    assert "ev.target" in fn and "closest('input')" in fn, \
        "restoreSchedule 没有让路给复选框 —— 点勾选会连带还原计划"


def test_对比最多两条且不给利用率标优劣(html: str):
    """对比最多保留 2 条；设备利用率**不标优劣**。

    利用率是仪表盘不是方向盘（见 MEMORY §一）：高利用率不等于计划更好，
    给它标绿会让用户以为"把设备塞满"是个优化目标。
    """
    fn = _strip_js_comments(_fn_body(html, "pickCmp", async_=False))
    assert "CMP_SEL.length>2" in fn, "勾选没有上限 —— 勾第 3 条会把前两条都留下"
    cmp_fn = _strip_js_comments(_fn_body(html, "openCompare", async_=True))
    assert "设备利用率" in cmp_fn, "对比表里没有设备利用率"
    line = [l for l in cmp_fn.splitlines() if "设备利用率" in l][0]
    assert "false)" in line, "给设备利用率标了优劣 —— 它是仪表盘不是方向盘"


def test_导出CSV必须带UTF8_BOM(html: str):
    """CSV 必须带 UTF-8 BOM，否则 Excel 打开中文表头全是乱码。

    ⚠ 实测踩到：注释里写了"必须带 BOM"，实现却漏了 —— 这种"注释与代码不一致"
      静态看不出来，只有真拿 Excel 打开才发现。
    """
    fn = _strip_js_comments(_fn_body(html, "downloadText", async_=False))
    assert "\\ufeff" in fn, "downloadText 没有加 UTF-8 BOM（Excel 中文会乱码）"
    assert "/csv/i" in fn, "BOM 没有限定在 csv —— 给 html/文本也加会多出看不见的字符"


def test_作业单不用window_open(html: str):
    """作业单必须走**下载**，不能用 `window.open` + `print()`。

    弹出窗口会被拦截器挡掉（用户看到的就是"点了没反应"），无头环境里
    `window.open` 还会直接抛错 —— 那是探针里最容易被误判成"页面有 bug"的一类。
    """
    fn = _strip_js_comments(_fn_body(html, "exportSheet", async_=False))
    assert "window.open" not in fn, "用了 window.open —— 会被拦截器挡掉且无头环境报错"
    assert "downloadText(" in fn, "作业单没有走下载"
    assert "<!DOCTYPE html>" in fn, "作业单不是一份可独立打开的 html"
    assert "window.print()" in fn, "作业单打开后不会自动进打印预览"


def test_快捷键不抢输入框(html: str):
    """全局快捷键必须在**可编辑元素**上让路，否则打字就会误触发。

    三条必须同时成立：① 不抢 input/textarea/select/contenteditable；
    ② 不抢带修饰键的组合（否则会盖掉浏览器/系统快捷键）；
    ③ 数字键走 `show()` 而不是自己改 classList（绕过会漏掉 loadLib 等副作用）。
    """
    m = re.search(r"document\.addEventListener\('keydown',e=>\{(.*?)\n\}\);", html, re.S)
    assert m, "找不到全局快捷键处理器"
    body = _strip_js_comments(m.group(1))
    for tag in ("input", "textarea", "select"):
        assert f"'{tag}'" in body, f"快捷键没有排除 <{tag}> —— 在里面打字会误触发"
    assert "isContentEditable" in body, "没有排除 contenteditable"
    assert "e.ctrlKey" in body and "e.altKey" in body and "e.metaKey" in body, \
        "没有排除修饰键组合 —— 会盖掉浏览器自身快捷键"
    assert "show(KEY_VIEWS[i],btn)" in body, "数字键没有走 show()（会漏掉站点切换的副作用）"
    assert "querySelectorAll('.menu button')[i]" in body, \
        "没有从 .menu 取按钮 —— show() 需要一个按钮元素来打高亮"


def test_对话记录会持久化(html: str):
    """对话必须存 localStorage —— `/api/chat` 本来就收整个 messages 数组，
    真实缺口是**刷新即失**（chatMsgs 只活在内存里）。"""
    assert "function chatSave()" in html and "function chatLoad()" in html, "没有持久化函数"
    assert "localStorage.setItem(CHAT_KEY" in html, "对话没有写进 localStorage"
    assert "chatSave();" in html, "push 之后没有保存"
    send = _strip_js_comments(_fn_body(html, "sendChat", async_=True))
    assert send.count("chatSave()") >= 2, "用户消息与模型回复都要保存（只存一半会串不上）"
    assert "renderChatHistory();" in html, "打开页面没有恢复上次的对话"


def test_改菜单必须按后端结论分流(html: str):
    """改菜单必须判 `d.ok`：模型不可用时**如实说**，绝不悄悄按原样重排。

    这是本项目最在意的一类缺陷 —— 界面上回一句"已更新"，实际一个菜都没改。
    """
    fn = _strip_js_comments(_fn_body(html, "reviseFromChat", async_=True))
    assert "if(!d.ok)" in fn, "没有判 d.ok —— 失败会被当成成功"
    assert "d.notice" in fn, "没有把后端给的说明转达给用户"
    assert "/api/revise_plan" in fn, "没有调改菜单接口"
    assert "await run()" in fn, "改完菜单没有重新排程（用户看到的是旧计划）"


def test_为什么这样排是按需弹层而不是常驻面板(html: str):
    """「为什么这样排」必须是**按需弹层**。

    2026-10-08 用户明确要求把常驻的排程诊断面板删掉（它一直占着列宽）。
    这里重新引入的是"不点不开"的弹层 —— 与那个要求不冲突，但**不能**退回成
    常驻容器（那等于把删掉的东西又加回来）。
    """
    assert "function openWhy()" in html, "找不到 openWhy()"
    fn = _strip_js_comments(_fn_body(html, "openWhy", async_=False))
    assert "insight" in fn, "没有用后端已有的 insight 数据"
    assert "openInfo(" in fn, "没有走弹层"
    # 常驻面板的标志物：一个被 render() 直接写入的诊断容器。
    render_fn = _strip_js_comments(_fn_body(html, "render", async_=False))
    assert "insight" not in render_fn, \
        "render() 里又出现了 insight —— 常驻诊断面板被加回来了（用户明确要求删掉）"


def test_不可行建议要能照着做(html: str):
    """排程不可行/有风险时必须给**可照做**的建议，不能只说"排程失败"。"""
    assert "function hintList(" in html and "function showHints(" in html, "没有建议生成器"
    fn = _strip_js_comments(_fn_body(html, "hintList", async_=False))
    assert "warnings" in fn, "没有用后端给的 warnings"
    assert "conflict" in fn, "没有处理设备冲突"
    assert "菜谱库" in fn, "缺失菜谱没给出可照做的下一步（去菜谱库新建）"
    assert "选择厨房" in fn, "设备不够没给出可照做的下一步（换厨房预设）"
    render_fn = _strip_js_comments(_fn_body(html, "render", async_=False))
    assert "showHints(" in render_fn, "render() 没有调用 showHints —— 建议永远不会出现"
    # 没有建议时必须是**空容器**（高度 0），否则会挤到甘特图。
    show_fn = _strip_js_comments(_fn_body(html, "showHints", async_=False))
    assert "hs.length?" in show_fn and ":'';" in show_fn, \
        "没有建议时没有把容器清空 —— 空容器会占高度挤甘特图"


def test_工具栏按钮不许用disabled(html: str):
    """工具栏按钮一律不用 `disabled`。

    禁用态点它"什么都没发生"，正是用户这次要查的那类"操作没反应"；
    改成"点了就告诉你为什么不行"（弹层/alert）体验更好，也不给探针留歧义。
    """
    seg = re.search(r'<div class="tool-row" id="planTools">(.*?)</div>', html, re.S)
    assert seg, "找不到 #planTools"
    assert "disabled" not in seg.group(1), "计划工具栏里有 disabled 按钮"
    fn = _strip_js_comments(_fn_body(html, "updPlanTools", async_=False))
    assert "disabled" not in fn, "updPlanTools 在切换 disabled —— 点下去会没反应"


# ===========================================================================
# v3.8 C1：撤销条 + 二次确认
# ---------------------------------------------------------------------------
# 危险操作两条腿：**事前确认**（说清后果）与**事后撤销**（点错了能回来）。
# 分工按"这个操作能不能真的回退"来定：能回退的给撤销条，回不去的（清空历史，
# 后端没有"写回"端点）保留确认弹层 + 用**延迟提交**换出撤销窗口。
# ===========================================================================
def test_撤销条必须是fixed且默认空容器(html: str):
    """撤销条必须 `position:fixed`，且初始是**空容器**。

    ⚠ 与 `.llm-toast` 同一条纪律：排程页高度是三列 flex 算出来的，往里插一个
      **流内**元素会把甘特图挤到 35px（见 MEMORY §五，静态测试看不见、只有真
      Edge 探针会红）。提示类组件一律悬浮，不参与排程页的高度计算。
    ⚠ 初始必须空的：非空但 `display:none` 也还好，可一旦忘了加 `.on` 的
      display 规则，一个空壳就会永久占着屏幕底部。
    """
    css = _strip_css_comments(html)
    m = re.search(r"\.undo-bar\{(.*?)\}", css, re.S)
    assert m, "找不到 .undo-bar 样式"
    rule = m.group(1).replace(" ", "").replace("\n", "")
    assert "position:fixed" in rule, "撤销条不是 fixed —— 会进文档流，把甘特图挤变形"
    assert "display:none" in rule, "撤销条默认不是隐藏的 —— 空壳会一直占着底部"
    assert '<div class="undo-bar" id="undoBar"></div>' in html, \
        "撤销条容器里不该有初始内容（它是默认隐藏的空容器）"


def test_撤销按钮要显式覆盖全局button样式(html: str):
    """`.undo-bar .ub-btn` 必须显式写 `width:auto` / `color` / `box-shadow:none`。

    全局 `button` 是 `width:100%;color:#fff;box-shadow:橙色`：不覆盖的话，
    「撤销」会被撑成整行、字变成白底白字 —— 在浅色提示条上直接看不见。
    """
    css = _strip_css_comments(html)
    m = re.search(r"\.undo-bar \.ub-btn\{(.*?)\}", css, re.S)
    assert m, "找不到 .undo-bar .ub-btn 样式"
    rule = m.group(1).replace(" ", "").replace("\n", "")
    assert "width:auto" in rule, "撤销按钮没写 width:auto —— 会被撑成整行"
    assert "box-shadow:none" in rule, "撤销按钮没写 box-shadow:none —— 会带上全局的橙色投影"
    assert "color:" in rule, "撤销按钮没显式写 color（全局是 #fff，在浅色条上看不见）"


def test_确认弹层不许用window_confirm(html: str):
    """新增的确认一律走 `askConfirm` 弹层，**不用** `window.confirm`。

    `window.confirm` 在 jsdom（两个 UI 探针）里是 not implemented，探针只能把它
    打桩成恒真来绕开 —— 等于"确认"这一步在测试里从来没被真正验证过。
    改成弹层之后，探针可以**真的点**「确认」和「取消」，两条分支都能测。
    """
    for fn_name in ("askConfirm", "runConfirm"):
        assert f"function {fn_name}(" in html, f"找不到 {fn_name}"
    for fn_name in ("askClearChat", "askClearToken"):
        fn = _strip_js_comments(_fn_body(html, fn_name, async_=False))
        assert "confirm(" not in fn, f"{fn_name} 用了 window.confirm"
        assert "askConfirm(" in fn, f"{fn_name} 没走统一的确认弹层"


def test_关闭弹层要丢掉待执行的确认回调(html: str):
    """`closeInfo()` 必须把 `CONFIRM_FN` 清掉。

    不清的话，用户点「取消」后那个回调还挂着，等下一次「确认」（**另一个**操作）
    被触发时执行 —— 用户点的是"清空对话"，实际执行的却是上一次的"删除菜谱"。
    """
    fn = _strip_js_comments(_fn_body(html, "closeInfo", async_=False)).replace(" ", "")
    assert "CONFIRM_FN=null" in fn, \
        "closeInfo 没清 CONFIRM_FN —— 取消后回调会在下一次确认时被误触发"
    # runConfirm 必须**先取出再关弹层**（closeInfo 会把它清成 null）
    rc = _strip_js_comments(_fn_body(html, "runConfirm", async_=False))
    assert rc.index("CONFIRM_FN") < rc.index("closeInfo()"), \
        "runConfirm 先关弹层才取回调 —— 拿到的永远是 null，确认按钮点了没反应"


def test_撤销回调只保留最近一次(html: str):
    """`undoToast` 每次都要清掉上一个定时器并覆盖回调。

    多个撤销条叠在一起，用户根本不知道该撤销哪一个 —— 保留最近一次是有意的
    取舍，不是漏了队列。旧定时器不清的话，它到点会把**新**那条一起收走。
    """
    fn = _strip_js_comments(_fn_body(html, "undoToast", async_=False)).replace(" ", "")
    assert "clearTimeout(UNDO_TIMER)" in fn, \
        "undoToast 没清上一个定时器 —— 旧的那条会提前把新的收走"
    assert "UNDO_FN=fn" in fn, "undoToast 没覆盖撤销回调"
    hide = _strip_js_comments(_fn_body(html, "hideUndo", async_=False)).replace(" ", "")
    assert "UNDO_FN=null" in hide, "hideUndo 没清回调 —— 撤销条收走了但回调还挂着"
    # 纯提示（没有回调）不该渲染「撤销」按钮：渲染了却点不动，比没有更糟
    assert "fn?" in fn, "undoToast 没有区分「纯提示」与「可撤销」两种形态"


def test_清空历史必须是延迟提交(html: str):
    """清空历史**不能立刻**调 `DELETE /api/schedules`。

    后端一删落盘文件就真回不来了（`/api/schedules` 没有"写回"端点），所以
    "不新增后端接口、又能真撤销"的唯一做法是：先给 8 秒窗口，到点再提交。
    代价是窗口内刷新页面 ⇒ 删除没生效 —— 这是**更安全**的方向。
    """
    fn = _strip_js_comments(_fn_body(html, "doClearHist", async_=False)).replace(" ", "")
    assert "method:'DELETE'" not in fn, "doClearHist 立刻删了 —— 撤销窗口是假的"
    assert "setTimeout(" in fn, "doClearHist 没有延迟提交"
    assert "commitClearHist" in fn, "doClearHist 没把真正的删除交给 commitClearHist"
    assert "undoToast(" in fn, "doClearHist 没给撤销入口"
    commit = _strip_js_comments(_fn_body(html, "commitClearHist", async_=True))
    assert "DELETE" in commit, "commitClearHist 没有真的删"
    undo = _strip_js_comments(_fn_body(html, "undoClearHist", async_=False))
    assert "renderHist()" in undo, "撤销没有重绘列表"
    # ⚠ 既要拦 `apiFetch` 直调，也要拦 `loadHist()` —— 后者内部才是 `apiFetch`。
    #   只判 `apiFetch` 的话，"撤销时改成 loadHist()"这个变异会被兜住（实测）。
    assert "apiFetch" not in undo and "loadHist(" not in undo, \
        "撤销里去重新拉了一次列表 —— 后端还没清，拉回来的是旧数据，看着像撤销失败"
    # 渲染必须只有一份（`loadHist` 走它，撤销也走它）
    load = _strip_js_comments(_fn_body(html, "loadHist", async_=True))
    assert "renderHist()" in load, "loadHist 没走 renderHist —— 渲染被拆成了两份"


def test_删除菜谱留了撤销快照(html: str):
    """删菜谱必须**在删除前**留快照，且撤销能真的把菜找回来。

    官方菜走 `/restore`（后端本来就有墓碑恢复）；自建菜是真删，只能用 `raw_*`
    原文重建 —— 拿展示用的结构化数组（`[{name,amount}]` / 切好的 steps）去 POST
    会被 `RecipeIn`（要字符串）拒掉或丢格式。
    """
    fn = _strip_js_comments(_fn_body(html, "deleteRecipe", async_=True))
    assert "undoToast(" in fn, "删菜谱没有撤销入口"
    assert "raw_ingredients" in fn and "raw_steps" in fn, \
        "快照没用 raw_* 原文 —— 自建菜重建会丢格式"
    assert "confirm(" in fn, "删菜谱丢了二次确认"
    undo = _strip_js_comments(_fn_body(html, "undoDeleteRecipe", async_=True))
    assert "/restore" in undo, "官方菜的撤销没走 /restore"
    assert "'/api/recipes'" in undo.replace('"', "'"), "自建菜的撤销没有走 POST 新建"


def test_恢复默认厨房可撤销(html: str):
    """「恢复默认」必须留一份重置前快照，撤销时写回 `POST /api/devices`。

    ⚠ 撤销**不能**再调 `/api/devices/reset` —— 那是"再恢复一次默认"，
      不是"回到我原来的样子"。两者只在"用户本来就一直在用默认"时才等价。
    ⚠ 快照必须在**重置前**取：`/api/devices/reset` 会把本机用户层删掉。
    """
    fn = _strip_js_comments(_fn_body(html, "resetKitchen", async_=True))
    assert "undoToast(" in fn, "恢复默认没有撤销入口"
    # ⚠ 必须判 `window._dev` 而不是裸 `_dev` —— 函数体后面还有 `_devStr(d)`，
    #   它含 `_dev` 子串，判裸 `_dev` 会让"不留快照"的变异被兜住（实测）。
    assert "window._dev" in fn, "没有取重置前的设备快照（window._dev）"
    assert fn.index("window._dev") < fn.index("/api/devices/reset"), \
        "快照在重置之后才取 —— 那时用户层已经被删了"
    undo = _strip_js_comments(_fn_body(html, "undoResetKitchen", async_=True))
    assert "'/api/devices'" in undo.replace('"', "'"), "撤销没有写回 /api/devices"
    assert "/api/devices/reset" not in undo, \
        "撤销调了 /reset —— 那是再恢复一次默认，不是回到重置前"


def test_清空对话与令牌可撤销(html: str):
    """清空对话/清除令牌都要能撤销，且对话撤销要**三处一起还原**。"""
    chat = _strip_js_comments(_fn_body(html, "doClearChat", async_=False))
    assert "undoToast(" in chat, "清空对话没有撤销入口"
    assert "chatSave()" in chat, "撤销没有写回 localStorage —— 刷新一下又没了"
    assert "renderChatHistory()" in chat, "撤销没有重绘气泡 —— 当前页面还是空的"
    tok = _strip_js_comments(_fn_body(html, "doClearToken", async_=False))
    assert "undoToast(" in tok, "清除令牌没有撤销入口"
    assert "saveToken()" in tok, "撤销没有重新保存令牌"
