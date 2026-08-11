# Agent 集群与任务编排分阶段执行方案

> 状态：实施中
>
> 日期：2026-07-30
>
> 范围：在既有 Agent Harness、Cloud Worker 与 Local Agent daemon 的基础上，交付有任务所有权、上下文隔离、可审计委派和可观察执行状态的多 Agent 协作平台。

---

## 当前实施状态（2026-07-30，已核验）

当前代码已完成 Task 核心、Cloud Run 接入、右侧工作台和受控 Cloud Pipeline 的实现。Local Task 执行、Hook 与 Supervisor 尚未开始；真实部署环境的 Redis 多进程与浏览器断线演练仍是发布门禁。

已实现并已验证：

1. SQLite 开发 schema 与 PostgreSQL Alembic migrations `20260727_0004`、`20260727_0005` 创建了 `tasks`、私有 Context/Dispatch Event、Notification、`task_assignments`、`task_results`、`task_handoffs` 和命令去重记录；`runs` 已关联 `task_id`、`assignment_id`，确认记录已预留 `task_id`。
2. 后端提供根 Task 的创建、列表、详情、私有 Context、Assignment、Result、Run、确认与 Dispatch Event 查询、指派、用户收尾/重开/取消及通知已读 API。Task 写入支持幂等键，状态变更和指派支持 `X-Task-State-Version` 乐观并发校验；Task Dispatch 写入会进入持久化 outbox。
3. 每次 Cloud 指派创建独立 Conversation、Assignment attempt 和 Run。Run snapshot 保存 `task_id`、Assignment、预算快照、`task_prompt_version=1` 和冻结的授权 Task Context 清单；Worker 对 Task Run 只读取该清单，不再读取 Conversation 历史。Task 实际限制 `max_total_tokens`、`max_tool_calls` 与 `max_concurrent_runs`，额度耗尽会进入 `attention_required`。
4. `post_progress`、`submit_result`、`request_proposer_decision`、`delegate_task`、`accept_assignment`、`decline_assignment`、子任务结果收取和 Agent-proposer 子任务收尾已作为仅对有效 Cloud Assignment 开放的结构化 Task 工具。委派必须提供显式输入包，并校验深度、子任务数、树并发与父预算。
5. `chat-client` 已提供手动 Task 创建、详情结果/Run trace/确认、通知，以及 `@agent` 的浏览器二次确认入口；仅确认表单中的目标文本会上传为 Task 输入。Task 领域状态机和存储定向测试、Python 编译检查及前端生产构建均已通过；并发收尾和持久化事件 sequence 补拉已有回归测试。

当前实现与目标契约的关键差距：

1. `TaskAccessPolicy` 已在 Tool Runtime 对当前 Assignment 和 Agent-proposer 子任务生效，但 Web API 仍只认证用户所有者；显式 participant、管理员角色和独立 Agent 身份令牌尚未接入。
2. 当前只依据 token、工具调用和并发数执行 Task 预算；没有可信的模型价格表，故费用预算未实现。子任务预算按父任务分配时保守预留，未实现终态未用额度回收。Local Assignment 及其 lease/workspace 约束未实现。
3. Task outbox、Redis `task-events` Stream、Pub/Sub relay 和 `/task/ws` 已实现；本机已验证持久化 sequence 补拉、Hub 所有者隔离和并发状态冲突。真实 Compose Redis 多进程 relay 与浏览器断线恢复尚未完成演练。
4. Local coding Tool Provider、Hook 和 Supervisor 均未开始。当前实现只支持受限 Cloud Pipeline，不应被描述为完整多 Agent 集群。

### 下一步执行计划

下一迭代先完成部署发布门禁和权限边界，再评估 Local Task 执行；不开放 Hook 或 Supervisor。

1. **任务契约加固（P0，已完成）**：已新增 `task_assignments`、`task_results` 和 `task_handoffs`，为 `runs` 增加 `assignment_id`；提供基础 `TaskAccessPolicy`、幂等键、乐观版本冲突和 Task outbox。
   剩余加强：将 Agent 身份、participant/管理员授权和并发请求的多进程竞争测试纳入后续权限与发布门禁。
2. **Cloud Run 正确接入（P0，已完成）**：Run snapshot 已冻结 Task Context、Assignment 和预算快照；Worker 只读取快照中的 Task Context。Task token/工具/并发预算与 Task 级确认已接入，Run 完成仅保存候选输出。
   剩余加强：部署环境完成 Worker 崩溃、Redis 重领与确认恢复演练。
