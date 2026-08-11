#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[$(date '+%F %T')] 开始重启 agentWorkCluster ..."
if ! "$ROOT_DIR/stop.sh"; then
  echo "重启已中止：关闭阶段失败。请先处理关闭错误，再重新执行 ./restart.sh。" >&2
  exit 1
fi

exec "$ROOT_DIR/start.sh"
