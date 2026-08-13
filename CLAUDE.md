# AgentWorkCluster 产品架构、设计与技术栈

> 本文是仓库的系统级事实说明，面向产品、开发、运维和自动化编码助手。
> 它描述的是当前代码已实现的行为；专项细节以各组件的 `CLAUDE.md`、`README.md` 和 `chat-server/docs/` 为准。
> 修改跨组件能力时，必须同步检查 API、前端代理、Caddy、部署文件、迁移和测试。

## 1. 产品定位与边界

AgentWorkCluster 是一个由四个可独立演进的子系统组成的协作产品：

1. **私密即时聊天**：浏览器端加密私聊和群聊，Rust 服务只存储和转发密文。
2. **云端 Agent 平台**：用户配置模型、提示词、工具、记忆和运行策略；系统执行模型调用、工具治理、审计、确认和实时轨迹。
3. **Task 多 Agent 编排**：用受预算和所有权约束的 Task 协调多个 Agent，不把任务上下文当作普通会话混用。
4. **外部/本机执行接入**：QQ Gateway 将 QQ 事件接入 Agent；Local Agent daemon 将已配对设备上的本机文本模型接入控制面。

两个数据安全域必须严格区分：

| 域 | 内容 | 服务端可见性 | 主要保护方式 |
| --- | --- | --- | --- |
| 普通聊天 | 私聊、群聊消息及群密钥 | 不可见明文 | 浏览器 TweetNaCl 端到端加密，Rust 仅保存密文 |
| Agent/Task/QQ 运行 | 提示词、模型结果、工具输入输出、记忆、任务目标 | Agent 服务和已配置模型提供方可见 | 服务端 Fernet 静态加密、访问控制、脱敏审计、工具权限 |

不要宣称 Agent Run 是普通聊天端到端加密的一部分。QQ 消息也由 QQ 平台处理，不属于浏览器聊天 E2EE 域。

## 2. 总体架构

```text
                              Browser: Svelte 4 SPA
                    ordinary chat / Agent / Task / governance UI
                                      |
                        HTTPS REST + WebSocket, same origin
                                      v
                         Caddy (production public :9010)
                     /                              \
                    v                                v
       Rust Chat Server :9010                   Agent API :9011
       Axum + redb              FastAPI
       /api, /ws                /api/v1/agents... , /agent/ws, /task/ws
          |                         |        |              |
          v                         v        v              v
     encrypted chat DB        PostgreSQL   Redis        Local Agent daemon
     (redb; dev/prod)         (prod)     Streams/      (outbound WSS,
                                         PubSub          local Direct model)
                                      |
                         Cloud Worker (Python)
                                      |
                                OpenAI-compatible model

 QQ platform <-> QQ Gateway :9013 <-> Agent API :9011
                  SQLite                 private service API
```

### 2.1 运行形态

| 场景 | Chat Server | Agent API | Agent Worker | 数据库/消息队列 | 网关 |
| --- | --- | --- | --- | --- | --- |
| 本机轻量开发 | `127.0.0.1:9012` | `127.0.0.1:9011` | 无，API 进程内编排 | SQLite，无 Redis | Vite `:3000` 代理；可选 QQ `:9013` |
| Docker/生产 | 容器内部 `:9010` | 内部 `:9011` | 独立容器 | PostgreSQL + Redis | Caddy 是唯一公开 `:9010` 入口 |
| QQ（可选） | 不直接处理 QQ | 接收标准化 channel event | 执行普通云端 run | Gateway 自己的 SQLite | QQ Gateway 内部 `:9013` |

根目录 `start.sh` / `stop.sh` / `restart.sh` 仅用于本机联调，按顺序管理 Chat Server、可选 QQ Gateway、Vite。生产使用 `chat-server/docker-compose.yml` 或 `chat-server/deploy/systemd/`，不能与本机 PID 脚本混用。

## 3. 组件、技术栈与职责