3. **投影与 Web 工作台（P1，已完成）**：Task outbox、`/task/ws` 的单 Task sequence 回放/补拉，以及全局任务订阅的持久化投影重同步已完成。右侧任务面板可创建、查看结果/Run trace/确认、展示树形调度摘要和通知；客户端以退避重连并在重连后刷新投影。
   剩余加强：在部署环境补充真实 Redis 多进程 relay 与浏览器断线恢复演练。
4. **受控委派（P1，已完成当前 Cloud Pipeline 范围）**：`delegate_task`、显式输入包、深度/子任务/并行/预算限制、直系子结果收取与 Agent-proposer 收尾已接入。浏览器只对已知 Agent 的开头 `@` 提及显示确认，伪 `@` 文本没有副作用。
   剩余加强：独立 Agent 身份认证、显式 participant/管理员授权和 E2EE 附件选择 UI 进入发布门禁；Local coding Agent、Hook 与 Supervisor 继续分别按 Phase 4、5、6 排期。

---

## 1. 目标与边界

本方案将现有一次性的 `run` 提升为可协作的 `task`。`run` 仍表示某个执行者的一次尝试；`task` 是用户或 Agent 提出的、有独立上下文和明确收尾权的工作单元。

### 1.1 产品目标

1. 用户或 Agent 可通过受控的 `@agent` / `delegate_task` 创建、指派和转交任务。
2. 每个任务只能由其提出者收尾；执行者只能提交结果、声明阻塞或请求转交。
3. 接手任务的执行者可读取该任务域内的全部授权上下文；不同任务域默认完全隔离。
4. 集群聊天只表达调度信息，不复制工作对话、代码、工具原始输出或模型中间内容。
5. Web 右侧持续展示任务树、当前执行者、状态与阻塞原因；点击任务打开完整任务工作台。
6. 本地 coding agent 作为受限执行目标接入同一任务模型，不让云端服务访问宿主机。
7. Hook 可在无人工持续监督时驱动受限的路由、重试、检查与通知，但不得绕过所有权、预算、确认和权限边界。

### 1.2 明确不做

1. 不在本阶段让服务端解密、扫描或自动消费 E2EE 私聊/群聊内容。
2. 不以模型自然语言文本作为委派、收尾或权限提升指令；这些操作必须使用结构化工具调用或 Web API。
3. 不实现共享的“集群全局记忆”。跨任务传播必须由提出者显式选择并生成可审计的输入包。
4. 不允许 Hook 修改 Agent 提示词、权限、工具配置或自动批准写/破坏性操作。
5. 不先实现自由辩论、自发递归委派或不受限的本机 Shell。

## 2. 核心术语与不变量

| 术语 | 含义 | 不变量 |
|---|---|---|
| Task | 具有目标、上下文域、提出者和收尾权的协作工作单元 | 与其他 Task 的工作上下文隔离 |
| Run | 一个执行者对某个 Task 的一次实际执行尝试 | 可失败、可取消、可重试，不等于 Task 收尾 |
| Proposer | 提出 Task 的主体，类型为 `user` 或 `agent` | 仅 Proposer 能收尾该 Task |
| Executor | 当前被指派执行 Task 的 Cloud Agent 或 Local Agent | 只能提交结果/阻塞/请求转交，不能收尾他人任务 |
| Context Domain | Task 私有的消息、附件引用、工具摘要、trace、确认和结果空间 | 不因同用户、同群组或同 Agent 自动共享 |
| Dispatch Event | 集群聊天可见的调度事件 | 不含工作正文、代码或工具原始输出 |
| Work Event | Task 详情可见的工作事件 | 按 Task ACL、脱敏和工具权限展示 |

所有实现必须满足：

1. `task.closed`、`task.cancelled` 与 `task.reopened` 必须校验调用主体等于 `proposer_kind + proposer_id`。
2. 子任务与父任务是两个 Context Domain。创建子任务时只能复制经显式选择、脱敏和预算裁剪后的输入包。
3. 当前 Executor 变更不改变 Proposer，也不授予收尾权。
4. Agent 的工具权限始终取发起者、Task 策略、执行者和执行目标权限的交集。
5. Local Agent 的本机绝对路径、密钥和未授权文件内容不进入云端 Task Context。
6. 所有状态变更均以持久化事件为事实来源，并具有幂等键和操作者身份。

## 3. 任务生命周期

### 3.1 Task 状态机

```text
draft -> queued -> assigned -> in_progress -> awaiting_proposer_close -> closed
                   |             |                    |
                   |             +-> attention_required+
                   |             +-> waiting_confirmation
                   +-> cancelled (仅 proposer)

attention_required -> queued | assigned | in_progress | cancelled (仅 proposer)
awaiting_proposer_close -> in_progress | assigned | closed | cancelled (仅 proposer)
```

