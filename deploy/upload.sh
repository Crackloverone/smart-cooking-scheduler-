#!/usr/bin/env bash
# =============================================================================
#  一键上传 + 部署到云服务器（在本机项目根目录执行）
#
#  用法：
#      HOST=112.124.50.217 bash deploy/upload.sh
#      HOST=1.2.3.4 PORT=8765 WITH_NGINX=1 bash deploy/upload.sh
#      HOST=1.2.3.4 SSH_USER=ubuntu bash deploy/upload.sh
#
#  环境变量：
#      HOST       必填，服务器公网 IP 或域名
#      SSH_USER   SSH 用户，默认 root
#      SSH_PORT   SSH 端口，默认 22
#      PORT       应用端口，默认 8000
#      WITH_NGINX 1 = 顺带装 nginx 把 80 反代过去（链接不用带端口）
#      SKIP_SETUP 1 = 只上传，不在服务器上执行安装
#
#  为什么要有这个脚本
#  ---------------------------------------------------------------------------
#  「上传了旧代码」是本项目真实踩过的坑：线上发布目录 publish/ 是**拷贝**，
#  改了源码不同步过去，线上就还是旧版本，而本地完全看不出来。
#  所以这里第一步**强制**跑 sync_publish，同步不通过就直接中止。
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${HOST:-}"
SSH_USER="${SSH_USER:-root}"
SSH_PORT="${SSH_PORT:-22}"
PORT="${PORT:-8000}"
WITH_NGINX="${WITH_NGINX:-0}"
SKIP_SETUP="${SKIP_SETUP:-0}"
REMOTE_DIR="${REMOTE_DIR:-/opt/cooking-agent}"

log()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[ -n "$HOST" ] || die "必须给 HOST。例如：HOST=1.2.3.4 bash deploy/upload.sh"
[ -f "requirements.txt" ] || die "请在项目根目录执行（找不到 requirements.txt）"

# ------------------------------------------------------- 挑一个能用的 python
if   [ -x ".venv/Scripts/python.exe" ]; then PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ];        then PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python  >/dev/null 2>&1; then PY=python
else die "找不到 python，请先建好 .venv"; fi

# ------------------------------------- 1+2. 同步 publish/ + 打包（单一真源）
# ⚠ 打包逻辑在 `scripts/pack_server.py`，本脚本**不自己打**。
#   原来内联的那段有两个真问题：
#     1) `cp deploy/* "$STAGE/deploy/"` —— deploy/ 下有 windows/ 子目录，cp 没有 -r
#        会报 "omitting directory" 且退出码非 0，在 set -e 下**整个上传必然中止**；
#     2) 打出来的包没有任何校验 —— "出厂层的 key 有没有进去"只能靠人肉看，
#        而它没进去时界面完全正常（排程照跑，只是 AI 不动），没有任何报错。
#   现在这两件事都归 pack_server.py：它 fail-closed，任一条断言不过就不产出包。
log "同步 publish/ 并打包（--only tar）"
"$PY" -m scripts.pack_server --only tar || die "打包失败（看上面的校验错误）"

TGZ="dist/cooking-agent-server.tar.gz"
[ -s "$TGZ" ] || die "没找到打好的包：$TGZ"
log "包大小：$(du -h "$TGZ" | cut -f1)"

# ---------------------------------------------------------------- 3. 上 传
SSH_OPTS=(-p "$SSH_PORT" -o StrictHostKeyChecking=accept-new)
DEST="$SSH_USER@$HOST"

log "上传到 $DEST:$REMOTE_DIR"
ssh "${SSH_OPTS[@]}" "$DEST" "mkdir -p '$REMOTE_DIR'"
scp -P "$SSH_PORT" -o StrictHostKeyChecking=accept-new "$TGZ" "$DEST:/tmp/cooking-agent.tgz"
ssh "${SSH_OPTS[@]}" "$DEST" "tar xzf /tmp/cooking-agent.tgz -C '$REMOTE_DIR' && rm -f /tmp/cooking-agent.tgz"
log "上传完成"

# ---------------------------------------------------------------- 4. 部 署
if [ "$SKIP_SETUP" = "1" ]; then
  log "SKIP_SETUP=1，跳过服务器端安装。需要时手动执行："
  echo "    ssh $DEST 'sudo PORT=$PORT WITH_NGINX=$WITH_NGINX bash $REMOTE_DIR/deploy/server-setup.sh'"
  exit 0
fi

log "在服务器上执行部署脚本（装依赖 / systemd / 自检）"
ssh "${SSH_OPTS[@]}" "$DEST" \
  "cd '$REMOTE_DIR' && PORT=$PORT WITH_NGINX=$WITH_NGINX bash deploy/server-setup.sh"

echo
log "完成"
if [ "$WITH_NGINX" = "1" ]; then
  echo "    对外链接： http://$HOST/app"
else
  echo "    对外链接： http://$HOST:$PORT/app"
  echo
  echo "    [重要] 若外部打不开、但服务器上 curl 正常，几乎一定是"
  echo "           云厂商控制台的【安全组/防火墙】没放行 TCP $PORT。"
fi
