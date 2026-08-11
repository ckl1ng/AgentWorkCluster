# Agent 平台完整设计方案

> 状态：v2 提案，待实现确认
>
> 日期：2026-07-23
>
> 范围：在现有聊天产品中建设可评估、可审计、可治理的 Agent Harness。首期交付私有单 Agent；群聊、代码执行和多 Agent 均受后续阶段门禁约束。

> 多 Agent、任务所有权、上下文隔离、右侧任务工作台、Local coding Agent 与 Hook 的具体交付顺序见 [Agent 集群与任务编排分阶段执行方案](./agent-cluster-execution-plan.md)。

---

## 1. 执行摘要

本平台不是“在聊天界面接入一个模型”，而是一个以 **Harness** 为核心的 Agent 执行平台：

```text
Agent = Model + Harness
Harness = Context + Policy + Tool Runtime + State + Trace + Evaluation
```

模型可替换；Harness 决定系统是否可靠、安全、可解释和可迭代。所有模型调用均经过同一套上下文组装、权限校验、预算、工具运行、状态管理、事件记录和评估机制。平台的产品边界是：用户主动提交的任务由 Agent 执行，系统不会隐式读取端到端加密私聊或群聊内容。

首要工作不是扩展模型、MCP、代码执行或多 Agent，而是完成以下闭环：

1. 单 Agent 可在可恢复的队列上稳定运行，实时事件跨进程可靠送达。
2. 运行绑定不可变配置、上下文、预算和工具快照，所有状态均可追溯。
3. 工具执行具备语义化副作用控制、SSRF 防护、确认、限额和审计。
4. 每个可发布的 Agent 版本均可通过评估集验证，不以主观试聊作为上线标准。

## 2. 决策摘要

| 主题 | 决策 |
|---|---|
| 平台中心 | Harness 是独立、版本化、可评估的运行时层；模型只是可替换适配器 |
| 首期产品 | 私有单 Agent、文本输入、OpenAI Chat Completions 兼容模型、受控 HTTP 工具 |
| 后端 | Python 3.12、FastAPI、独立 Worker、PostgreSQL 16、Redis 7 |
| 聊天服务 | 继续由 Rust + Axum + redb 负责认证、E2EE 私聊/群聊和在线状态 |
| 持久化 | PostgreSQL 是 Agent 运行事实来源；Redis 是队列、控制和实时分发层 |
| 评估 | 阶段 0 即建立评估集、回归门禁和 Harness/模型对比；无评估不发布 |
| 上下文 | 使用结构化状态栏、预算化组装、滑动窗口和可版本化摘要；禁止无限拼接历史 |
| 工具 | 统一 `ToolProvider` 抽象；支持 HTTP、远程 MCP、本地命令和 MCP STDIO；所有工具按副作用分级 |
| MCP | 远程 MCP 与本地 STDIO 均经过同一发现、授权、Schema、确认和审计链路；本地进程不使用 shell，并受超时和响应上限约束 |
| 代码执行 | 非首期功能；仅在隔离运行环境、审计和评估通过后以默认关闭的可选能力开放 |
| 记忆 | 默认关闭；只存经策略筛选的事实，支持置信度、冲突、衰减和删除 |
| 群聊 | 仅显式 `@Agent` 触发，浏览器选择性解密并提交上下文；Agent 服务没有群密钥 |
| 多 Agent | 最后实现；先 Pipeline，再 Supervisor；默认传递结构化结果而不是共享完整轨迹 |
| 对外端口 | 生产仅网关暴露 `9010`；Agent、数据库、Redis 不向主机发布端口 |

## 3. 目标、非目标与不变量

### 3.1 目标

1. 用户可创建私有 Agent，配置模型、提示词、技能、工具和运行策略，保存后立即运行。
2. 每个运行流式返回最终回复和脱敏的执行过程，并可从序号精确恢复。
3. 每次运行可复现其配置版本、工具版本、上下文来源、预算判断与关键状态转换。
4. 平台可对模型、提示词、上下文策略和 Harness 版本做可重复评估与回归比较。
5. 后续可在不破坏既有 E2EE 聊天承诺的前提下接入群聊 Agent。

### 3.2 首期非目标

1. 不支持任意宿主机 Shell、文件系统写入、浏览器自动化或无沙箱代码执行。
2. 不支持自动读取 E2EE 群聊历史、自动回复每条群消息或向 Agent 授予群密钥。
3. 不支持多 Agent 自动委派、辩论、群聊或自修改配置。
4. 不承诺统一模型厂商私有扩展；首期以 OpenAI Chat Completions 兼容协议为准。
5. 不以“隐藏思维链”作为产品输出；只显示可审计的编排步骤、工具调用与公开摘要。

### 3.3 不可放宽的约束

1. 原有私聊与群聊继续浏览器端到端加密，Rust 服务和 Agent 服务均不持有其明文或群密钥。
2. 模型 Key、工具凭据和 MCP 凭据只能在服务端加密保存与使用，绝不回传浏览器或写入日志。
3. Agent 只处理用户主动提交的输入、用户明确选择的上下文和授权工具的结果。
4. 所有运行均绑定不可变快照；配置更新不能改变已排队或执行中的运行。
5. 工具执行前后均执行策略检查；任何失败、取消或超限必须留下脱敏审计事件。
6. “可用”必须由评估和验收门禁定义，而非仅以构建成功或演示成功判定。

