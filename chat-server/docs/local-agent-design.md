# Local Agent CLI 产品与技术需求设计

> 状态：v2 设计稿
>
> 日期：2026-07-24
>
> 范围：为现有云 Agent 平台增加 Node.js Local Agent。一个已连接的本地 daemon 必须同时接受 Web 端和终端 CLI 的控制；两者共享同一套本机执行状态与安全边界。

配套的模块、协议、状态、恢复、安全和测试设计见：[Local Agent CLI 详细技术设计](./local-agent-technical-design.md)。

---

## 1. 目标与边界

### 1.1 产品目标

用户在自己的电脑上运行 `local-agent daemon` 后，获得一个受控的本机执行器：

1. Web 端可向指定电脑和工作区发送编码、文件和终端任务，并看到流式过程、确认请求和结果。
2. 终端可用 `local-agent run`、`chat`、`attach` 直接控制同一 daemon；终端任务默认也会同步到 Web，可从另一端继续观察、取消或确认。
3. 本机工具只能访问用户授权的工作区，所有写入、副作用命令和 Git 高风险动作均须经过不可篡改的确认流程。
4. Local Agent 与 Cloud Agent 共用会话、run、事件、审计和前端体验，但绝不让云端容器访问宿主机文件系统。

### 1.2 非目标

- v1 不支持从服务端反向连接用户电脑；所有连接由 daemon 主动发起。
- v1 不执行任意 shell 文本，也不支持用户自定义 JS/Shell 扩展。
- v1 不承诺“聊天端到端加密”覆盖 Agent run。文件内容、提示词和展示到 Web 的工具结果会按 Agent 平台策略进入服务端和模型提供方，必须在 UI 中明示。
- v1 不做多用户共享同一设备或跨账户控制。

### 1.3 核心原则

- **一个 daemon，一个本机真相源**：同一设备上的 Web 和 CLI 不各自运行 Harness；所有执行、工作区锁、工具确认和本地日志由 daemon 仲裁。
- **服务端是远程控制面**：服务端拥有 Web 鉴权、run 状态机、确认决定、审计事件和离线调度；daemon 拥有文件、进程和本地模型密钥。
- **能力最小化**：Web 只选择已注册的设备、工作区和工具，不向 daemon 下发任意命令或路径。
- **副作用不得重放**：任何写入类工具在崩溃后均不能自动重新执行。

---

## 2. 总体架构

```text
Web (Svelte)                         Terminal CLI
    |                                     |
    | REST / Agent WS                     | Unix socket / named pipe
    v                                     v
+-------------------+       WSS       +---------------------------+
| Agent API         |<--------------->| Local Agent Daemon         |
| 控制面            |  outbound only  | 本机唯一执行仲裁者         |
| - 用户/设备鉴权   |                 | - Local Harness            |
| - run/确认/审计   |                 | - 工作区锁与工具策略       |
| - 事件持久化推送  |                 | - 本地模型连接和密钥       |
+---------+---------+                 +-------------+-------------+
          |                                             |
          | Redis Streams，仅 Cloud run                 | file / git / process
          v                                             v
  Cloud Worker（Python）                         授权工作区与子进程
```

Cloud Worker 只执行 `execution_target=cloud` 的 run。Local run 不进入现有的 `agent-runs` Stream；由 Agent API 通过 daemon 已建立的出站 WSS 分发。这一点是与当前实现的必要隔离：当前 Worker 会消费统一 `agent-runs` Stream [worker.py](../../agent-service/app/worker.py)，不能与本地 daemon 共用。

### 2.1 双控制面行为

| 场景 | 入口 | 执行者 | Web 可见性 | 终端可见性 |
|---|---|---|---|---|
| Web 发起任务 | Web AgentChat | daemon | 完整可见 | `local-agent attach <run-id>` 可接管观察/确认 |
| 终端发起任务（daemon 在线） | `local-agent run` / `chat` | 同一 daemon | 默认同步为 Local run | 当前终端流式输出，其他终端可 attach |
| 显式离线任务 | `local-agent run --offline` | 前台临时 runtime | 不同步 | 仅当前终端 |

`--offline` 是唯一绕过 daemon 的模式。daemon 已连接时，普通 `run` 先通过本机 IPC 创建 run，再由 daemon 注册至 Agent API；因此 Web 可以看到、停止和确认终端发起的任务。若服务端暂时不可达，daemon 可在本地保留 queued journal，重连后上传；在上传前该 run 不会出现在 Web。

### 2.2 并发与工作区锁

