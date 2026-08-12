# agentWorkCluster

一个由端到端加密聊天、云端 Agent 平台、任务集群编排和本地执行器组成的工作区。四个组件保持独立可运行，但通过统一的认证、网关和运行协议协作。

## 组件

| 组件 | 职责 | 开发端口 | 文档 |
| --- | --- | --- | --- |
| `chat-client` | Svelte 聊天与 Agent/Task 工作台 | `3000` | [README](chat-client/README.md) / [CLAUDE](chat-client/CLAUDE.md) |
| `chat-server` | Rust 聊天 API、认证和生产网关/部署入口 | 本机 `9012`；公开网关 `9010` | [README](chat-server/README.md) / [CLAUDE](chat-server/CLAUDE.md) |
| `agent-service` | FastAPI Agent、工具、Task、评估和 Local Agent 控制面 | `9011` | [README](agent-service/README.md) / [CLAUDE](agent-service/CLAUDE.md) |
| `qq-gateway` | QQ Bot WebSocket、事件去重、Token 和 Agent 回复网关 | `9013` | [README](qq-gateway/README.md) |
| `local-agent` | 执行机上的 Node.js daemon/CLI | 本地 Unix socket | [README](local-agent/README.md) / [CLAUDE](local-agent/CLAUDE.md) |

## 架构

```text
Browser
  |  :3000 (Vite) / :9010 (Caddy)
  +-- Chat API and WebSocket ----------> chat-server
  +-- Agent / Task / device API --------> agent-service
                                           |-- PostgreSQL + Redis (production)
                                           +-- local-agent daemon on execution machines
  qq-gateway (:9013) -- WebSocket ------> QQ platform
                                            +-- internal Channel API --> agent-service
```

普通私聊和群聊的消息内容在客户端加密；Agent run、授权工具结果和 Task 审计内容不属于该 E2EE 边界，按 Agent 平台的运行与存储策略处理。

## 本机联调

### 前置条件

- Rust 工具链（构建 `chat-server`）
- Python 3.12+ 和 `pip`
- Node.js 18+、npm
- `curl`；生产模式额外需要 PostgreSQL、Redis、Docker/Caddy 或 systemd

### 首次配置与启动

```bash
cd /home/zhouzw/agentWorkCluster
cp chat-server/.agent.env.example chat-server/.agent.env
# 编辑 chat-server/.agent.env，设置 AGENT_SERVICE_SECRET 和 AGENT_MASTER_KEY
python3 -m pip install -r agent-service/requirements.txt
cd chat-client && npm ci && cd ..
./start.sh
```

脚本先启动 `chat-server`（Rust 聊天服务 `9012`、Agent API `9011`，配置 Redis 时也启动 Worker），再启动 Vite `3000`。浏览器访问 `http://127.0.0.1:3000`。

```bash
./stop.sh
./restart.sh
```

本机脚本的日志和 PID 位于各组件的 `.runtime/`。脚本管理的开发进程不要再交给 systemd 管理。

## 环境与端口

`chat-server/.agent.env` 是本机服务端与 Agent API 共用的配置入口。至少设置：

```dotenv
AGENT_SERVICE_SECRET=use-a-long-random-shared-secret
AGENT_MASTER_KEY=use-a-valid-fernet-key

# Set this only when using QQ Bot. AppID/AppSecret are entered in Agent settings.
QQ_GATEWAY_ENABLED=true
QQ_GATEWAY_MASTER_KEY=use-a-valid-fernet-key
```

可选的 `REDIS_URL` 加上 PostgreSQL 配置会启用生产式 Worker 路径；未配置 Redis 时使用 SQLite 和进程内编排，适合本机开发。前端代理可在 `chat-client/.frontend.env` 覆盖：开发脚本默认聊天服务 `http://127.0.0.1:9012`、Agent 服务 `http://127.0.0.1:9011`、前端 `3000`。

生产环境只应公开 Caddy 的单一入口；`9011`、数据库和 Redis 留在内部网络。网关必须覆盖聊天 API/`/ws`，以及 Agent 的 `/api/v1/agents*`、`/agent-conversations*`、`/agent-runs*`、`/tools*`、`/evaluations*`、`/tasks*`、`/task-dispatch-events*`、`/notifications*`、`/local-agent*`、`/agent/ws*`、`/task/ws*` 和 `/local-agent/ws*`。详细操作见 [systemd 部署说明](chat-server/deploy/systemd/README.md)。

QQ Bot 接入见 [qq-gateway README](qq-gateway/README.md)。将 `QQ_GATEWAY_ENABLED=true` 和 `QQ_GATEWAY_MASTER_KEY` 写入 `chat-server/.agent.env` 后，根目录 `./start.sh` 和 `./stop.sh` 会统一管理 Gateway。Gateway 仅通过主动建立的 WebSocket 连接接收 QQ 事件，不需要向公网暴露 Webhook 路径；Gateway 通过服务密钥调用 Agent 的内部 Channel API，QQ AppSecret 和 Gateway 事件内容均不进入日志。

## 验证

按修改范围运行对应检查：

```bash
cd chat-client && npm run build && npm test
cd ../chat-server && cargo test
cd ../agent-service && python -m unittest discover -s tests -p 'test_*.py' -v
cd ../local-agent && npm test
```

跨组件改动特别是 API、WebSocket、Task 或 Local Agent 协议，必须同时更新调用方、Vite/Caddy 代理、测试及组件文档。
