# Local Agent CLI 详细技术设计

> 对应需求：[local-agent-design.md](./local-agent-design.md)
>
> 状态：v1 实现设计
>
> 日期：2026-07-24

## 实施进度

> 更新日期：2026-07-24
>
> 当前状态：已完成本地 daemon/IPC、安全状态文件保护、基础配对、`local_direct` 本机凭据库、本机文本 Harness 及 daemon WSS 调度；前端和文件/进程工具尚未实现，仍不应作为生产远程执行功能开放。

| 项目 | 状态 | 交付内容 |
|---|---|---|
| 本机 daemon 与 CLI | 已完成（基础版） | 单实例锁、`0600` Unix socket JSON-RPC、工作区注册/控制面同步、append-only journal、重启后的 run 摘要恢复、`run.create/list/cancel`。 |
| 本机安全边界 | 已完成（基础版） | 私有状态目录/文件权限验证、`realpath` 路径包含检查、符号链接逃逸与 FIFO/设备/socket 拒绝、8 KiB carry 的流式脱敏。 |
| 本机模型凭据与 `local_direct` | 已完成（基础版） | daemon IPC 接收模型 key，以 device refresh credential 派生密钥进行 AES-256-GCM 加密本地存储；服务端仅登记模型元数据，Local run snapshot 不含模型 key。 |
| 服务端 Local 控制面 | 已完成（基础版） | 设备、工作区、Local run dispatch 表和 Alembic migration；Local run 不会写入 Cloud Worker outbox。已实现 WSS offer/claim/90 秒租约、过期 offer 重派发和有序事件上报。 |
| 前端控制面 | 未完成 | 仅有 REST API；没有 Agent 设置页、设备/workspace 选择器或 Local run 展示。 |
| 设备配对与设备凭据 | 已完成（基础版） | 10 分钟单次配对、CLI pairing secret、批准 API、refresh credential 哈希存储与 10 分钟 device access token；撤销设备立即使现有 access token 失效。 |
| daemon WSS、租约与事件上报 | 已完成（基础版） | daemon 以设备 access token 的 `Authorization` Header 建立出站 WSS，刷新 token 后自动重连；已实现 offer/claim、租约续期、取消信号和单调 sequence 事件上报。确认决定、断线中已领取 run 的恢复和事件持久幂等键仍未实现。 |
| Harness、工具确认与恢复 | 部分完成 | CLI 的 `run --agent` 可调用本机配置的 OpenAI 兼容文本模型并记录脱敏事件；尚未执行文件/进程工具、确认或恢复。 |

已运行 Local Agent 定向测试和 Agent API 全量 unittest；尚未进行端到端、迁移演练或生产发布。

## 1. 设计摘要

Local Agent 是宿主机上的长期运行 daemon，不是一个被 Web 和终端分别启动的第二套云 Agent。daemon 是本机唯一的执行仲裁者：

```text
Web AgentChat ── REST + /agent/ws ── Agent API
                                      │
Terminal CLI ── Unix socket/pipe ── Local daemon ── Model / File / Process
                                      │
                                      └── WSS（daemon 主动连接）
```

Web 和终端可以同时控制同一个 run。服务端保存用户可见的 run、confirmation 和 trace；daemon 保存绝对路径、模型密钥、本机完整 journal 和进程句柄。Cloud Worker 只处理 `execution_target=cloud` 的任务，Local run 不进入现有 `agent-runs` Redis Stream。

本设计重点解决四类风险：

1. Web 与 CLI 启动两个 Harness 导致的工作区竞态。
2. 设备离线、WSS 重连或进程崩溃导致的任务重复执行。
3. 模型生成危险路径、命令或子进程，突破宿主机边界。
4. stdout、stderr、文件内容和模型结果中的凭据泄露。

## 2. 信任边界