一个设备可服务多个 run，但每个 workspace 同一时刻只能有一个可能写入的 run：

- 纯 read run 可并发，默认上限为 2。
- 首次请求 write/destructive 工具时，run 必须取得 workspace 写锁；锁被占用时保持 `running`，事件显示“等待工作区锁”。
- 已持写锁的 run 结束、取消或 daemon 恢复失败后释放锁。
- `file_read` 等 read 工具在写锁运行时允许执行，但读取结果必须携带文件版本/mtime，避免模型把过期内容作为写入依据。
- 终端和 Web 的确认具有同等优先级，但服务端对同一 `confirmation_id` 只接受第一个有效决定；后续决定返回已处理状态。

---

## 3. 身份、设备与工作区

### 3.1 设备配对

daemon 不保存浏览器聊天 Bearer token，也不使用 token URL 参数。首次连接使用一次性配对：

1. CLI 执行 `local-agent auth login`，展示短时、单次的配对码/URL。
2. 已登录 Web 用户确认设备名称、系统、CLI 版本和请求的最小能力。
3. Agent API 创建 `local_agent_device`，签发 scope 为该用户和该设备的 device refresh credential。
4. 当前 CLI 将 device credential 保存到仅当前用户可读的 `0600` 文件；系统 Keychain 适配器是后续增强项。
5. daemon 使用短期 device access token 通过 `Authorization` Header 建立 `wss://.../local-agent/ws`。

用户可在 Web 的“本地设备”页撤销设备。撤销后服务端关闭连接、拒绝新 claim；daemon 删除本地 credential 并停止接受远程任务。设备 credential 只允许注册、心跳、领取本设备 run、上报本设备事件和接收取消/确认，不能调用普通用户 API。

### 3.2 工作区注册

工作区必须由本机用户显式添加：

```bash
local-agent workspace add /home/zhouzw/agentWorkCluster/chat-server --name chat-server
```

daemon 以 `realpath` 解析根目录，拒绝不存在目录、符号链接逃逸和重叠的特权目录。服务端仅保存 `workspace_id`、展示名、设备 ID、策略版本和能力摘要；绝不保存绝对路径。绝对路径及允许/阻止规则仅保留在 daemon 本地配置中。

Web 创建 Local Agent 时只能选择在线设备及其注册工作区。若设备离线，允许创建 run，但必须明确显示“等待设备上线”，不可转交 Cloud Worker。

### 3.3 数据模型

在现有 `agents`、`agent_versions`、`runs` 基础上新增：

```text
agents
  execution_target ENUM('cloud', 'local') NOT NULL DEFAULT 'cloud'
  default_device_id UUID NULL
  default_workspace_id UUID NULL
  model_mode ENUM('server_proxy', 'local_direct') NOT NULL DEFAULT 'server_proxy'

local_agent_devices
  id UUID PK, owner_user_id BIGINT, credential_hash BYTEA
  display_name TEXT, hostname TEXT, platform TEXT, cli_version TEXT
  status ENUM('online', 'offline', 'degraded', 'revoked')
  capabilities JSONB, last_heartbeat_at TIMESTAMPTZ, created_at TIMESTAMPTZ

local_workspaces
  id UUID PK, device_id UUID FK, display_name TEXT
  policy_version INT, capabilities JSONB, created_at TIMESTAMPTZ

local_run_dispatches
  run_id UUID PK FK, device_id UUID FK, workspace_id UUID FK
  lease_id UUID NULL, lease_expires_at TIMESTAMPTZ NULL
  executor_state ENUM('pending', 'offered', 'claimed', 'disconnected', 'recovery_required')
  local_session_id TEXT NULL, last_acked_sequence INT NOT NULL DEFAULT 0
```

`runs.state` 保持现有状态机：`queued -> running -> waiting_confirmation -> completed|failed|cancelled`。设备离线、派发等待和 lease 过期记录在 `local_run_dispatches.executor_state`，不扩散新的用户可见终态。现有状态机定义在 [state_machine.py](../../agent-service/app/state_machine.py)。

---

## 4. 调度、协议与恢复

### 4.1 远程 run 生命周期

```text
Web/CLI 创建 run
  -> Agent API 事务写入 run + snapshot + local_run_dispatch(pending)
  -> daemon 在线：WSS run.offer；离线：等待心跳
  -> daemon claim(run_id, lease_id) 原子取得租约
  -> Agent API: queued -> running，持久化并转发事件
  -> 工具需要确认：waiting_confirmation
  -> 决定回传 daemon，继续运行或取消
  -> daemon complete/fail，Agent API 写终态
```