状态含义：

| 状态 | 写入方 | 含义 |
|---|---|---|
| `draft` | Proposer | 尚未完成输入包或尚未指派的草稿 |
| `queued` | Proposer / Router | 已可调度，等待路由或执行资源 |
| `assigned` | Router | 已分配执行者，尚未开始 Run |
| `in_progress` | Executor | 至少有一个有效 Run 正在执行 |
| `waiting_confirmation` | Policy Engine | 等待该 Task 授权主体确认副作用 |
| `awaiting_proposer_close` | Executor | 执行者已交付结果，等待提出者验收、重开或收尾 |
| `attention_required` | Executor / Router | 执行失败、资源不可用、预算耗尽或需要提出者决策 |
| `closed` | Proposer | 已验收且不可继续写入工作上下文 |
| `cancelled` | Proposer | 已放弃，保留审计与结果快照 |

执行者提交完成不得将 Task 直接变为 `closed`。它必须执行 `task.submit_result`，生成结构化结果、证据链接、遗留风险和推荐下一步，然后进入 `awaiting_proposer_close`。Proposer 可选择：收尾、重开原执行者、转交其他执行者、创建子任务或取消。

### 3.2 Run 状态机与 Task 的关系

沿用既有 `queued -> running -> waiting_confirmation -> completed/failed/cancelled` Run 状态机，但增加 `task_id` 和 `assignment_id`。Run 终态不会自动决定 Task 终态：

- `completed` 加结果后，若 Executor 调用 `task.submit_result`，Task 进入 `awaiting_proposer_close`。
- `failed`、`cancelled`、租约丢失或 Local Agent 恢复失败，Task 进入 `attention_required`，除非仍有其他有效 Run。
- 每一次重试或转交创建新的 Assignment 和 Run attempt；历史 attempt 永不覆盖。

## 4. 数据模型与迁移

### 4.1 新表

| 表 | 关键字段 | 用途 |
|---|---|---|
| `tasks` | `id`, `root_task_id`, `parent_task_id`, `proposer_kind`, `proposer_id`, `state`, `goal_encrypted`, `context_scope_id`, `budget_snapshot`, `result_summary_encrypted`, `closed_by_*` | 任务事实记录与所有权 |
| `task_assignments` | `id`, `task_id`, `executor_kind`, `executor_id`, `device_id`, `workspace_id`, `state`, `lease_id`, `attempt`, `assigned_by_*` | 可追溯的执行者分配和租约 |
| `task_context_events` | `task_id`, `sequence`, `kind`, `content_encrypted`, `redacted_payload`, `source_run_id`, `visibility` | Task 私有工作上下文与重放 |
| `task_handoffs` | `from_task_id`, `to_task_id`, `from_principal_*`, `to_executor_*`, `input_manifest`, `input_encrypted`, `schema_version` | 显式创建的跨 Task 输入包 |
| `task_results` | `task_id`, `assignment_id`, `submitted_by_*`, `result_encrypted`, `evidence_manifest`, `risk_summary`, `created_at` | 执行者提交的可验收结果 |
| `task_dispatch_events` | `task_id`, `sequence`, `event_type`, `actor_*`, `summary`, `metadata` | 集群聊天和右侧任务树的无工作正文事件 |
| `notifications` | `owner_user_id`, `kind`, `task_id`, `payload`, `read_at`, `delivery_state`, `dedupe_key` | Web 未读、桌面通知和重试 |
| `hook_definitions` | `owner_user_id`, `trigger`, `filter`, `action`, `policy`, `enabled`, `version` | 可审核的自动化规则 |
| `hook_executions` | `hook_id`, `source_event_id`, `idempotency_key`, `state`, `attempt`, `result`, `error` | Hook 幂等、限额和审计 |

所有正文、输入包、结果和工作事件沿用现有列级加密；列表、过滤和集群聊天只读脱敏摘要。数据库迁移必须使用 Alembic，先新增 nullable 列与表，完成回填和双写后才为关联字段增加非空与外键约束。

### 4.2 现有表变更

1. `runs` 新增 `task_id`、`assignment_id`、`attempt`，并为 `(task_id, created_at)` 建索引。
2. `confirmations` 关联 `task_id`，使任务详情能展示等待确认及其操作者。
3. `outbox_events` 增加 `aggregate_type` 与 `aggregate_id`，区分 Run 与 Task 事件；保持现有消费者兼容。
4. Local Agent dispatch 增加 `assignment_id`，并将 lease 与 Task 的当前 Assignment 绑定。
5. 建立 `TaskAccessPolicy`，不能直接复用 Conversation 的 owner 判断；访问必须校验 Proposer、当前/历史 Executor、显式 participant 和管理员角色。

