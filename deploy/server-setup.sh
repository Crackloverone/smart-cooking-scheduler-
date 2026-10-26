#!/usr/bin/env bash
# =============================================================================
#  云服务器一键部署（在服务器上以 root 执行）
#
#  前提：代码已经解包到 /opt/cooking-agent（用本机的 deploy/upload.sh 上传）。
#
#  用法：
#      sudo PORT=8000 WITH_NGINX=1 bash /opt/cooking-agent/deploy/server-setup.sh
#
#  环境变量：
#      PORT        监听端口，默认 8000
#      WITH_NGINX  1 = 顺带装 nginx 并把 80 反代到应用（链接里就不用带端口）
#                  0/不设 = 只起应用，直接访问 http://<IP>:<PORT>/app
#      WITH_LLM    1/不设 = **开启**模型（默认）。出厂层 app/agent/llm_defaults.json
#                           自带接入配置与凭据，服务器解包即可用上 AI 对话 / 通知
#                           润色 / 重排姿态仲裁。
#                  0 = 关闭模型（离线档）。排程质量与耗时**不变** —— 语义先验走
#                      data/llm_agent_cache.json 离线缓存，只是没有在线 AI 行为。
#                      服务器访问不到模型出口、或不希望外发请求时用它。
#      USER_NAME   跑应用的系统用户，默认 cooking
#
#  做的事（全部幂等，可以反复执行）：
#      1) apt 装 python3 / venv / pip
#      2) 建系统用户 + 建虚拟环境 + 装依赖（含 ortools，约 40MB，慢是正常的）
#      3) 注册 systemd 服务并启动（开机自启 + 崩溃自拉）
#      4) 可选：装 nginx 反代
#      5) 自检 /health /ready /api/plan
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cooking-agent}"
PORT="${PORT:-8000}"
WITH_NGINX="${WITH_NGINX:-0}"
WITH_LLM="${WITH_LLM:-1}"
USER_NAME="${USER_NAME:-cooking}"
SVC=cooking-agent

log()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

# 以应用用户身份执行。
# 优先 runuser（util-linux 自带，最小化镜像也有）；退回 sudo —— 有些精简系统
# 根本没装 sudo，脚本会在"装依赖"这一步莫名其妙地失败。
as_user() {
  if command -v runuser >/dev/null 2>&1; then runuser -u "$USER_NAME" -- "$@"
  elif command -v sudo   >/dev/null 2>&1; then sudo -u "$USER_NAME" -- "$@"
  else die "既没有 runuser 也没有 sudo，无法以 $USER_NAME 身份执行"
  fi
}

[ "$(id -u)" -eq 0 ] || die "请用 root 执行：sudo bash $0"
[ -d "$APP_DIR/app" ] || die "找不到 $APP_DIR/app —— 先把代码传到 $APP_DIR"
[ -f "$APP_DIR/requirements.txt" ] || die "找不到 $APP_DIR/requirements.txt"

# ---------------------------------------------------------------- 1. 系统依赖
log "安装系统依赖"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip curl ca-certificates >/dev/null
else
  warn "没识别出 apt/dnf，请自行确认 python3 与 venv 已安装"
fi
python3 -V

# ------------------------------------------------------- 2. 用户 + 虚拟环境
log "准备运行用户 $USER_NAME"
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$USER_NAME"
fi
# data/ 需要可写：档案删除（P-03）会写 data/profiles_user.json
mkdir -p "$APP_DIR/data"
chown -R "$USER_NAME":"$USER_NAME" "$APP_DIR"

log "安装 Python 依赖（ortools 约 40MB，第一次会比较久）"
[ -d "$APP_DIR/.venv" ] || as_user python3 -m venv "$APP_DIR/.venv"
as_user "$APP_DIR/.venv/bin/python" -m pip install -q --upgrade pip
as_user "$APP_DIR/.venv/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"

# 依赖真的能 import 才算装好 —— pip 退出码 0 不代表装成功
as_user "$APP_DIR/.venv/bin/python" - <<'PY' || die "依赖导入失败，请看上面的报错"
import fastapi, uvicorn, pydantic, ortools
print("fastapi/uvicorn/pydantic/ortools 导入正常")
PY