创建 run 时必须冻结以下 snapshot：Agent 版本、目标 device/workspace、模型模式、工具能力及版本、运行预算、系统提示词、会话版本。设备后续更新工具或工作区策略不改变已运行任务；但 daemon 在执行时仍可因本地策略更严格而拒绝该工具。

### 4.2 daemon WSS 消息

WSS 是 daemon 的唯一实时调度通道，所有消息含 `protocol_version`、`message_id`，并由服务端确认。重要消息：

| 方向 | 消息 | 要求 |
|---|---|---|
| daemon -> API | `hello` / `heartbeat` | 设备、版本、能力、当前负载；30 秒内未收到视为离线 |
| API -> daemon | `run.offer` | 只含本设备 snapshot、lease 候选和脱敏展示信息 |
| daemon -> API | `run.claim` | 带 `lease_id`，DB compare-and-set；重复 claim 幂等 |
| daemon -> API | `run.event` | `run_id + sequence + event_id` 单调且幂等 |
| API -> daemon | `confirmation.decision` | 绑定 confirmation ID 与 arguments hash |
| API -> daemon | `run.cancel` | daemon 取消模型请求并终止该 run 的进程组 |
| daemon -> API | `run.complete` / `run.fail` | 服务端验证租约、状态和最终 sequence 后转终态 |

HTTP 仅保留配对、设备/workspace 管理和 Web 查询；不提供“GET pending 后轮询执行”的普通用户端点。这样不暴露可被重放的任务内容，也避免两个 daemon 抢任务。

### 4.3 事件与确认

服务端继续使用现有 `agent_trace_event` 的顺序事件和 `/agent/ws` 浏览器补发机制。daemon 不可直接写任意事件类型；Agent API 校验该 run 的租约、允许事件集合、sequence 和 payload 上限。

写工具改为两阶段：

1. daemon 生成 `tool.requested`：`operation_id`、工具名、arguments hash、文件 precondition hash、diff/命令预览、风险级别。
2. Agent API 创建 confirmation，run 转为 `waiting_confirmation`，现有前端确认组件可复用。
3. Web 或拥有本机 IPC 权限的终端批准后，Agent API 发出绑定 hash 的决定。
4. daemon 再次验证 arguments、precondition 与本地策略，执行后上报 `tool.completed`；任一不匹配均创建新的确认。

当前前端已能处理 `agent.tool.confirmation_required` 和 run 事件 [AgentChat.svelte](../../chat-client/src/views/AgentChat.svelte)，但需增加“设备/工作区/发起端”和 diff 预览字段。

### 4.4 断线、取消与崩溃

- daemon 失联后，未 claim 的 run 保持 queued；已 claim 的 run 标记 `disconnected`，租约默认 90 秒。
- 同一 daemon 重连并给出 `local_session_id` 与 journal 摘要时，可续租并继续未完成的纯计算/读操作。
- 有副作用的 operation 若处于“已批准但未完成回报”，run 进入 `recovery_required`。不可自动重放；用户必须查看本机 journal 后重新确认或取消。
- 取消由服务端立即持久化，daemon 收到 `run.cancel` 后中止 AbortController 和子进程组，并在每次工具执行前检查取消状态。
- 服务端事件先持久化再推送，沿用当前实现的断点补发语义 [main.py](../../agent-service/app/main.py)。

---

## 5. 模型与 Harness

### 5.1 模型模式

| 模式 | 模型调用位置 | 密钥位置 | 适用场景 |
|---|---|---|---|
| `local_direct` | daemon 直连 OpenAI 兼容 API | 本机 AES-GCM 加密凭据库 | 低延迟、代码不经 Agent API 模型代理 |
| `server_proxy` | Agent API/Cloud Worker | 服务端加密存储 | 统一计费、现有云模型配置 |

禁止把服务端加密保存的 API Key 下发给 CLI。因此“复用云端模型连接”只能是 `server_proxy`，而不是 daemon 取得云端密钥。`local_direct` 时，Web 只显示模型显示名、提供方和连接状态，绝不读取本地密钥。

两种模式都必须在首次添加 workspace 时提示：文件内容和工具结果可能会发送给所选模型提供方；若任务同步至 Web，展示内容还会按 Agent API 的加密和审计规则保存。

### 5.2 Local Harness

daemon 内部维护轻量 Harness：

```text
Context: 系统提示词 + workspace 摘要 + 当前会话 + 已验证工具结果
Policy: 本地路径/命令/输出限制 + 工具次数 + workspace 锁
Model: OpenAI Chat Completions SSE adapter 或 server proxy adapter
Runtime: 结构化本地工具执行器
State: daemon journal；连接模式下与 Agent API run snapshot 对齐
Trace: 本地完整 journal；向服务端发送经策略脱敏的事件
```

