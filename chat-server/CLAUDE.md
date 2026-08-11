# CLAUDE.md

本文件为 AI 助手（Claude 等）在此代码库中工作时提供指导。

## 项目概述

一个基于 **Axum**（HTTP 框架）+ **WebSocket** + **redb**（嵌入式键值数据库）构建的实时聊天服务器。服务器处理端到端加密消息——它永远不会看到明文消息内容。所有消息内容在传输前由客户端加密，并以不透明二进制 blob 的形式存储（在 JSON 中以 base64 编码）。

### 技术栈

- **Rust** edition 2021
- **Axum 0.7** — HTTP + WebSocket 框架
- **Tokio** — 异步运行时（full features）
- **redb 2** — 嵌入式、类型化键值存储（纯 Rust，无 SQL）
- **serde / serde_json** — 序列化
- **rand** — 随机 token 生成
- **chrono** — 时间戳
- **base64** — JSON 传输中的二进制编码
- **tower-http** — CORS 中间件
- **broadcast channels**（tokio::sync）— 向 WebSocket 连接投递消息

### Agent 平台

Agent 与 Task 不在 Rust 聊天服务的路由模块中。它们位于 `agent-service/`，由 FastAPI API、PostgreSQL、Redis Streams 和独立 Worker 构成；Rust 服务提供既有聊天能力和内部认证检查。生产入口由 `Caddyfile` 汇聚到 `9010`，并将 `/api/v1/agents*`、`/api/v1/agent-conversations*`、`/api/v1/agent-runs*`、`/api/v1/tools*`、`/api/v1/evaluations*`、`/api/v1/tasks*`、`/api/v1/task-dispatch-events*`、`/api/v1/notifications*`、`/api/v1/local-agent*`，以及 `/agent/ws*`、`/task/ws*`、`/local-agent/ws*` 代理到 Agent API。

阶段 B 包含受控 HTTP/MCP 工具、工具分配、长期记忆、运行工作台和评估读取接口。修改这些能力时，同步检查 `agent-service/app/main.py`、数据库迁移与存储层、`Caddyfile`、客户端 API 代理以及 `docs/agent-platform-design.md`；不能只更新其中一个服务。

Task 集群通过 FastAPI 的 Task 状态机执行，拥有独立上下文、预算、提出者和执行者。网关与前端只能展示脱敏调度摘要；不要在 Caddy 日志、通知或路由层引入任务正文、模型隐藏推理、模型密钥或绝对路径。新增 Task API 或 WebSocket 消息时，同步更新 `Caddyfile`、`chat-client/vite.config.js`、`TaskPanel.svelte`、Agent 测试和文档。

`local-agent/` 是独立 Node.js daemon/CLI，应部署在本机执行任务的电脑上，而不是 Agent API 容器。生产控制面必须经对外网关转发 `/api/v1/local-agent*` 和 `/local-agent/ws*`；不得要求暴露内部 `9011`。前端已支持在 Agent 配置中批准配对、选择同步工作区、绑定 `server_proxy` 或 `local_direct`，运行工作台会显示本机派发状态；但当前仅 `local_direct` 会被服务端派发到 daemon。CLI 的实际参数以 `local-agent/src/cli.js` 为准；修改 CLI、设备/工作区 API 或本地派发时，必须同步更新 `chat-client/src/views/HelpCenter.svelte`、两个仓库的 README 和本文件。文档必须区分 CLI 的本地 `ws_...` 工作区 ID 与网页的远端工作区 ID，且不得描述已上线前端能力为“未实现”或把 `server_proxy` 说成已可本机运行。

Local Agent 当前只执行文本模型运行。它不支持本机文件/进程工具、终端工具确认或断线恢复；不能将其描述为生产远程执行能力。模型 API Key 和绝对工作区路径只留在本机，不得记录、回显或提交。

## 架构

```
src/
├── main.rs          # 入口：tracing 初始化、配置、数据库、路由装配、监听
├── config.rs        # 基于环境变量的配置（HOST、PORT、DATA_DIR）
├── crypto.rs        # 随机 token 生成、公钥验证（Curve25519）
├── error.rs         # AppError 枚举 → HTTP 响应
├── models.rs        # 所有数据模型 + 序列化辅助函数
├── db.rs            # redb 数据库层 — 用户、消息、群组
├── routes/
│   ├── mod.rs
│   ├── users.rs     # POST /api/register, GET /api/users, /api/users/me, /api/users/:id
│   ├── groups.rs    # POST /api/groups, GET /api/groups/list, join, members
│   └── messages.rs  # GET /api/messages/:user_id（私聊历史）, /api/groups/:id/messages（群聊历史）
└── ws/
    ├── mod.rs
    ├── handler.rs   # WebSocket 升级处理 + 消息处理循环
    └── manager.rs   # ConnectionManager：在线状态跟踪 + 每个用户的广播通道
```

