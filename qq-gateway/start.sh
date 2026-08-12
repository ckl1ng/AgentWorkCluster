#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/qq-gateway.pid"
LOG_FILE="$RUNTIME_DIR/qq-gateway.log"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PORT="${QQ_GATEWAY_PORT:-9013}"

ENV_FILE="${QQ_ENV_FILE:-$ROOT_DIR/../chat-server/.agent.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
PORT="${QQ_GATEWAY_PORT:-9013}"

mkdir -p "$RUNTIME_DIR"
if [[ -f "$PID_FILE" ]]; then
  EXISTING_PID="$(<"$PID_FILE")"
  if [[ "$EXISTING_PID" =~ ^[0-9]+$ ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    EXISTING_COMMAND="$(ps -p "$EXISTING_PID" -o args= 2>/dev/null || true)"
    if [[ "$EXISTING_COMMAND" == *"uvicorn app.main:app"* && "$EXISTING_COMMAND" == *"$ROOT_DIR"* ]]; then
      echo "QQ Gateway 已运行：PID $EXISTING_PID"
      exit 0
    fi
    echo "拒绝复用 PID $EXISTING_PID：它不是当前 QQ Gateway 进程。请检查 $PID_FILE。" >&2
    exit 1
  fi
  rm -f "$PID_FILE"
fi
if ! "$PYTHON_BIN" -c 'import fastapi, httpx, cryptography, uvicorn, websockets' >/dev/null 2>&1; then
  echo "缺少 QQ Gateway 依赖，请执行：python3 -m pip install -r $ROOT_DIR/requirements.txt" >&2
  exit 1
fi
# The root lifecycle script uses fd 9 for flock. A daemon must not inherit it,
# otherwise it keeps the root start/stop lock after the script exits.
if { true >&9; } 2>/dev/null; then
  exec 9>&-
fi
if command -v setsid >/dev/null 2>&1; then
  setsid "$PYTHON_BIN" -m uvicorn app.main:app --app-dir "$ROOT_DIR" --host "${QQ_GATEWAY_HOST:-127.0.0.1}" --port "$PORT" </dev/null >"$LOG_FILE" 2>&1 &
else
  nohup "$PYTHON_BIN" -m uvicorn app.main:app --app-dir "$ROOT_DIR" --host "${QQ_GATEWAY_HOST:-127.0.0.1}" --port "$PORT" </dev/null >"$LOG_FILE" 2>&1 &
fi
PID=$!
echo "$PID" >"$PID_FILE"
for _ in {1..30}; do
  if curl --silent --max-time 1 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "QQ Gateway 已启动：PID $PID，http://${QQ_GATEWAY_HOST:-127.0.0.1}:$PORT"
    exit 0
  fi
  sleep 0.25
done
echo "QQ Gateway 启动失败，请查看 $LOG_FILE" >&2
kill -TERM "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
exit 1