终端与 Web 任务都调用同一 Harness。终端并不是第二个执行进程，而是 IPC 客户端；这保证同一个工作区不会被两个独立模型循环同时编辑。

---

## 6. 工具与本机安全策略

### 6.1 工具分级

| 工具 | 副作用 | 默认策略 |
|---|---|---|
| `file_read`、`file_glob`、`file_grep`、`file_tree` | read | 自动执行，结果大小受限 |
| `git_status`、`git_diff`、`git_log`、`process_list` | read | 自动执行 |
| `file_write`、`file_edit`、`file_move` | write | 每次 diff + per-call 确认 |
| `shell_exec`、`package_install`、`git_branch` | write | 结构化参数 + per-call 确认 |
| `file_delete`、`shell_spawn`、`process_kill`、`git_commit`、`git_push` | destructive | 强制 per-call 确认 |

服务端工具定义需增加 `execution_scope: server | device`。现有 `kind=local` 保留给服务端进程内工具，避免 daemon 工具被现有 `execute_local_tool()` 错误在容器内执行。`device` 工具仅是 capability 声明；最终的路径和命令校验永远在 daemon 内完成。

### 6.2 文件系统

- 每个请求路径经 `realpath`、父目录解析和 workspace root 前缀比较，任何符号链接逃逸均拒绝。
- 白名单 root 是授权依据；`/etc`、`/proc`、`~/.ssh` 等黑名单只是额外拒绝规则，不能替代 root 校验。
- 文件读取、grep、tree 分别限制 1 MiB、2,000 行、最大深度和排除目录；默认忽略 `.git`、依赖目录和二进制文件。
- 覆盖已有文件必须提供 unified diff；写入前校验读取时的 content hash，不一致则重新读取并重新确认。
- 删除 v1 只允许工作区内的普通文件，默认移动到工作区 `.local-agent-trash/`；递归目录删除推迟到后续版本。

### 6.3 命令执行

- 不使用 `sh -c`、`cmd.exe /c` 或 PowerShell 字符串解释。`shell_exec` 接收 `{program, args, cwd}`，使用 `spawn/execFile` 执行。
- program 必须在版本化 allowlist 内；禁止 `sudo`、解释器 `-c`、重定向、管道、后台 `&` 和未授权联网安装。
- 超时默认 30 秒，stdout/stderr 合计上限 256 KiB；超限杀死整个子进程组。
- 子进程仅继承最小环境变量集合，过滤 `SECRET`、`KEY`、`TOKEN`、`PASSWORD`、`AUTH`、`CREDENTIAL` 前缀；`env_read` 不进入 v1。
- `git push` 显示 remote、分支、commit 和 force 标志；永不自动 force push 或修改 git config。

### 6.4 审计与隐私

本地 journal 记录完整 operation 状态，用于恢复，仅当前 OS 用户可读并按保留期清理。上传的 trace 默认只包含工具名、路径相对名、hash、时长、退出码、截断且脱敏的摘要；用户可为某个 run 选择“同步完整输出到 Web”。服务端沿用加密字段和脱敏事件，而非记录原始密钥或环境变量。

---

## 7. CLI 与本机 IPC

### 7.1 命令

```text
local-agent daemon [--workspace <path>]
local-agent status
local-agent run <prompt> [--workspace <id>] [--offline] [--private]
local-agent chat [--workspace <id>]
local-agent runs
local-agent attach <run-id>
local-agent approve <confirmation-id>
local-agent reject <confirmation-id> [--reason <text>]
local-agent workspace add|list|remove
local-agent auth login|status|logout
```

`run`、`chat`、`runs`、`attach`、`approve` 均通过 Unix domain socket（Windows 为 named pipe）调用 daemon。socket 所属用户独占，并要求 `0600` 权限；不同 OS 用户不可借此控制设备。`run --offline` 不连接 daemon/服务端，明确显示“不会同步到 Web”。

### 7.2 终端和 Web 共同控制

- CLI 创建的联网 run 会立即将 `origin=terminal`、workspace、展示名和事件注册到 Agent API；Web 时间线显示其来源。
- Web 创建的 run 会在 `local-agent runs` 中出现；`attach` 可显示实时事件、输入本机确认、取消任务。
- 同一确认的竞争采用服务端原子决定；无论来源，结果都会广播到 Web 与所有 attach 终端。
- `--private` 允许终端任务不上传正文和工具输出，只上传运行状态；该模式下 Web 只能看到“本地私有任务正在占用工作区”，不能接管内容。