### 数据流

1. **注册**：客户端发送用户名 + Curve25519 公钥 → 服务器生成认证 token → 存储用户 + 公钥 + token 索引
2. **认证**：每个 REST/WS 请求携带 `?token=xxx` → 服务器在 `TOKEN_INDEX` 表中查找用户
3. **私聊消息**（WebSocket）：客户端发送 `{"type":"private","to_user_id":N,"encrypted_content":"<base64>"}` → 服务器存入数据库 → 通过广播通道转发给接收者（如果在线）→ 向发送者返回 `ack`
4. **群聊消息**（WebSocket）：客户端发送 `{"type":"group","group_id":N,"encrypted_content":"<base64>"}` → 服务器存入数据库 → 通过广播通道向所有群成员广播
5. **历史记录**：REST 端点返回已存储的加密消息；客户端本地解密

### WebSocket 消息类型

| type         | 方向    | 描述                          |
|-------------|--------|-------------------------------|
| `private`   | 入/出   | 一对一加密消息                   |
| `group`     | 入/出   | 群组加密消息（广播）              |
| `ack`       | 出     | 向发送者发送投递确认              |
| `ping`      | 入     | 客户端心跳                      |
| `pong`      | 出     | 心跳响应                        |
| `error`     | 出     | 错误响应                        |
| `connected` | 出     | WebSocket 连接建立时发送（含 user_id）|

## 关键设计决策

### 数据库模式（redb）

redb 使用类型化表定义。复杂值使用 JSON 序列化；简单索引使用原生类型。

| 表名              | 键类型                    | 值类型                         | 用途                    |
|------------------|--------------------------|-------------------------------|------------------------|
| `USERS`          | `u64`（user_id）          | JSON 字符串（User）             | 用户记录                 |
| `USERNAME_INDEX` | `&str`（username）        | `u64`（user_id）               | 用户名→ID 查找          |
| `TOKEN_INDEX`    | `&str`（token）           | `u64`（user_id）               | 认证 token→ID 查找      |
| `PRIVATE_MSGS`   | `u128`（conv_id<<32\|seq）| JSON 字符串（PrivateMessage）   | 私聊消息                 |
| `PRIVATE_SEQ`    | `u64`（conv_id）          | `u64`（seq）                   | 每个会话的序号            |
| `GROUPS`         | `u64`（group_id）         | JSON 字符串（Group）            | 群组记录                 |
| `GROUP_MEMBERS`  | `(u64, u64)`（group, user）| `&[u8]`（encrypted_key）      | 群组成员 + 每个成员的加密群密钥 |
| `GROUP_MSGS`     | `u128`（group_id<<32\|seq）| JSON 字符串（GroupMessage）     | 群聊消息                 |
| `GROUP_MSG_SEQ`  | `u64`（group_id）         | `u64`（seq）                   | 每个群的序号              |
| `NEXT_USER_ID`   | `u64`（0）                | `u64`                          | 自增用户 ID              |
| `GROUP_NEXT_ID`  | `u64`（0）                | `u64`                          | 自增群组 ID              |

### 私聊消息的会话键

```
conv_id = (min(user_a, user_b) << 32) | max(user_a, user_b)
msg_key = (conv_id as u128) << 32 | seq_id
```

这确保双方以确定性方式查询同一个会话。

### 认证模式

Token 通过查询参数 `?token=xxx` 在每个请求中传递。这种方式简单但不理想——token 会出现在服务器日志中。未来改进方向是使用 `Authorization: Bearer` 头。

### 连接管理器

使用 `tokio::sync::broadcast` 通道：
- 每个用户一个广播通道（首次连接时创建）
- 用户的每个 WebSocket 连接通过 `subscribe()` 获取一个 `Receiver`
- 通过调用 `tx.send()` 向用户发送消息——该用户的所有连接都会收到
- `user_online`/`user_offline` 方法仅跟踪在线状态，不注销通道（这样离线用户无需重新创建通道）

### 端到端加密模型

- 服务器仅存储**加密内容**（`Vec<u8>`）和加密的群密钥
- 公钥验证为 32 字节（Curve25519），但加密操作由客户端完成
- 群组加密：创建者生成对称群密钥，用每个成员的公钥分别加密，将所有加密副本发送给服务器
- 服务器永远无法访问明文或解密密钥

## 常见任务

### Agent 平台工作流

涉及 Agent 平台时，按以下顺序推进，并在 `docs/agent-platform-design.md` 的实施进度报告中更新已完成步骤及验证结果：