| 目录/服务 | 技术栈 | 持久化 | 核心职责 |
| --- | --- | --- | --- |
| `chat-client/` | Svelte 4、Vite 5、JavaScript、TweetNaCl、lucide-svelte | 浏览器 localStorage / IndexedDB | 登录、E2EE 聊天、Agent/Task/工具治理 UI、WebSocket 客户端 |
| `chat-server/` | Rust 2021、Axum 0.7、Tokio、redb、bcrypt、tracing | `chat.db`（redb） | 用户/好友/群、普通聊天 REST/WS、聊天密文存储、Agent 用户身份 introspection |
| `agent-service/` | Python、FastAPI、uvicorn、httpx、cryptography、jsonschema、Alembic、psycopg、redis | PostgreSQL（生产）或 SQLite（开发） | Agent、Run、工具、记忆、Task、Local Agent 控制面、调度、WebSocket、审计 |
| `qq-gateway/` | Python、FastAPI、websockets、httpx、Fernet | `qq-gateway.db`（SQLite） | QQ OAuth/WS、事件去重、被动回复、群/成员登记、主动投递、QQ 凭据隔离 |
| `local-agent/` | Node.js 18+、ESM、`ws`、Node IPC/fs | `~/.local-agent` 私有文件 | 本机 daemon、设备凭据、本机工作区、本机模型密钥、文本模型运行 |
| `chat-server/Caddyfile` | Caddy 2 | 无 | 公网反代：聊天域、Agent/Task/Local Agent API 和 WS 路由；当前文件不托管 SPA 静态文件 |

### 3.1 前端：`chat-client/`

前端是单页应用，`App.svelte` 在同一个工作区中切换以下工作面：

| 视图/组件 | 面向用户的职责 | 后端通道 |
| --- | --- | --- |
| `Login.svelte` | 注册、登录、浏览器端私钥恢复 | Rust `/api/v1/register`、`/login` |
| `ChatRoom.svelte` | 私聊、群聊、附件、贴纸、投递状态 | Rust REST + `/ws` |
| `Contacts.svelte` | 搜索用户、好友请求、建群、加群、成员管理 | Rust `/api/v1/users*`、`/groups*` |
| `AgentCreate.svelte` | 创建云端 Agent | Agent API `/api/v1/agents` |
| `AgentChat.svelte` | 会话、Run 流式消息/推理/工具轨迹、状态栏、会话清空/彻底删除 | Agent REST + `/agent/ws` |
| `AgentSettings.svelte` | 模型、执行策略、记忆、QQ 连接、本机设备绑定 | Agent API |
| `AgentGovernance.svelte` | 工具目录、详细说明、JSON Schema、风险级别、确认模式、限频、启停与分配 | `/api/v1/tools*`、Agent 工具分配 API |
| `AgentRuns.svelte` | 运行列表、确认请求、审计时间线、评估比较 | `/api/v1/agent-runs*`、`/evaluations*` |
| `TaskPanel.svelte` | Task 状态、上下文、指派、结果、确认、Run 轨迹、通知 | `/api/v1/tasks*`、`/task/ws` |
| `HelpCenter.svelte` | 用户可操作说明和 Local Agent 接入说明 | 静态内容 |

**客户端加密设计**：`src/lib/crypto.js` 使用 Curve25519 `nacl.box` 加密私聊，使用随机 32 字节 `nacl.secretbox` 群密钥加密群消息；群密钥以各成员公钥分别加密。密码派生密钥用于加密保存浏览器私钥。消息、附件二进制和密钥在发送前加密，Rust 服务只见 Base64 密文。前端 Agent API 使用同源相对路径，避免浏览器直连内部 `9011`。

**时间显示**：Agent 状态栏、运行页和 Task 面板将 Agent 相关时间固定格式化为 `Asia/Shanghai`；普通聊天时间按客户端本地时间显示。浏览器本地时区不影响 Agent 的时间语义。

### 3.2 Rust 聊天服务：`chat-server/`

Rust 服务是普通聊天和统一用户身份的边界，不存储 Agent 运行内容。

