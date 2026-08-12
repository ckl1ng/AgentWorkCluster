# QQ Agent Gateway

根目录下的 QQ Bot 接入网关。Gateway 仅支持从 Agent 设置页动态连接 QQ WebSocket：用户输入 AppID 和 AppSecret 后，Agent API 将连接命令转发到 Gateway，Gateway 加密保存凭证、自动刷新 access_token、维持心跳并将事件投递到对应 Agent。

## 本地启动

本机联调由根目录脚本统一管理。将以下配置写入 `chat-server/.agent.env`：

```dotenv
QQ_GATEWAY_ENABLED=true
QQ_GATEWAY_MASTER_KEY=replace-with-a-valid-fernet-key
QQ_GATEWAY_INTERNAL_URL=http://127.0.0.1:9013
```

`AGENT_SERVICE_SECRET` 必须与 Agent 服务使用同一个值。AppID/AppSecret 在网页 Agent 设置中填写，Gateway 加密保存，不写入环境文件。安装 Python 依赖后，从根目录启动：

```bash
python3 -m pip install -r qq-gateway/requirements.txt
./start.sh
./stop.sh
```

健康检查：`http://127.0.0.1:9013/healthz`。QQ 事件通过 Gateway 主动建立的 WebSocket 连接接收，不需要回调地址或公网 Webhook 入口。根脚本会将 PID 和日志写入 `qq-gateway/.runtime/`。

## 网页一键连接 QQ Bot

1. 启动 Agent API、QQ Gateway 和前端，并在网页打开目标 Agent 的“配置”。
2. 在基础配置的“QQ Bot 连接”区域填写 QQ 开放平台的 AppID、AppSecret；Bot ID 可留空，连接成功后从 READY 事件自动识别。
3. 点击“一键连接 QQ Bot”。Gateway 会调用 `/app/getAppAccessToken` 获取临时凭证，再调用 `/gateway` 获取 `wss://` 网关地址并建立 WebSocket 连接，接收 Hello 后发送 Identify；短暂断线会使用 session_id/seq 发送 Resume。默认 Intents 为 `33554432`（`GROUP_AND_C2C_EVENT`），可接收 `GROUP_AT_MESSAGE_CREATE` 和 `C2C_MESSAGE_CREATE`。
4. Gateway 在自己的加密数据库中保存凭证，进程重启后自动恢复连接；浏览器不会保存 AppSecret。点击“断开连接”会删除 Gateway 中的连接配置。

WebSocket 运行时遵循 QQ opcode：Hello(10) -> Identify(2)，按 Hello 周期发送 Heartbeat(1)，接收 Heartbeat ACK(11)，断线优先 Resume(6)，并处理 Reconnect(7) 与 Invalid Session(9)。HTTP 回调专属的 opcode 12/13 不参与 WebSocket 流程。

QQ Gateway 仍需要 `AGENT_SERVICE_SECRET` 和 `QQ_GATEWAY_MASTER_KEY` 作为服务间认证及凭证加密密钥。AppID、AppSecret、Agent ID 和 owner ID 不再需要写入 `.env`。Docker Compose 中 Agent API 使用 `QQ_GATEWAY_INTERNAL_URL=http://qq-gateway:9013` 访问 Gateway。

## 数据流和安全边界

- Gateway 是 QQ provider state 的所有者；Agent 仍拥有 Agent、Conversation 和 Run。
- Inbox 事件内容使用 `QQ_GATEWAY_MASTER_KEY` 加密存储，并按 `bot_id + event_id` 去重。
- Gateway 使用 `Authorization: Service <AGENT_SERVICE_SECRET>` 调用 `agent-service` 的 `/internal/v1/channel-events` 和 `/internal/v1/channel-runs/{run_id}`。
- Access Token 只保存在进程内并提前 5 分钟刷新；QQ AppSecret 不写日志。
- 未完成事件会持久化并由后台循环重试；超过被动回复窗口的事件会标记为过期，不会再尝试使用失效的 `msg_id`。回复发送按 401 刷新、429/5xx 退避重试。
- WebSocket 接收循环独立于 Agent 运行；`QQ_MAX_EVENT_TASKS`（默认 `32`）限制后台事件处理并发，避免慢模型任务耗尽 Gateway 资源。

## 配置注意事项

`QQ_API_BASE_URL` 默认值来自当前项目联调约定。QQ 平台域名、消息字段、事件权限和 Intents 可能随官方版本变化，正式部署前必须以 QQ 官方最新文档为准。默认 Intents 为 `33554432`（`GROUP_AND_C2C_EVENT`）。

Token 接口默认使用 `https://bots.qq.com`，Gateway 和消息 API 默认使用 `https://api.bot.qq.com`。如 QQ 开放平台为当前应用分配了不同域名，可通过 `QQ_TOKEN_API_BASE_URL` 覆盖 Token 服务地址；不要把 AppSecret 写入日志或 URL。

Docker Compose 已包含 `qq-gateway` 服务。没有 QQ 配置时不要启动该 Compose 服务；默认根目录本机联调仍只启动聊天服务、Agent API 和前端。

Docker 部署时，QQ Gateway 使用可选 profile：配置服务间密钥后执行 `docker compose --profile qq up -d`，再从网页 Agent 设置连接 QQ Bot。不带该 profile 的默认集群不会启动 QQ Gateway。