# ---------------------------------------------------------------- 3. systemd
log "注册 systemd 服务 $SVC"
install -m 0644 "$APP_DIR/deploy/cooking-agent.service" "/etc/systemd/system/$SVC.service"
# 让 unit 里的 PORT 跟着本次传进来的值走
sed -i "s|^Environment=PORT=.*|Environment=PORT=$PORT|" "/etc/systemd/system/$SVC.service"

# 模型开关跟着 WITH_LLM 走。
# ⚠ 这一步是"开箱可用"的落点：出厂层 app/agent/llm_defaults.json 里带着接入配置
#   与凭据，但**只有这四个开关是 on 时它才会被用上**。写死 off 的话，界面显示
#   「模型已关闭」、所有 AI 行为静默消失，而排程与耗时完全正常 —— 没有任何报错
#   能指向"其实只是开关关着"，排查起来最容易绕远。
if [ "$WITH_LLM" = "0" ]; then LLM_VAL=off; else LLM_VAL=on; fi
for k in FT_LLM FT_AGENT_LLM FT_WARM_LLM FT_NOTIFY_LLM; do
  sed -i "s|^Environment=$k=.*|Environment=$k=$LLM_VAL|" "/etc/systemd/system/$SVC.service"
done
# 改完必须复核：sed 没匹配上是**静默**的（模式写错就什么都不改），
# 光看退出码分不出"改好了"和"压根没找到那一行"。
grep -q "^Environment=FT_LLM=$LLM_VAL\$" "/etc/systemd/system/$SVC.service" \
  || die "systemd 里的 FT_LLM 没改成 $LLM_VAL，请检查 deploy/cooking-agent.service"
if [ "$LLM_VAL" = "on" ]; then
  log "模型开关 = 开（出厂层凭据生效；想关掉：WITH_LLM=0 重跑本脚本）"
else
  warn "模型开关 = 关（离线档）。排程质量不变，但没有在线 AI 行为。"
fi
systemctl daemon-reload
systemctl enable "$SVC" >/dev/null
systemctl restart "$SVC"
sleep 4

systemctl is-active --quiet "$SVC" || {
  warn "服务没起来，最近日志："
  journalctl -u "$SVC" -n 40 --no-pager || true
  die "启动失败"
}

# ---------------------------------------------------------------- 4. nginx
if [ "$WITH_NGINX" = "1" ]; then
  log "安装并配置 nginx（把 80 反代到 127.0.0.1:$PORT）"
  command -v nginx >/dev/null 2>&1 || {
    apt-get install -y -qq nginx >/dev/null 2>&1 || dnf install -y -q nginx >/dev/null
  }
  install -m 0644 "$APP_DIR/deploy/nginx-cooking-agent.conf" /etc/nginx/sites-available/cooking-agent
  # 反代目标端口必须跟着本次 PORT 走：配置文件里写死的是 8000，
  # 若这里不改，PORT=8765 时 nginx 会去连 8000 ⇒ 502 Bad Gateway。
  sed -i "s|proxy_pass         http://127.0.0.1:[0-9]*;|proxy_pass         http://127.0.0.1:$PORT;|" \
      /etc/nginx/sites-available/cooking-agent
  grep -q "127.0.0.1:$PORT;" /etc/nginx/sites-available/cooking-agent \
      || die "nginx 配置里的反代端口没改成 $PORT，请检查 deploy/nginx-cooking-agent.conf"
  ln -sf /etc/nginx/sites-available/cooking-agent /etc/nginx/sites-enabled/cooking-agent
  # 不删默认站点会出现 nginx 欢迎页，看起来像"部署没生效"
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
fi

# ---------------------------------------------------------------- 5. 自检
log "自检"
fail=0
for path in /health /ready /app; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://127.0.0.1:$PORT$path" || echo 000)
  printf '  %-10s %s\n' "$path" "$code"
  [ "$code" = "200" ] || fail=1
done
echo -n "  /ready   "; curl -s --max-time 15 "http://127.0.0.1:$PORT/ready"; echo