## 5. API、工具与事件契约

### 5.1 Task Agent System Prompt 契约

每一个作为集群成员执行 Task 的 LLM 都必须收到由 `ContextManager` 生成的、版本化的 `Task Agent System Prompt`。该 Prompt 不是可选提示，也不能由用户消息覆盖；它必须在当前 Task 的工作上下文之前注入，并在每个新的 Run、恢复 Run、转交 Run 与 Local/Cloud 执行目标中保持等价语义。

Prompt 必须明确告知 LLM 以下事实：

1. 它运行于一个多 Agent 任务集群中，当前只负责一个明确的 `task_id`，不是整个用户目标的无限制代理。
2. 当前 Task 的标题、目标、提出者、当前执行者、父/根任务引用、状态、完成定义、截止时间（如有）、预算、工具权限与执行目标。
3. 当前 Task Context Domain 中提供的是该 Task 的完整授权上下文；它不得假设能够访问其他 Task、其他会话、其他工作区、其他设备或未显式传递的父/子任务内容。
4. 不同 Task 的上下文严格隔离。需要他人完成工作时，必须使用 `delegate_task` 创建/转交一个新 Task，并传递最小、相关、已授权的输入包。
5. 执行者无权关闭、取消或重开非自己提出的 Task。完成工作后必须使用 `submit_result`，让 Task 进入 `awaiting_proposer_close`；由该 Task 的 Proposer 决定验收、重开、转交或取消。
6. 集群调度聊天只用于简短的任务分配、接手、阻塞、结果待验收和状态通知。不得把工作过程、代码全文、工具原始输出、凭据、绝对路径或内部推理写入调度消息。
7. 完整工作内容应当通过 Task Context 的受控事件、工具调用和结果提交保存，且只使用当前 Task 授予的工具、工作区和预算。
8. 遇到不完整需求、权限不足、确认请求、预算耗尽、工具失败、设备离线或不确定是否完成时，必须使用 `request_proposer_decision` 或 `post_progress` 报告，不能自行扩权、猜测或静默结束。
9. 本地执行目标只能访问已绑定 workspace。不得索取、展示、上传或在任务间传播密钥、未授权文件内容和本机绝对路径；写入、Git 高风险操作与进程执行必须遵守确认流程。
10. 所有结构化操作都受服务端状态机、权限、预算、schema 和幂等校验约束；Prompt 中的说明不会赋予额外权限，也不能替代工具调用。

运行时必须把下面的动态状态栏与固定规则一同注入。状态栏内容由程序生成，禁止让模型自行声称或篡改：

```json
{
  "cluster": {
    "task_id": "task_...",
    "root_task_id": "task_...",
    "parent_task_id": "task_... | null",
    "task_state": "in_progress",
    "proposer": {"kind": "user", "id": "...", "display_name": "..."},
    "executor": {"kind": "cloud_agent | local_device", "id": "...", "display_name": "..."},
    "completion_definition": ["..."],
    "allowed_next_actions": ["post_progress", "submit_result", "request_proposer_decision", "delegate_task"],
    "delegation": {"depth": 1, "max_depth": 3, "remaining_subtasks": 4},
    "budget_remaining": {"tool_calls": 8, "output_tokens": 3000, "cost": "..."},
    "context_boundary": "Only this task's authorized context is available.",
    "dispatch_policy": "Summaries only; never include work content or secrets."
  }
}
```

固定 Prompt 内容与动态状态栏均必须写入不可变 Run snapshot，并记录 `task_prompt_version`。当 Task 角色、预算、Assignment、权限或上下文输入包发生变化时，只影响后续 Run；已运行的 Run 继续使用原快照。

推荐的固定 Prompt 模板如下，实际实现可使用本地化文本，但不得删减其中的约束语义：

```text
你是 Agent 集群中的任务执行者。你当前只能处理状态栏指定的 Task。
你获得的是当前 Task 的完整授权上下文，不代表你有权访问任何其他 Task、会话、设备或工作区。

执行工作，使用授权工具记录可验证进展。需要其他 Agent 时，仅能调用 delegate_task 并传递最小授权输入包。
完成时调用 submit_result，包含结果、证据、未完成项和风险；这不会关闭 Task。只有该 Task 的提出者可以收尾、取消或重开。

集群调度信息仅写简短状态摘要，绝不发布工作对话、代码、工具原始输出、密钥或绝对路径。
若需求、权限、确认、预算或执行条件不明确，调用 request_proposer_decision；不得自行扩大权限、跳过确认或假定任务已被收尾。
所有操作必须通过可用的结构化工具完成，并服从系统状态栏和工具返回的限制。
```