```text
┌──────────────────────────────────────────────────────────────┐
│ Browser                                                     │
│ 用户 token；可见脱敏事件；不能读取本地绝对路径或设备密钥       │
└───────────────────────┬──────────────────────────────────────┘
                        │ HTTPS/WSS
┌───────────────────────▼──────────────────────────────────────┐
│ Agent API / PostgreSQL / Redis                               │
│ 用户鉴权、设备凭据哈希、run 状态、确认、审计事件               │
│ 不拥有工作区文件系统；server_proxy 时可调用模型               │
└───────────────────────┬──────────────────────────────────────┘
                        │ daemon outbound WSS
┌───────────────────────▼──────────────────────────────────────┐
│ Local daemon（当前 OS 用户权限）                             │
│ 本地加密凭据库、绝对路径、workspace policy、完整 journal       │
│ 经过 realpath、argv、sanitizer、process-group 的工具边界        │
└───────────────────────┬──────────────────────────────────────┘
                        │
              authorized workspace / child processes
```

### 2.1 必须明确的隐私事实

- `local_direct` 只代表模型 API Key 不离开本机，不代表源代码不会发给模型提供方。
- 同步到 Web 的 run 内容会按 Agent API 的加密和审计策略存储；`--private` 只能关闭正文/工具输出同步，不能让 Web 控制该 run。
- 服务端保存 workspace ID 和展示名，不保存绝对路径；路径只在 daemon journal 和本地配置中出现。
- 本地日志脱敏是出站防线，不是安全证明。工具本身必须尽量避免读取凭据，模型系统提示词也必须禁止索取未授权秘密。

`local_direct` 的 API Key 仅由 daemon 通过本机 IPC 接收，并写入权限为 `0600` 的 AES-256-GCM 加密凭据库；加密密钥由本机 device refresh credential 通过 scrypt 派生。服务端仅登记 device、Agent、base URL 和 model ID，直接模式 run snapshot 不携带 API Key。系统 Keychain 适配器仍是后续增强项，不得将本基础实现描述为硬件或 OS 凭据隔离。

## 3. 进程与模块设计

### 3.1 Node.js 项目布局

```text
local-agent/
├── package.json
├── bin/local-agent.js
├── src/
│   ├── cli.js                 # commander 入口和输出渲染
│   ├── daemon.js              # 生命周期、优雅退出、单实例锁
│   ├── ipc-server.js          # Unix socket / named pipe JSON-RPC
│   ├── ipc-client.js          # run/chat/attach/status 的本机客户端
│   ├── transport.js            # WSS、token refresh、重连和消息确认
│   ├── registry-client.js      # 配对、设备、workspace 注册
│   ├── model-store.js           # 本机加密模型凭据库
│   ├── dispatch-client.js      # offer/claim/lease/cancel/complete
│   ├── run-manager.js          # run 状态、事件 sequence、控制端订阅
│   ├── harness.js              # Context、Model、Tool、Policy 主循环
│   ├── model/
│   │   ├── openai-sse.js       # OpenAI Chat Completions 兼容协议
│   │   └── proxy.js             # server_proxy 适配器
│   ├── workspace.js             # realpath、策略和 workspace 锁
│   ├── journal.js               # SQLite/JSONL 本地持久化和恢复
│   ├── sanitizer.js             # 流式 stdout/stderr/事件脱敏
│   ├── process-tree.js          # 跨平台进程组启动和终止
│   ├── git-checkpoint.js        # checkpoint、manifest、rollback
│   ├── policy.js                # 工具/路径/命令/输出限制
│   └── tools/
│       ├── registry.js          # capability 与版本
│       ├── file.js
│       ├── search.js
│       ├── shell.js
│       └── git.js
└── test/
    ├── protocol/
    ├── security/
    ├── recovery/
    └── fixtures/
```

### 3.2 daemon 生命周期

1. 获取 `daemon.lock`，发现已有实例时 CLI 只连接现有 IPC，不创建第二实例。
2. 加载本地 device credential、加密模型凭据库和 workspace policy；拒绝权限过宽的 config、socket、journal 文件。
3. 启动 IPC server，注册设备和 workspace capability。
4. 连接 Agent API WSS；连接未建立时仍可执行 `--offline` 或本地已接受的 private run。
5. 启动 heartbeat、lease watchdog、journal flusher、进程回收器和内存压力保护。
6. 收到 SIGINT/SIGTERM 时停止接受新 run，通知服务端 `daemon.draining`，等待安全终止或按策略取消活动 run，再关闭 socket。

