#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT_DIR/chat-server"
DATABASE_PATH="$SERVER_DIR/data/chat.db"
REPORT_DIR="$SERVER_DIR/.runtime/password-resets"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
BACKUP_PATH="$SERVER_DIR/data/chat.db.before-password-reset-$TIMESTAMP"
REPORT_PATH="$REPORT_DIR/passwords-$TIMESTAMP.txt"
TEMP_REPORT=""
SERVICES_STOPPED=false
RESET_COMPLETE=false

usage() {
  cat <<'EOF'
用法: ./reset-all-passwords.sh --yes

为 chat.db 中的每个账号生成独立的随机密码。脚本会停止服务、备份数据库、
写入新的 bcrypt 哈希、保存密码清单，并重新启动服务。
EOF
}

cleanup() {
  if [[ -n "$TEMP_REPORT" && -f "$TEMP_REPORT" ]]; then
    unlink "$TEMP_REPORT"
  fi
  if [[ "$SERVICES_STOPPED" == true && "$RESET_COMPLETE" != true ]]; then
    echo "密码重置未完成，服务保持停止状态。数据库备份：$BACKUP_PATH" >&2
    echo "确认问题后运行 ./start.sh 恢复服务。" >&2
  fi
}
trap cleanup EXIT

if [[ "${1:-}" != "--yes" || "$#" -ne 1 ]]; then
  usage >&2
  exit 2
fi

for command in cargo cp mktemp; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "缺少命令：$command" >&2
    exit 1
  fi
done
if [[ ! -f "$DATABASE_PATH" ]]; then
  echo "未找到聊天数据库：$DATABASE_PATH" >&2
  exit 1
fi

umask 077
mkdir -p "$REPORT_DIR"

echo "正在停止服务..."
"$ROOT_DIR/stop.sh"
SERVICES_STOPPED=true

echo "正在备份数据库到：$BACKUP_PATH"
cp --preserve=mode,timestamps "$DATABASE_PATH" "$BACKUP_PATH"

TEMP_REPORT="$(mktemp "$REPORT_DIR/.passwords-$TIMESTAMP.XXXXXX")"
echo "正在重置所有账号密码..."
(
  cd "$SERVER_DIR"
  cargo run --release --locked --quiet --bin reset-all-passwords -- "$DATABASE_PATH"
) >"$TEMP_REPORT"
mv "$TEMP_REPORT" "$REPORT_PATH"
TEMP_REPORT=""
chmod 600 "$REPORT_PATH"

echo "正在重新启动服务..."
"$ROOT_DIR/start.sh"
RESET_COMPLETE=true

echo
echo "密码重置完成。新密码清单：$REPORT_PATH"
echo "数据库备份：$BACKUP_PATH"
echo "请通过受控渠道把每个账号对应的新密码交给用户，并在完成后安全删除该清单。"