测试要求：为上述十项规则各增加至少一个提示词/工具调用回归用例，特别覆盖“模型试图直接声称任务已关闭”、“模型把其他 Task 内容当作可见”、“模型把完整工作输出发到 Dispatch Chat”与“模型创建无预算的递归子任务”。

### 5.2 对 Web 的 API

| API | 行为 | 权限 |
|---|---|---|
| `POST /api/v1/tasks` | 用户创建根 Task 或草稿 | 已认证用户 |
| `GET /api/v1/tasks` | 按状态、执行者、根任务筛选任务树 | Task participant |
| `GET /api/v1/tasks/{id}` | 返回任务元数据、执行者、结果和权限 | Task participant |
| `GET /api/v1/tasks/{id}/context` | 分页读取完整 Task 工作对话与 trace | Task participant，按脱敏策略 |
| `POST /api/v1/tasks/{id}/assignments` | 指派/转交执行者 | Proposer 或受限 Router |
| `POST /api/v1/tasks/{id}/close` | 验收并收尾 | 仅 Proposer |
| `POST /api/v1/tasks/{id}/reopen` | 回到可执行状态 | 仅 Proposer |
| `POST /api/v1/tasks/{id}/cancel` | 取消任务及有效 Run | 仅 Proposer |
| `GET /api/v1/dispatch-events` | 获取集群调度聊天流 | 集群 participant |
| `GET/PATCH /api/v1/notifications` | 拉取、标记已读、配置 Web 通知 | 通知所有者 |

所有写入 API 接收 `idempotency_key`。返回冲突时必须携带当前 Task 状态、最后事件序号和可执行动作，而不是静默覆盖。

### 5.3 Agent 可调用的结构化工具

仅向有委派权限的 Agent 暴露以下工具，参数和输出采用 JSON Schema 固定版本：

1. `delegate_task`：创建新 Task 或从 `attention_required` 转交。必须传目标 Agent、目标、输入包引用、预算请求和理由。
2. `accept_assignment` / `decline_assignment`：执行者显式确认接手或说明拒绝原因。
3. `post_progress`：写入 Task 工作事件与不超过限定长度的调度摘要。
4. `submit_result`：提交结果、证据、已完成项、未完成项、风险和建议；不收尾 Task。
5. `request_proposer_decision`：用于需要澄清、确认、预算增加、改派或收尾验收的情况。

模型输出文本中出现 `@name` 不产生副作用。只有浏览器提交的显式 `@`，或经过 Tool Runtime 校验的 `delegate_task` 才会创建或转交 Task。

### 5.4 事件分类

`task_dispatch_events` 只允许：`task.created`、`task.assigned`、`assignment.accepted`、`assignment.declined`、`task.blocked`、`task.awaiting_proposer_close`、`task.reopened`、`task.closed`、`task.cancelled`、`task.notification_sent`。其 `summary` 上限 280 字符并经过脱敏。

工作内容写入 `task_context_events`，例如：用户输入、Agent 对话、工具调用摘要、附件引用、Run trace、结果提交和确认。集群聊天不订阅此流。

## 6. Web 交互设计

### 6.1 布局

主聊天保留当前用户对话；集群调度聊天为独立视图。右侧固定任务面板包含：

1. 状态筛选：进行中、待我收尾、需处理、已完成。
2. 任务树：标题、当前执行者、状态、最后事件时间、未读标记和子任务数量。
3. 只显示调度摘要，绝不在任务卡片内渲染工作对话或工具正文。
4. 任务详情抽屉/页面：概览、完整对话、执行 trace、结果、执行者历史、子任务、确认记录和收尾操作。

`awaiting_proposer_close` 与 `attention_required` 必须有高优先级视觉标识，但不自动弹出或中断用户当前输入。所有任务详情 URL 使用 `task_id`，允许通知点击后恢复到同一位置。

### 6.2 `@` 创建规则

1. 浏览器在可分配的集群调度输入框中解析 `@`，仅列出当前用户有权使用的 Agent。
2. 选择 Agent 后，浏览器构造根 Task：任务目标、被 @ 的 Agent、用户显式勾选的附件/上下文、默认预算和可见性。
3. 若来自 E2EE 群聊，浏览器只提交触发消息及用户明确选择的有限上下文；Agent API 和 Rust 服务不持有群密钥。
4. 提交成功后，调度聊天只显示“已指派给 X”；完整请求仅出现在 Task 详情。

