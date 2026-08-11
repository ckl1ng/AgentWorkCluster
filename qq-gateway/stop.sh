#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT_DIR/.runtime/qq-gateway.pid"
if [[ ! -f "$PID_FILE" ]]; then
  echo "QQ Gateway 未由 start.sh 启动。"
  exit 0
fi
PID="$(<"$PID_FILE")"
if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" 2>/dev/null; then
  kill -TERM "$PID"
  for _ in {1..20}; do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.25
  done
fi
rm -f "$PID_FILE"
echo "QQ Gateway 已停止。"
