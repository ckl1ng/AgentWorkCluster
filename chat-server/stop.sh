#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"

mkdir -p "$RUNTIME_DIR"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$RUNTIME_DIR/lifecycle.lock"
  if ! flock -n 9; then
    echo "另一个启动或停止操作正在执行，请稍后重试。" >&2
    exit 1
  fi
fi

process_is_running() {
  local pid="$1"
  local state

  state="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]')"
  # kill -0 succeeds for a zombie. It has already exited and can no longer
  # hold a port, so retaining its PID file only makes the next start fail.
  [[ -n "$state" && "$state" != Z* ]]
}

stop_service() {
  local name="$1"
  local expected="$2"
  local pid_file="$RUNTIME_DIR/$name.pid"

  if [[ ! -f "$pid_file" ]]; then
    echo "$name 未由 start.sh 启动。"
    return
  fi

  local pid
  pid="$(<"$pid_file")"
  if ! [[ "$pid" =~ ^[0-9]+$ ]] || ! process_is_running "$pid"; then
    rm -f "$pid_file"
    echo "$name 已停止。"
    return
  fi

  local command
  command="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  if [[ "$command" != *"$expected"* ]]; then
    echo "拒绝停止 PID $pid：它不是由 start.sh 记录的 $name 进程。" >&2
    return 1
  fi

  kill -TERM "$pid"
  for _ in {1..20}; do
    if ! process_is_running "$pid"; then
      rm -f "$pid_file"
      echo "$name 已停止。"
      return
    fi
    sleep 0.25
  done

  echo "$name 未在 5 秒内退出，正在强制停止；请检查 $RUNTIME_DIR/$name.log。" >&2
  kill -KILL "$pid" 2>/dev/null || true
  for _ in {1..8}; do
    if ! process_is_running "$pid"; then
      rm -f "$pid_file"
      echo "$name 已强制停止。"
      return
    fi
    sleep 0.25
  done

  echo "$name 无法停止；PID $pid 仍在运行。" >&2
  return 1
}

stop_status=0
stop_service "agent-worker" "app.worker" || stop_status=1
stop_service "agent-api" "uvicorn app.main:app" || stop_status=1
stop_service "chat-server" "chat-server" || stop_status=1

exit "$stop_status"