### 6.3 通知

通知由 Task 状态事件驱动：

| 触发 | 接收者 | 默认方式 |
|---|---|---|
| 进入 `awaiting_proposer_close` | Proposer 为用户时 | Web 未读、角标、可选桌面通知 |
| 进入 `attention_required` | Proposer | Web 未读、角标、可选桌面通知 |
| 被重新指派 | 新 Executor 对应用户 | Web 未读 |
| 等待写/破坏性确认 | 有确认权的用户 | 高优先级 Web 通知 |

通知采用 `(owner_user_id, task_id, kind, state_version)` 去重键。浏览器 Desktop Notification 必须在用户显式授权后启用；断线期间通知持久化，重连后同步，不依赖 WebSocket 在线。

## 7. 分阶段实施

### Phase 0：契约冻结与基线

目标：在不改变生产行为的前提下冻结 Task 领域模型和可测的边界。

工作项：

1. 评审并冻结本文件中的 Task/Run/Assignment/Context Domain 定义、状态转换表和权限矩阵。
2. 编写 Alembic migration，创建 Task 表、事件表、结果表、通知表和必要索引，但不改变现有 Run 创建路径。
3. 实现纯领域 `TaskStateMachine`，对每个转换输入 actor、前置状态、理由、幂等键与事件序号。
4. 建立 Task fixture：用户发起、Agent 发起子任务、转交、失败、收尾、Local Agent 租约丢失、并发收尾。
5. 更新评估数据模型，新增任务隔离、收尾授权、循环委派和预算继承断言。

验收：所有原有 Agent 回归测试通过；新状态机测试覆盖全部允许和拒绝转换；非 Proposer 对 close/cancel/reopen 返回 403；Task 表尚未被旧 Run 流量写入。

### Phase 1：Task 核心与右侧任务面板

目标：用户可创建、查看和手动收尾独立任务，右侧可见真实任务状态。

后端工作项：

1. 实现 `POST/GET /tasks`、详情、收尾、重开、取消和 Context 分页 API。
2. 根 Task 创建时在同一数据库事务创建 Task、初始 Context Event、Dispatch Event、Notification outbox 与首个 Assignment。
3. 将 Task 事件持久化后发布到 Redis，增加 `/task/ws` 或扩展现有 `/agent/ws` 的版本化订阅通道；支持按 sequence 重放。
4. 从 `tasks` 投影聚合右侧面板所需的执行者、状态、最后事件与未读数，不让前端扫描 trace。
5. 为每个 Task 创建独立 Context Scope；查询层必须强制带 `task_id`，禁止按 user 或 agent 跨 Task 获取工作正文。

前端工作项：

1. 增加右侧任务面板、状态筛选、树形父子关系、当前执行者、未读与实时更新。
2. 点击任务打开详情抽屉：概览、工作对话、执行 trace、Assignment 历史、结果和 Proposer 操作。
3. `closed/cancelled` 操作只对当前 Proposer 显示；客户端隐藏不是权限控制，服务端仍必须拒绝。
4. 增加未读角标、浏览器内通知中心和通知点击跳转。

验收：两个不同 Task 使用同一 Agent 时相互无法读取 Context；刷新/断线重连不丢失任务树状态；执行者完成后 UI 只显示“待提出者收尾”；非提出者无法通过 API 或 UI 收尾。

### Phase 2：将现有 Cloud Run 接入 Task

目标：现有 Cloud Agent 可以作为 Task Executor 执行，且 Task 与 Run 保持独立审计。

工作项：

1. 在 Run snapshot 中冻结 `task_id`、上下文输入清单、Assignment、预算份额和权限交集。
2. 改造 Worker：从 Task Context 组装模型上下文，仅追加同一 Task 的工作事件；原 Conversation 模式继续兼容单 Agent 历史。
3. Run 完成后由 Harness 生成候选结果，但只有 `submit_result` 工具能让 Task 进入待收尾状态。
4. 将取消、确认、预算耗尽、Worker 崩溃映射为 Task 事件，并在有其他 active Assignment 时避免误报阻塞。
5. 引入每 Task 深度、并行、总 Run、token、费用与工具调用预算；子任务预算从父任务预留并回收未用额度。

验收：同一 Task 的 Run 崩溃恢复仍使用同一冻结上下文；Run 成功不自动关闭 Task；预算无法被多次子任务累加绕过；Task 详情能完整关联所有 attempt。