| 模块 | 设计职责 |
| --- | --- |
| `src/main.rs` | 组装公开 REST、认证 REST、`/ws`、内部 introspection；处理 SIGINT/SIGTERM 优雅退出 |
| `src/auth.rs` | Bearer Token 优先、查询参数兼容的认证中间件；将用户放入请求扩展 |
| `src/ratelimit.rs` | 内存窗口限流，REST 每 token 100 请求/分钟；WS 有单独消息限制 |
| `src/routes/users.rs` | 注册/登录、用户资料、公钥、头像、好友关系与请求 |
| `src/routes/groups.rs` | 建群、成员密钥包、加入和成员管理 |
| `src/routes/messages.rs` | 私聊/群聊密文历史读取 |
| `src/ws/handler.rs` | 认证后实时收发私聊/群消息、长度与 MIME 校验、投递 ack |
| `src/ws/manager.rs` | 在线连接注册、按用户路由、广播投递 |
| `src/db.rs` | redb 表、索引、事务和聊天实体持久化 |
| `src/routes/internal.rs` | 只供 Agent API 使用的用户 token introspection |

`redb` 中保存用户、用户名/token 索引、bcrypt 密码哈希、浏览器加密的私钥备份、好友与群索引、密文消息和群成员加密群密钥。服务端不解密聊天正文。内部身份端点要求 `Authorization: Service <AGENT_SERVICE_SECRET>`，Agent API 用用户的 Bearer token 进行代查。

### 3.3 Agent 平台：`agent-service/`

`app/main.py` 同时承载 FastAPI 路由、`AgentStore`、编排器和事件 Hub；复杂性按模块拆分：

| 模块 | 设计职责 |
| --- | --- |
| `main.py` | API、认证、存储仓储、Run/Task 状态与编排、QQ/Local Agent 内部协议 |
| `harness.py` | OpenAI-compatible SSE 流解析、上下文预算、工具声明、HTTP/MCP/STDIO/local 执行、SSRF 网络后端 |
| `safety.py` | URL/DNS/peer 公网验证、脱敏、审计摘要、OpenAPI 约束 |
| `db.py` | SQLite/PostgreSQL 兼容 DB-API 层；命名参数适配与 Row 统一访问 |
| `worker.py` | Redis Streams 消费者；通过 consumer group 与 pending reclaim 执行云端 Run |
| `state_machine.py` | Run 状态机：`queued -> running -> waiting_confirmation -> completed/failed/cancelled` |
| `task_state_machine.py` | Task 状态机与允许迁移 |
| `evaluation.py` | 无 LLM 裁判的确定性评估、回归分类与发布门禁 |
| `web_search_mcp.py` | 内建 STDIO MCP：搜索与网页读取 |

#### Agent、会话与 Run 设计

1. Agent 包含模型连接、加密 API Key、系统提示词、温度、token/超时、运行策略、记忆开关、工具分配、执行目标和版本号。
2. 每次创建 Run 都冻结 Agent 版本、系统提示词、已分配工具及其私密配置为加密 `run_snapshots`，确保之后修改 Agent 不改变在途运行。
3. Run 创建会做 Agent 状态、最大并发、日/月 token 预算检查；用户消息以加密形式加入会话。
4. `prepare_context` 以 `context_window - max_output_tokens - tool declarations` 得到输入预算，保留最近历史、必要时截断系统提示词，并记录 context manifest。
5. 模型接口为 OpenAI-compatible `/chat/completions` 流式调用；解析 token、reasoning 和增量工具调用，实时写事件。
6. Run 轨迹以单 Run 单调 sequence 写入 `trace_events`，客户端断线后以 `after_sequence` 补发，Redis Pub/Sub 只用于低延迟，不作为可靠存储。
7. 用户可清空上下文（增加 epoch，保留审计），也可彻底删除非 Task 会话。彻底删除会清理会话消息、Run、快照、工具调用、确认、事件、状态、成员映射和会话范围记忆；存在活跃 Run 或 Task 关联时拒绝删除。

#### 北京时间、Agent Status 和 QQ 会话系统信息

所有 Agent 对模型暴露的时间语义为 **北京时间 `Asia/Shanghai` / `UTC+08:00`**：

