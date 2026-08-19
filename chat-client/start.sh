#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
ENV_FILE="$ROOT_DIR/.frontend.env"
VITE_BIN="$ROOT_DIR/node_modules/.bin/vite"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

VITE_HOST="${VITE_HOST:-0.0.0.0}"
VITE_PORT="${VITE_PORT:-3000}"
CHAT_SERVER_URL="${CHAT_SERVER_URL:-http://127.0.0.1:9012}"
AGENT_SERVER_URL="${AGENT_SERVER_URL:-http://127.0.0.1:9011}"
PID_FILE="$RUNTIME_DIR/vite.pid"
LOG_FILE="$RUNTIME_DIR/vite.log"

port_in_use() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn "sport = :$1" | grep -q LISTEN
  else
    python3 - "$1" <<'PY'
import socket
import sys

with socket.socket() as sock:
    sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
  fi
}

wait_for_http() {
  for _ in {1..30}; do
    if curl --silent --max-time 1 --output /dev/null "http://127.0.0.1:$VITE_PORT/"; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

managed_client_is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid state command
  pid="$(tr -d '[:space:]' <"$PID_FILE")"
  if [[ -z "$pid" || ! "$pid" =~ ^[0-9]+$ ]]; then
    rm -f "$PID_FILE"
    return 1
  fi
  state="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]')"
  if [[ -z "$state" || "$state" == Z* ]]; then
    rm -f "$PID_FILE"
    return 1
  fi
  command="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  if [[ "$command" != *"$VITE_BIN"* ]]; then
    echo "PID 文件 $PID_FILE 指向非预期进程 $pid；请先人工确认后再处理。" >&2
    return 2
  fi
  return 0
}

if [[ ! -x "$VITE_BIN" ]]; then
  echo "未找到 Vite。请先执行：npm install" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "缺少 curl，无法检查前端启动状态。" >&2
  exit 1
fi
if managed_client_is_running; then
  if wait_for_http; then
    echo "Chat Client 已在运行：PID $(<"$PID_FILE")，http://$VITE_HOST:$VITE_PORT"
    exit 0
  fi
  echo "检测到已记录的前端进程但健康检查失败；请先执行 ./stop.sh。" >&2
  exit 1
else
  client_state=$?
  if [[ "$client_state" -eq 2 ]]; then
    exit 1
  fi
fi
if port_in_use "$VITE_PORT"; then
  echo "端口 $VITE_PORT 已被占用；若前端已运行，请先执行 ./stop.sh。" >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR"
(
  cd "$ROOT_DIR"
  export CHAT_SERVER_URL AGENT_SERVER_URL
  if command -v setsid >/dev/null 2>&1; then
    exec setsid "$VITE_BIN" --host "$VITE_HOST" --port "$VITE_PORT" </dev/null
  else
    exec nohup "$VITE_BIN" --host "$VITE_HOST" --port "$VITE_PORT" </dev/null
  fi
) >"$LOG_FILE" 2>&1 &

VITE_PID=$!
echo "$VITE_PID" >"$PID_FILE"

if ! wait_for_http; then
  echo "前端启动失败。请查看 $LOG_FILE" >&2
  "$ROOT_DIR/stop.sh" || true
  exit 1
fi

echo "Chat Client 已后台启动：PID $VITE_PID，http://$VITE_HOST:$VITE_PORT"
echo "聊天服务代理：$CHAT_SERVER_URL"
echo "Agent 服务代理：$AGENT_SERVER_URL"
echo "日志文件：$LOG_FILE"