### Phase 3：`@` 入口、集群调度聊天与 Pipeline 委派

目标：交付用户可见的多 Agent 任务流转，但只支持受控 Pipeline。

工作项：

1. 前端实现 Agent 提及选择器与权限过滤。普通聊天中的 `@` 先创建草稿，用户确认输入包后才创建 Task。
2. 对 E2EE 群聊实现浏览器代办式提交：明确显示将上传的消息、附件和上下文范围；服务端只收到该输入包。
3. 实现 `delegate_task`、`accept_assignment`、`decline_assignment`、`post_progress`、`submit_result` 与 `request_proposer_decision` Tool Provider。
4. Router 只接受显式目标的 Pipeline 委派，运行时检测 `root_task_id` 路径循环，限制深度、总子任务数和并行量。
5. 实现独立 Dispatch Chat 投影，只消费 Dispatch Event；禁止从 Task Context Event 投影工作内容。
6. Proposer 是 Agent 的子任务使用 Agent 身份验权收尾；根 Task 的人类用户仍保留最终业务验收权，可关闭整个任务树或要求重开。

验收：A -> B -> C 的结果能按输入包流转；B/C 都不能读取无关 Task；模型生成伪 `@agent` 文本没有副作用；循环委派、越权 Agent、预算超限和无权限 Context 访问均被拒绝并审计。

### Phase 4：Local coding agent 成为执行目标

目标：将 `device + workspace` 安全地加入 Assignment，而不是让本地 daemon 成为旁路系统。

前置条件：Local Agent Phase 2 的两阶段确认、工作区写锁、取消进程组、journal 恢复和断线 lease 语义完整实现并通过演练。

工作项：

1. 增加 `executor_kind=local_device` Assignment，冻结设备、工作区、允许工具、sync mode 和 Local Agent policy 版本。
2. 在 Local daemon 实现受限 coding Tool Provider：文件读写、补丁应用、目录列举、Git 状态/diff、结构化命令。禁止接受任意 shell 文本。
3. 写入、Git 破坏性动作与进程启动使用两阶段确认；确认记录回传 Task Context，批准后不可重放。
4. 按 workspace 建立写锁；只读任务可有限并发，读结果携带文件版本，写入前校验预条件。
5. 选择性同步工作内容：服务端默认只保存脱敏事件、diff 摘要和用户授权上传的片段。绝对路径、密钥、被策略拒绝的内容不得回传。
6. 可评估 OpenCode 作为 daemon 内部的 coding Tool Provider 适配层，复用其工具协议/补丁/Git 能力；Task 状态机、权限、确认、审计和调度仍由本项目控制。

验收：Web 指派任务只由目标 device/workspace 领取；daemon 在已批准写操作后崩溃不会重放；越界路径、符号链接逃逸、命令注入和 workspace 并发写冲突均被阻断；任务详情可展示脱敏对话、diff 摘要与确认记录。

### Phase 5：Hook 与无人监督持续工作

目标：在严格限额下自动驱动任务生命周期，不把自动化变成无界 Agent 循环。

初始内置 Hook：

| Trigger | 默认动作 | 限制 |
|---|---|---|
| `task.created` | 根据显式规则分配默认 Agent | 仅一次，不改 Proposer |
| `task.awaiting_proposer_close` | 创建用户通知 | 不自动 close |
| `task.attention_required` | 创建通知；可按策略创建一次诊断子任务 | 最大一次，不能自动批准权限 |
| `assignment.lease_expired` | 将 Assignment 标记失效并通知 Proposer | 不自动重放副作用 Run |
| `workspace.changed` / `git.commit` | 创建受限测试或审查 Task | 仅已注册 workspace，去抖和冷却 |
| `schedule.cron` | 检查长期阻塞 Task 并请求进度 | 不直接关闭或删除 Task |

工作项：

1. Hook 定义采用声明式 trigger/filter/action/policy schema，不执行用户自定义 JavaScript 或 shell。
2. 执行器按 `source_event_id + hook_id + version` 生成幂等键，所有调用写入 `hook_executions`。
3. 每条 Hook 设置最大触发次数、时间窗口、冷却时间、预算上限、允许 Agent/工具列表和失败策略。
4. Hook 创建的 Task 标记来源，仍遵守新 Task 的 Proposer 和收尾规则；默认由 Hook 所属用户作为 Proposer。
5. Web 提供 Hook 列表、启停、执行历史、失败原因与一键禁用；不在集群聊天内输出工作正文。