## 4. 总体架构

### 4.1 服务边界

```text
Browser
  | HTTPS / WSS :9010
  v
+------------------------ Gateway ------------------------+
| /api/v1/*, /ws             /api/v1/agents/*, /agent/ws |
+-------------------+--------------------+----------------+
                    |                    |
          +---------v--------+  +--------v----------------+
          | Rust Chat Server |  | Agent API                |
          | auth / E2EE chat |  | auth adapter / REST / WS |
          +---------+--------+  +--------+----------------+
                    |                    |
                    | introspection      | PostgreSQL read/write
                    |                    v
                    |         +----------+----------+
                    |         | Agent Harness        |
                    |         | Worker(s)            |
                    |         +--+-------+--------+--+
                    |            |       |        |
                    v            v       v        v
                 redb       Redis    Model API  Tool/MCP
                         streams/pubsub
                              |
                         PostgreSQL + pgvector
```

外部仅暴露网关 `9010`。Rust、Agent API、Worker、PostgreSQL 和 Redis 均通过容器网络通信。开发环境可由 Vite 将 Agent REST 与 WebSocket 代理至本地 Agent API。

### 4.2 Harness 组件

| 组件 | 职责 | 必须可版本化/评估的内容 |
|---|---|---|
| `ContextManager` | 组装系统提示词、状态栏、技能、记忆、摘要、历史和工具说明 | 组装顺序、裁剪和摘要策略 |
| `PolicyEngine` | 权限、并发、token/费用、工具确认、取消、速率和数据策略 | 策略版本、命中规则、决策理由 |
| `ModelAdapter` | 请求映射、流式解析、tool call 合并、usage 与错误标准化 | 适配器版本、模型参数 |
| `ToolRuntime` | 工具发现、参数校验、审批、网络执行、结果截断和重试 | Provider、工具版本、执行策略 |
| `RunStateMachine` | 管理运行、步骤、工具调用、确认与终态 | 状态转换与幂等键 |
| `TraceService` | 生成脱敏事件、事件序号、持久化和实时扇出 | 事件 schema、脱敏版本 |
| `EvaluationService` | 离线/预发布评估、回归比较、失败归因 | 用例集、判定器、阈值 |

Harness 版本与 Agent 配置版本不同：前者是平台发布版本，后者是创建者配置。每个运行保存二者，便于区分“模型退化、配置变更或平台逻辑回归”。

### 4.3 认证集成

浏览器沿用聊天 token。Agent API 仅通过 Rust 的内部接口解析身份：

```text
POST /internal/v1/auth/introspect
Authorization: Service <agent-service-secret>
Body: { "user_token": "..." }
Response: { "user_id": 42, "username": "alice", "active": true }
```

内部接口只在容器网络注册，并校验服务密钥。Agent 数据库不复制聊天 token、用户凭据或群密钥。

## 5. 运行模型

### 5.1 运行状态机

```text
queued -> running -> waiting_confirmation -> running -> completed
   |        |                 |                 |
   |        +-----------------+-----------------+--> failed
   +---------------------------------------------> cancelled
```

状态转换只能由 Worker 在数据库事务中提交。用户取消可从 `queued`、`running` 或 `waiting_confirmation` 进入 `cancelled`；确认拒绝会以结构化工具结果回填模型或结束运行，取决于策略。每个转换携带 `transition_id`，以 `run_id + transition_id` 幂等。

### 5.2 单次运行循环

```text
1. API 鉴权并验证 Agent 可见性、状态和提交速率。
2. 在同一 PostgreSQL 事务创建 run、用户消息、不可变快照和 outbox 记录。
3. 发布器将 outbox 投递到 Redis Stream；Worker 按 run_id 幂等认领。
4. Worker 读取快照，PolicyEngine 预检并组装上下文。
5. 调用模型，持久化文本增量、usage 和公开步骤摘要。
6. 若模型请求工具：校验工具、参数、预算与确认策略。
7. 执行工具或等待确认；将结构化结果作为 tool message 回填模型。
8. 达到最终回复、取消、预算/轮数上限或不可恢复错误时写入终态。
9. TraceService 将已提交事件发布到 Redis；API WebSocket 网关扇出给订阅者。
```

模型、工具和外部网络不参与数据库事务。外部副作用无法回滚，因此写工具必须拥有调用幂等键、显式确认和结果状态。Worker 崩溃后的恢复从最后持久化步骤继续；非幂等工具不会自动重放。

### 5.3 结构化状态栏

每次模型请求固定注入短小、由程序维护的状态栏，避免依赖模型“记住”已做过什么：

```json
{
  "run_goal": "查询上海天气并给出穿衣建议",
  "completed": ["已获得上海未来三天天气"],
  "facts": [{"key":"weather.source","value":"weather_lookup","confidence":"tool"}],
  "pending_confirmation": null,
  "remaining": {"tool_calls": 4, "output_tokens": 1400, "cost": "unknown"},
  "constraints": ["不得执行写操作，除非用户确认"]
}
```

