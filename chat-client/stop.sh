#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT_DIR/.runtime/vite.pid"

process_is_running() {
  local pid="$1"
  local state

  state="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]')"
  [[ -n "$state" && "$state" != Z* ]]
}

if [[ ! -f "$PID_FILE" ]]; then
  echo "未找到前端 PID 文件，前端可能未由 ./start.sh 启动。"
  exit 0
fi

VITE_PID="$(tr -d '[:space:]' <"$PID_FILE")"
if [[ -z "$VITE_PID" || ! "$VITE_PID" =~ ^[0-9]+$ ]]; then
  echo "PID 文件内容无效：$PID_FILE" >&2
  exit 1
fi

if ! process_is_running "$VITE_PID"; then
  echo "前端进程 $VITE_PID 已退出。"
  rm -f "$PID_FILE"
  exit 0
fi

kill -TERM "$VITE_PID"
for _ in {1..20}; do
  if ! process_is_running "$VITE_PID"; then
    rm -f "$PID_FILE"
    echo "Chat Client 已停止。"
    exit 0
  fi
  sleep 0.25
done

echo "前端进程 $VITE_PID 未在 5 秒内退出，正在强制停止。" >&2
kill -KILL "$VITE_PID" 2>/dev/null || true
for _ in {1..8}; do
  if ! process_is_running "$VITE_PID"; then
    rm -f "$PID_FILE"
    echo "Chat Client 已强制停止。"
    exit 0
  fi
  sleep 0.25
done

echo "前端进程 $VITE_PID 无法停止。" >&2
exit 1