- `current_time` 内置工具返回带 `+08:00` 的 ISO 时间和 `timezone=Asia/Shanghai`。
- `timer_create` 只接收 `+08:00` 的北京时间 ISO-8601；数据库内部转换为 UTC，调度循环以 UTC 比较，工具返回再转换为北京时间。
- 每一次模型调用前，`runtime_system_messages()` 都插入权威 system message：实时北京时间、会话 `agent_status` 时间及其更新时间，并明确禁止模型声称当前时间不可用。
- `agent_status` 保持最小内容，只含状态来源、北京时间和更新时间；界面也只显示这些内容。
- QQ 会话额外插入 system message：`provider=qq`、`scope_type`（`c2c` 或 `group`）、完整 scope OpenID，以及当前会话已观察成员的 `display name -> complete OpenID` 映射。成员映射仅在对应会话可见。

#### 内建工具与工具治理

工具是用户拥有、Agent 显式分配的能力；内建工具也不自动赋予每个 Agent。当前内建目录包括：

| 工具 | 类型/副作用 | 作用 |
| --- | --- | --- |
| `current_time` | local/read | 当前北京时间 |
| `search_web`、`read_url`、`web_fetch` | MCP STDIO/read | 网络搜索/公开网页读取 |
| `amap_weather` | HTTP/read | 高德天气 |
| `qq_list_groups`、`qq_list_group_members` | local/read | 当前 Agent 已登记 QQ 群和已观察成员 |
| `qq_send_group_message` | local/write | 向已登记 QQ 群主动发文本 |
| `qq_remind_group_member` | local/write | 主动 @ 已登记成员并发消息 |
| `timer_create` | local/write | 创建一次性北京时间定时 Run |

自定义工具支持 `http`、`openapi`、`mcp`（远程 HTTP）、`mcp_stdio`、`local`；Task 工具是受 Task 权限控制的内部类别。每个工具有输入 JSON Schema、`side_effect`（read/write/destructive）、确认模式、单 Run 限频、启用状态和提供方版本。治理规则如下：

- 非 `GET`/`HEAD` HTTP 操作不得标记为 read；自定义 write 必须确认；destructive 必须逐次确认。
- QQ 主动投递与一次性定时任务是受服务端实现和 JSON Schema 约束的内建自主工具，确认模式为 `none`，不应按普通 HTTP 写工具的规则复制到第三方工具。
- 运行时对 write/destructive 创建与参数哈希绑定的持久化确认；恢复时从加密 checkpoint 继续，不能替换参数。
- 只读工具仍受 JSON Schema、单 Run 限频和 Task 总工具预算约束。
- `tool_declarations()` 会过滤未满足确认治理的遗留/畸形写工具，模型看不到它们。
- HTTP/MCP 出站请求执行 HTTPS、URL、DNS、连接 IP 和实际 peer 的多层 SSRF 检查；禁止重定向和 Unix socket。
- 工具配置与原始结果不进入普通日志；用户可见轨迹使用脱敏摘要。

#### 记忆、评估和 Task 设计

**记忆**：可选长期记忆按 `agent`、`conversation`、`qq_user`、`qq_group` 范围隔离，按 preference/profile/constraint/fact/experience 分类。当前检索用关键词重叠和重要度，不依赖 embedding 服务。冲突信息不静默覆盖，而是标为 `conflicted`；过期和删除可审计。

**评估**：评估用例、结果和版本可持久化。断言只验证确定性条件（输出包含/不包含、状态、工具、错误类别、确认需求、上下文），不使用 LLM 充当裁判。发布门禁要求基线通过、安全案例不失败且 p95 延迟满足阈值。

**Task**：Task 是独立于普通会话的协作工作单元。它有 owner、proposer、根/父子关系、独立 context scope、预算快照、指派、handoff、结果和通知。Task Agent 只能通过 `post_progress`、`submit_result`、`accept/decline_assignment`、`delegate_task`、`collect_child_result` 等服务端 Task 工具改变状态。子任务只获得显式 handoff，不继承父任务完整上下文。预算限制 token、工具调用、并发、深度、子任务数；超限进入 `attention_required`，不会静默关闭。

#### 数据设计与加密

生产数据库由 Alembic 迁移管理；开发 SQLite 由 `AgentStore._migrate()` 建表和兼容升级。加表/加列必须同时更新迁移、SQLite schema、必要的 `_ensure_column` 和迁移脚本。

