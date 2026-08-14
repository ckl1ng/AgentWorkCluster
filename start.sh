#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT_DIR/chat-server"
CLIENT_DIR="$ROOT_DIR/chat-client"
QQ_GATEWAY_DIR="$ROOT_DIR/qq-gateway"
RUNTIME_DIR="$ROOT_DIR/.runtime"
TOOLS_DIR="$ROOT_DIR/.tools/bin"
VENV_DIR="$ROOT_DIR/.venv"
UV_CACHE_DIR="$RUNTIME_DIR/uv-cache"
SERVER_BIN="${CHAT_SERVER_BIN:-$SERVER_DIR/chat-server}"

mkdir -p "$RUNTIME_DIR"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$RUNTIME_DIR/lifecycle.lock"
  if ! flock -n 9; then
    echo "启动或关闭操作正在执行，请稍后重试。" >&2
    exit 1
  fi
fi

ensure_command() {
  local command="$1"
  local hint="$2"
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "缺少 $command。$hint" >&2
    exit 1
  fi
}

ensure_uv() {
  if [[ ! -x "$TOOLS_DIR/uv" || ! -x "$TOOLS_DIR/uvx" ]]; then
    ensure_command curl "请安装 curl 后重试。"
    echo "[$(date '+%F %T')] 正在安装项目私有 uv ..."
    mkdir -p "$TOOLS_DIR"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$TOOLS_DIR" UV_NO_MODIFY_PATH=1 sh
  fi
  export PATH="$TOOLS_DIR:$PATH"
  export UV_CACHE_DIR
}

ensure_python_environment() {
  ensure_uv
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "[$(date '+%F %T')] 正在创建 Python 运行环境 ..."
    "$TOOLS_DIR/uv" venv --python 3.12 "$VENV_DIR"
  fi
  export PYTHON_BIN="$VENV_DIR/bin/python"
  echo "[$(date '+%F %T')] 正在同步 Agent 服务依赖 ..."
  "$TOOLS_DIR/uv" pip install --python "$PYTHON_BIN" -r "$ROOT_DIR/agent-service/requirements.txt" -r "$QQ_GATEWAY_DIR/requirements.txt"
}

ensure_web_fetch_runtime() {
  local request js_dir
  request='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent-work-cluster","version":"1.0"}}}'
  echo "[$(date '+%F %T')] 正在预热 web_fetch MCP 工具 ..."
  if command -v timeout >/dev/null 2>&1; then
    printf '%s\n' "$request" | timeout 90s "$TOOLS_DIR/uvx" --from 'mcp-server-fetch==2026.7.10' --with 'mcp<2' mcp-server-fetch >/dev/null
  else
    printf '%s\n' "$request" | "$TOOLS_DIR/uvx" --from 'mcp-server-fetch==2026.7.10' --with 'mcp<2' mcp-server-fetch >/dev/null
  fi
  js_dir="$(find "$UV_CACHE_DIR" -type d -path '*/readabilipy/javascript' -print -quit 2>/dev/null || true)"
  if [[ -n "$js_dir" ]]; then
    ensure_command npm "web_fetch 的正文提取需要 Node.js 18+ 和 npm。"
    (cd "$js_dir" && npm install --omit=dev --no-audit --no-fund >/dev/null)
  fi
}

ensure_client_dependencies() {
  ensure_command npm "请安装 Node.js 18+ 与 npm 后重试。"
  ensure_command node "请安装 Node.js 18+ 后重试。"
  local node_major
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  if [[ "$node_major" -lt 18 ]]; then
    echo "Node.js 版本过低（当前 $node_major），需要 18+。" >&2
    exit 1
  fi
  if [[ ! -x "$CLIENT_DIR/node_modules/.bin/vite" ]]; then
    echo "[$(date '+%F %T')] 正在安装前端依赖 ..."
    (cd "$CLIENT_DIR" && npm ci --no-audit --no-fund)
  fi
}

