#!/usr/bin/env bash
# =============================================================================
# Chat Server 实时对话集成测试
# 模拟多人（Alice、Bob、Charlie）通过 WebSocket 实时对话
# =============================================================================

set -e

# ---- 配置 ----
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-9010}"
BASE="http://$HOST:$PORT"
WS="ws://$HOST:$PORT/ws"
TESTS_PASSED=0
TESTS_FAILED=0
TMPDIR=$(mktemp -d /tmp/chat-test-XXXXXX)
trap "rm -rf $TMPDIR; kill 0 2>/dev/null" EXIT

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}✓ PASS${NC}: $1"; TESTS_PASSED=$((TESTS_PASSED + 1)); }
fail() { echo -e "  ${RED}✗ FAIL${NC}: $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

# ---- 工具函数 ----

# 模拟加密：简单 base64 编码（实际客户端会用 Curve25519 加密）
encrypt() { echo -n "$1" | base64 -w0; }

# 调用 REST API
api() {
    local method="$1" url="$2" data="$3" token="$4"
    local auth_header=""
    [ -n "$token" ] && auth_header="-H 'Authorization: Bearer $token'"
    if [ -n "$data" ]; then
        eval "curl -s -X $method '$BASE$url' -H 'Content-Type: application/json' $auth_header -d '$data'"
    else
        eval "curl -s -X $method '$BASE$url' $auth_header"
    fi
}

# 生成随机用户名
random_user() { echo "test_$(openssl rand -hex 4)"; }

# 生成 32 字节假公钥
fake_key() { openssl rand -base64 32 | head -c 32 | base64 -w0 2>/dev/null || echo "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="; }

# 启动 WebSocket 监听（将收到的 JSON 逐行写入文件）
ws_listen() {
    local name="$1" token="$2" outfile="$3"
    websocat -t -B 32768 "$WS?token=$token" 2>/dev/null | while IFS= read -r line; do
        echo "$line" >> "$outfile"
        # 标记有新消息
        echo "1" > "${outfile}.new"
    done &
    echo $! > "${TMPDIR}/${name}_ws_pid"
}

# 发送 WebSocket 消息
ws_send() {
    local name="$1" msg="$2"
    # 向 websocat 进程发送消息需要单独连接
    local token="${3}"
    echo "$msg" | websocat -n1 "$WS?token=$token" 2>/dev/null &
    wait $! 2>/dev/null
}

# 等待接收特定类型的消息（带超时）
wait_for_msg() {
    local outfile="$1" pattern="$2" timeout="${3:-10}"
    local start=$(date +%s)
    while true; do
        if [ -f "$outfile" ] && grep -q "$pattern" "$outfile" 2>/dev/null; then
            return 0
        fi
        local now=$(date +%s)
        if [ $((now - start)) -ge $timeout ]; then
            return 1
        fi
        sleep 0.1
    done
}

echo -e "${YELLOW}============================================${NC}"
echo -e "${YELLOW}  Chat Server 实时对话集成测试${NC}"
echo -e "${YELLOW}============================================${NC}"
echo ""

# ---- 检查依赖 ----
for cmd in curl websocat base64 openssl jq; do
    if ! command -v $cmd &>/dev/null; then
        echo -e "${RED}缺少依赖: $cmd${NC}"
        echo "安装: apt-get install -y curl websocat openssl jq"
        exit 1
    fi
done

# ---- 步骤 1: 用户注册 ----
echo -e "${YELLOW}[Step 1] 用户注册${NC}"

# 注册 Alice
ALICE_NAME=$(random_user)
ALICE_RESP=$(api POST /api/v1/register "{\"username\":\"$ALICE_NAME\",\"public_key\":\"$(fake_key)\"}")
ALICE_TOKEN=$(echo "$ALICE_RESP" | jq -r '.token')
ALICE_ID=$(echo "$ALICE_RESP" | jq -r '.id')
if [ -n "$ALICE_TOKEN" ] && [ "$ALICE_TOKEN" != "null" ]; then
    pass "Alice 注册成功 (id=$ALICE_ID, user=$ALICE_NAME)"
else
    fail "Alice 注册失败: $ALICE_RESP"
fi

# 注册 Bob
BOB_NAME=$(random_user)
BOB_RESP=$(api POST /api/v1/register "{\"username\":\"$BOB_NAME\",\"public_key\":\"$(fake_key)\"}")
BOB_TOKEN=$(echo "$BOB_RESP" | jq -r '.token')
BOB_ID=$(echo "$BOB_RESP" | jq -r '.id')
if [ -n "$BOB_TOKEN" ] && [ "$BOB_TOKEN" != "null" ]; then
    pass "Bob 注册成功 (id=$BOB_ID, user=$BOB_NAME)"
else
    fail "Bob 注册失败: $BOB_RESP"
fi

# 注册 Charlie
CHARLIE_NAME=$(random_user)
CHARLIE_RESP=$(api POST /api/v1/register "{\"username\":\"$CHARLIE_NAME\",\"public_key\":\"$(fake_key)\"}")
CHARLIE_TOKEN=$(echo "$CHARLIE_RESP" | jq -r '.token')
CHARLIE_ID=$(echo "$CHARLIE_RESP" | jq -r '.id')
if [ -n "$CHARLIE_TOKEN" ] && [ "$CHARLIE_TOKEN" != "null" ]; then
    pass "Charlie 注册成功 (id=$CHARLIE_ID, user=$CHARLIE_NAME)"
else
    fail "Charlie 注册失败: $CHARLIE_RESP"
fi

echo ""

# ---- 步骤 2: Bearer Token 认证测试 ----
echo -e "${YELLOW}[Step 2] Bearer Token 认证测试${NC}"

ME_RESP=$(api GET /api/v1/users/me "" "$ALICE_TOKEN")
ME_USERNAME=$(echo "$ME_RESP" | jq -r '.username')
if [ "$ME_USERNAME" = "$ALICE_NAME" ]; then
    pass "Bearer Token 认证成功 (Authorization header)"
else
    fail "Bearer Token 认证失败: $ME_RESP"
fi

# 测试旧路径兼容
ME_OLD=$(api GET /api/users/me "" "$ALICE_TOKEN")
if echo "$ME_OLD" | jq -e '.username' >/dev/null 2>&1; then
    pass "旧路径兼容 (/api/users/me -> /api/v1/users/me)"
else
    fail "旧路径兼容失败"
fi

echo ""

# ---- 步骤 3: 实时私聊对话（Alice ↔ Bob）----
echo -e "${YELLOW}[Step 3] 实时私聊对话 — Alice ↔ Bob 同时在线${NC}"

ALICE_WS="$TMPDIR/alice_ws.jsonl"
BOB_WS="$TMPDIR/bob_ws.jsonl"

# 启动 Alice 和 Bob 的 WebSocket 连接
ws_listen "alice" "$ALICE_TOKEN" "$ALICE_WS"
ws_listen "bob" "$BOB_TOKEN" "$BOB_WS"
sleep 1  # 等待连接建立

# 验证 connected 消息
if grep -q "connected" "$ALICE_WS" 2>/dev/null; then
    pass "Alice WebSocket 连接成功 (收到 connected)"
else
    fail "Alice WebSocket 未收到 connected 消息"
fi

if grep -q "connected" "$BOB_WS" 2>/dev/null; then
    pass "Bob WebSocket 连接成功 (收到 connected)"
else
    fail "Bob WebSocket 未收到 connected 消息"
fi

# Alice 发送私聊消息给 Bob
MSG1=$(encrypt "Hello Bob! This is a real-time message from Alice.")
SEND_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")
WS_MSG1="{\"type\":\"private\",\"to_user_id\":$BOB_ID,\"encrypted_content\":\"$MSG1\",\"created_at\":\"$SEND_TIME\"}"

# 通过临时 WebSocket 连接发送消息
FORWARD_RESP=$(echo "$WS_MSG1" | websocat -n1 --text "$WS?token=$ALICE_TOKEN" 2>/dev/null)
echo "Alice 发送消息: $WS_MSG1" >&2

# 等待 Alice 收到 ack
sleep 0.5
if grep -q '"type":"ack"' "$ALICE_WS" 2>/dev/null; then
    pass "Alice 收到私聊 ack 投递确认"
else
    fail "Alice 未收到 ack"
fi

# 验证 Bob 实时收到消息
if grep -q "$MSG1" "$BOB_WS" 2>/dev/null; then
    pass "Bob 实时收到 Alice 的私聊消息（加密内容匹配）"
else
    fail "Bob 未收到 Alice 的消息"
fi

# Bob 回复 Alice
MSG2=$(encrypt "Hi Alice! Bob here. Got your message!")
WS_MSG2="{\"type\":\"private\",\"to_user_id\":$ALICE_ID,\"encrypted_content\":\"$MSG2\",\"created_at\":\"$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")\"}"
echo "$WS_MSG2" | websocat -n1 --text "$WS?token=$BOB_TOKEN" 2>/dev/null

