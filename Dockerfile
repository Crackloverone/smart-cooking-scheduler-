FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# 系统依赖: curl 供 HEALTHCHECK 使用
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY data ./data
COPY scripts ./scripts

# 默认关闭在线模型调用: 服务器/评审环境常常没有外网出口或额度不可用,
# 排程仍会走 Agent 闭环(策略择优 + 离线语义先验缓存), 零网络依赖。
#
# ⚠ 本地 Ollama 档在**容器里默认不可用**, 原因有两个, 都得自己解:
#   ① 容器内的 127.0.0.1 指向容器自身, 不是宿主机 —— 要用宿主机的 Ollama
#      得把端点换成 host.docker.internal:11434 (Linux 还需
#      `--add-host=host.docker.internal:host-gateway`), 并让 Ollama 监听
#      0.0.0.0 (设 OLLAMA_HOST=0.0.0.0:11434 后重启)。
#   ② 就算端点通了, 镜像里也没有模型权重, 模型必须在宿主机上先 pull 好。
# 所以容器默认离线不是"忘了配", 是刻意的: 要么挂宿主机 Ollama, 要么用远端
# OpenAI 兼容端点(FT_LLM_PROVIDER=openai + 注入 FT_LLM_API_KEY), 要么就离线。
ENV FT_LLM=off FT_AGENT_LLM=off PYTHONPATH=/app

# 监听端口: 云平台(如一键发布/容器服务)会注入 PORT 环境变量, 这里必须跟随,
# 否则容器里监听的端口和对外暴露的端口对不上, 访问直接超时。
ENV PORT=8000

# 非 root 运行; data/ 需要可写 (模型产物缓存会落在这里)
RUN useradd -m -u 10001 appuser && mkdir -p /app/data && chown -R appuser /app/data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

# exec 形式: 让 uvicorn 直接成为 PID 1, 否则 docker stop 的信号会被 sh 吞掉,
# 优雅停机失效。启动阶段会做 OR-Tools 预热, 所以 start-period 给到 25s。
CMD ["sh", "-c", "exec uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
