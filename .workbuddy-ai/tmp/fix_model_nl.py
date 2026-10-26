"""修掉上一步 heredoc 造成的 /n 损坏（Git Bash 会把 \\n 里的反斜杠改写成 /）。"""
import io

p = 'app/scheduler/model.py'
t = io.open(p, encoding='utf-8').read()

bad = '    if workers is None:/n        workers = int(os.environ.get("FT_EXACT_WORKERS", "1") or 1)'
good = ('    if workers is None:\n'
        '        workers = int(os.environ.get("FT_EXACT_WORKERS", "1") or 1)')
assert bad in t, '找不到被损坏的那一行'
t = t.replace(bad, good, 1)
io.open(p, 'w', encoding='utf-8', newline='').write(t)
print('已修复换行')