---

## 8. API 与前端需求

### 8.1 服务端 API

浏览器使用用户 Bearer token：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/local-agent/pairings` | 创建配对会话 |
| `POST` | `/api/v1/local-agent/pairings/{id}/approve` | Web 批准设备 |
| `GET` | `/api/v1/local-agent/devices` | 列出用户设备和工作区 |
| `DELETE` | `/api/v1/local-agent/devices/{id}` | 撤销设备 |
| `POST` | `/api/v1/local-agent/workspaces` | CLI 注册工作区（device credential） |
| `POST` | `/api/v1/local-agent/runs` | CLI 创建同步 run（device credential） |

daemon 使用 device credential：

| 通道 | 路径 | 说明 |
|---|---|---|
| `WSS` | `/local-agent/ws` | hello、心跳、offer、claim、事件、确认、取消 |
| `POST` | `/api/v1/local-agent/token/refresh` | 刷新短期设备 access token |

浏览器 Agent API 需在 `create_agent`/`create_run` 时验证 `execution_target=local` 的设备归属、workspace 归属及工具 capability。Caddy 与 Vite 代理增加 `/api/v1/local-agent*` 和 `/local-agent/ws*` 到 Agent API。现有浏览器事件通道 `/agent/ws` 保持不变。

### 8.2 前端体验

1. 创建 Agent 时先选择“云端 Agent / 本地 Agent”；选择本地后展示在线设备、工作区、模型模式和数据同步提示。
2. Agent Chat 顶栏显示设备在线状态、工作区、任务来源（Web/终端）与写锁占用。
3. 确认对话框展示相对路径、完整 diff 或结构化命令、风险、发起端及 precondition；无法校验时不显示批准按钮。
4. 运行记录可筛选 device、workspace、origin、同步级别和恢复状态。
5. 设备页支持配对、工作区列表、撤销和 last seen；不展示绝对本地路径。

---

## 9. 实施计划与验收

### Phase 0：本机安全执行内核

- Node.js ESM 项目放在总项目目录的 `local-agent/`，与 `chat-server/` 并列。
- daemon、IPC、workspace 注册、本地 journal。
- `file_read`、`file_glob`、`file_grep`、`file_edit`，结构化 `shell_exec`。
- `run --offline` 与本地模型配置；写入均在终端确认。

验收：两个 CLI 客户端同时连接 daemon；同一 workspace 的第二个写任务被正确阻塞，且任意路径逃逸、符号链接逃逸、shell 注入均被拒绝。

### Phase 1：设备配对与 Web 调度

- 数据迁移、device credential、WSS 连接、在线状态和 workspace UI。
- Local run 独立于 Redis Cloud Worker Stream 的 dispatch/claim/lease。
- daemon 事件上报，复用现有 Web trace 和事件重连。
- CLI 联网 run 与 Web `attach` 双向可见。

验收：Web 任务只被目标设备领取；终端任务可在 Web 看到；设备离线时任务不被 Cloud Worker 消费，上线后正确执行一次。

### Phase 2：确认、恢复与取消

- 两阶段 write protocol、diff/precondition、双端原子确认。
- run cancel 推送、进程组终止、租约和 daemon journal 恢复。
- `recovery_required` UI 与人工处理流程。

验收：Web 与终端同时决定确认时只执行一次；daemon 在批准后崩溃不会重放写入；取消能在超时内停止子进程。

### Phase 3：Git、REPL 与评估

- `chat`、`attach`、Git read/write 工具、私有同步模式。
- Local run fixture、路径安全、协议幂等、断线恢复和 UI 端到端测试。
- 将 Local Agent 场景加入现有评估体系，但不把本机文件样本上传到生产评估。

---

## 10. 最终决策

1. **daemon 是连接后的唯一执行器**；终端是本机 IPC 控制端，Web 是远程控制端，两者可同时使用。
2. **Local run 不进入 Cloud Worker Redis Stream**；按目标设备通过 daemon WSS 派发并使用租约。
3. **模型密钥按位置隔离**：本地直连密钥只留在 daemon 的本机加密凭据库；服务端密钥只供 server proxy 使用。系统 Keychain 适配器是后续增强项。
4. **工作区路径只留在本机**；服务端持有设备、工作区 ID 和展示元数据。
5. **写/破坏性操作始终两阶段确认且不自动重放**；终端或 Web 均可决定，但服务端保证一次性结果。