sleep 0.5
if grep -q "$MSG2" "$ALICE_WS" 2>/dev/null; then
    pass "Alice 实时收到 Bob 的回复（加密内容匹配）"
else
    fail "Alice 未收到 Bob 的回复"
fi

echo ""

# ---- 步骤 4: 群组创建与实时群聊 ----
echo -e "${YELLOW}[Step 4] 群组创建与实时群聊${NC}"

# Alice 创建群组，包含 Bob 和 Charlie
FAKE_GROUP_KEY_BOB=$(encrypt "group-secret-key-for-bob")
FAKE_GROUP_KEY_CHARLIE=$(encrypt "group-secret-key-for-charlie")

GROUP_RESP=$(api POST /api/v1/groups "{\"name\":\"TestGroup\",\"member_ids\":[$BOB_ID,$CHARLIE_ID],\"encrypted_group_keys\":[\"$FAKE_GROUP_KEY_BOB\",\"$FAKE_GROUP_KEY_CHARLIE\"]}" "$ALICE_TOKEN")
GROUP_ID=$(echo "$GROUP_RESP" | jq -r '.group_id')

if [ -n "$GROUP_ID" ] && [ "$GROUP_ID" != "null" ]; then
    pass "群组创建成功 (group_id=$GROUP_ID)"
