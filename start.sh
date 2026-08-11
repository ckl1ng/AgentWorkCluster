#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT_DIR/chat-server"
CLIENT_DIR="$ROOT_DIR/chat-client"
RUNTIME_DIR="$ROOT_DIR/.runtime"

mkdir -p "$RUNTIME_DIR"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$RUNTIME_DIR/lifecycle.lock"
  if ! flock -n 9; then
    echo "启动或关闭操作正在执行，请稍后重试。" >&2
    exit 1
  fi
fi

tail_logs() {
  local component="$1"
  shift
  local log
  for log in "$@"; do
    if [[ -f "$log" ]]; then
      echo "--- $component 日志: $log (最后 30 行) ---" >&2
      tail -n 30 "$log" >&2 || true
    fi
  done
}

report_failure() {
  local component="$1"
  local code="$2"
  echo >&2
  echo "[$(date '+%F %T')] $component 启动失败（退出码 $code）。" >&2
  case "$component" in
    "Chat Server")
      tail_logs "$component" "$SERVER_DIR/.runtime/chat-server.log" "$SERVER_DIR/.runtime/agent-api.log" "$SERVER_DIR/.runtime/agent-worker.log"
      echo "建议：检查 chat-server/.agent.env 中的 AGENT_SERVICE_SECRET、AGENT_MASTER_KEY；确认已安装 Rust/Python 依赖；必要时运行：" >&2
      echo "  cd $SERVER_DIR && cargo build --release" >&2
      echo "  python3 -m pip install -r $ROOT_DIR/agent-service/requirements.txt" >&2
      ;;
    "Chat Client")
      tail_logs "$component" "$CLIENT_DIR/.runtime/vite.log"
      echo "建议：确认 Node.js 18+、npm 依赖和 ${VITE_PORT:-3000} 端口；必要时运行：" >&2
      echo "  cd $CLIENT_DIR && npm ci" >&2
      echo "  ss -ltnp | rg ':${VITE_PORT:-3000}'" >&2
      echo "  检查 chat-client/.frontend.env 中的 CHAT_SERVER_URL、AGENT_SERVER_URL。" >&2
      ;;
  esac
}

run_component() {
  local component="$1"
  local script="$2"
  echo "[$(date '+%F %T')] 正在启动 $component ..."
  if [[ ! -x "$script" ]]; then
    echo "$component 脚本不存在或不可执行：$script" >&2
    report_failure "$component" 127
    return 127
  fi
  if "$script"; then
    echo "[$(date '+%F %T')] $component 启动成功。"
    return 0
  else
    local code=$?
    report_failure "$component" "$code"
    return "$code"
  fi
}

if ! run_component "Chat Server" "$SERVER_DIR/start.sh"; then
  exit 1
fi

# Do not let the long-lived Vite process inherit the orchestration lock.
exec 9>&-

if ! run_component "Chat Client" "$CLIENT_DIR/start.sh"; then
  echo "客户端失败，正在回收已启动的服务端，避免留下不完整进程。" >&2
  "$SERVER_DIR/stop.sh" || true
  exit 1
fi

echo
echo "全部组件已启动。"
echo "前端: http://127.0.0.1:${VITE_PORT:-3000}"
echo "服务端日志: $SERVER_DIR/.runtime/"
echo "客户端日志: $CLIENT_DIR/.runtime/vite.log"
