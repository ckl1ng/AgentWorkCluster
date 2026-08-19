#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_SERVICE_DIR="${AGENT_SERVICE_DIR:-$ROOT_DIR/../agent-service}"
RUNTIME_DIR="$ROOT_DIR/.runtime"
ENV_FILE="$ROOT_DIR/.agent.env"
SERVER_BIN="${CHAT_SERVER_BIN:-$ROOT_DIR/chat-server}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CHAT_SERVER_HOST="${CHAT_SERVER_HOST:-127.0.0.1}"
CHAT_SERVER_PORT="${CHAT_SERVER_PORT:-9012}"
AGENT_API_HOST="${AGENT_API_HOST:-127.0.0.1}"
AGENT_API_PORT="${AGENT_API_PORT:-9011}"

mkdir -p "$RUNTIME_DIR"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$RUNTIME_DIR/lifecycle.lock"
  if ! flock -n 9; then
    echo "另一个启动或停止操作正在执行，请稍后重试。" >&2
    exit 1
  fi
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

require_value() {
  local name="$1"
  if [[ -z "${!name:-}" || "${!name}" == replace-with-* ]]; then
    echo "缺少 $name。请复制 .agent.env.example 为 .agent.env 并填写真实值。" >&2
    exit 1
  fi
}

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn "sport = :$port" | grep -q LISTEN
  else
    "$PYTHON_BIN" - "$port" <<'PY'
import socket
import sys

with socket.socket() as sock:
    sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
  fi
}

managed_service_is_running() {
  local name="$1"
  local expected="$2"
  local pid_file="$RUNTIME_DIR/$name.pid"
  local pid state command

  [[ -f "$pid_file" ]] || return 1
  pid="$(<"$pid_file")"
  if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
    rm -f "$pid_file"
    return 1
  fi

  state="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]')"
  if [[ -z "$state" || "$state" == Z* ]]; then
    rm -f "$pid_file"
    return 1
  fi

  command="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  if [[ "$command" != *"$expected"* ]]; then
    echo "PID 文件 $pid_file 指向非预期进程 $pid；请先人工确认后再处理。" >&2
    return 2
  fi

  return 0
}

service_is_healthy() {
  local url="$1"
  curl --silent --max-time 1 --output /dev/null "$url"
}

if [[ ! -x "$SERVER_BIN" ]]; then
  echo "未找到可执行后端：$SERVER_BIN" >&2
  echo "请先执行：export PATH=\"\$HOME/.cargo/bin:\$PATH\" && cargo build --release --target x86_64-unknown-linux-musl" >&2
  exit 1
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1 || ! "$PYTHON_BIN" -c 'import fastapi, uvicorn, httpx, cryptography, jsonschema, redis' >/dev/null 2>&1; then
  echo "Agent 依赖未安装。请执行：python3 -m pip install -r $AGENT_SERVICE_DIR/requirements.txt" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "缺少 curl，无法检查服务启动状态。" >&2
  exit 1
fi

chat_state=1
agent_state=1
if managed_service_is_running "chat-server" "chat-server"; then
  chat_state=0
else
  chat_state=$?
fi
if managed_service_is_running "agent-api" "uvicorn app.main:app"; then
  agent_state=0
else
  agent_state=$?
fi
if [[ "$chat_state" -eq 2 || "$agent_state" -eq 2 ]]; then
  exit 1
fi
if [[ "$chat_state" -eq 0 && "$agent_state" -eq 0 ]]; then
  if service_is_healthy "http://127.0.0.1:$CHAT_SERVER_PORT/healthz" && service_is_healthy "http://127.0.0.1:$AGENT_API_PORT/healthz"; then
    echo "Chat Server 和 Agent API 已在运行。"
    exit 0
  fi
  echo "检测到已记录的服务进程但健康检查失败；请先执行 ./stop.sh。" >&2
  exit 1
fi
if [[ "$chat_state" -eq 0 || "$agent_state" -eq 0 ]]; then
  echo "检测到不完整的已启动服务；请先执行 ./stop.sh。" >&2
  exit 1
fi

require_value AGENT_SERVICE_SECRET
require_value AGENT_MASTER_KEY

if [[ -n "${REDIS_URL:-}" && -z "${AGENT_DATABASE_URL:-}" && -z "${PGHOST:-}" ]]; then
  echo "配置 REDIS_URL 时必须同时配置 AGENT_DATABASE_URL 或 PGHOST，生产 Worker 不支持 SQLite。" >&2
  exit 1
fi
if [[ -n "${REDIS_URL:-}" && "${AGENT_DATABASE_URL:-}" == *replace-with-* ]]; then
  echo "AGENT_DATABASE_URL 仍是示例占位值；请在 .agent.env 中填写真实 PostgreSQL 地址。" >&2
  exit 1
fi

if port_in_use "$CHAT_SERVER_PORT"; then
  echo "端口 $CHAT_SERVER_PORT 已被占用；若服务已运行，请先执行 ./stop.sh。" >&2
  exit 1