状态栏只包含可验证事实、运行限制和工作进度，不保存隐藏推理。它由状态机和工具结果更新，并在 trace 中记录变更摘要。

## 6. 上下文、技能与记忆

### 6.1 上下文组装与预算

上下文按稳定顺序组装，前缀尽量固定以利于支持前缀缓存的供应商；每层都有 token 上限和可解释的裁剪原因：

```text
核心系统提示词与安全约束
-> 固定状态栏
-> 本轮命中的技能详情
-> 已授权且高相关的长期记忆
-> 当前 epoch 的会话摘要
-> 最近原始消息
-> 本轮用户输入
-> 可调用工具声明
```

上下文策略是 Agent 版本的一部分，至少包括：模型上下文窗口、各层 token 配额、滑动窗口大小、摘要触发阈值、摘要模型、技能触发规则及记忆检索阈值。组装器输出 `context_manifest`，记录各来源的数量、token、裁剪和摘要版本，不保存不必要的原文副本。

### 6.2 压缩与上下文腐化控制

首期实现以下策略，不引入不可验证的“自动智能压缩”：

| 策略 | 用途 | 触发条件 |
|---|---|---|
| 滑动窗口 | 保留最近的原始互动 | 原始历史超出配额 |
| 版本化摘要 | 压缩稳定历史结论 | 超过消息数或 token 阈值 |
| 重要度筛选 | 优先保留带工具结果、用户约束和未完成任务的内容 | 摘要仍超预算 |
| 显式清空 | 创建新的上下文 epoch | 用户确认 |

每次压缩均记录输入范围、摘要版本和保留事实。系统监控“有效信息密度”：被引用的历史/记忆比例、重复内容比例、摘要失败率和上下文 token 占用。超过阈值时优先触发摘要；只有用户决定是否清空上下文。

### 6.3 技能

技能是可审核的提示词包，不是可执行代码。一个技能包含目录项、触发规则、详细内容、版本和 token 上限。模型只能看到技能目录；`ContextManager` 根据用户任务、当前状态和显式选择注入少数详情。技能更新创建新 Agent 版本。

### 6.4 长期记忆

长期记忆在阶段 B 后默认关闭并显式授权。记忆只保存经策略筛选、未来可能有用且不含敏感数据的事实，不保存完整对话或隐藏推理。

```text
memory_item
  id UUID PK
  owner_user_id BIGINT
  agent_id UUID NULL
  scope ENUM(user, agent, group)
  kind ENUM(preference, profile, constraint, fact, experience)
  content_encrypted BYTEA
  embedding vector(...)
  source_confidence ENUM(user, tool, inferred, imported)
  importance SMALLINT
  access_count BIGINT
  conflict_state ENUM(active, superseded, conflicted, deleted)
  source_message_id UUID
  expires_at / last_accessed_at / created_at
```

检索先做权限过滤，再做混合召回和重排序，最后受 token 预算裁剪。冲突事实不得静默覆盖：用户声明优先，工具事实带时间戳；其他冲突进入待确认或保留历史状态。过期、低重要度且长期未访问的记忆按保留策略删除。记忆的写入、读取和淘汰均生成脱敏 trace 摘要。

## 7. 模型适配器

### 7.1 首期协议

首期只支持 OpenAI Chat Completions 兼容 API：

```http
POST {base_url}/chat/completions
Authorization: Bearer {decrypted_api_key}
Content-Type: application/json
```

连接保存前以不含用户内容的请求验证连通性、TLS 和协议响应。模型连接是独立资源，密钥和额外请求头应用层加密；API 响应只返回 `api_key_configured` 状态。

### 7.2 请求和流式协议

适配器负责 SSE 分段解析、`delta.content` 合并、分段 `tool_calls` 参数合并、finish reason 和 usage 标准化。平台事件永不直接透传供应商原始 payload。请求包含模型、参数、组装后的 messages 以及已授权工具的 OpenAI function schema。

```json
{
  "model": "configured-model",
  "stream": true,
  "messages": ["..."],
  "tools": ["..."],
  "temperature": 0.4,
  "max_tokens": 2048
}
```

适配器必须在模型流结束时捕获 usage；供应商未返回 usage 时记录 `unknown`，禁止推测费用。模型协议、超时、限流和无效工具调用均归类为结构化错误，供运行和评估使用。

## 8. 工具与 MCP

### 8.1 统一工具抽象

所有工具经统一接口暴露给 Harness：

```text
ToolProvider.discover(connection) -> ToolDescriptor[]
ToolProvider.invoke(invocation, execution_context) -> ToolResult
```

`ToolDescriptor` 包含名称、描述、JSON Schema、副作用级别、认证范围、超时、输出限制和 provider 版本。`ToolResult` 包含脱敏摘要、机器可读内容、状态、耗时、重试信息和外部调用标识。模型只能使用 Agent 版本中固定的 descriptor 快照。

### 8.2 工具类型与开放顺序

