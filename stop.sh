#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT_DIR/chat-server"
CLIENT_DIR="$ROOT_DIR/chat-client"
QQ_GATEWAY_DIR="$ROOT_DIR/qq-gateway"
RUNTIME_DIR="$ROOT_DIR/.runtime"

mkdir -p "$RUNTIME_DIR"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$RUNTIME_DIR/lifecycle.lock"
  if ! flock -n 9; then
    echo "启动或关闭操作正在执行，请稍后重试。" >&2
    exit 1
  fi
fi

report_failure() {
  local component="$1"
  local code="$2"
  echo "[$(date '+%F %T')] $component 关闭失败（退出码 $code）。" >&2
  echo "建议：检查对应 .runtime/*.pid 与进程状态；确认没有被 systemd 或其他脚本接管。" >&2
}

stop_component() {
  local component="$1"
  local script="$2"
  echo "[$(date '+%F %T')] 正在关闭 $component ..."
  if [[ ! -x "$script" ]]; then
    echo "$component 脚本不存在或不可执行：$script" >&2
    report_failure "$component" 127
    return 127
  fi
  if "$script"; then
    echo "[$(date '+%F %T')] $component 已关闭。"
    return 0
  else
    local code=$?
    report_failure "$component" "$code"
    return "$code"
  fi
}

stop_status=0
stop_component "Chat Client" "$CLIENT_DIR/stop.sh" || stop_status=1
if [[ -x "$QQ_GATEWAY_DIR/stop.sh" ]]; then
  stop_component "QQ Gateway" "$QQ_GATEWAY_DIR/stop.sh" || stop_status=1
fi
stop_component "Chat Server" "$SERVER_DIR/stop.sh" || stop_status=1

if [[ "$stop_status" -eq 0 ]]; then
  echo "全部组件已关闭。"
else
  echo "关闭操作未完全成功，请按上面的建议处理。" >&2
fi
exit "$stop_status"