daemon 必须支持优雅恢复：启动时扫描 journal 中的 `claimed`、`tool.requested`、`tool.approved`、`tool.running` 记录，向服务端上报 session recovery；任何副作用操作的状态不确定时进入 `recovery_required`，不自动调用工具。

## 4. 本机 IPC 设计

### 4.1 传输与鉴权

- Linux/macOS 使用 Unix domain socket；Windows 使用 named pipe。
- socket/pipe 只允许当前 OS 用户访问；Unix 文件模式为 `0600`，父目录为 `0700`。
- 每次连接先发送 `hello`，daemon 返回协议版本、daemon ID、可用 workspace 和当前 run 摘要。
- IPC 不接受用户输入中的任意代码作为命令；所有操作均为固定 JSON-RPC 方法。
- CLI 不保存 Web token。CLI 只通过 daemon 间接访问 Agent API。

### 4.2 JSON-RPC 方法

```json
{"jsonrpc":"2.0","id":"r-1","method":"run.create","params":{
  "prompt":"检查这个模块的测试覆盖率",
  "workspace_id":"ws_123",
  "sync":"full",
  "origin":"terminal"
}}
```

核心方法：

| 方法 | 权限 | 说明 |
|---|---|---|
| `daemon.status` | read | 连接、版本、负载、workspace、run |
| `run.create` | write | 创建联网或 private run，返回 run ID |
| `run.list` | read | 列出当前用户可 attach 的 run |
| `run.attach` | read | 订阅 run 事件和确认 |
| `run.cancel` | write | 取消 run；服务端状态优先 |
| `confirmation.decide` | write | 提交本机确认，服务端原子裁决 |
| `workspace.list` | read | 返回本机 workspace ID 和展示名 |
| `workspace.add` | write | 解析并注册路径 |
| `workspace.remove` | write | 停止该 workspace 的新任务 |

IPC 事件使用同一 envelope：

```json
{
  "protocol_version": 1,
  "type": "run.event",
  "message_id": "m_01J...",
  "run_id": "run_123",
  "sequence": 17,
  "payload": {"event_type":"agent.tool.completed", "payload":{}}
}
```

CLI 断开后，daemon 继续执行；重新 `attach` 时先发送本地 journal 中的最后 sequence，再补发缺失事件。IPC 客户端不能通过修改 sequence、run_id 或 confirmation hash 伪造结果。

## 5. Agent API 与 WSS 协议

### 5.1 设备配对

配对码必须单次使用、有效期不超过 10 分钟，并绑定发起设备的随机 nonce。流程如下：

```text
CLI                 Agent API                 已登录 Web
 | POST pairing       |                         |
 |<-- code + URL -----|                         |
 |                    |<----- GET pending -----|
 |                    |<-- approve + scopes ---|
 |<-- device refresh -|                         |
```

服务端只保存 credential hash，不保存可解密的 refresh token。基础版将 CLI 已持有的 pairing secret 作为 device refresh credential，批准时仅将其 hash 绑定到 device；claim 响应不回传 refresh token。access token 有效期为 10 分钟。daemon 在建立或重连 WSS 前刷新 access token；refresh token 轮换仍是后续增强。WSS 必须使用 `Authorization: Bearer <device-access-token>` Header，不使用 query string token。

### 5.2 WSS envelope

每条消息包含 `protocol_version`、`message_id`、`type`、`sent_at`。需要持久化的消息必须包含 `run_id` 和幂等键。服务端对收到的消息先鉴权、再校验租约、最后持久化。

#### daemon -> API：hello

```json
{
  "type":"hello", "message_id":"m1", "protocol_version":1,
  "device_id":"dev_123", "cli_version":"0.1.0",
  "platform":"linux", "node_version":"22.0.0",
  "workspaces":[{"id":"ws_123","name":"chat-server","policy_version":3}],
  "capabilities":[{"name":"file_edit","version":"1"}]
}
```

服务端返回当前策略版本、待派发 run、需要取消的 run 和 token refresh 时间。capability 是声明，不是授权；run snapshot 中未包含的工具即使 hello 声明也不能执行。