# 官方评分接口冒烟：这一步过了才算真的能用。
# ⚠ 入参是**裸数组** [{id,name}]（见 app/api/main.py 的 PlanItem），
#   写成 {"recipes":[...]} 会 422 —— 而 422 也是"请求成功发出"，只看 curl 退出码
#   会误判成通过，所以这里必须断言 HTTP 200。
echo -n "  POST /api/plan -> "
plan_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
  -X POST "http://127.0.0.1:$PORT/api/plan" \
  -H 'Content-Type: application/json' \
  -d '[{"id":1,"name":"鸡翅包饭"},{"id":2,"name":"苹果芒果派"}]' || echo 000)
echo "$plan_code"
[ "$plan_code" = "200" ] || fail=1

# 模型配置链路自检 —— 这一步决定"AI 到底会不会动"。
# ⚠ 光看 /health 和 /api/plan 全绿是**不够**的：排程走离线先验缓存，
#   即使模型整个关掉，这两个接口也照样 200 且指标一模一样。
#   所以必须单独断言"出厂层的接入配置与凭据真的被读进来了"。
USE_PY="$APP_DIR/.venv/bin/python"
[ -x "$USE_PY" ] || USE_PY="$(command -v python3 || command -v python || echo python3)"
jget() {  # jget <端点> <字段>  —— 取不到就输出 unknown
  "$USE_PY" - "$1" "$2" "$3" <<'PY' 2>/dev/null || echo unknown
import json, sys, urllib.request
try:
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:%s%s" % (sys.argv[1], sys.argv[2]),
                                          timeout=15).read().decode())
    print(str(d.get(sys.argv[3], "unknown")).lower())
except Exception:
    print("unknown")
PY
}
llm_enabled=$(jget "$PORT" /api/llm_status enabled)
llm_state=$(jget "$PORT" /api/llm_status state)
key_set=$(jget "$PORT" /api/model_settings api_key_set)
provider=$(jget "$PORT" /api/model_settings provider)
printf '  %-14s enabled=%s state=%s provider=%s api_key_set=%s\n' \
       "模型链路" "$llm_enabled" "$llm_state" "$provider" "$key_set"

if [ "$WITH_LLM" = "0" ]; then
  [ "$llm_enabled" = "false" ] || warn "WITH_LLM=0 但模型仍是 enabled=$llm_enabled（预期 false）"
else
  # enabled=true 只说明"开关开 + 凭据齐"；state=breaker 说明开关和凭据都对，
  # 只是这台机器访问不到那个出口 —— 排程不受影响，不该因此判部署失败。
  if [ "$llm_enabled" != "true" ]; then
    warn "模型未启用（enabled=$llm_enabled）。最常见两个原因："
    warn "  1) systemd 里 FT_LLM 被改成 off —— 检查 /etc/systemd/system/$SVC.service"
    warn "  2) 出厂层 app/agent/llm_defaults.json 没随包上传 —— 检查 $APP_DIR/app/agent/"
    fail=1
  elif [ "$llm_state" = "breaker" ]; then
    warn "模型已启用但处于熔断（服务器访问不到模型出口）。排程与耗时不受影响，"
    warn "     只是没有在线 AI 行为。要确认能通：curl -v $APP_DIR 所在机器的出口地址。"
  fi
fi

PUBIP=$(curl -s --max-time 5 https://api.ipify.org || echo "<公网IP>")
echo
if [ "$fail" -eq 0 ]; then
  log "部署完成"
  echo "    本机验证： http://127.0.0.1:$PORT/app"
  if [ "$WITH_NGINX" = "1" ]; then
    echo "    对外链接： http://$PUBIP/app"
  else
    echo "    对外链接： http://$PUBIP:$PORT/app"
    echo
    warn "别忘了在云厂商控制台的【安全组/防火墙】放行 TCP $PORT —— 不放行外部打不开，"
    warn "本机 curl 却一切正常，最容易卡在这里。"
  fi
  echo
  echo "    改代码后重新部署：在本机跑 deploy/upload.sh，再执行本脚本（幂等）。"
else
  die "自检有失败项，请看上面的状态码"
fi