| 类型 | 阶段 | 说明 |
|---|---|---|
| 手工 HTTP | A | 明确 URL 模板、认证、Schema 和响应提取规则 |
| OpenAPI 导入 | B | 导入为候选工具，创建者逐个确认；不自动公开全部 operation |
| 远程 MCP | B | 仅经受控网络连接的 SSE/Streamable HTTP；自动发现后仍需逐项授权 |
| `stdio` MCP | D | 仅允许显式配置或固定内置服务器；无 shell 启动，视为代码执行能力并受运行时上限约束 |
| 代码执行 | D | 默认关闭的受控工具，不是普通 HTTP 工具 |

MCP 是标准化工具适配器，不是绕过策略层的旁路。MCP 的 `tools/list` 只生成候选工具；每个工具仍需副作用分类、权限确认、输入 Schema 检查、速率限制、脱敏和审计。

### 8.3 工具副作用和确认

工具的确认策略首先依据业务语义，而非 HTTP 方法：

| 副作用级别 | 示例 | 默认策略 |
|---|---|---|
| `read` | 查询天气、读取工单 | 可自动执行，受频率/预算限制 |
| `write` | 创建草稿、更新记录 | 每 run 或每 call 确认，由创建者收紧 |
| `destructive` | 删除、支付、发送外部通知 | 每 call 明确确认，不允许降低 |

确认事件展示工具名称、脱敏参数、影响摘要、目标系统和不可逆提示。确认只对指定 `run_id + tool_call_id + arguments_hash` 有效，修改参数后必须重新确认。

### 8.4 HTTP 工具安全

1. 仅允许公网 HTTPS 域名；开发环境可显式打开 HTTP。
2. 初始 URL、每一次重定向、DNS 解析结果和最终连接地址都必须校验，拒绝私网、回环、链路本地、保留地址、元数据地址、Unix socket、`localhost` 与 `.local`。
3. 禁止自动携带服务端 Cookie、代理环境变量和非工具专属 Authorization。
4. 默认连接超时 10 秒、总超时 30 秒、响应体 1 MiB；按工具和 Agent 限制调用次数与并发。
5. 参数以 JSON Schema 校验；路径、查询、请求体的映射规则显式配置，禁止任意模板执行。
6. 工具错误转换为结构化 `tool_error` 回填模型一次；连续修复失败或超出轮数时结束运行。

### 8.5 代码执行门禁

代码执行只有在以下条件全部满足后才能进入有限 Beta：独立容器或 gVisor 沙箱、无宿主机挂载、只读基础镜像、最小依赖白名单、默认无网络或仅允许显式域名、CPU/内存/磁盘/时限、命令审计、恶意样本评估和管理员级开关。Agent 生成的代码、文件和结果均按敏感数据策略保存与脱敏。代码不得自动注册为长期工具，必须经过测试、人工审批和版本化发布。

## 9. 数据模型与数据保护

### 9.1 配置和权限

```text
agent
  id UUID PK
  owner_user_id BIGINT
  name / avatar_url / description
  state ENUM(draft, active, paused, archived)
  current_version_id UUID
  created_at / updated_at / archived_at

agent_version
  id UUID PK
  agent_id UUID FK
  version INT
  harness_version TEXT
  system_prompt_encrypted BYTEA
  skills_catalog JSONB
  context_policy JSONB
  run_policy JSONB
  model_connection_id UUID
  created_by_user_id BIGINT
  created_at

model_connection
  id UUID PK
  owner_user_id BIGINT
  display_name / base_url / model_id
  encrypted_api_key BYTEA
  encrypted_extra_headers BYTEA
  default_params JSONB
  status ENUM(active, invalid, disabled)

tool_connection
  id UUID PK
  owner_user_id BIGINT
  provider ENUM(http, openapi, mcp_remote, mcp_stdio, code_sandbox)
  encrypted_config BYTEA
  status ENUM(active, invalid, disabled)

tool_definition
  id UUID PK
  owner_user_id BIGINT
  connection_id UUID FK
  name / description / input_schema JSONB
  side_effect_level ENUM(read, write, destructive)
  confirmation_mode ENUM(none, per_run, per_call)
  output_policy JSONB
  enabled BOOLEAN

agent_tool
  agent_version_id UUID FK
  tool_id UUID FK
  alias VARCHAR(64)
```

私有 Agent 只允许 owner 查看、运行、编辑、暂停、归档和读取完整轨迹。任何未来的 participant 权限只能获得已脱敏的群内运行数据，不可见系统提示词、模型连接、工具认证、密钥或费用信息。

### 9.2 会话、运行与审计

```text
agent_conversation
  id UUID PK
  agent_id UUID FK
  owner_user_id BIGINT
  source ENUM(private, group)
  source_group_id BIGINT NULL
  context_epoch INT
  title / deleted_at / created_at / updated_at

agent_message
  id UUID PK
  conversation_id UUID FK
  run_id UUID NULL
  role ENUM(user, assistant, tool, system_summary)
  content_encrypted BYTEA
  context_epoch INT
  created_at

agent_run
  id UUID PK
  conversation_id UUID FK
  agent_id UUID FK
  agent_version_id UUID FK
  initiated_by_user_id BIGINT
  state ENUM(queued, running, waiting_confirmation, completed, failed, cancelled)
  snapshot_encrypted BYTEA
  context_manifest_encrypted BYTEA
  model_usage JSONB / cost_snapshot JSONB
  error_code / error_summary_redacted
  started_at / completed_at

agent_trace_event
  id UUID PK
  run_id UUID FK
  sequence BIGINT
  type VARCHAR(80)
  payload_encrypted BYTEA
  redacted_payload JSONB
  created_at

outbox_event
  id UUID PK
  aggregate_type / aggregate_id / event_type
  payload JSONB
  published_at / created_at
```

