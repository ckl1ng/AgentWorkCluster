#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT_DIR/.runtime/qq-gateway.pid"

process_is_running() {
  local pid="$1"
  local state
  state="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]')"
  [[ -n "$state" && "$state" != Z* ]]
}

if [[ ! -f "$PID_FILE" ]]; then
  echo "QQ Gateway 未由 start.sh 启动。"
  exit 0
fi
PID="$(<"$PID_FILE")"
if ! [[ "$PID" =~ ^[0-9]+$ ]]; then
  rm -f "$PID_FILE"
  echo "QQ Gateway PID 文件无效，已清理。"
  exit 0
fi
if process_is_running "$PID"; then
  COMMAND="$(ps -p "$PID" -o args= 2>/dev/null || true)"
  if [[ "$COMMAND" != *"uvicorn app.main:app"* || "$COMMAND" != *"$ROOT_DIR"* ]]; then
    echo "拒绝停止 PID $PID：它不是当前 QQ Gateway 进程。请检查 $PID_FILE。" >&2
    exit 1
  fi
  kill -TERM "$PID"
  for _ in {1..20}; do
    if ! process_is_running "$PID"; then
      rm -f "$PID_FILE"
      echo "QQ Gateway 已停止。"
      exit 0
    fi
    sleep 0.25
  done
  echo "QQ Gateway 未在 5 秒内退出，正在强制停止。" >&2
  kill -KILL "$PID" 2>/dev/null || true
  for _ in {1..8}; do
    if ! process_is_running "$PID"; then
      rm -f "$PID_FILE"
      echo "QQ Gateway 已强制停止。"
      exit 0
    fi
    sleep 0.25
  done
  echo "QQ Gateway 无法停止；PID $PID 仍在运行。" >&2
  exit 1
fi
rm -f "$PID_FILE"
echo "QQ Gateway 已停止。"