#### API -> daemon：run.offer

```json
{
  "type":"run.offer", "message_id":"m2", "run_id":"run_123",
  "lease_id":"lease_123", "lease_expires_at":"2026-07-24T12:00:00Z",
  "snapshot":{
    "agent_version":4, "workspace_id":"ws_123", "model_mode":"local_direct",
    "tools":[{"name":"file_edit","version":"1","side_effect":"write"}],
    "policy":{"max_tool_calls":6,"max_output_bytes":262144}
  }
}
```

daemon 不能仅凭收到 offer 就执行，必须先用 `run.claim` 成功取得租约。拒绝原因包括 workspace 不存在、能力版本不匹配、本地策略更严格或并发已满。

#### daemon -> API：run.claim

```json
{"type":"run.claim","message_id":"m3","run_id":"run_123",
 "lease_id":"lease_123","local_session_id":"sess_456"}
```

服务端使用 `UPDATE ... WHERE executor_state IN ('pending','offered') AND lease_id = ?` 进行 compare-and-set。重复 claim 返回原结果；不同 device 或过期 lease 一律拒绝。

#### daemon -> API：run.event

```json
{"type":"run.event","message_id":"m4","run_id":"run_123",
 "lease_id":"lease_123","sequence":8,"event_id":"evt_8",
 "event_type":"agent.tool.requested",
 "payload":{"operation_id":"op_1","tool":"file_edit",
            "arguments_hash":"sha256...","summary":"修改 src/a.js"}}
```

服务端对 `(run_id, sequence)` 和 `event_id` 建唯一约束。重复发送返回 ACK，不重复生成浏览器事件；sequence 跳跃时返回 `event.resync_required`，daemon 从本地 journal 补发。

### 5.3 租约与心跳

- lease 默认 90 秒；daemon 每 15 秒发送 heartbeat 或 `lease.renew`。
- 服务端在 3 个 heartbeat 周期内未收到 renew，标记 device offline 并停止发送新 offer。
- lease 到期不自动把 run 交给另一个设备，除非用户显式选择“重新调度”且 run 尚未产生不确定副作用。
- 一个 run 的 lease、workspace 写锁和 `local_session_id` 都必须在同一事务中关联。

## 6. Run、Operation 与 journal 状态

### 6.1 服务端状态

`runs.state` 沿用现有状态：

```text
queued -> running -> waiting_confirmation -> running
   |          |                  |
   +----------+------------------+--> failed/cancelled
                         running --------> completed
```

设备派发状态单独记录：

```text
pending -> offered -> claimed -> disconnected
                         |             |
                         +-------------+--> recovery_required
```

`recovery_required` 是 executor 状态，不是普通 run 终态。只有用户确认已核对本机状态后，run 才能 `failed`、`cancelled` 或创建新的 operation。

### 6.2 daemon 本地 journal

建议使用 SQLite，启用 WAL、`foreign_keys=ON` 和单写者队列。核心表：

```sql
CREATE TABLE local_runs (
  run_id TEXT PRIMARY KEY,
  local_session_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN ('web','terminal')),
  sync_mode TEXT NOT NULL CHECK(sync_mode IN ('full','redacted','private')),
  state TEXT NOT NULL,
  last_sequence INTEGER NOT NULL DEFAULT 0,
  lease_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE operations (
  operation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES local_runs(run_id),
  tool_name TEXT NOT NULL,
  side_effect TEXT NOT NULL,
  arguments_hash TEXT NOT NULL,
  precondition_hash TEXT,
  state TEXT NOT NULL,
  pid INTEGER,
  started_at TEXT,
  completed_at TEXT,
  result_hash TEXT
);

CREATE TABLE journal_events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  uploaded_at TEXT,
  UNIQUE(run_id, sequence)
);

CREATE TABLE checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  base_head TEXT,
  manifest_json TEXT NOT NULL,
  patch_path TEXT,
  untracked_archive_path TEXT,
  created_at TEXT NOT NULL
);
```

`journal_events` 保存完整本地 payload，上传前由 sanitizer 生成另一份脱敏 payload；服务端永远不能要求 daemon 上传完整 journal。

