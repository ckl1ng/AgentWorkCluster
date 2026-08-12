# QQ Agent Gateway

根目录下的 QQ Bot 接入网关。Gateway 支持从 Agent 设置页动态连接 QQ WebSocket：用户输入 AppID 和 AppSecret 后，Agent API 将连接命令转发到 Gateway，Gateway 加密保存凭证、自动刷新 access_token、维持心跳并将事件投递到对应 Agent。旧版 Webhook 模式仍保留用于兼容部署。

## 本地启动

```bash
cd qq-gateway
cp .env.example .env
# 填写 AGENT_SERVICE_SECRET 和 QQ_GATEWAY_MASTER_KEY；QQ AppID/AppSecret 在网页 Agent 配置中填写
set -a; source .env; set +a
python3 -m pip install -r requirements.txt
./start.sh
```

健康检查：`http://127.0.0.1:9013/healthz`。Webhook 地址为 `/qq/webhook/{QQ_BOT_ID}`，生产环境由 Caddy 代理到该地址。

## 网页一键连接 QQ Bot

1. 启动 Agent API、QQ Gateway 和前端，并在网页打开目标 Agent 的“配置”。
2. 在基础配置的“QQ Bot 连接”区域填写 QQ 开放平台的 AppID、AppSecret；Bot ID 可留空，连接成功后从 READY 事件自动识别。
3. 点击“一键连接 QQ Bot”。Gateway 会调用 `/app/getAppAccessToken` 获取临时凭证，再调用 `/gateway` 获取 `wss://` 网关地址并建立 WebSocket 连接，发送 Identify（默认 Intents 为 513），收到 `GROUP_AT_MESSAGE_CREATE` 或 `C2C_MESSAGE_CREATE` 后自动转给当前 Agent。
4. Gateway 在自己的加密数据库中保存凭证，进程重启后自动恢复连接；浏览器不会保存 AppSecret。点击“断开连接”会删除 Gateway 中的连接配置。

QQ Gateway 仍需要 `AGENT_SERVICE_SECRET` 和 `QQ_GATEWAY_MASTER_KEY` 作为服务间认证及凭证加密密钥。AppID、AppSecret、Agent ID 和 owner ID 不再需要写入 `.env`。Docker Compose 中 Agent API 使用 `QQ_GATEWAY_INTERNAL_URL=http://qq-gateway:9013` 访问 Gateway。

## 数据流和安全边界

- Gateway 是 QQ provider state 的所有者；Agent 仍拥有 Agent、Conversation 和 Run。
- Inbox 事件内容使用 `QQ_GATEWAY_MASTER_KEY` 加密存储，并按 `bot_id + event_id` 去重。
- Gateway 使用 `Authorization: Service <AGENT_SERVICE_SECRET>` 调用 `agent-service` 的 `/internal/v1/channel-events` 和 `/internal/v1/channel-runs/{run_id}`。
- Access Token 只保存在进程内并提前 5 分钟刷新；QQ AppSecret 不写日志。
- 未完成事件会持久化并由后台循环重试；超过被动回复窗口的事件会标记为过期，不会再尝试使用失效的 `msg_id`。回复发送按 401 刷新、429/5xx 退避重试。

## 配置注意事项

`QQ_API_BASE_URL` 默认值来自当前项目联调约定。QQ 平台域名、Webhook 验证签名和消息字段可能随官方版本变化，正式部署前必须以 QQ 官方最新文档为准。生产默认使用 `ed25519` 验证模式；本地测试可使用 `hmac-sha256`。

Docker Compose 已包含 `qq-gateway` 服务和 Caddy `/qq/webhook*` 路由。没有 QQ 配置时不要启动该 Compose 服务；默认根目录本机联调仍只启动聊天服务、Agent API 和前端。

Docker 部署时，QQ Gateway 使用可选 profile：配置服务间密钥后执行 `docker compose --profile qq up -d`，再从网页 Agent 设置连接 QQ Bot。不带该 profile 的默认集群不会启动 QQ Gateway。
