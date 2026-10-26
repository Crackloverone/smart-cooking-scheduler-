#!/usr/bin/env bash
# =============================================================================
#  打**服务器部署包**（只打 tar.gz + zip，不上传）
#
#  用法：
#      bash deploy/pack-server.sh              # 产出 dist/cooking-agent-server.{tar.gz,zip}
#      bash deploy/pack-server.sh --only tar   # 只打 tar.gz（服务器用这个就够）
#      bash deploy/pack-server.sh --check      # 只校验，不产出包
#
#  为什么要有这个脚本
#  ---------------------------------------------------------------------------
#  真正的打包逻辑在 `scripts/pack_server.py`（**单一真源**，跨平台）。
#  本脚本只是薄封装，让习惯命令行的人不必记 `python -m scripts.pack_server`。
#  `deploy/upload.sh` 也调用同一个脚本 —— 两边不会漂移。
#
#  ⚠ 打包前会**强制**同步 publish/ 并复核，不同步就中止。
#    这条是本项目真实踩过的坑：线上发布目录是**拷贝**，改了源码不同步过去，
#    线上就还是旧版本，而本地完全看不出来。
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

# ------------------------------------------------------- 挑一个能用的 python
if   [ -x ".venv/Scripts/python.exe" ]; then PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ];        then PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python  >/dev/null 2>&1; then PY=python
else echo "[x] 找不到 python，请先建好 .venv" >&2; exit 1; fi

exec "$PY" -m scripts.pack_server "$@"