ensure_server_binary() {
  if [[ -x "$SERVER_BIN" ]]; then
    return
  fi
  ensure_command cargo "未找到后端二进制，且未安装 Rust 工具链。"
  echo "[$(date '+%F %T')] 正在构建 Chat Server ..."
  (cd "$SERVER_DIR" && cargo build --release --locked)
  if [[ -x "$SERVER_DIR/target/release/chat-server" && "$SERVER_BIN" == "$SERVER_DIR/chat-server" ]]; then
    cp "$SERVER_DIR/target/release/chat-server" "$SERVER_BIN"
    chmod +x "$SERVER_BIN"
  fi
  if [[ ! -x "$SERVER_BIN" ]]; then
    echo "构建后仍未找到可执行服务端：$SERVER_BIN" >&2
    exit 1
  fi
}

load_and_validate_environment() {
  if [[ ! -f "$SERVER_DIR/.agent.env" ]]; then
    echo "缺少 $SERVER_DIR/.agent.env。请复制 .agent.env.example 后填写 AGENT_SERVICE_SECRET 和 AGENT_MASTER_KEY。" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "$SERVER_DIR/.agent.env"
  set +a
  for name in AGENT_SERVICE_SECRET AGENT_MASTER_KEY; do
    if [[ -z "${!name:-}" || "${!name}" == replace-with-* ]]; then
      echo "缺少有效的 $name。请检查 $SERVER_DIR/.agent.env。" >&2
      exit 1
    fi
  done
}

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
      echo "建议：检查 chat-server/.agent.env 中的 AGENT_SERVICE_SECRET、AGENT_MASTER_KEY；确认已拉取仓库中的服务端二进制：" >&2
      echo "  $SERVER_BIN" >&2
      echo "  python3 -m pip install -r $ROOT_DIR/agent-service/requirements.txt" >&2
      ;;
    "Chat Client")
      tail_logs "$component" "$CLIENT_DIR/.runtime/vite.log"
      echo "建议：确认 Node.js 18+、npm 依赖和 ${VITE_PORT:-3000} 端口；必要时运行：" >&2
      echo "  cd $CLIENT_DIR && npm ci" >&2
      echo "  ss -ltnp | rg ':${VITE_PORT:-3000}'" >&2
      echo "  检查 chat-client/.frontend.env 中的 CHAT_SERVER_URL、AGENT_SERVER_URL。" >&2
      ;;
    "QQ Gateway")
      tail_logs "$component" "$QQ_GATEWAY_DIR/.runtime/qq-gateway.log"
      echo "建议：检查 chat-server/.agent.env 中的 QQ_* 配置；QQ Gateway 依赖可执行的 Python 环境。" >&2
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

load_and_validate_environment
ensure_server_binary
ensure_python_environment
ensure_web_fetch_runtime
ensure_client_dependencies

export CHAT_SERVER_BIN="$SERVER_BIN"

if ! run_component "Chat Server" "$SERVER_DIR/start.sh"; then
  exit 1
fi

if [[ "${QQ_GATEWAY_ENABLED:-false}" == "true" ]]; then
  if ! run_component "QQ Gateway" "$QQ_GATEWAY_DIR/start.sh"; then
    echo "QQ Gateway 失败，正在回收已启动的服务端，避免留下不完整进程。" >&2
    "$SERVER_DIR/stop.sh" || true
    exit 1
  fi
fi

# Do not let the long-lived Vite process inherit the orchestration lock.
exec 9>&-

if ! run_component "Chat Client" "$CLIENT_DIR/start.sh"; then
  echo "客户端失败，正在回收已启动的服务端，避免留下不完整进程。" >&2
  if [[ "${QQ_GATEWAY_ENABLED:-false}" == "true" ]]; then
    "$QQ_GATEWAY_DIR/stop.sh" || true
  fi
  "$SERVER_DIR/stop.sh" || true
  exit 1
fi

echo
echo "全部组件已启动。"
echo "服务端二进制: $SERVER_BIN"
echo "前端: http://127.0.0.1:${VITE_PORT:-3000}"
echo "服务端日志: $SERVER_DIR/.runtime/"
echo "客户端日志: $CLIENT_DIR/.runtime/vite.log"