fi
if port_in_use "$AGENT_API_PORT"; then
  echo "端口 $AGENT_API_PORT 已被占用；若服务已运行，请先执行 ./stop.sh。" >&2
  exit 1
fi
if [[ -n "${REDIS_URL:-}" ]] && ! "$PYTHON_BIN" -m alembic --version >/dev/null 2>&1; then
  echo "缺少 Alembic。请使用生产虚拟环境安装 $AGENT_SERVICE_DIR/requirements.txt。" >&2
  exit 1
fi

mkdir -p "$ROOT_DIR/data"

if [[ -n "${REDIS_URL:-}" ]]; then
  (
    cd "$AGENT_SERVICE_DIR"
    export AGENT_DATABASE_URL PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD
    "$PYTHON_BIN" -m alembic upgrade head
  )
fi

(
  exec 9>&-
  export HOST="$CHAT_SERVER_HOST"
  export PORT="$CHAT_SERVER_PORT"
  export DATA_DIR="${DATA_DIR:-$ROOT_DIR/data}"
  export AGENT_SERVICE_SECRET
  if command -v setsid >/dev/null 2>&1; then
    exec setsid "$SERVER_BIN" </dev/null
  else
    exec nohup "$SERVER_BIN" </dev/null
  fi
) >"$RUNTIME_DIR/chat-server.log" 2>&1 &
CHAT_PID=$!
echo "$CHAT_PID" >"$RUNTIME_DIR/chat-server.pid"

(
  exec 9>&-
  cd "$AGENT_SERVICE_DIR"
  export AGENT_SERVICE_SECRET AGENT_MASTER_KEY AMAP_WEATHER_API_KEY AGENT_DATABASE_URL REDIS_URL PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD
  export AGENT_DATABASE_PATH="${AGENT_DATABASE_PATH:-$ROOT_DIR/data/agents.db}"
  export CHAT_AUTH_INTROSPECTION_URL="${CHAT_AUTH_INTROSPECTION_URL:-http://127.0.0.1:$CHAT_SERVER_PORT/internal/v1/auth/introspect}"
  export AGENT_ALLOW_HTTP="${AGENT_ALLOW_HTTP:-false}"
  if command -v setsid >/dev/null 2>&1; then
    exec setsid "$PYTHON_BIN" -m uvicorn app.main:app --app-dir "$AGENT_SERVICE_DIR" --host "$AGENT_API_HOST" --port "$AGENT_API_PORT" </dev/null
  else
    exec nohup "$PYTHON_BIN" -m uvicorn app.main:app --app-dir "$AGENT_SERVICE_DIR" --host "$AGENT_API_HOST" --port "$AGENT_API_PORT" </dev/null
  fi
) >"$RUNTIME_DIR/agent-api.log" 2>&1 &
AGENT_PID=$!
echo "$AGENT_PID" >"$RUNTIME_DIR/agent-api.pid"

WORKER_PID=""
if [[ -n "${REDIS_URL:-}" ]]; then
  (
    exec 9>&-
    cd "$AGENT_SERVICE_DIR"
    export AGENT_SERVICE_SECRET AGENT_MASTER_KEY AMAP_WEATHER_API_KEY AGENT_DATABASE_URL REDIS_URL PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD
    export CHAT_AUTH_INTROSPECTION_URL="${CHAT_AUTH_INTROSPECTION_URL:-http://127.0.0.1:$CHAT_SERVER_PORT/internal/v1/auth/introspect}"
    if command -v setsid >/dev/null 2>&1; then
      exec setsid "$PYTHON_BIN" -m app.worker </dev/null
    else
      exec nohup "$PYTHON_BIN" -m app.worker </dev/null
    fi
  ) >"$RUNTIME_DIR/agent-worker.log" 2>&1 &
  WORKER_PID=$!
  echo "$WORKER_PID" >"$RUNTIME_DIR/agent-worker.pid"
fi

wait_for_http() {
  local url="$1"
  for _ in {1..30}; do
    if curl --silent --max-time 1 --output /dev/null "$url"; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

if ! wait_for_http "http://127.0.0.1:$CHAT_SERVER_PORT/healthz" || ! wait_for_http "http://127.0.0.1:$AGENT_API_PORT/healthz"; then
  echo "启动失败。请查看 $RUNTIME_DIR/chat-server.log、$RUNTIME_DIR/agent-api.log 和 $RUNTIME_DIR/agent-worker.log" >&2
  "$ROOT_DIR/stop.sh" || true
  exit 1
fi

echo "Chat Server 已后台启动：PID $CHAT_PID，http://$CHAT_SERVER_HOST:$CHAT_SERVER_PORT"
echo "Agent API 已后台启动：PID $AGENT_PID，http://$AGENT_API_HOST:$AGENT_API_PORT"
if [[ -n "$WORKER_PID" ]]; then
  echo "Agent Worker 已后台启动：PID $WORKER_PID，Redis $REDIS_URL"
fi
echo "日志目录：$RUNTIME_DIR"