| 数据域 | 关键表 |
| --- | --- |
| Agent 运行 | `agents`、`agent_versions`、`conversations`、`messages`、`runs`、`run_snapshots`、`trace_events`、`agent_status` |
| 工具治理 | `tools`、`agent_tools`、`tool_invocations`、`tool_confirmations` |
| 记忆和评估 | `memory_items`、`evaluation_cases`、`evaluation_runs`、`evaluation_results` |
| Task | `tasks`、`task_context_events`、`task_dispatch_events`、`task_assignments`、`task_handoffs`、`task_results`、`notifications` |
| 可靠派发 | `outbox_events`、去重和 dispatch 表 |
| Local Agent | `pairing_sessions`、`local_agent_devices`、`local_workspaces`、`local_agent_models`、`local_run_dispatches` |
| QQ 会话上下文/定时 | `conversation_channel_identities`、`agent_schedules`、`channel_event_deduplications` |

系统提示词、模型 Key、会话消息、最终答复、Run snapshot、trace 原文、工具配置、记忆、Task 内容/结果、确认参数都使用 `AGENT_MASTER_KEY` 的 Fernet 加密。敏感列明文保持为空。审计展示使用 `redact()`、`audit_payload()` 与响应摘要，不记录 token、密码、API key、Cookie、原始提示词或完整工具响应。

### 3.4 QQ Gateway：`qq-gateway/`

QQ Gateway 是 QQ 官方 API 与 Agent API 的适配边界。QQ 凭据不进入 Agent 数据库；Gateway 使用自己的 SQLite 和 `QQ_GATEWAY_MASTER_KEY` 加密连接信息。

| 责任 | 实现方式 |
| --- | --- |
| QQ 连接管理 | 每 Agent 一份 QQ App 配置；Agent API 通过内部服务密钥配置、查询和断开 |
| OAuth 与 Gateway WS | 获取/刷新 access token，连接官方 Gateway，处理心跳、重连、会话恢复 |
| 事件标准化 | 将 C2C/群 @ 消息抽为 Agent API 的 `/internal/v1/channel-events`，携带 bot、event、sender、scope、完整 OpenID、名称和内容 |
| 幂等与回复窗口 | `inbox_events` 事件去重；被动回复关联 QQ event/message ID，并遵守平台被动回复窗口 |
| 群与成员发现 | `qq_groups` 与 `qq_group_members` 保存已观察群和成员 OpenID；仅登记过的实体可供自主工具选择 |
| 主动消息 | `proactive_outbound` 以 Agent + idempotency key 去重、保存状态/尝试/平台错误；主动请求不携带被动 `msg_id/event_id` |
| 可观测性 | 持久化投递状态及 QQ HTTP 状态/错误码/错误消息；结构化 `qq_proactive` 日志不得记录消息正文或凭据 |

QQ 群事件的 `author.member_openid` 是群内发送者标识，C2C 使用 `author.user_openid`；群标识为 `group_openid`。Gateway 将完整值传给 Agent API，后者写入会话 system info。OpenID 仅对该机器人相对稳定，不能猜测或截断。

**平台限制**：QQ 主动消息是否可发送由 QQ 平台能力和机器人审核状态决定。QQ 返回如 `40034105 主动消息失败, 无权限` 时，说明请求已到平台但平台拒绝，不能靠 Agent 重试、OpenID 修复或前端改动绕过。

### 3.5 Local Agent：`local-agent/`

Local Agent 将本机设备暴露为受控执行器，但控制面仍在 Agent API：

1. CLI 通过一次性 pairing ID + 六位码发起配对；网页批准后 daemon 得到设备 refresh credential。
2. daemon 使用短期设备 access token 主动连接 `/local-agent/ws`，服务端不会反向连接用户机器。
3. 工作区在本机 `realpath` 后注册；服务端仅保存展示名、能力和远端 ID，不保存绝对路径。
4. 本机状态目录 `~/.local-agent`、锁、socket、日志、模型和凭据要求目录 `0700`、文件 `0600`，并拒绝符号链接。
5. Web/CLI Run 通过本机 IPC 汇聚到一个 daemon；远端 Run 通过 lease claim、周期续租、单调事件 sequence 和 finish 上报。
6. `local_direct` 的模型 API Key 仅在本机加密保存，服务端只登记 Base URL 与模型 ID；`server_proxy` 是控制面可选项，但当前 daemon 不执行它。