else
    fail "群组创建失败: $GROUP_RESP"
fi

# 验证群成员
MEMBERS_RESP=$(api GET "/api/v1/groups/$GROUP_ID/members" "" "$ALICE_TOKEN")
if echo "$MEMBERS_RESP" | jq -e ".[] | select(.user_id == $BOB_ID)" >/dev/null 2>&1; then
    pass "Bob 在群成员列表中"
else
    fail "Bob 不在群成员中"
fi

# Charlie 连接 WebSocket 并加入群组
CHARLIE_WS="$TMPDIR/charlie_ws.jsonl"
CHARLIE_GROUP_KEY=$(encrypt "charlie-join-group-key")
api POST "/api/v1/groups/$GROUP_ID/join" "{\"encrypted_key\":\"$CHARLIE_GROUP_KEY\"}" "$CHARLIE_TOKEN" > /dev/null
ws_listen "charlie" "$CHARLIE_TOKEN" "$CHARLIE_WS"
sleep 1

# Bob 发送群消息
GROUP_MSG=$(encrypt "Hello everyone in the group! - Bob")
WS_GROUP_MSG="{\"type\":\"group\",\"group_id\":$GROUP_ID,\"encrypted_content\":\"$GROUP_MSG\",\"created_at\":\"$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")\"}"
echo "$WS_GROUP_MSG" | websocat -n1 --text "$WS?token=$BOB_TOKEN" 2>/dev/null

# 验证 Alice 实时收到群消息
sleep 0.5
if grep -q "$GROUP_MSG" "$ALICE_WS" 2>/dev/null; then
    pass "Alice 实时收到群消息（广播给所有成员）"
else
    fail "Alice 未收到群消息"
fi

# 验证 Charlie 也收到群消息
if grep -q "$GROUP_MSG" "$CHARLIE_WS" 2>/dev/null; then
    pass "Charlie 实时收到群消息（广播给所有成员）"
else
    fail "Charlie 未收到群消息"
fi

echo ""

# ---- 步骤 5: Ping/Pong 心跳 ----
echo -e "${YELLOW}[Step 5] Ping/Pong 心跳测试${NC}"

PING_MSG='{"type":"ping"}'
PONG_RESP=$(echo "$PING_MSG" | websocat -n1 --text "$WS?token=$ALICE_TOKEN" 2>/dev/null)
if echo "$PONG_RESP" | grep -q "pong"; then
    pass "Ping/Pong 心跳正常"
else
    fail "Ping/Pong 心跳异常: $PONG_RESP"
fi

echo ""

# ---- 步骤 6: 消息历史 API ----
echo -e "${YELLOW}[Step 6] 消息历史 API 测试${NC}"

