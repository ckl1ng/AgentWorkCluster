# Chat Server

基于 Rust、Axum 和 WebSocket 构建的实时端到端加密聊天服务器。服务器仅作为消息中继和加密数据存储——它永远看不到明文消息内容。

## 功能特性

- **端到端加密** — 所有消息内容和群密钥均在客户端加密。服务器仅存储和转发密文。
- **用户注册** — 使用用户名和 Curve25519 公钥注册，获得认证 token。
- **私聊** — 一对一加密聊天，通过 WebSocket 实时投递。
- **群聊** — 创建群组，使用每个成员各自的加密群密钥邀请加入，广播群消息。
- **消息持久化** — 所有消息存储在嵌入式数据库（redb）中，可按历史记录检索。
- **在线状态** — 实时跟踪已连接用户；消息即时投递给在线接收者。
- **投递确认** — 私聊消息的发送者会收到包含投递状态的 `ack` 回执。
- **Agent 平台与 Task 集群** — 独立 Agent API 与 Worker，提供版本化配置、运行队列、受控工具、长期记忆、评估，以及预算受限、上下文隔离的多 Agent Task 编排。

## 架构

```
客户端 A ←──WebSocket──→ Chat Server (Axum) ←──WebSocket──→ 客户端 B
                              │
                              ├── REST API（注册、用户、群组、历史记录）
                              │
                              └── redb（嵌入式键值数据库）
```