1. 编写或更新设计方案报告。
2. 按报告中的阶段和验收门禁实施。
3. 每完成一个步骤，更新报告中的进度、交付内容、验证结果和未完成风险。
4. 使用以下命令编译 Rust 静态发布二进制，并将 `target/x86_64-unknown-linux-musl/release/chat-server` 安装至后端项目根目录的 `chat-server`：

   ```bash
   export PATH="$HOME/.cargo/bin:$PATH" && cd /home/zhouzw/agentWorkCluster/chat-server && cargo build --release --target x86_64-unknown-linux-musl 2>&1
   install -m 755 target/x86_64-unknown-linux-musl/release/chat-server ./chat-server
   ```

5. 完成验证后，使用配置的 SSH Git 远端提交并推送 GitHub；报告提交哈希与推送结果。

6. Agent API 的路由新增或修改后，验证网关前缀仍指向 `agent-api:9011`。生产中看到阶段 B `404`，优先检查远端 `Caddyfile`、Agent API 镜像和前端是否来自同一版本；看到 run 创建 `500`，检查 `agent-api` 与 `agent-worker` 日志及模型、密钥、PostgreSQL、Redis 配置。

7. 工具保持最小授权：非 `GET`/`HEAD` HTTP 操作不能标记为 `read`；`write` 必须要求确认；`destructive` 必须 `per_call` 确认。不要记录、回显或提交模型密钥、Authorization、Cookie、提示词原文或未脱敏工具响应。

8. 修改帮助内容或 Local Agent CLI 后，检查 `chat-client/src/views/HelpCenter.svelte`、根目录 `README.md`、本文件和 `chat-client/README.md` 是否与当前 UI、`local-agent/src/cli.js` 的 usage 一致。

### 添加新的 REST 端点

1. 在相应的 `src/routes/*.rs` 文件中添加处理函数
2. 在模块的 `routes()` 函数中注册路由
3. 如果需要数据库访问，使用 `State<AppState>` 获取 `Arc<Database>`
4. 如果需要认证，使用 `Query<AuthQuery>` 提取 token

### 添加新的 WebSocket 消息类型

1. 在 [src/ws/handler.rs](src/ws/handler.rs) 的 `process_incoming()` 中添加分支
2. 如果需要，在 [src/models.rs](src/models.rs) 的 `WsIncoming`/`WsOutgoing` 中添加字段
3. 根据需要添加持久化和转发逻辑

### 添加新的数据库表

1. 在 [src/db.rs](src/db.rs) 中定义 `const TABLE: TableDefinition<K, V>`
2. 在 `Database::open()` 中打开/初始化该表
3. 在 `Database` 上添加相应的 CRUD 方法

### 运行服务器

```bash
# 默认：0.0.0.0:9010，./data/chat.db
cargo run

# 自定义：
HOST=127.0.0.1 PORT=8080 DATA_DIR=/var/lib/chat cargo run
```

### 编译静态发布文件

```bash
export PATH="$HOME/.cargo/bin:$PATH" && cd /home/zhouzw/agentWorkCluster/chat-server && cargo build --release --target x86_64-unknown-linux-musl 2>&1
```

### 测试

Rust 聊天服务的测试可按模块或集成测试补充。Agent 平台的回归门禁为：

```bash
cd /home/zhouzw/agentWorkCluster/agent-service
python -m unittest discover -s tests -p 'test_*.py' -v
```

Local Agent 的 Node 测试为：

```bash
cd /home/zhouzw/agentWorkCluster/local-agent
npm test
```

新增 Rust 测试时：
- 单元测试放在内联的 `#[cfg(test)] mod tests { ... }` 中
- 集成测试放在项目根目录的 `tests/` 目录中
- `redb` 数据库使用文件——测试应使用临时目录

## 已知问题和改进方向

1. **无自动化测试** — 所有验证都是手动的
2. **通过查询参数认证** — token 会泄露到日志中；应迁移到 Bearer 头
3. **群成员查找是 O(n)** — `get_user_groups()` 扫描所有 GROUP_MEMBERS 条目；应添加反向索引（`user_id → Vec<group_id>`）
4. **无速率限制** — 存在滥用风险
5. **无优雅关闭** — SIGTERM 时连接直接断开
6. **消息历史无游标分页** — 只有 `limit`；大对话需要获取所有消息
7. **无已读回执或输入状态提示** — 常见聊天功能未实现
8. **不支持消息编辑/删除** — 消息一旦存储不可变
9. **broadcast 通道上限 256** — 慢消费者会丢失消息（通道满时会丢弃旧消息）
10. **通过 Mutex 单线程访问数据库** — `redb` 需要独占写访问；写操作会阻塞并发读
11. **无 API 版本控制** — 所有端点都在裸 `/api/` 前缀下
12. **错误类型区分不足** — `AppError::Database` 和 `AppError::Internal` 都映射到 500