### 6.3 幂等规则

- 所有工具调用必须有稳定 `operation_id`；同一 operation 只允许一个 terminal result。
- read operation 可在未完成时重试，但需生成新 attempt 并重新记录观察结果。
- write/destructive operation 的 `approved`、`running`、`completed` 只能单向前进；daemon 重启后只能报告不确定，不得自动进入 running。
- `run.complete` 只接受 daemon 已上报的 final sequence；服务端不会因一条 complete 消息补写缺失事件。

## 7. Harness 执行循环

### 7.1 上下文与状态

每次模型调用从 journal 和服务端 snapshot 组装：

```text
system prompt
authoritative run status JSON
workspace display name（不含绝对路径）
recent user/assistant/tool messages
recent tool observations（带 content hash / mtime）
available device tool declarations
```

绝对路径只有在 daemon 给本地模型的上下文中使用；上传 Web 的事件转换为相对路径或 basename。`--private` 模式仍需在本地上下文中包含绝对路径，否则工具无法工作。

### 7.2 伪代码

```text
execute(run):
  verify_claim_and_snapshot(run)
  context = prepare_context(run, journal, workspace)
  while budget.allows():
    check_cancelled()
    turn = model.stream(context, tool_declarations)
    emit_text_deltas(sanitize(turn.text))

    if no tool calls:
      persist assistant final
      complete(run)
      return

    for call in turn.tool_calls:
      validate_schema_and_policy(call)
      if call.side_effect == read:
        result = execute_read(call)
        append_observation(result)
        continue

      lock_workspace(call.workspace_id)
      checkpoint = create_checkpoint_if_needed(run)
      operation = request_confirmation(call, checkpoint)
      wait_for_server_or_terminal_decision(operation)
      verify_hashes_and_preconditions(operation)
      result = execute_write_once(operation)
      append_observation(result)

  fail(run, "tool/model budget exceeded")
```

模型流必须支持 abort signal；收到 cancel、workspace revoke、policy violation 或超时后立即停止流式请求。

## 8. 工作区并发与锁

### 8.1 锁实现

daemon 内使用按 workspace ID 的异步读写锁：

- read lock 允许最多 `max_read_runs` 个 run。
- write lock 排斥所有新的 write operation；已经执行的 read operation 不强制中断。
- lock owner 是 `run_id + operation_id`，释放操作必须幂等。
- daemon 崩溃后锁从 journal 恢复为“未知”，所有原持锁 write operation 进入 recovery_required，不恢复为可执行状态。

服务端不把 workspace 锁当作跨设备分布式锁。一个 workspace 只注册到一个 device；迁移设备必须先停止旧设备、确认无活动 run，再更新绑定。

### 8.2 乐观版本检查

`file_read` 返回 `path_relative`、`content_hash`、`size`、`mtime_ns`。`file_edit` 的 operation 携带读取时 hash。执行前若 hash/mtime 改变，daemon 拒绝执行并要求模型重新读取；不能用“用户仍已批准”绕过过期的 precondition。

## 9. 文件与命令工具实现

### 9.1 路径解析

所有工具统一调用：

```text
resolvePath(input, workspaceRoot):
  reject NUL byte and invalid UTF-8
  candidate = realpath(parent(input)) + basename(input)
  reject if candidate is not inside realpath(workspaceRoot)
  reject blocked mount / device / special file
  return candidate
```

不存在的新文件只能在已存在且已授权的父目录中创建；不能以 `realpath` 失败为理由直接放行原始字符串。读取和写入都要检查符号链接，不能只在写操作检查。

### 9.2 结构化 shell

```json
{
  "program":"npm",
  "args":["test","--","src/a.test.js"],
  "cwd":"workspace-relative-dir",
  "timeout_ms":30000
}
```

策略顺序：schema 校验 -> program allowlist -> 参数规则 -> cwd 解析 -> env 最小化 -> spawn -> 输出限制 -> 退出码归一化。`program` 必须以 basename 匹配 allowlist，并可配置绝对可执行文件 hash；不要把 `npm`、`node` 这类解释器的 `-e`、`-c` 参数加入 allowlist。

## 10. 进程组、超时与取消