**当前实际边界**：已实现的是本机直连模型的文本 Run。尚未实现本机文件/进程工具、终端工具确认、可靠断线恢复；因此它不是通用远程代码执行器。

## 4. 关键端到端数据流

### 4.1 普通加密聊天

```text
浏览器生成/恢复私钥
  -> 浏览器加密正文或群密钥
  -> Rust REST / WebSocket 接收密文
  -> redb 保存密文并按用户推送
  -> 收件浏览器用自己的私钥/群密钥解密
```

聊天服务验证 token、好友/群成员资格、消息大小和 MIME，但不解密正文。

### 4.2 云端 Agent Run

```text
浏览器创建 Run
  -> Agent API：认证、预算/并发检查、加密写 user message
  -> 冻结 run snapshot + 写 outbox (生产) 或进程内任务 (开发)
  -> Redis Stream -> Worker -> orchestrate_run
  -> 组装系统提示词、实时北京时间/QQ system info、记忆、预算内历史、工具声明
  -> 流式模型调用
  -> 工具：校验/限频/确认/执行/脱敏事件
  -> 加密保存 final result + usage + trace
  -> /agent/ws 推送；客户端可按 sequence 回放
```

生产 outbox 是可靠派发源。Redis 故障时 Run 保持 `queued` 供重试，不伪造成功或直接标失败。Worker 可以接管 pending Stream 消息。

### 4.3 QQ 入站、被动回复与自主投递

```text
QQ official Gateway event
  -> QQ Gateway：验签/去重/提取 scope 与 author OpenID/登记群成员
  -> Agent API /internal/v1/channel-events：定位或创建 QQ 会话、写 system info、创建 Run
  -> Agent Run：模型上下文中包含北京时间及 QQ scope/member map
  -> Gateway 轮询 channel run 结果
  -> 被动 QQ 回复（在平台窗口内）

定时 Run / Agent QQ tool
  -> Agent API -> QQ Gateway /internal/v1/qq/proactive-messages
  -> QQ platform主动消息接口
  -> proactive_outbound 保存 sent/failed 和平台错误
```

定时任务记录来源会话 ID；新建任务触发时复用来源会话，保留 QQ system info 与历史。旧任务没有来源会话才回退到标题为“自主定时任务”的新会话。

### 4.4 Task 协作

```text
用户或 Agent 创建 Task
  -> Task budget/context scope/assignment 持久化
  -> 创建隔离 Task conversation 和 Run
  -> 执行 Agent 仅经 Task tools 汇报、交付或委派
  -> child task 得到显式 handoff，不读取父上下文
  -> proposer 审核并关闭；失败/超限转 attention_required
```

### 4.5 Local Direct Run

```text
浏览器创建 local_direct Run
  -> Agent API 写 local_run_dispatch (不进入 Cloud Worker)
  -> 已连接 daemon 收到 offer，claim 90 秒 lease
  -> daemon 在已注册工作区内调用本机模型
  -> WSS run.event / run.finish
  -> Agent API 持久化轨迹和终态，浏览器照常通过 /agent/ws 观察
```

## 5. API、认证与网络边界

| 路由域 | 所有者 | 客户端认证 | 说明 |
| --- | --- | --- | --- |
| `/api/v1/register`、`/login`、`/users*`、`/groups*`、`/messages*` | Rust | 用户 Bearer（公开端点除外） | 普通聊天域 |
| `/ws` | Rust | WebSocket token | 普通聊天实时通道 |
| `/api/v1/agents*`、`/agent-conversations*`、`/agent-runs*`、`/tools*`、`/evaluations*`、`/tasks*`、`/notifications*`、`/local-agent*` | Agent API | 用户 Bearer，经 Rust introspection | Agent/Task 控制面 |
| `/agent/ws`、`/task/ws` | Agent API | token 查询参数 | Agent/Task 事件回放与订阅 |
| `/local-agent/ws` | Agent API | device access token Header | daemon 设备协议 |
| `/internal/v1/auth/introspect` | Rust | `Service` secret | 仅私网服务间认证 |
| `/internal/v1/channel-events`、`/channel-runs/*` | Agent API | `Service` secret | QQ Gateway 内部接口 |
| `/internal/v1/qq/*` | QQ Gateway | `Service` secret | Agent API 的 QQ 群/成员/投递调用 |

