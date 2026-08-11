# QQ Agent Gateway

根目录下的 QQ Bot 接入网关。第一版实现 Webhook 模式：QQ 事件快速确认后进入加密 Inbox，Gateway 复用 QQ 群/用户对应的 Agent 会话，调用 Agent 内部 Channel API 创建 Run，完成后通过 QQ 消息 API 回复。

## 本地启动

```bash
cd qq-gateway
cp .env.example .env
# 填写 QQ_APP_ID、QQ_CLIENT_SECRET、QQ_DEFAULT_AGENT_ID、QQ_DEFAULT_OWNER_USER_ID、
# AGENT_SERVICE_SECRET 和 QQ_GATEWAY_MASTER_KEY
set -a; source .env; set +a
python3 -m pip install -r requirements.txt
./start.sh
```

健康检查：`http://127.0.0.1:9013/healthz`。Webhook 地址为 `/qq/webhook/{QQ_BOT_ID}`，生产环境由 Caddy 代理到该地址。

## 数据流和安全边界

- Gateway 是 QQ provider state 的所有者；Agent 仍拥有 Agent、Conversation 和 Run。
- Inbox 事件内容使用 `QQ_GATEWAY_MASTER_KEY` 加密存储，并按 `bot_id + event_id` 去重。
- Gateway 使用 `Authorization: Service <AGENT_SERVICE_SECRET>` 调用 `agent-service` 的 `/internal/v1/channel-events` 和 `/internal/v1/channel-runs/{run_id}`。
- Access Token 只保存在进程内并提前 5 分钟刷新；QQ AppSecret 不写日志。
- 未完成事件会持久化并由后台循环重试；超过被动回复窗口的事件会标记为过期，不会再尝试使用失效的 `msg_id`。回复发送按 401 刷新、429/5xx 退避重试。

## 配置注意事项

`QQ_API_BASE_URL` 默认值来自当前项目联调约定。QQ 平台域名、Webhook 验证签名和消息字段可能随官方版本变化，正式部署前必须以 QQ 官方最新文档为准。生产默认使用 `ed25519` 验证模式；本地测试可使用 `hmac-sha256`。

Docker Compose 已包含 `qq-gateway` 服务和 Caddy `/qq/webhook*` 路由。没有 QQ 配置时不要启动该 Compose 服务；默认根目录本机联调仍只启动聊天服务、Agent API 和前端。

Docker 部署时，QQ Gateway 使用可选 profile：配置完整凭证后执行 `docker compose --profile qq up -d`。不带该 profile 的默认集群不会启动 QQ Gateway。
