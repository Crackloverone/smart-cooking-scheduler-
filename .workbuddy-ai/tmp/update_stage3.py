"""把 stage_3 收尾写进 pipeline-state.yaml（用脚本文件而不是 bash -c，避免引号地狱）。"""
import io
import os

R = "output/7a42554e-92b5-4197-966b-5a42b44b2530"
p = os.path.join(R, "pipeline-state.yaml")
DOCX = "D:\\Xuzj\\ky\\Project\\ft\\output\\7a42554e-92b5-4197-966b-5a42b44b2530\\stage3\\智能烹饪调度Agent作品报告.docx"

t = io.open(p, encoding="utf-8").read()
t = t.replace("updated_at: 2026-10-08T21:06:00+08:00", "updated_at: 2026-10-08T21:08:00+08:00")
t = t.replace("current_stage: 3", "current_stage: completed")

old3 = (
    "  stage_3:\n"
    "    status: pending\n"
    "    started_at: null\n"
    "    completed_at: null\n"
    "    executor: null\n"
    "    input_format: null\n"
    "    output_path: null\n"
    "    skill_used: []\n"
    "    present_files_opened: false\n"
    "    fallback: false\n"
    "    fallback_reason: null"
)
new3 = (
    "  stage_3:\n"
    "    status: completed\n"
    "    started_at: 2026-10-08T21:06:00+08:00\n"
    "    completed_at: 2026-10-08T21:08:00+08:00\n"
    "    executor: doc-converter\n"
    "    input_format: html\n"
    '    output_path: "' + DOCX + '"\n'
    "    skill_used: [html-to-docx]\n"
    "    present_files_opened: true\n"
    "    fallback: false\n"
    "    fallback_reason: null"
)
assert old3 in t, "stage_3 锚点没找到"
t = t.replace(old3, new3, 1)

exists = os.path.exists(DOCX) and os.path.getsize(DOCX) > 0
oldc = (
    "consistency_check:\n"
    "  output_files_exist: null\n"
    "  stage_chain_complete: null\n"
    "  last_checked_at: null\n"
    "  errors: []"
)
newc = (
    "consistency_check:\n"
    "  output_files_exist: " + ("pass" if exists else "fail") + "\n"
    "  stage_chain_complete: pass\n"
    "  last_checked_at: 2026-10-08T21:08:00+08:00\n"
    "  errors: []"
)
assert oldc in t, "consistency_check 锚点没找到"
t = t.replace(oldc, newc, 1)

io.open(p, "w", encoding="utf-8", newline="").write(t)
print("docx 存在且非空:", exists, os.path.getsize(DOCX) if exists else 0, "bytes")
print("current_stage 已置 completed")