### 10.1 Unix

使用 Node `spawn(program, args, {cwd, env, shell:false, detached:true})`，记录真实 PID。取消时按以下顺序：

1. 发送 SIGTERM 到负 PID（进程组）。
2. 等待 2 秒，读取组内存活进程。
3. 仍存活则发送 SIGKILL 到负 PID。
4. 关闭 stdout/stderr，记录 `termination_reason` 和存活检查结果。

必须先验证 PID 属于本 operation 的 process group，防止 PID 复用误杀其他任务。`detached` 只是建立新组，不等于自动清理；所有路径都需要 watchdog。

### 10.2 Windows

使用 `detached` 配合 Job Object；若 Node 版本/库不支持可靠 Job Object，使用受控 `taskkill /PID <pid> /T /F`，并核验进程映像和父子树。不得仅调用 `child.kill()`，否则会留下 dev server、Python worker 等后代进程。

### 10.3 僵尸回收

daemon 启动时扫描 journal 中未完成 PID，检查 process start time 与记录是否一致。属于本 daemon 的进程组先终止，再将 operation 标为 recovery_required；不通过 PID 号单独判断归属。

## 11. Git Checkpoint 与 Rollback

### 11.1 原则

Git checkpoint 是恢复工具，不是无条件的自动回滚。daemon 不 checkout 隐藏分支、不改变用户当前分支，也不覆盖 checkpoint 创建后的人工修改。

### 11.2 checkpoint 内容

在 run 首次取得 workspace write lock 后创建 checkpoint：

1. 记录 repository root、当前 `HEAD`、index hash 和 status manifest。
2. 保存 `git diff --binary HEAD` 与 staged diff。
3. 保存 untracked 文件的相对路径和内容到仅本机可读的 archive；忽略 blocked/大文件。
4. 可选创建 `refs/local-agent/checkpoints/<run-id>` 指向当时的 HEAD，供审计定位，但不 checkout。
5. 在 journal 中写入 checkpoint hash、文件清单和创建时间。

非 Git workspace 使用受限的 manifest + binary patch/archive；不宣称支持任意文件系统的原子回滚。

### 11.3 rollback 命令

```text
local-agent rollback <run-id>
```

执行前必须：

- workspace 取得独占写锁；
- 当前 manifest 与 run 的最终 manifest 匹配，若用户已经继续修改则拒绝；
- 用户在终端或 Web 显式确认；
- 生成 rollback operation 和新审计事件。

回滚 tracked 文件使用反向 binary patch，恢复 untracked 文件使用 archive；删除 run 后新增的文件前再次确认。rollback 失败时不继续尝试覆盖，进入 `rollback_failed` 并保留临时文件供人工恢复。

## 12. 动态日志脱敏

### 12.1 分层策略

1. **源头最小化**：子进程不继承敏感环境变量；工具不支持读取任意环境变量；模型提示词禁止要求 secret。
2. **流式 sanitizer**：stdout、stderr、模型 delta 和工具结果在 daemon 内先脱敏，再决定是否上传。
3. **上传策略**：默认上传摘要、退出码、耗时和截断结果；`sync=full` 必须在首次 run 创建时明确同意。
4. **本地完整日志**：完整输出只写入本地 journal，受权限和保留期保护。

### 12.2 sanitizer 规则

检测并替换：

- `Authorization: Bearer ...`、Cookie、Basic auth；
- JWT、PEM 私钥、云厂商 access key、数据库 URL 密码段；
- `KEY=...`、`TOKEN=...`、`PASSWORD=...` 等 key-value；
- 用户配置的 secret literal 哈希表（只在本机存储 hash 和短前缀）；
- 路径中的 home 目录前缀、workspace 绝对路径和 Unix socket 路径。

sanitizer 采用增量处理，保留末尾 8 KiB carry buffer 处理跨 chunk 的 token；输出限长在脱敏之后计算。规则命中应只产生 `redaction_count` 和类别，不把原文写入上传失败日志。误报时用户可查看本地 journal，但不能通过 Web 要求 daemon 回传原文。

### 12.3 限制