运行数据的原文使用列级加密；前端和 WebSocket 只读取 `redacted_payload`。日志默认不记录提示词、模型原文、工具敏感响应、Authorization、Cookie 或私钥。数据保留策略按类型执行：用户删除会话时删除可见消息、运行和轨迹；审计需要保留的最小元数据不含可恢复原文。

### 9.3 密钥管理

开发环境可用可轮换的主密钥进行信封加密；生产使用 KMS/Vault 管理数据密钥和密钥轮换。数据库备份、对象存储和监控导出同样必须加密。密钥轮换不应要求重新输入每个模型 Key，并必须可审计。

## 10. 事件、API 与前端

### 10.1 REST API

所有 Agent REST API 位于 `/api/v1/agents`、`/api/v1/agent-conversations`、`/api/v1/agent-runs` 和 `/api/v1/tools`，使用现有 Bearer token 并经内部认证适配器鉴权。核心端点：

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/agents` | 创建 Agent 和首个不可变版本 |
| `GET` / `PUT` | `/agents/{id}` | 读取或创建新配置版本，仅 owner |
| `POST` | `/agents/{id}/pause`、`/resume` | 生命周期控制，仅 owner |
| `DELETE` | `/agents/{id}` | 归档；异步执行保留策略 |
| `POST` | `/agents/{id}/conversations` | 创建会话 |
| `POST` | `/agent-conversations/{id}/runs` | 提交运行并返回 `run_id` |
| `POST` | `/agent-runs/{id}/cancel` | 取消本人运行；owner 可取消其 Agent 的全部运行 |
| `POST` | `/agent-conversations/{id}/clear-context` | 递增 context epoch |
| `GET` | `/agent-runs/{id}/trace` | 拉取可见脱敏事件 |
| `POST` | `/tools`、`/tools/{id}/validate` | 创建及无副作用预校验工具 |
| `POST` | `/tool-confirmations/{id}` | 对指定工具调用确认或拒绝 |
| `POST` | `/evaluations` | 创建预发布评估任务，仅 owner/管理员 |

创建或更新响应绝不返回密钥、未脱敏配置或未授权记忆内容。所有写操作接受 `Idempotency-Key`，避免浏览器重试创建重复 Agent 或重复运行。

### 10.2 WebSocket 事件

```text
wss://{host}:9010/agent/ws?token={existing_chat_token}
```

客户端订阅：

```json
{"type":"agent.subscribe","run_id":"run-uuid","after_sequence":12}
```

服务端事件统一格式：

```json
{
  "type": "agent.tool.completed",
  "run_id": "run-uuid",
  "sequence": 13,
  "timestamp": "2026-07-23T08:00:00Z",
  "payload": {"tool_name":"weather_lookup","status":"success","result_summary":"返回 3 条天气记录"}
}
```

事件类型包含 `agent.run.queued`、`agent.run.started`、`agent.context.prepared`、`agent.state.updated`、`agent.message.delta`、`agent.tool.requested`、`agent.tool.confirmation_required`、`agent.tool.started`、`agent.tool.completed`、`agent.tool.failed`、`agent.run.completed`、`agent.run.failed` 与 `agent.run.cancelled`。`sequence` 按 run 单调递增；网关先从 PostgreSQL 补发，再订阅 Redis 实时事件，并以 sequence 去重，确保多进程和重连不丢事件。

### 10.3 前端工作台

创建者拥有三个视图：

1. **运行**：Markdown 最终回复、默认折叠的执行过程、工具确认、取消、清空上下文和新建会话。
2. **配置**：基础信息、模型连接、角色-目标-约束-输出格式提示词、技能、工具、上下文策略和预算。保存产生新版本，不能原地覆盖历史。
3. **运行记录与评估**：按版本查看成功率、延迟、用量、错误类别、工具失败率和评估集结果。

所有 Markdown 使用受控渲染器，禁用危险 HTML。执行过程只呈现脱敏公开步骤，不呈现模型隐藏推理。未来 participant 只能看到授权群聊运行视图和轨迹。

## 11. 可靠性、并发与成本

### 11.1 队列与实时分发

API 在写入 `agent_run` 的同一事务写入 `outbox_event`。发布器幂等写入 Redis Stream `agent-runs`；Worker consumer group 消费并通过 `XAUTOCLAIM` 认领超时 pending 消息。Worker 提交 trace 后写入 `agent-events`，Agent API 订阅并将事件推给 WebSocket。Redis 不是真相来源；断线恢复始终从 PostgreSQL trace 重建。

### 11.2 重试与幂等

模型调用可按错误类型有限重试。`read` 工具可在安全条件下重试；`write` 和 `destructive` 工具仅在提供外部幂等键且未产生确定结果时允许人工重试。运行按步骤保存 checkpoint，避免 Worker 重启后重复执行已提交工具调用。达到最大尝试次数后标记 `failed` 并显示可理解错误。

### 11.3 取消、暂停和限额

取消标记写数据库并广播控制事件。Worker 在模型流分段、工具调用前后检查取消；第三方写操作已发出时只能记录“取消请求已发送，外部结果未知”。暂停 Agent 时拒绝新 run、取消未启动任务、请求运行中 Worker 在安全边界停止。

每个 Agent 版本至少限制：最大模型轮数、最大工具调用数、输入/输出 token、单 run 成本、最大并发、每日/月度成本和工具频率。预算在每次模型与工具调用前预留，调用后根据 usage 结算；供应商不返回价格时成本标为 `unknown` 并仍执行 token/请求限制。

## 12. 评估与发布门禁

### 12.1 阶段 0：评估基础设施

评估早于功能扩展建立。每个 Agent 至少维护 20 个典型任务，覆盖正常任务、无工具回答、读工具、写工具拒绝、确认、超时、无效参数、取消、长上下文和预算超限。高风险 Agent 应扩展至 50 个以上并按真实失败案例回填。

```text
evaluation_case
  id / agent_id / version_range
  input_encrypted / selected_context_encrypted
  expected_assertions JSONB
  tags JSONB