- **HTTP 框架**：[Axum 0.7](https://github.com/tokio-rs/axum)
- **数据库**：[redb](https://github.com/cberner/redb) — 嵌入式、类型化键值存储
- **异步运行时**：[Tokio](https://tokio.rs/)
- **实时通信**：WebSocket + `tokio::sync::broadcast` 通道

## 快速开始

### 环境要求

- Rust 1.75+（edition 2021）

### 构建与运行

```bash
# 克隆仓库
git clone <repo-url>
cd chat-server

# 运行（默认：0.0.0.0:9010）
cargo run

# 或使用自定义配置
HOST=127.0.0.1 PORT=8080 DATA_DIR=./mydata cargo run
```

服务器启动后输出：

```
聊天服务启动于 http://0.0.0.0:9010
WebSocket 端点: ws://0.0.0.0:9010/ws
```

### 清空开发数据

先停止服务，再删除本地数据库；这会移除所有账号、好友关系、群组和消息：

```bash
cd /home/zhouzw/agentWorkCluster/chat-server
rm -f data/chat.db
```

使用 Docker Compose 时，执行 `docker compose down -v` 删除同项目的数据库卷。浏览器端的登录状态和收藏表情保存在浏览器存储中，可在开发者工具的“应用/存储”页面对该站点执行“清除站点数据”。

### 配置项

| 变量        | 默认值      | 描述                |
|------------|------------|---------------------|
| `HOST`     | `0.0.0.0`  | 监听地址              |
| `PORT`     | `9010`     | 监听端口              |
| `DATA_DIR` | `./data`   | `chat.db` 的存放目录   |

## Agent 开发联调

Agent 是独立的 FastAPI 服务，使用聊天服务的内部认证检查接口，不读取 `chat.db`。本地联调需在三个终端分别运行：

### 一键启动

根目录的 `start.sh` 会后台启动根目录的 release 后端二进制和 Agent API，并将 PID 与日志写入 `.runtime/`。首次运行前请准备密钥配置：

```bash
cd /home/zhouzw/agentWorkCluster/chat-server
cp .agent.env.example .agent.env
# 编辑 .agent.env，替换两个必填密钥占位值
./start.sh

# 停止两个服务
./stop.sh
```

这组脚本只用于本机联调。它启动的聊天服务在 `127.0.0.1:9012`，Agent API 在 `127.0.0.1:9011`；若前端通过 `:9010` 访问，必须同时运行仓库的 Caddy 配置作为网关。生产环境应使用下文的 systemd 单元和 Caddy，不能混用 `start.sh`/`stop.sh` 与 `systemctl`。脚本以互斥锁避免并发操作；重复执行 `start.sh` 会复用健康服务，失效 PID 文件会自动清理。`stop.sh` 会将僵尸 PID 视为已退出并清理 PID 文件；只有已验证的进程超过 5 秒仍未退出时才会强制终止。

后端二进制由以下命令构建并放置在根目录：

```bash
export PATH="$HOME/.cargo/bin:$PATH"
cargo build --release --target x86_64-unknown-linux-musl
install -m 755 target/x86_64-unknown-linux-musl/release/chat-server ./chat-server
```

也可以按下面的方式分终端启动，便于调试：

```bash
# 终端 1：Rust 聊天服务
export AGENT_SERVICE_SECRET='replace-with-a-long-random-service-secret'
cargo run

# 终端 2：Agent API（Python 3.8+ 可用于开发；容器使用 Python 3.12）
export AGENT_SERVICE_SECRET='replace-with-the-same-service-secret'
export AGENT_MASTER_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export CHAT_AUTH_INTROSPECTION_URL='http://127.0.0.1:9010/internal/v1/auth/introspect'
export AGENT_ALLOW_HTTP=true  # 仅本地模型调试；生产环境不要设置
uvicorn app.main:app --app-dir ../agent-service --host 127.0.0.1 --port 9011

# 终端 3：Svelte 客户端
cd /home/zhouzw/agentWorkCluster/chat-client
npm run dev
```

Vite 会把 `/api/v1/agents*`、`/api/v1/agent-conversations*`、`/api/v1/agent-runs*`、`/api/v1/tools*`、`/api/v1/evaluations*`、`/api/v1/tasks*`、`/api/v1/task-dispatch-events*`、`/api/v1/notifications*`、`/api/v1/local-agent*` 以及 `/agent/ws`、`/task/ws` 转发到 `9011`；原有聊天 API 和 `/ws` 仍转发到 `9010`。生产部署由 Caddy 收敛到唯一公开端口 `9010`。无 Docker 主机使用 `127.0.0.1:9012` 运行 Rust 聊天服务、`127.0.0.1:9011` 运行 Agent API，由 Caddy 代理并提供 `chat-client` 的静态构建产物。

### Task 集群

Task 由 Agent API 负责，不属于 Rust 聊天路由。它提供所有权、预算和上下文隔离的多 Agent 协作，前端通过 `/api/v1/tasks*`、`/api/v1/task-dispatch-events*`、`/api/v1/notifications*` 与 `/task/ws` 使用。修改这些接口时，必须同步更新 `Caddyfile`、Vite 代理、Agent API 测试与对应文档；调度事件只能携带脱敏短摘要，不能成为工作内容或密钥的旁路。

Agent 服务要求 `AGENT_MASTER_KEY`，用于加密模型 Key、提示词、消息、运行快照和 trace 原文。不要把该值、`AGENT_SERVICE_SECRET` 或数据库密码提交到仓库。`start.sh` 在未配置 `REDIS_URL` 时可用 SQLite 做轻量开发；配置 PostgreSQL 与 Redis 后会执行 Alembic 并启动独立 Worker。

### 无 Docker 生产部署

生产机无需安装 Docker。使用系统包或托管服务提供 PostgreSQL 16 与 Redis 7，并用 systemd 管理聊天服务、Agent API 和 Worker。Caddy 是唯一公开监听者；应用、PostgreSQL 和 Redis 均只应监听 loopback 或私有网络。完整安装、升级和验证步骤见 [deploy/systemd/README.md](./deploy/systemd/README.md)。

### Agent 平台 Compose 部署

生产拓扑仅由 Caddy 对主机发布 `9010`，PostgreSQL、Redis、Agent API 和 Worker 只在 Compose 网络内可见：

```bash
cd /home/zhouzw/agentWorkCluster/chat-server
set -a
source .agent.env
set +a
docker compose up --build -d
docker compose ps
```

Agent API 启动前会自动执行 `alembic upgrade head`。Redis 暂时不可用时，run 与 outbox 会保持 `queued` 并自动重试；不会被误标为失败。Worker 通过 consumer group、pending 心跳和 `XAUTOCLAIM` 恢复进程故障任务，WebSocket 使用持久化 sequence 补发断线事件。

如需迁移旧的开发 SQLite 数据，先对空 PostgreSQL 执行 Alembic，再运行非破坏性迁移工具。源文件不会被删除：

```bash
cd /home/zhouzw/agentWorkCluster/agent-service
export AGENT_DATABASE_URL='postgresql://agent:...@127.0.0.1:5432/agent'
export AGENT_MASTER_KEY='与旧库相同的 Fernet key'
python3 scripts/migrate_sqlite_to_postgres.py --source ../data/agents.db
```

### Agent 回归门禁

评估基线位于 `agent-service/evaluation/baseline_cases.json`，包含 20 个确定性案例。阶段 A 测试另行覆盖加密列、预算上下文、分段 tool call、并发限制、outbox、故障恢复和状态机。安装 Agent 依赖后可执行：

```bash
cd /home/zhouzw/agentWorkCluster/agent-service
python -m unittest discover -s tests -p 'test_*.py' -v
```

CI 会在 `agent-service` 或该工作流发生变更时运行这套门禁。关键安全案例、成功率和 p95 延迟任一不满足基线都会使回归失败。

### Local Agent CLI

`local-agent/` 是独立的 Node.js daemon/CLI（Node.js 18+）。它把网页和终端的本地运行汇聚到同一个 daemon：网页已可在“Agent 配置 → 本地执行”批准配对、选择设备和已同步工作区、绑定本地执行；运行工作台会显示本机派发状态。服务端只保存设备及工作区展示名，绝不保存本机绝对路径。

#### 安装和首次配对

在需要执行任务的电脑部署 `local-agent/`，而不是放进 Agent API 容器。该电脑必须能访问对外网关；网关必须代理 `/api/v1/local-agent*` 和 `/local-agent/ws*`，不要暴露内部 Agent API 的 `9011`。在 `local-agent/` 目录安装依赖并启动 daemon。保持 daemon 在独立终端运行；同一用户只能运行一个实例。

```bash
cd /home/zhouzw/agentWorkCluster/local-agent
npm ci
node bin/local-agent.js daemon
```

生产环境可使用当前用户的 systemd service 常驻 daemon：

```ini
# ~/.config/systemd/user/local-agent.service
[Unit]
Description=Local Agent daemon
After=network-online.target

[Service]
WorkingDirectory=/opt/agentWorkCluster/local-agent
ExecStart=/usr/bin/node /opt/agentWorkCluster/local-agent/bin/local-agent.js daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now local-agent
loginctl enable-linger "$USER"
```

在第二个终端创建配对会话。开发环境可省略 `--api` 并使用默认值 `http://127.0.0.1:9011`；部署环境应传入执行机可访问的网关 URL。CLI 会显示配对会话 ID 和配对码；在网页任一 Agent 的“配置 → 本地执行”填入二者并点击“批准本地设备”。

```bash
node bin/local-agent.js auth login --api https://chat.example.com
node bin/local-agent.js auth status
```

#### 注册工作区并绑定 Agent

显式注册需要授权的目录。命令返回的 `ws_...` 是本地工作区 ID，供 CLI `run` 使用；网页绑定使用同步后的远端工作区 ID，两者不能混用。

```bash
node bin/local-agent.js workspace add /absolute/path/to/project --name my-project
node bin/local-agent.js workspace list
node bin/local-agent.js status
```

随后在网页“配置 → 本地执行”选择设备、工作区和模型模式。`server_proxy` 使用服务端已有模型连接；`local_direct` 使用本机保存的模型 API Key。当前只有 `local_direct` 会被服务端经 `/local-agent/ws` 派发给 daemon，不会由 Cloud Worker 消费。`server_proxy` 是可绑定的控制面选项，但当前版本不会将其运行派发给 daemon；需要实际本机执行时使用 `local_direct`。

#### 配置和使用本机直连模型

`model set` 需要已配对设备和正在运行的 daemon。API Key 从环境变量读取，默认变量是 `LOCAL_AGENT_MODEL_API_KEY`，只加密保存在 `~/.local-agent/models.json`；服务端只登记 Base URL 与模型 ID。

```bash
export LOCAL_AGENT_MODEL_API_KEY='your-model-key'
node bin/local-agent.js model set AGENT_ID \
  --base-url https://model.example/v1 \
  --model-id model-name
node bin/local-agent.js model list
node bin/local-agent.js model remove AGENT_ID
```

使用本地工作区 ID 创建、查看和追踪运行：

```bash
node bin/local-agent.js run "总结当前变更" --workspace ws_xxx --agent AGENT_ID
node bin/local-agent.js run list
node bin/local-agent.js run events run_xxx
node bin/local-agent.js run attach run_xxx
```

命令总览：`auth login` / `auth status`、`daemon` / `status`、`workspace add` / `workspace list`、`model set` / `model list` / `model remove`、`run` / `run list` / `run events` / `run attach`。除 `auth` 和 `daemon` 外，大多数命令通过 `~/.local-agent/daemon.sock` 与 daemon 通信；看到 `connect ENOENT ... daemon.sock` 时，应先启动 daemon。

当前 Local Agent 默认执行文本模型运行，另支持 `codex` 执行器（把运行委托给本机 Codex 外部 CLI agent，黑盒、工作区内、内部工具不受平台治理）；不支持本机文件或进程工具、终端工具确认和断线恢复，不能作为生产远程执行器部署。私聊和群聊的端到端加密边界不覆盖 Agent run：任务、必要上下文和已授权工具结果会按 Agent 平台策略发送给模型服务并保存审计记录。

`local_direct` 要求 Agent 从未保存服务端模型 Key。当前网页“创建 Agent”表单仍要求 API Key，因此新建纯本地 Agent 需要通过 Agent API 以无 API Key、`execution_target=local`、`model_mode=local_direct` 创建；已经保存服务端 Key 的 Agent 不能改绑为 `local_direct`。

### 阶段 B 路由与部署检查

阶段 B 的工具治理、长期记忆、运行记录和评估由 Agent API 提供。Caddy 必须将以下前缀反向代理到 `agent-api:9011`：

```text
/api/v1/agents*
/api/v1/agent-conversations*
/api/v1/agent-runs*
/api/v1/tools*
/api/v1/evaluations*
/agent/ws*
/api/v1/local-agent*
/local-agent/ws*
```

更新服务端时，`agent-api`、`agent-worker` 和 `gateway` 必须使用同一份当前代码和 `Caddyfile` 重新创建；仅更新静态前端会导致阶段 B 端点返回 `404`：

```bash
cd /home/zhouzw/agentWorkCluster/chat-server
git pull
set -a && source .agent.env && set +a
docker compose up --build -d agent-api agent-worker gateway
docker compose ps
docker compose logs --tail=100 agent-api agent-worker gateway
```

`POST /api/v1/agent-conversations/{id}/runs` 返回 `500` 表示请求已到达 Agent API，但运行创建失败。应查看 `agent-api` 日志及模型连接、`AGENT_MASTER_KEY`、`AGENT_SERVICE_SECRET`、PostgreSQL 和 Redis 配置；不要通过前端吞掉该错误。

阶段 B 的主要 REST 端点如下。均使用现有聊天账号的 Bearer token，除 `healthz` 外不应暴露为匿名接口。

| 类别 | 端点 |
|---|---|
| 工具 | `GET`/`POST /api/v1/tools`，`POST /api/v1/tools/openapi/import`，`POST /api/v1/tools/mcp/discover`、`/mcp/discover-stdio`，`POST /api/v1/tools/{id}/validate` |
| Agent 工具授权 | `GET`/`PUT /api/v1/agents/{id}/tools` |
| 长期记忆 | `GET`/`POST /api/v1/agents/{id}/memories`，`DELETE /api/v1/agents/{id}/memories/{memory_id}` |
| 运行记录 | `GET /api/v1/agent-runs`，`GET /api/v1/agent-runs/{id}`，`GET /api/v1/agent-runs/{id}/trace`，`GET /api/v1/agent-runs/{id}/confirmations` |
| 工具确认 | `POST /api/v1/agent-runs/{id}/confirmations/{confirmation_id}` |

### 固定工具与本地 Provider

每个用户的工具目录会自动包含并分配给新 Agent：`current_time`（本地内置工具）以及
`search_web`、`read_url`（同 MCP STDIO 协议的内置 Web Search 服务）。无需填写 URL、API Key
或额外配置。原始示例中的 `simple-web-search-mcp` 当前不在 npm registry，因此仓库内置了无依赖替代实现，
避免 `npx` 启动阶段 404；工具名和 MCP `tools/list`/`tools/call` 行为保持一致。

工具运行时支持 `http`、远程 `mcp`、本地 `local` 命令和 `mcp_stdio` 四种 Provider。固定工具不能移除，
自定义本地工具使用无 shell 的 argv 调用并受超时、响应大小和既有确认/审计策略约束。
| 评估 | `GET /api/v1/evaluations/runs`，`GET /api/v1/evaluations/runs/{id}`，`GET /api/v1/evaluations/compare` |

## API 参考

### 认证

大多数端点需要 `?token=<token>` 查询参数。Token 在注册时返回。

---

### 用户

#### `POST /api/register`

注册新用户。

**请求体：**
```json
{
  "username": "alice",
  "public_key": "<base64 编码的 32 字节 Curve25519 公钥>"
}
```

**响应：** `200 OK`
```json
{
  "id": 1,
  "username": "alice",
  "token": "a1b2c3...64 个十六进制字符"
}
```

**错误：**
- `400` — 公钥长度无效（必须为 32 字节），或用户名已被占用/无效

#### `GET /api/users?token=xxx`

列出所有已注册用户。

**响应：** `200 OK`
```json
[
  { "id": 1, "username": "alice", "public_key": "<base64>", "created_at": "..." },
  { "id": 2, "username": "bob", "public_key": "<base64>", "created_at": "..." }
]
```

#### `GET /api/users/me?token=xxx`

获取当前用户信息。

#### `GET /api/users/:id?token=xxx`

按 ID 获取用户信息。

#### `GET /api/users/:id/public_key?token=xxx`

获取用户公钥（发起加密聊天前需要获取）。

**响应：**
```json
{ "user_id": 2, "public_key": "<base64>" }
```

---

### 群组

#### `POST /api/groups?token=xxx`

创建新群组。提供群组名称、成员 ID 以及每个成员的加密群密钥（用对应成员的公钥加密，base64 编码）。

**请求体：**
```json
{
  "name": "工程组",
  "member_ids": [1, 2, 3],
  "encrypted_group_keys": [
    "<成员1的base64加密密钥>",
    "<成员2的base64加密密钥>",
    "<成员3的base64加密密钥>"
  ]
}
```

**响应：**
```json
{ "group_id": 1, "name": "工程组" }
```

#### `GET /api/groups/list?token=xxx`

列出当前用户所属的所有群组。

#### `POST /api/groups/:id/join?token=xxx`

通过提供自己的加密群密钥加入已有群组。

**请求体：**
```json
{ "encrypted_key": "<base64>" }
```

#### `GET /api/groups/:id/members?token=xxx`

获取群成员及其各自的加密群密钥。

**响应：**
```json
[
  { "user_id": 1, "username": "alice", "encrypted_key": "<base64>" },
  { "user_id": 2, "username": "bob", "encrypted_key": "<base64>" }
]
```

---

### 消息历史

#### `GET /api/messages/:user_id?token=xxx&limit=50`

获取与指定用户的私聊消息历史。仅返回加密消息。

#### `GET /api/groups/:id/messages?token=xxx&limit=50`

获取群聊消息历史。调用者必须是群成员。

**参数：**
- `limit` — 最大返回条数（默认 50，上限 200）

---

### WebSocket

连接到 `ws://<host>:<port>/ws?token=<token>`

#### 发送消息

**私聊消息：**
```json
{
  "type": "private",
  "to_user_id": 2,
  "encrypted_content": "<base64>",
  "created_at": "2025-01-01T00:00:00.000Z"
}
```

**群聊消息：**
```json
{
  "type": "group",
  "group_id": 1,
  "encrypted_content": "<base64>",
  "created_at": "2025-01-01T00:00:00.000Z"
}
```

**心跳：**
```json
{ "type": "ping" }
```

#### 接收消息

**连接成功（加入时）：**
```json
{ "type": "connected", "user_id": 1, "username": "alice" }
```

**收到的消息：**
```json
{
  "type": "private",
  "message_id": 42,
  "from_user_id": 2,
  "from_username": "bob",
  "encrypted_content": "<base64>",
  "created_at": "2025-01-01T00:00:00.000Z"
}
```

**投递确认：**
```json
{
  "type": "ack",
  "message_id": 42,
  "to_user_id": 2,
  "delivered": true,
  "created_at": "2025-01-01T00:00:00.000Z"
}
```

**心跳响应：**
```json
{ "type": "pong" }
```

**错误：**
```json
{ "type": "error", "message": "..." }
```

## 加密模型

本服务器实现**零知识**架构：

1. **注册** — 客户端生成 Curve25519 密钥对；公钥注册到服务器。
2. **私聊** — 消息在发送前用接收者的公钥加密。只有接收者可以解密。
3. **群聊** — 创建者生成对称群密钥，用每个成员的公钥分别加密后分发。成员各自获取其加密的群密钥副本，解密后用于后续消息。
4. **服务器角色** — 服务器验证密钥格式（32 字节公钥），存储加密数据块，并转发消息。它**永远不会**持有私钥、明文，也无法解密任何内容。

## 项目结构

```
src/
├── main.rs          # 服务器入口，路由装配
├── config.rs        # 基于环境变量的配置
├── crypto.rs        # Token 生成，公钥验证
├── error.rs         # AppError → HTTP 响应映射
├── models.rs        # 数据模型（User、Message、Group、WebSocket 类型）
├── db.rs            # 数据库层（redb — 用户、消息、群组）
├── routes/
│   ├── users.rs     # /api/register, /api/users/*
│   ├── groups.rs    # /api/groups/*
│   └── messages.rs  # /api/messages/*（历史记录）
└── ws/
    ├── handler.rs   # WebSocket 升级 + 消息处理
    └── manager.rs   # 连接管理器 + 广播通道
```

## 依赖项

| Crate            | 用途             |
|------------------|------------------|
| `axum 0.7`       | HTTP + WebSocket 框架 |
| `tokio 1`        | 异步运行时         |
| `redb 2`         | 嵌入式键值数据库    |
| `serde` / `serde_json` | JSON 序列化  |
| `tower-http 0.5` | CORS 中间件        |
| `chrono 0.4`     | 时间戳             |
| `base64 0.22`    | 二进制与 JSON 之间的编解码 |
| `uuid 1`         | （可供后续使用）    |
| `rand 0.8`       | 随机 token 生成    |
| `tracing 0.1`    | 结构化日志         |

## 当前局限

- 无自动化测试
- 无速率限制
- 不支持消息编辑和删除
- 无输入状态提示及已读回执
- 不支持文件/附件
- 群成员查找采用全表扫描（未按用户索引）
- 认证 token 通过查询参数传递（应使用 `Authorization: Bearer` 头）
- 数据库使用单个 `Mutex`——写操作会阻塞读操作
- 消息历史无游标分页

## License

MIT