正则无法识别所有秘密。生产策略不得把“未命中规则”等同于“无敏感信息”；模型调用和 `sync=full` 页面必须持续展示数据外发风险。

## 13. 确认与恢复协议

### 13.1 两阶段 write operation

```text
prepared -> requested -> waiting_confirmation
                         | approve
                         v
                       approved -> running -> completed
                         |
                         +------> rejected/cancelled
```

`requested` payload：

```json
{
  "operation_id":"op_1",
  "tool":"file_edit",
  "side_effect":"write",
  "arguments_hash":"sha256(arguments)",
  "precondition_hash":"sha256(files-at-read)",
  "preview":{"path":"src/a.js","diff":"..."},
  "approval_expires_at":"2026-07-24T12:10:00Z"
}
```

批准时服务端校验 run、confirmation、用户、hash 和过期时间，使用唯一更新：`state=pending -> approved|rejected`。daemon 收到批准后必须再次比较 arguments/precondition；任何差异都回报 `confirmation_invalidated`。

### 13.2 崩溃判断

| journal 最后状态 | 恢复动作 |
|---|---|
| `prepared/requested` | 可重新等待同一 confirmation，但需确认仍未过期 |
| `approved` 未启动 | 标为 recovery_required，默认要求重新批准 |
| `running` | 标为 recovery_required；检查文件/进程/journal，不能重放 |
| `completed` | 发送缺失完成事件，幂等结束 |
| read `running` | 可新建 attempt 重新读取 |

## 14. API、数据库与现有平台改造

### 14.1 API 端点

浏览器用户 token：

```text
POST   /api/v1/local-agent/pairings
POST   /api/v1/local-agent/pairings/{id}/approve
GET    /api/v1/local-agent/devices
DELETE /api/v1/local-agent/devices/{id}
GET    /api/v1/local-agent/devices/{id}/workspaces
POST   /api/v1/agents/{agent_id}/local-bind
POST   /api/v1/agent-conversations/{conversation_id}/runs
```

device token：

```text
POST   /api/v1/local-agent/token/refresh
POST   /api/v1/local-agent/workspaces
POST   /api/v1/local-agent/runs
WSS    /local-agent/ws
```

`POST /agent-conversations/{id}/runs` 必须在同一事务内创建 run snapshot，并根据 `execution_target`：

- cloud：写入 cloud outbox，由现有 Worker 消费；
- local：写入 `local_run_dispatches`，通知已连接的目标 daemon，不写 cloud outbox。

现有 `enqueue_run()` 和 Worker 的 Redis Stream 逻辑需要按目标拆分；不可在 Worker 内通过“发现 agent_type=local 后跳过”补救，因为那会造成消息已被错误消费和 pending 恢复冲突。

### 14.2 数据迁移

新增 migration：

1. `agents.execution_target`、`default_device_id`、`default_workspace_id`、`model_mode`。
2. `local_agent_devices`、`local_workspaces`、`local_run_dispatches`、`pairing_sessions`。
3. `runs.origin`、`runs.sync_mode`、`runs.executor_state`，或等价的 dispatch 表字段。
4. `agent_trace_events` 增加 `source`、`event_id` 唯一索引和 payload size 校验。
5. 工具定义增加 `execution_scope`、`capability_version` 和 `workspace_required`。

迁移期间所有既有 Agent 默认 `execution_target=cloud`，不改变现有行为。

### 14.3 网关和前端代理

Caddy 和 Vite 需要将 `/api/v1/local-agent*`、`/local-agent/ws*` 路由到 Agent API。浏览器订阅 run 仍使用现有 `/agent/ws`；daemon 的 WSS 与浏览器通道必须分离，不能允许浏览器伪装 device hello。

## 15. 前端详细需求

### 15.1 Local Agent 设置

- 设备列表：名称、平台、CLI 版本、在线时间、撤销按钮。
- workspace 列表：展示名、设备、策略版本；不显示绝对路径。
- 模型模式：`local_direct` 或 `server_proxy`，显示密钥存储位置和数据流向。
- 工具 capability：名称、版本、read/write/destructive、是否可用；服务端只展示，不把本地绝对命令路径暴露给普通用户。

### 15.2 Chat 和 Runs

