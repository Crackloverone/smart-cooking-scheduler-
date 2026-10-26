"""
把"测量类"脚本钉在**离线档**（`setdefault`，显式传进来的值优先）。

为什么需要它
------------
`bench / ablation / lb_probe / gap_probe / demo_schedule / l1_* / batch_parse`
量的是**排程器**，不是模型。

改造前它们天然离线 —— 因为 `llm.enabled()` 要求 `bool(FT_LLM_API_KEY)`，
而仓库里 `API_KEY` 默认为空。2026-10-04 默认 provider 换成**本地 Ollama**
（免密钥）之后，`enabled()` 默认为 True，这些脚本会把模型真的拉进闭环：

  · 结果变成"这台机器装没装 Ollama、装的是哪个模型"的函数，**不可复现**；
  · 50 组基准从几十秒变成几分钟；
  · 而文档 / MEMORY 里记录的基线（出锅差中位 2.0 · <5min 82% · 总时长 82.0）
    是**离线**测的 —— 静默换口径等于让那组基线悄悄失效，
    比"跑得慢"危险得多。

要测"模型在环"的版本，用环境变量显式打开：

    FT_LLM=on FT_AGENT_LLM=on python -m scripts.bench 50

`warm_cache.py` **不调用本函数** —— 它的全部意义就是调模型。
"""
from __future__ import annotations

import os

_OFFLINE_SWITCHES = ("FT_LLM", "FT_AGENT_LLM", "FT_WARM_LLM", "FT_NOTIFY_LLM")


def force_offline_default() -> None:
    """四个 LLM 开关都按"未设置即 off"处理（已显式设置的不动）。"""
    for k in _OFFLINE_SWITCHES:
        os.environ.setdefault(k, "off")