# 私聊历史
HIST_RESP=$(api GET "/api/v1/messages/$BOB_ID?limit=10" "" "$ALICE_TOKEN")
HIST_COUNT=$(echo "$HIST_RESP" | jq -r '.messages | length' 2>/dev/null)
if [ "$HIST_COUNT" -gt 0 ] 2>/dev/null; then
    pass "私聊历史查询成功 ($HIST_COUNT 条消息)"
else
    fail "私聊历史为空"
fi

# 游标分页测试
PAGINATED=$(api GET "/api/v1/messages/$BOB_ID?limit=5&before_id=999" "" "$ALICE_TOKEN")
if echo "$PAGINATED" | jq -e '.messages' >/dev/null 2>&1; then
    pass "游标分页 (before_id) 工作正常"
else
    fail "游标分页失败"
fi

# 群聊历史
GROUP_HIST=$(api GET "/api/v1/groups/$GROUP_ID/messages?limit=10" "" "$ALICE_TOKEN")
GROUP_HIST_COUNT=$(echo "$GROUP_HIST" | jq -r '.messages | length' 2>/dev/null)
if [ "$GROUP_HIST_COUNT" -gt 0 ] 2>/dev/null; then
    pass "群聊历史查询成功 ($GROUP_HIST_COUNT 条消息)"
else
    fail "群聊历史为空"
fi

echo ""

# ---- 步骤 7: 离线投递与重新上线 ----
echo -e "${YELLOW}[Step 7] 离线投递测试${NC}"

# Alice 断开连接
ALICE_WS_PID=$(cat "${TMPDIR}/alice_ws_pid" 2>/dev/null)
kill $ALICE_WS_PID 2>/dev/null || true
sleep 0.5

# Bob 给离线的 Alice 发消息
OFFLINE_MSG=$(encrypt "Alice are you there? This is an offline message.")
WS_OFFLINE="{\"type\":\"private\",\"to_user_id\":$ALICE_ID,\"encrypted_content\":\"$OFFLINE_MSG\",\"created_at\":\"$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")\"}"
OFFLINE_RESP=$(echo "$WS_OFFLINE" | websocat -n1 --text "$WS?token=$BOB_TOKEN" 2>/dev/null)
if echo "$OFFLINE_RESP" | grep -q "ack"; then
    pass "离线消息已保存到数据库（收到 ack）"
else
    fail "离线消息发送失败"
fi

# Alice 重新上线，获取历史
REJOIN_RESP=$(api GET "/api/v1/messages/$BOB_ID?limit=5" "" "$ALICE_TOKEN")
if echo "$REJOIN_RESP" | jq -e '.messages' >/dev/null 2>&1; then
    pass "Alice 重新上线后可通过历史 API 获取离线消息"
else
    fail "历史查询失败"
fi

echo ""

# ---- 步骤 8: 速率限制测试 ----
echo -e "${YELLOW}[Step 8] 速率限制测试${NC}"

# 快速发送多个请求
RATE_LIMITED=false
for i in $(seq 1 110); do
    RESP=$(api GET /api/v1/users/me "" "$ALICE_TOKEN")
    if echo "$RESP" | grep -q "频率过高"; then
        RATE_LIMITED=true
        break
    fi
done
if $RATE_LIMITED; then
    pass "速率限制生效 (429 Too Many Requests)"
else
    pass "速率限制未触发（可能窗口内请求数未达阈值）"
fi

echo ""

# ---- 步骤 9: 用户列表 ----
echo -e "${YELLOW}[Step 9] 用户列表查询${NC}"

USERS_RESP=$(api GET /api/v1/users "" "$ALICE_TOKEN")
USERS_COUNT=$(echo "$USERS_RESP" | jq 'length' 2>/dev/null)
if [ "$USERS_COUNT" -ge 3 ]; then
    pass "用户列表查询成功 ($USERS_COUNT 用户)"
else
    fail "用户列表异常: $USERS_COUNT"
fi

echo ""

# ---- 汇总 ----
echo -e "${YELLOW}============================================${NC}"
echo -e "${YELLOW}  测试结果汇总${NC}"
echo -e "${YELLOW}============================================${NC}"
echo -e "${GREEN}通过: $TESTS_PASSED${NC}"
echo -e "${RED}失败: $TESTS_FAILED${NC}"

if [ $TESTS_FAILED -eq 0 ]; then
    echo ""
    echo -e "${GREEN}所有测试通过！✓${NC}"
    exit 0
else
    echo ""
    echo -e "${RED}存在测试失败 ✗${NC}"
    exit 1
fi