验收：重复事件不会创建重复任务或通知；Hook 不能递归自触发超过策略阈值；禁用后不再产生新执行；任何 Hook 均不能关闭他人任务、增加工具权限或自动确认写操作。

### Phase 6：Supervisor 与发布门禁

目标：在 Pipeline 指标稳定后，开放受限 Supervisor 编排。

工作项：

1. Supervisor 只能查看任务级状态栏、Dispatch Event、子任务结果摘要和显式授权的输入包，默认不可读取子 Agent 原始工作对话。
2. Supervisor 的每次委派走相同的 `delegate_task`、预算、循环检测和所有权校验。
3. 增加跨执行目标评估集：Cloud-only、Local-only、Cloud-to-Local、失败转交、确认拒绝、通知与收尾。
4. 建立指标：Task 成功率、提出者收尾时延、错误转交率、上下文越权拒绝率、Hook 触发成功率、每 Task 成本、队列时延和 Local lease 恢复率。
5. 执行故障演练：Redis/Worker/daemon 强制终止、WebSocket 断线、重复投递、并发收尾、权限撤销、设备撤销和预算耗尽。

开放条件：关键权限/上下文隔离测试 100% 通过；多 Agent 任务成功率不低于同类单 Agent 基线；不存在循环委派或重复副作用；生产日志抽样不含工作正文、密钥或绝对路径；人工验收能够从 Task 详情追溯完整决策链。

## 8. 测试与发布策略

### 8.1 必测用例

1. 非 Proposer 尝试关闭、取消、重开、覆盖结果和伪造 actor 身份。
2. 同 Agent 执行两个 Task，任一 Run、上下文查询、摘要和记忆检索均不跨域。
3. Agent 转交后新 Executor 能读全量授权 Context，不能读兄弟/父 Task 的原始上下文。
4. Dispatch Chat 不包含代码、工具正文、模型对话、绝对路径或密钥片段。
5. 相同 idempotency key、重复 Redis 事件、WebSocket 重连和 daemon 重连不重复创建 Assignment、通知或副作用。
6. Local Agent lease 丢失、确认后崩溃、写锁冲突、设备撤销和 workspace 删除。
7. Hook 冷却、最大次数、递归触发、禁用、失败重试和预算耗尽。
8. 通知离线积压、去重、已读同步、桌面权限拒绝和任务删除/收尾后的跳转。

### 8.2 迁移与灰度

1. 初始版本只允许内部测试用户创建 Task，旧 Conversation/Run 完全保持原路径。
2. Feature flags 分别控制 `task_core`、`task_sidebar`、`agent_delegation`、`local_task_execution`、`hooks`、`supervisor`。
3. Phase 1 开始双写 Run 与 Task 关联仅用于观测，确认投影一致后才让 Task 成为新 UI 的事实来源。
4. 每一 Phase 独立完成数据库迁移演练、回滚演练、测试模型回归、真实 Redis 恢复测试和目标环境日志审计。
5. 不删除旧数据；旧会话可在需要时创建新的根 Task，但不得自动将历史会话合并到 Task Context。

## 9. 实施顺序与依赖

```text
Phase 0: 领域契约/迁移/测试
    -> Phase 1: Task 核心 + 右侧任务面板 + 通知
    -> Phase 2: Cloud Run 接入 Task
    -> Phase 3: @ 入口 + Pipeline 委派 + Dispatch Chat
    -> Phase 4: Local coding agent 执行目标
    -> Phase 5: Hooks
    -> Phase 6: Supervisor 与生产开放
```

Phase 1 可以先让用户创建并手动推进 Task，尽早验证右侧工作台和收尾权模型。Phase 3 前不开放 Agent 自动委派；Phase 4 前不将 coding 工作派给 Local Agent；Phase 5 前不启动无人监督的自动化链路。

## 10. 完成定义

功能不以“能让多个模型互相发消息”为完成标准。只有同时满足以下条件，才能宣布 Agent 集群可用：

1. 用户可在 Web 创建和追踪独立 Task，右侧始终显示真实状态和当前执行者。
2. 执行者完成后，只有提出者能够收尾，所有尝试均可审计。
3. 接手者可见当前 Task 的完整授权上下文，任何不同 Task 均无法越界读取。
4. 集群聊天只显示调度摘要；任务详情才显示工作对话与 trace。
5. Cloud 与 Local coding Agent 都通过相同 Task/Assignment/权限/预算/确认链路执行。
6. Hook 能够可靠通知和处理有限自动化，而不产生无界递归、重复副作用或越权收尾。
7. 自动化评估、故障演练和生产安全审计均通过发布门禁。