evaluation_run
  id / harness_version / agent_version_id / model_connection_id
  aggregate_metrics JSONB / started_at / completed_at

evaluation_result
  evaluation_run_id / case_id
  passed / score / latency_ms / usage / tool_trace_ref / failure_category
```

### 12.2 指标和判定

| 层级 | 指标 |
|---|---|
| 单元 | Schema 校验、权限拒绝、SSRF、脱敏、状态转换、幂等 |
| 工具 | 参数正确率、确认覆盖率、重复副作用数、超时和错误分类 |
| 任务 | 断言成功率、结构化输出正确性、人工/LLM Judge 分数 |
| 系统 | p50/p95 延迟、事件恢复率、取消时效、成本、队列积压 |
| 记忆 | 相关性、过期命中率、冲突处理正确率、错误召回率 |

每次 Agent 配置、模型、Harness、上下文策略或工具版本变更后运行评估。预发布门禁默认要求：关键安全用例 100% 通过；任务成功率不得低于基线设定的容忍区间；成本与 p95 延迟不得超过阈值。模型切换实验固定 Harness；Harness 消融实验一次只关闭一个组件，避免把波动误判为改进。

### 12.3 人工与自动评判

确定性断言优先，例如工具参数、状态、是否确认及输出 schema。开放文本可使用经过校准的 LLM Judge，但必须记录 judge 模型、prompt 与评分理由，并定期抽样人工复核。评估结果按任务类型分组；不能只看总平均分。

## 13. 群聊、经验学习和多 Agent

### 13.1 群聊 Agent

群聊仅在阶段 C 开放：成员在 E2EE 群内 `@Agent` 后，由其浏览器解密本次任务和明确选择的有限上下文，将明文提交给 Agent API。Agent 完成后，发起浏览器将最终答案和轨迹快照加密发布为群消息，并以 `run_id` 去重。Agent 服务不读取未显式提交的群历史，也不获取群密钥。

### 13.2 经验学习

经验学习不是自动修改 Agent。完成的运行首先进入受权限控制的经验候选池，记录任务标签、成功/失败、工具序列、错误类别和脱敏结论。后续任务可检索高质量相似经验作为案例，但必须标明来源且受上下文预算限制。创建者可查看经验命中和效果；可删除任何经验。提示词、工具和配置的变更始终需要人工确认与评估通过。

### 13.3 多 Agent

多 Agent 的前提是单 Agent、工具、审计和评估闭环稳定。通信必须是结构化消息：发送方、接收方、意图、输入 schema、输出 schema、预算和可见性。

| 模式 | 默认上下文策略 |
|---|---|
| Pipeline | 不共享完整历史；仅传递经 schema 验证的结果和必要摘要 |
| Supervisor | 可读取任务级状态栏与子任务摘要，不读取子 Agent 原始隐藏内容 |
| Debate | 默认关闭；每轮严格预算与轮数限制 |

委派图在运行时检测循环，限制最大深度、总子任务数、总 token/费用和并行度。任何 Agent 均不能突破发起者、owner 或工具权限边界。

## 14. 实施路线与验收门禁

### 阶段 0：评估与平台基线

交付：测试模型服务、确定性工具模拟器、评估数据模型、至少 20 个基线用例、CI 回归任务和安全测试集。

通过条件：关键安全用例全绿；每次配置变更可复跑；结果可区分模型、Harness、工具和策略失败。

### 阶段 A：私有单 Agent 生产闭环

交付：PostgreSQL/Alembic、Redis Streams/outbox、跨进程 WebSocket 事件、不可变 run snapshot、加密原文/脱敏事件、上下文状态栏、OpenAI 适配器、HTTP `read` 工具、取消/暂停、并发/预算、配置与运行 UI。

通过条件：Worker 故障后 pending run 可恢复；实时事件与重连事件无漏失；单 Agent 评估达到基线；外部端口仅 `9010`；安全审计确认没有明文敏感日志。

### 阶段 B：工具治理、受控 MCP 与记忆

交付：OpenAPI 候选导入、工具副作用分级、确认 UI、远程 MCP provider、工具速率限制、长期记忆、评估对比和运行记录工作台。

通过条件：写/破坏性工具的确认覆盖率 100%；SSRF、重定向、DNS rebinding 与凭据脱敏测试通过；记忆可解释、可删除、可处理冲突；MCP 无绕过策略层路径。

### 阶段 C：群聊与经验学习

交付：群绑定、participant 权限、浏览器代办式 `@mention`、群消息回写、经验候选池和检索。

通过条件：Agent 无法读取未提交群消息；成员无权读取 owner 配置与凭据；群内轨迹和最终快照按 `run_id` 幂等；经验检索通过隐私和相关性评估。

### 阶段 D：受控代码执行与多 Agent

交付：沙箱代码工具、`stdio` MCP、Pipeline、Supervisor、委派追踪和全局预算。

通过条件：沙箱逃逸/网络/资源限制演练通过；无循环委派；全局预算和权限不会被子任务绕过；多 Agent 评估优于或等于单 Agent 基线后才开放。

### 阶段 E：多模态交互

交付：先接入 ASR 文本入口；必要时再评估流式语音和受限的只读 Computer Use。

通过条件：不降低已有文本任务评估；语音数据、截图和界面操作遵守独立的隐私、确认与审计策略。

## 15. 当前实现基线与迁移计划

### 15.1 已有可复用部分

1. Rust 内部认证检查接口与 Agent API 认证适配已存在。
2. 私有 Agent、会话、上下文 epoch、运行、取消、归档、基础配置版本和 API Key Fernet 加密已具备开发原型。
3. OpenAI 兼容 SSE 文本流、前端 Agent 创建/聊天/轨迹面板、基本 WebSocket 重连已具备。
4. 工具配置、JSON Schema 预校验、OpenAPI 候选解析、URL 安全预检和 Redis Worker 骨架已出现。

### 15.2 阶段 A 收尾前阻塞项（已解决）

以下条目记录阶段 A 开始时的差距；对应实现和验证结果见 17.3 节。

1. Agent 仍使用 SQLite 与进程内事件 Hub；Compose 的 API 与 Worker 分离后，Worker 事件不能实时到达 API WebSocket。
2. Worker 缺少 outbox、pending 认领、步骤 checkpoint、重试分类和非幂等工具保护。
3. Agent 消息、系统提示词和 trace 原文仍未实现设计要求的列级加密和保留策略。
4. 工具未进入模型请求和运行循环，尚无真实 `tool_calls`、确认、HTTP 执行或结果回填。
5. `run_policy`、memory 表和 usage 字段尚未实际执行并发、预算、检索或费用统计。
6. 没有 Agent 专属自动化测试和评估集；现有构建通过不等于 Agent 行为正确。

### 15.3 迁移原则

不对现有 SQLite 原型做长期兼容扩展。先定义 PostgreSQL schema 和 Alembic 初始迁移，编写一次性、可审计的开发数据迁移工具；生产环境从空库启动。切换事件总线和 Worker 后，再实现工具循环与策略，避免在不可靠的基础上叠加新能力。每一阶段仅在验收门禁通过后进入下一阶段。

## 16. 近期实施优先级

1. 修复跨进程事件路径，落实 PostgreSQL、outbox、Redis Streams、consumer group 和 WebSocket 网关。
2. 建立阶段 0 评估、Agent 单元/集成测试和测试模型/工具模拟器。
3. 落地 Harness 接口、运行状态机、配置/快照和加密数据模型。
4. 实现上下文状态栏、预算化组装与 HTTP `read` 工具完整 ReAct 循环。
5. 再加入写工具确认、MCP、记忆、群聊和多 Agent。

第 1 至 4 项已在阶段 A 完成。写/破坏性工具、长期记忆、群聊 Agent、MCP、代码执行和多 Agent 仍受阶段 B-D 门禁约束，不得提前开放。

## 17. 实施进度报告

> 更新日期：2026-07-23
>
> 当前状态：阶段 0、阶段 A 与阶段 B 已完成实现和本地验收；生产发布仍需在目标环境执行凭据、安全日志与故障演练复核。

| 工作流步骤 | 状态 | 结果 |
|---|---|---|
| 1. 设计方案报告 | 已完成 | 本文定义 Harness、阶段门禁、评估指标与后续迁移边界。 |
| 2. 按步骤执行 | 已完成（阶段 0） | 已交付确定性测试模型、工具模拟器、评估数据模型、20 个基线用例、CI 回归任务和安全测试集。 |
| 3. 报告进度 | 已完成 | 本节记录当前交付、验证结果和剩余风险。 |
| 4. 编译并安装后端二进制 | 已完成 | `cargo build --release --target x86_64-unknown-linux-musl` 成功；根目录 `chat-server` 已更新为 static-pie 发布二进制。 |
| 5. SSH 提交并推送 GitHub | 已完成 | 已通过 `git@github.com:ckl1ng/chat-server.git` 推送 `42074f6`（`feat: add agent evaluation baseline`）至 `main`。 |

### 17.1 阶段 0 交付与验证

1. 新增 `evaluation_case`、`evaluation_run`、`evaluation_result` 的开发数据模型；用例输入和显式上下文使用现有 Fernet 机制加密存储。
2. 新增本地 OpenAI Chat Completions SSE 测试模型与确定性工具模拟器，覆盖只读工具、写工具确认、超时、无效参数、取消和策略拒绝。
3. 新增 20 个基线用例，覆盖正常任务、无工具回答、工具确认、上下文、预算、SSRF、凭据脱敏和事件恢复。
4. 新增 GitHub Actions 回归门禁。关键安全用例、成功率或 p95 延迟不满足门禁时将失败。
5. 本地验证：`python -m unittest discover -s tests -p 'test_*.py' -v`，结果为 10 项测试全部通过。
6. 发布二进制：已使用 `x86_64-unknown-linux-musl` release 配置编译并安装；构建仅报告 3 个既有 dead-code 警告，无编译错误。
7. 版本交付：实现提交 `42074f6` 已通过 SSH 推送到 GitHub `main` 分支。

### 17.2 剩余风险

阶段 0 只建立评估基线，不改变第 15.2 节中列出的生产阻塞项。PostgreSQL/Alembic、outbox/Redis Streams、跨进程事件、完整上下文/工具循环和列级加密仍属于阶段 A 的工作范围。

### 17.3 阶段 A 当前进度

1. 已完成：生产运行时切换为 PostgreSQL 16，Alembic 管理首版 schema；SQLite 仅保留为本地开发和一次性迁移来源，迁移工具要求目标空库且不删除源文件。
2. 已完成：run、用户消息、不可变快照与 outbox 同事务提交；Redis Streams consumer group、pending 心跳、`XAUTOCLAIM` 和幂等状态认领覆盖 Worker 故障恢复。
3. 已完成：trace 先持久化再通过 Redis Pub/Sub 扇出；WebSocket 以 sequence 重放并每秒从 PostgreSQL 补查，覆盖断线、发布丢失和订阅竞态。
4. 已完成：系统提示词、消息、最终回复、run snapshot 和 trace 原文使用 Fernet 列级加密；审计副本不复制模型正文或工具正文，敏感 JSON 字段脱敏。
5. 已完成：预算化滑动上下文、结构化状态栏、并发与日/月 token 门禁、OpenAI SSE/tool call 分段合并、GET/HEAD HTTP 工具循环、SSRF 前后校验、响应上限和取消状态机。
6. 已完成：Agent 创建/编辑、暂停/恢复、模型连接、提示词、上下文/预算、只读工具分配和运行状态/attempt/usage UI。
7. 当前验证：Agent 自动化套件共 21 项测试全部通过；前端单测与生产构建通过；Alembic SQL 生成通过；临时 PostgreSQL 实例上的迁移/密文仓库集成通过；真实 Redis Streams 上的 outbox、`XAUTOCLAIM`、Pub/Sub 与 sequence 重放集成通过。
8. 发布说明：阶段 A 改动尚未提交或推送。目标生产环境仍需用正式凭据复跑 Compose、外部端口扫描、日志采样和 Worker 强制终止演练后再发布。

### 17.4 阶段 B 当前进度

1. 已完成：OpenAPI 文档仅导入候选 operation；创建者须逐项创建并分配工具。HTTP 与 MCP 工具均固定输入 Schema、工具版本、副作用等级和每 run 调用上限。
2. 已完成：`read`、`write` 与 `destructive` 工具均经过同一策略层。写操作必须确认，破坏性操作强制逐次确认；确认绑定 `run_id + tool_call_id + arguments_hash`，批准后从加密 checkpoint 恢复，避免重新执行已确认的外部副作用。
3. 已完成：远程 MCP 仅支持经受控 HTTPS 网络边界的 JSON-RPC/Streamable HTTP `tools/list` 与 `tools/call`；发现结果只是候选项，不能绕过分配、Schema、确认、限额、SSRF 或审计检查。`stdio` MCP 仍未开放。
4. 已完成：长期记忆默认关闭，必须由 Agent owner 显式启用与创建。记忆内容加密存储，保留来源、类型、重要度、过期时间、访问计数和冲突状态；冲突不会静默覆盖，支持检索解释和删除。
5. 已完成：运行工作台 API 提供按 Agent/状态筛选的运行记录、确认记录、事件重放、评估运行结果和基线比较；比较会明确列出回归用例。
6. 当前验证：`python3 -m unittest discover -s tests -p 'test_*.py' -v` 共 27 项通过；覆盖确认哈希绑定、未确认写工具隔离、记忆冲突/删除、评估回归、SSRF/DNS rebinding、凭据脱敏与既有阶段 A 恢复路径。`alembic upgrade head --sql` 可生成阶段 B schema。