Caddy 和 Vite proxy 都必须同步新增 Agent API 前缀及 WebSocket 路径。浏览器不得把内部 9011/9013 直接暴露为公网依赖。

## 6. 安全、可靠性与可观测性设计

### 6.1 安全控制

- 凭据：不提交 `.agent.env`、数据库密码、Fernet key、模型 Key、QQ AppSecret、Service secret、设备 refresh token 或浏览器私钥。
- 用户身份：Agent API 不读取 Rust `chat.db`，而是用私网 service secret 调用 introspection；本机 daemon 使用独立设备 token。
- 数据最小化：QQ Gateway 不把完整 QQ 凭据转给 Agent；Local Agent 不把本机绝对路径和本机模型 Key 转给服务端。
- 工具最小授权：没有显式分配就没有工具；写入和破坏性操作有确认/参数绑定；出站 HTTP 有 SSRF 纵深防御。
- 日志：禁止记录明文用户/模型内容、密钥、token、Cookie。运行明细在加密库中保存，日志只保留脱敏摘要与关联 ID。
- 所有权与隔离：所有 Agent/Task/会话/记忆/本机设备 API 都以 owner 校验；Task 子任务上下文必须显式传递。

### 6.2 可靠性与恢复

- Run 使用状态机、不可变快照、outbox、Redis consumer group、pending reclaim 和持久化 trace sequence。
- WebSocket 断线不丢历史事件：客户端以 `after_sequence` 重放。
- 工具确认用加密 checkpoint，批准后只恢复对应工具调用；拒绝会取消 Run。
- QQ 入站和主动投递均有幂等键；`proactive_outbound` 保存完整投递状态以便排障。
- 计划任务是一次性任务：领取时原子设置 `last_triggered_at` 并禁用；避免重复触发。
- Local Agent 使用独占 daemon lock、IPC、WSS lease 和 sequence；但文件/进程工具与断线恢复尚未上线。

### 6.3 日志和排障位置

| 组件 | 本机日志/状态 | 优先排查内容 |
| --- | --- | --- |
| Chat Server | `chat-server/.runtime/chat-server.log` | Rust 启动、认证、普通 WS |
| Agent API | `chat-server/.runtime/agent-api.log` | 模型 API、Run 编排、迁移、内部鉴权 |
| Agent Worker | `chat-server/.runtime/agent-worker.log` | Redis Stream、pending reclaim、后台 Run |
| QQ Gateway | `qq-gateway/.runtime/qq-gateway.log` + `qq-gateway/data/qq-gateway.db` | QQ WS、事件、`proactive_outbound.last_error` |
| Vite | `chat-client/.runtime/vite.log` | 前端编译、代理 |
| Local Agent | `~/.local-agent/journal.jsonl` | daemon、设备、模型、本机 Run |

对于 QQ 主动投递，优先查询 `proactive_outbound`；HTTP 502 是 Gateway 给 Agent API 的包装状态，真正平台原因在 `last_error` 的 QQ HTTP code/message。对于时间问题，先检查运行时 system message 和 `current_time` 工具结果，不以服务器日志 UTC 时间戳误判系统时钟。

## 7. 部署、配置与运维

### 7.1 必要配置

| 变量 | 用途 |
| --- | --- |
| `AGENT_SERVICE_SECRET` | Rust、Agent API、QQ Gateway 间 service-to-service 认证 |
| `AGENT_MASTER_KEY` | Agent 数据库所有敏感字段的 Fernet 加密 |
| `AGENT_DATABASE_URL` 或 `PGHOST/...` | 生产 PostgreSQL 连接 |
| `REDIS_URL` | 生产 Run/Task 派发和跨进程实时事件 |
| `CHAT_AUTH_INTROSPECTION_URL` | Agent API 到 Rust 的私网用户认证端点 |
| `AMAP_WEATHER_API_KEY` | 可选内建天气工具密钥 |
| `QQ_GATEWAY_INTERNAL_URL` | Agent API 到 QQ Gateway 的私网地址 |
| `QQ_GATEWAY_MASTER_KEY` | Gateway QQ 连接凭据静态加密 |
| `QQ_*` | QQ 官方配置或默认回退配置；推荐在 Agent 设置中保存每 Agent 连接 |

