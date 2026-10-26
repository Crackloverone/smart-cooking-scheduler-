"""静态护栏：探针源码里的「模板字符串吃掉反斜杠」陷阱。

背景（2026-10-03 实测事故，价值最高的一条）
--------------------------------------------------
`scripts/ui_layout_probe.mjs` 里的 `fitExpr` 是一段**模板字符串**，会被送进 CDP
当 JS 源码求值。它原本写着：

    t.split(/\\s+/)      ← 源码里实际只有一个反斜杠

但模板字符串会把 `\\s` 解析成 `s`（反斜杠被吃掉），求值时变成 `split(/s+/)`。
而 `"260px 639px 260px"` 里**没有任何字母 s** ⇒ 永远只切出 1 段
⇒ `colTracks` 恒为 1 ⇒ 永远判定"窄屏/堆叠"⇒ **桌面分支那 7 条检查从未执行过**。

更阴的是：窄屏分支在桌面尺寸下也照样全绿（`vpScrollH>=vpClientH-1`、
`vpH>=innerH-2` 这类条件在桌面也成立），所以探针一直报"通过"，
属于典型的"绿得毫无意义"——比没有护栏更危险。

同文件 `VP_MEASURE` 写的是 `\\s+`（双反斜杠，正确），只有 `fitExpr` 漏了一个，
肉眼极难发现。所以用这条护栏把整个 bug 类堵死：
**模板字符串里不许出现「单个反斜杠 + 词类字符」的正则片段。**

（`${...}` 里的代码是真正求值的 JS，正则写法合法；本扫描会把整段模板串都算进来，
若将来真有 `${}` 内含正则，需在这里放行。）
"""
import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# 会被模板串吞掉反斜杠、且正是正则常用词类的转义
#   \s \d \w \S \D \W \b \B
# 注意：\n \t \r \f \v \0 \x \u 是合法转义（不会被吞），不在此列。
DANGEROUS = re.compile(r"(?<!\\)\\[sdwSDWbB]")


def template_literals(src):
    """产出 (起始行号, 模板串正文)。按未转义反引号切分，够用即可。"""
    out, buf, i, start, line = [], [], 0, None, 1
    while i < len(src):
        ch = src[i]
        if ch == "\n":
            line += 1
        if ch == "\\":
            buf.append(src[i:i + 2])       # 转义对整体保留（含 \\ 与 \`）
            i += 2
            continue
        if ch == "`":
            if start is None:
                start = line
                buf = []
            else:
                out.append((start, "".join(buf)))
                start = None
            i += 1
            continue
        if start is not None:
            buf.append(ch)
        i += 1
    return out


def _strip_line_comments(body):
    """去掉行首 `//` 注释。

    模板串正文会被当成 JS 源码求值，里面**可以**写 `//` 注释；注释里出现
    `\\s` 这类文字是合理说明，不该报警。只剥「行首注释」，避免把
    `'http://…'` 这种字符串误伤（那才是不该动的真代码）。
    """
    keep = []
    for ln in body.split("\n"):
        if ln.lstrip().startswith("//"):
            keep.append("")          # 保留换行，行号不错位
        else:
            keep.append(ln)
    return "\n".join(keep)


def _scan(path):
    src = io.open(path, encoding="utf-8").read()
    bad = []
    for start_line, body in template_literals(src):
        clean = _strip_line_comments(body)          # 行数不变，行号仍对得上
        for m in DANGEROUS.finditer(clean):
            ln = start_line + clean[:m.start()].count("\n")
            ctx = clean[max(0, m.start() - 32):m.start() + 24].replace("\n", "⏎")
            bad.append(f"{path.name}:{ln}  {m.group(0)}  … {ctx}")
    return bad


def test_no_single_backslash_regex_inside_template_literals():
    """模板串里的正则必须写双反斜杠，否则反斜杠会被吞、护栏静默失效。"""
    problems = []
    for p in sorted(SCRIPTS.glob("*.mjs")):
        problems += _scan(p)
    assert not problems, (
        "模板字符串里出现会被吞掉反斜杠的正则片段（应写双反斜杠 \\\\s）：\n  "
        + "\n  ".join(problems)
    )


def test_guard_has_teeth():
    """变异自检：把正确的双反斜杠改回单个，扫描器必须报出来。"""
    src = io.open(SCRIPTS / "ui_layout_probe.mjs", encoding="utf-8").read()
    assert "split(/\\\\s+/)" in src, "找不到预期的双反斜杠写法，护栏锚点已失效"
    mutated = src.replace("split(/\\\\s+/)", "split(/\\s+/)", 1)
    assert mutated != src, "变异未生效"
    hits = []
    for start_line, body in template_literals(mutated):
        for m in DANGEROUS.finditer(body):
            hits.append((start_line, m.group(0)))
    assert hits, "变异后扫描器没抓到 —— 这条护栏没有牙齿"