- 顶部显示 `device / workspace / origin / sync_mode / lock`。
- 等待设备显示 queued；等待写锁显示 running + lock reason；设备失联显示 degraded。
- confirmation 卡片显示相对路径、diff/argv、precondition、风险、发起端和过期时间。
- `recovery_required` 必须阻止批准新的副作用操作，提供查看状态、重新确认、取消和 rollback 入口。
- 浏览器断线使用现有 `after_sequence` 续传；事件重复时按 `(run_id, sequence)` 去重。

## 16. 可观测性与运维

服务端和 daemon 都输出结构化日志，但默认不输出 prompt、绝对路径、arguments 原文和原始 stdout。指标至少包括：

```text
local_device_online
local_ws_reconnect_total
local_run_queue_age_seconds
local_run_claim_total / claim_conflict_total
local_run_lease_expired_total
workspace_lock_wait_seconds
tool_confirmation_age_seconds
operation_recovery_required_total
process_group_kill_total
sanitizer_redaction_total{category}
event_resync_total
```

告警条件：设备频繁重连、lease 大量过期、recovery_required 未处理、进程组杀不干净、事件 sequence 持续 resync、sanitizer 命中异常增长。

## 17. 测试设计

### 17.1 单元测试

- realpath：`..`、Unicode、符号链接、dangling symlink、大小写不敏感文件系统。
- argv policy：解释器参数、NUL、超长参数、blocked program、cwd 越界。
- sanitizer：跨 chunk token、JWT/PEM、环境变量、路径替换、误报和限长。
- process-tree：超时、SIGTERM/SIGKILL、后代进程、PID 复用保护。
- checkpoint：tracked/staged/untracked、二进制文件、当前状态已变化时拒绝 rollback。

### 17.2 协议与故障测试

- offer 重复、claim 竞争、lease 过期、token refresh 轮换。
- event 重复/乱序/跳序、服务端 ACK 丢失、daemon 重连补发。
- Web 与 terminal 同时 approve/reject，只有一个决定生效。
- daemon 在 approved、running、completed 各阶段被强制终止。
- Cloud Worker 不消费 local run；离线设备上线后只执行一次。

### 17.3 端到端验收

1. Web 发起 `file_edit`，终端 `attach` 能看到 diff，终端批准后 Web 收到 completed。
2. 终端发起联网 run，Web 能看到 origin=terminal，并从 Web 取消；子进程组无残留。
3. 两个终端同时编辑同一 workspace，只有一个取得写锁，另一个等待并最终得到一致结果。
4. stdout 中包含模拟 Token、JWT、绝对路径时，服务端只收到脱敏内容。
5. daemon 在批准后崩溃，重启不自动重放，Web 显示 recovery_required，并能通过 checkpoint rollback 恢复。

## 18. 分阶段交付

### Phase 0：daemon 与安全内核

完成单实例 daemon、IPC、workspace realpath、read 工具、结构化 shell、process-tree、journal、sanitizer 和 `run --offline`。

### Phase 1：设备连接与双控制面

完成 pairing、device credential、WSS、local dispatch/claim/lease、CLI 联网 run、Web attach 和 Cloud/Local 队列隔离。

### Phase 2：副作用与恢复

完成两阶段 confirmation、workspace 写锁、Git checkpoint/rollback、cancel 进程组清理、recovery_required 流程。

### Phase 3：完善体验

完成 REPL、Git 工具、private/redacted/full 同步模式、多 workspace 迁移、评估 fixture 和运维指标。

## 19. 未决决策

以下决策不影响 Phase 0，但在 Phase 1 前必须冻结：

1. Agent API 是继续使用现有 Python 服务，还是拆出独立 Local Control Service。
2. daemon 本地 journal 使用 SQLite 还是加密 JSONL；建议 SQLite + WAL。
3. `server_proxy` 是否允许服务端把本地工具结果发送到云模型；建议创建 Agent 时强制再次确认。
4. Windows Job Object 使用自研 Node 原生模块还是经过审计的第三方库。
5. Git rollback 是否在 v1 开放给 Web；建议先支持 CLI，并在完整 manifest 校验后开放 Web。
