#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_BIN="${CHAT_SERVER_BIN:-$ROOT_DIR/chat-server/chat-server}"

echo "[$(date '+%F %T')] 开始重启 agentWorkCluster ..."
if [[ ! -x "$SERVER_BIN" ]]; then
  echo "未找到可执行的服务端二进制：$SERVER_BIN" >&2
  echo "请确认已拉取包含 chat-server/chat-server 的仓库版本，或通过 CHAT_SERVER_BIN 指定路径。" >&2
  exit 1
fi

if ! "$ROOT_DIR/stop.sh"; then
  echo "重启已中止：关闭阶段失败。请先处理关闭错误，再重新执行 ./restart.sh。" >&2
  exit 1
fi

exec env CHAT_SERVER_BIN="$SERVER_BIN" "$ROOT_DIR/start.sh"