`.agent.env` 权限应为 `0600`，生产数据库和 Redis 不公开监听。Compose 使用 Postgres 16、Redis 7.4 和 Caddy；Agent API 容器启动前执行 `alembic upgrade head`。无 Docker 生产部署使用 systemd unit 与同一套 Caddy 路由。

### 7.2 启停原则

- 先使用对应层级的脚本，不要手工杀不确定 PID。
- 本机脚本会检查 PID 对应命令、健康检查和端口；超过 5 秒不能优雅退出时才 SIGKILL，并应检查对应日志。
- 重启 Agent API 后，内建工具目录会同步更新描述、Schema 和配置；已分配 Agent 的**未来** Run 使用新快照，在途 Run 仍使用旧快照。
- 修改数据库 schema 时，先执行 migration，再滚动 API/Worker；不要在生产 Postgres 上依赖 SQLite 的 `_migrate()` DDL。

## 8. 测试与变更门禁

| 组件 | 命令 | 覆盖重点 |
| --- | --- | --- |
| Agent API | `cd agent-service && python3 -m unittest discover -s tests -p 'test_*.py' -v` | 加密、状态机、工具安全、QQ/时间、Task、Local Agent、评估 |
| QQ Gateway | `cd qq-gateway && python3 -m unittest tests.test_gateway` | 事件解析、投递、幂等、QQ payload |
| 前端 | `cd chat-client && npm test && npm run build` | 消息合并、Svelte 编译、代理可用性 |
| Local Agent | `cd local-agent && npm test` | IPC、权限、工作区边界、协议、日志 |
| Rust 聊天 | `cd chat-server && cargo test`；另有 `tests/test_chat.sh` | 用户、群、密文消息、WebSocket |

跨组件改动最低验证集合：前端构建、受影响的 Agent/QQ 单元测试、Caddy/Vite 路径、启动健康检查。增加表/字段时必须补 migration 和存储测试；改变工具权限时必须同时覆盖 API 校验、模型声明过滤、运行时确认/限频和安全测试。

## 9. 当前限制与非目标

- QQ 主动群消息依赖 QQ 官方平台权限；平台拒绝不能由本产品代码绕过。
- QQ “已观察成员”不是全量群成员目录，只来自本机器人已收到的事件；昵称可能重复，工具执行依赖完整 OpenID。
- 定时任务是一次性提醒，不是 Cron/周期任务；内部 UTC 存储是实现细节，用户/模型输入输出一律北京时间。
- Local Agent 当前只执行本机文本模型 Run，不支持文件、Git、终端或进程工具，不支持完整断线恢复。
- 普通聊天 E2EE 不覆盖 Agent、Task、QQ 或 Local Agent Run；这些内容会按 Agent 平台策略发送给模型和加密保存。
- Rust 聊天服务的 REST 限流是进程内实现；多副本部署如需全局限流，应在网关或共享存储层增强。
- `agent-service/app/main.py` 是当前集成中枢，结构性扩展应优先抽离清晰领域模块，同时保持 API、迁移和事件契约兼容。

## 10. 文档导航

- [根 README](README.md)：本地启动、整体使用入口。
- [Agent Service 指南](agent-service/CLAUDE.md)：Agent、Task、安全、迁移、Worker 的实现级约束。
- [Chat Server 指南](chat-server/CLAUDE.md)：Rust 聊天服务与部署边界。
- [Chat Client 指南](chat-client/CLAUDE.md)：前端实现约束。
- [Local Agent 指南](local-agent/CLAUDE.md)：daemon/CLI 实现约束。
- [Agent 平台设计](chat-server/docs/agent-platform-design.md)：阶段化产品设计基线。
- [自主 QQ Agent 设计](chat-server/docs/autonomous-qq-agent-design.md)：QQ 接入与自主能力设计。
- [Local Agent 设计](chat-server/docs/local-agent-design.md)：本机执行器设计与未来边界。
- [前端设计规范](chat-server/docs/frontend-design-spec.md)：界面信息架构与视觉约束。
